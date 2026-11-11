/**
 * shim 的 HTTP 封装（docs/06 §3/§7）。
 *
 * 超时：`search` 15s、`remember` 15s、`status` 2.5s（对齐官方基类 15s；status
 * 短超时是为了不拖住 Memory 页的目录刷新）。`discover` 用 15s，宿主自身还有 30s
 * 的 rediscover 上限。
 *
 * `status` 做 **stale-while-revalidate 30s** 与**并发合并**：
 * - 有 30s 内的成功结果 → 直接返回，不发请求；
 * - 有超过 30s 的旧结果 → **立即返回旧值**并在后台重验（失败静默，保留旧值）；
 * - 无缓存 → 发起请求，并发调用共享同一个 in-flight Promise。
 *
 * 错误分类（`ShimError.kind`）：`config` / `network` / `timeout` / `rebuilding` /
 * `server` / `client` / `invalid-response`。适配器据此区分「明确没写成」与
 * 「结果未知，可能已写入」。
 */

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export type ShimErrorKind =
  | 'config'
  | 'network'
  | 'timeout'
  | 'rebuilding'
  | 'server'
  | 'client'
  | 'invalid-response';

export class ShimError extends Error {
  readonly kind: ShimErrorKind;
  readonly status: number | undefined;

  constructor(
    kind: ShimErrorKind,
    message: string,
    options?: { status?: number; cause?: unknown },
  ) {
    super(message, options?.cause === undefined ? undefined : { cause: options.cause });
    this.name = 'ShimError';
    this.kind = kind;
    this.status = options?.status;
  }
}

export const SEARCH_TIMEOUT_MS = 15_000;
export const REMEMBER_TIMEOUT_MS = 15_000;
export const STATUS_TIMEOUT_MS = 2_500;
export const SPACES_TIMEOUT_MS = 15_000;
/** status 的成功结果复用窗口（docs/06 §3 的 ~30s stale-while-revalidate）。 */
export const STATUS_STALE_MS = 30_000;

export type FactlayerTimeoutName = 'search' | 'remember' | 'status' | 'spaces';

const DEFAULT_TIMEOUTS: Record<FactlayerTimeoutName, number> = {
  search: SEARCH_TIMEOUT_MS,
  remember: REMEMBER_TIMEOUT_MS,
  status: STATUS_TIMEOUT_MS,
  spaces: SPACES_TIMEOUT_MS,
};

export interface FactlayerClientOptions {
  baseUrl: string;
  apiKey?: string;
  fetch?: FetchLike;
  /** 可注入时钟（测试用），默认 `Date.now`。 */
  now?: () => number;
  /** 逐方法超时覆盖（测试用）。 */
  timeouts?: Partial<Record<FactlayerTimeoutName, number>>;
}

export interface RememberInput {
  content: string;
  /** = `body.provider.settings.memoryBodyId`（适配器负责取并校验）。 */
  sessionId: string;
  /** 每次新的 uuid4（契约无幂等位，见 docs/06 §5）。 */
  clientWriteId: string;
}

export class FactlayerClient {
  readonly baseUrl: string;
  readonly apiKey: string;
  private readonly fetchImpl: FetchLike;
  private readonly now: () => number;
  private readonly timeouts: Record<FactlayerTimeoutName, number>;

  private statusCache: { payload: unknown; at: number } | undefined;
  private statusInflight: Promise<unknown> | undefined;
  /** invalidateStatus 后旧 in-flight 结果不得回填缓存。 */
  private statusGeneration = 0;

  constructor(options: FactlayerClientOptions) {
    const baseUrl = options.baseUrl.trim().replace(/\/+$/u, '');
    if (baseUrl === '') {
      throw new ShimError(
        'config',
        'factlayer baseUrl 未配置：请在 dsh-mnemon 的 factlayer 服务配置里填写 shim 地址（如 http://127.0.0.1:8000）',
      );
    }
    this.baseUrl = baseUrl;
    this.apiKey = (options.apiKey ?? '').trim();
    this.fetchImpl = options.fetch ?? ((input, init) => fetch(input, init));
    this.now = options.now ?? (() => Date.now());
    this.timeouts = {
      search: options.timeouts?.search ?? DEFAULT_TIMEOUTS.search,
      remember: options.timeouts?.remember ?? DEFAULT_TIMEOUTS.remember,
      status: options.timeouts?.status ?? DEFAULT_TIMEOUTS.status,
      spaces: options.timeouts?.spaces ?? DEFAULT_TIMEOUTS.spaces,
    };
  }

  // ------------------------------------------------------------------ 读
  /** `GET /spaces`：discover 的命名空间列表（shim 侧 O(1)，不查库）。 */
  async spaces(signal?: AbortSignal): Promise<unknown> {
    return this.request('/spaces', { method: 'GET' }, this.timeouts.spaces, signal);
  }

  /** `GET /health`：经 30s SWR 包装，供适配器的 `status()` 使用。 */
  async health(): Promise<unknown> {
    const cached = this.statusCache;
    if (cached !== undefined) {
      const age = this.now() - cached.at;
      if (age < STATUS_STALE_MS) return cached.payload;
      // 过期：立即返回旧值，后台重验；失败静默（保留旧值，下一轮再试）。
      void this.revalidateHealth().catch(() => undefined);
      return cached.payload;
    }
    return this.revalidateHealth();
  }

  private revalidateHealth(): Promise<unknown> {
    const existing = this.statusInflight;
    if (existing !== undefined) return existing;
    const generation = this.statusGeneration;
    const promise: Promise<unknown> = this.request(
      '/health',
      { method: 'GET' },
      this.timeouts.status,
    )
      .then((payload) => {
        if (generation === this.statusGeneration) {
          this.statusCache = { payload, at: this.now() };
        }
        return payload;
      })
      .finally(() => {
        if (this.statusInflight === promise) this.statusInflight = undefined;
      });
    this.statusInflight = promise;
    return promise;
  }

  /** 清空 status 快照与 generation（`invalidateStatus` / `dispose` 调用）。 */
  invalidateStatus(): void {
    this.statusGeneration += 1;
    this.statusCache = undefined;
  }

  // ------------------------------------------------------------------ 写
  /** `POST /search`：返回原始 payload，由适配器做 `items → results` 翻译。 */
  async search(input: { query: string; limit?: number }, signal?: AbortSignal): Promise<unknown> {
    const body: Record<string, unknown> = { query: input.query };
    if (input.limit !== undefined) body.limit = input.limit;
    return this.request(
      '/search',
      { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) },
      this.timeouts.search,
      signal,
    );
  }

  /** `POST /memories`：shim 的三个必填字段由适配器造好再传进来。 */
  async remember(input: RememberInput, signal?: AbortSignal): Promise<unknown> {
    return this.request(
      '/memories',
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          client_write_id: input.clientWriteId,
          content: input.content,
          session_id: input.sessionId,
          source_client: 'mnemon-provider',
        }),
      },
      this.timeouts.remember,
      signal,
    );
  }

  // ------------------------------------------------------------------ 传输
  private async request(
    path: string,
    init: RequestInit,
    timeoutMs: number,
    signal?: AbortSignal,
  ): Promise<unknown> {
    signal?.throwIfAborted();
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    const relay = () => controller.abort();
    signal?.addEventListener('abort', relay, { once: true });

    try {
      let response: Response;
      try {
        response = await this.fetchImpl(`${this.baseUrl}${path}`, {
          ...init,
          headers: this.headers(init.headers),
          signal: controller.signal,
        });
      } catch (error) {
        if (timedOut) {
          throw new ShimError(
            'timeout',
            `factlayer 请求超时（${timeoutMs}ms）：${path}`,
            { cause: error },
          );
        }
        if (signal?.aborted === true) throw error;
        throw new ShimError(
          'network',
          `factlayer 无法连接 ${this.baseUrl}：${errorMessage(error)}`,
          { cause: error },
        );
      }

      const raw = await response.text();
      const payload = parseJson(raw);
      if (!response.ok) throw httpError(response.status, payload);
      return payload;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', relay);
    }
  }

  private headers(extra: HeadersInit | undefined): Record<string, string> {
    const headers: Record<string, string> = { accept: 'application/json' };
    if (this.apiKey !== '') headers.authorization = `Bearer ${this.apiKey}`;
    return { ...headers, ...(extra as Record<string, string> | undefined) };
  }
}

// ---------------------------------------------------------------- 纯函数

function parseJson(raw: string): unknown {
  if (raw.trim() === '') return {};
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    return raw;
  }
}

function httpError(status: number, payload: unknown): ShimError {
  const record = asRecord(payload);
  const detail =
    asString(record?.detail) ?? asString(record?.message) ?? asString(record?.error);
  if (status === 503) {
    const code = asString(record?.error);
    // 重建期：shim 返回 {"error":"rebuilding","message":"索引重建中，请稍后"}。
    if (code === 'rebuilding' || code === undefined) {
      return new ShimError(
        'rebuilding',
        asString(record?.message) ?? '索引重建中，请稍后',
        { status },
      );
    }
  }
  const suffix = detail === undefined ? '' : `: ${detail}`;
  return status >= 500
    ? new ShimError('server', `factlayer HTTP ${status}${suffix}`, { status })
    : new ShimError('client', `factlayer HTTP ${status}${suffix}`, { status });
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}
