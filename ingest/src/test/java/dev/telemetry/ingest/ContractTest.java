package dev.telemetry.ingest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.networknt.schema.JsonSchema;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaLocation;
import com.networknt.schema.SpecVersion;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class ContractTest {

  static final ObjectMapper JSON = new ObjectMapper();
  static final Path CONTRACTS = Path.of("../contracts").toAbsolutePath();

  // The schemas have no $id, so the file location is what resolves the relative common.schema.json
  // $ref.
  static JsonSchema schema(String name) {
    return JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012)
        .getSchema(SchemaLocation.of(CONTRACTS.resolve(name).toUri().toString()));
  }

  @Test
  void builtMessageMatchesSqsSchema() throws Exception {
    var example = JSON.readTree(CONTRACTS.resolve("http-batch.example.json").toFile());
    assertEquals(0, schema("http-batch.schema.json").validate(example).size());

    var batch = JSON.treeToValue(example, Batch.class);
    var readings = new ArrayList<>(batch.readings());
    readings.add(
        Map.of(
            "sensorId",
            "engine-1.tank_pressure",
            "ts",
            batch.second() * 1000,
            "value",
            1,
            "unit",
            "psi"));
    var result =
        BatchValidatorTest.validatorAt(batch.second() * 1000 + 500)
            .validate(
                new Batch(
                    batch.schemaVersion(),
                    batch.flightId(),
                    batch.phase(),
                    batch.engineId(),
                    batch.second(),
                    readings));
    assertEquals(4, result.accepted());
    assertEquals(1, result.rejected());

    var message = JSON.valueToTree(result.message());
    assertTrue(message.get("rejectedCounts").has("engine-1.tank_pressure"));
    assertEquals(List.of(), List.copyOf(schema("sqs-message.schema.json").validate(message)));
  }
}
