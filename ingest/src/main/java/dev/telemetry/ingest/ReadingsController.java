package dev.telemetry.ingest;

import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
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

  // JSON only: the YAML converter on the classpath skips the strict spring.jackson.* binding.
  @PostMapping(value = "/readings", consumes = MediaType.APPLICATION_JSON_VALUE)
  @ResponseStatus(HttpStatus.ACCEPTED)
  Map<String, Integer> readings(@RequestBody Batch batch) {
    var result = validator.validate(batch);
    // Every batch goes out, even with no valid reading: the processor's rules then see that
    // second, so a corrupt-only sensor is present and an absent one counts toward its dropout.
    try {
      var body = json.writeValueAsString(result.message());
      sqs.sendMessage(SendMessageRequest.builder().queueUrl(queueUrl).messageBody(body).build());
    } catch (SdkException e) {
      // Never 202 data that was lost; the simulator logs the failure.
      throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "queue unavailable", e);
    }
    return Map.of("accepted", result.accepted(), "rejected", result.rejected());
  }
}
