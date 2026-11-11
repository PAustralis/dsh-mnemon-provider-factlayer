/**
 * `MemoryProviderAdapter` 实现（docs/06 §3/§5/§6，docs/05 §2.1）。
 *
 * 这一层只做「翻译」：每个方法 = 一次 shim HTTP 调用 + 契约要求的最小形状补齐。
 * 业务逻辑（检索、派生、作废）都在 Python 侧，这里不复制。
 *
 * 三个必须记住的点：
 * 1. `search` 的 `{items}` 要改名成 `{results}`，并剥掉事实层私有字段
 *    （`seq`/`session_id`/`routes`/`content_truncated`/`expanded*`/`inherited_rank`）。
 *    `recorded_at`(ms) → ISO 8601；坏值**不丢这一条**，只省略 `createdAt`
 *    并加 `tags:["no-time"]`。
 * 2. `remember` 的回执必须是 `action:"stored"` + `committed:true` + 计数 > 0，
 *    否则 Source 判成 `unknown`、空间不激活（06 §5）。
 * 3. `hint` 只在**空结果**时透传，且必须以 `NO_MATCH` 开头（06 §6、03 §4.1）。
 */

import { FactlayerClient, ShimError } from './client.js';
import { ConnectionCache, type FactlayerConnection } from './config.js';
import { PROVIDER_ID } from './manifest.js';
import type {
  Insight,
  JsonValue,
  MemoryGraphSnapshot,
  MemoryListRequest,
  MemoryProviderAdapter,
  MemoryProviderConnection,
  MemorySpace,
  ProviderSearchResult,
  ProviderSpaceStatus,
  ProviderMemorySpace,
  RememberRequest,
  SearchRequest,
} from './contract.js';

export const MAX_INSIGHT_ID_LENGTH = 1000;
export const MAX_TAGS = 8;
export const MAX_TAG_LENGTH = 300;
export const MAX_ENTITIES = 50;
export const MAX_EXTERNAL_ID_LENGTH = 2000;
const MAX_DATE_MS = 8.64e15;

export interface FactlayerAdapterOptions {
  connections: ConnectionCache;
  /** 测试注入点：给定连接造客户端（默认 new FactlayerClient）。 */
  createClient?: (connection: FactlayerConnection) => FactlayerClient;
  now?: () => number;
}

export class FactlayerAdapter implements MemoryProviderAdapter {
  /** 必须与 `manifest.typeId` / 子模块 `id` 逐字一致（PJ:259）。 */
  readonly id = PROVIDER_ID;

  /** 与 manifest 的 `scoreSemantics` 一致；RRF 分不是 0..1 校准分。 */
  readonly scoreSemantics = { kind: 'provider-native' } as const;

  private readonly connections: ConnectionCache;
  private readonly createClient: ((connection: FactlayerConnection) => FactlayerClient) | undefined;
  private readonly now: (() => number) | undefined;
  private readonly clients = new Map<string, FactlayerClient>();

  constructor(options: FactlayerAdapterOptions) {
    this.connections = options.connections;
    this.createClient = options.createClient;
    this.now = options.now;
  }

  // ---------------------------------------------------------------- discover
  /**
   * `GET /spaces` → 单空间。`externalId` 取 shim 返回的 **id**（不是 memory 字段），
   * `connection.memoryBodyId` 写同一个值：remember 的 `session_id` 从那里读，
   * 因此两台机器天然一致（06 §3 阻塞项 2）。同值重复调用幂等。
   */
  async discover(connection: MemoryProviderConnection): Promise<ProviderMemorySpace[]> {
    const values = this.connectionFromValues(connection);
    const client = this.clientFor(values);
    const payload = await client.spaces();
    if (!Array.isArray(payload)) {
      throw new ShimError(
        'invalid-response',
        'factlayer /spaces 返回的不是数组（期望 [{"id","name"}]）',
      );
    }
    const spaces: ProviderMemorySpace[] = [];
    const seen = new Set<string>();
    for (const entry of payload) {
      const record = asRecord(entry);
      if (record === undefined) {
        throw new ShimError('invalid-response', 'factlayer /spaces 的元素不是对象');
      }
      const externalId = (asString(record.id) ?? '').trim();
      if (externalId === '') {
        throw new ShimError('invalid-response', 'factlayer /spaces 返回了空 id');
      }
      if (externalId.length > MAX_EXTERNAL_ID_LENGTH) {
        throw new ShimError(
          'invalid-response',
          `factlayer /spaces 的 id 超过 ${MAX_EXTERNAL_ID_LENGTH} 字符`,
        );
      }
      if (seen.has(externalId)) {
        throw new ShimError(
          'invalid-response',
          `factlayer /spaces 返回了重复 id：${externalId}`,
        );
      }
      seen.add(externalId);
      const name = nonEmpty(asString(record.name)) ?? 'factlayer';
      spaces.push({
        externalId,
        name,
        description: `factlayer space ${externalId}`,
        connection: { memoryBodyId: externalId },
      });
    }
    if (spaces.length === 0) {
      throw new ShimError(
        'invalid-response',
        'factlayer /spaces 没有返回任何 Memory Space（shim 不可用时 discover 必须抛错，不能返回空数组）',
      );
    }
    return spaces;
  }

  // ---------------------------------------------------------------- status
  /**
   * `GET /health` → `ProviderSpaceStatus`。
   * 超时 → `{healthy:false, error:"starting"}`；只有 shim 明确返回
   * `{"error":"rebuilding"}` 的 503 才映射成 `"rebuilding"`，网关的普通 503
   * 走 `statusError` 的通用「服务暂时不可用」文案（第三层审查 #1）。
   * 配置缺失/网络错误 → unhealthy + 人话原因（`status` 永不抛错，否则空间永远显示异常）。
   */
  async status(body: MemorySpace): Promise<ProviderSpaceStatus> {
    let client: FactlayerClient;
    try {
      client = this.clientFor(this.connections.get(body.id));
    } catch (error) {
      return { healthy: false, error: errorMessage(error) };
    }
    try {
      const payload = await client.health();
      const record = asRecord(payload);
      if (record === undefined) {
        return { healthy: false, error: 'factlayer /health 返回的不是对象' };
      }
      if (record.status === 'starting') return { healthy: false, error: 'starting' };
      if (record.rebuilding === true) return { healthy: false, error: 'rebuilding' };
      return { healthy: true };
    } catch (error) {
      return { healthy: false, error: statusError(error) };
    }
  }

  // ---------------------------------------------------------------- search
  async search(
    body: MemorySpace,
    request: SearchRequest,
    signal?: AbortSignal,
  ): Promise<ProviderSearchResult> {
    const client = this.clientFor(this.connections.get(body.id));
    const payload = await client.search(
      { query: request.query, limit: clampLimit(request.limit) },
      signal,
    );
    const record = asRecord(payload);
    if (record === undefined) {
      throw new ShimError('invalid-response', 'factlayer /search 返回的不是对象');
    }
    if (!Array.isArray(record.items)) {
      throw new ShimError(
        'invalid-response',
        'factlayer /search 缺少 items 数组（不能当成空结果）',
      );
    }
    const results = record.items.map((item) => toInsight(item));
    if (results.length === 0) {
      // hint 只在空结果时有意义；多空间拼接时每个空间各自带 NO_MATCH 前缀。
      return { results: [], hint: noMatchHint(record.hint) };
    }
    return { results };
  }

  // ---------------------------------------------------------------- remember
  /**
   * `POST /memories`。三个必填字段：
   * - `session_id` = `body.provider.settings.memoryBodyId`（**缺值直接抛错**，
   *   不得用 body.id 或默认值静默兜底）；
   * - `source_client` = `"mnemon-provider"`；
   * - `client_write_id` = 每次新的 uuid4（契约无幂等位，宁可重复也不吞）。
   */
  async remember(
    body: MemorySpace,
    request: RememberRequest,
    signal?: AbortSignal,
  ): Promise<JsonValue> {
    const sessionId = memoryBodyIdOf(body);
    const client = this.clientFor(this.connections.get(body.id));
    let payload: unknown;
    try {
      payload = await client.remember(
        {
          content: request.content,
          sessionId,
          clientWriteId: newWriteId(),
        },
        signal,
      );
    } catch (error) {
      if (error instanceof ShimError && error.kind === 'timeout') {
        // shim 可能已落盘：模型若当成「失败」会重写产生重复（06 §5）。
        throw new ShimError(
          'timeout',
          `factlayer 写入超时（结果未知，可能已写入）：${error.message}`,
          { cause: error },
        );
      }
      throw error;
    }
    const record = asRecord(payload);
    const id = nonEmpty(asString(record?.id)) ?? '';
    // 回执命中 16 词白名单 + committed:true + count>0；三个都给，避免被上游判 unknown。
    return {
      action: 'stored',
      committed: true,
      count: 1,
      memoryBodyIds: [body.id],
      ids: id === '' ? [] : [id],
    };
  }

  // ---------------------------------------------------------------- browse
  /** `browse:false`：绝不伪造浏览能力（06 §3）。 */
  async list(_body: MemorySpace, _request: MemoryListRequest): Promise<Insight[]> {
    return [];
  }

  /** `graph:false`：返回空快照，不产出一张没有边的假图。 */
  async graph(_body: MemorySpace): Promise<MemoryGraphSnapshot> {
    return { nodes: [], edges: [], generatedAt: new Date().toISOString() };
  }

  // ---------------------------------------------------------------- 生命周期
  /** 用户点「重新连接」：清配置缓存 + status 快照（否则改了 key 还在用旧值）。 */
  invalidateStatus(memoryBodyId?: string): void {
    this.connections.invalidate(memoryBodyId);
    for (const client of this.clients.values()) client.invalidateStatus();
  }

  dispose(): void {
    this.connections.dispose();
    this.clients.clear();
  }

  // ---------------------------------------------------------------- 内部
  private clientFor(connection: FactlayerConnection): FactlayerClient {
    const key = `${connection.baseUrl}\u0000${connection.apiKey}`;
    const existing = this.clients.get(key);
    if (existing !== undefined) return existing;
    const client =
      this.createClient !== undefined
        ? this.createClient(connection)
        : new FactlayerClient({
            baseUrl: connection.baseUrl,
            apiKey: connection.apiKey,
            ...(this.now === undefined ? {} : { now: this.now }),
          });
    this.clients.set(key, client);
    return client;
  }

  /** discover 拿到的是 service 级连接（还没有空间），直接用它造客户端。 */
  private connectionFromValues(connection: MemoryProviderConnection): FactlayerConnection {
    const baseUrl = asConnectionString(connection.baseUrl).trim().replace(/\/+$/u, '');
    if (baseUrl === '') {
      throw new ShimError(
        'config',
        'factlayer baseUrl 未配置：请在 dsh-mnemon 的 factlayer 服务配置里填写 shim 地址（如 http://127.0.0.1:8000）',
      );
    }
    return { baseUrl, apiKey: asConnectionString(connection.apiKey).trim() };
  }
}

// ---------------------------------------------------------------- 映射

/** `items[]` → `Insight`（**白名单**：只放契约字段）。 */
export function toInsight(value: unknown): Insight {
  const record = asRecord(value);
  if (record === undefined) {
    throw new ShimError('invalid-response', 'factlayer /search 的 item 不是对象');
  }
  const id = (asString(record.id) ?? '').trim();
  if (id === '') {
    throw new ShimError(
      'invalid-response',
      'factlayer /search 返回了空 id（契约要求 id 非空且 ≤1000 字符）',
    );
  }
  if (id.length > MAX_INSIGHT_ID_LENGTH) {
    throw new ShimError(
      'invalid-response',
      `factlayer /search 返回的 id 超过 ${MAX_INSIGHT_ID_LENGTH} 字符`,
    );
  }
  const content = asString(record.content);
  if (content === undefined) {
    throw new ShimError(
      'invalid-response',
      `factlayer /search 返回的 ${id} 缺少 content（不能静默丢）`,
    );
  }

  const insight: Insight = { id, content };
  const createdAt = isoFromRecordedAt(record.recorded_at);
  let tags = cleanTags(record.tags);
  if (createdAt === undefined) {
    // 坏时间戳：只退化成「没有 createdAt」+ 标记，**这一条仍然返回**。
    tags = ['no-time', ...tags].slice(0, MAX_TAGS);
  } else {
    insight.createdAt = createdAt;
  }
  if (tags.length > 0) insight.tags = tags;

  // shim 未给 score 时补默认值：缺 score → tier unknown → 策略每回合只收 1 条。
  insight.score = asFiniteNumber(record.score) ?? 0;

  const category = nonEmpty(asString(record.category));
  if (category !== undefined) insight.category = category;
  const importance = asFiniteNumber(record.importance);
  if (importance !== undefined) insight.importance = importance;
  if (Array.isArray(record.entities)) {
    const entities = cleanTextList(record.entities, MAX_ENTITIES);
    if (entities.length > 0) insight.entities = entities;
  }
  const source = nonEmpty(asString(record.source));
  if (source !== undefined) insight.source = source;
  return insight;
}

/** `recorded_at`(ms) → ISO 8601 UTC；None/负数/NaN/超范围 → undefined。 */
export function isoFromRecordedAt(value: unknown): string | undefined {
  const ms = asFiniteNumber(value);
  if (ms === undefined || ms < 0 || ms > MAX_DATE_MS) return undefined;
  const date = new Date(ms);
  const time = date.getTime();
  if (Number.isNaN(time)) return undefined;
  return date.toISOString();
}

/**
 * tags 清洗：去英文/全角逗号、trim、去重、每个 ≤300、最多 8 个
 * （docs/03 §4.4：读侧也一律不用逗号，避免与写入侧校验冲突）。
 */
export function cleanTags(value: unknown): string[] {
  return cleanTextList(value, MAX_TAGS);
}

function cleanTextList(value: unknown, maxItems: number): string[] {
  if (!Array.isArray(value)) return [];
  const out: string[] = [];
  for (const entry of value) {
    if (typeof entry !== 'string') continue;
    const text = entry.replace(/[,，]/gu, '').trim();
    if (text === '') continue;
    const clipped = text.length > MAX_TAG_LENGTH ? text.slice(0, MAX_TAG_LENGTH) : text;
    if (!out.includes(clipped)) out.push(clipped);
    if (out.length >= maxItems) break;
  }
  return out;
}

/** 空结果的 hint：保证 `NO_MATCH` 前缀（模型看到的是 `unavailable` 键）。 */
export function noMatchHint(value: unknown): string {
  const hint = (asString(value) ?? '').trim();
  if (hint === '') return 'NO_MATCH';
  return hint.startsWith('NO_MATCH') ? hint : `NO_MATCH｜${hint}`;
}

/** 契约 request.limit 是候选量，上限 50（shim 的 SearchIn 也只收 1..50）。 */
export function clampLimit(value: number | undefined): number | undefined {
  if (value === undefined || !Number.isFinite(value)) return undefined;
  return Math.min(50, Math.max(1, Math.trunc(value)));
}

/** `session_id` 的**唯一**来源：memory 作用域的非密字段。缺值抛错，不兜底。 */
export function memoryBodyIdOf(body: MemorySpace): string {
  const settings = body.provider.settings ?? {};
  const raw = settings.memoryBodyId;
  const value = typeof raw === 'string' ? raw.trim() : '';
  if (value === '') {
    throw new Error(
      'factlayer: body.provider.settings.memoryBodyId 缺失。请在 factlayer Memory Space 的设置里填写固定空间标识（discover 会自动写入 FACTLAYER_SPACE_ID 的值）。',
    );
  }
  return value;
}

/** every remember 一个新的 uuid4（契约没有幂等位，docs/05 §4.5）。 */
export function newWriteId(): string {
  const cryptoApi = globalThis.crypto as Crypto | undefined;
  if (cryptoApi !== undefined && typeof cryptoApi.randomUUID === 'function') {
    return cryptoApi.randomUUID();
  }
  // 极老运行时的退化路径（Node >= 19 一定有 randomUUID）。
  const hex = () => Math.trunc(Math.random() * 0x10000).toString(16).padStart(4, '0');
  return `${hex()}${hex()}-${hex()}-4${hex().slice(1)}-a${hex().slice(1)}-${hex()}${hex()}${hex()}`;
}

// ---------------------------------------------------------------- 小工具

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function asFiniteNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function asConnectionString(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return '';
}

function nonEmpty(value: string | undefined): string | undefined {
  if (value === undefined) return undefined;
  const trimmed = value.trim();
  return trimmed === '' ? undefined : trimmed;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

function statusError(error: unknown): string {
  if (error instanceof ShimError) {
    if (error.kind === 'timeout') return 'starting';
    if (error.kind === 'rebuilding') return 'rebuilding';
  }
  return errorMessage(error);
}
