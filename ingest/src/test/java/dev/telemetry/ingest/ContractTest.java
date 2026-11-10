package dev.telemetry.ingest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.networknt.schema.InputFormat;
import com.networknt.schema.Schema;
import com.networknt.schema.SchemaException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import tools.jackson.databind.ObjectMapper;

class ContractTest {

  static final ObjectMapper JSON = new ObjectMapper();
  static final Path CONTRACTS = Path.of("../contracts").toAbsolutePath();

  // The schemas have no $id, so the file location is what resolves the relative common.schema.json
  // $ref.
  static Schema schema(String name) {
    return BatchValidator.contractSchema(CONTRACTS.resolve(name));
  }

  @Test
  void httpRefIsRefusedNotFetched(@TempDir Path dir) throws Exception {
    var file = dir.resolve("remote-ref.schema.json");
    Files.writeString(
        file,
        """
        {"$schema": "https://json-schema.org/draft/2020-12/schema",
         "$ref": "https://example.com/evil.schema.json"}
        """);
    var e =
        assertThrows(
            SchemaException.class,
            () -> BatchValidator.contractSchema(file).validate("{}", InputFormat.JSON));
    assertTrue(e.getMessage().contains("is not allowed to be loaded"), e.getMessage());
  }

  @Test
  void sqsExampleMatchesSqsSchema() throws Exception {
    var example = JSON.readTree(CONTRACTS.resolve("sqs-message.example.json").toFile());
    assertEquals(List.of(), List.copyOf(schema("sqs-message.schema.json").validate(example)));
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
