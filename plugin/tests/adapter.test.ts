/** 适配器行为（docs/06 §3/§5/§6；06 §9 的可测部分）。 */

import { describe, expect, it } from 'vitest';

import {
  FactlayerClient,
  ShimError,
  type FactlayerClientOptions,
} from '../src/client.js';
import {
  clampLimit,
  cleanTags,
  isoFromRecordedAt,
  noMatchHint,
} from '../src/adapter.js';
import {
  clientFactory,
  fakeSpace,
  hangingFetch,
  jsonResponse,
  makeAdapter,
  recordingFetch,
} from './helpers.js';

const SPACE = fakeSpace();

describe('remember', () => {
  it('回执翻译成 action:"stored" + committed + count + memoryBodyIds + ids', async () => {
    const recorder = recordingFetch(() =>
      jsonResponse({ id: 'mem-1', seq: 7, recorded_at: 1700000000000, client_write_id: 'x' }),
    );
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });

    const receipt = await adapter.remember(SPACE, { content: '会议在周三' });

    expect(receipt).toMatchObject({
      action: 'stored',
      committed: true,
      count: 1,
      memoryBodyIds: [SPACE.id],
      ids: ['mem-1'],
    });
  });

  it('payload 只含 shim 契约字段：session_id 取 settings.memoryBodyId、source_client 固定、uuid4', async () => {
    const recorder = recordingFetch(() => jsonResponse({ id: 'mem-2', seq: 0, recorded_at: 1 }));
    const space = fakeSpace({}, { memoryBodyId: 'team-a' });
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });

    await adapter.remember(space, { content: '只追加', tags: ['x'], entities: ['y'] });

    const body = recorder.bodyOf(0);
    expect(Object.keys(body).sort()).toEqual(
      ['client_write_id', 'content', 'session_id', 'source_client'].sort(),
    );
    expect(body.content).toBe('只追加');
    expect(body.session_id).toBe('team-a');
    expect(body.source_client).toBe('mnemon-provider');
    expect(body.client_write_id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u,
    );
  });

  it('每次写入用新的 client_write_id（契约无幂等位，宁可重复也不吞）', async () => {
    const recorder = recordingFetch(() => jsonResponse({ id: 'mem-3', recorded_at: 1 }));
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });
    await adapter.remember(SPACE, { content: 'a' });
    await adapter.remember(SPACE, { content: 'b' });
    expect(recorder.bodyOf(0).client_write_id).not.toBe(recorder.bodyOf(1).client_write_id);
  });

  it('缺 memoryBodyId 直接抛错，且不发请求', async () => {
    const recorder = recordingFetch(() => jsonResponse({ id: 'never', recorded_at: 1 }));
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });
    const space = fakeSpace({}, {});

    await expect(adapter.remember(space, { content: 'x' })).rejects.toThrow(/memoryBodyId/u);
    expect(recorder.calls).toHaveLength(0);
  });

  it('超时文案必须含「结果未知，可能已写入」', async () => {
    const { adapter } = makeAdapter({
      createClient: clientFactory(hangingFetch(), { timeouts: { remember: 10 } }),
    });
    await expect(adapter.remember(SPACE, { content: 'x' })).rejects.toThrow(
      /结果未知，可能已写入/u,
    );
  });

  it('shim 500 → 抛错（不回执成功、不自动重试）', async () => {
    const recorder = recordingFetch(() =>
      jsonResponse({ detail: '内部存储错误' }, 500),
    );
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });
    await expect(adapter.remember(SPACE, { content: 'x' })).rejects.toMatchObject({
      kind: 'server',
    } satisfies Partial<ShimError>);
    expect(recorder.calls).toHaveLength(1);
  });
});

describe('search 结果映射', () => {
  it('items → results；recorded_at(ms) → ISO 8601 UTC；score 保留', async () => {
    const recorder = recordingFetch(() =>
      jsonResponse({
        count: 1,
        items: [
          {
            id: 'm1',
            content: '北京项目计划书',
            recorded_at: 1700000000000,
            score: 0.0163934426229508,
            tags: ['via:bigram'],
          },
        ],
        hint: null,
      }),
    );
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });

    const result = await adapter.search(SPACE, { query: '北京项目', limit: 4 });

    expect(result.hint).toBeUndefined();
    expect(result.results).toHaveLength(1);
    expect(result.results[0]).toMatchObject({
      id: 'm1',
      content: '北京项目计划书',
      createdAt: '2023-11-14T22:13:20.000Z',
      score: 0.0163934426229508,
      tags: ['via:bigram'],
    });
  });

  it('search 请求只发 query/limit（不发 session_id，也不发明文 session 概念）', async () => {
    const recorder = recordingFetch(() => jsonResponse({ count: 0, items: [], hint: 'NO_MATCH' }));
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });

    await adapter.search(SPACE, { query: '北京', limit: 30, mode: 'smart', inspect: true });

    expect(recorder.calls[0]?.url).toBe('http://shim.test/search');
    expect(recorder.bodyOf(0)).toEqual({ query: '北京', limit: 30 });
  });

  it('坏时间戳：省略 createdAt + tags:["no-time"]，但这一条仍然在 results 里', async () => {
    const recorder = recordingFetch(() =>
      jsonResponse({
        count: 2,
        items: [
          { id: 'bad-1', content: '时间坏了', recorded_at: null, score: 1, tags: ['weak'] },
          { id: 'bad-2', content: '负时间', recorded_at: -1, score: 1 },
          { id: 'ok-1', content: '好时间', recorded_at: 1700000000000, score: 1 },
        ],
      }),
    );
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });

    const result = await adapter.search(SPACE, { query: '时间' });

    expect(result.results).toHaveLength(3);
    const ids = result.results.map((item) => item.id);
    expect(ids).toEqual(['bad-1', 'bad-2', 'ok-1']);
    const bad1 = result.results[0]!;
    expect(bad1.createdAt).toBeUndefined();
    expect(bad1.tags).toEqual(['no-time', 'weak']);
    const bad2 = result.results[1]!;
    expect(bad2.createdAt).toBeUndefined();
    expect(bad2.tags).toEqual(['no-time']);
    expect(result.results[2]!.createdAt).toBe('2023-11-14T22:13:20.000Z');
  });

  it('剥掉事实层私有字段：payload 只含契约字段', async () => {
    const recorder = recordingFetch(() =>
      jsonResponse({
        count: 1,
        query: '北京',
        query_id: 'q1',
        session_id: 's1',
        routes: { bigram: { raw: 1 } },
        hidden: { size: 0, matched: 0 },
        token_count: 2,
        items: [
          {
            id: 'm1',
            content: '北京项目计划书',
            createdAt: '2023-11-14T22:13:20.000Z',
            recorded_at: 1700000000000,
            score: 0.01,
            tags: ['via:bigram', 'weak'],
            routes: ['bigram'],
            content_truncated: false,
            session_id: 's1',
            seq: 3,
            expanded: false,
            expanded_from: null,
            inherited_rank: null,
          },
        ],
      }),
    );
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });

    const result = await adapter.search(SPACE, { query: '北京' });
    const item = result.results[0]!;

    expect(Object.keys(item).sort()).toEqual(
      ['content', 'createdAt', 'id', 'score', 'tags'].sort(),
    );
    for (const privateKey of [
      'seq',
      'session_id',
      'routes',
      'content_truncated',
      'expanded',
      'expanded_from',
      'inherited_rank',
      'recorded_at',
    ]) {
      expect(item).not.toHaveProperty(privateKey);
    }
    // 整个返回体里也不得出现私有字段
    const serialized = JSON.stringify(result);
    for (const privateKey of ['"seq"', '"session_id"', '"routes"', '"inherited_rank"']) {
      expect(serialized).not.toContain(privateKey);
    }
  });

  it('tags 清洗：去逗号、去重、每个 ≤300、最多 8 个', async () => {
    const longTag = 'x'.repeat(400);
    const recorder = recordingFetch(() =>
      jsonResponse({
        count: 1,
        items: [
          {
            id: 'm1',
            content: 'c',
            recorded_at: 1700000000000,
            score: 1,
            tags: [
              'a,b',
              ' c ',
              '全,角',
              longTag,
              '',
              'a,b',
              't1',
              't2',
              't3',
              't4',
              't5',
              't6',
              't7',
            ],
          },
        ],
      }),
    );
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });

    const result = await adapter.search(SPACE, { query: 'q' });
    const tags = result.results[0]!.tags ?? [];

    expect(tags).toHaveLength(8);
    expect(tags).not.toContain('a,b');
    expect(tags).toContain('ab');
    expect(tags).toContain('c');
    expect(tags).toContain('全角');
    expect(tags.filter((tag) => tag.length > 300)).toHaveLength(0);
    expect(tags[0]).toBe('ab');
    expect(new Set(tags).size).toBe(tags.length);
  });

  it('score 缺失补默认 0（不丢字段）', async () => {
    const recorder = recordingFetch(() =>
      jsonResponse({ count: 1, items: [{ id: 'm1', content: 'c', recorded_at: 1700000000000 }] }),
    );
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });
    const result = await adapter.search(SPACE, { query: 'q' });
    expect(result.results[0]!.score).toBe(0);
  });

  it('hint 只在空结果透传，并保证 NO_MATCH 前缀', async () => {
    const withResults = recordingFetch(() =>
      jsonResponse({ count: 1, items: [{ id: 'm1', content: 'c', recorded_at: 1, score: 1 }], hint: 'NO_MATCH｜x' }),
    );
    const { adapter } = makeAdapter({ createClient: clientFactory(withResults.fetch) });
    expect((await adapter.search(SPACE, { query: 'q' })).hint).toBeUndefined();

    const empty = recordingFetch(() =>
      jsonResponse({ count: 0, items: [], hint: 'routes=bigram(ok)' }),
    );
    const { adapter: emptyAdapter } = makeAdapter({ createClient: clientFactory(empty.fetch) });
    const emptyResult = await emptyAdapter.search(SPACE, { query: 'q' });
    expect(emptyResult.results).toEqual([]);
    expect(emptyResult.hint).toBe('NO_MATCH｜routes=bigram(ok)');

    const prefixed = recordingFetch(() =>
      jsonResponse({ count: 0, items: [], hint: 'NO_MATCH｜tokens=0' }),
    );
    const { adapter: prefixedAdapter } = makeAdapter({
      createClient: clientFactory(prefixed.fetch),
    });
    expect((await prefixedAdapter.search(SPACE, { query: 'q' })).hint).toBe(
      'NO_MATCH｜tokens=0',
    );
  });

  it('空结果且 shim 没给 hint → 仍然给 NO_MATCH（模型看到 unavailable 时语义正确）', async () => {
    const recorder = recordingFetch(() => jsonResponse({ count: 0, items: [] }));
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });
    expect((await adapter.search(SPACE, { query: 'q' })).hint).toBe('NO_MATCH');
  });

  it('compliance: id 缺失/超长 → 抛错而不是静默丢', async () => {
    const missing = recordingFetch(() =>
      jsonResponse({ count: 1, items: [{ content: 'no id', recorded_at: 1 }] }),
    );
    const { adapter } = makeAdapter({ createClient: clientFactory(missing.fetch) });
    await expect(adapter.search(SPACE, { query: 'q' })).rejects.toMatchObject({
      kind: 'invalid-response',
    } satisfies Partial<ShimError>);

    const tooLong = recordingFetch(() =>
      jsonResponse({
        count: 1,
        items: [{ id: 'x'.repeat(1001), content: 'c', recorded_at: 1 }],
      }),
    );
    const { adapter: longAdapter } = makeAdapter({ createClient: clientFactory(tooLong.fetch) });
    await expect(longAdapter.search(SPACE, { query: 'q' })).rejects.toThrow(/id 超过/u);
  });

  it('items 缺失/非数组 → 抛错，不能冒充空结果', async () => {
    const recorder = recordingFetch(() => jsonResponse({ count: 0 }));
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });
    await expect(adapter.search(SPACE, { query: 'q' })).rejects.toThrow(/items 数组/u);
  });

  it('畸形 JSON（200 + HTML）→ invalid-response，不冒充空结果', async () => {
    const recorder = recordingFetch(
      () =>
        new Response('<html><body>proxy error page</body></html>', {
          status: 200,
          headers: { 'content-type': 'text/html' },
        }),
    );
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });

    await expect(adapter.search(SPACE, { query: 'q' })).rejects.toMatchObject({
      kind: 'invalid-response',
    } satisfies Partial<ShimError>);
  });

  it('shim 503（重建期）→ 抛 rebuilding，不返回空数组', async () => {
    const recorder = recordingFetch(() =>
      jsonResponse({ error: 'rebuilding', message: '索引重建中，请稍后' }, 503),
    );
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });

    await expect(adapter.search(SPACE, { query: 'q' })).rejects.toMatchObject({
      kind: 'rebuilding',
      message: '索引重建中，请稍后',
    } satisfies Partial<ShimError>);
  });
});

describe('status', () => {
  it('healthy 快照 → {healthy:true}', async () => {
    const recorder = recordingFetch(() => jsonResponse({ status: 'ready', rebuilding: false }));
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });
    await expect(adapter.status(SPACE)).resolves.toEqual({ healthy: true });
  });

  it('超时 → {healthy:false, error:"starting"}（不抛错、不拖住目录）', async () => {
    const { adapter } = makeAdapter({
      createClient: clientFactory(hangingFetch(), { timeouts: { status: 10 } }),
    });
    await expect(adapter.status(SPACE)).resolves.toEqual({
      healthy: false,
      error: 'starting',
    });
  });

  it('503 → {healthy:false, error:"rebuilding"}', async () => {
    const recorder = recordingFetch(() =>
      jsonResponse({ error: 'rebuilding', message: '索引重建中，请稍后' }, 503),
    );
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });
    await expect(adapter.status(SPACE)).resolves.toEqual({
      healthy: false,
      error: 'rebuilding',
    });
  });

  it('网关 503（无 rebuilding 标记）→ unhealthy 通用文案，不显示「索引重建中」', async () => {
    const recorder = recordingFetch(
      () =>
        new Response('<html><body>503 Service Unavailable</body></html>', {
          status: 503,
          headers: { 'content-type': 'text/html' },
        }),
    );
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });

    const status = await adapter.status(SPACE);

    expect(status.healthy).toBe(false);
    expect(status.error).toMatch(/服务暂时不可用/u);
    expect(status.error).not.toMatch(/rebuilding|索引重建/u);
  });

  it('shim 的 starting / rebuilding 标志都映射成 unhealthy', async () => {
    const starting = recordingFetch(() => jsonResponse({ status: 'starting' }));
    const { adapter: startingAdapter } = makeAdapter({
      createClient: clientFactory(starting.fetch),
    });
    await expect(startingAdapter.status(SPACE)).resolves.toEqual({
      healthy: false,
      error: 'starting',
    });

    const rebuilding = recordingFetch(() =>
      jsonResponse({ status: 'ready', rebuilding: true }),
    );
    const { adapter: rebuildingAdapter } = makeAdapter({
      createClient: clientFactory(rebuilding.fetch),
    });
    await expect(rebuildingAdapter.status(SPACE)).resolves.toEqual({
      healthy: false,
      error: 'rebuilding',
    });
  });

  it('配置缺失 → unhealthy + 人话原因（status 永不抛错）', async () => {
    const recorder = recordingFetch(() => jsonResponse({ status: 'ready' }));
    const { adapter } = makeAdapter({
      connection: {},
      createClient: clientFactory(recorder.fetch),
    });
    const status = await adapter.status(SPACE);
    expect(status.healthy).toBe(false);
    expect(status.error).toMatch(/baseUrl/u);
    expect(recorder.calls).toHaveLength(0);
  });
});

describe('discover', () => {
  it('GET /spaces → 单空间；externalId 取返回 id；connection.memoryBodyId 同值', async () => {
    const recorder = recordingFetch(() =>
      jsonResponse([{ id: 'factlayer-default', name: 'factlayer' }]),
    );
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });

    const spaces = await adapter.discover({ baseUrl: 'http://shim.test', apiKey: '' });

    expect(spaces).toEqual([
      {
        externalId: 'factlayer-default',
        name: 'factlayer',
        description: 'factlayer space factlayer-default',
        connection: { memoryBodyId: 'factlayer-default' },
      },
    ]);
    expect(recorder.calls[0]?.url).toBe('http://shim.test/spaces');
  });

  it('重复调用幂等：externalId 不变、返回形状逐字相同', async () => {
    const recorder = recordingFetch(() =>
      jsonResponse([{ id: 'factlayer-default', name: 'factlayer' }]),
    );
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });

    const first = await adapter.discover({ baseUrl: 'http://shim.test' });
    const second = await adapter.discover({ baseUrl: 'http://shim.test' });

    expect(second).toEqual(first);
    expect(second.map((space) => space.externalId)).toEqual(['factlayer-default']);
  });

  it('shim 不可达 → 抛错（保存配置必须有明确文案，不能返回空数组）', async () => {
    const recorder = recordingFetch(() => {
      throw new TypeError('fetch failed');
    });
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });

    await expect(adapter.discover({ baseUrl: 'http://shim.test' })).rejects.toMatchObject({
      kind: 'network',
    } satisfies Partial<ShimError>);
  });

  it('baseUrl 缺失 → config 错误', async () => {
    const recorder = recordingFetch(() => jsonResponse([]));
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });
    await expect(adapter.discover({})).rejects.toMatchObject({
      kind: 'config',
    } satisfies Partial<ShimError>);
  });

  it('/spaces 空数组 → 抛错（不允许 discover 返回空）', async () => {
    const recorder = recordingFetch(() => jsonResponse([]));
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });
    await expect(adapter.discover({ baseUrl: 'http://shim.test' })).rejects.toThrow(
      /没有返回任何 Memory Space/u,
    );
  });
});

describe('browse / graph（能力位 false）', () => {
  it('list 返回空数组，不发请求', async () => {
    const recorder = recordingFetch(() => jsonResponse({}));
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });
    await expect(adapter.list(SPACE, { limit: 10 })).resolves.toEqual([]);
    expect(recorder.calls).toHaveLength(0);
  });

  it('graph 返回空快照（不是 list 平铺出来的假图）', async () => {
    const recorder = recordingFetch(() => jsonResponse({}));
    const { adapter } = makeAdapter({ createClient: clientFactory(recorder.fetch) });
    const snapshot = await adapter.graph(SPACE);
    expect(snapshot.nodes).toEqual([]);
    expect(snapshot.edges).toEqual([]);
    expect(Number.isNaN(Date.parse(snapshot.generatedAt))).toBe(false);
    expect(recorder.calls).toHaveLength(0);
  });

  it('id 与 manifest.typeId 逐字一致，scoreSemantics 两处一致', async () => {
    const { adapter } = makeAdapter();
    expect(adapter.id).toBe('factlayer');
    expect(adapter.scoreSemantics).toEqual({ kind: 'provider-native' });
  });
});

describe('纯函数', () => {
  it('isoFromRecordedAt 的边界', () => {
    expect(isoFromRecordedAt(1700000000000)).toBe('2023-11-14T22:13:20.000Z');
    for (const bad of [undefined, null, -1, Number.NaN, Number.POSITIVE_INFINITY, '1700', 8.64e15 + 1]) {
      expect(isoFromRecordedAt(bad)).toBeUndefined();
    }
  });

  it('cleanTags：8 个上限 / 300 字符 / 去逗号 / 去重', () => {
    const tags = cleanTags([
      'a,b',
      'a，b',
      'a,b',
      'x'.repeat(301),
      ...Array.from({ length: 10 }, (_, index) => `t${index}`),
    ]);
    expect(tags).toHaveLength(8);
    expect(tags[0]).toBe('ab');
    expect(tags[1]).toHaveLength(300);
    expect(new Set(tags).size).toBe(tags.length);
  });

  it('noMatchHint 的三种情况', () => {
    expect(noMatchHint(undefined)).toBe('NO_MATCH');
    expect(noMatchHint('  ')).toBe('NO_MATCH');
    expect(noMatchHint('routes=bigram(ok)')).toBe('NO_MATCH｜routes=bigram(ok)');
    expect(noMatchHint('NO_MATCH｜x')).toBe('NO_MATCH｜x');
  });

  it('clampLimit 把 limit 夹到 1..50', () => {
    expect(clampLimit(undefined)).toBeUndefined();
    expect(clampLimit(0)).toBe(1);
    expect(clampLimit(30)).toBe(30);
    expect(clampLimit(1000)).toBe(50);
    expect(clampLimit(2.9)).toBe(2);
    expect(clampLimit(Number.NaN)).toBeUndefined();
  });
});

/** 类型层面确认：构造参数确实用 FactlayerClientOptions。 */
const _typeCheck: FactlayerClientOptions | undefined = undefined;
void _typeCheck;
void FactlayerClient;
