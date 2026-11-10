"""Rules as pure functions over one sensor's readings for one second.

A rule is `(config, state, readings, ctx) -> (new_state, events)`:
- `config` is the parsed sensors.yaml, `readings` the sensor's `[{ts, value}]` from one batch.
- `state` is a small JSON-serialisable dict for one flight, sensor and rule; start with {}.
- An `open` event holds the alert's fields except alertKey and ttl. A `peak` event holds
  the alert's key fields and its new peakValue; at most one per call, never with a close.
  A `close` event holds the alert's key fields, closedAt and the final peakValue.

Readings with a ts at or before the last one seen (`lastTs`) are skipped, so a redelivered
or late second cannot re-open or close an alert. Dropout does the same per batch second.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Context:
    flight_id: str
    sensor_id: str
    phase: str
    # since the phase began; no alert opens within the rule's grace
    seconds_in_phase: int
    # the batch's epoch second, and the sensor's rejectedCounts in it (0 if none)
    second: int
    corrupt: int


def threshold(config: dict, state: dict, readings: list[dict], ctx: Context):
    """Fires while a value is outside the phase's [min, max]."""
    return _evaluate("threshold", config, state, readings, ctx, _outside_limits)


def rate_of_change(config: dict, state: dict, readings: list[dict], ctx: Context):
    """Fires when |delta value| / delta t between consecutive readings exceeds maxRatePerSec.

    Closes once the rate is normal and the value is back inside [min, max], so a spike's
    jump up and jump back down are one anomaly, not two.
    """
    return _evaluate("rate_of_change", config, state, readings, ctx, _too_fast)


def drift(config: dict, state: dict, readings: list[dict], ctx: Context):
    """Fires when the mean over windowSeconds stays more than driftTolerance from the
    phase's nominal for sustainSeconds. Opens at the first reading of that run."""
    return _evaluate("drift", config, state, readings, ctx, _drifting)


def flatline(config: dict, state: dict, readings: list[dict], ctx: Context):
    """Fires when a value stays exactly equal for unchangedSeconds. Noise is always > 0,
    so normal readings never repeat. Opens at the first reading of the run."""
    return _evaluate("flatline", config, state, readings, ctx, _unchanged)


def dropout(config: dict, state: dict, readings: list[dict], ctx: Context):
    """Fires when a sensor is missing from its engine's batches for missingSeconds in a row.

    Call once per expected sensor per batch, with readings=[] when the sensor is absent.
    A sensor with only corrupt readings is present. Opens at the first missing second.
    """
    sensor_type, spec, rules, key = _setup("dropout", config, ctx)
    state = dict(state)
    if "lastSecond" in state and ctx.second <= state["lastSecond"]:
        return state, []
    state["lastSecond"] = ctx.second
    if readings or ctx.corrupt:
        state.pop("missingFrom", None)
        if "openedAt" not in state:
            return state, []
        closed = {"openedAt": state.pop("openedAt"), "closedAt": ctx.second * 1000}
        return state, [{"event": "close", **key, **closed, "peakValue": None}]
    if ctx.seconds_in_phase < rules["graceSeconds"]:
        state.pop("missingFrom", None)
        return state, []
    missing_from = state.setdefault("missingFrom", ctx.second)
    missing = ctx.second - missing_from + 1
    if "openedAt" in state or missing < rules["missingSeconds"]:
        return state, []
    state["openedAt"] = missing_from * 1000
    detail = f"{sensor_type} missing for {missing} s in {ctx.phase}"
    return state, [_opened(key, spec, ctx, state["openedAt"], None, detail)]


def _outside_limits(reading: dict, lim: dict, state: dict):
    value, unit = reading["value"], lim["unit"]
    if value > lim["max"]:
        return f"{value:g} {unit} above max {lim['max']:g} {unit}", reading["ts"]
    if value < lim["min"]:
        return f"{value:g} {unit} below min {lim['min']:g} {unit}", reading["ts"]
    return None


def _too_fast(reading: dict, lim: dict, state: dict):
    # A baseline from inside grace may be the ramp's last reading, a step noise can push
    # past the max: it only seeds the next rate, unless an alert is already open.
    if "lastTs" not in state or (state.get("lastInGrace") and "openedAt" not in state):
        return None
    dt = reading["ts"] - state["lastTs"]
    rate = (reading["value"] - state["lastValue"]) * 1000 / dt
    if abs(rate) <= lim["maxRatePerSec"]:
        # An open alert stays open until the value is back inside [min, max].
        return _outside_limits(reading, lim, state) if "openedAt" in state else None
    unit, limit = lim["unit"], lim["maxRatePerSec"]
    moved = "rose" if rate > 0 else "fell"
    detail = f"{moved} {abs(rate):g} {unit}/s, above max {limit:g} {unit}/s"
    return detail, reading["ts"]


def _drifting(reading: dict, lim: dict, state: dict):
    ts, window_seconds = reading["ts"], lim["windowSeconds"]
    second = ts // 1000
    # Per-second [second, sum, count] buckets keep the state to windowSeconds entries.
    window = [b for b in state.get("window", []) if b[0] > second - window_seconds]
    # Readings outside [min, max] are threshold's anomaly; a spike must not move the mean.
    if lim["min"] <= reading["value"] <= lim["max"]:
        _, total, count = (
            window.pop() if window and window[-1][0] == second else (0, 0, 0)
        )
        window.append([second, total + reading["value"], count + 1])
    state["window"] = window
    if not window:
        return None
    mean = sum(b[1] for b in window) / sum(b[2] for b in window)
    off = abs(mean - lim["nominal"])
    unit, tolerance = lim["unit"], lim["driftTolerance"]
    if off <= tolerance:
        state.pop("runStart", None)
        return None
    detail = (
        f"mean {mean:g} {unit} over {window_seconds} s is {off:g} {unit} from "
        f"nominal {lim['nominal']:g} {unit}, above tolerance {tolerance:g} {unit}"
    )
    return _sustained(state, ts, ts, lim["sustainSeconds"], detail)


def _unchanged(reading: dict, lim: dict, state: dict):
    if state.get("lastValue") != reading["value"]:
        state.pop("runStart", None)
        return None
    seconds = lim["unchangedSeconds"]
    detail = f"unchanged at {reading['value']:g} {lim['unit']} over {seconds} s"
    # The run starts at the previous reading: the first with this value.
    return _sustained(state, reading["ts"], state["lastTs"], seconds, detail)


def _sustained(state: dict, ts: int, start: int, seconds: int, detail: str):
    """(detail, run start) once the run that began at `start` has lasted `seconds`."""
    start = state.setdefault("runStart", start)
    return (detail, start) if ts - start >= seconds * 1000 else None


def _setup(rule, config, ctx):
    sensor_type = ctx.sensor_id.split(".", 1)[1]
    spec = config["sensorTypes"][sensor_type]
    key = {"flightId": ctx.flight_id, "sensorId": ctx.sensor_id, "rule": rule}
    return sensor_type, spec, config["rules"][rule], key


def _opened(key, spec, ctx, opened_at, peak, detail):
    return {
        "event": "open",
        **key,
        "severity": spec["severity"],
        "phase": ctx.phase,
        "openedAt": opened_at,
        "peakValue": peak,
        "detail": detail,
    }


def _evaluate(rule, config, state, readings, ctx, offence):
    """One alert per anomaly: open when `offence` first returns (detail, openedAt), track
    the peak, close on the first reading for which it returns None."""
    sensor_type, spec, rules, key = _setup(rule, config, ctx)
    lim = {**spec["phases"][ctx.phase], **rules, "unit": spec["unit"]}
    in_grace = ctx.seconds_in_phase < rules["graceSeconds"]
    state, events = dict(state), []
    reported_peak = state.get("peakValue")
    for reading in sorted(readings, key=lambda r: r["ts"]):
        ts, value = reading["ts"], reading["value"]
        if "lastTs" in state and ts <= state["lastTs"]:
            continue
        found = offence(reading, lim, state)
        if in_grace:
            # A sustained run starts after grace, so openedAt never falls inside it.
            state.pop("runStart", None)
        state |= {"lastTs": ts, "lastValue": value, "lastInGrace": in_grace}
        if "openedAt" in state:
            if found is None:
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
        elif found is not None and not in_grace:
            detail, opened_at = found
            state |= {
                "openedAt": opened_at,
                "peakValue": value,
                "nominal": lim["nominal"],
            }
            reported_peak = value
            detail = f"{sensor_type} {detail} for {ctx.phase}"
            events.append(_opened(key, spec, ctx, opened_at, value, detail))
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
