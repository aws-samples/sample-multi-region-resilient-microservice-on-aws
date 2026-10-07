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

package com.amazon.sample.orders.messaging;

import com.amazon.sample.events.orders.Order;
import com.amazon.sample.events.orders.OrderCreatedEvent;
import com.amazon.sample.orders.entities.OrderEntity;
import com.amazon.sample.orders.entities.OrderItemEntity;
import org.apache.logging.log4j.Level;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.core.Logger;
import org.apache.logging.log4j.core.LogEvent;
import org.apache.logging.log4j.core.appender.AbstractAppender;
import org.apache.logging.log4j.core.config.Configurator;
import org.apache.logging.log4j.core.config.Property;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.mockito.ArgumentCaptor;
import org.springframework.amqp.AmqpIOException;
import org.springframework.context.ApplicationEventPublisher;

import java.net.SocketTimeoutException;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.awaitility.Awaitility.await;
import static org.junit.jupiter.api.Assertions.assertTimeoutPreemptively;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoMoreInteractions;

/**
 * The order-created publish is best effort: a broker that is down, slow or gone must not make
 * an order fail or wait. The pool here has one thread and a queue of two unless a test says
 * otherwise, so that "full" is reached with three events.
 *
 * Several tests make the broker block. If the publish ever moves back onto the caller's thread,
 * the test thread would be the one stuck, so every test has a time limit and the stub's wait is
 * bounded: a regression fails the test, it does not hang the build.
 */
@Timeout(30)
class OrdersEventHandlerTests {

    /** How long a call may take and still count as not waiting for the broker. */
    private static final Duration PROMPTLY = Duration.ofSeconds(2);

    /** Long enough for a pool thread to start and run an event; the tests never wait this long for nothing. */
    private static final Duration EVENTUALLY = Duration.ofSeconds(10);

    private final StubProvider provider = new StubProvider();
    private final ApplicationEventPublisher springPublisher = mock(ApplicationEventPublisher.class);
    private LogCapture logs;
    private OrdersEventHandler handler;

    @BeforeEach
    void startCapturingWarnings() {
        logs = new LogCapture(OrdersEventHandler.class);
        handler = handlerWith(1, 2, Duration.ofSeconds(5));
    }

    @AfterEach
    void stopEverything() {
        provider.release.countDown();
        handler.destroy();
        logs.close();
    }

    // --- off the request path -------------------------------------------------------------

    @Test
    void publishesTheEventOnAThreadOfItsOwn() {
        handler.onOrderCreated(eventFor("order-1"));

        await().atMost(EVENTUALLY).untilAsserted(() -> assertThat(provider.published).containsExactly("order-1"));
        assertThat(provider.threads).singleElement().asString()
            .startsWith("order-event-publisher-").isNotEqualTo(Thread.currentThread().getName());
        assertThat(provider.daemon).containsOnly(true);
    }

    @Test
    void returnsAtOnceWhileThePublishIsBlockedOnTheBroker() throws Exception {
        provider.block = true;

        assertTimeoutPreemptively(PROMPTLY, () -> handler.onOrderCreated(eventFor("order-1")));

        assertThat(provider.started.tryAcquire(EVENTUALLY.toSeconds(), TimeUnit.SECONDS)).as("the pool thread is in the provider").isTrue();
        assertThat(provider.published).as("and is still stuck there").isEmpty();
        provider.release.countDown();
        await().atMost(EVENTUALLY).untilAsserted(() -> assertThat(provider.published).containsExactly("order-1"));
    }

    // --- a publish that fails -------------------------------------------------------------

    @Test
    void aProviderThatThrowsDoesNotFailTheCaller() {
        provider.failure = new AmqpIOException(new SocketTimeoutException("Connect timed out"));

        assertThatCode(() -> handler.onOrderCreated(eventFor("order-7"))).doesNotThrowAnyException();

        await().atMost(EVENTUALLY).untilAsserted(() -> assertThat(logs.lines()).containsExactly(
            "WARN Could not publish the order-created event for order order-7: SocketTimeoutException: Connect timed out"));
    }

    @Test
    void aFailedPublishIsNotRetriedAndTheNextEventStillGoesOut() {
        provider.failure = new IllegalStateException("broker is down");
        handler.onOrderCreated(eventFor("order-1"));
        await().atMost(EVENTUALLY).until(() -> provider.attempts.get() == 1);

        provider.failure = null;
        handler.onOrderCreated(eventFor("order-2"));

        await().atMost(EVENTUALLY).untilAsserted(() -> assertThat(provider.published).containsExactly("order-2"));
        assertThat(provider.attempts.get()).isEqualTo(2);
    }

    // --- a queue that is full -------------------------------------------------------------

    @Test
    void aFullQueueDropsTheEventWithAWarningAndNoException() throws Exception {
        provider.block = true;
        handler.onOrderCreated(eventFor("order-1"));
        assertThat(provider.started.tryAcquire(EVENTUALLY.toSeconds(), TimeUnit.SECONDS)).isTrue();   // order-1 holds the only thread
        handler.onOrderCreated(eventFor("order-2"));
        handler.onOrderCreated(eventFor("order-3"));                                                 // the queue of two is now full
        assertThat(logs.lines()).isEmpty();

        assertThatCode(() -> handler.onOrderCreated(eventFor("order-4"))).doesNotThrowAnyException();

        assertThat(logs.lines()).containsExactly("WARN Dropped the order-created event for order order-4: the publish queue is full");
        provider.release.countDown();
        await().atMost(EVENTUALLY).untilAsserted(() ->
            assertThat(provider.published).containsExactlyInAnyOrder("order-1", "order-2", "order-3"));
        assertThat(provider.published).doesNotContain("order-4");
    }

    @Test
    void everyDroppedEventIsNamedInTheLog() throws Exception {
        provider.block = true;
        handler.onOrderCreated(eventFor("order-1"));
        assertThat(provider.started.tryAcquire(EVENTUALLY.toSeconds(), TimeUnit.SECONDS)).isTrue();
        handler.onOrderCreated(eventFor("order-2"));
        handler.onOrderCreated(eventFor("order-3"));

        handler.onOrderCreated(eventFor("order-4"));
        handler.onOrderCreated(eventFor("order-5"));

        assertThat(logs.lines()).containsExactly(
            "WARN Dropped the order-created event for order order-4: the publish queue is full",
            "WARN Dropped the order-created event for order order-5: the publish queue is full");
    }

    @Test
    void anEventWithoutAnOrderIsDroppedWithoutFailingTheLogging() throws Exception {
        provider.block = true;
        handler.onOrderCreated(eventFor("order-1"));
        assertThat(provider.started.tryAcquire(EVENTUALLY.toSeconds(), TimeUnit.SECONDS)).isTrue();
        handler.onOrderCreated(eventFor("order-2"));
        handler.onOrderCreated(eventFor("order-3"));

        assertThatCode(() -> handler.onOrderCreated(new OrderCreatedEvent())).doesNotThrowAnyException();

        assertThat(logs.lines()).containsExactly("WARN Dropped the order-created event for order unknown: the publish queue is full");
    }

    // --- the sizes the service runs with ----------------------------------------------------

    @Test
    void theServiceRunsTwoDaemonThreadsAndQueuesOneHundredEvents() throws Exception {
        OrdersEventHandler production = new OrdersEventHandler(springPublisher, provider);
        provider.block = true;
        try {
            // Two events go straight to the two threads, a hundred wait, the next is dropped.
            for (int i = 1; i <= 102; i++) {
                production.onOrderCreated(eventFor("order-" + i));
            }
            assertThat(provider.started.tryAcquire(2, EVENTUALLY.toSeconds(), TimeUnit.SECONDS)).as("both threads are busy").isTrue();
            assertThat(logs.lines()).as("102 events fit").isEmpty();
            assertThat(provider.threads).hasSize(2).doesNotHaveDuplicates();
            assertThat(provider.daemon).containsOnly(true);

            production.onOrderCreated(eventFor("order-103"));

            assertThat(logs.lines()).containsExactly("WARN Dropped the order-created event for order order-103: the publish queue is full");
        } finally {
            provider.release.countDown();
            production.destroy();
        }
    }

    // --- shutdown ----------------------------------------------------------------------------

    @Test
    void anEventAfterShutdownIsDroppedWithAWarningAndNoException() {
        handler.destroy();

        assertThatCode(() -> handler.onOrderCreated(eventFor("order-9"))).doesNotThrowAnyException();

        assertThat(logs.lines()).containsExactly("WARN Dropped the order-created event for order order-9: the service is shutting down");
    }

    @Test
    void shutdownLetsEventsAlreadyQueuedGoOut() {
        handler.onOrderCreated(eventFor("order-1"));
        handler.onOrderCreated(eventFor("order-2"));

        handler.destroy();

        assertThat(provider.published).containsExactlyInAnyOrder("order-1", "order-2");
    }

    @Test
    void shutdownGivesUpOnWhatIsStillStuckAndSaysHowMuch() throws Exception {
        handler.destroy();
        handler = handlerWith(1, 2, Duration.ofMillis(200));
        provider.block = true;
        handler.onOrderCreated(eventFor("order-1"));
        assertThat(provider.started.tryAcquire(EVENTUALLY.toSeconds(), TimeUnit.SECONDS)).isTrue();
        handler.onOrderCreated(eventFor("order-2"));
        handler.onOrderCreated(eventFor("order-3"));

        assertTimeoutPreemptively(PROMPTLY, handler::destroy);

        assertThat(logs.lines()).containsExactly("WARN Gave up on 2 order-created events still waiting to be published at shutdown");
        assertThat(provider.published).isEmpty();
    }

    // --- what was already there ----------------------------------------------------------

    @Test
    void postCreatedEventHandsTheOrderToSpringsPublisher() {
        OrderItemEntity item = new OrderItemEntity();
        item.setProductId("sku-1");
        OrderEntity entity = new OrderEntity();
        entity.setId("order-1");
        entity.setFirstName("Canary");
        entity.setLastName("Test");
        entity.setEmail("canary@test.local");
        entity.getItems().add(item);

        handler.postCreatedEvent(entity);

        ArgumentCaptor<OrderCreatedEvent> sent = ArgumentCaptor.forClass(OrderCreatedEvent.class);
        verify(springPublisher).publishEvent(sent.capture());
        verifyNoMoreInteractions(springPublisher);
        Order order = sent.getValue().getOrder();
        assertThat(order.getId()).isEqualTo("order-1");
        assertThat(order.getFirstName()).isEqualTo("Canary");
        assertThat(order.getLastName()).isEqualTo("Test");
        assertThat(order.getEmail()).isEqualTo("canary@test.local");
        assertThat(order.getOrderItems()).containsExactly(item);
        assertThat(provider.attempts.get()).as("posting hands off, it does not publish itself").isZero();
    }

    // --- helpers -----------------------------------------------------------------------------

    private OrdersEventHandler handlerWith(int threads, int queueCapacity, Duration shutdownWait) {
        return new OrdersEventHandler(springPublisher, provider, OrdersEventHandler.newPublishExecutor(threads, queueCapacity), shutdownWait);
    }

    private static OrderCreatedEvent eventFor(String orderId) {
        Order order = new Order();
        order.setId(orderId);
        order.setFirstName("Canary");
        order.setLastName("Test");
        order.setEmail("canary@test.local");
        OrderCreatedEvent event = new OrderCreatedEvent();
        event.setOrder(order);
        return event;
    }

    /** A broker the test controls: it can be told to block until released, or to fail. */
    static final class StubProvider implements MessagingProvider {
        // Never forever: see the class comment.
        static final Duration BLOCKED_AT_MOST = Duration.ofSeconds(5);

        final List<String> published = new CopyOnWriteArrayList<>();
        final List<String> threads = new CopyOnWriteArrayList<>();
        final List<Boolean> daemon = new CopyOnWriteArrayList<>();
        final AtomicInteger attempts = new AtomicInteger();
        final Semaphore started = new Semaphore(0);
        final CountDownLatch release = new CountDownLatch(1);
        volatile boolean block;
        volatile RuntimeException failure;

        @Override
        public void publishEvent(Object event) {
            attempts.incrementAndGet();
            if (!threads.contains(Thread.currentThread().getName())) {
                threads.add(Thread.currentThread().getName());
                daemon.add(Thread.currentThread().isDaemon());
            }
            started.release();
            if (block) {
                try {
                    release.await(BLOCKED_AT_MOST.toSeconds(), TimeUnit.SECONDS);
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

    /** Collects the WARN lines the handler logs. Log4j 2 is the logging backend of this service. */
    static final class LogCapture extends AbstractAppender implements AutoCloseable {
        private final Logger logger;
        private final Level previousLevel;
        private final List<String> lines = new CopyOnWriteArrayList<>();

        LogCapture(Class<?> source) {
            super("capture-" + source.getSimpleName(), null, null, true, Property.EMPTY_ARRAY);
            this.logger = (Logger) LogManager.getLogger(source);
            this.previousLevel = logger.getLevel();
            Configurator.setLevel(logger.getName(), Level.WARN);
            start();
            logger.addAppender(this);
        }

        @Override
        public void append(LogEvent event) {
            lines.add(event.getLevel() + " " + event.getMessage().getFormattedMessage());
        }

        List<String> lines() {
            return lines;
        }

        @Override
        public void close() {
            logger.removeAppender(this);
            stop();
            Configurator.setLevel(logger.getName(), previousLevel);
        }
    }
}
