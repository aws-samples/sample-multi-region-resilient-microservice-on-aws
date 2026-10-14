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
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.DisposableBean;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.core.NestedExceptionUtils;
import org.springframework.stereotype.Component;
import org.springframework.transaction.event.TransactionalEventListener;

import java.time.Duration;
import java.util.List;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Publishes the order-created event to the message broker, best effort, so that a broker
 * outage can't fail or slow an order.
 *
 * The listener runs after the order's transaction commits, on the request thread, and only
 * queues the event for a small pool that does the publishing. Before this, the publish ran on
 * the request thread: with the broker unreachable, reconnecting could block it for the
 * client library's default 60 seconds, callers give up on orders after 3 seconds (Service
 * Connect), so checkout returned an error and ui served an error page, although the order
 * had already been saved. The orders-broker-dependency test found it (see
 * docs/ngrh-test-ground-truth.md).
 *
 * Best effort means what it says. Nothing in this service consumes the events, so:
 * <ul>
 * <li>an event that can't be queued is dropped and logged (WARN, with the order id);</li>
 * <li>a publish that fails is logged and not retried;</li>
 * <li>the pool and its queue are bounded, so a long outage costs a fixed amount of memory:
 *     {@value #PUBLISH_THREADS} threads and {@value #PUBLISH_QUEUE_CAPACITY} waiting events.</li>
 * </ul>
 * application-rabbitmq.yml limits how long a pool thread can block opening a connection
 * (spring.rabbitmq.connection-timeout). A connection that was open when the broker became
 * unreachable can hold a thread longer, until the client's heartbeat notices; the events
 * that arrive meanwhile wait in the queue or are dropped, and no request waits for them.
 *
 * The orders-created metric is counted by OrdersMetrics, in a listener of its own that stays
 * on the request thread, so a dropped or failed publish doesn't silence it.
 */
@Component
@Slf4j
public class OrdersEventHandler implements DisposableBean {

    static final int PUBLISH_THREADS = 2;
    static final int PUBLISH_QUEUE_CAPACITY = 100;

    // Longer than a publish thread can block connecting (spring.rabbitmq.connection-timeout is 2 s).
    static final Duration SHUTDOWN_WAIT = Duration.ofSeconds(3);

    private final ApplicationEventPublisher publisher;
    private final MessagingProvider messagingProvider;
    private final ThreadPoolExecutor publishExecutor;
    private final Duration shutdownWait;

    @Autowired
    public OrdersEventHandler(ApplicationEventPublisher publisher, MessagingProvider messagingProvider) {
        this(publisher, messagingProvider, newPublishExecutor(PUBLISH_THREADS, PUBLISH_QUEUE_CAPACITY), SHUTDOWN_WAIT);
    }

    // Tests use a smaller queue and a shorter shutdown wait than production does.
    OrdersEventHandler(ApplicationEventPublisher publisher, MessagingProvider messagingProvider,
                       ThreadPoolExecutor publishExecutor, Duration shutdownWait) {
        this.publisher = publisher;
        this.messagingProvider = messagingProvider;
        this.publishExecutor = publishExecutor;
        this.shutdownWait = shutdownWait;
    }

    static ThreadPoolExecutor newPublishExecutor(int threads, int queueCapacity) {
        AtomicInteger number = new AtomicInteger();
        return new ThreadPoolExecutor(threads, threads, 0L, TimeUnit.MILLISECONDS,
            new ArrayBlockingQueue<>(queueCapacity),
            task -> {
                // Daemon threads: a publish stuck on an unreachable broker must not hold the JVM open.
                Thread thread = new Thread(task, "order-event-publisher-" + number.incrementAndGet());
                thread.setDaemon(true);
                return thread;
            },
            new ThreadPoolExecutor.AbortPolicy());
    }

    @TransactionalEventListener
    public void onOrderCreated(OrderCreatedEvent event) {
        try {
            publishExecutor.execute(() -> publish(event));
        } catch (RejectedExecutionException e) {
            log.warn("Dropped the order-created event for order {}: {}", orderId(event),
                publishExecutor.isShutdown() ? "the service is shutting down" : "the publish queue is full");
        }
    }

    private void publish(OrderCreatedEvent event) {
        try {
            messagingProvider.publishEvent(event);
        } catch (RuntimeException e) {
            Throwable cause = NestedExceptionUtils.getMostSpecificCause(e);
            log.warn("Could not publish the order-created event for order {}: {}: {}", orderId(event),
                cause.getClass().getSimpleName(), cause.getMessage());
        }
    }

    private static String orderId(OrderCreatedEvent event) {
        return event.getOrder() == null ? "unknown" : event.getOrder().getId();
    }

    public void postCreatedEvent(OrderEntity entity) {
        Order order = new Order();
        order.setId(entity.getId());
        order.setFirstName(entity.getFirstName());
        order.setLastName(entity.getLastName());
        order.setEmail(entity.getEmail());
        order.setOrderItems(entity.getItems());

        OrderCreatedEvent event = new OrderCreatedEvent();
        event.setOrder(order);

        publisher.publishEvent(event);
    }

    @Override
    public void destroy() {
        publishExecutor.shutdown();
        try {
            if (!publishExecutor.awaitTermination(shutdownWait.toMillis(), TimeUnit.MILLISECONDS)) {
                List<Runnable> abandoned = publishExecutor.shutdownNow();
                log.warn("Gave up on {} order-created events still waiting to be published at shutdown", abandoned.size());
            }
        } catch (InterruptedException e) {
            publishExecutor.shutdownNow();
            Thread.currentThread().interrupt();
        }
    }
}
