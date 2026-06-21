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

import { RedisCheckoutRepository } from './RedisCheckoutRepository';

/**
 * Unit tests for RedisCheckoutRepository session TTL behaviour.
 *
 * Guards against regression of the session-lifetime bug: bare Redis
 * SET calls with no EX/PX create keys that never expire, causing
 * stale checkout sessions to accumulate indefinitely in Redis.
 */
describe('RedisCheckoutRepository', () => {
  describe('set()', () => {
    it('writes keys with a finite TTL (prevents indefinite session accumulation)', async () => {
      const mockClient = {
        set: jest.fn().mockResolvedValue('OK'),
      };

      const repo = new RedisCheckoutRepository('redis://primary', 'redis://reader');
      // Inject the mock to avoid opening a real connection.
      // eslint-disable-next-line @typescript-eslint/dot-notation
      repo['_client'] = mockClient as any;

      await repo.set('customer-abc', '{"cart":"data"}');

      expect(mockClient.set).toHaveBeenCalledTimes(1);
      const [key, value, options] = mockClient.set.mock.calls[0];
      expect(key).toBe('customer-abc');
      expect(value).toBe('{"cart":"data"}');
      expect(options).toBeDefined();
      expect(options).toHaveProperty('EX');
      expect(typeof options.EX).toBe('number');
      expect(options.EX).toBeGreaterThan(0);
      expect(options.EX).toBeLessThanOrEqual(86400); // sanity: <= 1 day
    });

    it('uses 3600 seconds (1 hour) TTL specifically', async () => {
      const mockClient = {
        set: jest.fn().mockResolvedValue('OK'),
      };
      const repo = new RedisCheckoutRepository('redis://primary', 'redis://reader');
      // eslint-disable-next-line @typescript-eslint/dot-notation
      repo['_client'] = mockClient as any;

      await repo.set('customer-xyz', 'value');

      expect(mockClient.set).toHaveBeenCalledWith(
        'customer-xyz',
        'value',
        { EX: 3600 },
      );
    });
  });
});
