/**
 * dsh-mnemon Memory Space Provider 契约的**本地类型镜像**（最小必要集）。
 *
 * 来源（逐字段抄写，不改语义）：
 * - `docs/05-provider接口契约.md` §2（17 个成员）、§3（逐方法字段）、§6.1（manifest 16 字段）、
 *   §6.2（fields）、§6.3（capabilities）、§6.4（scoreSemantics）、§6.6（factory context）。
 * - 复核锚点：`dsh-mnemon@0.5.37` / `dsh-mnemon-source-memory-spaces@0.5.24`
 *   的 `lib/definitions-*.d.ts`（`MemoryProviderAdapter` / manifest / host 定义）
 *   与 `lib/types/contracts.d.ts`（`Insight` / `SearchRequest` / `RememberRequest` /
 *   `MemoryListRequest` / capabilities / `MemorySpaceStats` / graph 快照）。
 *
 * **为什么不直接 import**：本包不把 `dsh-mnemon` 装成依赖（它是 peer，由宿主的
 * profile 提供），编译期 node_modules 里没有它；运行期也只通过动态 import 取
 * `defineMemorySpaceProvider` 一个函数（见 `define.ts`）。因此编译期用这份镜像，
 * 运行期由宿主校验真实形状。
 */

/** `MemoryJsonValue` 的镜像（`dsh-mnemon/contracts`）。 */
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

/** 六个分类（contracts.d.ts:242 CATEGORIES）。 */
export type Category =
  | 'preference'
  | 'decision'
  | 'fact'
  | 'insight'
  | 'context'
  | 'general';

/** 三种来源（contracts.d.ts:244 SOURCES）。 */
export type Source = 'user' | 'agent' | 'external';

/** 四种意图（contracts.d.ts:248 INTENTS）。 */
export type Intent = 'WHY' | 'WHEN' | 'ENTITY' | 'GENERAL';

/** 四值边类型（contracts.d.ts:246 EDGE_TYPES）。注意 `supersedes` 不是合法值。 */
export type EdgeType = 'temporal' | 'semantic' | 'causal' | 'entity';

/** 连接值只允许标量（contracts.d.ts:21）。 */
export type MemoryProviderConnectionValue = string | number | boolean;
export type MemoryProviderConnection = Record<
  string,
  MemoryProviderConnectionValue
>;

export interface MemoryProviderIcon {
  kind: 'brand' | 'data-url' | 'glyph';
  value: string;
}

export interface MemoryProviderConfigOption {
  value: string;
  label: string;
  i18nKey?: string;
}

/** 连接字段（contracts.d.ts:34-57 / 05 §6.2 的逐字段校验）。 */
export interface MemoryProviderConfigField {
  key: string;
  label: string;
  i18nKey?: string;
  /** service：一次配置（Plugins 页）；memory：每个 Memory Space 各配一份。 */
  scope: 'service' | 'memory';
  role?: 'global-location';
  input: 'text' | 'url' | 'secret' | 'number' | 'boolean' | 'select' | 'path';
  required: boolean;
  defaultValue?: MemoryProviderConnectionValue;
  placeholder?: string;
  help?: string;
  min?: number;
  max?: number;
  maxLength?: number;
  pattern?: string;
  normalize?: 'trim-trailing-slash';
  validationMessage?: string;
  discoveryDefaultFrom?: string;
  options?: MemoryProviderConfigOption[];
}

/** 8 个布尔 + 2 个枚举（05 §6.3，必须写全）。 */
export interface MemoryProviderCapabilities {
  search: boolean;
  browse: boolean;
  graph: boolean;
  entities: boolean;
  related: boolean;
  remember: boolean;
  link: boolean;
  forget: boolean;
  writeMode: 'exact' | 'async-extracting';
  deletionMode: 'soft' | 'hard' | 'unsupported';
}

export interface MemorySpaceStats {
  totalInsights: number;
  deletedInsights: number;
  edgeCount: number;
  oplogCount: number;
  dbSizeBytes: number;
  byCategory: Record<string, number>;
  topEntities: Array<{ entity: string; count: number }>;
}

/** Provider 在空间目录里的身份投影（contracts.d.ts:172-189，只镜像用到的键）。 */
export interface MemorySpaceProvider {
  id: string;
  typeId?: string;
  label: string;
  icon?: MemoryProviderIcon;
  origin?: 'native' | 'third-party';
  kind: 'local' | 'remote';
  location: string;
  targetUri?: string;
  account?: string;
  user?: string;
  actorPeerId?: string;
  apiKeyConfigured: boolean;
  settings: MemoryProviderConnection;
  configuredSecrets: string[];
  capabilities: MemoryProviderCapabilities;
}

/** 适配器方法收到的 `body`（contracts.d.ts:198-208）。 */
export interface MemorySpace {
  id: string;
  name: string;
  description: string;
  active: boolean;
  dbPath: string;
  provider: MemorySpaceProvider;
  createdAt: string;
  updatedAt: string;
}

/** 检索结果项（contracts.d.ts:256-285）。 */
export interface Insight {
  id: string;
  content: string;
  category?: string;
  importance?: number;
  tags?: string[];
  entities?: string[];
  source?: string;
  score?: number;
  normalizedScore?: number;
  relevanceTier?: 'high' | 'medium' | 'ranked' | 'low' | 'unknown';
  federatedScore?: number;
  confidence?: string;
  intent?: string;
  matchedVia?: string;
  createdAt?: string;
  depth?: number;
  edgeType?: string;
  memoryBodyId?: string;
  memoryBodyName?: string;
  memoryProviderId?: string;
  memoryProviderLabel?: string;
  memoryCapabilities?: MemoryProviderCapabilities;
  externalUri?: string;
}

/** `search` 的请求（contracts.d.ts:286-301；无扩展位）。 */
export interface SearchRequest {
  query: string;
  mode?: 'smart' | 'keyword' | 'basic';
  limit?: number;
  category?: Category;
  source?: Source;
  intent?: Intent;
  memoryBodyIds?: string[];
  inspect?: boolean;
}

/** `remember` 的请求（contracts.d.ts:302-311；无扩展位）。 */
export interface RememberRequest {
  content: string;
  category?: Category;
  importance?: number;
  tags?: string[];
  entities?: string[];
  source?: Source;
  memoryBodyId?: string;
}

/** `list` 的请求（contracts.d.ts:453-458；**没有 session/offset**）。 */
export interface MemoryListRequest {
  query?: string;
  category?: Category;
  limit?: number;
  memoryBodyIds?: string[];
}

export interface MemoryGraphNode extends Insight {
  color: string;
  graphId?: string;
  kind?: 'memory' | 'entity' | 'space';
  memoryBodyIds?: string[];
  memoryBodyNames?: string[];
  occurrenceCount?: number;
}

export interface MemoryGraphEdge {
  sourceId: string;
  targetId: string;
  label: string;
  color: string;
  type?: EdgeType | 'scope';
}

export interface MemoryGraphSnapshot {
  nodes: MemoryGraphNode[];
  edges: MemoryGraphEdge[];
  generatedAt: string;
  memoryBodies?: Array<Pick<MemorySpace, 'id' | 'name' | 'active'>>;
  sources?: unknown[];
}

/** `status` 的返回（definitions d.ts:39-43 / 05 §3.4）。 */
export interface ProviderSpaceStatus {
  healthy: boolean;
  error?: string;
  stats?: MemorySpaceStats;
}

/** `search` 的返回（definitions d.ts:44-47 / 05 §2.1 #4）。 */
export interface ProviderSearchResult {
  results: Insight[];
  hint?: string;
}

/** adapter 侧的分数语义（两值，05 §6.4）。 */
export interface ProviderScoreSemantics {
  kind: 'normalized-relevance' | 'provider-native';
}

/** `discover` 的命名空间（definitions d.ts:62-68 / 05 §3.5）。 */
export interface ProviderMemorySpace {
  externalId: string;
  name: string;
  description: string;
  connection: MemoryProviderConnection;
}

export interface ProviderEntityIndex {
  memories: Insight[];
  complete: boolean;
}

/** Provider 数据面（definitions d.ts:73-106；17 个成员）。 */
export interface MemoryProviderAdapter {
  readonly id: MemorySpace['provider']['id'];
  readonly scoreSemantics?: ProviderScoreSemantics;
  discover?(
    connection: MemoryProviderConnection,
    signal?: AbortSignal,
  ): Promise<ProviderMemorySpace[]>;
  invalidateStatus?(memoryBodyId?: string): void;
  status(body: MemorySpace, signal?: AbortSignal): Promise<ProviderSpaceStatus>;
  search(
    body: MemorySpace,
    request: SearchRequest,
    signal?: AbortSignal,
  ): Promise<ProviderSearchResult>;
  graph(body: MemorySpace, signal?: AbortSignal): Promise<MemoryGraphSnapshot>;
  list(
    body: MemorySpace,
    request: MemoryListRequest,
    signal?: AbortSignal,
  ): Promise<Insight[]>;
  remember(
    body: MemorySpace,
    request: RememberRequest,
    signal?: AbortSignal,
  ): Promise<JsonValue>;
  metadataSample?(
    body: MemorySpace,
    limit: number,
    signal?: AbortSignal,
  ): Promise<Insight[]>;
  entityIndex?(body: MemorySpace, signal?: AbortSignal): Promise<ProviderEntityIndex>;
  rememberMany?(
    body: MemorySpace,
    requests: readonly RememberRequest[],
    signal?: AbortSignal,
  ): Promise<JsonValue[]>;
  get?(body: MemorySpace, id: string, signal?: AbortSignal): Promise<Insight | undefined>;
  related?(
    body: MemorySpace,
    id: string,
    depth: number,
    edge?: EdgeType,
    signal?: AbortSignal,
  ): Promise<Insight[]>;
  link?(
    body: MemorySpace,
    sourceId: string,
    targetId: string,
    type: EdgeType,
    weight: number,
    reason?: string,
    signal?: AbortSignal,
  ): Promise<JsonValue>;
  forget?(body: MemorySpace, id: string, signal?: AbortSignal): Promise<JsonValue>;
  dispose?(): void | Promise<void>;
}

/**
 * manifest 的 API 版本（逐字来自 `MEMORY_SPACE_PROVIDER_API_VERSION`，
 * `dsh-mnemon-source-memory-spaces@0.5.24/lib/definitions-DfmOqJjR.js`）。
 *
 * 注意：第 4 条工具输出里写的 `dsh-mnemon.memory-space-provider/v1` 是值本身；
 * 这里抄的是**本机已装官方 provider 用的那个常量**，不是猜的字符串。
 */
export const MEMORY_SPACE_PROVIDER_API_VERSION =
  'dsh-mnemon.memory-space-provider/v1' as const;

/** manifest（definitions d.ts:117-134；05 §6.1 的 16 个字段）。 */
export interface MemorySpaceProviderManifest {
  apiVersion: typeof MEMORY_SPACE_PROVIDER_API_VERSION;
  kind: 'provider';
  typeId: string;
  packageName: string;
  version: string;
  label: string;
  icon?: MemoryProviderIcon;
  summary: string;
  summaryI18nKey?: string;
  origin: 'native' | 'third-party';
  locality: 'local' | 'remote';
  workspaceBinding: 'automatic' | 'optional-override' | 'provider-global';
  capabilities: MemoryProviderCapabilities;
  fields: MemoryProviderConfigField[];
  secrets: string[];
  scoreSemantics: 'normalized-relevance' | 'provider-native' | 'none';
}

/**
 * Cordis `Context` 的最小结构镜像（真实类型来自 `@deepseek-ai/cordis`）。
 * `host.install(owner, ...)` 只把它当作拥有者句柄转交，本包不读它的任何成员。
 */
export interface MemorySpaceProviderContextLike {
  readonly [key: string]: unknown;
}

/** 父 Fiber 交给子模块的最小权限（definitions d.ts:5-11）。 */
export interface MemorySpaceAuthority {
  readonly runner: { effectiveDataDir(): string };
  list(): MemorySpace[];
  providerConnection(
    id: string,
    expectedProviderId?: string,
  ): MemoryProviderConnection;
}

/** `create()` 收到的上下文（definitions d.ts:28-38 / 135-139）。 */
export interface MemoryProviderAdapterFactoryContext {
  /** Canonical name; optional while older Source hosts remain supported. */
  memorySpaces?: MemorySpaceAuthority;
  /** @deprecated 同一对象；新代码用 memorySpaces。 */
  memoryBodies: MemorySpaceAuthority;
  config: {
    timeoutMs: number;
    defaultRecallLimit?: number;
  };
  nativeRunner?: unknown;
}

export interface MemorySpaceProviderRuntimeContext
  extends MemoryProviderAdapterFactoryContext {
  providerInstanceId: string;
  manifest: MemorySpaceProviderManifest;
}

export interface MemorySpaceProviderDefinition {
  manifest: MemorySpaceProviderManifest;
  create(context: MemorySpaceProviderRuntimeContext): MemoryProviderAdapter;
}

export type MemorySpaceProviderDisposer = () => void | Promise<void>;

/** 闭包私有的 host（definitions d.ts:146-148）。 */
export interface MemorySpaceProviderHost {
  install(
    owner: MemorySpaceProviderContextLike,
    definition: MemorySpaceProviderDefinition,
  ): MemorySpaceProviderDisposer;
}

/** 子模块形状（definitions d.ts:150-155）。 */
export interface MemorySpaceProviderModule<Config = undefined> {
  readonly id: string;
  readonly Config?: unknown;
  apply(
    ctx: MemorySpaceProviderContextLike,
    host: MemorySpaceProviderHost,
    config: Config,
  ): void | Promise<void>;
}

/** `defineMemorySpaceProvider` 的运行期形状（本包只关心恒等函数语义）。 */
export type DefineMemorySpaceProvider = <Config>(
  module: MemorySpaceProviderModule<Config>,
) => MemorySpaceProviderModule<Config>;
