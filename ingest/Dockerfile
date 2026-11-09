# Build from the repo root: docker build -f ingest/Dockerfile .
FROM eclipse-temurin:25-jdk AS build
WORKDIR /app
COPY ingest/gradlew ingest/settings.gradle.kts ingest/build.gradle.kts ./
COPY ingest/gradle ./gradle
RUN ./gradlew --no-daemon dependencies >/dev/null
COPY ingest/src ./src
RUN ./gradlew --no-daemon bootJar -x test -x compileTestJava && mv build/libs/*.jar app.jar

FROM eclipse-temurin:25-jre
RUN useradd --system --uid 10001 app
COPY --from=build /app/app.jar /app/app.jar
# sensors.yaml is not baked in: mount it (ConfigMap) at this path.
ENV TELEMETRY_SENSORS_CONFIG=/etc/telemetry/sensors.yaml
USER 10001
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
