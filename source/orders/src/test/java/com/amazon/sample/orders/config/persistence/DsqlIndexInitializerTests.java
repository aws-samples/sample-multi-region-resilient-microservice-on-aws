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

import org.junit.jupiter.api.Test;
import org.springframework.context.annotation.Profile;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class DsqlIndexInitializerTests {

    @Test
    void theStatementIsTheAsyncIdempotentDsqlForm() {
        // DSQL rejects CREATE INDEX without ASYNC; without IF NOT EXISTS every restart would fail.
        assertThat(DsqlIndexInitializer.CREATED_ON_INDEX).isEqualTo(
            "CREATE INDEX ASYNC IF NOT EXISTS customer_order_created_on_idx ON customer_order (created_on)");
    }

    @Test
    void runSubmitsTheIndexBuildWithABoundedWait() throws SQLException {
        Statement statement = mock(Statement.class);
        ResultSet result = mock(ResultSet.class);
        when(statement.execute(anyString())).thenReturn(true);
        when(statement.getResultSet()).thenReturn(result);
        when(result.next()).thenReturn(true);
        when(result.getString(1)).thenReturn("jh2gbtx4mzhgfkbimtgwn5j45y");

        new DsqlIndexInitializer(dataSourceFor(statement)).run(null);

        verify(statement).setQueryTimeout(DsqlIndexInitializer.QUERY_TIMEOUT_SECONDS);
        verify(statement).execute(DsqlIndexInitializer.CREATED_ON_INDEX);
        assertThat(DsqlIndexInitializer.QUERY_TIMEOUT_SECONDS).isBetween(1, 30);
    }

    @Test
    void runCarriesOnWhenTheIndexAlreadyExists() throws SQLException {
        Statement statement = mock(Statement.class);
        when(statement.execute(anyString())).thenReturn(false);  // IF NOT EXISTS: notice, no rows

        assertThatCode(() -> new DsqlIndexInitializer(dataSourceFor(statement)).run(null))
            .doesNotThrowAnyException();
    }

    @Test
    void runNeverFailsStartupWhenTheDatabaseRefuses() throws SQLException {
        DataSource unreachable = mock(DataSource.class);
        when(unreachable.getConnection()).thenThrow(new SQLException("change conflicts with another transaction", "40001"));

        assertThatCode(() -> new DsqlIndexInitializer(unreachable).run(null)).doesNotThrowAnyException();
    }

    @Test
    void runNeverFailsStartupWhenTheStatementFails() throws SQLException {
        Statement statement = mock(Statement.class);
        when(statement.execute(anyString())).thenThrow(new SQLException("canceling statement due to user request", "57014"));

        assertThatCode(() -> new DsqlIndexInitializer(dataSourceFor(statement)).run(null))
            .doesNotThrowAnyException();
    }

    @Test
    void itRunsOnlyAgainstDsql() {
        // CREATE INDEX ASYNC is DSQL syntax; the default profile's H2 and plain PostgreSQL reject it.
        Profile profile = DsqlIndexInitializer.class.getAnnotation(Profile.class);
        assertThat(profile).isNotNull();
        assertThat(profile.value()).containsExactly("dsql");
    }

    private static DataSource dataSourceFor(Statement statement) throws SQLException {
        Connection connection = mock(Connection.class);
        when(connection.createStatement()).thenReturn(statement);
        DataSource dataSource = mock(DataSource.class);
        when(dataSource.getConnection()).thenReturn(connection);
        return dataSource;
    }
}
