"""Run every rule on a batch, with rule state in Valkey, and record the alerts in DynamoDB.

Each rule-state key also holds the events its rule raised but that are not yet written
(`pending`). They are saved with the state in one atomic update and cleared only once
written (and an open published), so a failed write is retried by the redelivery even though the rule then skips
the already-seen readings. An alert still open when its flight ends stays open: no
batch arrives to close it, and v1 has no timers (#8).
"""

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from functools import cache, partial

import boto3
import redis
from botocore.exceptions import ClientError

from processor import rules
from processor.contracts import load_sensors, validator

TTL_SECONDS = 7 * 24 * 60 * 60
STATE_TTL_SECONDS = 24 * 60 * 60
VALUE_RULES = {
    "threshold": rules.threshold,
    "rate_of_change": rules.rate_of_change,
    "drift": rules.drift,
    "flatline": rules.flatline,
}


@cache
def _valkey():
    return redis.Redis.from_url(os.environ["VALKEY_URL"])


@cache
def _table():
    return boto3.resource("dynamodb").Table(os.environ["ALERTS_TABLE"])


@cache
def _sns():
    return boto3.client("sns")


def _update(key: str, change):
    """Save change(state) -> (new state, result) to `key` atomically and return result.

    WATCH makes EXEC fail if another processor wrote the key in between; redis-py then
    reruns `change` on the newer state.
    """

    def apply(pipe):
        raw = pipe.get(key)
        state, result = change(json.loads(raw) if raw else {})
        pipe.multi()
        pipe.set(key, json.dumps(state), ex=STATE_TTL_SECONDS)
        return result

    return _valkey().transaction(apply, key, value_from_callable=True)


def detect(message: dict) -> dict[str, list[dict]]:
    """Run the rules for each sensor expected in the message's batch.

    Value rules run on present sensors, dropout on all. Returns the pending events of
    each rule-state key that has any: new ones plus any an earlier call did not write.
    """
    config = load_sensors()
    flight_id, phase, second = message["flightId"], message["phase"], message["second"]

    def phase_start(starts):
        # The earliest second seen in a phase is its start, whatever the arrival order.
        starts[phase] = min(starts.get(phase, second), second)
        return starts, starts[phase]

    seconds_in_phase = second - _update(f"phase:{flight_id}", phase_start)
    pending = {}
    for sensor_type in config["sensorTypes"]:
        sensor_id = f"{message['engineId']}.{sensor_type}"
        readings = message["readings"].get(sensor_id, [])
        corrupt = message.get("rejectedCounts", {}).get(sensor_id, 0)
        ctx = rules.Context(
            flight_id, sensor_id, phase, seconds_in_phase, second, corrupt
        )
        to_run = {**VALUE_RULES} if readings else {}
        to_run["dropout"] = rules.dropout

        for name, rule in to_run.items():
            key = f"rule:{flight_id}:{sensor_id}:{name}"
            if events := _update(key, partial(_step, rule, config, readings, ctx)):
                pending[key] = events
    return pending


def _step(rule, config, readings, ctx, state):
    waiting = state.pop("pending", [])
    state, events = rule(config, state, readings, ctx)
    # A peak update needs the opening phase's nominal to tell which is more extreme.
    events = [
        {**e, "nominal": state["nominal"]} if e["event"] == "peak" else e
        for e in events
    ]
    state["pending"] = waiting + events
    return state, state["pending"]


def record(message: dict, publish=lambda event: None) -> None:
    """Detect anomalies in a stored message, write its alerts and publish each open event.

    Email is at-least-once: an open event stays pending until it is written and
    published, so a failure in between publishes it again on the redelivery, even when
    its alert item already exists. A redelivery after success publishes nothing.
    """
    for key, events in detect(message).items():
        for event in events:
            _write(event)
            if event["event"] == "open":
                publish(event)
        _update(key, partial(_written, events))


def notify(event: dict) -> None:
    """Email a critical alert's open, at most once per sensor per cooldown.

    The cooldown starts only after the publish, so a crash or failure before it never
    loses the email: the redelivery sends it. Two racing processors may both send.
    """
    if event["severity"] != "critical":
        return
    key = f"cooldown:{event['sensorId']}"
    if _valkey().exists(key):
        return
    opened = datetime.fromtimestamp(event["openedAt"] / 1000, UTC).isoformat()
    _sns().publish(
        TopicArn=os.environ["ALERTS_TOPIC_ARN"],
        Subject=f"Critical alert: {event['sensorId']} {event['rule']}",
        Message="\n".join(
            [
                f"Flight: {event['flightId']}",
                f"Sensor: {event['sensorId']}",
                f"Rule: {event['rule']}",
                f"Value: {event['peakValue']}",
                f"Opened at: {opened}",
                f"Detail: {event['detail']}",
            ]
        ),
    )
    _valkey().set(key, 1, ex=load_sensors()["alertCooldownSeconds"])


def _written(events, state):
    state["pending"] = [e for e in state.get("pending", []) if e not in events]
    return state, None


def _write(event: dict) -> bool:
    """Apply one event to its alert item. False if it was already applied or is stale."""
    fields = {k: v for k, v in event.items() if k not in ("event", "nominal")}
    fields["alertKey"] = f"{event['openedAt']}#{event['sensorId']}#{event['rule']}"
    schema = validator("alert")
    try:
        if event["event"] == "open":
            item = {**fields, "ttl": event["openedAt"] // 1000 + TTL_SECONDS}
            schema.validate(item)
            _table().put_item(
                Item=_dynamo(item),
                ConditionExpression="attribute_not_exists(alertKey)",
            )
            return True
        schema.evolve(schema={**schema.schema, "required": []}).validate(fields)
        if event["event"] == "peak":
            # Only a peak further from nominal than the stored one, whatever the write order.
            off = abs(event["peakValue"] - event["nominal"])
            update = "SET peakValue = :p"
            condition = "peakValue BETWEEN :lo AND :hi"
            values = {
                ":p": event["peakValue"],
                ":lo": event["nominal"] - off,
                ":hi": event["nominal"] + off,
            }
        else:
            update = "SET closedAt = :c, peakValue = :p"
            condition = "attribute_exists(alertKey) AND attribute_not_exists(closedAt)"
            values = {":c": event["closedAt"], ":p": event["peakValue"]}
        _table().update_item(
            Key={"flightId": fields["flightId"], "alertKey": fields["alertKey"]},
            UpdateExpression=update,
            ConditionExpression=condition,
            ExpressionAttributeValues=_dynamo(values),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    return True


def _dynamo(value):
    # DynamoDB rejects floats, so numbers go in as Decimal.
    return json.loads(json.dumps(value), parse_float=Decimal)
