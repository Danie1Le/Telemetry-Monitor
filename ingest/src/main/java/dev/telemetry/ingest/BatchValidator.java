package dev.telemetry.ingest;

import com.networknt.schema.InputFormat;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaLocation;
import com.networknt.schema.SpecVersion;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Clock;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.server.ResponseStatusException;
import org.yaml.snakeyaml.Yaml;

/** Checks a batch's envelope, drops corrupt readings and builds the SQS message body. */
@Component
public class BatchValidator {

  /** Outcome of one batch: HTTP counts plus the SQS message body (see contracts/). */
  public record Result(int accepted, int rejected, Map<String, Object> message) {}

  private static final Pattern FLIGHT_ID = Pattern.compile("^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{4,}$");
  private static final Pattern ENGINE_ID = Pattern.compile("^engine-[1-9][0-9]*$");
  private static final Pattern SENSOR_ID = Pattern.compile("^engine-[1-9][0-9]*\\.[a-z_]+$");

  private final Clock clock;
  private final Set<String> phases;
  private final Map<String, String> units = new HashMap<>();
  private final long staleMillis;
  private final long futureMillis;

  BatchValidator(
      @Value("${telemetry.sensors-config}") String sensorsConfig,
      @Value("${telemetry.contracts-dir}") String contractsDir,
      Clock clock)
      throws IOException {
    this.clock = clock;
    var yaml = Files.readString(Path.of(sensorsConfig));
    // The schema has no $id, so its file location resolves the relative common.schema.json $ref.
    var errors =
        JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012)
            .getSchema(
                SchemaLocation.of(
                    Path.of(contractsDir, "sensors.schema.json")
                        .toAbsolutePath()
                        .toUri()
                        .toString()))
            .validate(yaml, InputFormat.YAML);
    if (!errors.isEmpty()) {
      throw new IllegalStateException("invalid sensors config " + sensorsConfig + ": " + errors);
    }
    Map<String, Map<String, Object>> config = new Yaml().load(yaml);
    @SuppressWarnings("unchecked")
    var types = (Map<String, Map<String, Object>>) (Object) config.get("sensorTypes");
    types.forEach((type, def) -> units.put(type, (String) def.get("unit")));
    @SuppressWarnings("unchecked")
    var phaseList = (List<Map<String, Object>>) (Object) config.get("phases");
    phases = phaseList.stream().map(p -> (String) p.get("name")).collect(Collectors.toSet());
    @SuppressWarnings("unchecked")
    var skew = (Map<String, Integer>) (Object) config.get("skew");
    staleMillis = skew.get("staleSeconds") * 1000L;
    futureMillis = skew.get("futureSeconds") * 1000L;
  }

  public Result validate(Batch b) {
    if (!envelopeValid(b)) {
      throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "invalid batch envelope");
    }
    long receivedAt = clock.millis();
    Map<String, List<Map<String, Object>>> valid = new LinkedHashMap<>();
    Map<String, Integer> rejectedCounts = new LinkedHashMap<>();
    int accepted = 0;
    int rejected = 0;
    for (var r : b.readings()) {
      if (isValid(b, r, receivedAt)) {
        accepted++;
        valid
            .computeIfAbsent((String) r.get("sensorId"), k -> new ArrayList<>())
            .add(Map.of("ts", r.get("ts"), "value", r.get("value")));
      } else {
        rejected++;
        // Only this engine's sensors count, so another engine's id cannot mask its dropout.
        if (r.get("sensorId") instanceof String id
            && SENSOR_ID.matcher(id).matches()
            && id.startsWith(b.engineId() + ".")) {
          rejectedCounts.merge(id, 1, Integer::sum);
        }
      }
    }
    Map<String, Object> message = new LinkedHashMap<>();
    message.put("schemaVersion", b.schemaVersion());
    message.put("flightId", b.flightId());
    message.put("phase", b.phase());
    message.put("engineId", b.engineId());
    message.put("second", b.second());
    message.put("receivedAt", receivedAt);
    message.put("readings", valid);
    message.put("rejectedCounts", rejectedCounts);
    return new Result(accepted, rejected, message);
  }

  private boolean envelopeValid(Batch b) {
    return b != null
        && Integer.valueOf(1).equals(b.schemaVersion())
        && b.flightId() != null
        && FLIGHT_ID.matcher(b.flightId()).matches()
        && phases.contains(b.phase())
        && b.engineId() != null
        && ENGINE_ID.matcher(b.engineId()).matches()
        && b.second() != null
        && b.second() >= 0
        && b.second() < Long.MAX_VALUE / 1000
        && b.readings() != null
        && b.readings().stream().noneMatch(Objects::isNull);
  }

  private boolean isValid(Batch b, Map<String, Object> r, long now) {
    if (!(r.get("sensorId") instanceof String id
        && (r.get("ts") instanceof Integer || r.get("ts") instanceof Long)
        && r.get("value") instanceof Number n
        && Double.isFinite(n.doubleValue())
        && r.get("unit") instanceof String unit)) {
      return false;
    }
    long ts = ((Number) r.get("ts")).longValue();
    String prefix = b.engineId() + ".";
    return id.startsWith(prefix)
        && unit.equals(units.get(id.substring(prefix.length())))
        && ts >= b.second() * 1000
        && ts <= b.second() * 1000 + 999
        && ts >= now - staleMillis
        && ts <= now + futureMillis;
  }
}
