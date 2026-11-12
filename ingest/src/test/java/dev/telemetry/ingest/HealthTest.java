package dev.telemetry.ingest;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.test.web.servlet.MockMvc;

// Spring Boot turns the liveness and readiness groups on by itself only on Kubernetes.
@SpringBootTest(properties = "management.endpoint.health.probes.enabled=true")
@AutoConfigureMockMvc
class HealthTest {

  @Autowired MockMvc mvc;

  // No X-Api-Key: the kubelet probes and the scripts' readiness check send none.
  @Test
  void healthIsUpWithoutApiKey() throws Exception {
    for (var path :
        new String[] {
          "/actuator/health", "/actuator/health/liveness", "/actuator/health/readiness"
        }) {
      mvc.perform(get(path)).andExpect(status().isOk()).andExpect(jsonPath("$.status").value("UP"));
    }
  }

  // Actuator's handler mapping skips ApiKeyInterceptor, so only the exposure list keeps the rest
  // closed: the discovery page and unexposed endpoints fall through to the intercepted mappings.
  @Test
  void restOfActuatorNeedsApiKey() throws Exception {
    for (var path :
        new String[] {"/actuator", "/actuator/env", "/actuator/info", "/actuator/beans"}) {
      mvc.perform(get(path)).andExpect(status().isUnauthorized());
    }
  }
}
