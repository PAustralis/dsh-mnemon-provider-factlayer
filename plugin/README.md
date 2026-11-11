# dsh-mnemon-provider-factlayer

`dsh-mnemon` 的 **Memory Space Provider 适配器**（第三层，见 [`../docs/06-对接层设计.md`](../docs/06-对接层设计.md)）。
它把 mnemon 契约的 `discover / status / search / remember / list / graph` 逐方法翻译成对
**factlayer FastAPI shim** 的一次 HTTP 调用，并在翻译时补齐契约要求的形状（回执 `action`、
ISO 时间、`results` 数组、`provider-native` 分数语义）。**业务逻辑一律不在这层**：检索、派生、
作废都在 Python shim 里。

- 包名：`dsh-mnemon-provider-factlayer`
- `manifest.typeId` = 模块 `id` = `adapter.id` = **`factlayer`**
- 类型镜像：`src/contract.ts`（抄自 `docs/05-provider接口契约.md` 与
  `dsh-mnemon-source-memory-spaces@0.5.24` 的 `definitions-*.d.ts` / `contracts.d.ts`）
- 契约锚点：`dsh-mnemon@0.5.37`、`dsh-mnemon-source-memory-spaces@0.5.24`

## 前置条件

1. shim 已启动（单 worker）：

   ```bash
   python -m uvicorn app.main:create_app --factory --workers 1   # 默认 127.0.0.1:8000
   ```

2. 宿主 profile 已装 `dsh-mnemon`（0.5.37+）。本包**不把 peer 装进自己的 node_modules**：
   `dsh-mnemon` / `dsh-mnemon-source-memory-spaces` 是 `peerDependencies`，由宿主 profile 提供。

## 构建与测试

```bash
cd plugin
pnpm install          # typescript + vitest（peer 不安装，见 pnpm-workspace.yaml）
pnpm build            # tsc → dist/
pnpm test             # vitest run
npx tsc --noEmit      # 类型检查（src）
pnpm run typecheck    # src + tests 两份 tsconfig 都检查
```

## 安装

**重要（docs/05 §1.2）**：DSH 的在线安装器只接受 `dsh-mnemon-source-*` / `dsh-mnemon-strategy-*`，
`dsh-mnemon-provider-*` 包**不能从 GUI 的插件入口安装**。落地方式只有两条：

1. **随 bundle 的 `cordis.patch.yml` 一起发**（官方 9 个 provider 同款路径，包维护者用）；
2. **用户手改 profile 的 `cordis.patch.yml`**（推荐给终端用户）。

另外 loader 用**包名** import 子模块，所以包必须能被 profile 解析到。本包目前未发布 npm，
可以本地安装：

```bash
# <profile> 例如 %USERPROFILE%\.dsh\profiles\web
pnpm -C <profile> add file:<本仓库>/plugin      # 或 pnpm pack 后安装 tarball
# 也接受直接解包到 <profile>/node_modules/dsh-mnemon-provider-factlayer/
```

### 方式 A：bundle patch（包维护者）

在 `dsh-mnemon` 的 `cordis.patch.yml` 里，把本包追加进
`mnemon-source-memory-spaces` 的 `providers` 列表（片段）：

```yaml
- insert:
    - id: mnemon-source-memory-spaces
      name: dsh-mnemon-source-memory-spaces
      config:
        providers:
          - use: dsh-mnemon-provider-mnemon-native
            instanceId: mnemon-native
          # …（其余默认 provider 原样保留）
          - use: dsh-mnemon-provider-factlayer
            instanceId: factlayer
            config: {}
```

### 方式 B：profile 的 `cordis.patch.yml`（可整块粘贴）

profile patch 按 `id` **整块替换**该行的 `config`（不是深合并），所以下面的块必须把想保留的
provider 全部列出。下面是 `dsh-mnemon@0.5.37` 默认 9 个 provider + factlayer 的完整可粘贴块：

```yaml
# <profile>/cordis.patch.yml（例如 %USERPROFILE%\.dsh\profiles\web\cordis.patch.yml）
# ⚠ config 整块替换：必须列全想保留的 provider；删掉某一个等于把它从装配里移除。
- id: mnemon-source-memory-spaces
  name: dsh-mnemon-source-memory-spaces
  config:
    providers:
      - use: dsh-mnemon-provider-mnemon-native
        instanceId: mnemon-native
      - use: dsh-mnemon-provider-openviking
        instanceId: openviking
      - use: dsh-mnemon-provider-honcho
        instanceId: honcho
      - use: dsh-mnemon-provider-mem0
        instanceId: mem0
      - use: dsh-mnemon-provider-hindsight
        instanceId: hindsight
      - use: dsh-mnemon-provider-holographic
        instanceId: holographic
      - use: dsh-mnemon-provider-retaindb
        instanceId: retaindb
      - use: dsh-mnemon-provider-byterover
        instanceId: byterover
      - use: dsh-mnemon-provider-supermemory
        instanceId: supermemory
      - use: dsh-mnemon-provider-factlayer
        instanceId: factlayer
        config: {}
```

> 不要把这个块改成 `- insert:` 塞进 profile patch：bundle 已经 insert 过同 id 的 entry，
> 重复 id 会让 Settings 存不住（`dsh-mnemon/cordis.patch.yml` 顶部注释，issue #470）。

改完重启 `dsh web`，在 Memory 页的 Factlayer 服务配置里填 `baseUrl`，保存时会自动
`discover()` 出 `factlayer-default` 空间（见下）。

## 配置字段

| 字段 | scope | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|---|
| `baseUrl` | service | `url` | 是 | `http://127.0.0.1:8000` | shim 地址。保存时宿主强校验 http(s) 且禁止内嵌凭据；末尾斜杠会被归一化掉。 |
| `apiKey` | service | `secret` | 否 | 空 | shim 目前**不校验**鉴权；非空时以 `Authorization: Bearer <key>` 发送。明文只存在于 Provider 内部（`providerConnection()`），GUI/RPC 只看得到「已配置」名单。 |
| `memoryBodyId` | **memory** | `text` | 是 | `factlayer-default` | shim 的固定空间标识（= shim 的 `FACTLAYER_SPACE_ID`）。**discover 会把 `/spaces` 返回的 id 自动写进空间 connection**；手动建空间时要填同一个值，否则 `remember` 直接报错。 |

`manifest.secrets` 恰好等于 `fields` 里 `input:'secret'` 的集合：`["apiKey"]`。

## 两台机器一致性

- **不需要人工约定 `memoryBodyId`**：`discover()` 用 `GET /spaces` 的返回值作为 `externalId`，
  同时把**同一个值**写进 `connection.memoryBodyId`；`remember` 的 `session_id` 就取
  `body.provider.settings.memoryBodyId`（缺值抛错，不兜底）。所以 A 机保存一次服务配置后，
  两台机器写进 shim 的 `session_id` 天然一致。
- **仍建议两台机器的 `baseUrl` 指向同一个 shim**：事实只存在 shim 的 SQLite 里。指向不同
  shim 时，两边各自独立、互不可见——这是部署选择，不是适配器能补救的。
- shim 侧 `GET /search` **不按 `session_id` 过滤**（session 只用于写入分组与会话展开），
  因此 A 机写的记忆 B 机能搜到；这也是 06 §5 的硬约束。

## 行为摘要（适配器侧）

| 方法 | shim 调用 | 行为 |
|---|---|---|
| `discover` | `GET /spaces` | 单空间；`externalId` = 返回的 id，`connection.memoryBodyId` 同值；不可达/空数组/非法 id → 抛错（不返回空） |
| `status` | `GET /health` | 2.5s 超时；超时 → `{healthy:false,error:"starting"}`；503 → `"rebuilding"`；成功结果 **30s stale-while-revalidate**，并发调用合并成一次请求 |
| `search` | `POST /search` | `items` → `results`；`recorded_at`(ms) → ISO 8601；剥掉 `seq/session_id/routes/content_truncated/expanded*/inherited_rank`；坏时间戳 → 省略 `createdAt` + `tags:["no-time"]`（该条仍保留）；`tags` 去逗号、≤8 个 × ≤300 字符；`score` 缺失补 0；`hint` 仅在空结果透传并保证 `NO_MATCH` 前缀 |
| `remember` | `POST /memories` | `session_id` = `settings.memoryBodyId`；`source_client` = `mnemon-provider`；`client_write_id` = 每次 uuid4；回执 `{action:"stored",committed:true,count:1,memoryBodyIds:[空间 id],ids:[记忆 id]}`；超时文案含「结果未知，可能已写入」 |
| `list` | 无 | 空数组（`browse:false`） |
| `graph` | 无 | 空快照（`graph:false`，不产假图） |

能力位：`search`/`remember` 为 true，其余 6 个 false；`writeMode:'exact'`、
`deletionMode:'unsupported'`、`scoreSemantics:'provider-native'`（manifest 与 adapter 两处一致）。

## 与设计的偏差 / 已知限制

1. **`defineMemorySpaceProvider` 的 import 来源**：任务要求动态 `import('dsh-mnemon')`；实测
   `dsh-mnemon@0.5.37` 主入口**没有**导出它（官方 9 个 provider 都从
   `dsh-mnemon-source-memory-spaces/provider-sdk` import）。实现按
   `dsh-mnemon` → `dsh-mnemon-source-memory-spaces/provider-sdk` 顺序探测，两者都没有时抛
   「需要安装 dsh-mnemon」。`peerDependencies` 同时声明了这两个包（以及 `@deepseek-ai/cordis`）。
2. **入口使用顶层 `await`**：动态 import 必须在模块求值期完成，default export 才能真正经过
   `defineMemorySpaceProvider` 包装。Node ≥ 20 的 ESM loader 原生支持；本仓库不装 peer，
   因此单测只能断言「失败文案」而不能断言成功装配（见 `tests/module.test.ts`）。
3. **未与真实 dsh-mnemon 装配联调**：本仓库按任务要求不安装 peer；`host.install` 的形状、
   `providerConnection` 的调用、`discover` 写 `connection` 都按 05/06 的源码行号实现，
   但「装进 profile 后 Memory 页真的出现空间」需要宿主环境验证。
4. **`summaryI18nKey` 没有对应 locale 条目**：manifest 16 字段要求写全，客户端在缺翻译时
   回退到 `summary`（类型注释如此声明）。
5. **status 失败结果不缓存**：只对「成功快照」做 30s SWR。shim 停机时每次目录刷新最多等
   2.5s（超时后报 `starting`），不会误报健康。
6. **未实现**：`get` / `related` / `link` / `forget` / `rememberMany` / `entityIndex` /
   `metadataSample`（能力位全 false；契约里都是可选方法）。
7. **`content` 8192 上限由宿主 Source 执行**（`prepareRemember`），适配器只管管道转发；
   `>8192` 会在到达 shim 之前以 Source 错误失败，不会被截断。
