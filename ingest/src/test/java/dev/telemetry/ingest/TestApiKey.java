package dev.telemetry.ingest;

/** The ingest API key in every test. */
final class TestApiKey {

  // Must match telemetry.api-key in src/test/resources/config/application.properties.
  static final String KEY = "test-key";

  private TestApiKey() {}
}
