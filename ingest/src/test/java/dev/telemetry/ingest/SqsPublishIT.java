package dev.telemetry.ingest;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.wait.strategy.Wait;
import org.testcontainers.utility.MountableFile;
import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.model.ReceiveMessageRequest;

@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
class SqsPublishIT {

  // Same image tag as docker-compose.yml; the queue config is the one used locally.
  static final GenericContainer<?> ELASTICMQ =
      new GenericContainer<>("softwaremill/elasticmq-native:1.6.11")
          .withCopyFileToContainer(
              MountableFile.forHostPath("../local/elasticmq.conf"), "/opt/elasticmq.conf")
          .withExposedPorts(9324)
          .waitingFor(Wait.forListeningPort());

  static {
    ELASTICMQ.start();
  }

  @DynamicPropertySource
  static void queue(DynamicPropertyRegistry registry) {
    var endpoint = "http://" + ELASTICMQ.getHost() + ":" + ELASTICMQ.getMappedPort(9324);
    registry.add("telemetry.sqs-endpoint", () -> endpoint);
    registry.add("telemetry.queue-url", () -> endpoint + "/000000000000/telemetry-readings");
  }

  @LocalServerPort int port;
  @Autowired SqsClient sqs;

  @Value("${telemetry.queue-url}")
  String queueUrl;

  @Test
  void onePostPublishesOneMessageMatchingContract() throws Exception {
    long second = System.currentTimeMillis() / 1000;
    var body =
        """
        {"schemaVersion":1,"flightId":"20261109T140300Z-7f3a","phase":"ascent",
         "engineId":"engine-1","second":%d,"readings":[
          {"sensorId":"engine-1.tank_pressure","ts":%d,"value":341.2,"unit":"kPa"},
          {"sensorId":"engine-1.tank_pressure","ts":%d,"value":339.8,"unit":"psi"}]}\
        """
            .formatted(second, second * 1000 + 100, second * 1000 + 200);
    var response =
        HttpClient.newHttpClient()
            .send(
                HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/readings"))
                    .header("Content-Type", "application/json")
                    .header(ApiKeyInterceptor.HEADER, TestApiKey.KEY)
                    .POST(HttpRequest.BodyPublishers.ofString(body))
                    .build(),
                HttpResponse.BodyHandlers.ofString());
    assertEquals(202, response.statusCode());

    List<String> bodies =
        sqs
            .receiveMessage(
                ReceiveMessageRequest.builder()
                    .queueUrl(queueUrl)
                    .maxNumberOfMessages(10)
                    .waitTimeSeconds(5)
                    .build())
            .messages()
            .stream()
            .map(m -> m.body())
            .toList();
    assertEquals(1, bodies.size());
    var message = ContractTest.JSON.readTree(bodies.get(0));
    assertEquals(
        List.of(), List.copyOf(ContractTest.schema("sqs-message.schema.json").validate(message)));
    assertEquals(1, message.get("readings").get("engine-1.tank_pressure").size());
    assertEquals(1, message.get("rejectedCounts").get("engine-1.tank_pressure").asInt());
  }
}
