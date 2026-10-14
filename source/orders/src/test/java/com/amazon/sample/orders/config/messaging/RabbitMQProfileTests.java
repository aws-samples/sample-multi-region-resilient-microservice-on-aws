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

package com.amazon.sample.orders.config.messaging;

import com.amazon.sample.orders.messaging.MessagingProvider;
import com.amazon.sample.orders.messaging.OrdersEventHandler;
import com.amazon.sample.orders.messaging.rabbitmq.RabbitMQMessagingProvider;
import org.junit.jupiter.api.Test;
import org.springframework.amqp.rabbit.connection.CachingConnectionFactory;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * What the service is configured with when it runs against a broker (SPRING_PROFILES_ACTIVE
 * has rabbitmq in ECS). The context starts without a broker: connections open on first use.
 */
@SpringBootTest
@ActiveProfiles("rabbitmq")
class RabbitMQProfileTests {

    @Autowired
    private ConnectionFactory connectionFactory;
    @Autowired
    private MessagingProvider messagingProvider;
    @Autowired
    private OrdersEventHandler eventHandler;

    @Test
    void aPublishThreadWaitsAtMostTwoSecondsToConnect() {
        // The client library's default is 60 seconds.
        com.rabbitmq.client.ConnectionFactory client = ((CachingConnectionFactory) connectionFactory).getRabbitConnectionFactory();
        assertThat(client.getConnectionTimeout()).isEqualTo(2_000);
    }

    @Test
    void theEventsGoToTheBrokerThroughTheRabbitProvider() {
        assertThat(messagingProvider).isInstanceOf(RabbitMQMessagingProvider.class);
        assertThat(eventHandler).isNotNull();
    }
}
