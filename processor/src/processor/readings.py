import json
import os
from decimal import Decimal
from functools import cache

import boto3
from botocore.exceptions import ClientError
from jsonschema import ValidationError

from processor.contracts import load_sensors, validator

TTL_SECONDS = 24 * 60 * 60


@cache
def _table():
    return boto3.resource("dynamodb").Table(os.environ["READINGS_TABLE"])


def to_dynamo(value):
    # DynamoDB rejects floats, so numbers go in as Decimal.
    return json.loads(json.dumps(value), parse_float=Decimal)


def to_items(message: dict, now: int) -> list[dict]:
    """One `readings` item per sensor in an SQS message. `now` is epoch seconds.

    Every bad message (schema, unknown sensor type, offset outside the second) raises
    jsonschema.ValidationError, so the caller needs one `except` to dead-letter it.
    """
    validator("sqs-message").validate(message)
    sensor_types = load_sensors()["sensorTypes"]
    second = message["second"]
    for sensor_id in message["readings"]:
        if sensor_id.split(".", 1)[1] not in sensor_types:
            raise ValidationError(f"unknown sensor type in {sensor_id!r}")
    items = [
        {
            "sensorId": sensor_id,
            "second": second,
            "offsets": [r["ts"] - second * 1000 for r in readings],
            "values": [r["value"] for r in readings],
            "unit": sensor_types[sensor_id.split(".", 1)[1]]["unit"],
            "phase": message["phase"],
            "flightId": message["flightId"],
            "ttl": now + TTL_SECONDS,
        }
        for sensor_id, readings in message["readings"].items()
    ]
    # Ingest already keeps ts inside the second; the item schema re-checks offsets 0-999.
    for item in items:
        validator("readings-item").validate(item)
    return items


def store(message: dict, now: int) -> None:
    """Write each item once; a redelivered message is a no-op."""
    for item in to_items(message, now):
        try:
            _table().put_item(
                Item=to_dynamo(item),
                ConditionExpression="attribute_not_exists(sensorId)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
