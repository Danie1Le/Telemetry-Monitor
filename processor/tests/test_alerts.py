import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from processor import __main__ as proc
from processor import alerts
from processor.contracts import load_sensors, validator

CONFIG = load_sensors()
FLIGHT_ID = "20261109T140300Z-7f3a"
SECOND = 1794233041  # first second of a test flight's ascent
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
    """One process, so a transaction never conflicts."""

    def __init__(self):
        self.data = {}

    def transaction(self, apply, key, value_from_callable):
        return apply(self)

    def get(self, key):
        return self.data.get(key)

    def multi(self):
        pass

    def set(self, key, value, ex):
        self.data[key] = value

    def exists(self, key):
        return key in self.data


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


def test_handle_records_alerts_after_store_then_deletes(monkeypatch):
    calls = []
    monkeypatch.setattr(proc.readings, "store", lambda m, now: calls.append("store"))
    monkeypatch.setattr(alerts, "record", lambda m, publish: calls.append("record"))
    sqs = FakeSqs()

    proc.handle(sqs, "q", "dlq", sqs_message(batch(0)))
    assert calls == ["store", "record"]
    assert sqs.calls == ["delete"]


def test_handle_leaves_the_message_when_recording_alerts_fails(monkeypatch):
    def fail(message, publish):
        raise ConnectionError("Valkey unreachable")

    monkeypatch.setattr(proc.readings, "store", lambda m, now: None)
    monkeypatch.setattr(alerts, "record", fail)
    sqs = FakeSqs()

    with pytest.raises(ConnectionError):
        proc.handle(sqs, "q", "dlq", sqs_message(batch(0)))
    assert sqs.calls == []


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
