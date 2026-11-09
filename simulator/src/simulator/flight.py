"""Pure, deterministic vehicle model: flight schedule and sensor curves. Time is always passed in."""

import random
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Flight:
    flight_id: str
    start: int  # epoch second of the first pre_launch second


def check_config(cfg: dict) -> None:
    """Cross-field checks that sensors.schema.json cannot express. Raises ValueError."""
    errors = []
    phases = cfg["phases"]
    ramp = max(p["rampSeconds"] for p in phases)
    rules = cfg["rules"]
    if cfg["rateHz"] > 1000:
        errors.append("rateHz must be at most 1000 (ts has millisecond resolution)")
    if phases[0]["rampSeconds"] != 0:
        errors.append(
            "first phase rampSeconds must be 0 (a flight starts at its nominal)"
        )
    for p in phases:
        if p["rampSeconds"] > p["durationSeconds"]:
            errors.append(f"phase {p['name']}: rampSeconds exceeds durationSeconds")
    for rule in ("threshold", "rate_of_change"):
        if rules[rule]["graceSeconds"] < ramp:
            errors.append(
                f"rules.{rule}.graceSeconds must be >= longest rampSeconds ({ramp})"
            )
    if rules["drift"]["graceSeconds"] < ramp + rules["drift"]["windowSeconds"]:
        errors.append(
            "rules.drift.graceSeconds must be >= longest rampSeconds + windowSeconds"
        )
    for sensor, spec in cfg["sensorTypes"].items():
        for phase, lim in spec["phases"].items():
            if not (
                lim["min"] <= lim["nominal"] - lim["noise"]
                and lim["nominal"] + lim["noise"] <= lim["max"]
            ):
                errors.append(f"{sensor}.{phase}: need min <= nominal -/+ noise <= max")
    if errors:
        raise ValueError("invalid sensors config: " + "; ".join(errors))


def flight_seconds(cfg: dict) -> int:
    return sum(p["durationSeconds"] for p in cfg["phases"])


def start_flight(start: int, rng: random.Random) -> Flight:
    stamp = datetime.fromtimestamp(start, UTC).strftime("%Y%m%dT%H%M%SZ")
    return Flight(f"{stamp}-{rng.getrandbits(16):04x}", start)


def phase_at(cfg: dict, offset: int) -> tuple[int, int] | None:
    """(phase index, seconds into that phase) for a flight offset; None once the flight is over."""
    for i, p in enumerate(cfg["phases"]):
        if offset < p["durationSeconds"]:
            return i, offset
        offset -= p["durationSeconds"]
    return None


def _value(cfg: dict, sensor: str, i: int, t: float, rng: random.Random) -> float:
    """Ramp from the previous phase's nominal over rampSeconds, then hold nominal; plus noise. Never rounded."""
    phases = cfg["phases"]
    limits = cfg["sensorTypes"][sensor]["phases"][phases[i]["name"]]
    value = limits["nominal"]
    if t < phases[i]["rampSeconds"]:
        prev = cfg["sensorTypes"][sensor]["phases"][phases[i - 1]["name"]]["nominal"]
        value = prev + (value - prev) * t / phases[i]["rampSeconds"]
    return value + rng.uniform(-limits["noise"], limits["noise"])


def make_batch(cfg: dict, flight: Flight, engine: int, second: int) -> dict | None:
    """One engine's batch for an epoch second; None if the flight is over (idle)."""
    at = phase_at(cfg, second - flight.start)
    if at is None:
        return None
    i, into = at
    # Per-batch rng: output depends only on (flight, engine, second), not on call order.
    rng = random.Random(f"{flight.flight_id}:{engine}:{second}")
    hz = cfg["rateHz"]
    engine_id = f"engine-{engine}"
    readings = [
        {
            "sensorId": f"{engine_id}.{sensor}",
            "ts": second * 1000 + k * 1000 // hz,
            "value": _value(cfg, sensor, i, into + k / hz, rng),
            "unit": spec["unit"],
        }
        for sensor, spec in cfg["sensorTypes"].items()
        for k in range(hz)
    ]
    return {
        "schemaVersion": cfg["schemaVersion"],
        "flightId": flight.flight_id,
        "phase": cfg["phases"][i]["name"],
        "engineId": engine_id,
        "second": second,
        "readings": readings,
    }
