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

package com.amazon.sample.ui.web.util;

import org.junit.jupiter.api.Test;
import org.springframework.mock.http.server.reactive.MockServerHttpRequest;
import org.springframework.mock.web.server.MockServerWebExchange;
import org.springframework.web.server.ServerWebExchange;

import java.util.HashSet;
import java.util.Set;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assertions.fail;

/**
 * Regression tests for SessionIDUtil.
 *
 * Guards against re-introduction of a hardcoded session identifier
 * that previously caused all site visitors to share the same checkout
 * session. See commit history for details.
 */
class SessionIDUtilTest {

    /**
     * Two successive calls to addSessionCookie must produce distinct IDs.
     * If this fails, the fix for the shared-session bug has regressed.
     */
    @Test
    void addSessionCookie_returnsDistinctIdsOnConsecutiveCalls() {
        String id1 = SessionIDUtil.addSessionCookie(exchange());
        String id2 = SessionIDUtil.addSessionCookie(exchange());

        assertNotNull(id1);
        assertNotNull(id2);
        assertNotEquals(id1, id2,
            "addSessionCookie must return a unique ID per call. "
                + "A hardcoded constant here was the root cause of session "
                + "sharing across all site visitors.");
    }

    /**
     * Generates many IDs and asserts they are all unique.
     * A hardcoded sentinel would fail this immediately.
     */
    @Test
    void addSessionCookie_producesUniqueIdsAcrossManyCalls() {
        final int iterations = 1000;
        Set<String> seen = new HashSet<>();

        for (int i = 0; i < iterations; i++) {
            String id = SessionIDUtil.addSessionCookie(exchange());
            assertTrue(seen.add(id),
                "Duplicate session ID generated at iteration " + i + ": " + id);
        }

        assertEquals(iterations, seen.size());
    }

    /**
     * The session ID must be a valid UUID string, not any old constant.
     */
    @Test
    void addSessionCookie_returnsValidUuid() {
        String id = SessionIDUtil.addSessionCookie(exchange());

        try {
            UUID parsed = UUID.fromString(id);
            // UUID.fromString accepts some malformed inputs; round-trip
            // ensures canonical form.
            assertEquals(id, parsed.toString(),
                "Session ID must be a canonical UUID string");
        } catch (IllegalArgumentException e) {
            fail("Session ID is not a valid UUID: " + id);
        }
    }

    /**
     * The returned ID must match the SESSIONID cookie written to the response
     * (i.e., the method's return value equals the cookie it sets).
     */
    @Test
    void addSessionCookie_writesSessionIdCookieMatchingReturnValue() {
        ServerWebExchange exchange = exchange();

        String returned = SessionIDUtil.addSessionCookie(exchange);

        String cookieValue = exchange.getResponse()
            .getCookies()
            .getFirst(SessionIDUtil.COOKIE_NAME)
            .getValue();

        assertEquals(returned, cookieValue,
            "Cookie value written to response must match the returned session ID");
    }

    private static ServerWebExchange exchange() {
        return MockServerWebExchange.from(MockServerHttpRequest.get("/"));
    }
}
