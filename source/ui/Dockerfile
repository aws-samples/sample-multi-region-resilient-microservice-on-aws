FROM public.ecr.aws/docker/library/amazoncorretto:17-al2023 as build-env

ARG JAR_PATH

VOLUME /tmp
WORKDIR /

COPY .mvn .mvn
COPY mvnw .
COPY pom.xml .

RUN ./mvnw dependency:go-offline -B -q

COPY ./src ./src

RUN ./mvnw -DskipTests package -q && mv ./target/ui-0.0.1-SNAPSHOT.jar /app.jar

# Package Stage
FROM public.ecr.aws/docker/library/amazoncorretto:17-al2023

RUN dnf install -q -y \
    shadow-utils wget && \
    dnf clean all

ENV APPUSER=appuser
ENV APPUID=1000
ENV APPGID=1000

RUN useradd \
    --home "/app" \
    --create-home \
    --user-group \
    --uid "$APPUID" \
    "$APPUSER"

# The ADOT Java agent is not part of this image. The task definition in deployment/ecs.yaml copies
# it from the adot-autoinstrumentation-java sidecar into a shared volume and sets JAVA_TOOL_OPTIONS.

WORKDIR /app
USER appuser

COPY ./ATTRIBUTION.md ./LICENSES.md
COPY --chown=appuser:appuser --from=build-env /app.jar .

EXPOSE 8080
HEALTHCHECK CMD curl --fail http://localhost:8080 || exit 1
ARG JAVA_OPTS

ENTRYPOINT ["sh", "-c", "java $JAVA_OPTS -jar /app/app.jar"]