package dev.telemetry.ingest;

import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;
import software.amazon.awssdk.core.exception.SdkException;
import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.model.SendMessageRequest;
import tools.jackson.databind.json.JsonMapper;

@RestController
class ReadingsController {

  private final BatchValidator validator;
  private final SqsClient sqs;
  private final JsonMapper json;
  private final String queueUrl;

  ReadingsController(
      BatchValidator validator,
      SqsClient sqs,
      JsonMapper json,
      @Value("${telemetry.queue-url}") String queueUrl) {
    this.validator = validator;
    this.sqs = sqs;
    this.json = json;
    this.queueUrl = queueUrl;
  }

  @PostMapping("/readings")
  @ResponseStatus(HttpStatus.ACCEPTED)
  Map<String, Integer> readings(@RequestBody Batch batch) {
    var result = validator.validate(batch);
    // A batch of only corrupt readings still goes out: those sensors are present, not dropouts.
    if (!batch.readings().isEmpty()) {
      try {
        var body = json.writeValueAsString(result.message());
        sqs.sendMessage(SendMessageRequest.builder().queueUrl(queueUrl).messageBody(body).build());
      } catch (SdkException e) {
        // Never 202 data that was lost; the simulator logs the failure.
        throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "queue unavailable", e);
      }
    }
    return Map.of("accepted", result.accepted(), "rejected", result.rejected());
  }
}
