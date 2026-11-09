package dev.telemetry.ingest;

import java.math.BigDecimal;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;
import software.amazon.awssdk.core.exception.SdkException;
import software.amazon.awssdk.services.dynamodb.DynamoDbClient;
import software.amazon.awssdk.services.dynamodb.model.AttributeValue;
import software.amazon.awssdk.services.dynamodb.model.QueryRequest;

@RestController
class AlertsController {

  // alertKey starts with openedAt as 13-digit epoch millis, so 13 digits is the largest bound
  // whose string order is still time order.
  private static final long MAX_SINCE = 9_999_999_999_999L;

  private final DynamoDbClient dynamo;
  private final String table;

  AlertsController(DynamoDbClient dynamo, @Value("${telemetry.alerts-table}") String table) {
    this.dynamo = dynamo;
    this.table = table;
  }

  @GetMapping("/alerts")
  List<Map<String, Object>> alerts(
      @RequestParam String flightId, @RequestParam(required = false) Long since) {
    if (flightId.isBlank() || (since != null && (since < 0 || since > MAX_SINCE))) {
      throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "invalid flightId or since");
    }
    var values = new HashMap<String, AttributeValue>();
    values.put(":f", AttributeValue.fromS(flightId));
    var condition = "flightId = :f";
    if (since != null) {
      condition += " AND alertKey >= :since";
      values.put(":since", AttributeValue.fromS("%013d".formatted(since)));
    }
    var request =
        QueryRequest.builder()
            .tableName(table)
            .keyConditionExpression(condition)
            .expressionAttributeValues(values)
            .build();
    try {
      // The paginator follows LastEvaluatedKey, so a large result never silently truncates.
      return dynamo.queryPaginator(request).items().stream().map(AlertsController::plain).toList();
    } catch (SdkException e) {
      throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "alerts unavailable", e);
    }
  }

  // Alert items hold only strings, numbers and a null peakValue (dropout).
  private static Map<String, Object> plain(Map<String, AttributeValue> item) {
    var out = new HashMap<String, Object>();
    item.forEach((k, v) -> {
      if (v.s() != null) {
        out.put(k, v.s());
      } else {
        out.put(k, v.n() == null ? null : new BigDecimal(v.n()));
      }
    });
    return out;
  }
}
