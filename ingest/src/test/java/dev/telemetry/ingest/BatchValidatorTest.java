package dev.telemetry.ingest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.io.IOException;
import java.nio.file.Path;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.web.server.ResponseStatusException;

class BatchValidatorTest {

  static final long SECOND = 1794233040L;
  static final long NOW = SECOND * 1000 + 500;
  static final String SENSOR = "engine-1.tank_pressure";

  static BatchValidator validatorAt(long nowMillis) {
    try {
      return new BatchValidator(
          Path.of("../sensors.yaml"), Clock.fixed(Instant.ofEpochMilli(nowMillis), ZoneOffset.UTC));
    } catch (IOException e) {
      throw new IllegalStateException(e);
    }
  }

  static Map<String, Object> reading(Object sensorId, Object ts, Object value, Object unit) {
    Map<String, Object> r = new HashMap<>();
    r.put("sensorId", sensorId);
    r.put("ts", ts);
    r.put("value", value);
    r.put("unit", unit);
    return r;
  }

  static Map<String, Object> good(long ts) {
    return reading(SENSOR, ts, 340.4, "kPa");
  }

  static Batch batch(List<Map<String, Object>> readings) {
    return new Batch(1, "20261109T140300Z-7f3a", "ascent", "engine-1", SECOND, readings);
  }

  /** A batch of one good and one corrupt reading: the corrupt one is dropped and counted. */
  void assertDropped(
      BatchValidator validator, Map<String, Object> corrupt, Map<String, Integer> counts) {
    var result = validator.validate(batch(List.of(good(SECOND * 1000), corrupt)));
    assertEquals(1, result.accepted());
    assertEquals(1, result.rejected());
    assertEquals(
        Map.of(SENSOR, List.of(Map.of("ts", SECOND * 1000, "value", 340.4))),
        result.message().get("readings"));
    assertEquals(counts, result.message().get("rejectedCounts"));
  }

  @Test
  void validReadingsAreGroupedBySensor() {
    var readings = new ArrayList<>(List.of(good(SECOND * 1000), good(SECOND * 1000 + 100)));
    readings.add(reading("engine-1.chamber_temp", SECOND * 1000, 3300.5, "K"));
    var result = validatorAt(NOW).validate(batch(readings));
    assertEquals(3, result.accepted());
    assertEquals(0, result.rejected());
    var grouped = (Map<?, ?>) result.message().get("readings");
    assertEquals(2, ((List<?>) grouped.get(SENSOR)).size());
    assertEquals(1, ((List<?>) grouped.get("engine-1.chamber_temp")).size());
    assertEquals(Map.of(), result.message().get("rejectedCounts"));
    assertEquals(NOW, result.message().get("receivedAt"));
  }

  @Test
  void wrongUnit() {
    assertDropped(validatorAt(NOW), reading(SENSOR, SECOND * 1000, 1.0, "psi"), Map.of(SENSOR, 1));
  }

  @Test
  void missingField() {
    var r = good(SECOND * 1000);
    r.remove("value");
    assertDropped(validatorAt(NOW), r, Map.of(SENSOR, 1));
  }

  @Test
  void wrongTypedField() {
    assertDropped(
        validatorAt(NOW), reading(SENSOR, SECOND * 1000, "340", "kPa"), Map.of(SENSOR, 1));
  }

  @Test
  void stale() {
    // skew.staleSeconds is 5: a reading 5.4 s old passes, 6.4 s old does not.
    var ts = SECOND * 1000 + 100;
    assertEquals(1, validatorAt(ts + 5000).validate(batch(List.of(good(ts)))).accepted());
    var result = validatorAt(ts + 6000).validate(batch(List.of(good(ts))));
    assertEquals(1, result.rejected());
    assertEquals(Map.of(SENSOR, 1), result.message().get("rejectedCounts"));
  }

  @Test
  void future() {
    // skew.futureSeconds is 2: a reading 2 s ahead passes, 3 s ahead does not.
    var ts = SECOND * 1000 + 100;
    assertEquals(1, validatorAt(ts - 2000).validate(batch(List.of(good(ts)))).accepted());
    var result = validatorAt(ts - 3000).validate(batch(List.of(good(ts))));
    assertEquals(1, result.rejected());
    assertEquals(Map.of(SENSOR, 1), result.message().get("rejectedCounts"));
  }

  @Test
  void nonFiniteValue() {
    assertDropped(
        validatorAt(NOW),
        reading(SENSOR, SECOND * 1000, Double.POSITIVE_INFINITY, "kPa"),
        Map.of(SENSOR, 1));
  }

  @Test
  void otherEnginesSensorCountsOnlyInTotal() {
    assertDropped(
        validatorAt(NOW), reading("engine-2.tank_pressure", SECOND * 1000, 1.0, "kPa"), Map.of());
  }

  @Test
  void unknownSensorType() {
    assertDropped(
        validatorAt(NOW),
        reading("engine-1.warp_core", SECOND * 1000, 1.0, "kPa"),
        Map.of("engine-1.warp_core", 1));
  }

  @Test
  void tsOutsideBatchSecond() {
    assertDropped(validatorAt(NOW), good(SECOND * 1000 + 1000), Map.of(SENSOR, 1));
    assertDropped(validatorAt(NOW), good(SECOND * 1000 - 1), Map.of(SENSOR, 1));
  }

  @Test
  void unattributableCorruptReadingOnlyCountsInTotal() {
    var result =
        validatorAt(NOW).validate(batch(List.of(reading("garbage", SECOND * 1000, 1.0, "kPa"))));
    assertEquals(1, result.rejected());
    assertEquals(Map.of(), result.message().get("rejectedCounts"));
    assertEquals(Map.of(), result.message().get("readings"));
  }

  @Test
  void sensorWithAllReadingsRejectedAppearsOnlyInRejectedCounts() {
    var result =
        validatorAt(NOW).validate(batch(List.of(reading(SENSOR, SECOND * 1000, 1.0, "psi"))));
    assertEquals(Map.of(), result.message().get("readings"));
    assertEquals(Map.of(SENSOR, 1), result.message().get("rejectedCounts"));
  }

  @Test
  void badEnvelopeThrows() {
    var v = validatorAt(NOW);
    var id = "20261109T140300Z-7f3a";
    assertThrows(
        ResponseStatusException.class,
        () -> v.validate(new Batch(2, id, "ascent", "engine-1", SECOND, List.of())));
    assertThrows(
        ResponseStatusException.class,
        () -> v.validate(new Batch(1, id, "warp", "engine-1", SECOND, List.of())));
    assertThrows(
        ResponseStatusException.class,
        () -> v.validate(new Batch(1, id, "ascent", "engine-1", Long.MAX_VALUE, List.of())));
    assertThrows(
        ResponseStatusException.class,
        () -> v.validate(new Batch(1, id, "ascent", "engine-1", SECOND, null)));
  }
}
