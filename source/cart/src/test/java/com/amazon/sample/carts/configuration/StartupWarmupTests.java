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

import com.amazon.sample.carts.controllers.CartsController;
import com.amazon.sample.carts.services.CartService;
import com.amazon.sample.carts.services.InMemoryCartService;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.availability.AvailabilityChangeEvent;
import org.springframework.boot.availability.ReadinessState;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.ApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Import;
import org.springframework.context.annotation.Primary;
import org.springframework.context.event.EventListener;
import org.springframework.mock.env.MockEnvironment;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.URI;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

import static org.assertj.core.api.Assertions.assertThat;

class StartupWarmupTests {

    /**
     * The cart API with the in-memory service. Imports its beans rather than
     * scanning the package, because a scan would also pick up other tests'
     * configurations (DynamoDBCartServiceTests.TestConfiguration needs DynamoDB).
     * A plain @Configuration, not a @TestConfiguration: given only test
     * configurations, @SpringBootTest also loads CartApplication, which scans.
     */
    @Configuration(proxyBeanMethods = false)
    @EnableAutoConfiguration
    @Import({CartsController.class, InMemoryConfiguration.class, StartupWarmup.class})
    static class TestApplication {
    }

    /** What happened during one application context's startup, in order. */
    static class Timeline {
        final List<String> events = new CopyOnWriteArrayList<>();

        @EventListener
        void readiness(AvailabilityChangeEvent<ReadinessState> event) {
            events.add("readiness " + event.getState());
        }
    }

    @TestConfiguration
    static class RecordingConfiguration {

        @Bean
        Timeline timeline() {
            return new Timeline();
        }

        /** The in-memory cart service, recording when the warm-up's customer is read. */
        @Bean
        @Primary
        CartService recordingCartService(Timeline timeline) {
            CartService delegate = new InMemoryCartService();
            return (CartService) Proxy.newProxyInstance(
                    CartService.class.getClassLoader(), new Class<?>[]{CartService.class},
                    (proxy, method, args) -> {
                        if (method.getName().equals("get") && StartupWarmup.CUSTOMER_ID.equals(args[0])) {
                            timeline.events.add("cart read for the warm-up customer");
                        }
                        try {
                            return method.invoke(delegate, args);
                        } catch (InvocationTargetException e) {
                            throw e.getCause();
                        }
                    });
        }
    }

    @Nested
    @SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
            classes = {TestApplication.class, RecordingConfiguration.class})
    class WhenEnabled {

        @Autowired
        private Timeline timeline;

        @Test
        void requestsACartThroughTheApiBeforeTheApplicationReportsReady() {
            // The request went through the HTTP endpoint to the cart service
            // and finished before Spring marked the application ready.
            assertThat(timeline.events).containsExactly(
                    "cart read for the warm-up customer",
                    "readiness " + ReadinessState.ACCEPTING_TRAFFIC);
        }
    }

    @Nested
    @SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
            classes = TestApplication.class,
            properties = "carts.startup-warmup.enabled=false")
    class WhenDisabled {

        @Autowired
        private ApplicationContext context;

        @Test
        void doesNotRun() {
            assertThat(context.getBeansOfType(StartupWarmup.class)).isEmpty();
        }
    }

    @Test
    void givesUpAtTheTimeoutWhenTheServerNeverAnswers() throws Exception {
        // The kernel accepts the connection into the backlog, but nothing ever
        // answers, like a request stuck behind an unreachable DynamoDB.
        InetAddress loopback = InetAddress.getByName("127.0.0.1");
        try (ServerSocket silent = new ServerSocket(0, 1, loopback)) {
            StartupWarmup warmup = new StartupWarmup(new MockEnvironment(), Duration.ofMillis(300));
            long started = System.nanoTime();
            warmup.warmUp(URI.create("http://127.0.0.1:" + silent.getLocalPort() + "/carts/" + StartupWarmup.CUSTOMER_ID));
            Duration took = Duration.ofNanos(System.nanoTime() - started);
            assertThat(took).isBetween(Duration.ofMillis(250), Duration.ofSeconds(3));
        }
    }

    @Test
    void carriesOnWhenNothingListens() throws Exception {
        int port;
        try (ServerSocket probe = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))) {
            port = probe.getLocalPort();
        }
        StartupWarmup warmup = new StartupWarmup(new MockEnvironment(), Duration.ofSeconds(2));
        warmup.warmUp(URI.create("http://127.0.0.1:" + port + "/carts/" + StartupWarmup.CUSTOMER_ID));
    }

    @Test
    void skipsWithoutAWebServer() {
        new StartupWarmup(new MockEnvironment(), Duration.ofSeconds(2)).run(null);
    }
}
