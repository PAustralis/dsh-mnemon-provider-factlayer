/** HTTP 客户端：超时、错误分类、status 的 30s stale-while-revalidate 与并发合并。 */

import { describe, expect, it } from 'vitest';

import {
  FactlayerClient,
  ShimError,
  STATUS_STALE_MS,
} from '../src/client.js';
import { hangingFetch, jsonResponse, recordingFetch } from './helpers.js';

describe('status stale-while-revalidate', () => {
  it('30s 内的成功结果直接复用，不发第二次请求', async () => {
    let clock = 0;
    const recorder = recordingFetch(() => jsonResponse({ status: 'ready' }));
    const client = new FactlayerClient({
      baseUrl: 'http://shim.test',
      fetch: recorder.fetch,
      now: () => clock,
    });

    await client.health();
    clock = STATUS_STALE_MS - 1;
    await client.health();

    expect(recorder.calls).toHaveLength(1);
  });

  it('超过 30s：立即返回旧值，并在后台重验', async () => {
    let clock = 0;
    let sequence = 0;
    const recorder = recordingFetch(() =>
      jsonResponse({ status: 'ready', sequence: ++sequence }),
    );
    const client = new FactlayerClient({
      baseUrl: 'http://shim.test',
      fetch: recorder.fetch,
      now: () => clock,
    });

    const first = (await client.health()) as Record<string, unknown>;
    clock = STATUS_STALE_MS + 1;
    const stale = (await client.health()) as Record<string, unknown>;

    expect(stale).toBe(first); // 同步路径拿到的就是旧值
    expect(stale.sequence).toBe(1);
    expect(recorder.calls.length).toBeLessThanOrEqual(2); // 重验已在后台发起

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(recorder.calls).toHaveLength(2);
    const fresh = (await client.health()) as Record<string, unknown>;
    expect(fresh.sequence).toBe(2);
    expect(recorder.calls).toHaveLength(2); // 新值在 30s 窗口内，不再打第三次
  });

  it('并发调用合并成一次请求', async () => {
    let release: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const recorder = recordingFetch(async () => {
      await gate;
      return jsonResponse({ status: 'ready' });
    });
    const client = new FactlayerClient({
      baseUrl: 'http://shim.test',
      fetch: recorder.fetch,
    });

    const first = client.health();
    const second = client.health();
    release?.();

    const [a, b] = await Promise.all([first, second]);
    expect(recorder.calls).toHaveLength(1);
    expect(a).toBe(b);
  });

  it('starting 不写缓存：连续两次调用各发一次请求（冷启动不被锁死 30s）', async () => {
    let clock = 0;
    const recorder = recordingFetch(() => jsonResponse({ status: 'starting' }));
    const client = new FactlayerClient({
      baseUrl: 'http://shim.test',
      fetch: recorder.fetch,
      now: () => clock,
    });

    await client.health();
    clock += 1; // 若被缓存，这里仍在 30s 窗口内
    await client.health();

    expect(recorder.calls).toHaveLength(2);
  });

  it('rebuilding 不写缓存；ready 之后才复用', async () => {
    let clock = 0;
    let rebuilding = true;
    const recorder = recordingFetch(() =>
      jsonResponse({ status: 'ready', rebuilding }),
    );
    const client = new FactlayerClient({
      baseUrl: 'http://shim.test',
      fetch: recorder.fetch,
      now: () => clock,
    });

    await client.health();
    await client.health(); // 重建中不复用
    expect(recorder.calls).toHaveLength(2);

    rebuilding = false;
    await client.health(); // 第一次 ready：真查并写缓存
    expect(recorder.calls).toHaveLength(3);
    clock += STATUS_STALE_MS - 1;
    await client.health(); // 30s 内复用
    expect(recorder.calls).toHaveLength(3);
  });

  it('invalidateStatus 后下一次重新请求（用户点了「重新连接」）', async () => {
    let clock = 0;
    const recorder = recordingFetch(() => jsonResponse({ status: 'ready' }));
    const client = new FactlayerClient({
      baseUrl: 'http://shim.test',
      fetch: recorder.fetch,
      now: () => clock,
    });

    await client.health();
    clock += 1;
    client.invalidateStatus();
    await client.health();

    expect(recorder.calls).toHaveLength(2);
  });
});

describe('错误分类', () => {
  it('连不上 → network', async () => {
    const client = new FactlayerClient({
      baseUrl: 'http://shim.test',
      fetch: () => Promise.reject(new TypeError('fetch failed')),
    });
    await expect(client.spaces()).rejects.toMatchObject({
      kind: 'network',
    } satisfies Partial<ShimError>);
  });

  it('超时 → timeout（含超时毫秒数）', async () => {
    const client = new FactlayerClient({
      baseUrl: 'http://shim.test',
      fetch: hangingFetch(),
      timeouts: { status: 10 },
    });
    await expect(client.health()).rejects.toMatchObject({
      kind: 'timeout',
    } satisfies Partial<ShimError>);
  });

  it('503 rebuilding → rebuilding，并带 shim 的原文文案', async () => {
    const client = new FactlayerClient({
      baseUrl: 'http://shim.test',
      fetch: () => Promise.resolve(jsonResponse({ error: 'rebuilding', message: '索引重建中，请稍后' }, 503)),
    });
    await expect(client.search({ query: 'q' })).rejects.toMatchObject({
      kind: 'rebuilding',
      message: '索引重建中，请稍后',
      status: 503,
    } satisfies Partial<ShimError>);
  });

  it('503 但没有 error code（网关）→ server + 通用「服务暂时不可用」，不误判重建', async () => {
    const client = new FactlayerClient({
      baseUrl: 'http://shim.test',
      fetch: () =>
        Promise.resolve(jsonResponse({ detail: 'upstream connect error' }, 503)),
    });
    const error = (await client.search({ query: 'q' }).catch((e: unknown) => e)) as ShimError;

    expect(error).toBeInstanceOf(ShimError);
    expect(error.kind).toBe('server');
    expect(error.status).toBe(503);
    expect(error.message).toContain('服务暂时不可用');
    expect(error.message).not.toContain('索引重建');
  });

  it('503 + HTML body（parseJson 回退成字符串）→ server，不误判重建', async () => {
    const client = new FactlayerClient({
      baseUrl: 'http://shim.test',
      fetch: () =>
        Promise.resolve(
          new Response('<html><body>503 Service Unavailable</body></html>', {
            status: 503,
            headers: { 'content-type': 'text/html' },
          }),
        ),
    });
    await expect(client.search({ query: 'q' })).rejects.toMatchObject({
      kind: 'server',
      status: 503,
    } satisfies Partial<ShimError>);
  });

  it('4xx → client；5xx → server', async () => {
    const client4 = new FactlayerClient({
      baseUrl: 'http://shim.test',
      fetch: () => Promise.resolve(jsonResponse({ detail: 'bad' }, 422)),
    });
    await expect(client4.search({ query: 'q' })).rejects.toMatchObject({
      kind: 'client',
      status: 422,
    } satisfies Partial<ShimError>);

    const client5 = new FactlayerClient({
      baseUrl: 'http://shim.test',
      fetch: () => Promise.resolve(jsonResponse({ detail: 'boom' }, 500)),
    });
    await expect(client5.search({ query: 'q' })).rejects.toMatchObject({
      kind: 'server',
      status: 500,
    } satisfies Partial<ShimError>);
  });

  it('外部 AbortSignal 先取消 → 透传取消错误（不当成 timeout）', async () => {
    const controller = new AbortController();
    const client = new FactlayerClient({
      baseUrl: 'http://shim.test',
      fetch: hangingFetch(),
      timeouts: { search: 10 },
    });
    const pending = client.search({ query: 'q' }, controller.signal);
    controller.abort();
    await expect(pending).rejects.toThrow();
  });

  it('baseUrl 为空 → 构造即抛 config 错误', () => {
    expect(() => new FactlayerClient({ baseUrl: '  ' })).toThrow(/baseUrl/u);
  });

  it('会发送 Bearer apiKey（为空则不发送）', async () => {
    const withKey = recordingFetch(() => jsonResponse([{ id: 's1' }]));
    const client = new FactlayerClient({
      baseUrl: 'http://shim.test',
      apiKey: 'secret-key',
      fetch: withKey.fetch,
    });
    await client.spaces();
    expect((withKey.calls[0]?.init.headers as Record<string, string>).authorization).toBe(
      'Bearer secret-key',
    );

    const withoutKey = recordingFetch(() => jsonResponse([{ id: 's1' }]));
    const anon = new FactlayerClient({ baseUrl: 'http://shim.test', fetch: withoutKey.fetch });
    await anon.spaces();
    expect(
      (withoutKey.calls[0]?.init.headers as Record<string, string>).authorization,
    ).toBeUndefined();
  });
});
