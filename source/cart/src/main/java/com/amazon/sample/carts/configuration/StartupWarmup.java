/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy of this
 * software and associated documentation files (the "Software"), to deal in the Software
 * without restriction, including without limitation the rights to use, copy, modify,
 * merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
 * permit persons to whom the Software is furnished to do so.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
 * INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
 * PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
 * HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
 * OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
 * SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
 */

package com.amazon.sample.carts.configuration;

import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

/**
 * Sends one cart request to this application before it reports ready.
 *
 * <p>The first request a new carts process serves is slow: it builds the DynamoDB
 * client, fetches the task's credentials, opens the first TLS connection to
 * DynamoDB, and initializes Spring's request handling. In test2 (2026-10-05) that
 * first {@code GET /carts} took 3.8-4.7 s on 0.5 vCPU, longer than the 3-second
 * Service Connect timeout on ui's calls, so the first callers of every new task
 * got 504s.
 *
 * <p>Spring Boot runs {@link ApplicationRunner}s before it marks the application
 * ready, so the readiness probe that the ECS health check calls stays down until
 * this request has finished, and Service Connect routes to a task only once its
 * health check passes. The request is best effort: if it fails or takes longer
 * than {@code carts.startup-warmup.timeout}, carts starts anyway, so a DynamoDB
 * outage can't stop it from starting.
 *
 * <p>{@code GET /carts/{customerId}} only reads with the DynamoDB service. The
 * in-memory and MongoDB services create an empty cart for an unknown customer,
 * so they keep one empty cart under {@link #CUSTOMER_ID}.
 */
@Slf4j
@Component
@ConditionalOnProperty(prefix = "carts.startup-warmup", name = "enabled", havingValue = "true", matchIfMissing = true)
public class StartupWarmup implements ApplicationRunner {

    /** A customer ID that no shopper has. */
    public static final String CUSTOMER_ID = "startup-warmup";

    private final Environment environment;
    private final Duration timeout;

    public StartupWarmup(Environment environment,
                         @Value("${carts.startup-warmup.timeout:10s}") Duration timeout) {
        this.environment = environment;
        this.timeout = timeout;
    }

    @Override
    public void run(ApplicationArguments args) {
        Integer port = environment.getProperty("local.server.port", Integer.class);
        if (port == null) {
            log.info("Startup warm-up skipped: no web server is running");
            return;
        }
        warmUp(URI.create("http://localhost:" + port + "/carts/" + CUSTOMER_ID));
    }

    void warmUp(URI uri) {
        HttpClient client = HttpClient.newBuilder().connectTimeout(timeout).build();
        HttpRequest request = HttpRequest.newBuilder(uri)
                .timeout(timeout)
                // The cart endpoints accept only JSON requests.
                .header("Content-Type", "application/json")
                .GET()
                .build();
        long started = System.nanoTime();
        try {
            HttpResponse<Void> response = client.send(request, HttpResponse.BodyHandlers.discarding());
            log.info("Startup warm-up: GET {} returned {} in {} ms",
                    uri.getPath(), response.statusCode(), elapsedMillis(started));
        } catch (IOException e) {
            log.warn("Startup warm-up: GET {} failed after {} ms, starting anyway: {}",
                    uri.getPath(), elapsedMillis(started), e.toString());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            log.warn("Startup warm-up interrupted, starting anyway");
        }
    }

    private static long elapsedMillis(long started) {
        return Duration.ofNanos(System.nanoTime() - started).toMillis();
    }
}
