/**
 * 测试辅助：假 fetch / 假 Memory Space / 假 authority。
 *
 * 不 mock 网络库：`FetchLike` 是客户端构造参数，测试直接注入一个读请求、写响应
 * 的函数——这样能断言「payload 只含契约字段」这类线上真实会发生的形状问题。
 */

import { FactlayerAdapter } from '../src/adapter.js';
import { FactlayerClient, type FetchLike } from '../src/client.js';
import type {
  MemoryProviderCapabilities,
  MemoryProviderConnection,
  MemorySpace,
  MemorySpaceAuthority,
  MemorySpaceProvider,
  MemorySpaceProviderRuntimeContext,
} from '../src/contract.js';
import { createFactlayerAdapter, type FactlayerModuleOptions } from '../src/module.js';
import { FACTLAYER_MANIFEST, PROVIDER_ID } from '../src/manifest.js';

export interface FetchCall {
  url: string;
  init: RequestInit;
}

export interface RecordingFetch {
  fetch: FetchLike;
  calls: FetchCall[];
  /** 解析第 n 次请求的 JSON body。 */
  bodyOf(index: number): Record<string, unknown>;
}

export function recordingFetch(
  handler: (call: FetchCall) => Response | Promise<Response>,
): RecordingFetch {
  const calls: FetchCall[] = [];
  return {
    calls,
    fetch: async (url, init) => {
      const call = { url, init: init ?? {} };
      calls.push(call);
      return handler(call);
    },
    bodyOf(index: number): Record<string, unknown> {
      const body = calls[index]?.init.body;
      if (typeof body !== 'string') return {};
      return JSON.parse(body) as Record<string, unknown>;
    },
  };
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** 一个真正会等 signal 的「永不返回」fetch：abort 时像真 fetch 一样 reject。 */
export function hangingFetch(): FetchLike {
  return (_url, init) =>
    new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => {
        reject(new Error('The operation was aborted'));
      });
    });
}

export const DEFAULT_CAPABILITIES: MemoryProviderCapabilities =
  FACTLAYER_MANIFEST.capabilities;

export function fakeProvider(
  settings: MemoryProviderConnection,
): MemorySpaceProvider {
  return {
    id: PROVIDER_ID,
    typeId: PROVIDER_ID,
    label: FACTLAYER_MANIFEST.label,
    kind: 'remote',
    location: 'http://127.0.0.1:8000',
    apiKeyConfigured: false,
    settings,
    configuredSecrets: [],
    capabilities: DEFAULT_CAPABILITIES,
  };
}

export function fakeSpace(
  overrides: Partial<MemorySpace> = {},
  settings: MemoryProviderConnection = { memoryBodyId: 'factlayer-default' },
): MemorySpace {
  return {
    id: 'factlayer-abc123',
    name: 'Factlayer',
    description: 'factlayer memory space',
    active: true,
    dbPath: '/data/factlayer-abc123',
    provider: fakeProvider(settings),
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  };
}

export interface FakeAuthority {
  authority: MemorySpaceAuthority;
  providerConnectionCalls: Array<{ id: string; expectedProviderId: string | undefined }>;
}

export function fakeAuthority(connection: MemoryProviderConnection): FakeAuthority {
  const providerConnectionCalls: FakeAuthority['providerConnectionCalls'] = [];
  return {
    providerConnectionCalls,
    authority: {
      runner: { effectiveDataDir: () => '/data' },
      list: () => [],
      providerConnection(id, expectedProviderId) {
        providerConnectionCalls.push({ id, expectedProviderId });
        return connection;
      },
    },
  };
}

export interface TestAdapter {
  /** createFactlayerAdapter 总是产出 FactlayerAdapter；用具体类让 discover 等可选方法可直接调用。 */
  adapter: FactlayerAdapter;
  authority: FakeAuthority;
}

export function makeAdapter(options: {
  connection?: MemoryProviderConnection;
  createClient?: FactlayerModuleOptions['createClient'];
  now?: () => number;
} = {}): TestAdapter {
  const fake = fakeAuthority(
    options.connection ?? { baseUrl: 'http://shim.test', apiKey: 'secret-key' },
  );
  const context: MemorySpaceProviderRuntimeContext = {
    memorySpaces: fake.authority,
    memoryBodies: fake.authority,
    config: { timeoutMs: 15_000 },
    providerInstanceId: PROVIDER_ID,
    manifest: FACTLAYER_MANIFEST,
  };
  const adapter = createFactlayerAdapter(context, {
    ...(options.createClient === undefined ? {} : { createClient: options.createClient }),
    ...(options.now === undefined ? {} : { now: options.now }),
  }) as FactlayerAdapter;
  return { adapter, authority: fake };
}

/** 用给定 fetch/时钟/超时造一个客户端（走生产用的 FactlayerAdapter 注入点）。 */
export function clientFactory(
  fetch: FetchLike,
  options: { now?: () => number; timeouts?: ConstructorParameters<typeof FactlayerClient>[0]['timeouts'] } = {},
) {
  return (connection: { baseUrl: string; apiKey: string }): FactlayerClient =>
    new FactlayerClient({
      baseUrl: connection.baseUrl,
      apiKey: connection.apiKey,
      fetch,
      ...(options.now === undefined ? {} : { now: options.now }),
      ...(options.timeouts === undefined ? {} : { timeouts: options.timeouts }),
    });
}
