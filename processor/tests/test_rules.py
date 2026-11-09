import json

import pytest

from processor.contracts import load_sensors, validator
from processor.rules import Context, rate_of_change, threshold

CONFIG = load_sensors()
ASCENT = CONFIG["sensorTypes"]["chamber_temp"]["phases"]["ascent"]
FLIGHT_ID = "20261109T140300Z-7f3a"
SECOND = 1794233041  # epoch second of the first reading in a run_rule series
MS = SECOND * 1000  # ts of the first reading in a run_rule series
CTX = Context(FLIGHT_ID, "engine-1.chamber_temp", "ascent", 10)


def run_rule(
    rule, values, sensor_id="engine-1.chamber_temp", phase="ascent", seconds_in_phase=10
):
    """Feed a fixed series to a rule at rateHz, one call per second, and return its events.

    State goes through JSON between seconds, as it will through Valkey.
    """
    hz = CONFIG["rateHz"]
    state, events = {}, []
    for n, i in enumerate(range(0, len(values), hz)):
        second = SECOND + n
        readings = [
            {"ts": second * 1000 + j * 1000 // hz, "value": v}
            for j, v in enumerate(values[i : i + hz])
        ]
        ctx = Context(FLIGHT_ID, sensor_id, phase, seconds_in_phase + n)
        state, new = rule(CONFIG, json.loads(json.dumps(state)), readings, ctx)
        events += new
    return events


def normal(lim, seconds):
    """Normal readings at the edge of noise, alternating, so consecutive readings move as
    fast as noise ever moves them."""
    return [lim["nominal"] + lim["noise"] * (-1) ** i for i in range(seconds * 10)]


@pytest.mark.parametrize("rule", [threshold, rate_of_change])
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
    alert = {k: v for k, v in opened.items() if k != "event"} | {
        "alertKey": f"{opened['openedAt']}#{opened['sensorId']}#threshold",
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
    pre_launch = Context(FLIGHT_ID, "engine-1.chamber_temp", "pre_launch", 10)
    ignition = Context(FLIGHT_ID, "engine-1.chamber_temp", "ignition", 0)
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
