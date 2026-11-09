# Telemetry Monitor

Telemetry from a simulated launch vehicle: sensors produce readings, and the system detects anomalies in them and raises alerts.

## Language

### The vehicle

**Vehicle**:
The simulated launch vehicle whose telemetry the system monitors.
_Avoid_: Rocket, craft, device

**Sensor**:
One named measurement point on the vehicle, such as engine chamber temperature or tank pressure.
_Avoid_: Channel, probe, signal

**Reading**:
One sensor's value at one instant.
_Avoid_: Sample, data point, measurement, event

### Abnormal behaviour

**Fault**:
An abnormal condition the simulator injects on purpose, such as a pressure spike or a sensor dropout. A fault is a cause.
_Avoid_: Failure, error, anomaly

**Anomaly**:
Abnormal behaviour the system detects in readings. An anomaly is an observation, which may or may not come from a fault.
_Avoid_: Fault, outlier, incident

**Alert**:
The record of one detected anomaly.
_Avoid_: Alarm, notification, warning
