"""Faults: post-process a batch from make_batch. A fault's startSecond counts from its flight's start."""

from pathlib import Path

from simulator.contracts import load_yaml
from simulator.flight import Flight, flight_seconds

DRIFT_CAP = 3  # slow_drift's peak offset, in driftTolerance units


def load_scenario(path: Path, cfg: dict) -> list[dict]:
    """The scenario's faults, validated against scenario.schema.json and the sensor config."""
    faults = load_yaml(path, "scenario")["faults"]
    unknown = {f["sensor"] for f in faults} - cfg["sensorTypes"].keys()
    if unknown:
        raise ValueError(f"scenario {path}: unknown sensor types {sorted(unknown)}")
    for f in faults:
        if f["engine"] > cfg["engineCount"]:
            raise ValueError(f"scenario {path}: engine {f['engine']} > engineCount")
        if f["startSecond"] + f["durationSeconds"] > flight_seconds(cfg):
            raise ValueError(f"scenario {path}: a fault runs past the flight's end")
    window = cfg["rules"]["drift"]["windowSeconds"]
    for sensor, spec in cfg["sensorTypes"].items():
        for phase, lim in spec["phases"].items():
            cap = DRIFT_CAP * lim["driftTolerance"]
            # slow_drift must fire drift only: stay under max, and keep each step
            # (noise swing + ramp) under maxRatePerSec.
            if lim["nominal"] + lim["noise"] + cap >= lim["max"]:
                raise ValueError(f"{sensor}.{phase}: slow_drift would pass max")
            if 2 * lim["noise"] * cfg["rateHz"] + cap / window >= lim["maxRatePerSec"]:
                raise ValueError(
                    f"{sensor}.{phase}: slow_drift would pass maxRatePerSec"
                )
    return faults


def apply_faults(cfg: dict, faults: list[dict], flight: Flight, batch: dict) -> dict:
    """Apply every fault active in the batch's second, in place, and return the batch."""
    offset = batch["second"] - flight.start
    for fault in faults:
        start = fault["startSecond"]
        if batch["engineId"] != f"engine-{fault['engine']}" or not (
            start <= offset < start + fault["durationSeconds"]
        ):
            continue
        sensor_id = f"{batch['engineId']}.{fault['sensor']}"
        if fault["kind"] == "dropout":
            batch["readings"] = [
                r for r in batch["readings"] if r["sensorId"] != sensor_id
            ]
            continue
        lim = cfg["sensorTypes"][fault["sensor"]]["phases"][batch["phase"]]
        start_ms = (flight.start + start) * 1000
        for r in batch["readings"]:
            if r["sensorId"] == sensor_id:
                t = (r["ts"] - start_ms) / 1000
                r["value"] = _faulted(fault, r["value"], t, lim, cfg)
    return batch


def _faulted(fault: dict, value: float, t: float, lim: dict, cfg: dict) -> float:
    """A reading's value t seconds into a fault, from that sensor's limits in this phase."""
    if fault["kind"] == "spike":
        # From any in-limit value: lands above max, and the jump (>= maxRatePerSec, as noise
        # <= (max - min) / 2) happens within one reading, so the rate exceeds maxRatePerSec.
        return value + lim["max"] - lim["min"] + lim["maxRatePerSec"]
    if fault["kind"] == "stuck_value":
        return lim["nominal"]
    # slow_drift: ramp up over the drift window, hold the cap, and ramp back down before the
    # fault ends, since even a cap of driftTolerance dropped in one reading breaks maxRatePerSec.
    window = cfg["rules"]["drift"]["windowSeconds"]
    ramp = min(t, fault["durationSeconds"] - t, window) / window
    return value + DRIFT_CAP * lim["driftTolerance"] * ramp
