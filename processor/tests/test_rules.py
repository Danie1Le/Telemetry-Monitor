import json
from pathlib import Path

import pytest
import yaml

from processor.contracts import load_sensors, validator
from processor.rules import (
    Context,
    drift,
    dropout,
    flatline,
    rate_of_change,
    threshold,
)

CONFIG = load_sensors()
ASCENT = CONFIG["sensorTypes"]["chamber_temp"]["phases"]["ascent"]
FLIGHT_ID = "20261109T140300Z-7f3a"
SECOND = 1794233041  # epoch second of the first reading in a run_rule series
MS = SECOND * 1000  # ts of the first reading in a run_rule series
CTX = Context(FLIGHT_ID, "engine-1.chamber_temp", "ascent", 10, SECOND, 0)
SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"


def run_rule(
    rule,
    values,
    sensor_id="engine-1.chamber_temp",
    phase="ascent",
    seconds_in_phase=10,
    deliveries=1,
):
    """Feed a fixed series to a rule at rateHz, one call per second, and return its events.

    State goes through JSON between seconds, as it will through Valkey. deliveries > 1
    redelivers every second that many times in a row.
    """
    hz = CONFIG["rateHz"]
    state, events = {}, []
    for n, i in enumerate(range(0, len(values), hz)):
        second = SECOND + n
        readings = [
            {"ts": second * 1000 + j * 1000 // hz, "value": v}
            for j, v in enumerate(values[i : i + hz])
        ]
        ctx = Context(FLIGHT_ID, sensor_id, phase, seconds_in_phase + n, second, 0)
        for _ in range(deliveries):
            state, new = rule(CONFIG, json.loads(json.dumps(state)), readings, ctx)
            events += new
    return events


def normal(lim, seconds):
    """Normal readings at the edge of noise, alternating, so consecutive readings move as
    fast as noise ever moves them."""
    return [lim["nominal"] + lim["noise"] * (-1) ** i for i in range(seconds * 10)]


@pytest.mark.parametrize("rule", [threshold, rate_of_change, drift, flatline])
@pytest.mark.parametrize(
    ("sensor", "phase"),
    [(s, p) for s, spec in CONFIG["sensorTypes"].items() for p in spec["phases"]],
)
def test_normal_series_stays_quiet(rule, sensor, phase):
    lim = CONFIG["sensorTypes"][sensor]["phases"][phase]
    assert run_rule(rule, normal(lim, 10), f"engine-1.{sensor}", phase) == []


@pytest.mark.parametrize(
    ("rule", "closed_at"),
    [
        (threshold, MS + 4000),
        # The jump back down is itself too fast; the next reading is normal.
        (rate_of_change, MS + 4100),
    ],
)
def test_spike_opens_one_alert_tracks_peak_and_closes(rule, closed_at):
    # Same shape as the simulator's spike fault: 2 s at value + max - min + maxRatePerSec.
    offset = ASCENT["max"] - ASCENT["min"] + ASCENT["maxRatePerSec"]
    spike = [v + offset for v in normal(ASCENT, 2)]

    opened, closed = run_rule(rule, normal(ASCENT, 2) + spike + normal(ASCENT, 2))

    assert opened["event"] == "open"
    assert opened["openedAt"] == MS + 2000
    assert closed == {
        "event": "close",
        "flightId": FLIGHT_ID,
        "sensorId": "engine-1.chamber_temp",
        "rule": rule.__name__,
        "openedAt": MS + 2000,
        "closedAt": closed_at,
        "peakValue": max(spike),
    }


def test_threshold_below_min_matches_the_alert_contract():
    low = [3050, 2900, 3000] + [3050] * 7

    opened, _peak, closed = run_rule(threshold, [3300] * 10 + low + [3300] * 10)

    assert opened["detail"] == "chamber_temp 3050 K below min 3100 K for ascent"
    assert closed["peakValue"] == 2900
    assert closed["closedAt"] == MS + 2000
    validate_alert(opened, closed)


def validate_alert(opened, closed):
    """Build the stored alert from its open and close events and validate it."""
    alert = {k: v for k, v in opened.items() if k != "event"} | {
        "alertKey": f"{opened['openedAt']}#{opened['sensorId']}#{opened['rule']}",
        "closedAt": closed["closedAt"],
        "peakValue": closed["peakValue"],
        "ttl": opened["openedAt"] // 1000 + 7 * 86400,
    }
    validator("alert").validate(alert)


@pytest.mark.parametrize(("step", "moved"), [(3500, "rose"), (3100, "fell")])
def test_rate_compares_across_seconds_and_a_step_inside_limits_closes(step, moved):
    opened, closed = run_rule(rate_of_change, [3300] * 10 + [step] * 10)

    assert opened["openedAt"] == MS + 1000  # first reading of the second second
    assert opened["detail"] == (
        f"chamber_temp {moved} 2000 K/s, above max 900 K/s for ascent"
    )
    assert closed["closedAt"] == MS + 1100


def test_peak_is_reported_once_per_call_while_open_and_not_with_a_close():
    rising = [3700, 3800, 3900] + [3700] * 7
    peak_then_close = [4000] + [3300] * 9

    events = run_rule(threshold, [3300] * 10 + rising + peak_then_close)

    assert [(e["event"], e["peakValue"]) for e in events] == [
        ("open", 3700),
        ("peak", 3900),
        ("close", 4000),
    ]
    assert events[1] == {
        "event": "peak",
        "flightId": FLIGHT_ID,
        "sensorId": "engine-1.chamber_temp",
        "rule": "threshold",
        "openedAt": MS + 1000,
        "peakValue": 3900,
    }


def steady(n, value):
    """10 readings of value in second SECOND + n."""
    return [{"ts": MS + n * 1000 + j * 100, "value": value} for j in range(10)]


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (
            [(0, 3700), (1, 3300), (0, 3700), (2, 3700)],
            [("open", MS, None), ("close", MS, MS + 1000), ("open", MS + 2000, None)],
        ),
        (
            [(0, 3700), (2, 3700), (1, 3300), (3, 3300)],
            [("open", MS, None), ("close", MS, MS + 3000)],
        ),
    ],
    ids=["redelivered offending second", "late normal second"],
)
def test_seconds_at_or_before_the_last_reading_are_skipped(seconds, expected):
    state, events = {}, []
    for n, value in seconds:
        state, new = threshold(CONFIG, state, steady(n, value), CTX)
        events += new

    assert [(e["event"], e["openedAt"], e.get("closedAt")) for e in events] == expected


def test_peak_is_measured_against_the_opening_phase_nominal():
    pre_launch = Context(
        FLIGHT_ID, "engine-1.chamber_temp", "pre_launch", 10, SECOND, 0
    )
    ignition = Context(FLIGHT_ID, "engine-1.chamber_temp", "ignition", 0, SECOND, 0)
    state, _ = threshold(CONFIG, {}, steady(0, 320), pre_launch)

    # Ignition's ramp starts at pre_launch values, outside ignition's limits.
    state, events = threshold(CONFIG, state, steady(1, 290), ignition)
    assert events == []
    _, [closed] = threshold(CONFIG, state, steady(2, 2800), ignition)

    assert closed["peakValue"] == 320


@pytest.mark.parametrize(
    ("rule", "opened_at"),
    [(threshold, [MS + 5000]), (rate_of_change, [])],
)
def test_no_alert_opens_within_grace_after_a_phase_change(rule, opened_at):
    # Jumps above max 2 s into the phase and stays there; grace is 5 s for both rules.
    events = run_rule(rule, [3300] * 20 + [3700] * 60, seconds_in_phase=0)

    assert [e["openedAt"] for e in events if e["event"] == "open"] == opened_at


def test_readings_within_a_second_are_taken_in_ts_order():
    readings = [{"ts": MS + 100, "value": 4000}, {"ts": MS, "value": 4100}]

    _, [opened] = threshold(CONFIG, {}, readings, CTX)

    assert (opened["openedAt"], opened["peakValue"]) == (MS, 4100)


def test_rate_ignores_readings_with_the_same_ts():
    readings = [{"ts": MS, "value": 3300}, {"ts": MS, "value": 3500}]

    assert rate_of_change(CONFIG, {}, readings, CTX)[1] == []


@pytest.mark.parametrize("rule", [threshold, rate_of_change])
def test_state_round_trips_through_json(rule):
    readings = [{"ts": MS, "value": 3300}, {"ts": MS + 100, "value": 4700}]

    state, [opened] = rule(CONFIG, {}, readings, CTX)

    assert json.loads(json.dumps(state)) == state
    assert state["openedAt"] == opened["openedAt"]


VIBRATION = CONFIG["sensorTypes"]["vibration"]["phases"]["ascent"]
FUEL_FLOW = CONFIG["sensorTypes"]["fuel_flow"]["phases"]["ascent"]


def slow_drift(lim, seconds):
    """The simulator's slow_drift fault on normal readings: ramp up to 3 x driftTolerance
    over windowSeconds, hold, and ramp back down before the fault ends."""
    window = CONFIG["rules"]["drift"]["windowSeconds"]
    return [
        v + 3 * lim["driftTolerance"] * min(i / 10, seconds - i / 10, window) / window
        for i, v in enumerate(normal(lim, seconds))
    ]


# Same fault as scenarios/drift.yaml: vibration drifts for 30 s, 20 s into the series.
DRIFTING = normal(VIBRATION, 20) + slow_drift(VIBRATION, 30) + normal(VIBRATION, 20)
# Same fault as scenarios/stuck.yaml: fuel_flow holds its nominal for 10 s.
STUCK = normal(FUEL_FLOW, 2) + [FUEL_FLOW["nominal"]] * 100 + normal(FUEL_FLOW, 2)


def test_drift_opens_at_the_start_of_the_sustained_run_and_closes():
    opened, closed = run_rule(
        drift, DRIFTING, "engine-1.vibration", seconds_in_phase=20
    )

    # The mean leaves tolerance 8 s into the fault; the alert opens sustainSeconds later.
    assert opened["openedAt"] == MS + 28000
    assert opened["detail"] == (
        "vibration mean 4.44132 g over 10 s is 1.44132 g from nominal 3 g, "
        "above tolerance 0.6 g for ascent"
    )
    # The mean is back within tolerance 1.7 s after the fault ends.
    assert closed["closedAt"] == MS + 51700
    assert closed["peakValue"] == max(DRIFTING)
    validate_alert(opened, closed)


# 331 readings end at MS + 33000, sustainSeconds after the run's first reading.
@pytest.mark.parametrize(("readings", "opened_at"), [(330, []), (331, [MS + 28000])])
def test_drift_must_last_sustain_seconds(readings, opened_at):
    events = run_rule(
        drift, DRIFTING[:readings], "engine-1.vibration", seconds_in_phase=20
    )

    assert [e["openedAt"] for e in events] == opened_at


@pytest.mark.parametrize(
    ("ascent_starts_at", "opens"),
    [(0, False), (CONFIG["rules"]["drift"]["graceSeconds"], True)],
    ids=["grace", "no grace"],
)
def test_drift_does_not_fire_on_the_ramp_after_a_phase_change(ascent_starts_at, opens):
    # Ignition, then ascent ramps from ignition's nominal over its rampSeconds while the
    # window still holds ignition readings.
    ignition = CONFIG["sensorTypes"]["chamber_temp"]["phases"]["ignition"]
    ramp = next(p["rampSeconds"] for p in CONFIG["phases"] if p["name"] == "ascent")
    gap = ignition["nominal"] - ASCENT["nominal"]
    values = normal(ignition, 20) + [
        v + gap * max(0, 1 - i / 10 / ramp) for i, v in enumerate(normal(ASCENT, 30))
    ]
    state, events = {}, []
    for n in range(50):
        phase, into = (
            ("ignition", 20 + n) if n < 20 else ("ascent", ascent_starts_at + n - 20)
        )
        readings = [
            {"ts": MS + n * 1000 + j * 100, "value": v}
            for j, v in enumerate(values[n * 10 : n * 10 + 10])
        ]
        ctx = Context(FLIGHT_ID, "engine-1.chamber_temp", phase, into, SECOND + n, 0)
        state, new = drift(CONFIG, state, readings, ctx)
        events += new

    assert any(e["event"] == "open" for e in events) == opens


@pytest.mark.parametrize("before", [0, 2], ids=["first readings", "after normal"])
def test_drift_stays_quiet_on_a_spike(before):
    # A spike is threshold's anomaly: its readings are outside [min, max].
    offset = ASCENT["max"] - ASCENT["min"] + ASCENT["maxRatePerSec"]
    spike = [v + offset for v in normal(ASCENT, 2)]

    assert run_rule(drift, normal(ASCENT, before) + spike + normal(ASCENT, 20)) == []


def test_drift_run_starting_within_grace_opens_at_the_end_of_grace():
    # The mean leaves tolerance 10 s into the phase, inside drift's 15 s grace.
    values = normal(VIBRATION, 2) + slow_drift(VIBRATION, 30) + normal(VIBRATION, 20)

    opened, _ = run_rule(drift, values, "engine-1.vibration", seconds_in_phase=0)

    assert opened["openedAt"] == MS + CONFIG["rules"]["drift"]["graceSeconds"] * 1000


def test_flatline_opens_at_the_first_stuck_reading_and_closes_when_it_moves():
    opened, closed = run_rule(flatline, STUCK, "engine-1.fuel_flow")

    assert opened["openedAt"] == MS + 2000
    assert opened["detail"] == "fuel_flow unchanged at 250 kg/s over 5 s for ascent"
    assert closed["closedAt"] == MS + 12000
    assert closed["peakValue"] == FUEL_FLOW["nominal"]
    validate_alert(opened, closed)


# 51 equal readings at 10 Hz span exactly unchangedSeconds.
@pytest.mark.parametrize(("readings", "opened_at"), [(70, []), (71, [MS + 2000])])
def test_flatline_must_last_unchanged_seconds(readings, opened_at):
    events = run_rule(flatline, STUCK[:readings], "engine-1.fuel_flow")

    assert [e["openedAt"] for e in events] == opened_at


@pytest.mark.parametrize(
    ("rule", "sensor_id", "values"),
    [
        (drift, "engine-1.vibration", DRIFTING),
        (flatline, "engine-1.fuel_flow", STUCK),
    ],
)
def test_redelivered_seconds_change_nothing(rule, sensor_id, values):
    once = run_rule(rule, values, sensor_id, seconds_in_phase=20)

    assert run_rule(rule, values, sensor_id, seconds_in_phase=20, deliveries=2) == once


def run_dropout(batches, config=CONFIG, seconds_in_phase=10):
    """One dropout call per (n, kind) for second SECOND + n: P = tank_pressure present,
    C = present with corrupt readings only, - = missing. State goes through JSON."""
    state, events = {}, []
    for n, kind in batches:
        readings = [{"ts": MS + n * 1000, "value": 340}] if kind == "P" else []
        corrupt = 3 if kind == "C" else 0
        ctx = Context(
            FLIGHT_ID,
            "engine-1.tank_pressure",
            "ascent",
            seconds_in_phase + n,
            SECOND + n,
            corrupt,
        )
        state, new = dropout(config, json.loads(json.dumps(state)), readings, ctx)
        events += new
    return events


def test_dropout_opens_after_missing_seconds_and_closes_when_present_again():
    # Same fault as scenarios/dropout.yaml: tank_pressure missing for 5 s.
    opened, closed = run_dropout(enumerate("PP-----PP"))

    key = {
        "flightId": FLIGHT_ID,
        "sensorId": "engine-1.tank_pressure",
        "rule": "dropout",
        "openedAt": MS + 2000,  # the first missing second
    }
    assert opened == {
        "event": "open",
        **key,
        "severity": "critical",
        "phase": "ascent",
        "peakValue": None,
        "detail": "tank_pressure missing for 3 s in ascent",
    }
    assert closed == {"event": "close", **key, "closedAt": MS + 7000, "peakValue": None}
    validate_alert(opened, closed)


@pytest.mark.parametrize("batches", ["P--P--P", "--C--C--"])
def test_dropout_needs_missing_seconds_in_a_row(batches):
    assert run_dropout(enumerate(batches)) == []


def test_present_but_corrupt_is_never_a_dropout():
    faults = yaml.safe_load((SCENARIOS / "corrupt.yaml").read_text())["faults"]
    for seconds in (f["durationSeconds"] for f in faults):
        assert seconds > CONFIG["rules"]["dropout"]["missingSeconds"]
        assert run_dropout(enumerate("P" + "C" * seconds + "P")) == []


@pytest.mark.parametrize(
    ("batches", "opened_at"), [("----P", []), ("--------", [MS + 5000])]
)
def test_dropout_does_not_count_missing_seconds_within_grace(batches, opened_at):
    rules = {**CONFIG["rules"], "dropout": {"graceSeconds": 5, "missingSeconds": 3}}

    events = run_dropout(enumerate(batches), {**CONFIG, "rules": rules}, 0)

    assert [e["openedAt"] for e in events] == opened_at


@pytest.mark.parametrize(
    ("batches", "expected"),
    [
        ([(0, "-"), (1, "-"), (1, "-")], []),
        (
            [(0, "-"), (1, "-"), (2, "-"), (1, "P"), (3, "P")],
            [("open", MS, None), ("close", MS, MS + 3000)],
        ),
        (
            [(0, "-"), (1, "-"), (2, "-"), (3, "P"), (2, "-"), (4, "-"), (5, "-")],
            [("open", MS, None), ("close", MS, MS + 3000)],
        ),
    ],
    ids=[
        "redelivered missing second",
        "late present second",
        "redelivered missing second after close",
    ],
)
def test_dropout_skips_seconds_at_or_before_the_last_batch(batches, expected):
    events = run_dropout(batches)

    assert [(e["event"], e["openedAt"], e.get("closedAt")) for e in events] == expected
