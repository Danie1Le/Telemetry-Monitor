# Telemetry Monitor

Telemetry from a simulated launch vehicle: sensors produce readings, and the system detects anomalies in them and raises alerts.

## Language

### The vehicle

**Vehicle**:
The simulated launch vehicle whose telemetry the system monitors.
_Avoid_: Rocket, craft, device

**Engine**:
One engine on the vehicle. Every sensor belongs to exactly one engine.
_Avoid_: Motor, unit, booster

**Sensor**:
One named measurement point on the vehicle, such as engine chamber temperature or tank pressure.
_Avoid_: Channel, probe, signal

**Reading**:
One sensor's value at one instant.
_Avoid_: Sample, data point, measurement, event

**Batch**:
All of one engine's readings for one second of a flight. Every sensor on that engine is expected in every batch.
_Avoid_: Packet, frame, payload, chunk

**Flight**:
One simulated run of the vehicle, from pre-launch to engine cutoff.
_Avoid_: Run, mission, launch

**Phase**:
One stage of a flight: pre-launch, ignition, ascent, or engine cutoff. Normal sensor behaviour differs by phase.
_Avoid_: Stage, mode, step

**Scenario**:
A scripted timeline of faults to inject during a flight, used for repeatable tests and demos.
_Avoid_: Script, test case, playbook

### Abnormal behaviour

**Fault**:
An abnormal condition the simulator injects on purpose, such as a pressure spike or a sensor dropout. A fault is a cause.
_Avoid_: Failure, error, anomaly

**Corrupt reading**:
A reading that is malformed as data, such as one with a wrong unit, a missing field, or a stale timestamp. It is a data problem, not a vehicle problem, so it is never a fault.
_Avoid_: Bad fault, invalid fault, garbage

**Anomaly**:
Abnormal behaviour the system detects in readings. An anomaly is an observation, which may or may not come from a fault.
_Avoid_: Fault, outlier, incident

**Rule**:
A named check that detects one kind of anomaly, such as threshold, rate of change, drift, dropout, or flatline.
_Avoid_: Detector, check, policy

**Alert**:
The record of one detected anomaly. It opens when a rule first fires and closes when the readings return to normal. Each alert is either a warning or critical.
_Avoid_: Alarm, notification, warning
