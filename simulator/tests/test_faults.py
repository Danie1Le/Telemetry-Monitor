import copy
import random
from itertools import pairwise
from pathlib import Path

import pytest
from jsonschema import ValidationError

from simulator.contracts import load_sensors
from simulator.faults import apply_faults, load_scenario
from simulator.flight import flight_seconds, make_batch, start_flight

START = 1_794_233_040
SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"
AT, FOR = 60, 10  # ascent hold: ascent starts at 40 s, every rule grace ends by 55 s


@pytest.fixture
def cfg():
    return load_sensors() | {"engineCount": 2}


def fly(cfg, faults):
    """Clean and faulted batches for every second and engine of one seeded flight."""
    flight = start_flight(START, random.Random(1))
    clean = [
        make_batch(cfg, flight, e, s)
        for s in range(START, START + flight_seconds(cfg))
        for e in range(1, cfg["engineCount"] + 1)
    ]
    return clean, [apply_faults(cfg, faults, flight, copy.deepcopy(b)) for b in clean]


def fault(kind, sensor="chamber_temp", start=AT, duration=FOR):
    return {
        "kind": kind,
        "engine": 1,
        "sensor": sensor,
        "startSecond": start,
        "durationSeconds": duration,
    }


def values(batches, sensor_id):
    return [
        (b["second"], r["value"])
        for b in batches
        for r in b["readings"]
        if r["sensorId"] == sensor_id
    ]


def ascent(cfg, sensor):
    return cfg["sensorTypes"][sensor]["phases"]["ascent"]


@pytest.mark.parametrize("kind", ["spike", "dropout", "stuck_value", "slow_drift"])
def test_fault_changes_only_its_sensor_for_exactly_its_duration(cfg, kind):
    clean, faulted = fly(cfg, [fault(kind)])
    pairs = list(zip(clean, faulted, strict=True))
    changed = [(c["second"] - START, c["engineId"]) for c, f in pairs if c != f]
    assert changed == [(s, "engine-1") for s in range(AT, AT + FOR)]
    for c, f in pairs:
        rest = [r for r in c["readings"] if r["sensorId"] != "engine-1.chamber_temp"]
        assert [r for r in f["readings"] if r in rest] == rest


def test_spike_breaks_max_and_rate(cfg):
    lim = ascent(cfg, "chamber_temp")
    _, faulted = fly(cfg, [fault("spike", duration=2)])
    series = values(faulted, "engine-1.chamber_temp")
    hz = cfg["rateHz"]
    before, after = series[AT * hz - 1][1], series[(AT + 2) * hz][1]
    during = [v for s, v in series if s - START in (AT, AT + 1)]
    assert len(during) == 2 * hz
    assert min(during) > lim["max"]
    assert (during[0] - before) * hz > lim["maxRatePerSec"]
    assert (during[-1] - after) * hz > lim["maxRatePerSec"]
    assert after <= lim["max"]


def test_dropout_removes_the_sensor_for_exactly_the_duration(cfg):
    _, faulted = fly(cfg, [fault("dropout", sensor="tank_pressure", duration=5)])
    missing = [
        b["second"] - START
        for b in faulted
        if b["engineId"] == "engine-1"
        and not any(r["sensorId"] == "engine-1.tank_pressure" for r in b["readings"])
    ]
    assert missing == [60, 61, 62, 63, 64]


def test_stuck_value_holds_the_phase_nominal(cfg):
    _, faulted = fly(cfg, [fault("stuck_value", sensor="fuel_flow")])
    series = values(faulted, "engine-1.fuel_flow")
    stuck = {v for s, v in series if AT <= s - START < AT + FOR}
    assert stuck == {ascent(cfg, "fuel_flow")["nominal"]}
    assert len({v for s, v in series if s - START == AT + FOR}) == cfg["rateHz"]


@pytest.mark.parametrize(
    "sensor",
    ["chamber_temp", "tank_pressure", "fuel_flow", "valve_position", "vibration"],
)
def test_drift_scenario_fires_only_drift_on_every_sensor(cfg, sensor):
    """drift.yaml's fault, moved to each sensor: rolling mean out of tolerance for longer
    than sustainSeconds, never over max, and every step under maxRatePerSec, including
    the return to normal after the fault ends."""
    lim, hz = ascent(cfg, sensor), cfg["rateHz"]
    drift = cfg["rules"]["drift"]
    (f,) = load_scenario(SCENARIOS / "drift.yaml", cfg)
    at, end = f["startSecond"], f["startSecond"] + f["durationSeconds"]
    _, faulted = fly(cfg, [f | {"sensor": sensor}])
    series = [
        v
        for s, v in values(faulted, f"engine-1.{sensor}")
        if s - START < end + drift["windowSeconds"]
    ]
    assert max(series) < lim["max"]
    steps = [abs(b - a) * hz for a, b in pairwise(series[at * hz - 1 :])]
    assert max(steps) < lim["maxRatePerSec"]
    # rolling mean at the end of each second of the fault
    window = drift["windowSeconds"] * hz
    out = [
        abs(sum(series[e - window : e]) / window - lim["nominal"])
        > lim["driftTolerance"]
        for e in range((at + 1) * hz, end * hz + 1, hz)
    ]
    first_out = out.index(True)
    assert all(out[first_out:])
    assert len(out) - 1 - first_out > drift["sustainSeconds"]


def test_fault_on_other_engine_or_after_flight_end_changes_nothing(cfg):
    clean, faulted = fly(
        cfg, [fault("spike") | {"engine": 3}, fault("spike", start=flight_seconds(cfg))]
    )
    assert faulted == clean


@pytest.mark.parametrize("name", ["spike", "dropout", "stuck", "drift"])
def test_scenario_faults_fall_in_ascent_hold(cfg, name):
    faults = load_scenario(SCENARIOS / f"{name}.yaml", cfg)
    assert len(faults) == 1
    ascent_start = sum(p["durationSeconds"] for p in cfg["phases"][:2])
    ascent_end = ascent_start + cfg["phases"][2]["durationSeconds"]
    grace = max(r["graceSeconds"] for r in cfg["rules"].values())
    for f in faults:
        assert ascent_start + grace <= f["startSecond"]
        assert f["startSecond"] + f["durationSeconds"] <= ascent_end


def test_none_scenario_has_no_faults(cfg):
    assert load_scenario(SCENARIOS / "none.yaml", cfg) == []


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (
            "faults: [{kind: melt, engine: 1, sensor: vibration, startSecond: 60, durationSeconds: 2}]",
            ValidationError,
        ),
        (
            "faults: [{kind: spike, engine: 1, sensor: vibration, startSecond: 60}]",
            ValidationError,
        ),
        (
            "faults: [{kind: spike, engine: 0, sensor: vibration, startSecond: 60, durationSeconds: 2}]",
            ValidationError,
        ),
        (
            "faults: [{kind: spike, engine: 1, sensor: vibration, startSecond: -1, durationSeconds: 2}]",
            ValidationError,
        ),
        (
            "faults: [{kind: spike, engine: 1, sensor: vibration, startSecond: 60, durationSeconds: 0}]",
            ValidationError,
        ),
        ("fault: []", ValidationError),
        (
            "faults: [{kind: spike, engine: 1, sensor: oxygen, startSecond: 60, durationSeconds: 2}]",
            ValueError,
        ),
        (
            "faults: [{kind: spike, engine: 3, sensor: vibration, startSecond: 60, durationSeconds: 2}]",
            ValueError,
        ),
        (
            "faults: [{kind: spike, engine: 1, sensor: vibration, startSecond: 170, durationSeconds: 11}]",
            ValueError,
        ),
    ],
)
def test_bad_scenario_fails_validation(cfg, tmp_path, text, error):
    path = tmp_path / "bad.yaml"
    path.write_text(text)
    with pytest.raises(error):
        load_scenario(path, cfg)


def test_fault_may_end_exactly_at_the_flights_end(cfg, tmp_path):
    path = tmp_path / "end.yaml"
    path.write_text(
        "faults: [{kind: spike, engine: 2, sensor: vibration, startSecond: 170, durationSeconds: 10}]"
    )
    assert len(load_scenario(path, cfg)) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    # vibration ascent needs max > 5.1 and maxRatePerSec > 6.18
    [("max", 5.0), ("maxRatePerSec", 6.0)],
)
def test_config_where_slow_drift_would_break_a_limit_is_rejected(cfg, field, value):
    ascent(cfg, "vibration")[field] = value
    with pytest.raises(ValueError, match="vibration.ascent: slow_drift"):
        load_scenario(SCENARIOS / "none.yaml", cfg)
