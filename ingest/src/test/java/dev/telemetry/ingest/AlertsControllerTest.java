package dev.telemetry.ingest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import software.amazon.awssdk.core.exception.SdkClientException;
import software.amazon.awssdk.services.dynamodb.DynamoDbClient;
import software.amazon.awssdk.services.dynamodb.model.AttributeValue;
import software.amazon.awssdk.services.dynamodb.model.QueryRequest;
import software.amazon.awssdk.services.dynamodb.model.QueryResponse;
import software.amazon.awssdk.services.dynamodb.paginators.QueryIterable;

@WebMvcTest(AlertsController.class)
class AlertsControllerTest {

  @Autowired MockMvc mvc;
  @MockitoBean DynamoDbClient dynamo;

  // The real paginator runs on top of the mocked client, so query() is the seam.
  @BeforeEach
  void stubClient() {
    when(dynamo.queryPaginator(any(QueryRequest.class)))
        .thenAnswer(i -> new QueryIterable(dynamo, i.<QueryRequest>getArgument(0)));
    when(dynamo.query(any(QueryRequest.class))).thenReturn(QueryResponse.builder().build());
  }

  // With the key: ApiKeyTest covers requests without it.
  static MockHttpServletRequestBuilder alerts() {
    return get("/alerts").header(ApiKeyInterceptor.HEADER, ApiKeyTest.KEY);
  }

  QueryRequest queried() {
    var captor = ArgumentCaptor.forClass(QueryRequest.class);
    verify(dynamo).query(captor.capture());
    return captor.getValue();
  }

  @Test
  void missingFlightIdIs400() throws Exception {
    mvc.perform(alerts()).andExpect(status().isBadRequest());
    mvc.perform(alerts().param("flightId", " ")).andExpect(status().isBadRequest());
    verifyNoInteractions(dynamo);
  }

  @Test
  void badSinceIs400() throws Exception {
    for (var since : new String[] {"abc", "-1", "10000000000000"}) {
      mvc.perform(alerts().param("flightId", "f1").param("since", since))
          .andExpect(status().isBadRequest());
    }
    verifyNoInteractions(dynamo);
  }

  @Test
  void queriesByFlightOnly() throws Exception {
    mvc.perform(alerts().param("flightId", "f1"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$").isEmpty());
    var request = queried();
    assertEquals("alerts", request.tableName());
    assertEquals("flightId = :f", request.keyConditionExpression());
    assertEquals(1, request.expressionAttributeValues().size());
    assertEquals("f1", request.expressionAttributeValues().get(":f").s());
  }

  @Test
  void sincePadsToThirteenDigits() throws Exception {
    mvc.perform(alerts().param("flightId", "f1").param("since", "12345"))
        .andExpect(status().isOk());
    var request = queried();
    assertEquals("flightId = :f AND alertKey >= :since", request.keyConditionExpression());
    assertEquals("0000000012345", request.expressionAttributeValues().get(":since").s());
  }

  @Test
  void followsLastEvaluatedKeyAcrossPages() throws Exception {
    Map<String, AttributeValue> first = Map.of("alertKey", AttributeValue.fromS("0000000000001"));
    Map<String, AttributeValue> second = Map.of("alertKey", AttributeValue.fromS("0000000000002"));
    when(dynamo.query(any(QueryRequest.class)))
        .thenReturn(
            QueryResponse.builder().items(first).lastEvaluatedKey(first).build(),
            QueryResponse.builder().items(second).build());
    mvc.perform(alerts().param("flightId", "f1"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.length()").value(2))
        .andExpect(jsonPath("$[0].alertKey").value("0000000000001"))
        .andExpect(jsonPath("$[1].alertKey").value("0000000000002"));
  }

  @Test
  void dynamoFailureIs503() throws Exception {
    when(dynamo.query(any(QueryRequest.class))).thenThrow(SdkClientException.create("down"));
    mvc.perform(alerts().param("flightId", "f1")).andExpect(status().isServiceUnavailable());
  }
}
