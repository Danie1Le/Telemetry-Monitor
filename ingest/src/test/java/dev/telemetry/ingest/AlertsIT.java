package dev.telemetry.ingest;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.wait.strategy.Wait;
import software.amazon.awssdk.services.dynamodb.DynamoDbClient;
import software.amazon.awssdk.services.dynamodb.model.AttributeDefinition;
import software.amazon.awssdk.services.dynamodb.model.AttributeValue;
import software.amazon.awssdk.services.dynamodb.model.KeySchemaElement;
import software.amazon.awssdk.services.dynamodb.model.KeyType;
import software.amazon.awssdk.services.dynamodb.model.ProvisionedThroughput;
import software.amazon.awssdk.services.dynamodb.model.PutItemRequest;
import software.amazon.awssdk.services.dynamodb.model.ScalarAttributeType;
import tools.jackson.databind.JsonNode;

@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
class AlertsIT {

  // Same image tag as docker-compose.yml; the table matches local/init.sh.
  static final GenericContainer<?> DYNAMODB =
      new GenericContainer<>("amazon/dynamodb-local:2.5.4")
          .withExposedPorts(8000)
          .waitingFor(Wait.forListeningPort());

  static {
    DYNAMODB.start();
  }

  @DynamicPropertySource
  static void table(DynamicPropertyRegistry registry) {
    registry.add(
        "telemetry.dynamodb-endpoint",
        () -> "http://" + DYNAMODB.getHost() + ":" + DYNAMODB.getMappedPort(8000));
  }

  static final String FLIGHT = "20261109T140300Z-7f3a";
  static final String OTHER_FLIGHT = "20261109T150000Z-aaaa";

  @LocalServerPort int port;

  @BeforeAll
  static void seed(@Autowired DynamoDbClient dynamo) {
    dynamo.createTable(b ->
        b.tableName("alerts")
            .attributeDefinitions(
                AttributeDefinition.builder()
                    .attributeName("flightId")
                    .attributeType(ScalarAttributeType.S)
                    .build(),
                AttributeDefinition.builder()
                    .attributeName("alertKey")
                    .attributeType(ScalarAttributeType.S)
                    .build())
            .keySchema(
                KeySchemaElement.builder().attributeName("flightId").keyType(KeyType.HASH).build(),
                KeySchemaElement.builder().attributeName("alertKey").keyType(KeyType.RANGE).build())
            .provisionedThroughput(
                ProvisionedThroughput.builder()
                    .readCapacityUnits(5L)
                    .writeCapacityUnits(5L)
                    .build()));
    // Put out of time order: the query must return them sorted by alertKey.
    put(
        dynamo,
        FLIGHT,
        1794233050300L,
        "engine-1.tank_pressure",
        "threshold",
        "412.7",
        1794233053100L);
    put(dynamo, FLIGHT, 1794233020000L, "engine-1.tank_pressure", "dropout", null, null);
    put(dynamo, FLIGHT, 1794233090000L, "engine-1.chamber_temp", "drift", "801.5", null);
    put(dynamo, OTHER_FLIGHT, 1794233060000L, "engine-1.tank_pressure", "threshold", "400", null);
  }

  static void put(
      DynamoDbClient dynamo,
      String flightId,
      long openedAt,
      String sensorId,
      String rule,
      String peakValue,
      Long closedAt) {
    var item = new java.util.HashMap<String, AttributeValue>();
    item.put("flightId", AttributeValue.fromS(flightId));
    item.put("alertKey", AttributeValue.fromS(openedAt + "#" + sensorId + "#" + rule));
    item.put("openedAt", AttributeValue.fromN(Long.toString(openedAt)));
    item.put("sensorId", AttributeValue.fromS(sensorId));
    item.put("rule", AttributeValue.fromS(rule));
    item.put("severity", AttributeValue.fromS("critical"));
    item.put("phase", AttributeValue.fromS("ascent"));
    item.put(
        "peakValue",
        peakValue == null ? AttributeValue.fromNul(true) : AttributeValue.fromN(peakValue));
    item.put("detail", AttributeValue.fromS("seeded " + rule));
    item.put("ttl", AttributeValue.fromN(Long.toString(openedAt / 1000 + 7 * 86400)));
    if (closedAt != null) {
      item.put("closedAt", AttributeValue.fromN(Long.toString(closedAt)));
    }
    dynamo.putItem(PutItemRequest.builder().tableName("alerts").item(item).build());
  }

  JsonNode get(String query) throws Exception {
    var response =
        HttpClient.newHttpClient()
            .send(
                HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/alerts?" + query))
                    .header(ApiKeyInterceptor.HEADER, ApiKeyTest.KEY)
                    .build(),
                HttpResponse.BodyHandlers.ofString());
    assertEquals(200, response.statusCode());
    return ContractTest.JSON.readTree(response.body());
  }

  List<String> keys(JsonNode alerts) {
    return alerts.valueStream().map(a -> a.get("alertKey").asString()).toList();
  }

  @Test
  void returnsOnlyRequestedFlightInOrderMatchingContract() throws Exception {
    var alerts = get("flightId=" + FLIGHT);
    assertEquals(
        List.of(
            "1794233020000#engine-1.tank_pressure#dropout",
            "1794233050300#engine-1.tank_pressure#threshold",
            "1794233090000#engine-1.chamber_temp#drift"),
        keys(alerts));
    var schema = ContractTest.schema("alert.schema.json");
    for (var alert : alerts) {
      assertEquals(List.of(), List.copyOf(schema.validate(alert)));
    }
    assertEquals(412.7, alerts.get(1).get("peakValue").asDouble());
    assertEquals(true, alerts.get(0).get("peakValue").isNull());
  }

  @Test
  void sinceKeepsAlertsOpenedAtOrAfter() throws Exception {
    assertEquals(
        List.of(
            "1794233050300#engine-1.tank_pressure#threshold",
            "1794233090000#engine-1.chamber_temp#drift"),
        keys(get("flightId=" + FLIGHT + "&since=1794233050300")));
    assertEquals(List.of(), keys(get("flightId=" + FLIGHT + "&since=1794233090001")));
  }

  @Test
  void unknownFlightIsEmpty() throws Exception {
    assertEquals(0, get("flightId=nope").size());
  }
}
