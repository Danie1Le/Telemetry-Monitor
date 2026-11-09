import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from botocore.exceptions import ClientError

from processor import __main__ as proc
from processor import readings
from processor.contracts import CONTRACTS_DIR

MESSAGE = json.loads((CONTRACTS_DIR / "sqs-message.example.json").read_text())


def at_second(second: int) -> dict:
    """The example message moved to another second."""
    shift = (second - MESSAGE["second"]) * 1000
    return {
        **MESSAGE,
        "second": second,
        "readings": {
            sensor_id: [{**r, "ts": r["ts"] + shift} for r in rs]
            for sensor_id, rs in MESSAGE["readings"].items()
        },
    }


def send(queues, message) -> None:
    body = message if isinstance(message, str) else json.dumps(message)
    queues.sqs.send_message(QueueUrl=queues.url, MessageBody=body)


def depth(queues, url: str) -> int:
    attrs = queues.sqs.get_queue_attributes(
        QueueUrl=url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
        ],
    )["Attributes"]
    return sum(int(v) for v in attrs.values())


def run_loop(queues, until, polls=10) -> None:
    """Drive the real loop until `until()` holds, at most `polls` receives."""
    for _ in range(polls):
        proc.poll(queues.sqs, queues.url, queues.dlq, wait_seconds=1)
        if until():
            return
    pytest.fail(f"condition not met after {polls} polls")


def dead_letters(queues) -> list[str]:
    response = queues.sqs.receive_message(
        QueueUrl=queues.dlq, MaxNumberOfMessages=10, WaitTimeSeconds=1
    )
    return [m["Body"] for m in response.get("Messages", [])]


def stored(table) -> dict:
    return {(i["sensorId"], int(i["second"])): i for i in table.scan()["Items"]}


@pytest.fixture
def store_calls(monkeypatch):
    """Seconds of every message `store` was called with; `store` still runs."""
    calls, real = [], readings.store

    def spy(message, now):
        calls.append(message["second"])
        real(message, now)

    monkeypatch.setattr(readings, "store", spy)
    return calls


def test_duplicate_delivery_stores_one_item_per_sensor_second(
    queues, table, store_calls
):
    send(queues, MESSAGE)
    run_loop(queues, until=lambda: depth(queues, queues.url) == 0)
    first = stored(table)

    send(queues, MESSAGE)  # a distinct SQS message with an identical body
    run_loop(queues, until=lambda: depth(queues, queues.url) == 0)

    assert store_calls == [MESSAGE["second"]] * 2
    assert set(first) == {
        ("engine-1.chamber_temp", MESSAGE["second"]),
        ("engine-1.tank_pressure", MESSAGE["second"]),
    }
    assert stored(table) == first  # the second copy changed nothing, not even ttl


def test_failed_dlq_send_leaves_the_message_for_the_next_poll(queues, monkeypatch):
    real_send, failed = queues.sqs.send_message, []

    def fail_once(**kwargs):
        if not failed:
            failed.append(True)
            raise ConnectionError("DLQ unreachable")
        return real_send(**kwargs)

    monkeypatch.setattr(queues.sqs, "send_message", fail_once)
    real_send(QueueUrl=queues.url, MessageBody="not json")

    proc.poll(queues.sqs, queues.url, queues.dlq, wait_seconds=1)

    assert depth(queues, queues.url) == 1
    assert depth(queues, queues.dlq) == 0

    run_loop(queues, until=lambda: depth(queues, queues.dlq) == 1)

    assert depth(queues, queues.url) == 0
    assert dead_letters(queues) == ["not json"]


def test_crash_before_delete_is_redelivered_and_stored_once(
    queues, table, store_calls, monkeypatch
):
    real_delete, crashed = queues.sqs.delete_message, []

    def crash_once(**kwargs):
        if not crashed:
            crashed.append(True)
            raise ConnectionError("crashed after store, before delete")
        real_delete(**kwargs)

    monkeypatch.setattr(queues.sqs, "delete_message", crash_once)
    send(queues, MESSAGE)

    run_loop(queues, until=lambda: bool(store_calls))
    first = stored(table)
    run_loop(queues, until=lambda: depth(queues, queues.url) == 0)

    assert store_calls == [MESSAGE["second"]] * 2
    assert stored(table) == first  # the redelivery changed nothing, not even ttl


def test_poison_message_reaches_dlq_after_3_receives_and_queue_keeps_flowing(
    queues, table, monkeypatch
):
    poison, good = at_second(MESSAGE["second"] + 10), MESSAGE
    real_store, poison_attempts = readings.store, []

    def store(message, now):
        if message["second"] == poison["second"]:
            poison_attempts.append(now)
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "PutItem")
        real_store(message, now)

    monkeypatch.setattr(readings, "store", store)
    send(queues, poison)
    send(queues, good)

    run_loop(queues, until=lambda: depth(queues, queues.dlq) == 1, polls=15)

    assert len(poison_attempts) == 3
    assert {second for _, second in stored(table)} == {good["second"]}
    assert depth(queues, queues.url) == 0
    assert [json.loads(b) for b in dead_letters(queues)] == [poison]


def test_out_of_order_messages_are_all_stored(queues, table):
    base = MESSAGE["second"]
    for offset in (3, 1, 2):
        send(queues, at_second(base + offset))

    run_loop(queues, until=lambda: depth(queues, queues.url) == 0)

    items = stored(table)
    assert set(items) == {
        (sensor_id, base + offset)
        for sensor_id in MESSAGE["readings"]
        for offset in (1, 2, 3)
    }
    assert all(
        i["offsets"] == items[(i["sensorId"], base + 1)]["offsets"]
        for i in items.values()
    )


@pytest.mark.parametrize(
    "body",
    [json.dumps({**MESSAGE, "schemaVersion": 2}), "not json"],
    ids=["unknown-schemaVersion", "invalid-json"],
)
def test_bad_message_is_dead_lettered_at_once_and_nothing_stored(queues, table, body):
    send(queues, body)

    proc.poll(queues.sqs, queues.url, queues.dlq, wait_seconds=1)

    assert dead_letters(queues) == [body]
    assert depth(queues, queues.url) == 0
    assert stored(table) == {}


def test_contract_example_message_is_stored_as_the_example_item(queues, table):
    item = json.loads((CONTRACTS_DIR / "readings-item.example.json").read_text())
    send(queues, MESSAGE)

    run_loop(queues, until=lambda: depth(queues, queues.url) == 0)

    actual = json.loads(
        json.dumps(stored(table)[(item["sensorId"], item["second"])], default=float)
    )
    del actual["ttl"], item["ttl"]  # ttl depends on the processor's clock
    assert actual == item


@pytest.fixture
def health():
    server = ThreadingHTTPServer(("127.0.0.1", 0), proc.Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def status(url: str) -> int:
    try:
        return urllib.request.urlopen(url, timeout=2).status
    except urllib.error.HTTPError as e:
        return e.code


def test_health_fails_when_the_loop_stops_polling(health, monkeypatch):
    monkeypatch.setattr(proc, "last_poll", proc.time.monotonic())
    assert status(f"{health}/healthz") == 200
    assert status(f"{health}/other") == 404

    monkeypatch.setattr(
        proc, "last_poll", proc.time.monotonic() - proc.STALE_AFTER_SECONDS
    )
    assert status(f"{health}/healthz") == 503
