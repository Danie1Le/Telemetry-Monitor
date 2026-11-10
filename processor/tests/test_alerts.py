import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from redis.exceptions import LockNotOwnedError

from processor import __main__ as proc
from processor import alerts
from processor.contracts import load_sensors, validator

CONFIG = load_sensors()
SECOND = 1794233041  # first second of a test flight, in ascent
FLIGHT_ID = "20261109T140401Z-7f3a"  # starts at SECOND
MS = SECOND * 1000
CHAMBER = CONFIG["sensorTypes"]["chamber_temp"]["phases"]["ascent"]
# Same shape as the simulator's spike fault.
SPIKE = CHAMBER["nominal"] + CHAMBER["max"] - CHAMBER["min"] + CHAMBER["maxRatePerSec"]


def batch(n, spike=(), missing=(), corrupt=(), flight_id=FLIGHT_ID) -> dict:
    """The SQS message for second n of a flight's ascent: every sensor normal, alternating
    at the edge of noise, except chamber_temp at SPIKE in `spike` seconds and the sensor
    types in `missing` (absent) and `corrupt` (all readings rejected)."""
    second, readings = SECOND + n, {}
    for sensor_type, spec in CONFIG["sensorTypes"].items():
        if sensor_type in missing or sensor_type in corrupt:
            continue
        lim = spec["phases"]["ascent"]
        value = [lim["nominal"] + lim["noise"] * (-1) ** j for j in range(10)]
        if sensor_type == "chamber_temp" and n in spike:
            value = [SPIKE] * 10
        readings[f"engine-1.{sensor_type}"] = [
            {"ts": second * 1000 + j * 100, "value": v} for j, v in enumerate(value)
        ]
    message = {
        "schemaVersion": 1,
        "flightId": flight_id,
        "phase": "ascent",
        "engineId": "engine-1",
        "second": second,
        "receivedAt": second * 1000 + 1035,
        "readings": readings,
        "rejectedCounts": {f"engine-1.{t}": 10 for t in corrupt},
    }
    validator("sqs-message").validate(message)
    return message


class FakeValkey:
    """One process, so a transaction never conflicts. Like Valkey, a hash or set with no
    member is gone."""

    def __init__(self):
        self.data, self.locks = {}, {}

    def transaction(self, apply, key, **kwargs):
        return apply(self)

    def get(self, key):
        return self.data.get(key)

    def multi(self):
        pass

    def set(self, key, value, ex):
        self.data[key] = value

    def exists(self, key):
        return key in self.data

    def expire(self, key, seconds):
        pass

    def hsetnx(self, key, field, value):
        self.data.setdefault(key, {}).setdefault(str(field).encode(), value)

    def hgetall(self, key):
        return dict(self.data.get(key, {}))

    def hdel(self, key, *fields):
        for field in fields:
            self.data[key].pop(str(field).encode())
        if not self.data[key]:
            del self.data[key]

    def sadd(self, key, member):
        self.data.setdefault(key, set()).add(member.encode())

    def smembers(self, key):
        return set(self.data.get(key, ()))

    def srem(self, key, member):
        self.data.get(key, set()).discard(member.encode())
        if not self.data.get(key):
            self.data.pop(key, None)

    def lock(self, name, timeout, blocking):
        """A lock expires when a test deletes it from `locks`."""
        locks = self.locks

        class Lock:
            def acquire(self):
                return locks.setdefault(name, self) is self

            def reacquire(self):
                if locks.get(name) is not self:
                    raise LockNotOwnedError

            def release(self):
                self.reacquire()
                del locks[name]

        return Lock()


@pytest.fixture
def fake(monkeypatch):
    """Valkey in memory; alert writes are collected instead of sent to DynamoDB."""
    valkey, written = FakeValkey(), []
    monkeypatch.setattr(alerts, "_valkey", lambda: valkey)
    monkeypatch.setattr(alerts, "_write", lambda e: written.append(e) or True)
    return valkey, written


def test_value_rules_run_on_present_sensors_and_dropout_on_every_expected_one(fake):
    valkey, _ = fake
    alerts.record(batch(0, missing=["fuel_flow"], corrupt=["vibration"]))

    present = ["chamber_temp", "tank_pressure", "valve_position"]
    assert set(valkey.data) == {f"phase:{FLIGHT_ID}"} | {
        f"rule:{FLIGHT_ID}:engine-1.{t}:{rule}"
        for t in CONFIG["sensorTypes"]
        for rule in ["dropout", *(alerts.VALUE_RULES if t in present else [])]
    }


def test_phase_start_is_the_earliest_second_seen(fake):
    valkey, _ = fake
    for n in (3, 0, 5):
        alerts.record(batch(n))
    assert json.loads(valkey.data[f"phase:{FLIGHT_ID}"]) == {"ascent": SECOND}


def test_alerts_within_grace_of_the_phase_start_do_not_open(fake):
    _, written = fake
    # threshold grace is 5 s from the first second seen in ascent
    for n in range(4, 9):
        alerts.record(batch(n, spike=[8]))
    assert written == []
    alerts.record(batch(9, spike=[9]))
    assert [(e["event"], e["rule"], e["openedAt"]) for e in written] == [
        ("open", "threshold", MS + 9000)
    ]


def test_a_failed_write_is_retried_by_the_redelivery(fake, monkeypatch):
    _, written = fake
    for n in range(10):
        alerts.record(batch(n))

    def fail(event):
        raise ConnectionError("DynamoDB unreachable")

    monkeypatch.setattr(alerts, "_write", fail)
    with pytest.raises(ConnectionError):
        alerts.record(batch(10, spike=[10]))
    monkeypatch.setattr(alerts, "_write", lambda e: written.append(e) or True)

    published = []
    alerts.record(batch(10, spike=[10]), published.append)

    assert [(e["event"], e["rule"]) for e in published] == [
        ("open", "threshold"),
        ("open", "rate_of_change"),
    ]
    assert published == written
    alerts.record(batch(10, spike=[10]), published.append)
    assert len(written) == len(published) == 2  # written once, then cleared


def critical_open(sensor_id="engine-1.chamber_temp") -> dict:
    return {
        "event": "open",
        "flightId": FLIGHT_ID,
        "sensorId": sensor_id,
        "rule": "threshold",
        "severity": "critical",
        "phase": "ascent",
        "openedAt": MS + 10_000,
        "peakValue": SPIKE,
        "detail": "value above max",
    }


@pytest.fixture
def sns(fake, monkeypatch):
    """Sent emails as (Subject, Message); `fail` makes the next publish raise."""
    sent, topic = [], SimpleNamespace(fail=False)

    def publish(**kwargs):
        if topic.fail:
            raise ConnectionError("SNS unreachable")
        assert kwargs["TopicArn"] == "arn:alerts"
        sent.append((kwargs["Subject"], kwargs["Message"]))

    monkeypatch.setenv("ALERTS_TOPIC_ARN", "arn:alerts")
    monkeypatch.setattr(alerts, "_sns", lambda: SimpleNamespace(publish=publish))
    return sent, topic


def test_critical_open_emails_flight_sensor_rule_value_and_time(sns):
    sent, _ = sns
    alerts.notify(critical_open())
    [(subject, body)] = sent
    assert subject == "Critical alert: engine-1.chamber_temp threshold"
    assert body.splitlines() == [
        f"Flight: {FLIGHT_ID}",
        "Sensor: engine-1.chamber_temp",
        "Rule: threshold",
        f"Value: {SPIKE}",
        "Opened at: 2026-11-09T14:04:11+00:00",
        "Detail: value above max",
    ]


def test_warning_never_emails(sns):
    sent, _ = sns
    alerts.notify({**critical_open("engine-1.fuel_flow"), "severity": "warning"})
    assert sent == []


def test_second_critical_in_cooldown_is_suppressed_but_another_sensor_emails(sns):
    sent, _ = sns
    alerts.notify(critical_open())
    alerts.notify({**critical_open(), "rule": "rate_of_change"})
    assert len(sent) == 1
    alerts.notify(critical_open("engine-1.tank_pressure"))
    assert len(sent) == 2


def test_failed_publish_raises_and_the_retry_sends(sns):
    sent, topic = sns
    topic.fail = True
    with pytest.raises(ConnectionError):
        alerts.notify(critical_open())
    topic.fail = False
    alerts.notify(critical_open())  # no cooldown was set, so the retry sends
    assert len(sent) == 1


class FakeSqs:
    def __init__(self):
        self.calls = []

    def send_message(self, **kwargs):
        self.calls.append("send")

    def delete_message(self, **kwargs):
        self.calls.append("delete")


def sqs_message(body) -> dict:
    return {"MessageId": "1", "ReceiptHandle": "r", "Body": json.dumps(body)}


def test_handle_stores_buffers_then_deletes(monkeypatch):
    sqs = FakeSqs()
    monkeypatch.setattr(
        proc.readings, "store", lambda m, now: sqs.calls.append("store")
    )
    monkeypatch.setattr(alerts, "buffer", lambda m, now: sqs.calls.append("buffer"))

    proc.handle(sqs, "q", "dlq", sqs_message(batch(0)))
    assert sqs.calls == ["store", "buffer", "delete"]


def test_handle_leaves_the_message_when_buffering_fails(monkeypatch):
    def fail(message, now):
        raise ConnectionError("Valkey unreachable")

    monkeypatch.setattr(proc.readings, "store", lambda m, now: None)
    monkeypatch.setattr(alerts, "buffer", fail)
    sqs = FakeSqs()

    with pytest.raises(ConnectionError):
        proc.handle(sqs, "q", "dlq", sqs_message(batch(0)))
    assert sqs.calls == []


# Reorder buffer: each engine's batches reach the rules in second order.

WAIT = CONFIG["reorderWaitSeconds"]
ENGINE = f"{FLIGHT_ID}:engine-1"


def unexpected(message):
    pytest.fail(f"second {message['second'] - SECOND} dead-lettered")


def arrive(n, now, published, dead_letter=unexpected, **faults) -> None:
    """Batch n of the flight arrives at `now`: handle buffers it, then poll drains."""
    alerts.buffer(batch(n, **faults), now)
    alerts.drain_all(now, published.append, dead_letter)


def opened(published) -> list[tuple]:
    return [(e["rule"], e["openedAt"]) for e in published]


def cursor(valkey) -> int:
    return int(valkey.data[f"cursor:{ENGINE}"])


SPIKE_OPENS = [("threshold", MS + 10_000), ("rate_of_change", MS + 10_000)]
SPIKE_11_OPENS = [("threshold", MS + 11_000), ("rate_of_change", MS + 11_000)]


def test_batches_in_order_are_recorded_at_once(fake):
    valkey, _ = fake
    published = []
    for n in range(10):
        arrive(n, 0, published)
    arrive(10, 0, published, spike=[10])

    assert opened(published) == SPIKE_OPENS
    assert cursor(valkey) == SECOND + 11
    assert f"buffer:{ENGINE}" not in valkey.data
    assert alerts.BUFFERED not in valkey.data


def test_a_late_batch_within_the_wait_is_recorded_before_the_later_ones(fake):
    published = []
    for n in range(10):
        arrive(n, 0, published)
    for n in range(11, 16):  # second 10 is late, as after a processor crash
        arrive(n, n, published)
    assert published == []

    arrive(10, 10 + WAIT - 1, published, spike=[10])

    assert opened(published) == SPIKE_OPENS


def test_a_missing_second_is_skipped_once_a_later_batch_has_waited(fake):
    valkey, _ = fake
    published = []
    for n in range(10):
        arrive(n, 0, published)
    arrive(11, 100, published, spike=[11])
    arrive(12, 100 + WAIT - 1, published)
    assert published == []

    arrive(13, 100 + WAIT, published)

    assert opened(published) == SPIKE_11_OPENS
    assert cursor(valkey) == SECOND + 14
    arrive(10, 100 + WAIT, published, spike=[10])  # too late: dropped
    assert len(published) == 2
    assert f"buffer:{ENGINE}" not in valkey.data


def test_a_batch_that_keeps_failing_is_dead_lettered_after_the_wait(fake, monkeypatch):
    valkey, _ = fake
    published, dead, real = [], [], alerts.detect

    def detect(message):
        if message["second"] == SECOND + 10:
            raise ValueError("a rule bug")
        return real(message)

    monkeypatch.setattr(alerts, "detect", detect)
    for n in range(10):
        arrive(n, 0, published)
    arrive(10, 0, published, spike=[10])
    arrive(11, WAIT - 1, published, spike=[11])
    assert published == []  # 10 is retried, and 11 waits behind it

    arrive(12, WAIT, published, dead.append)

    assert dead == [batch(10, spike=[10])]
    assert opened(published) == SPIKE_11_OPENS
    assert cursor(valkey) == SECOND + 13


def test_poll_drains_a_tail_behind_a_gap_with_no_new_message(fake, monkeypatch):
    valkey, _ = fake
    published = []
    for n in range(10):
        arrive(n, 0, published)
    arrive(11, 100, published, spike=[11])
    monkeypatch.setattr(alerts, "notify", published.append)
    monkeypatch.setattr(proc.time, "time", lambda: 100 + WAIT)
    sqs = FakeSqs()
    sqs.receive_message = lambda **kwargs: {}

    proc.poll(sqs, "q", "dlq", wait_seconds=0)

    assert opened(published) == SPIKE_11_OPENS
    assert alerts.BUFFERED not in valkey.data


def test_a_redelivered_batch_already_recorded_is_dropped(fake):
    valkey, _ = fake
    published = []
    for n in range(11):
        arrive(n, 0, published, spike=[10])

    arrive(10, 1, published, spike=[10])

    assert opened(published) == SPIKE_OPENS  # not opened twice
    assert f"buffer:{ENGINE}" not in valkey.data


def test_an_engine_another_processor_drains_is_left_to_it(fake):
    valkey, _ = fake
    published = []
    for n in range(10):
        arrive(n, 0, published)
    other = valkey.lock(f"drain:{ENGINE}", timeout=10, blocking=False)
    other.acquire()

    arrive(10, 0, published, spike=[10])
    assert published == []
    other.release()
    alerts.drain_all(0, published.append, unexpected)

    assert opened(published) == SPIKE_OPENS


def test_a_drainer_whose_lock_expired_never_moves_the_cursor_back(fake, monkeypatch):
    valkey, _ = fake
    published, real = [], alerts.detect
    for n in range(10):
        arrive(n, 0, published)

    def slow(message):
        monkeypatch.setattr(alerts, "detect", real)
        valkey.locks.clear()  # the lock expires while second 10 is recorded
        arrive(11, 0, published, spike=[11])  # another processor drains 10 and 11
        return real(message)

    alerts.buffer(batch(10), 0)
    monkeypatch.setattr(alerts, "detect", slow)
    alerts.drain(ENGINE, 0, published.append, unexpected)

    assert opened(published) == SPIKE_11_OPENS
    assert cursor(valkey) == SECOND + 12


# Integration: real Valkey and DynamoDB Local.


def stored(alerts_table) -> list[dict]:
    """Every alert item, numbers back to int/float, each checked against the contract."""
    items = json.loads(
        json.dumps(
            alerts_table.scan()["Items"],
            default=lambda d: int(d) if d == int(d) else float(d),
        )
    )
    for item in items:
        validator("alert").validate(item)
    return sorted(items, key=lambda i: i["alertKey"])


def spike_flight(flight_id=FLIGHT_ID) -> list[dict]:
    """16 s of ascent with a 2 s chamber_temp spike from second 10."""
    return [batch(n, spike=[10, 11], flight_id=flight_id) for n in range(16)]


def deliver(queues, messages) -> None:
    """Send each message and let the real loop handle it before the next."""
    for message in messages:
        queues.sqs.send_message(QueueUrl=queues.url, MessageBody=json.dumps(message))
        proc.poll(queues.sqs, queues.url, queues.dlq, wait_seconds=1)


def expected_spike_alerts(flight_id=FLIGHT_ID) -> list[dict]:
    opened = MS + 10_000
    common = {
        "flightId": flight_id,
        "openedAt": opened,
        "sensorId": "engine-1.chamber_temp",
        "severity": "critical",
        "phase": "ascent",
        "peakValue": SPIKE,
        "ttl": opened // 1000 + 7 * 24 * 60 * 60,
    }
    return [
        {
            **common,
            "alertKey": f"{opened}#engine-1.chamber_temp#rate_of_change",
            "rule": "rate_of_change",
            # The jump back down is itself too fast; the next reading is normal.
            "closedAt": MS + 12_100,
        },
        {
            **common,
            "alertKey": f"{opened}#engine-1.chamber_temp#threshold",
            "rule": "threshold",
            "closedAt": MS + 12_000,
        },
    ]


def without_detail(items: list[dict]) -> list[dict]:
    return [{k: v for k, v in i.items() if k != "detail"} for i in items]


@pytest.mark.integration
def test_spike_opens_and_closes_one_threshold_and_one_rate_alert(
    queues, table, alerts_table
):
    deliver(queues, spike_flight())

    items = stored(alerts_table)
    assert without_detail(items) == expected_spike_alerts()
    assert all(i["detail"] for i in items)


@pytest.mark.integration
def test_spike_on_a_critical_sensor_emails_once_and_redelivery_does_not(
    queues, table, emails
):
    deliver(queues, spike_flight())  # threshold and rate_of_change both open

    [body] = emails()
    assert f"Flight: {FLIGHT_ID}" in body
    assert "Sensor: engine-1.chamber_temp" in body

    deliver(queues, spike_flight())
    assert emails() == []


@pytest.mark.integration
def test_spike_in_a_batch_redelivered_after_a_crash_still_alerts_once(
    queues, table, alerts_table, emails, monkeypatch
):
    messages = [batch(n, spike=[10]) for n in range(16)]
    deliver(queues, messages[:10])
    real, buffered = alerts.buffer, []

    def crash_on_first_10(message, now):
        n = message["second"] - SECOND
        buffered.append(n)
        if n == 10 and buffered.count(10) == 1:
            raise ConnectionError("killed after store, before buffer")
        return real(message, now)

    monkeypatch.setattr(alerts, "buffer", crash_on_first_10)
    deliver(queues, messages[10:11])
    for message in messages[11:]:
        queues.sqs.send_message(QueueUrl=queues.url, MessageBody=json.dumps(message))
    for _ in range(10):
        proc.poll(queues.sqs, queues.url, queues.dlq, wait_seconds=1)
        if len(stored(alerts_table)) == 2:
            break

    assert buffered.index(11) < buffered.index(10, 1)  # 10 was redelivered after 11
    assert [(i["rule"], i["openedAt"]) for i in stored(alerts_table)] == [
        ("rate_of_change", MS + 10_000),
        ("threshold", MS + 10_000),
    ]  # and no dropout
    assert len(emails()) == 1


@pytest.mark.integration
def test_redelivered_messages_change_no_alert(queues, table, alerts_table):
    deliver(queues, spike_flight())
    first = stored(alerts_table)

    deliver(queues, spike_flight())

    assert stored(alerts_table) == first
    published = []
    for message in spike_flight():
        alerts.record(message, published.append)
    assert published == []
    assert stored(alerts_table) == first


@pytest.mark.integration
def test_an_open_whose_publish_failed_is_published_again_on_redelivery(
    alerts_table,
):
    messages = spike_flight()
    for message in messages[:10]:
        alerts.record(message)

    published = []

    def fail_rate(event):
        if event["rule"] == "rate_of_change":
            raise ConnectionError("SNS unreachable")
        published.append(event)

    with pytest.raises(ConnectionError):
        alerts.record(messages[10], fail_rate)  # after both alert items are put
    first = stored(alerts_table)
    assert len(first) == 2
    alerts.record(messages[10], published.append)

    assert [(e["event"], e["rule"]) for e in published] == [
        ("open", "threshold"),
        ("open", "rate_of_change"),  # again, though its put now fails its condition
    ]
    assert stored(alerts_table) == first
    alerts.record(messages[10], published.append)
    assert len(published) == 2  # cleared once published


@pytest.mark.integration
def test_out_of_order_seconds_raise_the_same_alerts(alerts_table):
    in_order = "20261109T140300Z-0001"
    for message in spike_flight(in_order):
        alerts.record(message)
    late = "20261109T140300Z-0002"
    messages = spike_flight(late)
    # second 1 before 0, and 12 (back to normal) before 11 (still spiking)
    for n in (1, 0, *range(2, 11), 12, 11, 13, 14, 15):
        alerts.record(messages[n])

    by_flight = {}
    for item in stored(alerts_table):
        by_flight.setdefault(item.pop("flightId"), []).append(item)
    assert by_flight[late] == by_flight[in_order]
    assert len(by_flight[late]) == 2


@pytest.mark.integration
def test_concurrent_updates_of_one_sensor_lose_no_state(alerts_table, monkeypatch):
    for message in spike_flight()[:10]:
        alerts.record(message)
    # Both threads read the same threshold state before either saves it.
    barrier, waited = threading.Barrier(2, timeout=10), set()
    real = alerts.VALUE_RULES["threshold"]

    def racing(config, state, readings, ctx):
        if ctx.sensor_id == "engine-1.chamber_temp" and (
            threading.get_ident() not in waited
        ):
            waited.add(threading.get_ident())
            barrier.wait()
        return real(config, state, readings, ctx)

    monkeypatch.setitem(alerts.VALUE_RULES, "threshold", racing)

    with ThreadPoolExecutor(2) as pool:
        # list() re-raises an exception from either thread
        list(pool.map(lambda n: alerts.record(batch(n, spike=[10, 11])), (10, 11)))

    assert len(waited) == 2
    threshold = [i for i in stored(alerts_table) if i["rule"] == "threshold"]
    assert len(threshold) == 1  # a lost update would let both seconds open one
    state = json.loads(
        alerts._valkey().get(f"rule:{FLIGHT_ID}:engine-1.chamber_temp:threshold")
    )
    assert state["lastTs"] == MS + 11_900


@pytest.mark.integration
def test_present_but_corrupt_sensor_raises_no_dropout(alerts_table):
    for n in range(6):
        alerts.record(batch(n, missing=["fuel_flow"], corrupt=["tank_pressure"]))
    alerts.record(batch(6))

    assert [
        (i["sensorId"], i["rule"], i["openedAt"], i.get("closedAt"), i["peakValue"])
        for i in stored(alerts_table)
    ] == [("engine-1.fuel_flow", "dropout", MS, MS + 6000, None)]


@pytest.mark.integration
def test_an_empty_batch_stores_nothing_and_its_absent_sensors_drop_out(
    queues, table, alerts_table
):
    every_type = list(CONFIG["sensorTypes"])
    empty = [batch(n, missing=every_type) for n in range(2, 6)]
    assert all(m["readings"] == m["rejectedCounts"] == {} for m in empty)

    deliver(queues, [batch(0), batch(1), *empty, batch(6)])

    stored_seconds = {int(i["second"]) for i in table.scan()["Items"]}
    assert stored_seconds == {SECOND, SECOND + 1, SECOND + 6}
    assert [
        (i["sensorId"], i["rule"], i["openedAt"], i.get("closedAt"))
        for i in stored(alerts_table)
    ] == [
        (f"engine-1.{t}", "dropout", MS + 2000, MS + 6000) for t in sorted(every_type)
    ]


@pytest.mark.integration
def test_writes_apply_once_and_a_peak_only_if_further_from_nominal(alerts_table):
    key = {
        "flightId": FLIGHT_ID,
        "sensorId": "engine-1.chamber_temp",
        "rule": "threshold",
        "openedAt": MS,
    }
    opened = {
        "event": "open",
        **key,
        "severity": "critical",
        "phase": "ascent",
        "peakValue": 4700,
        "detail": "chamber_temp spiked",
    }

    def peak(value):
        event = {"event": "peak", **key, "peakValue": value}
        return alerts._write({**event, "nominal": CHAMBER["nominal"]})  # 3300

    assert alerts._write(opened)
    assert not alerts._write(opened)
    assert [peak(v) for v in (4800, 4750, 2000, 1700, 1800)] == [
        True,
        False,  # a late, less extreme peak
        False,
        True,  # further from nominal on the other side
        False,
    ]
    closed = {"event": "close", **key, "closedAt": MS + 5000, "peakValue": 1700}
    assert alerts._write(closed)
    assert not alerts._write({**closed, "closedAt": MS + 6000})
    [item] = stored(alerts_table)
    assert (item["peakValue"], item["closedAt"]) == (1700, MS + 5000)
