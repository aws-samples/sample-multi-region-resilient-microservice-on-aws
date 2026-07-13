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
 * Orders Service API client models.
 *
 * App-facing field names are preserved from the previous openapi-generator
 * client (`unitCost`, `id`) and mapped to the wire names expected by the
 * orders service (`price`, `productId`) during (de)serialization in
 * ordersApi.ts.
 */

export interface OrderItem {
  /** Serialized to wire field `price`. */
  unitCost?: number;
  /** Serialized to wire field `productId`. */
  id?: string;
  name?: string;
  totalCost?: number;
  quantity?: number;
}

export interface Order {
  email?: string;
  firstName?: string;
  lastName?: string;
  items?: Array<OrderItem>;
}

export interface ExistingOrder {
  id?: string;
  email?: string;
  firstName?: string;
  lastName?: string;
  items?: Array<OrderItem>;
}

/** Wire representation of an order item (orders service contract). */
interface WireOrderItem {
  price?: number;
  productId?: string;
  name?: string;
  totalCost?: number;
  quantity?: number;
}

export function toWireItem(item: OrderItem): WireOrderItem {
  return {
    price: item.unitCost,
    productId: item.id,
    name: item.name,
    totalCost: item.totalCost,
    quantity: item.quantity,
  };
}

export function fromWireItem(item: WireOrderItem): OrderItem {
  return {
    unitCost: item.price,
    id: item.productId,
    name: item.name,
    totalCost: item.totalCost,
    quantity: item.quantity,
  };
}
