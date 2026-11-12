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

  // Set for every test in src/test/resources/config/application.properties.
  static final String KEY = "test-key";

  @Autowired MockMvc mvc;
  @MockitoBean BatchValidator validator;
  @MockitoBean SqsClient sqs;
  @MockitoBean DynamoDbClient dynamo;

  void assertRejected(MockHttpServletRequestBuilder request) throws Exception {
    mvc.perform(request).andExpect(status().isUnauthorized()).andExpect(content().string(""));
    verifyNoInteractions(validator, sqs, dynamo);
  }

  MockHttpServletRequestBuilder readings() {
    return post("/readings")
        .contentType(MediaType.APPLICATION_JSON)
        .content(ReadingsControllerTest.batch("1", "", "[]"));
  }

  MockHttpServletRequestBuilder alerts() {
    return get("/alerts").param("flightId", "f1");
  }

  @Test
  void missingKeyIs401() throws Exception {
    assertRejected(readings());
    assertRejected(alerts());
  }

  @Test
  void wrongKeyIs401() throws Exception {
    for (var wrong : new String[] {"", "test-ke", "test-key2", "TEST-KEY"}) {
      assertRejected(readings().header(ApiKeyInterceptor.HEADER, wrong));
      assertRejected(alerts().header(ApiKeyInterceptor.HEADER, wrong));
    }
  }

  @Test
  void blankKeyFailsStartup() {
    assertThrows(IllegalStateException.class, () -> new ApiKeyInterceptor(""));
    assertThrows(IllegalStateException.class, () -> new ApiKeyInterceptor(" "));
  }
}
