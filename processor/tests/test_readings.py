import json

import boto3
import pytest
from botocore.exceptions import ClientError
from jsonschema import ValidationError
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from processor import readings
from processor.contracts import CONTRACTS_DIR, validator

NOW = 1794233042
MESSAGE = json.loads((CONTRACTS_DIR / "sqs-message.example.json").read_text())


def test_example_matches_schema():
    example = json.loads((CONTRACTS_DIR / "readings-item.example.json").read_text())
    validator("readings-item").validate(example)


def test_message_maps_to_one_item_per_sensor():
    items = readings.to_items(MESSAGE, NOW)

    assert items == [
        {
            "sensorId": "engine-1.chamber_temp",
            "second": 1794233041,
            "offsets": [0, 100],
            "values": [3303.3, 3294.1],
            "unit": "K",
            "phase": "ascent",
            "flightId": "20261109T140300Z-7f3a",
            "ttl": NOW + 86400,
        },
        {
            "sensorId": "engine-1.tank_pressure",
            "second": 1794233041,
            "offsets": [100],
            "values": [340.4],
            "unit": "kPa",
            "phase": "ascent",
            "flightId": "20261109T140300Z-7f3a",
            "ttl": NOW + 86400,
        },
    ]
    for item in items:
        validator("readings-item").validate(item)


def test_message_breaking_the_contract_is_rejected():
    with pytest.raises(ValidationError, match="schemaVersion"):
        readings.to_items({**MESSAGE, "schemaVersion": 2}, NOW)


def test_unknown_sensor_type_is_rejected():
    bad = {
        **MESSAGE,
        "readings": {"engine-1.bogus": [{"ts": 1794233041000, "value": 1}]},
    }
    with pytest.raises(ValidationError, match="unknown sensor type"):
        readings.to_items(bad, NOW)


@pytest.mark.parametrize("ts", [1794233040999, 1794233042000])
def test_offset_outside_the_second_is_rejected(ts):
    bad = {**MESSAGE, "readings": {"engine-1.chamber_temp": [{"ts": ts, "value": 1.0}]}}
    with pytest.raises(ValidationError):
        readings.to_items(bad, NOW)


@pytest.fixture(scope="module")
def table():
    with (
        DockerContainer("amazon/dynamodb-local:2.5.4")
        .with_exposed_ports(8000)
        .with_command("-jar DynamoDBLocal.jar -inMemory")
        .waiting_for(LogMessageWaitStrategy("Initializing DynamoDB Local")) as container
    ):
        endpoint = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8000)}"
        env = pytest.MonkeyPatch()
        env.setenv("AWS_ENDPOINT_URL", endpoint)
        env.setenv("AWS_DEFAULT_REGION", "us-west-2")
        env.setenv("AWS_ACCESS_KEY_ID", "local")
        env.setenv("AWS_SECRET_ACCESS_KEY", "local")
        env.setenv("READINGS_TABLE", "readings")
        readings._table.cache_clear()
        # Same keys as local/init.sh.
        boto3.client("dynamodb").create_table(
            TableName="readings",
            AttributeDefinitions=[
                {"AttributeName": "sensorId", "AttributeType": "S"},
                {"AttributeName": "second", "AttributeType": "N"},
            ],
            KeySchema=[
                {"AttributeName": "sensorId", "KeyType": "HASH"},
                {"AttributeName": "second", "KeyType": "RANGE"},
            ],
            ProvisionedThroughput={"ReadCapacityUnits": 20, "WriteCapacityUnits": 20},
        )
        yield readings._table()
        env.undo()
        readings._table.cache_clear()


def test_duplicate_delivery_stores_one_item_per_sensor_second(table):
    readings.store(MESSAGE, NOW)
    readings.store(
        MESSAGE, NOW + 5
    )  # redelivery must not overwrite, even with a new ttl

    stored = table.scan()["Items"]
    assert len(stored) == 2
    assert {i["ttl"] for i in stored} == {NOW + 86400}
    chamber = next(i for i in stored if i["sensorId"] == "engine-1.chamber_temp")
    assert [float(v) for v in chamber["values"]] == [3303.3, 3294.1]


def test_other_client_errors_are_not_swallowed(table, monkeypatch):
    # Swallowing these would make the caller delete a message that was never stored.
    monkeypatch.setenv("READINGS_TABLE", "missing")
    readings._table.cache_clear()
    try:
        with pytest.raises(ClientError, match="ResourceNotFoundException"):
            readings.store(MESSAGE, NOW)
    finally:
        readings._table.cache_clear()
