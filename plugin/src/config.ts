/**
 * 连接配置解析（docs/06 §4，B3）。
 *
 * 契约**不把 secret 值交给** `body.provider.settings`（那里只有 memory 作用域的
 * 非密字段 + `configuredSecrets` 名单）。要拿 baseUrl/apiKey 必须走
 * `context.memorySpaces.providerConnection(spaceId, providerId)`——它自带 provider
 * 身份校验，并把 service + memory + 空间级覆盖合并后做完整归一化。
 *
 * 缓存**按 spaceId 分键**，`invalidateStatus` / `dispose` 时清掉：用户改了 key
 * 之后必须重新读取，不能继续用旧值。
 */

import { ShimError } from './client.js';
import type {
  MemoryProviderAdapterFactoryContext,
  MemoryProviderConnection,
  MemorySpaceAuthority,
} from './contract.js';

export interface FactlayerConnection {
  readonly baseUrl: string;
  readonly apiKey: string;
}

export class ConnectionCache {
  private readonly cache = new Map<string, FactlayerConnection>();

  constructor(
    private readonly context: MemoryProviderAdapterFactoryContext,
    /** `providerConnection(id, expectedProviderId)` 的第二参：本子模块的实例 id。 */
    private readonly providerId: string,
  ) {}

  get(spaceId: string): FactlayerConnection {
    const hit = this.cache.get(spaceId);
    if (hit !== undefined) return hit;

    const authority = this.authority();
    let raw: MemoryProviderConnection;
    try {
      raw = authority.providerConnection(spaceId, this.providerId);
    } catch (error) {
      throw new ShimError(
        'config',
        `factlayer 无法读取 Memory Space ${spaceId} 的连接配置：${errorMessage(error)}`,
        { cause: error },
      );
    }

    const baseUrl = asConnectionString(raw.baseUrl).trim().replace(/\/+$/u, '');
    if (baseUrl === '') {
      throw new ShimError(
        'config',
        'factlayer baseUrl 未配置：请在 dsh-mnemon 的 factlayer 服务配置里填写 shim 地址（如 http://127.0.0.1:8000）',
      );
    }
    if (!/^https?:\/\//iu.test(baseUrl)) {
      throw new ShimError(
        'config',
        `factlayer baseUrl 必须是 http(s) 地址（当前为 ${baseUrl}）`,
      );
    }

    const connection: FactlayerConnection = {
      baseUrl,
      apiKey: asConnectionString(raw.apiKey).trim(),
    };
    this.cache.set(spaceId, connection);
    return connection;
  }

  /** 传 spaceId 只清该空间；不传清全部（用户点了「重新连接」）。 */
  invalidate(spaceId?: string): void {
    if (spaceId === undefined) this.cache.clear();
    else this.cache.delete(spaceId);
  }

  dispose(): void {
    this.cache.clear();
  }

  private authority(): MemorySpaceAuthority {
    const authority = this.context.memorySpaces ?? this.context.memoryBodies;
    if (authority === undefined || typeof authority.providerConnection !== 'function') {
      throw new ShimError(
        'config',
        'factlayer 需要 dsh-mnemon 的 Memory Space authority（context.memorySpaces.providerConnection）',
      );
    }
    return authority;
  }
}

function asConnectionString(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return '';
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}
