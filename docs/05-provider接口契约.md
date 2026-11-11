# 05 · Provider 完整接口契约（第三层实现手册）

> **本文只回答一件事：第三方 Provider（第三层）到底要实现什么、怎么声明、调用方在什么条件下调它、失败长什么样。**
> 检索侧的字段映射见 `03-对接层契约映射.md`（§1.2 请求、§1.3 响应、§4.1 `hint`/`unavailable`）——**本文不重复，只引用**。
> 老仓库 `HANDOFF.md` 只读交叉验证；**与源码冲突处一律以源码为准并注明**。未改 03，未改 HANDOFF，不含代码。
>
> 复核锚点（2026-11-11，本机实装）：`dsh-mnemon@0.5.37`、`dsh-mnemon-source-memory-spaces@0.5.24`、`dsh-mnemon-strategy-default-*`（three-tier）。

---

## 0. 证据等级与路径缩写

| 级 | 含义 |
|---|---|
| **[A]** | HANDOFF 已给的源码结论（HANDOFF 自带文件/行号或声明"签名准确"） |
| **[B]** | HANDOFF 的推断 / 未验证 |
| **[A+]** | **本次复核**：本机已装包的 `lib/types/*.d.ts` 与 `lib/*.js`（行号给出），HANDOFF 未覆盖或需修正处 |
| **[E]** | 本会话实测观察 |
| **[推断]** | 源码未写死、未实测，明确标注 |

路径缩写（下文 `P:123` 即该文件第 123 行）：

| 缩写 | 文件 |
|---|---|
| **PD** | `…\dsh-mnemon-source-memory-spaces@0.5.24_dsh-mnemon@0.5.37\…\lib\definitions-BNmfQoWk.d.ts` ← **适配器/清单的唯一真源**（文件名带内容哈希，升级会变，用 `lib\definitions-*.d.ts` 找） |
| **PG** | 同包 `lib\definitions-DfmOqJjR.js`（`MEMORY_SPACE_PROVIDER_API_VERSION`、manifest 校验） |
| **PJ** | 同包 `lib\plugin-m1PeU1IE.js`（注册表、`assertRuntime`、descriptor 投影） |
| **PC** | 同包 `lib\types\contracts.d.ts`（`Insight` / `SearchRequest` / `RememberRequest` / 能力位） |
| **PS** | 同包 `lib\provider-sdk.d.ts`、`lib\provider-sdk.js`（对外导出面、`HttpMemoryProvider`） |
| **P** | 同包 `lib\index.js`（Source 本体：调用点、归一化、回执判定） |
| **M** | `…\dsh-mnemon@0.5.37\…\lib\index.js`（Core：能力门禁、错误映射、写 worker） |
| **MS** | `…\dsh-mnemon@0.5.37\…\lib\strategy-configuration-CKP2kRss.js`（输入校验原语 `integer/text/stringArray/record`，即 `memoryInput*`） |
| **S** | `…\dsh-mnemon-strategy-default_*\…\dsh-mnemon-strategy-default-three-tier\lib\index.js` |

---

## 1. 谁把 Provider 装进来（先看这一节，它决定交付形态）

### 1.1 三层装配（不是 Cordis Service）

```
DSH bundle (dsh-mnemon/cordis.patch.yml)
  └─ Entry: dsh-mnemon-source-memory-spaces   ← 父 Fiber
       config.providers: [ {use: "dsh-mnemon-provider-*", instanceId, config} … ]
         └─ ctx.plugin(provider 子模块)        ← 子 Fiber，只能通过闭包拿到 host
              module.apply(ctx, host, config) → host.install(ctx, definition)
                   definition = { manifest, create(context) → MemoryProviderAdapter }
```

- `host` 是**闭包私有的 `PrivateMemorySpaceProviderHost`，不是 Cordis Service，`ctx.mnemonProvider` 不存在** [A+] `PJ:324-370`；子模块拿不到任何别的父权限 [A+] `PJ:377-387`。
- 子模块必须 **default export 一个 typed child module**：`defineMemorySpaceProvider({ id, apply, Config? })` [A+] `P:4280-4296`、`PJ:161`；`id` 必须匹配 `^[a-z][a-z0-9-]{0,127}$` [A+] `PG:4,6-10`。
- 子模块**必须真的 install 一份 definition**，否则装配直接失败：`Memory Space Provider child did not install a definition: <id>` [A+] `P:4317`。
- `definition.manifest.typeId` 必须等于 `module.id`，否则 `PJ:344` 抛错；`adapter.id` 必须等于 `manifest.typeId`，否则 `PJ:259` 抛错（**注意之后会被 Proxy 换成 instanceId**，见 §6.4）。

### 1.2 声明式挂载：`providers` 是唯一入口

| 事实 | 证据 |
|---|---|
| Schema：`providers: (string \| { use: string; instanceId?: string; config?: any })[]`，默认 `[]` | [A+] `P:4267-4272` |
| `use` = 包名，经 `loader.import(use)` 解析；**不做依赖扫描**，只认显式列出的子模块 | [A+] `P:4285-4295` |
| 至少一个子模块，否则 `Memory Spaces requires at least one explicit Provider child` | [A+] `P:4287,4309` |
| `instanceId` 缺省 = `module.id`；重复 `instanceId` 直接抛错 | [A+] `P:4297,4301` |
| 每个子模块的 `config` 交给 `module.apply(ctx, host, config)` 与 `plugin.Config` | [A+] `P:4299`、`PJ:378-386` |
| 装配结果进 `memoryConfigurationDigest`，**改 provider 列表 = 换 generation**（View 需要重算） | [A+] `P:4324-4327` |

**⚠️ 交付形态的硬约束（本文最容易被忽略的一条）**：
DSH 的**在线安装器只接受 `dsh-mnemon-source-*` / `dsh-mnemon-strategy-*`**，报错文案是 `Use an exact dsh-mnemon-source-* or dsh-mnemon-strategy-* package name` [A+] `M:17131,17163-17167`。
→ **`dsh-mnemon-provider-*` 包无法从 GUI 的插件安装入口装**。落地方式只有两条：①随 bundle 的 `cordis.patch.yml` 一起发（官方 9 个 provider 就是这么发的 [A+] `dsh-mnemon\cordis.patch.yml:56-74`）；②用户手改 profile 的 `cordis.patch.yml`（本机 `web` profile 的 `cordis.patch.yml:45-101` 已整块覆盖过 mnemon config，说明这条路是现实路径）。**这是第三层交付必须先向用户说明的成本**，不是实现细节。

### 1.3 运行时状态：启用 ≠ 装配

| 概念 | 落点 | 证据 |
|---|---|---|
| 服务配置/开关持久化 | `<dataDir>\state\memory-providers.json`（`services` / `enabled` / `bodies`） | [A+] `PC:654,1070-1079` |
| 读服务目录（RPC `provider-services`） | `MemoryProviderServiceCatalog { providers, items, generatedAt }` | [A+] `P:355-374` |
| 保存服务配置（RPC `provider-service-update`） | **必须先 `discover()`**，否则 `… does not support Memory Space discovery` | [A+] `P:3072-3081` |
| 重扫（RPC `provider-rediscover`） | 每 provider 30 s 上限（`MEMORY_SPACES_REDISCOVERY_PROVIDER_TIMEOUT_MS`），超时/失败只记录、不动已有空间 | [A+] `P:1687,3090-3164` |
| 空间是否可读 | `active && (native \|\| serviceEnabled)`；`serviceEnabled` 要 `configured && enabled===true` | [A+] `P:343-345,313-315` |
| 写入是否开放 | `config.writeEnabled`，否则所有写方法在 Source 层就抛 `… configured read-only` | [A+] `P:3358-3360` |

---

## 2. Provider 必须实现哪些方法（必答 1）

`MemoryProviderAdapter` 共 **17 个成员**：2 个只读属性 + 15 个方法（其中 **10 个**带 `?`）。[A+] `PD:73-106`

### 2.1 成员总表

| # | 成员 | 类型必填 | 签名（参数顺序照抄类型） | 返回 | 调用方在什么场景调用 | 证据 |
|---|---|---|---|---|---|---|
| 1 | `id` | ✅ | `readonly string` | — | 注册时校验；运行时被 Proxy 换成 instanceId | [A+] `PD:74`、`PJ:259,273-279` |
| 2 | `scoreSemantics` | ❌ 但**必须**与 manifest 一致 | `{ kind: 'normalized-relevance' \| 'provider-native' }` | — | recall 质量策略定档 | [A+] `PD:75`、`P:2455,1463-1514` |
| 3 | `status` | ✅ | `(body: MemorySpace, signal?) => Promise<ProviderSpaceStatus>` | `{healthy, error?, stats?}` | **每个空间目录刷新都调**（Memory 页、Status、实体读） | [A+] `PD:80`、`P:2013,2873` |
| 4 | `search` | ✅ | `(body, request: SearchRequest, signal?) => Promise<ProviderSearchResult>` | `{results: Insight[], hint?}` | `mnemon_recall` / GUI 检索 / `list()` 的 query-only 兜底 / 元数据采样 | [A+] `PD:81`、`P:2432,2688,2578` |
| 5 | `graph` | ✅ | `(body, signal?) => Promise<MemoryGraphSnapshot>` | `{nodes, edges, generatedAt, …}` | 图谱页；**仅当 `capabilities.graph`** | [A+] `PD:82`、`P:2607-2628` |
| 6 | `list` | ✅ | `(body, request: MemoryListRequest, signal?) => Promise<Insight[]>` | 裸 `Insight[]` | 列表页、实体索引兜底、元数据采样；**仅当 `capabilities.browse`** | [A+] `PD:83`、`P:2691,2910,2585` |
| 7 | `remember` | ✅ | `(body, request: RememberRequest, signal?) => Promise<JsonValue>` | **任意 JSON 回执**（见 §4.5） | `mnemon_remember`（写 worker 内部）；`rememberMany` 的逐条兜底 | [A+] `PD:84`、`P:2969` |
| 8 | `discover?` | 可选，**但启用服务时事实必填** | `(connection: MemoryProviderConnection, signal?) => Promise<ProviderMemorySpace[]>` | 命名空间列表 | 保存服务配置、启动/手动重扫 | [A+] `PD:77`、`P:3078,3123` |
| 9 | `invalidateStatus?` | 可选 | `(memoryBodyId?: string) => void` | — | 用户点"重新连接"一处 | [A+] `PD:79`、`P:2385` |
| 10 | `metadataSample?` | 可选 | `(body, limit: number, signal?) => Promise<Insight[]>` | 裸 `Insight[]` | 只有 native 走这条；否则退回 `list()`/`search()` | [A+] `PD:86`、`P:2572-2586` |
| 11 | `entityIndex?` | 可选 | `(body, signal?) => Promise<ProviderEntityIndex>` | `{memories: Insight[], complete: boolean}` | 实体页；**仅当 `capabilities.entities`** | [A+] `PD:92`、`P:2827,2906` |
| 12 | `rememberMany?` | 可选 | `(body, requests: readonly RememberRequest[], signal?) => Promise<JsonValue[]>` | **与 requests 等长的回执数组** | 写通道 `remember-many`（单次 ≤1000 条）；写 worker 收多张票时 | [A+] `PD:94`、`P:2955-2965,3684-3686` |
| 13 | `get?` | 可选 | `(body, id: string, signal?) => Promise<Insight \| undefined>` | 单条或 `undefined` | 用 id 反查归属空间（`forget`/`related` 前定位） | [A+] `PD:100`、`P:2981-2993` |
| 14 | `related?` | 可选 | `(body, id, depth: number, edge?: EdgeType, signal?) => Promise<Insight[]>` | 裸 `Insight[]` | `mnemon_related`；**仅当 `capabilities.related`** | [A+] `PD:101`、`P:2995-3001` |
| 15 | `link?` | 可选 | `(body, sourceId, targetId, type: EdgeType, weight: number, reason?, signal?) => Promise<JsonValue>` | 回执 | `mnemon_link`；**仅当 `capabilities.link`** | [A+] `PD:102`、`P:3002-3011` |
| 16 | `forget?` | 可选 | `(body, id: string, signal?) => Promise<JsonValue>` | 回执 | `mnemon_forget`；**仅当 `capabilities.forget`** | [A+] `PD:103`、`P:3013-3020` |
| 17 | `dispose?` | 可选 | `() => void \| Promise<void>` | — | generation 销毁，**逆序**调用；任一抛错聚合成 `AggregateError` | [A+] `PD:105`、`P:1984-1990` |

**与 HANDOFF 的一致性**：HANDOFF §5 的"必需 6 / 可选 10"= `id/status/search/graph/list/remember` + 其余，**与类型定义逐字吻合**，无冲突 [A] §5 ↔ [A+] `PD:73-106`。

### 2.2 两层强制：类型层 vs 运行层（**这是一处 HANDOFF 没写的坑**）

`assertRuntime` 只在 `create()` 返回后检查下面这些，**不检查 `status`**：

| 能力位 | 必须存在的方法 | 证据 |
|---|---|---|
| `capabilities.search` | `search` | [A+] `PJ:261` |
| `capabilities.browse` | `list` | [A+] `PJ:262` |
| `capabilities.graph` | `graph` | [A+] `PJ:263` |
| `capabilities.related` | `related` | [A+] `PJ:264` |
| `capabilities.remember` | `remember` | [A+] `PJ:265` |
| `capabilities.link` | `link` | [A+] `PJ:266` |
| `capabilities.forget` | `forget` | [A+] `PJ:267` |
| 无条件 | `adapter.id === manifest.typeId` | [A+] `PJ:259` |
| 条件 | `manifest.scoreSemantics==='normalized-relevance'` → `adapter.scoreSemantics.kind` 必须是它 | [A+] `PJ:270` |

推论（第三层必须知道）：
1. **能力位声明 `false` 的方法可以不实现，装配不会报错**；但类型层面仍要求 `status/search/graph/list/remember` 存在（TS 编译期）。运行时漏实现 `status` 的后果是：`collectSpaces` 捕获异常并把空间标成 `unhealthy`，错误文案是 `… is not a function` [A+] `P:2012-2019` —— 不崩，但**整个空间永远显示不健康**。
2. `capabilities.entities` **没有对应的方法强制**；`entityIndex` 缺失时 Source 退回 `list()`，且若 `browse` 也是 `false`，实体页直接给 `query-only / query-required` [A+] `P:2827-2831`。
3. Source **不校验 `Insight` 的字段**（见 §3.13）：形状错了不会在装配期被拦住，会一路带到运行期。

---

## 3. 逐方法的字段与"静默丢弃"规则（必答 2）

### 3.1 `search` → 直接引用 03，本节只补 Source 侧三条新事实

请求/响应字段表、必填与降档规则、`hint` 仅在空结果透传，**见 `03` §1.2 / §1.3 / §4.1，不复制**。本节只补 03 未写的调用方行为：

| 事实 | 证据 |
|---|---|
| **`results` 缺失或非数组 → 该空间降级为 `unavailable`，不抛到工具层**（`result.results.length` 在 `try` 内） | [A+] `P:2431-2448` |
| 每个空间的 `hint` 被拼成 `"<空间名>: <hint>"`，多空间用 `\n` 连接后作为最终 `hint` | [A+] `P:2465,2558` |
| 抛错时 Source 自己造 hint：`unavailable: <error.message>`，而 `sources[].hint` 只放原始 message | [A+] `P:2444-2446` |
| `mode==='smart'` 且无任何过滤条件时，Source 可能**再调一次 `search`**（换 query、`mode:'keyword'`）做补救；**只对 native 空间做**，失败静默忽略 | [A+] `P:2450,2478-2502` |
| 跨 Provider 有多个 `provider.id` 时写 `federatedScore = 1/(60+rank)`，且**排序改成按 Provider 返回顺序** | [A+] `P:2467-2470` |
| 候选量由策略放大：`candidateLimit = min(50, max(limit, ceil(limit×candidateMultiplier)))`，默认 ×3 | [A+] `P:1460-1462,2398,3454` |

### 3.2 `list`（`MemoryListRequest`）

| 字段 | 类型 | 必填 | 调用方归一化 / 后果 | 证据 |
|---|---|---|---|---|
| `query` | `string` | 否 | Source 侧 `trim()`，>500 抛错；**只用于本地过滤**（`content`/`id` 子串，大小写不敏感） | [A+] `P:2668-2670,2694`、`PC:447-452` |
| `category` | `Category` | 否 | 枚举校验，非法抛错；同样在 Source 侧过滤 | [A+] `P:2671,2694` |
| `limit` | `number` | 否 | 默认 200，**必须整数且在 1..1000，否则抛错**（不是钳制）；最终结果再被 `slice(0, limit)` | [A+] `P:2672,2713`、`MS:71-74` |
| `memoryBodyIds` | `string[]` | 否 | **在 Source 层消费**，不进 Provider 请求 | [A+] `P:2673` |

丢弃/崩溃规则（**关键，03 未覆盖**）：
- 返回项缺 `content`：**只有带 `query` 时才炸**——过滤表达式先短路 `query === ""`，无查询时不会碰 `item.content` [A+] `P:2694`。带查询时 `TypeError` 落在 `try` 内 → 该空间 `unavailable` [A+] `P:2700-2705`。
- 返回项缺 `id`：同上，`item.id.toLocaleLowerCase()` 抛错。
- 返回非数组：`.filter` 抛错 → `unavailable`。
- `capabilities.browse === false` 时**永不调用 `list`**；若同时 `search === true`，Source 改调 `search({query, limit})`（`query` 为空则直接返回 `query-required` 而不调用任何方法）[A+] `P:2675-2690`。

### 3.3 `graph`（`MemoryGraphSnapshot`）

| 字段 | 必填 | 语义 / 后果 | 证据 |
|---|---|---|---|
| `nodes[]` | ✅ | `GraphNode extends Insight`，每个节点额外 `color: string`；Source 覆盖写 `graphId = "<空间id>:<node.id>"` | [A+] `PC:381-388`、`P:2644-2648` |
| `edges[]` | ✅ | `{sourceId, targetId, label, color, type?}`；`type` 仅 `temporal\|semantic\|causal\|entity\|scope` | [A+] `PC:389-395`、`PC:240-241` |
| `generatedAt` | ✅（类型） | Source 返回时**用自己的时间覆盖**，Provider 给的值不进最终响应 | [A+] `P:2655-2658` |
| `memoryBodies` / `sources` | 否 | Source 也全部自己重写，Provider 给了会被覆盖 | [A+] `P:2659-2664` |

- 缺失 `nodes`/`edges` → `snapshot.nodes.length` 抛错，落在 `try` 内 → 该空间 `unavailable`，其余空间照常 [A+] `P:2622-2637`。
- **`capabilities.graph === false` 时永不调用**；Source 用 `browse` 做平铺投影（`projection`），否则 `query-only` / `unsupported` [A+] `P:2607-2621`。
- 失效标记**不能塞 `edge.type`**（枚举固定 4+1 值），只能进节点 `tags` 或 `label` [A] §15.5、[A+] `PC:394`。

### 3.4 `status`（`ProviderSpaceStatus`）

| 字段 | 必填 | 语义 | 证据 |
|---|---|---|---|
| `healthy` | ✅ | 布尔；非布尔/缺失 → 空间视图里 `healthy` 变 `undefined`（UI 视为异常） | [A+] `PD:39-43`、`P:2021-2024` |
| `error` | 否 | 人类可读失败原因，直接进空间视图 `error` | [A+] 同上 |
| `stats` | 否 | `MemorySpaceStats { totalInsights, deletedInsights, edgeCount, oplogCount, dbSizeBytes, byCategory, topEntities[] }` | [A+] `PC:306-317` |

- **调用频率最高**：每次空间目录刷新都会对每个空间调一次；`spaces(signal)` 会合并并发但**不缓存结果** [A+] `P:1992-2032`。
- 实体读路径会调 `status(body)`（**不带 signal**）并缓存，`maxAgeMs` 复用窗口 [A+] `P:2866-2882`。
- 抛错 → 被 catch 成 `{healthy:false, error}`，**不会**让目录接口失败 [A+] `P:2014-2019,2873-2875`。
- **无超时保护**（见 §5.2）：`status` 自己慢就会拖慢整个空间目录。

### 3.5 `discover`（`ProviderMemorySpace[]`）

| 字段 | 必填 | 规则 | 证据 |
|---|---|---|---|
| `externalId` | ✅ | 非空、`trim` 后 ≤2000；**同一次返回内重复 → 整体抛错** | [A+] `P:402-404,188` |
| `name` | ✅（空则回退） | 空时回退到连接里的就近字段或 `"<label> <externalId>"`；**截断到 100 字符** | [A+] `P:189-196` |
| `description` | 否 | 空时自动生成 `"<label> memory namespace mapped from <externalId>."`；截断到 1000 | [A+] `P:193-196` |
| `connection` | ✅（可为 `{}`） | 经 `providerCatalog.normalizeMemory` 校验：**未在 `fields[scope=memory]` 声明的键直接抛错** `unsupported <label> memory setting: <key>` | [A+] `P:405-409`、`PJ:87-89` |

- 空间 id 由 Source 生成：`<providerId>-<sha256(externalId) 前 24 位>`，撞号加后缀；**不是 Provider 能指定的** [A+] `P:411-417`。
- 空间 id 必须匹配 `^[a-zA-Z0-9][a-zA-Z0-9_-]*$`（`MEMORY_SPACE_ID_PATTERN`）[A+] `P:232-235`。
- 已挂载空间的 `externalId` 不变时，保留本地 `name`/`description`（若 `metadataSource !== "provider"`）与 `active` 状态 [A+] `P:420-432`。
- 服务级字段支持 `discoveryDefaultFrom`：从第一个发现的空间里回填空着的服务字段 [A+] `P:434-441`。
- **`discover` 缺失 → 服务保存直接失败**（`P:3078`），而"启用第三方空间"必须先保存服务配置 → **UI 路径绕不过 `discover`**（只有手改 `state\memory-providers.json` 才能绕过）；`rediscover` 路径遇到缺失是静默跳过 [A+] `P:3078,3117`。

### 3.6 `metadataSample`（可选，几乎只有 native 用）

- 调用点固定 `limit = 6`；返回结果再被 `slice(0, 6)` [A+] `P:2569,2594`。
- 每条 evidence：`content` 截到 **720 字符**（721 处加 `…`）、`category`、`entities` 取前 **8** 个 [A+] `P:2594-2598`。
- 非 native Provider 走 `METADATA_SEARCH_FIRST_PROVIDERS` 白名单或 `!browse` → `search({query: description 或 name 的前 400 字, mode:'basic', limit:6})`；否则 `list({limit:6})` [A+] `P:2572-2586`。
- **这条路径没有任何 try/catch**：Provider 抛错 → RPC `internal` 错误冒到调用方（见 §5.1）[A+] `P:2566-2599`。

### 3.7 `entityIndex`（可选）

- `ProviderEntityIndex { memories: Insight[]; complete: boolean }`；`complete=false` 会让 UI 出现 `"The Provider indexed only part of this space."` [A+] `PD:55-60`、`P:2836-2837`。
- 每条 memory 会被 `entityMemory()` **剥掉检索字段**（`score`/`normalizedScore`/`relevanceTier`/`federatedScore`/`confidence`/`intent`/`matchedVia`/`depth`/`edgeType`），只留列表语义 [A+] `P:2914-2917`。
- 实体键归一化用 `normalizeEntityKey`（大小写/拼写归一）；Provider 只需给 `entities: string[]` [A+] `PC:245-249`。
- **`capabilities.entities === false` → 永不调用**，实体页直接 `unsupported` [A+] `P:2815-2819`。

### 3.8 `get`（可选，但决定 `forget`/`related` 能否按 id 用）

- Source 只在"要用 id 定位归属空间"时调用：`locate()` 遍历候选空间，逐空间 `provider.get(body, exact)` [A+] `P:2981-2993`。
- **返回值里的 `id` 必须与请求的 `id` 严格相等**，否则那条被**静默丢弃**：`if (insight !== undefined && insight.id === exact)` [A+] `P:2991`。
- 返回 `undefined` = "这个空间没有这条"，是正常语义，不报错 [A+] `PD:96-100`。
- **无 try/catch**：抛错会冒泡到工具层 [A+] `P:2990`。

### 3.9 `related`（可选）

| 参数 | 约束 | 证据 |
|---|---|---|
| `id` | 非空、≤2000 | [A+] `P:3000` |
| `depth` | 默认 2，**必须整数且在 1..5，否则抛 `value must be an integer within 1..5`**（不是钳制） | [A+] `P:3000`、`PD:101` |
| `edge` | 可选，必须是 `EDGE_TYPES` 之一，否则抛错 | [A+] `P:2997`、`PC:240-241` |
| 能力门禁 | `capabilities.related === false` **或**方法缺失 → 抛 `… does not support related-memory traversal` | [A+] `P:2999` |

### 3.10 `link`（可选）

| 参数 | 约束 | 证据 |
|---|---|---|
| `sourceId` / `targetId` | 非空、≤2000 | [A+] `P:3009` |
| `type` | 缺省 `semantic`；必须在 `EDGE_TYPES` 内 | [A+] `P:3006`、`PC:240` |
| `weight` | 必须有限且 `0..1`，否则 `weight must be within 0..1` | [A+] `P:3005` |
| `reason` | 可选；`trim()` 后为空串 **归一成 `undefined`**（不是 `""`） | [A+] `P:3009` |
| 能力门禁 | `capabilities.link` 为假或方法缺失 → 抛 `… does not support explicit memory links` | [A+] `P:3008` |

### 3.11 `forget`（可选）

- `id` 非空 ≤2000；`capabilities.forget` 为假或方法缺失 → 抛 `… does not expose safe forget semantics in this integration` [A+] `P:3017-3018`。
- 回执走同一套判定（§4.5）；**只有判定为 committed 才会触发空间激活/`touch()`** [A+] `P:3019,3352-3357`。

### 3.12 `dispose` / `invalidateStatus`

- `dispose`：generation 销毁时**逆序**调用，异常收集后 `AggregateError` [A+] `P:1984-1990`。
- `invalidateStatus`：仅在 `reconnectSpace` 里被调用（用户点重连），`?.` 可选链 [A+] `P:2385`。

### 3.13 统一"静默丢弃"清单（把 03 与本节的边界钉死）

| 现象 | 谁做的 | 后果 | 证据 |
|---|---|---|---|
| `Insight.id` 缺失/空/超 1000 字符 | **策略层 S**，不是宿主 | `id.length===0 或 >1000` → `continue`，**静默丢** | [A+] `S:95` |
| `Insight.content` 缺失 | **策略层 S** 直接 `truncateMemoryText(undefined)` → **TypeError** | 工具调用失败，**不是静默** | [A+] `S:105`、`S:81-87` |
| `Insight.content` 为空串 | 策略层 | 静默丢（`content === ""` → `continue`） | [A+] `S:106` |
| `createdAt` 非 ISO 字符串 | Provider 自己的归一化（各包自实现） | 宿主原样透传（`S:120` 只做截断），排序键会退化；**"给数字被静默丢弃"是 Provider 侧 normalize 的行为**，官方 honcho 用 `jsonString(item?.created_at)` 取时间，数字直接被丢、字段整个消失 `honcho\lib\index.js:11` | [A] §7 坑 4；[A+] `S:120`、`honcho\lib\index.js:4-21` |
| `score` 缺失 | 质量策略 | tier `unknown`，strict-v1 下最多 2 条、策略每回合只收 1 条 | [A+] `P:1464-1469,1526-1531`、`S:54` |
| `tier === "low"` | 策略层 | 静默丢 | [A+] `S:97` |
| `medium` / `unknown` 超配额 | 策略层 | 每回合各 **1** 条（不是 2；2 是 Source 策略的 `maxUnknownResults` 默认值） | [A+] `S:53-54,98-99`、`P:3457` |
| 相同 `id` 或相同 `content` 摘要 | 策略层 | 去重后静默丢 | [A+] `S:100-102,57-59` |
| `list` 返回项缺 `content`/`id` 且带 query | Source | 该空间整体 `unavailable`（不是丢单条） | [A+] `P:2694,2700-2705` |
| `get` 返回的 `id` ≠ 请求 id | Source | 单条静默丢 | [A+] `P:2991` |

> **对 03 的一处收紧**：03 §1.3 把"`id`/`content` 缺失 → 静默丢弃"标为宿主行为。**严格来说宿主不校验也不静默**——`id` 的丢弃发生在策略层（`S:95`），`content` 缺失会造成 TypeError 而不是安静丢弃。真正"静默 return undefined + filter"的是**各 Provider 自己的 normalize**（例如 honcho 的 `insight()`），HANDOFF §7 坑 4 说的正是这一层。详见 §9。

---

## 4. 写入路径（必答 3）

### 4.1 调用链

```
主 Agent: mnemon_remember ─→ coordinator.remember ─→ write() ─→ delegate(spawn) 起写 worker 子 Agent
                                                                    └─ 子 Agent 内调 mnemon_remember → sourceAction 直连 RPC（不再嵌套）
子 Agent（任意）: mnemon_remember ─→ sourceAction → /dsh-mnemon-write `remember` → Source.remember → adapter.remember
```

- "每次写入本身是一次子智能体运行"（HANDOFF §5）**在主 Agent 路径成立**：`write()` 里 `this.delegate(parent, "write", …)` [A+] `M:10867-10882`；子 Agent 路径**不再套娃**（`isSubagent(exec.agent) ? sourceAction(...) : coordinator.remember(...)`）[A+] `M:15954`。→ HANDOFF §5 的描述对主路径准确，但对"写 worker 内的工具调用"要按直连理解（否则会以为递归）。
- Source 侧入口：`remember(request, signal)` → `assertWritable()` → `prepareRemember()` → `provider.remember()` → `activateAfterWrite()` → `annotateResult()` [A+] `P:2922-2928`。

### 4.2 `RememberRequest` 完整字段表（含宿主归一化，**这是 Provider 实际收到的值**）

| 字段 | 类型 | 必填 | Host 归一化（`prepareRemember`） | 证据 |
|---|---|---|---|---|
| `content` | `string` | ✅ | `trim()` 后非空，**≤8192 字符**，否则抛错 | [A+] `P:3313,1812-1817` |
| `category` | `Category`(6 值) | ✅（宿主补） | 枚举校验；缺省 **`'general'`** | [A+] `P:3315,236-237` |
| `source` | `'user'\|'agent'\|'external'` | ✅（宿主补） | 枚举校验；缺省 **`'user'`** | [A+] `P:3316,238-239` |
| `importance` | `number` | 否 | 给了就必须是 **1..5 整数**，否则抛错；缺省不发该键（由 Provider 决定默认） | [A+] `P:3314`、`PC:300` |
| `tags` | `string[]` | 否 | 每个 `trim`、去空；**≤20 个**；**任一含英文逗号 → 抛错**；全空 → 不发该键 | [A+] `P:3317,1822-1828` |
| `entities` | `string[]` | 否 | 同上，**≤50 个**，禁逗号 | [A+] `P:3318` |
| `memoryBodyId` | `string` | ✅（宿主补） | 目标空间 id；缺省时要求"活动空间恰好 1 个"，否则抛 `memoryBodyId is required when the number of active memory spaces is not exactly one` | [A+] `P:3299-3308,3326` |

**扩展位：没有。** 三个白名单（RPC 输入 → Source `prepareRemember` → Provider）逐层重建对象，任何自定义键（`metadata`/`extra`/`session_id`/`client_write_id`/`valid_at`…）都进不去 [A] §7 坑 1；[A+] `P:3319-3330`、`P:3666`。
**RPC 层与 Source 层上限不一致（坑）**：RPC 允许 `content` 到 **100000**（`memoryInputText(input.content, "content", 1e5)`），但 `prepareRemember` 只允许 **8192**，超过即在 Source 层抛错 [A+] `P:3666` vs `P:3313`。→ 第三层不要依赖 100k，按 8192 设计。

### 4.3 `content` 的结构化编码约定（引用 HANDOFF §23，不重写）

`RememberRequest` 无扩展位，所以**"给人读的一句话 + 给 shim 建图的三元组"只能共用一个 `content` 字符串** [A] §23.1。定案格式（HANDOFF §23.2）：

| 约定 | 值 |
|---|---|
| 载体 | `content` 的**最后一段**，前面至少一个空行 |
| 围栏 | 三反引号 + 语言标记 **`fact`**（不是 `json`） |
| 唯一性 | 一段 content 内**最多一个**块 |
| `v` | `1` |
| `valid_at` | ISO 8601，**必须带时区**，UTC 优先 |
| 可选键 | `subject` / `predicate` / `object` / `supersedes` |
| 缺失或解析失败 | **不拒绝写入**：整段文字当事实、`valid_at`=收到时间、回执带 `warning:"fact-block-missing"` 并计数 |
| 解析者 | **shim（Python），不是插件**；插件只做管道，逐字转发 |
| 剥离时机 | **进 Graphiti 之前**剥离，避免 `{"v":1,…}` 污染 embedding；原文含块存入 `source_content` |

证据：[A] §23.1–§23.5。**与源码的一致性**：Source 对 `content` 只做长度/非空校验，**不解析也不改写内容** [A+] `P:3313`；因此"插件只当管道"这一条在 0.5.37 依然成立，格式升级不需要重新发布插件。**唯一新增约束**：`content` 上限 8192（§4.2），围栏块与正文合计不能超。

### 4.4 写入回执：`remember` 返回什么才算"写成功"

Source 用**启发式**把任意 JSON 回执翻译成 5 个状态之一（`mutationResultCompletion`），**不认 HTTP 状态码、不认自定义字段**：

| 判定顺序 | 条件 | 结果 | 证据 |
|---|---|---|---|
| 1 | 状态串含 `partial`，或 `errors` 非空 **且** 有计数 > 0 | `partial` | [A+] `P:1880` |
| 2 | `success===false` / `ok===false` / `errors` 非空 / 状态串 ∈ {`canceled`,`cancelled`,`error`,`failed`} | `failed` | [A+] `P:1881,1854-1859` |
| 3 | 状态串 === `candidate` | `candidate` | [A+] `P:1882` |
| 4 | 状态串 ∈ {`accepted`,`pending`,`processing`,`queued`,`running`} | `accepted` | [A+] `P:1883,1847-1853` |
| 5 | `committed===false` / `durable===false` / 状态串 === `skipped` | `unknown` | [A+] `P:1884` |
| 6 | `committed===true` / `durable===true` / 状态串 ∈ 16 词 / 计数字段 > 0 | **`committed`** | [A+] `P:1885,1829-1846` |
| 7 | 其它（含**空对象、只有 `id` 的对象**） | **`unknown`** | [A+] `P:1886` |

- 状态串来源：`action` → `status` → `state`（按序取第一个字符串，`trim().toLowerCase()`）[A+] `P:1873-1877`。
- committed 16 词：`added, committed, completed, created, deleted, forgotten, imported, invalidated, linked, merged, removed, replaced, stored, succeeded, success, updated` [A+] `P:1829-1846`。
- 计数字段：`created, deleted, edges_inserted, imported, removed, stored, updated`（必须 `>0`）[A+] `P:1860-1868`。
- **只有 `committed` 触发 `activateAfterWrite`** → 才会 `invalidateEntityIndex` + 自动激活/`touch()` 空间 [A+] `P:2926,3352-3357,1888-1890`。
- **`accepted` 永不触发激活**：与 HANDOFF §22.2 结论一致 [A] §22.2 ↔ [A+] `P:1883-1889`。
- **上游还有第二道闸**：主 Agent 的写工具结果要靠 `writeLanded(receipt)` 才算"落地"，`writeLanded` = committed **或** 状态串 ∈ {`accepted`,`queued`,`pending`,`processing`}；不满足时 `action` 被改写成 **`unknown`**、`memoryBodyIds` 为空 [A+] `M:10303-10311,10899-10908`。→ **回执缺 `action` 字段 → 记忆其实写进去了，但工具告诉模型"unknown、没有空间"**。这是第三层最容易踩的坑。

### 4.5 幂等键在哪：**契约里没有**

- `RememberRequest` 的全部字段见 §4.2，**没有任何"客户端写入 id"**；Source 也不生成、不比对 [A+] `PC:296-305`、`P:3309-3331`。
- 因此**幂等只能由 Provider 自己定义**：可选做法是按 `(memoryBodyId, 规范化 content, category, source)` 做确定性摘要当幂等键；代价是"同一个人真的写两条一模一样的记忆"会被合并 [推断，需产品决策]。
- HANDOFF §22.3 要求"必须加落盘 outbox"、§23.3 指出"写 worker 不会自动重试"——两者都因为**契约层没有重试与幂等位**，只能由第三层与 shim 共同承担 [A] §22.3、§23.3；[A+] 无重试见 §5.3。
- 事实层现有 `client_write_id` **没有上游来源**，只能在第三层自造（§7 差距 #W2）。

### 4.6 `rememberMany` 的额外约定

| 约定 | 后果 | 证据 |
|---|---|---|
| 返回数组**长度必须等于请求数** | 不等即抛 `batch remember did not return one receipt per request for Memory Space <id>`，整批失败 | [A+] `P:2959` |
| 顺序必须一一对应 | Source 按 `written.entries()` 下标回填到原请求位 | [A+] `P:2960-2964` |
| 只对 native 做 8000 字符切分；第三方 Provider 的批次**不做切分** | 若某条 `content` 过长，由 Provider 自己决定 | [A+] `P:2956` |
| 同一个 target 的请求会被分组；不同空间分批调 | — | [A+] `P:2939-2951` |
| 未覆盖的请求逐条回落 `remember` | — | [A+] `P:2966-2972` |

---

## 5. 失败与降级（必答 4）

### 5.1 Provider 抛错时的后果矩阵（**逐个方法，不是一句话**）

| 方法 | 有 try/catch？ | 抛错的可见后果 | 证据 |
|---|---|---|---|
| `search` | ✅ 每空间独立 | 该空间 `status:"unavailable"`、`hint:"unavailable: <msg>"`、结果 0 条；**其它空间照常**；模型侧 `evidence.unavailable` 只在结果为空时才看到（03 §4.1） | [A+] `P:2438-2448` |
| `list` | ✅ 每空间独立 | 该空间 `unavailable` + hint，其余照常 | [A+] `P:2700-2706` |
| `graph` | ✅ 每空间独立 | 该空间 `unavailable` + hint，其余照常 | [A+] `P:2629-2637` |
| `entityIndex` | ✅ 每空间独立 | 该空间实体读 `unavailable` | [A+] `P:2845-2851` |
| `status` | ✅ 调用处兜底 | `{healthy:false, error}`，目录不失败；空间显示不健康 | [A+] `P:2014-2019,2873` |
| `discover`（保存配置时） | ❌ | 整个保存操作失败，RPC 返回 `internal` + 原文 | [A+] `P:3079` |
| `discover`（重扫时） | ✅ | 该 provider 记 `error`；超时额外记 `timedOut:true` + `did not answer within <N> seconds`，空间不动 | [A+] `P:3136-3157` |
| `remember` | ❌ | 写失败，错误冒到工具/RPC（`internal`） | [A+] `P:2925` |
| `rememberMany` | ❌ | 同上（含长度不匹配） | [A+] `P:2958-2959` |
| `get` | ❌ | 冒到调用方（id 定位失败） | [A+] `P:2990` |
| `related` / `link` / `forget` | ❌ | 冒到工具/RPC | [A+] `P:2995-3021` |
| `metadataSample` | ❌ | 冒到 RPC | [A+] `P:2566-2599` |
| `dispose` | ✅ 聚合 | `AggregateError("Memory Space Provider disposal failed")` | [A+] `P:1984-1990` |

**结论**：**读路径全部"降级为空 + hint"，写路径全部"失败即失败"**。这就是为什么 03 §4.1 的"状态与结果不可兼得"只对读成立；写侧没有"部分成功"的自动补偿。

### 5.2 超时与取消

| 事实 | 证据 |
|---|---|
| Source **不给单次调用加超时**（`search`/`remember`/`graph`/… 直接透传调用方的 `signal`） | [A+] `P:2432,2925,2623` 等 |
| 唯一内置超时：重扫 `discover` 的 **30 s**，`AbortSignal.timeout` + `AbortSignal.any` 与外部 signal 合并 | [A+] `P:1687,3119-3123` |
| `config.timeoutMs`（默认 10000）**不作为 Source 侧超时**，只经 factory context 交给 Provider：`context.config.timeoutMs` | [A+] `P:1972-1977,3368,3489` |
| Provider 必须自己实现超时：官方 `HttpMemoryProvider` 默认 **15 s**，且把超时转成 `"<label> request timed out after <ms>ms"` | [A+] `PS.js:46,76,99` |
| 取消要**尊重 `signal`**：Source 在 RPC/工具层会传 `AbortSignal`；`HttpMemoryProvider` 已做 relay + `throwIfAborted` | [A+] `PS.js:71-74` |
| 进程类 Provider：`runProcess` 有 `timeout` / `abort` / `output-limit` 三类失败，错误带 `reason` 字段，最终被 Core 映射进 `RpcError.details.reason` | [A+] `PS.js:39-62`、`P:1166-1174` |

### 5.3 有没有重试：**没有**

- Source 与 Core 全链路没有任何重试/退避代码（本次在 `P` 与 `M` 全文检索 `retry/attempt` 无命中）[A+]。
- 唯一"再试一次"的形态是 `search` 的 **smart 模式补救查询**（换 query 再调一次）与 `discover` 的**用户手动重扫**，都不是自动重试 [A+] `P:2450,3090`。
- HANDOFF §23.3 的"写 worker 不会自动重试"由此得到源码级确认 [A] §23.3 ↔ [A+] 本节。

### 5.4 `hint` / `unavailable` 语义 → **引用 03 §4.1**

- `ProviderSearchResult.hint` **只在 `results` 为空时**才会变成模型的 `evidence.unavailable`；有结果时 hint 在路由层被丢弃 [A+] `P:4110`（03 §4.1 已系统化，不重复）。
- 补充一条 03 未写的：**抛错路径的 hint 带 `unavailable: ` 前缀**（`P:2444`），**Provider 自己返回的 hint 不带前缀**（`P:2465`）。→ 模型可以据此区分"Provider 说没命中"和"Provider 挂了"，但**键名相同**，文案必须自带语义（03 §7 待验证 8 的建议仍成立）。

### 5.5 错误面（RPC）

| 事实 | 证据 |
|---|---|
| Provider 抛出的 `Error` → `{ok:false, error:{code:"internal", message, details}}` | [A+] `M:14366-14376` |
| `ProcessError.reason` ∈ {`launch`,`timeout`,`aborted`,`output-limit`} 会被塞进 `details.reason` | [A+] `PS.d.ts:54-58`、`M:14366-14374` |
| 参数/入参形状问题 → `code:"bad-request"` + `details.issues` | [A+] `M:14377-14386` |
| 读/写端点还有**能力门禁**：例如 `search` 需要 `recall`、`forget`/`body-delete` 需要 `forget`、`provider-service-update`/`embedding-fill` 需要 `maintain` | [A+] `M:14387-14409` |

---

## 6. manifest / adapter 声明（必答 5）

### 6.1 `MemorySpaceProviderManifest` 字段表（**逐字段校验，写错装配即失败**）

| 字段 | 类型 | 校验 | 证据 |
|---|---|---|---|
| `apiVersion` | 字面量 | 必须 === `"dsh-mnemon.memory-space-provider/v1"`，否则 `unsupported Memory Space Provider API` | [A+] `PD:111,118`、`PG:3,165` |
| `kind` | 字面量 | 必须 === `"provider"` | [A+] `PG:166` |
| `typeId` | `string` | `^[a-z][a-z0-9-]{0,127}$`；**必须等于子模块 `module.id`** | [A+] `PG:4-10,167`、`PJ:344` |
| `packageName` | `string` | 非空，≤500 | [A+] `PG:168`、`PG:11-16` |
| `version` | `string` | 非空 ≤100；**不校验 semver**（官方填包版本号，如 `"0.5.5"`） | [A+] `PG:169`、`dsh-mnemon-provider-honcho\lib\index.js:147` |
| `label` | `string` | 非空 ≤100 | [A+] `PG:170` |
| `icon` | `{kind,value}` | `brand` 走 id 正则；`glyph` ≤16 字符；`data-url` 必须是 `data:image/(png\|jpeg\|webp\|svg+xml);base64,…` 且 ≤200000 | [A+] `PD:17-21`、`PG:22-32` |
| `summary` | `string` | 非空 ≤1000 | [A+] `PG:172` |
| `summaryI18nKey` | `string?` | 可选 ≤200 | [A+] `PG:173` |
| `origin` | `'native'\|'third-party'` | 第三方固定 `"third-party"` | [A+] `PG:175` |
| `locality` | `'local'\|'remote'` | 描述的是数据边界；映射到 descriptor 的 `kind` | [A+] `PG:174`、`PJ:300` |
| `workspaceBinding` | 三值 | `'automatic'\|'optional-override'\|'provider-global'`；远程服务通常 `provider-global` | [A+] `PG:176-180`、`PD:125` |
| `capabilities` | 对象 | 见 §6.3，8 个布尔 + 2 个枚举，逐项校验 | [A+] `PG:33-53` |
| `fields` | 数组 | 见 §6.2，键正则、去重、输入类型、默认值、选项、`discoveryDefaultFrom` 全校验 | [A+] `PG:54-135` |
| `secrets` | `string[]` | **必须与所有 `input:'secret'` 字段的 key 集合完全一致**（排序+规范化后逐字比较），否则 `secrets must exactly match secret config fields` | [A+] `PG:188-192` |
| `scoreSemantics` | 三值 | `'normalized-relevance'\|'provider-native'\|'none'` | [A+] `PD:133`、`PG:181-185` |

- 整份 manifest 会先做 **JSON 安全性 + 深度 ≤64 + 无环 + 无 class 实例** 校验，再 `deepFreeze` [A+] `PG:136-161,163-164,193`。
- `definition` 必须是 `{ manifest, create }` 且 `create` 是函数 [A+] `PG:195-201`。
- **比较稳定的写法**：官方 honcho 的做法是「descriptor 常量 + `defineMemorySpaceProviderDefinition` + `defineMemorySpaceProvider` 两行装配」，manifest 逐字段从 descriptor 展开 [A+] `dsh-mnemon-provider-honcho\lib\index.js:141-167`。

### 6.2 `fields`（连接字段）的关键约束

| 约束 | 说明 | 证据 |
|---|---|---|
| `key` | `^[a-z][a-zA-Z0-9_-]{0,127}$`，同 manifest 内唯一 | [A+] `PG:5,17-21,59-62` |
| `scope` | **`'service'`（一次配置，属于 dsh-mnemon 的 Plugins 页）或 `'memory'`（每个 Memory Space 各配一份）** | [A+] `PG:65`、`PC:33-34` |
| `input` | `text\|url\|secret\|number\|boolean\|select\|path` | [A+] `PG:66-74` |
| `required` | 必须显式布尔 | [A+] `PG:75` |
| `role: 'global-location'` | 只能是 **service + path**，用于"全局数据位置"复用 UI | [A+] `PG:76-77` |
| `select` | 必须有非空 `options`，`value` 唯一 | [A+] `PG:113-123` |
| `min`/`max`/`maxLength`/`pattern`/`validationMessage`/`normalize` | 都有类型+范围校验；`normalize` 只支持 `'trim-trailing-slash'` | [A+] `PG:78-112` |
| `defaultValue` | 必须与 `input` 类型匹配；`select` 还必须在 options 里 | [A+] `PG:124-126` |
| `discoveryDefaultFrom` | 必须指向一个 **memory + 非 secret** 字段；service 字段借此从发现结果回填 | [A+] `PG:128-133,434-441` |
| 保存时的归一化 | 未知键 → `unsupported <label> <scope> setting: <key>`；`url` 强制 http(s) 且禁内嵌凭据；`maxLength` 默认 secret 8000 / 其它 2000 | [A+] `PJ:9-31,37-43` |
| 秘钥可见性 | 对外只给 `configuredSecrets: string[]`（只有名字，**没有值**）。`MemoryProviderServiceView.secretValues` 虽然定义了，但 `providerServices(options)` 在本版本**没有任何调用点传 `includeSecrets:true`**，所以**明文秘钥既不进 GUI 也不进 RPC**；唯一能拿到值的地方是 Provider 自己调 `memorySpaces.providerConnection()` | [A+] `PJ:125-141`、`P:355-374`、`PC:141-142` |

### 6.3 `capabilities` 与它能"解锁"什么

`MemoryProviderCapabilities`：8 个布尔 `search, browse, graph, entities, related, remember, link, forget` + `writeMode: 'exact'|'async-extracting'` + `deletionMode: 'soft'|'hard'|'unsupported'` [A+] `PC:105-116`。

| 能力位 | Source 内部行为 | RPC 门禁 | 证据 |
|---|---|---|---|
| `search` | 允许 `search`；否则该空间 `unsupported` + `"This provider does not expose search."` | 读端点 `search`/`entities` 需要 `recall`/`search` | [A+] `P:2423-2430`、`M:14390-14391` |
| `browse` | 允许 `list`；否则 `query-only`/`unsupported` | 读端点 `list` 需要 `browse` | [A+] `P:2675-2685`、`M:14389` |
| `graph` | 允许 `graph`；否则退化成 browse 投影或 `query-required` | `graph` 需要 `graph` | [A+] `P:2607-2621`、`M:14388` |
| `entities` | 允许实体页；**不强制 `entityIndex` 方法** | — | [A+] `P:2815-2819` |
| `related` | 允许 `related` | `related` 需要 `related` | [A+] `P:2999`、`M:14392` |
| `remember` | 允许写 | 写端点 `remember` 需要 `write` | [A+] `PJ:265`、`M:14400` |
| `link` | 允许 `link` | 写端点 `link` 需要 `link` | [A+] `P:3008`、`M:14401` |
| `forget` | 允许 `forget` | `forget`/`body-delete` 需要 `forget` | [A+] `P:3017`、`M:14402-14405` |
| `writeMode: 'exact'` | 表示"写进去的就是写进去的"（无后台 LLM 抽取）；`'async-extracting'` 表示可能异步提炼，此时回执多为 `accepted` | 语义字段，Source 不做分支判断 | [A+] `PC:114`、[推断] 分支未见于 `P` 全文 |
| `deletionMode` | `soft`/`hard`/`unsupported`；`unsupported` 时**不要**声明 `forget:true` | 语义字段 | [A+] `PC:115` |

### 6.4 `scoreSemantics`：**两处声明，含义不同**（最容易写错的一处）

| 位置 | 类型 | 用途 | 证据 |
|---|---|---|---|
| manifest 顶层 | `'normalized-relevance'\|'provider-native'\|'none'` | 进 descriptor、进 generation digest、进装配断言 | [A+] `PD:133`、`PJ:254,270` |
| adapter 只读属性 | `{ kind: 'normalized-relevance'\|'provider-native' }` | **recall 质量策略实际读的就是它** | [A+] `PD:75,48-53`、`P:2455,1475-1484` |

- 两处都声明 `normalized-relevance` 才会被当成 0..1 校准分（阈值 0.25 / 0.6）[A+] `P:1491-1513`。
- **manifest 写 `normalized-relevance` 但 adapter 没声明 → 装配直接失败**（`declares normalized scores but its runtime does not`）[A+] `PJ:270`。
- **反过来不校验**：manifest 写 `'none'`、adapter 声明 `provider-native` → 装配通过，但运行时按 `provider-native` 定档（全部 `ranked`，strict-v1 只放 `maxRankedResults` 默认 **4** 条）[A+] `P:2455,1469,3456`。→ **两处必须一致，且第三层应选 `provider-native`**（03 §6 P0 已论证：声明 normalized 会让 RRF 分 0.016 全部低于 0.25 被 drop）。
- adapter 完全不给 `scoreSemantics`：分数被当 "unscaled" → tier `unknown` → 每回合只收 1 条 [A+] `P:1480-1484`、`S:54`。

### 6.5 从 manifest 到 descriptor / 实例 id 的投影

- `descriptor.id = instanceId`；**当 `instanceId !== manifest.typeId` 时才额外带 `typeId`** [A+] `PJ:295-299`。
- runtime 的 `adapter.id` 被 Proxy 改成 **instanceId**，其余方法 `bind` 到原 target [A+] `PJ:273-279`。
- 因此 `MemorySpace.provider.id` 是 **instanceId**；`body.provider.typeId` 才是 manifest 的 `typeId`。Provider 里比较自身身份时用 `body.provider.typeId ?? body.provider.id`（官方 `HttpMemoryProvider.connection()` 就是这么写的，不符则抛 `… cannot serve provider …`）[A+] `PS.js:58-61`。
- generation digest 只取语义字段（label/icon/summary 之外的一组），**改 label/icon/summary 不会导致 View 重算** [A+] `PJ:227-256`。

### 6.6 Source 传给 `create()` 的上下文（Provider 能从哪儿拿东西）

| 成员 | 内容 | 证据 |
|---|---|---|
| `memorySpaces` | `MemorySpaceAuthority { runner.effectiveDataDir(), list(), providerConnection(id, expectedProviderId) }` | [A+] `PD:5-11`、`P:1972-1977` |
| `memoryBodies` | **已废弃别名**，同一对象；新代码只用 `memorySpaces` | [A+] `PD:29-32` |
| `config` | `{ timeoutMs, defaultRecallLimit? }`（即 Source 的解析后 config，含更多字段） | [A+] `PD:33-36`、`P:1975` |
| `nativeRunner` | 仅 native 用（`runJson`/`runText`，带超时与输出上限） | [A+] `PD:23-27`、`P:1976` |
| `providerInstanceId` | 该子模块在本 Source 内的稳定实例 id | [A+] `PD:135-139`、`PJ:313-317` |
| `manifest` | 冻结后的清单 | 同上 |

**秘钥怎么拿**：`body.provider.settings` 只含 **memory scope 的非 secret 值**，外加 `configuredSecrets: string[]`（只有名字）[A+] `P:1027-1043`。要拿值必须走 `memorySpaces.providerConnection(spaceId, providerId)`，它把 service 字段 + memory 字段 + 空间级覆盖合并后做完整归一化 [A+] `P:322-338`、`PJ:90-124`。

### 6.7 现成基类 `HttpMemoryProvider`（能省什么、不能省什么）

| 已提供 | 证据 |
|---|---|
| 超时（默认 15 s，可传 `requestTimeoutMs`）、`signal` 中继、`throwIfAborted` | [A+] `PS.js:41-47,71-77` |
| JSON 解析容错（非 JSON 响应保留原文为字符串） | [A+] `PS.js:86-92` |
| HTTP 错误映射：`<label> HTTP <status>: <message\|error\|detail>` | [A+] `PS.js:25-32,93-96` |
| 默认 `graph()`：拿 `list({limit:200})` 转成节点、`edges: []`、统一 `color` | [A+] `PS.js:48-57` |
| `connection(body)`：身份校验 + 取完整连接（含秘钥） | [A+] `PS.js:58-61` |
| `request` / `requestConnection`（相对路径、`Content-Type`、方法推断） | [A+] `PS.js:62-105` |
| 便捷取值器 `jsonObject/jsonString/jsonNumber/jsonArray/firstArray`（`firstArray` 会同时看 `data` 下一层） | [A+] `PS.js:5-24` |

| **没有**（必须自己写） | 证据 |
|---|---|
| 重试、退避 | [A+] 全文无 |
| 全局 header / 认证注入（`HttpProviderOptions` 只有 `label/fetch/requestTimeoutMs`，**秘钥不能进构造函数**） | [A+] `PS.d.ts:4-8` |
| `search` / `remember` / `list` 的实现 | [A+] `PS.d.ts:31`（只有 `list` 是 abstract，其余全部自写） |
| `discover` / `status` / `link` / `forget` / `get` / `related` | 同上 |

> 基类还有一个**必须覆盖**的默认行为：不实现 `graph()` 就会得到"把 `list()` 前 200 条当节点、边为空"的平铺图。如果 `capabilities.graph` 声明为 `true`，这个默认实现会**产出一张没有边的假图**——要么自己实现，要么把 `graph` 声明为 `false` 走 browse 投影 [A+] `PS.js:48-57`、`P:2607`。

---

## 7. 与 factlayer 现有实现的差距清单（必答 6）

以 `app/main.py` 现有端点为基准 [A+] `main.py:228-373`，对照 Provider 契约。

### 7.1 端点级

| 契约要求 | factlayer 现状 | 差距 | 证据 |
|---|---|---|---|
| `search` | `POST /search` ✅ | 返回 `{count, items, hint, hidden, token_count, routes, output…}`，**键名 `items` 而非 `results`** → 需在第三层重命名，**不能直接把 body 转给 Host** | `main.py:292-306`、`retrieval.py:677-693` |
| `remember` | `POST /memories` ✅ | 必填 `client_write_id` / `session_id` / `source_client` —— **契约三个字段都没有对应** | `main.py:56-62,238-248` |
| `get` | `GET /memories/{id}` ✅ | 可直接映射；404 语义 = `undefined` | `main.py:250-255` |
| `list` | `GET /memories?session_id=` ⚠️ | 契约的 `MemoryListRequest` 是 `{query?, category?, limit?, memoryBodyIds?}`，**没有 `session_id`、没有 offset** → 只能表达成"某个空间内的浏览"，或声明 `browse:false` 走 query-only | `main.py:257-266`、`PC:447-452` |
| `graph` | **缺失** | 无节点/边投影。回落方案：`capabilities.graph:false` + `browse:true` 让 Source 平铺投影，或 `graph:false,browse:false` → `query-only` | `P:2607-2621` |
| `status` | `GET /health` ✅ | 结构完全不同（`health_report` 是自造报告）→ 需适配成 `{healthy, error?, stats?}`，`stats` 才能填 `MemorySpaceStats` | `main.py:308-321` |
| `discover` | **缺失** | **`discover` 缺失 → 服务根本保存不了**（`P:3078`）。必须有：至少返回一个命名空间（例如按 `group_id`/空间名枚举） | `P:3078` |
| `forget` | **缺失** | 只有 `/supersedes`（作废旧说法），语义是"关系"不是"删除" → `capabilities.forget:false` + `deletionMode:'unsupported'` | `main.py:268-284`、`PC:115` |
| `link` | **缺失** | `/supersedes` + `kind` 可以映射成 `link(type='semantic', weight=1, reason=kind)`，但**契约要求 4 值枚举里的 type**，`supersedes` 不是合法 type | `main.py:65-71`、`PC:240` |
| `related` | **缺失** | 无多跳遍历。`GET /supersedes/state` 只能回答"这一对有没有边"，不足以做 depth 1..5 | `main.py:279-284` |
| `entityIndex` | **缺失** | 无实体索引；`capabilities.entities:false` 即可 | — |
| `metadataSample` | **缺失** | 可选；缺失时 Source 退回 `search(basic)` | `P:2575-2582` |
| `rememberMany` | **缺失** | 可选；缺失时逐条 `remember` | `P:2967-2972` |
| `invalidateStatus` / `dispose` | **缺失** | 可选；无长连接可关时 `dispose` 可不写 | `PD:79,105` |
| （契约无对应） | `POST /derive/tick`、`POST /admin/rebuild`、`POST /admin/backup`、`GET /admin/search`、`POST /admin/canary` | **不属于 Provider 契约**：Provider 只有 15 个方法，没有"运维端点"通道。这些只能由人/评测直接调 shim（与 03 §5 方案 A 的 admin 端点定位一致） | `main.py:323-371`、`03` §5 |

### 7.2 字段级

| 契约字段 | factlayer 字段 | 处理 | 证据 |
|---|---|---|---|
| `Insight.id` | `id`（uuid4 hex） | ✅ 直接映射；长度合规（≤1000） | `write_path.py:133` |
| `Insight.content` | `content` | ✅ 但**必须检查 ≤8192**：factlayer 允许更大，Source `prepareRemember` 会在 8192 抛错（写侧）；读侧再被策略截 1200 | `P:3313`、`S:48` |
| `Insight.createdAt` | `createdAt` = `iso_utc(recorded_at)` ✅ **另有 `recorded_at`（毫秒整数）** | **不要**把 `recorded_at` 当 `createdAt` 传：策略层对它做 `truncateMemoryText(…, 300)`，整数没有 `.length` → **TypeError**（`S:120`） | `retrieval.py:819-820`、`S:120` |
| `Insight.score` | `score`（RRF，≈0.016） | ✅ 存在即可；**必须配 `provider-native`**，否则按 0.25 阈值全部 drop | `retrieval.py:821`、`03` §6 |
| `Insight.tags` | `tags`（含 `via:`/`weak`/`hidden:N`/`superseded-by:`/`expanded`/`cycle`/`chain-too-long`） | ✅ 直接映射；注意 tag 已做数值化（`hidden:2`），**不含英文逗号**，安全 | `retrieval.py:785-810` |
| `Insight.category` / `importance` / `entities` / `source` | factlayer **没有** category/importance/entities；`source` 是 `source_client` 语义 | 缺失可接受（`category` 读侧可选）；写侧 Source 会给 `category:'general'`、`source:'user'` | `PC:250-279`、`P:3315-3316` |
| `Insight.externalUri` | 无 | 可选；如要给人点回 shim 可填 | `PC:278` |
| 额外字段 `recorded_at`/`seq`/`session_id`/`routes`/`content_truncated`/`expanded*`/`inherited_rank` | — | **必须剥掉**（或至少保证不参与 `content`/`id` 校验）：这些不在 `Insight` 里，策略层白名单会把它们全丢，留着只会增加 payload | `S:112-128` vs `retrieval.py:816-830` |

### 7.3 结构级（**这三条不做，接上就是错的**）

| # | 问题 | 后果 | 依据 |
|---|---|---|---|
| **W1** | `POST /memories` 回执是 `{id, seq, recorded_at, client_write_id}`，**没有 `action`/`status`/`committed`/计数** | `mutationResultCompletion` 判为 `unknown` → **不触发空间激活**；上游 `writeLanded` 为假 → 主 Agent 工具结果 `action:"unknown"`、`memoryBodyIds:[]`（记忆写了但模型被告知没落地） | `write_path.py:174-179`、`P:1885-1886`、`M:10303-10311` |
| **W2** | 幂等键 `client_write_id` 只能由第三层生成 | 契约无幂等位（§4.5）。用 `content` 摘要当键会合并"真的写了两遍"；用随机 uuid 则失去幂等 | §4.5 |
| **W3** | `client_write_id`/`session_id`/`source_client` 三个必填字段**在 `RememberRequest` 里没有来源** | 必须由第三层造：`session_id` 可取 `memoryBodyId` 或空间名；`source_client` 固定 `"mnemon-provider"`；`client_write_id` 按 §4.5 定策略 | `main.py:56-62`、`P:3319-3330` |

补充两条结构性事实（不属于"缺端点"，但会决定设计）：

- **`/search` 的 `session_id` 过滤在契约里无处表达**：契约没有"按会话检索"的概念，`memoryBodyIds` 只在 Source 层被消费（03 §1.2）。→ 事实层的"会话窗口"概念要么映射成一个 Memory Space/命名空间，要么放弃（`discover` 的粒度选择决定这一点）。
- **`hidden`/`degraded`/`output` 这些结构化状态在契约里没有位**，只能按 03 §4.1–§4.2 的方案寄生在 `hint`（结果为空时）与 `tags`/`content` 尾行（结果非空时）。

### 7.4 建议的能力声明（**建议，非源码事实**）

| 位 | 建议值 | 理由 |
|---|---|---|
| `search` | `true` | 核心路径 |
| `browse` | `false`（一期）/ `true`（若实现 `list()` 空间级浏览） | 现有只有按 session 列，不满足浏览语义；`false` 时走 query-only，避免出现"能浏览但只能浏览一个 session"的假能力 |
| `graph` | `false` | 无节点/边投影；避免基类默认平铺假图 |
| `entities` | `false` | 无实体索引 |
| `related` | `false` | 无多跳 |
| `remember` | `true` | 有写路径 |
| `link` | `false` | `supersedes` 不是合法 `EdgeType` |
| `forget` | `false` | 只有作废，没有删除 |
| `writeMode` | `'exact'` | 写入是同步落盘（`write_path.py` 直写 SQLite），无后台 LLM 抽取 |
| `deletionMode` | `'unsupported'` | 一期未实现 forget |
| `scoreSemantics`（两处） | `'provider-native'` | RRF 分不是 0..1 校准分；normalized 会被 0.25 阈值清空 |

---

## 8. 待验证项（按风险排序，必答 7）

| # | 待验证 | 为什么重要 | 怎么验 | 现状 |
|---|---|---|---|---|
| 1 | **写回执缺 `action` 的连锁反应**是否真如 §7.3-W1 所述（`unknown` + `memoryBodyIds:[]` + 不激活空间） | 决定"能不能先跑通再补回执"；如果误判，会把"写成功"当"写失败"重试，产生重复 | 真机写一条，看 `mnemon_remember` 的工具返回文案与空间 `active` 变化 | [A+] 代码路径明确，**未实测**；`M:10907` 的 `landed` 判定为静态阅读 |
| 2 | **Source 是否真的不给 `remember` 加超时**（`config.timeoutMs` 仅传给 adapter） | 若为真，shim 卡住会一直挂住写 worker；若有隐藏超时，则必须处理中断后的半写状态 | 在 shim 上注入 hang，观察工具返回耗时与错误文案 | [A+] `P:1972-1977` 静态确认；**运行期未测** |
| 3 | **`content` 8192 上限的实际触发点**（RPC 允许 100k） | 大事实/长围栏块会被 8192 拦在 Source 层，错误文案是 `content is too long (max 8192 characters)`，模型看到的是工具失败 | 写一条 9000 字的记忆 | [A+] `P:3666` vs `P:3313`；**未实测** |
| 4 | **`discover` 的粒度选择**（一个空间 vs 每 group 一个） | 决定 `memoryBodyIds` 能否表达"会话窗口"；也决定 rediscover 会不会重复挂载 | 实现后连点两次"重新发现"，看 `added` 是否幂等 | 设计题，**未决** |
| 5 | **多空间混合（一个 `provider-native` + 一个 `normalized-relevance`）时，排序与 tier 如何交互** | 已确认：`useProviderOrder = 异构 \|\| 任一候选是 provider-native`（`P:2469`），此时排序只看 `providerRank`；而 tier 仍由**每个候选自己的** `scoreSemantics` 决定（`P:1475-1484`）→ 混合场景下会出现"按 A 的顺序、用 B 的阈值"。运行期观感未验证 | 两个空间各返回 2 条（一个声明 native、一个声明 normalized），看最终顺序与被 drop 的条数 | [A+] `P:2469-2470,1475-1484` 静态；**混合场景未实测**。附带结论（可当事实用）：`provider-native` 时 `score` 的**数值**不影响任何判定，只要"存在且有限"（`P:1470-1479`），所以 RRF 精度与排序无关 |
| 6 | **`unavailable` 前缀文案**（`unavailable: <msg>`）在策略层是否被改写 | 03 §4.1 依赖 hint 原文透传；若策略重写文案，`NO_MATCH` 前缀方案要调整 | 让 Provider 抛错，看最终 `evidence.unavailable` | [A+] `P:2444` + `S:167-182` 静态；**未实测** |
| 7 | **`state` 字段（`memory-providers.json`）与 `provider-service-update` 的并发安全** | 手工改 profile + GUI 保存服务配置可能互相覆盖 | 双端同时改 | [推断] 有 `withMemoryStorageLock` 引用（`P:10`）但未核对覆盖范围 |
| 8 | **`ProcessError.reason` 是否真的出现在 `RpcError.details.reason`** | 影响 shim 崩溃时的错误诊断文案 | 用进程型 Provider 触发 launch 失败 | [A+] `M:14366-14374` + `PS.d.ts:54-58` 静态；**未实测** |

---

## 9. 对 03 的更正建议（**只写不改**）

> **03 需更正（2 处硬冲突）**：①`id`/`content` 缺失**不是**宿主"静默丢弃"，`id` 缺失会在策略层抛 `TypeError`（§9-1、§9-2）；②`normalizedScore`/`relevanceTier`"不进模型上下文"的**证据层级**引错了，它们在 evidence 信封里，是策略层才剥掉的（§9-4）。
> 其余 5 条是"结论对、证据或边界需补"，不构成冲突。

| # | 03 的位置 | 03 的说法 | 源码事实 | 建议改法 |
|---|---|---|---|---|
| 1 | §1.3 表首行（`id`） | "缺 → **静默丢弃**；长度必须 1..1000，否则被策略跳过" | 两件事被合并了：**长度 1..1000 的静默跳过**确实在策略层（`S:95`）；但**缺失** `id` 会在策略层抛 `TypeError`（`S:95` 直接读 `.length`），**不是静默**。真正"return undefined + filter"的静默丢弃发生在**各 Provider 自己的 normalize**（HANDOFF §7 坑 4 说的是这一层）[A+] `S:95`、`P:2694` | 拆成两条：①"Provider **必须**保证 `id` 非空（缺了会在策略层抛错，整个召回失败）"；②"长度 >1000 会被策略静默跳过" |
| 2 | §1.3 表（`content`） | "缺 → **静默丢弃**" | `content` 缺失会让 `truncateMemoryText` 抛 `TypeError`（`S:105` → `truncate` 读 `.length`），**不是静默丢弃**；只有 `content === ""` 才静默丢（`S:106`）[A+] | 改为"缺 → 召回调用抛错；空串 → 静默丢" |
| 3 | §1.3 表（`score`） | "缺失 → tier `unknown` → 最多选 2 条、策略每回合只收 1 条" | `2` 是 **Source** 的 `maxUnknownResults` 默认值（`P:3457`）；**策略层每回合只收 1 条**（`S:54`）。两者都对，但"最多 2 条"会被后续 1 条再截一次 | 补一句"Source 放 2 → 策略再截 1，最终 1 条" |
| 4 | §1.3 表（`normalizedScore` / `relevanceTier` 那一行） | 这些字段"**全部不进模型上下文**：`evidence()` 的 provenance 白名单里没有它们"，引 `P:3976-3991` | 引错了层级：`score`（`normalizedScore ?? score`）**确实写进了 evidence item**（`P:3974`），`relevanceTier` **确实在 provenance 白名单里**（`P:3982`）。它们是在**策略层** `boundedModelInsights` 组装模型可见面时才被剥掉的（`S:112-128` 的字段白名单里没有 score / relevanceTier） | 结论"模型看不到"仍成立，但证据应改为 `S:112-128`；并补一句"evidence 信封里有 score 与 relevanceTier，只是模型面没有"——否则将来有人从信封读 score 会以为 03 说错了 |
| 5 | §4.4 风险 1 | "写入侧 `tags` 值禁英文逗号（含逗号直接抛错）" | 正确，但**抛错点是 Source 的 `prepareRemember`→`commaList`**，`P:1826`，不是 Provider。03 引的是 `contracts.d.ts` | 补上 Source 侧行号，避免误以为要在 Provider 里校验 |
| 6 | §7 待验证 2 | "`hint` 仅空结果透传，若上游改为总是透传则所有互斥结论松动" | 仍然成立；**新增**一条同类风险：抛错路径的 hint 带 `unavailable: ` 前缀（`P:2444`），与 Provider 自造的 hint 共用同一个键 | 在待验证清单里补这一条 |
| 7 | §1.3 表末（`hint`） | "只在结果为空时才被转成 `evidence.unavailable`" | 与源码一致 [A+] `P:4110`；但**多空间时 hint 会被拼成 `"<空间名>: <hint>"` 并用 `\n` 连接**（`P:2465,2558`），03 未写 | 补一句"多空间时 hint 是拼接文本，`NO_MATCH` 前缀要放在每个空间自己的 hint 里或统一在源文本开头" |

---

## 10. 一页速查

| 问题 | 答案 | 出处 |
|---|---|---|
| 要实现几个方法？ | 类型 6 个必填（`id`+`status`/`search`/`graph`/`list`/`remember`）；`discover` 在"保存服务配置"时事实上必填 | §2.1、§3.5 |
| 能力位为 false 的方法能不写吗？ | 能（运行时不查），但 TS 类型仍要求那 6 个 | §2.2 |
| 返回值容器 | `search` 是 `{results, hint?}`；`list`/`related`/`metadataSample` 是裸数组；`remember`/`link`/`forget` 是任意 JSON 回执；`rememberMany` 必须等长数组 | §2.1 |
| 回执怎么算成功 | 必须有 `action:"stored"`（或同义 16 词之一）/`committed:true`/计数字段 >0 | §4.4 |
| 幂等键 | **契约没有**，只能自造 | §4.5 |
| 有重试吗 | 没有，一次不成就失败 | §5.3 |
| 有超时吗 | Source 不给单次调用超时（30 s 只用于 rediscover）；Provider 自己实现 | §5.2 |
| `scoreSemantics` 写哪 | 两处：manifest + adapter，必须一致；建议 `provider-native` | §6.4 |
| 秘钥从哪拿 | `context.memorySpaces.providerConnection(spaceId, providerId)`；`body.provider.settings` 里没有秘钥值 | §6.6 |
| 怎么装进去 | Source 的 `providers: [{use, instanceId, config}]`；**GUI 安装器不支持 provider 包** | §1.2 |
