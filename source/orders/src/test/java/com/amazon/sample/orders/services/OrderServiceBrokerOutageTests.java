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

package com.amazon.sample.orders.services;

import com.amazon.sample.events.orders.OrderCreatedEvent;
import com.amazon.sample.orders.entities.OrderEntity;
import com.amazon.sample.orders.entities.OrderItemEntity;
import com.amazon.sample.orders.messaging.MessagingProvider;
import com.amazon.sample.orders.messaging.OrdersEventHandler;
import com.amazon.sample.orders.metrics.OrdersMetrics;
import com.amazon.sample.orders.repositories.OrderRepository;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.springframework.amqp.AmqpIOException;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.orm.jpa.DataJpaTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.test.annotation.DirtiesContext;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

import java.net.SocketTimeoutException;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.awaitility.Awaitility.await;
import static org.junit.jupiter.api.Assertions.assertTimeoutPreemptively;

/**
 * A broker outage can't fail or slow an order. These tests create orders through the real
 * OrderService, with real transactions and the real OrdersEventHandler and OrdersMetrics, and
 * a broker that throws or never answers.
 *
 * What the orders-broker-dependency test saw in test2 before the publish moved off the
 * request path: the order was saved, and then the request waited on the broker.
 */
@DataJpaTest
@Import({OrderService.class, OrdersEventHandler.class, OrdersMetrics.class, OrderServiceBrokerOutageTests.Broker.class})
@Transactional(propagation = Propagation.NOT_SUPPORTED)   // the order must commit: the event is sent after the commit
@DirtiesContext(classMode = DirtiesContext.ClassMode.AFTER_EACH_TEST_METHOD)   // each test gets its own publish pool, broker and meters
@Timeout(60)   // a publish that blocks the request thread must fail these tests, not hang the build
class OrderServiceBrokerOutageTests {

    private static final Duration PROMPTLY = Duration.ofSeconds(3);   // Service Connect gives orders 3 seconds

    @TestConfiguration
    static class Broker {
        @Bean
        FakeBroker messagingProvider() {
            return new FakeBroker();
        }

        @Bean
        MeterRegistry meterRegistry() {
            return new SimpleMeterRegistry();
        }
    }

    static final class FakeBroker implements MessagingProvider {
        final List<String> attempted = new CopyOnWriteArrayList<>();   // order ids, as each publish starts
        final List<String> published = new CopyOnWriteArrayList<>();   // order ids, as each publish succeeds
        final CountDownLatch release = new CountDownLatch(1);
        volatile boolean unreachable;     // the publish never returns until released, as a connect to a dropped host doesn't
        volatile RuntimeException failure;

        @Override
        public void publishEvent(Object event) {
            attempted.add(((OrderCreatedEvent) event).getOrder().getId());
            if (unreachable) {
                try {
                    release.await(5, TimeUnit.SECONDS);   // never forever: see the @Timeout above
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
            if (failure != null) {
                throw failure;
            }
            published.add(((OrderCreatedEvent) event).getOrder().getId());
        }
    }

    @Autowired
    private OrderService service;
    @Autowired
    private OrderRepository repository;
    @Autowired
    private FakeBroker broker;
    @Autowired
    private MeterRegistry meters;

    @AfterEach
    void letGoOfTheBroker() {
        broker.release.countDown();   // so the pool can drain before its context is closed
    }

    @Test
    void anOrderIsSavedAndReturnedWhileTheBrokerThrows() {
        broker.failure = new AmqpIOException(new SocketTimeoutException("Connect timed out"));

        OrderEntity saved = assertTimeoutPreemptively(PROMPTLY, () -> service.create(newOrder()));

        assertThat(repository.findById(saved.getId())).as("the order was committed").isPresent();
        await().atMost(Duration.ofSeconds(10)).untilAsserted(() -> assertThat(broker.attempted).containsExactly(saved.getId()));
        assertThat(broker.published).as("and the publish that threw published nothing").isEmpty();
    }

    @Test
    void anOrderIsSavedAndReturnedWhileTheBrokerNeverAnswers() {
        broker.unreachable = true;

        OrderEntity saved = assertTimeoutPreemptively(PROMPTLY, () -> service.create(newOrder()));

        assertThat(repository.findById(saved.getId())).as("the order was committed").isPresent();
        await().atMost(Duration.ofSeconds(10)).untilAsserted(() -> assertThat(broker.attempted).containsExactly(saved.getId()));
        assertThat(broker.published).as("while its publish is still stuck on the broker").isEmpty();
    }

    @Test
    void manyOrdersAreSavedWhileTheBrokerNeverAnswersAndThenOnlyTheEventsAreLost() {
        broker.unreachable = true;
        int orders = 150;      // more than two pool threads and a queue of 100 can hold

        assertTimeoutPreemptively(Duration.ofSeconds(30), () -> {
            for (int i = 0; i < orders; i++) {
                service.create(newOrder());
            }
        });

        assertThat(repository.count()).as("every order was saved").isEqualTo(orders);
        assertThat(meters.get("watch.orders").tag("productId", "*").counter().count())
            .as("and every one was counted").isEqualTo(orders);
    }

    @Test
    void theOrdersMetricIsCountedWhileTheBrokerThrows() {
        broker.failure = new IllegalStateException("broker is down");

        service.create(newOrder());
        service.create(newOrder());

        assertThat(meters.get("watch.orders").tag("productId", "*").counter().count()).isEqualTo(2);
    }

    @Test
    void theEventReachesTheBrokerWhenItIsUp() {
        OrderEntity saved = service.create(newOrder());

        await().atMost(Duration.ofSeconds(10)).untilAsserted(() -> assertThat(broker.published).containsExactly(saved.getId()));
    }

    private static OrderEntity newOrder() {
        OrderItemEntity item = new OrderItemEntity();
        item.setProductId("sku-1");
        item.setQuantity(1);
        item.setPrice(10);
        item.setName("Thing");
        item.setTotalCost(10);
        OrderEntity order = new OrderEntity();
        order.setFirstName("Canary");
        order.setLastName("Test");
        order.setEmail("canary@test.local");
        order.getItems().add(item);
        return order;
    }
}
