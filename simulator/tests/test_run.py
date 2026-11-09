import logging
import random
from itertools import pairwise
from pathlib import Path

import pytest

from simulator import __main__ as sim
from simulator.contracts import load_sensors
from simulator.faults import load_scenario
from simulator.flight import flight_seconds

START = 1_794_233_040
SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"


class Stop(Exception):
    pass


class Driver:
    """Fake clock: sleep advances time instantly; stops the loop after `seconds` ticks."""

    def __init__(self, seconds, fail_on=(), send_cost=0):
        self.now, self.end, self.fail_on, self.send_cost, self.sent = (
            START + 0.2,
            START + seconds,
            fail_on,
            send_cost,
            [],
        )

    def clock(self):
        return self.now

    def sleep(self, delay):
        if delay < 0:
            raise ValueError("sleep length must be non-negative")
        self.now += delay
        if self.now >= self.end:
            raise Stop

    def send(self, batch):
        self.now += self.send_cost
        if batch["second"] in self.fail_on:
            raise ConnectionRefusedError
        self.sent.append(batch)

    def run(self, cfg, seed=1, faults=()):
        with pytest.raises(Stop):
            sim.run(cfg, random.Random(seed), self.send, self.clock, self.sleep, faults)
        return self.sent


@pytest.fixture
def cfg():
    return load_sensors() | {"engineCount": 2}


def test_one_batch_per_engine_per_second(cfg):
    sent = Driver(5).run(cfg)
    assert [(b["second"], b["engineId"]) for b in sent] == [
        (s, f"engine-{e}") for s in range(START, START + 5) for e in (1, 2)
    ]


def test_next_flight_starts_after_idle_with_new_id_and_idle_sends_nothing(cfg, caplog):
    flight_len = flight_seconds(cfg)
    cycle = flight_len + cfg["idleSecondsBetweenFlights"]
    caplog.set_level(logging.INFO, "simulator")
    sent = Driver(cycle + 3).run(cfg)
    seconds = {b["second"] for b in sent}
    assert not seconds & set(range(START + flight_len, START + cycle))
    first, second = sent[0]["flightId"], sent[-1]["flightId"]
    assert first != second
    assert sent[-1]["second"] == START + cycle + 2
    assert [r.getMessage() for r in caplog.records] == [
        f"flight {first} started",
        f"flight {second} started",
    ]


def test_failed_send_does_not_stop_the_loop(cfg):
    sent = Driver(4, fail_on={START + 1}).run(cfg)
    assert {b["second"] for b in sent} == {START, START + 2, START + 3}


def test_slow_sends_skip_missed_seconds_and_keep_looping(cfg):
    sent = Driver(12, send_cost=1.3).run(cfg)
    seconds = sorted({b["second"] for b in sent})
    assert len(seconds) >= 3
    assert all(b - a > 1 for a, b in pairwise(seconds))


def test_scenario_drives_a_full_flight_and_repeats_each_flight(cfg):
    cycle = flight_seconds(cfg) + cfg["idleSecondsBetweenFlights"]
    clean = Driver(2 * cycle).run(cfg)
    faults = load_scenario(SCENARIOS / "dropout.yaml", cfg)
    faulted = Driver(2 * cycle).run(cfg, faults=faults)
    assert len(faulted) == len(clean)
    changed = [
        (c["second"] - START, c["engineId"])
        for c, f in zip(clean, faulted, strict=True)
        if c != f
    ]
    assert changed == [
        (start + s, "engine-1") for start in (0, cycle) for s in range(60, 65)
    ]
    for c, f in zip(clean, faulted, strict=True):
        if c != f:
            assert {r["sensorId"] for r in c["readings"]} - {
                r["sensorId"] for r in f["readings"]
            } == {"engine-1.tank_pressure"}


def test_none_scenario_changes_nothing(cfg):
    clean = Driver(flight_seconds(cfg)).run(cfg)
    assert (
        Driver(flight_seconds(cfg)).run(
            cfg, faults=load_scenario(SCENARIOS / "none.yaml", cfg)
        )
        == clean
    )


def test_main_runs_the_scenario_from_env(monkeypatch):
    seen = {}
    monkeypatch.setenv("SCENARIO", str(SCENARIOS / "spike.yaml"))
    monkeypatch.setattr(sim, "run", lambda *a, faults: seen.update(faults=faults))
    sim.main()
    assert [f["kind"] for f in seen["faults"]] == ["spike"]


def test_startup_fails_on_bad_config(monkeypatch, cfg):
    cfg["phases"][0]["rampSeconds"] = 1
    monkeypatch.setattr(sim, "load_sensors", lambda: cfg)
    with pytest.raises(ValueError, match="invalid sensors config"):
        sim.main()
