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

import com.amazon.sample.orders.entities.OrderEntity;
import com.amazon.sample.orders.messaging.OrdersEventHandler;
import org.hibernate.resource.jdbc.spi.StatementInspector;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.orm.jpa.DataJpaTest;
import org.springframework.boot.test.autoconfigure.orm.jpa.TestEntityManager;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

import java.util.List;
import java.util.Locale;
import java.util.concurrent.CopyOnWriteArrayList;

import static com.amazon.sample.orders.config.persistence.DsqlIndexInitializer.CREATED_ON_INDEX;
import static org.assertj.core.api.Assertions.assertThat;

@DataJpaTest(properties = "spring.jpa.properties.hibernate.session_factory.statement_inspector="
    + "com.amazon.sample.orders.services.OrderServiceListTests$SqlRecorder")
@Import(OrderService.class)
class OrderServiceListTests {

    /** Records every SQL statement Hibernate prepares. */
    public static class SqlRecorder implements StatementInspector {
        static final List<String> STATEMENTS = new CopyOnWriteArrayList<>();

        @Override
        public String inspect(String sql) {
            STATEMENTS.add(sql.toLowerCase(Locale.ROOT));
            return sql;
        }
    }

    @MockitoBean
    private OrdersEventHandler eventHandler;

    @Autowired
    private OrderService service;

    @Autowired
    private TestEntityManager entityManager;

    @BeforeEach
    void saveMoreOrdersThanOnePage() {
        // 25 orders, one minute apart, so a Page of 20 would need a count to know there are more.
        for (int minute = 0; minute < 25; minute++) {
            OrderEntity order = new OrderEntity();
            order.setFirstName("Canary");
            order.setLastName("Test");
            order.setEmail("canary@test.local");
            order.setCreatedOn(String.format("2026-10-06T12:%02d:00Z", minute));
            entityManager.persist(order);
        }
        entityManager.flush();
        entityManager.clear();
        SqlRecorder.STATEMENTS.clear();
    }

    @Test
    void listReturnsTheTwentyNewestOrdersNewestFirst() {
        List<OrderEntity> orders = service.list();

        assertThat(orders).hasSize(20);
        assertThat(orders.get(0).getCreatedOn()).isEqualTo("2026-10-06T12:24:00Z");
        assertThat(orders.get(19).getCreatedOn()).isEqualTo("2026-10-06T12:05:00Z");
    }

    @Test
    void listRunsNoCountQuery() {
        service.list();

        assertThat(SqlRecorder.STATEMENTS).isNotEmpty().noneMatch(sql -> sql.contains("count("));
    }

    @Test
    void listSortsOnTheColumnTheDsqlIndexCovers() {
        service.list();

        String listQuery = SqlRecorder.STATEMENTS.stream()
            .filter(sql -> sql.contains("from customer_order ")).findFirst().orElseThrow();
        assertThat(listQuery).containsPattern("order by \\w+\\.created_on desc");
        assertThat(CREATED_ON_INDEX).contains("ON customer_order (created_on)");
    }
}
