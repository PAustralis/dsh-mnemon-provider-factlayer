# 21 · mnemon 升级版本漂移核对（0.5.37 / 0.5.24 → 0.5.44 / 0.5.27）

**任务性质**：只读核对。本文不改代码、不装包、不重启，只回答一个问题——**这次升级有没有改变我们 provider 所依赖的契约**。

---

## 0. 结论（先看这段）

1. **契约没有变。** `MemoryProviderAdapter`、`MemorySpaceProviderManifest`、`provider-sdk` 导出面、`providers` 挂载 schema、写入回执启发式、`hint` 透传、`RememberRequest` 上限，全部与 0.5.37 / 0.5.24 一致。
2. 本次升级实际改的是**嵌入（embedding）凭据的存放方式**（`apiKey` → 新增 `apiKeyEnv` / `apiKeyRef` + Host credentials 服务）和一条 Windows 安装提示文案。与 provider 数据面无关。
3. 我们的 provider **不需要改任何代码**，改动清单为空（§4）。
4. 唯一需要后续动作的是**文档锚点行号**（§6）：docs/03、docs/05 里的 `P:` / `PG:` / `PJ:` / `M:` 行号整体漂移，语义未变。

一句话证据：**契约的四个真源（共五个文件）在新旧两版之间 SHA256 完全相同**（§2）——`lib/definitions-BNmfQoWk.d.ts`、`lib/definitions-DfmOqJjR.js`、`lib/plugin-m1PeU1IE.js`、`lib/provider-sdk.js`、`lib/provider-sdk.d.ts`。

---

## 1. 版本确认

| 包 | 声明版本 | pnpm 实目录 | `package.json` 的 `version` |
|---|---|---|---|
| `dsh-mnemon` | 0.5.44 | `.pnpm\dsh-mnemon@0.5.44\` | `0.5.44` |
| `dsh-mnemon-source-memory-spaces` | 0.5.27 | `.pnpm\dsh-mnemon-source-memory-spaces@0.5.27_dsh-mnemon@0.5.44\` | `0.5.27` |

- profile 声明：`C:\Users\ZhaoY\.dsh\profiles\web\package.json` → `"dsh-mnemon": "0.5.44"`，且 `dsh-mnemon` 被列为 bundle。
- **旧版仍在盘上**：`.pnpm\dsh-mnemon@0.5.37\`、`.pnpm\dsh-mnemon-source-memory-spaces@0.5.24_dsh-mnemon@0.5.37\`。因此本次是**源码对源码**比较，不需要重装或降级即可复核（§7 给出复现命令）。
- 旁证（**不在本次核对范围，但影响 docs 锚点**）：`dsh-mnemon-strategy-default-three-tier` 也从 **0.5.11 → 0.5.13**，docs/03、05 里 `S:` 前缀的锚点属于这个包。

---

## 2. 变更面总览：哪些文件真的变了

### 2.1 `dsh-mnemon-source-memory-spaces` 0.5.24 → 0.5.27

**SHA256 完全相同（这是本次结论的主证据）：**

| 文件 | 它是什么 |
|---|---|
| `lib/definitions-BNmfQoWk.d.ts` | **适配器/清单的唯一类型真源**：`MemoryProviderAdapter`、`MemorySpaceProviderManifest`、`MemorySpaceProviderModule`、`MemorySpaceProviderHost`、`defineMemorySpaceProvider` 声明 |
| `lib/definitions-DfmOqJjR.js` | **manifest 逐字段校验** + `defineMemorySpaceProvider` 实现 |
| `lib/plugin-m1PeU1IE.js` | 注册表、**`assertRuntime`**、descriptor 投影、实例 id 校验 |
| `lib/provider-sdk.js`、`lib/provider-sdk.d.ts` | 对外导出面（`defineMemorySpaceProvider` 等） |
| `lib/adapter-D-BnWkko.js` | 进程执行 / 超时 / 取消（`runProcess`） |
| `lib/space-identity.*`、`lib/testing.*`、`lib/native-cli.d.ts`、`README.md`、`locale/*.json` | — |

**有变化：**

| 文件 | 变化 |
|---|---|
| `lib/index.js` | 共 9 个 diff hunk：新增 `src/embedding-credentials.ts` 区段、`processEnvironment` 改 async、`resolveEmbedding` 增 `apiKeyEnv`/`apiKeyRef`、`createRunner` 增凭据读取与密钥脱敏、`installMemorySpaces` 增 `credentials` 服务读取、Windows 安装提示文案 |
| `lib/contracts.js` | 仅 +3 个常量：`EMBEDDING_CREDENTIAL_PREFIX`、`EMBEDDING_CREDENTIAL_REFERENCE`、`EMBEDDING_ENVIRONMENT_NAME` |
| `lib/contracts.d.ts` | 仅 embedding 配置：`apiKeyEnv`/`apiKeyRef`、`MnemonEmbeddingSettingsView`、`MnemonEmbeddingCredential` |
| `lib/index.d.ts` | 仅 re-export 列表（跟随上面两个文件） |
| `lib/client.js`、`lib/native-cli.js`、`presentation/locales.json`、`lib/types/client/overview-page.d.ts` | UI / CLI 提示层面 |
| `package.json` | `version`、devDependencies（不影响 `exports` 与 `peerDependencies`） |

> `lib/index.js` 的 9 个 hunk **没有一个**落在 provider 契约路径上：`scoreDecision`/召回质量、`mutationResultCompletion`、`prepareRemember`、`search`/`list`/`graph` 投影、`evidence()` 全部在未改动区间（§3 逐项给行号）。

### 2.2 `dsh-mnemon` 0.5.37 → 0.5.44

主机侧 `lib/index.js` 变动很大（18308 → 20190 行，+2669/−787），但**provider 相关代码块逐行相同**（用 `Compare-Object` 对同一文本块比对）：

| 代码块 | 0.5.37 行 | 0.5.44 行 | 结果 |
|---|---|---|---|
| `COMMITTED_MUTATION_STATES` / `UNCOMMITTED_MUTATION_STATES` / `COMMITTED_MUTATION_COUNTS` / `mutationResultCommitted` | 7727–7789 | 7850–7912 | **IDENTICAL** |
| `writeLanded` | 10303–10312 | 11104–11113 | **IDENTICAL** |
| `SPACE_READ_CAPABILITIES` / `SPACE_WRITE_CAPABILITIES`（RPC 能力门禁表） | 14387–14409 | 16104–16126 | **IDENTICAL** |

其余变化集中在：同步/Git、技能演化、idle-review、embedding 凭据、trajectory、工具注册。另新增 SDK 导出 `reserveMemoryProjection`（`lib/types/src/sdk/index.d.ts:1`，面向 Strategy 扩展，与 provider 无关）。

---

## 3. 九项逐条核对

> 行号约定：**未标注即指 0.5.44 / 0.5.27 的新版本行号**（本次实际读过的行）。旧行号在括号里，便于对照现有 docs。

| # | 核对项 | 0.5.37 / 0.5.24 的行为 | 0.5.44 / 0.5.27 的行为 | 是否有影响 |
|---|---|---|---|---|
| 1 | `MemoryProviderAdapter` 方法集合与签名 | 17 个成员（2 只读属性 + 15 方法，10 个可选） | **完全一致**，类型文件 SHA256 相同 | **没变** |
| 2 | `MemorySpaceProviderManifest` 校验、`capabilities` 键名、`deletionMode`/`writeMode`、`scoreSemantics` 两处声明 | 16 字段（14 必填 + 2 可选），8 布尔 + 2 枚举，`scoreSemantics` 两处声明且单向断言 | **完全一致**（校验器 `.js` 与类型 `.d.ts` 均 SHA256 相同） | **没变** |
| 3 | `search` 的 `Insight` 要求：`id/content/createdAt/score/tags`、`createdAt` ISO、`score` 缺失 | `id`/`content` 必填，其余可选；`createdAt` 不校验格式；缺 `score` → tier `unknown` | **完全一致**；`score` 缺失仍是 `tier: "unknown"`，`maxUnknownResults` 仍默认 2 | **没变**（`createdAt` 的"必须 ISO"另见 §5 未验证） |
| 4 | 回执判定：16 词表 / `committed` / 计数字段 / `writeLanded` | Source 7 条判定顺序 + 16 词表；Host `writeLanded` = committed 或 accepted/queued/pending/processing | **逐字一致**（Source 判定区未进 diff；Host 块 `IDENTICAL`） | **没变** |
| 5 | `hint` 是否只在结果为空时透传 | 是：`results.length === 0 ? hint : undefined` | **一致**（`lib/index.js:4144`，旧 `P:4110`） | **没变** |
| 6 | `RememberRequest` 字段与上限 | `content` ≤8192；`tags` ≤20、`entities` ≤50、禁逗号；`importance` 1..5；`category` 默认 `general`、`source` 默认 `user` | **完全一致**（`lib/index.js:3337-3358`） | **没变** |
| 7 | 挂载配置 `providers: [{use, instanceId, config}]` 与 `config` schema（`providers` 位置） | Source config 顶层 `providers` 数组，元素可为 string 或 `{use, instanceId, config}` | **完全一致**（`lib/index.js:4301-4306`） | **没变** |
| 8 | `defineMemorySpaceProvider` 的导出位置与 child module 形状 | `dsh-mnemon-source-memory-spaces/provider-sdk`；module = `{id, Config?, apply}` | **完全一致**：`provider-sdk.{js,d.ts}` SHA256 相同，`exports["./provider-sdk"]` 未变，`peerDependencies.dsh-mnemon: ^0.5.19` 未变 | **没变** |
| 9 | 升级是否带来新的**强制要求** | — | **没有**：未新增 manifest 字段、未新增必须实现的 adapter 方法、`assertRuntime` 未改、能力门禁表未改 | **没变** |

### 3.1 适配器方法集合与签名（第 1 项）

`lib/definitions-BNmfQoWk.d.ts:73-106` 与 0.5.24 **文件级完全相同**。17 个成员逐条：

| 成员 | 签名是否变化 |
|---|---|
| `id`（必填，只读） | 未变（`d.ts:74`） |
| `scoreSemantics?` | 未变（`d.ts:75`） |
| `discover?` | 未变（`d.ts:77`） |
| `invalidateStatus?` | 未变（`d.ts:79`） |
| `status` | 未变（`d.ts:80`） |
| `search` | 未变（`d.ts:81`） |
| `graph` | 未变（`d.ts:82`） |
| `list` | 未变（`d.ts:83`） |
| `remember` | 未变（`d.ts:84`） |
| `metadataSample?` | 未变（`d.ts:86`） |
| `entityIndex?` | 未变（`d.ts:92`） |
| `rememberMany?` | 未变（`d.ts:94`） |
| `get?` | 未变（`d.ts:100`） |
| `related?` | 未变（`d.ts:101`） |
| `link?` | 未变（`d.ts:102`） |
| `forget?` | 未变（`d.ts:103`） |
| `dispose?` | 未变（`d.ts:105`） |

**没有新增、没有删除、没有改签名。** 我们的 `plugin/src/contract.ts:256-311` 这份本地镜像仍然逐字段对得上（它本身就是按 0.5.24 抄的）。

### 3.2 manifest 校验（第 2 项）

- 字段数：**16**（`definitions-BNmfQoWk.d.ts:117-134`）。必填 14：`apiVersion / kind / typeId / packageName / version / label / summary / origin / locality / workspaceBinding / capabilities / fields / secrets / scoreSemantics`；可选 2：`icon? / summaryI18nKey?`。
- 逐字段校验器 `definitions-DfmOqJjR.js:162-194`（`captureManifest`）与 0.5.24 **SHA256 相同**：`apiVersion` 字面量 → `kind` → `typeId` 正则 → `packageName` → `version`（≤100，不校验 semver）→ `label` → `icon` → `summary`（≤1000）→ `summaryI18nKey?`（≤200）→ `locality` → `origin` → `workspaceBinding` 三值 → `scoreSemantics` 三值 → `capabilities` → `fields` → `secrets` 必须与 `input:'secret'` 字段集合完全一致（`:188-191`）。
- `capabilities` 键名：**8 布尔 + 2 枚举，一字不差**（`lib/contracts.d.ts:105-116`）：`search / browse / graph / entities / related / remember / link / forget` + `writeMode: 'exact'|'async-extracting'` + `deletionMode: 'soft'|'hard'|'unsupported'`。
- `scoreSemantics` **仍是两处声明**：manifest 顶层（`definitions-BNmfQoWk.d.ts:133`，三值）与 adapter 只读属性（`d.ts:75`，两值）。单向断言仍在 `plugin-m1PeU1IE.js:270`：manifest 声明 `normalized-relevance` 而 adapter 没声明 → 装配失败；反向不校验。**与文档 §6.4 完全一致。**
- 装配断言 `plugin-m1PeU1IE.js:257-271` 逐行未变：`adapter.id === manifest.typeId`，以及 `search→search / browse→list / graph→graph / related→related / remember→remember / link→link / forget→forget` 七个能力位与方法的对应。**注意：仍不检查 `status`**（文档 §2.2 的坑仍在）。

### 3.3 `Insight` 要求（第 3 项）

`lib/contracts.d.ts:250-279`（0.5.24 同）：

| 字段 | 是否必填 | 变化 |
|---|---|---|
| `id: string` | **必填** | 未变 |
| `content: string` | **必填** | 未变 |
| `createdAt?: string` | 可选 | 未变（类型仍是 `string`，Source **不做 ISO 格式校验**，原样透传到 `evidence.revision` 与 `provenance.createdAt`，`lib/index.js:4009,4019`） |
| `score?: number` | 可选 | 未变 |
| `tags?: string[]` | 可选 | 未变 |

`score` 缺失的后果**未变**，`lib/index.js:1491-1497`：

```js
function scoreDecision(candidate, context, lowScoreAction, nonPositiveAction) {
	const score = candidate.insight.score;
	if (score === void 0) return { action: "keep", tier: "unknown", reason: "unscored" };
```

并且 `maxUnknownResults` 仍默认 **2**（`lib/index.js:3491`），strict-v1 里 tier `unknown` 只取前 2 条（`:1558`）。→ 文档 03 §1.3 的"缺失 → tier unknown → 最多选 2 条"**成立**。

`createdAt` 的"必须 ISO"这一条**在源码里没有对应校验**，旧版也没有——这是文档的既有表述，不是本次漂移。详见 §5 未验证项 1。

### 3.4 回执判定（第 4 项）

**Source 侧**（`lib/index.js:1857-1918`，旧 `P:1829-1890`），逐字未变：

| 判定顺序 | 条件 | 结果 | 行 |
|---|---|---|---|
| 1 | 状态串含 `partial`，或 `errors` 非空且计数字段 >0 | `partial` | `1908` |
| 2 | `success===false` / `ok===false` / `errors` 非空 / 状态串 ∈ `canceled,cancelled,error,failed` | `failed` | `1909` |
| 3 | 状态串 === `candidate` | `candidate` | `1910` |
| 4 | 状态串 ∈ `accepted,pending,processing,queued,running` | `accepted` | `1911` |
| 5 | `committed===false` / `durable===false` / 状态串 === `skipped` | `unknown` | `1912` |
| 6 | `committed===true` / `durable===true` / 状态串 ∈ 16 词 / 计数字段 >0 | `committed` | `1913` |
| 7 | 其它（含空对象、只有 `id`） | `unknown` | `1914` |

- **16 词表**（`:1857-1874`）：`added, committed, completed, created, deleted, forgotten, imported, invalidated, linked, merged, removed, replaced, stored, succeeded, success, updated` —— 仍是 16 个。
- **PENDING 5 词**（`:1875-1881`）、**FAILED 4 词**（`:1882-1887`）、**计数字段 7 个**（`:1888-1896`：`created, deleted, edges_inserted, imported, removed, stored, updated`，必须 `>0`）全部未变。
- 状态串来源仍是 `action → status → state`，取第一个字符串并 `trim().toLowerCase()`（`:1901-1905`）。
- `mutationResultCommitted`（`:1916-1918`）仍只是 `=== "committed"`。

**Host 侧第二道闸**（`dsh-mnemon/lib/index.js:11104-11112`，旧 `M:10303`），块级 `IDENTICAL`：

```js
function writeLanded(value) {
	if (mutationResultCommitted(value)) return true;
	const state = [value.action, value.status, value.state]
		.find((entry) => typeof entry === "string")?.trim().toLocaleLowerCase();
	return state === "accepted" || state === "queued" || state === "pending" || state === "processing";
}
```

→ 不满足时 `action` 被改写成 `unknown`、`memoryBodyIds` 为空这条坑**仍然存在**（文档 §4.4 最后一条）。

**顺带发现一处文档没覆盖的既有分支**（不是漂移，两版相同，`dsh-mnemon/lib/index.js:7895-7900`）：如果回执带 `memoryReceipt` 或 `completion` 键，Host 走**严格结构化通道**——必须 `completion==="committed"` **且** `status==="succeeded"` **且** `committedAt` 可被 `Date.parse`，否则直接判未落地。也就是说：**只要回执里出现 `completion` 键，`committed:true` 这种宽松写法就失效了。** 我们的 shim 不要顺手加这个键（见 §4 建议）。

### 3.5 `hint` 透传（第 5 项）

`lib/index.js:4144`（旧 `P:4110`，偏移 +34）：

```js
return evidence(request, result.results, result.results.length === 0 ? result.hint : void 0);
```

**仍是"只在结果为空时透传"**，结果非空时在路由层就被丢弃。UI 读源面板另有一路 `:2464`（`result.hint === void 0 ? {} : { hint: result.hint }`），也未变。→ 文档 03 §4.1 的硬约束成立。

### 3.6 `RememberRequest` 字段与上限（第 6 项）

类型（`lib/contracts.d.ts:296-305`）：`content: string`、`category?`、`importance?`、`tags?`、`entities?`、`source?`、`memoryBodyId?`。**无扩展位。**

归一化（`lib/index.js:3337-3358`，旧 `P:3313` 附近，偏移 +28）：

| 字段 | 归一化 | 行 |
|---|---|---|
| `content` | `trim()` 后非空，**≤8192** | `3341` |
| `importance` | 给了就必须是 **1..5 整数** | `3342` |
| `category` | 枚举校验，缺省 `general` | `3343` |
| `source` | 枚举校验，缺省 `user` | `3344` |
| `tags` | 每个 `trim`、去空、**≤20**、禁逗号、全空则不发 | `3345`（`commaList` 在 `1850-1856`） |
| `entities` | 同上，**≤50** | `3346` |
| `memoryBodyId` | 缺省时要求活动空间恰好 1 个，否则抛错 | `3334` |

**上限一个都没动。** RPC 层允许 100000 而 Source 只允许 8192 这个不一致也仍在（文档 §4.2 的坑）。

### 3.7 挂载配置（第 7 项）

`lib/index.js:4301-4306`：

```js
const MemorySpaceProviderDeclarationSchema = z.object({
	use: z.string().required(),
	instanceId: z.string(),
	config: z.any()
});
const Config = z.intersect([MemorySpacesConfig, z.object({
	providers: z.array(z.union([z.string(), MemorySpaceProviderDeclarationSchema])).default([])
})]);
```

- `providers` **仍在 Source config 顶层**，元素仍可为裸字符串或 `{use, instanceId, config}`；`use` 必填，`instanceId` / `config` 可选。
- `resolveMemorySpaceProviderEntries`（`:4320-4340`）逻辑未变：**至少一个显式 Provider**、必须能拿到 Loader、`instanceId` 缺省取 `module.id`、重复 `instanceId` 报错。
- `installMemorySpaces`（`:4342-4360`）唯一变化是 `createMemorySpacesSource(..., readCredential)` 多传了一个凭据读取回调，**与 provider 装配无关**。
- `peerDependencies` 未变：`dsh-mnemon: ^0.5.19`。

### 3.8 `provider-sdk` 导出面（第 8 项）

- `package.json` 的 `exports["./provider-sdk"]` → `./lib/provider-sdk.js` / `.d.ts`，**未变**。
- `lib/provider-sdk.js` 与 `lib/provider-sdk.d.ts` **SHA256 相同** → 导出列表逐字一致：`CATEGORIES, DEFAULT_MAX_OUTPUT_BYTES, EDGE_TYPES, HttpMemoryProvider, INTENTS, MEMORY_SPACE_PROVIDER_API_VERSION, NORMALIZED_RELEVANCE_SCORE, ProcessError, SOURCES, defineMemorySpaceProvider, defineMemorySpaceProviderDefinition, firstArray, jsonArray, jsonNumber, jsonObject, jsonString, runProcess`（`provider-sdk.js:108`）。
- child module 形状未变（`definitions-BNmfQoWk.d.ts:150-155`）：`{ id: string; Config?; apply(ctx, host, config) }`；`defineMemorySpaceProvider` 实现仍是 `Object.freeze({ ...module, id })`（`definitions-DfmOqJjR.js:202-209`），**已安装的调用面是恒等函数**。
- 我们的探测顺序（`plugin/src/define.ts:27-30`：`dsh-mnemon-source-memory-spaces/provider-sdk` → `dsh-mnemon`）**第一个仍然命中**。

### 3.9 是否带来新的强制要求（第 9 项）

**没有。** 逐项排查：

| 可能有新要求的地方 | 结论 | 证据 |
|---|---|---|
| manifest 新增必填字段 | 无（仍 16 字段 / 14 必填） | `definitions-BNmfQoWk.d.ts:117-134` 哈希相同 |
| adapter 新增必实现方法 | 无（仍 17 成员，必填仍是 `id/status/search/graph/list/remember`） | `d.ts:73-106` 哈希相同 |
| `assertRuntime` 收紧 | 无 | `plugin-m1PeU1IE.js:257-271` 哈希相同 |
| RPC 能力门禁变化 | 无 | `dsh-mnemon/lib/index.js:16104-16126` 块级 IDENTICAL |
| 新增的服务依赖 | 仅 embedding 凭据可选读 `credentials` 服务；**不设 `apiKeyRef` 就不会调用** | `source lib/index.js:4355-4358`（`ctx.get("credentials", false)` 在回调内） |
| 新增 SDK 导出 | `reserveMemoryProjection`，面向 Strategy 扩展，与 provider 无关 | `dsh-mnemon/lib/types/src/sdk/index.d.ts:1` |
| Node 引擎要求 | 未变（`>=20`） | 两版 `package.json` `engines` 相同 |

---

## 4. 结论：我们的 provider 要改吗

**不需要改。** 改动清单为空。

依据链：我们对外只依赖四样东西——① `defineMemorySpaceProvider`（运行期动态 import）；② `manifest` 16 字段的形状与校验规则；③ `MemoryProviderAdapter` 的 `status/search/remember` 三个必填方法；④ `providers[].config` 的注入形状。这四样在本次升级中**全部逐字未变**（§3.1、§3.2、§3.7、§3.8），且 `manifest.capabilities`（`search:true, browse:false, graph:false, entities:false, related:false, remember:true, link:false, forget:false, writeMode:'exact', deletionMode:'unsupported'`）与 `scoreSemantics:'provider-native'` 仍满足校验与装配断言。

我们**不依赖**：embedding 配置（本次唯一实质变更面）、`nativeRunner`、`HttpMemoryProvider`、文档/技能/同步等模块。

### 4.1 建议（可选，非必须，均不改代码）

1. **不要把 `completion` / `memoryReceipt` 键放进 `remember` 回执**。一旦出现，Host 会走严格通道（§3.4 末尾），要求 `completion:"committed"` + `status:"succeeded"` + 可解析的 `committedAt`，比宽松启发式更难满足。当前 shim 不返回这两个键，保持现状即可。
2. 回执里**始终带 `action`**（如 `"created"`），否则 Host 的 `writeLanded` 会把它改写成 `unknown`，模型会被告知"没落地"而其实写进去了。
3. `Insight.createdAt` 继续给 **ISO 8601 带时区**。契约不校验，但下游排序/展示按字符串处理，给数字会在实体排序路径（`lib/index.js:1478` 的 `localeCompare`）直接抛 `TypeError`——虽然我们 `entities:false` 走不到，但别赌。
4. `search` 结果里 `score` 要嘛给、要嘛整条不给；给 `NaN`/`Infinity` 会被判 `invalid-score` 直接 drop（`lib/index.js:1498-1502`）。

---

## 5. 未验证项（源码无法确认，需运行期观察）

| # | 项 | 为什么无法从源码确认 |
|---|---|---|
| 1 | `createdAt` 是否"必须 ISO、给数字被静默丢弃" | 两版 Source 都**没有** ISO 校验代码；`lib/index.js:4009,4019` 是原样透传。docs/03:70、96 的这条表述在 0.5.27 源码里找不到实现。可能的校验在 DSH 的 memory view 协议侧（不在本次两包内）→ 需要一次真实 recall 观察模型信封才能定论。**结论：契约未变，但"要求"本身在源码层不存在。** |
| 2 | 模型面是否真的看不到 `score` | `evidence()` 把 `score` 放进了 evidence item（`lib/index.js:4008`），是否渲染到模型由 DSH 侧决定。本次只读源码、未跑真实 recall。**结论：与 0.5.24 相同，但未实测。** |
| 3 | 我们的 provider 在升级后的运行时能否真实装配 | 本次按要求**不重启**。结论"不需要改"是基于静态契约比对 + 校验器代码未变 + 我们的 manifest 满足校验，**不是**基于一次成功装配。建议下次重启后看一眼空间目录里 `factlayer` 是否仍显示健康。 |
| 4 | `S:` 前缀锚点（`id` 1..1000、1200/4800 截断、tags 取 8） | 属于 `strategy-default-three-tier`（0.5.11→0.5.13，**不在本次范围**）。抽查：`MODEL_RECALL_CONTENT_LIMIT=1200`、`MODEL_RECALL_TOTAL_CONTENT_LIMIT=4800`（`lib/index.js:49-50`）、`result.id.length > 1e3` 丢弃（`:96`）仍在，但这些证据未按本报告标准逐条复核。 |

---

## 6. 文档锚点维护清单（不改，只列）

行号整体漂移，**语义未变**。若继续按 `P:123` 这种方式引用，需要重定位：

| 前缀 | 文件 | 0.5.24/0.5.37 → 0.5.27/0.5.44 偏移 | 备注 |
|---|---|---|---|
| `P:` | `source-memory-spaces/lib/index.js` | 前 1102 行 **0**；中段 **+27~+28**（`P:1464`→`1491`、`P:3313`→`3341`）；`resolveEmbedding` 之后 **+34**（`P:3975`→`4009`，`P:4110`→`4144`） | 语义全部相同 |
| `PG:` | `lib/definitions-DfmOqJjR.js` | **0** | 文件 SHA256 相同，行号可直接沿用 |
| `PJ:` | `lib/plugin-m1PeU1IE.js` | **0** | 文件 SHA256 相同，行号可直接沿用 |
| `PD:` | `lib/definitions-BNmfQoWk.d.ts` | **0** | 文件 SHA256 相同，行号可直接沿用 |
| `PC:` | `lib/contracts.d.ts` | 前 522 行内 **0**；`MnemonEmbeddingConfig` 之后 **+20** | `Insight`/`RememberRequest`/`capabilities` 都在 522 行之前，锚点仍准 |
| `PS:` | `lib/provider-sdk.{js,d.ts}` | **0** | 文件 SHA256 相同 |
| `M:` | `dsh-mnemon/lib/index.js` | 早期 **+123**；`14387`→`16104`，即 **+717** | 只影响 `M:` 锚点，语义相同 |
| `S:` | `strategy-default-three-tier/lib/index.js` | 0.5.11→0.5.13，**未系统复核** | 抽查的常量仍在 |

另外：docs/05 的 §0 路径缩写表写死了 `@0.5.24_dsh-mnemon@0.5.37` 目录名，按当前安装已经指错（目录名现在是 `@0.5.27_dsh-mnemon@0.5.44`）。该表原本就提醒过"文件名带内容哈希，升级会变"，但**不带哈希的 `index.js` 这类文件名不变、行号会变**，这一点值得在下次维护时补一句。

**本报告自身**的行号锚点是按 **0.5.44 / 0.5.27** 写的，并且优先引用**哈希相同**的文件（`PG/PD/PJ/PS`），下次升级大概率仍可用。

---

## 7. 复现方法（只读）

两版包都在盘上，所以全程不需要装包：

```powershell
$base = "C:\Users\ZhaoY\.dsh\profiles\web\node_modules\.pnpm"

# 1) 版本确认
Get-ChildItem $base -Directory | Where-Object { $_.Name -like "*mnemon*" } | Select-Object -ExpandProperty Name

# 2) 哪些文件真的变了（SHA256 逐一比对）
#    见 §2：对 definitions-* / plugin-* / provider-sdk.* 四个真源应输出 same

# 3) 看完整 diff
$o = "$base\dsh-mnemon-source-memory-spaces@0.5.24_dsh-mnemon@0.5.37\node_modules\dsh-mnemon-source-memory-spaces\lib"
$n = "$base\dsh-mnemon-source-memory-spaces@0.5.27_dsh-mnemon@0.5.44\node_modules\dsh-mnemon-source-memory-spaces\lib"
git diff --no-index --unified=6 -- "$o\index.js" "$n\index.js"
git diff --no-index --unified=4 -- "$o\contracts.d.ts" "$n\contracts.d.ts"
```

判读要点：

- `definitions-*.d.ts` / `definitions-*.js` / `plugin-*.js` / `provider-sdk.*` **输出为空（哈希相同）** → 契约层没动。
- `index.js` 只应出现 embedding 凭据、`processFailureMessage`、`resolveEmbedding`、`createRunner`、`createMemorySpacesSource`、`installMemorySpaces` 六处，**不应出现** `mutationResultCompletion`、`prepareRemember`、`scoreDecision`、`hint`、`MemorySpaceProviderDeclarationSchema`。

---

## 8. 附：本次核对没有做的事

- 没有修改任何代码、没有 `pnpm install` / `pnpm add`、没有重启 DSH、没有触发一次真实 recall 或 remember。
- 没有核对 `dsh-mnemon-source-documents`（0.5.11→0.6.0）、`source-runtime`（0.5.16→0.5.19）、`source-skills`（0.5.4→0.5.11）、`strategy-*` 的漂移——它们在同一个升级里也动了，但不在本任务范围。
