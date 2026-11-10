"""Run every rule on a batch, with rule state in Valkey, and record the alerts in DynamoDB.

Batches reach the rules through a per-engine reorder buffer in Valkey (`buffer`, `drain`),
so the rules see each engine's seconds in order even though SQS reorders messages
(docs/adr/0001-reorder-buffer-for-rule-evaluation.md).

Each rule-state key also holds the events its rule raised but that are not yet written
(`pending`). They are saved with the state in one atomic update and cleared only once
written (and an open published), so a failed write is retried by the redelivery even though the rule then skips
the already-seen readings. An alert still open when its flight ends stays open: no
batch arrives to close it, and v1 has no timers (#8).
"""

import json
import logging
import os
from contextlib import suppress
from datetime import UTC, datetime
from functools import cache, partial

import boto3
import redis
from botocore.exceptions import ClientError
from redis.exceptions import LockNotOwnedError

from processor import rules
from processor.contracts import load_sensors, validator
from processor.readings import to_dynamo

log = logging.getLogger("processor")

TTL_SECONDS = 7 * 24 * 60 * 60
STATE_TTL_SECONDS = 24 * 60 * 60
# Renewed before each batch, so a drainer that dies frees its engine within this.
DRAIN_LOCK_SECONDS = 10
BUFFERED = "buffered"  # set of "<flightId>:<engineId>" whose buffer is not empty
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


def buffer(message: dict, now: int) -> None:
    """Hold a stored message's batch until `drain` reaches its second.

    The caller then deletes the SQS message, so the batch lives only in Valkey, as the
    rule state does. A redelivery keeps the first buffered time (`now`, epoch seconds).
    """
    engine = f"{message['flightId']}:{message['engineId']}"
    valkey, key = _valkey(), f"buffer:{engine}"
    valkey.hsetnx(key, message["second"], json.dumps({"at": now, "message": message}))
    valkey.expire(key, STATE_TTL_SECONDS)
    # After the batch, so `_forget` never drops an engine with a batch buffered.
    valkey.sadd(BUFFERED, engine)


def drain(engine: str, now: int, publish, dead_letter) -> None:
    """Record an engine's buffered batches in second order, from its flight's start.

    Stops at a missing second until the earliest later batch has been buffered for
    reorderWaitSeconds, then skips it: the rules see it as absent, like a batch never
    sent. A batch whose recording still fails after that wait is dead-lettered and
    skipped the same way. A batch before the next expected second is dropped. Only one
    processor drains an engine at a time; the others return at once.
    """
    valkey = _valkey()
    lock = valkey.lock(f"drain:{engine}", timeout=DRAIN_LOCK_SECONDS, blocking=False)
    if not lock.acquire():
        return  # its holder, or the next poll, records what is buffered
    try:
        wait = load_sensors()["reorderWaitSeconds"]
        key, cursor = f"buffer:{engine}", f"cursor:{engine}"
        second = int(valkey.get(cursor) or _flight_start(engine))
        while True:
            batches = {int(s): json.loads(b) for s, b in valkey.hgetall(key).items()}
            if done := [s for s in batches if s < second]:
                valkey.hdel(key, *done)
            later = sorted(s for s in batches if s > second)
            if second in batches:
                lock.reacquire()  # raises if it expired and another drainer took over
                held = batches[second]
                try:
                    record(held["message"], publish)
                except Exception:
                    if now - held["at"] < wait:
                        raise  # the next drain retries it
                    log.exception("%s: second %d dead-lettered", engine, second)
                    dead_letter(held["message"])
                second += 1
            elif later and now - min(batches[s]["at"] for s in later) >= wait:
                log.warning(
                    "%s: rules skip seconds %d-%d", engine, second, later[0] - 1
                )
                second = later[0]
            else:
                break
            # After the record: a crash in between records the batch again, idempotently.
            _update(cursor, partial(_advance, second))
        valkey.transaction(partial(_forget, key, engine), key)
    finally:
        with suppress(LockNotOwnedError):  # lost mid-record; keep the real error
            lock.release()


def drain_all(now: int, publish, dead_letter) -> None:
    """Drain every engine with buffered batches, so one behind a gap drains even when
    no new message arrives. A failing engine is logged and leaves the others draining."""
    for engine in _valkey().smembers(BUFFERED):
        try:
            drain(engine.decode(), now, publish, dead_letter)
        except Exception:
            log.exception("draining %s failed", engine.decode())


def _advance(second, cursor):
    # Never back: a drainer whose lock expired mid-record may finish after the next one.
    return max(cursor or second, second), None


def _forget(key, engine, pipe):
    # WATCHed: a batch buffered meanwhile aborts the removal (a hash with no field is gone).
    if not pipe.exists(key):
        pipe.multi()
        pipe.srem(BUFFERED, engine)


def _flight_start(engine: str) -> int:
    # A flightId starts with the flight's UTC start time, its first batch second.
    start = datetime.strptime(engine[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    return int(start.timestamp())


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
                Item=to_dynamo(item),
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
            ExpressionAttributeValues=to_dynamo(values),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    return True
