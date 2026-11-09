import copy
import random

import pytest

from simulator.contracts import load_sensors, validator
from simulator.flight import check_config, flight_seconds, make_batch, start_flight

START = 1_794_233_040
SEED = 42


@pytest.fixture
def cfg():
    return load_sensors()


def run_flight(cfg, seed=SEED):
    flight = start_flight(START, random.Random(seed))
    batches = [
        make_batch(cfg, flight, engine, second)
        for second in range(START, START + flight_seconds(cfg))
        for engine in range(1, cfg["engineCount"] + 1)
    ]
    return flight, batches


def test_flight_is_reproducible_with_exact_values(cfg):
    flight, batches = run_flight(cfg)
    assert flight.flight_id == "20261109T140400Z-a3b1"
    assert batches == run_flight(cfg)[1]
    assert batches != run_flight(cfg, seed=7)[1]
    first = batches[0]["readings"][0]
    assert first == {
        "sensorId": "engine-1.chamber_temp",
        "ts": START * 1000,
        "value": 289.07297230574864,
        "unit": "K",
    }
    assert batches[31]["readings"][0]["value"] == 1558.2019698052222


def test_ramp_then_hold_at_nominal(cfg):
    for spec in cfg["sensorTypes"].values():
        for lim in spec["phases"].values():
            lim["noise"] = 1e-9
    flight = start_flight(START, random.Random(SEED))

    def chamber(offset, k=0):
        b = make_batch(cfg, flight, 1, START + offset)
        return b["readings"][k]["value"]

    assert chamber(0) == pytest.approx(290)
    assert chamber(29, cfg["rateHz"] - 1) == pytest.approx(290)  # still pre_launch
    # ignition (starts at 30 s) ramps 290 -> 2800 over 2 s, then holds
    assert chamber(30) == pytest.approx(290)
    assert chamber(31) == pytest.approx(1545)
    assert chamber(32) == pytest.approx(2800)
    assert chamber(39) == pytest.approx(2800)
    # ascent (starts at 40 s) ramps 2800 -> 3300 over 5 s
    assert chamber(42) == pytest.approx(2800 + 500 * 2 / 5)
    assert chamber(45) == pytest.approx(3300)


def test_every_batch_matches_contract(cfg):
    _, batches = run_flight(cfg)
    assert len(batches) == flight_seconds(cfg)
    v = validator("http-batch")
    for b in batches:
        v.validate(b)
        assert len(b["readings"]) == len(cfg["sensorTypes"]) * cfg["rateHz"]
        by_sensor = {}
        for r in b["readings"]:
            by_sensor.setdefault(r["sensorId"], []).append(r["ts"])
        step = 1000 // cfg["rateHz"]
        for ts in by_sensor.values():
            assert ts == [b["second"] * 1000 + step * k for k in range(cfg["rateHz"])]


def test_batch_independent_of_call_order(cfg):
    flight, batches = run_flight(cfg)
    n = cfg["engineCount"]
    assert make_batch(cfg, flight, n, START + 50) == batches[50 * n + n - 1]


def test_phases_in_order_with_durations(cfg):
    _, batches = run_flight(cfg)
    seen = [b["phase"] for b in batches]
    for p in cfg["phases"]:
        assert seen.count(p["name"]) == p["durationSeconds"]
    assert list(dict.fromkeys(seen)) == [p["name"] for p in cfg["phases"]]


def test_idle_after_flight_has_no_batch(cfg):
    flight = start_flight(START, random.Random(SEED))
    assert make_batch(cfg, flight, 1, START + flight_seconds(cfg)) is None


def test_engine_count_from_config(cfg):
    cfg["engineCount"] = 3
    _, batches = run_flight(cfg)
    assert {b["engineId"] for b in batches} == {"engine-1", "engine-2", "engine-3"}


def test_values_within_limits_after_grace_and_never_flat(cfg):
    _, batches = run_flight(cfg)
    grace = cfg["rules"]["threshold"]["graceSeconds"]
    phase_start = {}
    for b in batches:
        phase_start.setdefault(b["phase"], b["second"])
    last = {}
    for b in batches:
        for r in b["readings"]:
            assert last.get(r["sensorId"]) != r["value"]
            last[r["sensorId"]] = r["value"]
            if b["second"] - phase_start[b["phase"]] >= grace:
                lim = cfg["sensorTypes"][r["sensorId"].split(".")[1]]["phases"][
                    b["phase"]
                ]
                assert lim["min"] <= r["value"] <= lim["max"]


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (
            lambda c: c["sensorTypes"]["vibration"]["phases"]["ascent"].update(min=5),
            "need min <= nominal",
        ),
        (
            lambda c: c["sensorTypes"]["vibration"]["phases"]["ascent"].update(
                noise=1e9
            ),
            "need min <= nominal",
        ),
        (
            lambda c: c["rules"]["threshold"].update(graceSeconds=1),
            "rules.threshold.graceSeconds",
        ),
        (
            lambda c: c["rules"]["rate_of_change"].update(graceSeconds=1),
            "rules.rate_of_change.graceSeconds",
        ),
        (
            lambda c: c["rules"]["drift"].update(graceSeconds=10),
            "rules.drift.graceSeconds",
        ),
        (lambda c: c["phases"][1].update(rampSeconds=11), "exceeds durationSeconds"),
        (lambda c: c["phases"][0].update(rampSeconds=1), "first phase rampSeconds"),
        (lambda c: c.update(rateHz=1001), "rateHz must be at most 1000"),
    ],
)
def test_bad_config_fails_startup_check(cfg, edit, message):
    check_config(cfg)
    broken = copy.deepcopy(cfg)
    edit(broken)
    with pytest.raises(ValueError, match=message):
        check_config(broken)
