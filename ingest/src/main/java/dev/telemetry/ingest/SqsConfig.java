package dev.telemetry.ingest;

import java.net.URI;
import java.time.Duration;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.sqs.SqsClient;

@Configuration
class SqsConfig {

  /**
   * Credentials come from the default provider chain; a custom endpoint means ElasticMQ, which
   * accepts any credentials. The call timeout keeps a hung queue from holding request threads.
   */
  @Bean
  SqsClient sqsClient(
      @Value("${telemetry.aws-region}") String region,
      @Value("${telemetry.sqs-endpoint:}") String endpoint) {
    var builder =
        SqsClient.builder()
            .region(Region.of(region))
            .overrideConfiguration(c -> c.apiCallTimeout(Duration.ofSeconds(5)));
    if (!endpoint.isBlank()) {
      builder
          .endpointOverride(URI.create(endpoint))
          .credentialsProvider(
              StaticCredentialsProvider.create(AwsBasicCredentials.create("local", "local")));
    }
    return builder.build();
  }
}
