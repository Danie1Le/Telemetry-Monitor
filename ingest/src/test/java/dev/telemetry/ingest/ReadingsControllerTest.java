package dev.telemetry.ingest;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.context.annotation.Primary;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;

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
