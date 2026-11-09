package dev.telemetry.ingest;

import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

@RestController
class ReadingsController {

  private final BatchValidator validator;

  ReadingsController(BatchValidator validator) {
    this.validator = validator;
  }

  @PostMapping("/readings")
  @ResponseStatus(HttpStatus.ACCEPTED)
  Map<String, Integer> readings(@RequestBody Batch batch) {
    var result = validator.validate(batch);
    return Map.of("accepted", result.accepted(), "rejected", result.rejected());
  }
}
