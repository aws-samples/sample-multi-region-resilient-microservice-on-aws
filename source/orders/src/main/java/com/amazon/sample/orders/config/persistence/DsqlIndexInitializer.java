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

package com.amazon.sample.orders.config.persistence;

import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;

/**
 * Creates the index that lets the order list read the newest orders instead of the whole
 * table. Without it, ORDER BY created_on scans and sorts every order (1.4 s of a 2.9 s
 * GET /orders in test2, growing as the canaries add orders).
 *
 * Hibernate's schema update can't create it: Aurora DSQL builds every index asynchronously
 * and accepts only CREATE INDEX ASYNC, which Hibernate doesn't emit. The statement returns
 * a job id at once and the index is used when the build completes. IF NOT EXISTS makes it
 * a no-op on every later start. Best effort: any failure is logged and startup continues,
 * because the list still works without the index, only slower.
 */
@Component
@Profile("dsql")
@Slf4j
public class DsqlIndexInitializer implements ApplicationRunner {

    public static final String CREATED_ON_INDEX =
        "CREATE INDEX ASYNC IF NOT EXISTS customer_order_created_on_idx ON customer_order (created_on)";

    // Runs before the application reports ready, so it must not hold startup for long.
    static final int QUERY_TIMEOUT_SECONDS = 10;

    private final DataSource dataSource;

    public DsqlIndexInitializer(DataSource dataSource) {
        this.dataSource = dataSource;
    }

    @Override
    public void run(ApplicationArguments args) {
        try (Connection connection = dataSource.getConnection();
             Statement statement = connection.createStatement()) {
            statement.setQueryTimeout(QUERY_TIMEOUT_SECONDS);
            if (statement.execute(CREATED_ON_INDEX)) {
                try (ResultSet result = statement.getResultSet()) {
                    if (result.next()) {
                        log.info("Submitted the customer_order created_on index build, job {}", result.getString(1));
                        return;
                    }
                }
            }
            log.info("The customer_order created_on index already exists");
        } catch (SQLException e) {
            log.warn("Could not create the customer_order created_on index, so the order list scans the table: {}",
                e.getMessage());
        }
    }
}
