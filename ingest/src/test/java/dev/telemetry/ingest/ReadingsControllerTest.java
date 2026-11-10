package dev.telemetry.ingest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.context.annotation.Primary;
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;
import software.amazon.awssdk.core.exception.SdkClientException;
import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.model.SendMessageRequest;

@WebMvcTest(ReadingsController.class)
@Import({BatchValidator.class, ReadingsControllerTest.FixedClock.class})
class ReadingsControllerTest {

  @TestConfiguration
  static class FixedClock {
    @Bean
    @Primary
    Clock fixedClock() {
      return Clock.fixed(Instant.ofEpochMilli(BatchValidatorTest.NOW), ZoneOffset.UTC);
    }
  }

  @Autowired MockMvc mvc;
  @MockitoBean SqsClient sqs;

  ResultActions send(String json) throws Exception {
    return mvc.perform(post("/readings").contentType(MediaType.APPLICATION_JSON).content(json));
  }

  static String batch(String version, String extra, String readings) {
    return """
    {"schemaVersion":%s,"flightId":"20261109T140300Z-7f3a","phase":"ascent",
     "engineId":"engine-1","second":1794233040%s,"readings":%s}\
    """
        .formatted(version, extra, readings);
  }

  @Test
  void acceptsBatchAndCountsRejected() throws Exception {
    var readings =
        """
        [{"sensorId":"engine-1.tank_pressure","ts":1794233040000,"value":341.2,"unit":"kPa"},
         {"sensorId":"engine-1.tank_pressure","ts":1794233040100,"value":339.8,"unit":"psi"},
         {"sensorId":"engine-1.tank_pressure","ts":1794233040200,"unit":"kPa"}]\
        """;
    send(batch("1", "", readings))
        .andExpect(status().isAccepted())
        .andExpect(jsonPath("$.accepted").value(1))
        .andExpect(jsonPath("$.rejected").value(2));
  }

  static final String VALID =
      """
      [{"sensorId":"engine-1.tank_pressure","ts":1794233040000,"value":341.2,"unit":"kPa"}]\
      """;
  static final String CORRUPT_ONLY =
      """
      [{"sensorId":"engine-1.tank_pressure","ts":1794233040100,"value":339.8,"unit":"psi"}]\
      """;

  String sentBody() {
    var captor = ArgumentCaptor.forClass(SendMessageRequest.class);
    verify(sqs).sendMessage(captor.capture());
    return captor.getValue().messageBody();
  }

  @Test
  void sendsOneMessageWithValidReadings() throws Exception {
    send(batch("1", "", VALID)).andExpect(status().isAccepted());
    var body = sentBody();
    assertEquals(
        ContractTest.JSON.readTree(
            """
            {"schemaVersion":1,"flightId":"20261109T140300Z-7f3a","phase":"ascent",
             "engineId":"engine-1","second":1794233040,"receivedAt":%d,
             "readings":{"engine-1.tank_pressure":[{"ts":1794233040000,"value":341.2}]},
             "rejectedCounts":{}}\
            """
                .formatted(BatchValidatorTest.NOW)),
        ContractTest.JSON.readTree(body));
  }

  @Test
  void corruptOnlyBatchStillSendsSoSensorIsPresent() throws Exception {
    send(batch("1", "", CORRUPT_ONLY)).andExpect(status().isAccepted());
    var body = sentBody();
    var message = ContractTest.JSON.readTree(body);
    assertEquals(0, message.get("readings").size());
    assertEquals(1, message.get("rejectedCounts").get("engine-1.tank_pressure").asInt());
  }

  @Test
  void emptyBatchStillSendsOneMessageSoItsSensorsCountAsMissing() throws Exception {
    send(batch("1", "", "[]")).andExpect(status().isAccepted());
    var message = ContractTest.JSON.readTree(sentBody());
    assertEquals(1794233040L, message.get("second").asLong());
    assertEquals(0, ContractTest.schema("sqs-message.schema.json").validate(message).size());
  }

  @Test
  void yamlBatchIs415() throws Exception {
    mvc.perform(
            post("/readings")
                .contentType(MediaType.APPLICATION_YAML)
                .content("schemaVersion: \"1\"\nreadings: []\n"))
        .andExpect(status().isUnsupportedMediaType());
    verifyNoInteractions(sqs);
  }

  @Test
  void invalidBatchSendsNothing() throws Exception {
    send(batch("2", "", VALID)).andExpect(status().isBadRequest());
    verifyNoInteractions(sqs);
  }

  @Test
  void sendFailureIs503() throws Exception {
    when(sqs.sendMessage(any(SendMessageRequest.class)))
        .thenThrow(SdkClientException.create("down"));
    send(batch("1", "", VALID)).andExpect(status().isServiceUnavailable());
  }

  @Test
  void missingEnvelopeFieldsIs400() throws Exception {
    send("{\"schemaVersion\":1,\"readings\":[]}").andExpect(status().isBadRequest());
  }

  @Test
  void unknownSchemaVersionIs400() throws Exception {
    send(batch("2", "", "[]")).andExpect(status().isBadRequest());
  }

  @Test
  void unknownTopLevelFieldIs400() throws Exception {
    send(batch("1", ",\"extra\":1", "[]")).andExpect(status().isBadRequest());
  }

  @Test
  void coercedEnvelopeScalarsAre400() throws Exception {
    send(batch("\"1\"", "", "[]")).andExpect(status().isBadRequest());
    send(batch("1.0", "", "[]")).andExpect(status().isBadRequest());
    send(batch("1", "", "[]").replace("1794233040", "\"1794233040\""))
        .andExpect(status().isBadRequest());
  }

  @Test
  void nonObjectReadingItemIs400() throws Exception {
    send(batch("1", "", "[5]")).andExpect(status().isBadRequest());
  }
}
