/** 连接配置缓存（docs/06 §4）：按 spaceId 分键、invalidate/dispose 清缓存。 */

import { describe, expect, it } from 'vitest';

import { FactlayerAdapter } from '../src/adapter.js';
import { FactlayerClient, ShimError } from '../src/client.js';
import { ConnectionCache } from '../src/config.js';
import type {
  MemoryProviderAdapterFactoryContext,
  MemoryProviderConnection,
} from '../src/contract.js';
import { fakeAuthority, fakeSpace, jsonResponse, recordingFetch } from './helpers.js';

function makeContext(
  connection: MemoryProviderConnection,
  calls?: Array<[string, string | undefined]>,
) {
  const authority = fakeAuthority(connection);
  if (calls !== undefined) {
    const inner = authority.authority.providerConnection.bind(authority.authority);
    authority.authority.providerConnection = (id, expected) => {
      calls.push([id, expected]);
      return inner(id, expected);
    };
  }
  const context: MemoryProviderAdapterFactoryContext = {
    memorySpaces: authority.authority,
    memoryBodies: authority.authority,
    config: { timeoutMs: 15_000 },
  };
  return { context, authority };
}

describe('ConnectionCache', () => {
  it('同 spaceId 只读一次 providerConnection；不同空间各自读', () => {
    const calls: Array<[string, string | undefined]> = [];
    const { context } = makeContext({ baseUrl: 'http://shim.test/' }, calls);
    const cache = new ConnectionCache(context, 'factlayer');

    expect(cache.get('space-1')).toEqual({ baseUrl: 'http://shim.test', apiKey: '' });
    expect(cache.get('space-1')).toEqual({ baseUrl: 'http://shim.test', apiKey: '' });
    expect(calls).toHaveLength(1);
    expect(calls[0]).toEqual(['space-1', 'factlayer']);

    cache.get('space-2');
    expect(calls).toHaveLength(2);
  });

  it('invalidate(spaceId) 只清该空间；invalidate() 清全部', () => {
    const calls: Array<[string, string | undefined]> = [];
    const { context } = makeContext({ baseUrl: 'http://shim.test' }, calls);
    const cache = new ConnectionCache(context, 'factlayer');

    cache.get('a');
    cache.get('b');
    expect(calls).toHaveLength(2);

    cache.invalidate('a');
    cache.get('a');
    expect(calls).toHaveLength(3);

    cache.invalidate();
    cache.get('a');
    cache.get('b');
    expect(calls).toHaveLength(5);
  });

  it('providerConnection 抛错 → config 错误（带空间 id）', () => {
    const { context } = makeContext({});
    context.memorySpaces!.providerConnection = () => {
      throw new Error('unknown memory space: ghost');
    };
    const cache = new ConnectionCache(context, 'factlayer');
    let caught: unknown;
    try {
      cache.get('ghost');
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(ShimError);
    expect((caught as ShimError).kind).toBe('config');
    expect((caught as ShimError).message).toMatch(/ghost/u);
  });

  it('baseUrl 缺失/非 http(s) → config 错误', () => {
    const { context } = makeContext({ baseUrl: '   ' });
    const cache = new ConnectionCache(context, 'factlayer');
    expect(() => cache.get('space-1')).toThrow(/baseUrl 未配置/u);

    const { context: ftp } = makeContext({ baseUrl: 'ftp://shim' });
    expect(() => new ConnectionCache(ftp, 'factlayer').get('space-1')).toThrow(/http\(s\)/u);
  });

  it('apiKey 原样取出（secret 值只能从这里拿）', () => {
    const { context } = makeContext({ baseUrl: 'http://shim.test', apiKey: 'k-123' });
    expect(new ConnectionCache(context, 'factlayer').get('space-1')).toEqual({
      baseUrl: 'http://shim.test',
      apiKey: 'k-123',
    });
  });

  it('adapter.invalidateStatus / dispose 让下一次 search 重读配置', async () => {
    const calls: Array<[string, string | undefined]> = [];
    const { context } = makeContext({ baseUrl: 'http://shim.test' }, calls);
    const cache = new ConnectionCache(context, 'factlayer');
    const recorder = recordingFetch(() => jsonResponse({ count: 0, items: [] }));
    const adapter = new FactlayerAdapter({
      connections: cache,
      createClient: () =>
        new FactlayerClient({ baseUrl: 'http://shim.test', fetch: recorder.fetch }),
    });

    await adapter.search(fakeSpace(), { query: 'q' });
    await adapter.search(fakeSpace(), { query: 'q' });
    expect(calls).toHaveLength(1);

    adapter.invalidateStatus('factlayer-abc123');
    await adapter.search(fakeSpace(), { query: 'q' });
    expect(calls).toHaveLength(2);

    adapter.dispose();
    await adapter.search(fakeSpace(), { query: 'q' });
    expect(calls).toHaveLength(3);
  });
});
