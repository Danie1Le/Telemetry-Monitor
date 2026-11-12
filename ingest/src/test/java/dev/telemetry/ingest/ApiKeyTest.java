package dev.telemetry.ingest;

import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import software.amazon.awssdk.services.dynamodb.DynamoDbClient;
import software.amazon.awssdk.services.sqs.SqsClient;

@WebMvcTest
class ApiKeyTest {

  @Autowired MockMvc mvc;
  @MockitoBean BatchValidator validator;
  @MockitoBean SqsClient sqs;
  @MockitoBean DynamoDbClient dynamo;

  void assertRejected(MockHttpServletRequestBuilder request) throws Exception {
    mvc.perform(request).andExpect(status().isUnauthorized()).andExpect(content().string(""));
    verifyNoInteractions(validator, sqs, dynamo);
  }

  // No X-Api-Key, unlike ReadingsControllerTest.readings() and AlertsControllerTest.alerts().
  MockHttpServletRequestBuilder unkeyedReadings() {
    return post("/readings")
        .contentType(MediaType.APPLICATION_JSON)
        .content(ReadingsControllerTest.batch("1", "", "[]"));
  }

  MockHttpServletRequestBuilder unkeyedAlerts() {
    return get("/alerts").param("flightId", "f1");
  }

  @Test
  void missingKeyIs401() throws Exception {
    assertRejected(unkeyedReadings());
    assertRejected(unkeyedAlerts());
  }

  @Test
  void wrongKeyIs401() throws Exception {
    for (var wrong : new String[] {"", "test-ke", "test-key2", "TEST-KEY"}) {
      assertRejected(unkeyedReadings().header(ApiKeyInterceptor.HEADER, wrong));
      assertRejected(unkeyedAlerts().header(ApiKeyInterceptor.HEADER, wrong));
    }
  }

  @Test
  void blankKeyFailsStartup() {
    assertThrows(IllegalStateException.class, () -> new ApiKeyInterceptor(""));
    assertThrows(IllegalStateException.class, () -> new ApiKeyInterceptor(" "));
  }
}
