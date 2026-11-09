package dev.telemetry.ingest;

import java.net.URI;
import java.time.Duration;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.awscore.client.builder.AwsClientBuilder;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.dynamodb.DynamoDbClient;
import software.amazon.awssdk.services.sqs.SqsClient;

@Configuration
class AwsConfig {

  @Bean
  SqsClient sqsClient(
      @Value("${telemetry.aws-region}") String region,
      @Value("${telemetry.sqs-endpoint:}") String endpoint) {
    return configure(SqsClient.builder(), region, endpoint).build();
  }

  @Bean
  DynamoDbClient dynamoDbClient(
      @Value("${telemetry.aws-region}") String region,
      @Value("${telemetry.dynamodb-endpoint:}") String endpoint) {
    return configure(DynamoDbClient.builder(), region, endpoint).build();
  }

  /**
   * Credentials come from the default provider chain; a custom endpoint means a local emulator
   * (ElasticMQ, DynamoDB Local), which accepts any credentials. The call timeout keeps a hung
   * service from holding request threads.
   */
  private static <B extends AwsClientBuilder<B, ?>> B configure(
      B builder, String region, String endpoint) {
    builder
        .region(Region.of(region))
        .overrideConfiguration(c -> c.apiCallTimeout(Duration.ofSeconds(5)));
    if (!endpoint.isBlank()) {
      builder
          .endpointOverride(URI.create(endpoint))
          .credentialsProvider(
              StaticCredentialsProvider.create(AwsBasicCredentials.create("local", "local")));
    }
    return builder;
  }
}
