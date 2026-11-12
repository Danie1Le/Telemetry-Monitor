package dev.telemetry.ingest;

import static java.nio.charset.StandardCharsets.UTF_8;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.security.MessageDigest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.servlet.HandlerInterceptor;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * Every request outside Actuator must carry the shared key in X-Api-Key. It covers all paths, so a
 * new endpoint is protected without being listed here, and an interceptor matches paths the same
 * way the controllers do.
 *
 * <p>Actuator's own handler mapping never runs this interceptor: every exposed Actuator endpoint is
 * public. application.properties therefore exposes only health (with its liveness and readiness
 * groups, for the kubelet probes) and turns off the /actuator discovery page. An unexposed Actuator
 * path falls through to the intercepted mappings and gets a 401.
 */
@Component
class ApiKeyInterceptor implements HandlerInterceptor, WebMvcConfigurer {

  static final String HEADER = "X-Api-Key";

  private final byte[] key;

  ApiKeyInterceptor(@Value("${telemetry.api-key}") String key) {
    // A blank key would let everyone in: refuse to start instead.
    if (key.isBlank()) {
      throw new IllegalStateException("telemetry.api-key (env INGEST_API_KEY) is not set");
    }
    this.key = key.getBytes(UTF_8);
  }

  @Override
  public void addInterceptors(InterceptorRegistry registry) {
    registry.addInterceptor(this);
  }

  @Override
  public boolean preHandle(
      HttpServletRequest request, HttpServletResponse response, Object handler) {
    var sent = request.getHeader(HEADER);
    // isEqual takes the same time wherever the bytes differ.
    if (sent != null && MessageDigest.isEqual(key, sent.getBytes(UTF_8))) {
      return true;
    }
    // setStatus, not sendError: no error page, so the reply says nothing beyond 401.
    response.setStatus(HttpStatus.UNAUTHORIZED.value());
    return false;
  }
}
