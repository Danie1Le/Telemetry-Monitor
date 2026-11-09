"""Rules as pure functions over one sensor's readings for one second.

A rule is `(config, state, readings, ctx) -> (new_state, events)`:
- `config` is the parsed sensors.yaml, `readings` the sensor's `[{ts, value}]` from one batch.
- `state` is a small JSON-serialisable dict for one flight, sensor and rule; start with {}.
- An `open` event holds the alert's fields except alertKey and ttl. A `peak` event holds
  the alert's key fields and its new peakValue; at most one per call, never with a close.
  A `close` event holds the alert's key fields, closedAt and the final peakValue.

Readings with a ts at or before the last one seen (`lastTs`) are skipped, so a redelivered
or late second cannot re-open or close an alert.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Context:
    flight_id: str
    sensor_id: str
    phase: str
    # since the phase began; no alert opens within the rule's grace
    seconds_in_phase: int


def threshold(config: dict, state: dict, readings: list[dict], ctx: Context):
    """Fires while a value is outside the phase's [min, max]."""
    return _evaluate("threshold", config, state, readings, ctx, _outside_limits)


def rate_of_change(config: dict, state: dict, readings: list[dict], ctx: Context):
    """Fires when |delta value| / delta t between consecutive readings exceeds maxRatePerSec.

    Closes once the rate is normal and the value is back inside [min, max], so a spike's
    jump up and jump back down are one anomaly, not two.
    """
    return _evaluate("rate_of_change", config, state, readings, ctx, _too_fast)


def _outside_limits(reading: dict, lim: dict, state: dict) -> str | None:
    value, unit = reading["value"], lim["unit"]
    if value > lim["max"]:
        return f"{value:g} {unit} above max {lim['max']:g} {unit}"
    if value < lim["min"]:
        return f"{value:g} {unit} below min {lim['min']:g} {unit}"
    return None


def _too_fast(reading: dict, lim: dict, state: dict) -> str | None:
    if "lastTs" not in state:
        return None
    dt = reading["ts"] - state["lastTs"]
    rate = (reading["value"] - state["lastValue"]) * 1000 / dt
    if abs(rate) <= lim["maxRatePerSec"]:
        return None
    unit, limit = lim["unit"], lim["maxRatePerSec"]
    moved = "rose" if rate > 0 else "fell"
    return f"{moved} {abs(rate):g} {unit}/s, above max {limit:g} {unit}/s"


def _evaluate(rule, config, state, readings, ctx, offence):
    """One alert per anomaly: open on the first offending reading, track the peak, close
    on the first reading that is normal and inside [min, max]."""
    sensor_type = ctx.sensor_id.split(".", 1)[1]
    spec = config["sensorTypes"][sensor_type]
    lim = {**spec["phases"][ctx.phase], "unit": spec["unit"]}
    in_grace = ctx.seconds_in_phase < config["rules"][rule]["graceSeconds"]
    key = {"flightId": ctx.flight_id, "sensorId": ctx.sensor_id, "rule": rule}
    state, events = dict(state), []
    reported_peak = state.get("peakValue")
    for reading in sorted(readings, key=lambda r: r["ts"]):
        ts, value = reading["ts"], reading["value"]
        if "lastTs" in state and ts <= state["lastTs"]:
            continue
        detail = offence(reading, lim, state)
        state |= {"lastTs": ts, "lastValue": value}
        if "openedAt" in state:
            if detail is None and lim["min"] <= value <= lim["max"]:
                events.append(
                    {
                        "event": "close",
                        **key,
                        "openedAt": state.pop("openedAt"),
                        "closedAt": ts,
                        "peakValue": state.pop("peakValue"),
                    }
                )
                del state["nominal"]
            # Against the opening phase's nominal: the alert may outlive its phase.
            elif abs(value - state["nominal"]) > abs(
                state["peakValue"] - state["nominal"]
            ):
                state["peakValue"] = value
        elif detail is not None and not in_grace:
            state |= {"openedAt": ts, "peakValue": value, "nominal": lim["nominal"]}
            reported_peak = value
            events.append(
                {
                    "event": "open",
                    **key,
                    "severity": spec["severity"],
                    "phase": ctx.phase,
                    "openedAt": ts,
                    "peakValue": value,
                    "detail": f"{sensor_type} {detail} for {ctx.phase}",
                }
            )
    if "openedAt" in state and state["peakValue"] != reported_peak:
        events.append(
            {
                "event": "peak",
                **key,
                "openedAt": state["openedAt"],
                "peakValue": state["peakValue"],
            }
        )
    return state, events
