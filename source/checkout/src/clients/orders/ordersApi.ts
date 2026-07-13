/**
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

/**
 * Orders Service API client.
 *
 * Uses native fetch (Node 18+) — replaces the openapi-generator
 * typescript-node client that depended on the deprecated `request`
 * package (and its vulnerable transitive deps uuid@3 / tough-cookie@2).
 */

import { Order, ExistingOrder, toWireItem, fromWireItem } from './models';

export class HttpError extends Error {
  constructor(public statusCode: number, public body: unknown) {
    super(`HTTP request failed with status ${statusCode}`);
    this.name = 'HttpError';
  }
}

export class OrdersApi {
  private basePath: string;

  constructor(basePath: string) {
    this.basePath = basePath.replace(/\/+$/, '');
  }

  /**
   * Create an order.
   * POST /orders
   */
  async createOrder(order: Order): Promise<ExistingOrder> {
    const wireOrder = {
      email: order.email,
      firstName: order.firstName,
      lastName: order.lastName,
      items: order.items?.map(toWireItem),
    };

    const response = await fetch(`${this.basePath}/orders`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
      },
      body: JSON.stringify(wireOrder),
    });

    if (!response.ok) {
      throw new HttpError(response.status, await response.text());
    }

    const body = await response.json() as Record<string, unknown>;
    return {
      id: body.id as string | undefined,
      email: body.email as string | undefined,
      firstName: body.firstName as string | undefined,
      lastName: body.lastName as string | undefined,
      items: Array.isArray(body.items) ? body.items.map(fromWireItem) : undefined,
    };
  }

  /**
   * List orders.
   * GET /orders
   */
  async listOrders(): Promise<Array<ExistingOrder>> {
    const response = await fetch(`${this.basePath}/orders`, {
      method: 'GET',
      headers: { 'Accept': 'application/json' },
    });

    if (!response.ok) {
      throw new HttpError(response.status, await response.text());
    }

    const body = await response.json() as Array<Record<string, unknown>>;
    return body.map((o) => ({
      id: o.id as string | undefined,
      email: o.email as string | undefined,
      firstName: o.firstName as string | undefined,
      lastName: o.lastName as string | undefined,
      items: Array.isArray(o.items) ? o.items.map(fromWireItem) : undefined,
    }));
  }
}
