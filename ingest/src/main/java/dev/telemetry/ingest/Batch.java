package dev.telemetry.ingest;

import java.util.List;
import java.util.Map;

/** HTTP batch envelope; readings stay raw maps so one corrupt reading cannot reject the batch. */
public record Batch(
    Integer schemaVersion,
    String flightId,
    String phase,
    String engineId,
    Long second,
    List<Map<String, Object>> readings) {}
