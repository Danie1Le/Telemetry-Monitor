from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from processor import readings

ELASTICMQ_CONF = Path(__file__).resolve().parents[2] / "local" / "elasticmq.conf"


def _endpoint(container: DockerContainer, port: int) -> str:
    return (
        f"http://{container.get_container_host_ip()}:{container.get_exposed_port(port)}"
    )


@pytest.fixture(scope="session")
def aws_env():
    env = pytest.MonkeyPatch()
    env.setenv("AWS_DEFAULT_REGION", "us-west-2")
    env.setenv("AWS_ACCESS_KEY_ID", "local")
    env.setenv("AWS_SECRET_ACCESS_KEY", "local")
    yield env
    env.undo()


@pytest.fixture(scope="session")
def _readings_table(aws_env):
    # Same image as docker-compose.yml.
    with (
        DockerContainer("amazon/dynamodb-local:2.5.4")
        .with_exposed_ports(8000)
        .with_command("-jar DynamoDBLocal.jar -inMemory")
        .waiting_for(LogMessageWaitStrategy("Initializing DynamoDB Local")) as container
    ):
        aws_env.setenv("AWS_ENDPOINT_URL_DYNAMODB", _endpoint(container, 8000))
        aws_env.setenv("READINGS_TABLE", "readings")
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
        readings._table.cache_clear()


@pytest.fixture
def table(_readings_table):
    """The `readings` table, emptied after each test."""
    yield _readings_table
    with _readings_table.batch_writer() as batch:
        for item in _readings_table.scan()["Items"]:
            batch.delete_item(
                Key={"sensorId": item["sensorId"], "second": item["second"]}
            )


@pytest.fixture(scope="session")
def _elasticmq(aws_env):
    # Same image and queue config (redrive to the DLQ after 3 receives) as docker-compose.yml.
    with (
        DockerContainer("softwaremill/elasticmq-native:1.6.11")
        .with_exposed_ports(9324)
        .with_volume_mapping(str(ELASTICMQ_CONF), "/opt/elasticmq.conf", "ro")
        .waiting_for(LogMessageWaitStrategy("started")) as container
    ):
        aws_env.setenv("AWS_ENDPOINT_URL_SQS", _endpoint(container, 9324))
        sqs = boto3.client("sqs")
        url = sqs.get_queue_url(QueueName="telemetry-readings")["QueueUrl"]
        dlq = sqs.get_queue_url(QueueName="telemetry-readings-dlq")["QueueUrl"]
        # 1s instead of 30s, so redelivery tests run in seconds.
        sqs.set_queue_attributes(QueueUrl=url, Attributes={"VisibilityTimeout": "1"})
        yield SimpleNamespace(sqs=sqs, url=url, dlq=dlq)


@pytest.fixture
def queues(_elasticmq):
    """The queue and its DLQ, purged after each test."""
    yield _elasticmq
    for url in (_elasticmq.url, _elasticmq.dlq):
        _elasticmq.sqs.purge_queue(QueueUrl=url)
