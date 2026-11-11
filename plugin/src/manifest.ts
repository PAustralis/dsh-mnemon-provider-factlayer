/**
 * `MemorySpaceProviderManifest`（docs/06 §2/§8、docs/05 §6.1）。
 *
 * 16 个字段写全；三处身份一致：`manifest.typeId` === 子模块 `id` === `adapter.id`。
 * 逐项校验里最容易踩的四个：
 * 1. `apiVersion` 是固定字面量，抄自本机已装官方 provider 用的常量
 *    （`MEMORY_SPACE_PROVIDER_API_VERSION`，见 contract.ts 注释）。
 * 2. `secrets` 必须与 `fields` 里 `input:'secret'` 的集合**完全一致**。
 * 3. `capabilities` 8 个布尔写全（漏键会被逐项校验拒绝）。
 * 4. `scoreSemantics` 与 adapter 的 `scoreSemantics.kind` 必须一致——
 *    这里与 `adapter.ts` 都是 `provider-native`（RRF 分不是 0..1 校准分，
 *    声明 normalized 会被 0.25 阈值清空，docs/03 §6）。
 */

import {
  MEMORY_SPACE_PROVIDER_API_VERSION,
  type MemoryProviderCapabilities,
  type MemoryProviderConfigField,
  type MemorySpaceProviderManifest,
} from './contract.js';

/** 子模块 id / manifest.typeId / adapter.id 三处逐字一致。 */
export const PROVIDER_ID = 'factlayer';

export const PROVIDER_PACKAGE_NAME = 'dsh-mnemon-provider-factlayer';

/** 本包版本（manifest 不校验 semver，与 package.json 保持一致）。 */
export const PROVIDER_VERSION = '0.1.0';

/** 能力声明（docs/06 §2）：只开 search + remember，绝不伪造 browse/graph/entities。 */
export const FACTLAYER_CAPABILITIES: MemoryProviderCapabilities = {
  search: true,
  browse: false,
  graph: false,
  entities: false,
  related: false,
  remember: true,
  link: false,
  forget: false,
  writeMode: 'exact',
  deletionMode: 'unsupported',
};

/**
 * 连接字段（docs/06 §3 表）。
 *
 * - `baseUrl`：service 作用域，shim 地址；`input:'url'` 由宿主强校验 http(s)。
 * - `apiKey`：service + `secret`；shim 目前不校验，留空即可（回环部署）。
 * - `memoryBodyId`：**memory** 作用域（discover 会把同值写进 `connection`），
 *   remember 的 `session_id` 就读它——两台机器因此天然一致。
 */
export const FACTLAYER_FIELDS: MemoryProviderConfigField[] = [
  {
    key: 'baseUrl',
    label: 'Shim base URL',
    scope: 'service',
    input: 'url',
    required: true,
    defaultValue: 'http://127.0.0.1:8000',
    normalize: 'trim-trailing-slash',
    placeholder: 'http://127.0.0.1:8000',
    help: 'factlayer FastAPI shim 的地址（单 worker，通常在本机 8000 端口）。',
  },
  {
    key: 'apiKey',
    label: 'Shim API key',
    scope: 'service',
    input: 'secret',
    required: false,
    help: 'shim 目前不校验鉴权；回环部署可留空。配置后会以 Authorization: Bearer 发送。',
  },
  {
    key: 'memoryBodyId',
    label: 'Factlayer space id',
    scope: 'memory',
    input: 'text',
    required: true,
    defaultValue: 'factlayer-default',
    maxLength: 2000,
    help: 'shim 的固定空间标识（= FACTLAYER_SPACE_ID）。discover 会自动把它写入 connection；手动创建空间时填同一个值。',
  },
];

export const FACTLAYER_MANIFEST: MemorySpaceProviderManifest = {
  apiVersion: MEMORY_SPACE_PROVIDER_API_VERSION,
  kind: 'provider',
  typeId: PROVIDER_ID,
  packageName: PROVIDER_PACKAGE_NAME,
  version: PROVIDER_VERSION,
  label: 'Factlayer',
  icon: { kind: 'glyph', value: 'FL' },
  summary:
    'Factlayer shim：只追加的事实层 + 混合检索（RRF），按 provider-native 顺序返回；写入是同步落盘的精确写。',
  summaryI18nKey: 'overview.providerSummary.factlayer',
  origin: 'third-party',
  // 数据在 shim 自己的 SQLite 里、DSH 只经 HTTP 访问：按「远端服务」声明更保守，
  // 不会被 local-only 的 placement 规则误选（docs/06 §3 只要求数据边界如实）。
  locality: 'remote',
  workspaceBinding: 'provider-global',
  capabilities: FACTLAYER_CAPABILITIES,
  fields: FACTLAYER_FIELDS,
  // 必须与 fields 里 input:'secret' 的 key 集合完全一致（这里只有 apiKey）。
  secrets: ['apiKey'],
  scoreSemantics: 'provider-native',
};
