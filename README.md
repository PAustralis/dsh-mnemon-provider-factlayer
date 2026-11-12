# factlayer

dsh-mnemon 的记忆 provider 后端：一个 FastAPI shim，对上暴露 mnemon provider 的 HTTP 契约（remember / recall / forget 等，接口文档后续单独一版），对内采用「事实层 SQLite（只追加）+ 派生层 embeddings / FTS5」双层存储。存储引擎可换，对外接口稳定。

## 架构

```
            ┌────────────────────────────────────────────┐
            │        mnemon provider HTTP 契约            │
            │      (remember / recall / forget ...)      │
            └──────────────────────┬─────────────────────┘
                                   │  FastAPI shim
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
┌───────▼────────┐        ┌────────▼────────┐        ┌────────▼────────┐
│ 事实层 (SQLite) │        │ 派生层 (可弃)    │        │ 检索层 v8        │
│ 只追加 INSERT   │───────▶│ embeddings 表    │───────▶│ 单混合列 bigram  │
│ 永不 UPDATE     │        │ FTS5 索引        │        │ 路内替换 → RRF   │
│ 永不 DELETE     │        │ derive_state 水位│        │ → 契约输出       │
└────────────────┘        └─────────────────┘        └─────────────────┘
   永远不动                 删光重跑零损失            FINAL_K=4 / 4800 字符
```

主轴：**派生层出错删掉重跑，事实层永远不动。**

## 当前状态

- **第一层（事实层）**：建库（只追加触发器 + FTS5）、写入路径（服务端 id/seq、client_write_id 幂等、409）、读取（按 id 全文 / 按会话截断输出）、supersedes 作废边、派生水位循环、自动备份调度（VACUUM INTO + 原子替换）、`/health`。设计见 [docs/01-事实层设计.md](docs/01-事实层设计.md)。
- **第二层（检索层，v8 + 2026-11-11 重测）**：出厂默认 **R1 vector 单路**（真实 embedding 重测 R@6=**0.8729**；bigram 基线 0.4875 仅作向量不可用时的降级路，**不做 RRF 融合**——等权融合只有 0.6833）；`gseq` 全局单调序；hidden 唯一口径（20 跳、可达存活链头）；路内替换；单路直出（score 仍 1/(60+rank)）→ FINAL_K=4；契约输出（数值 score、ISO 8601 UTC createdAt、tags 状态通道、1200/4800 字符预算）。设计见 [docs/02-检索层设计.md](docs/02-检索层设计.md)、重测报告 [docs/04-embedding重测报告.md](docs/04-embedding重测报告.md)、契约映射 [docs/03-对接层契约映射.md](docs/03-对接层契约映射.md)。
- **第三层（对接层，`plugin/` + shim 侧 `/spaces` / O(1) `/health`）**：一个极薄的 TypeScript provider 适配器包 `dsh-mnemon-provider-factlayer`（`defineMemorySpaceProvider` + `host.install`），把 mnemon 契约的 `discover/status/search/remember/list/graph` 逐方法翻译成对 shim 的一次 HTTP 调用；shim 侧新增 `GET /spaces`、`/health` 后台快照、重建期 503。设计见 [docs/06-对接层设计.md](docs/06-对接层设计.md)、接口契约 [docs/05-provider接口契约.md](docs/05-provider接口契约.md)。安装与配置见 [plugin/README.md](plugin/README.md)。

## 运行

```bash
python -m pip install fastapi uvicorn pytest httpx
cp .env.example .env   # 填 DSH_FACT_DB_PATH 等
python -m uvicorn app.main:create_app --factory --workers 1   # 固定单 worker（写路径依赖进程内单写锁）
python -m app.main     # 等价入口，同样用工厂创建 app
python -m pytest       # 运行测试
```

`app.main` 不再在模块级创建 `app`（工厂模式）：`import app.main` 无副作用，不会在当前目录生成 `facts.db`。启动后后台线程每 `DSH_BACKUP_INTERVAL_SECONDS`（默认 86400，设 0 关闭）检查一次 `last_backup_at`，到期自动备份；备份失败会在 `/health` 的 `last_backup_error` / `backup_consecutive_failures` 暴露，完整堆栈只写服务端日志。同一后台线程还负责 SQLite 维护：无写入空闲时执行 WAL `TRUNCATE` 检查点（`DSH_WAL_CHECKPOINT_INTERVAL_SECONDS`/`DSH_WAL_CHECKPOINT_IDLE_SECONDS`），并按 `DSH_FTS_OPTIMIZE_INTERVAL_SECONDS`（默认 86400）执行 FTS5 optimize；两者失败只记日志并进 `/health`。

主要端点：`POST /memories`、`GET /memories/{id}`（全文）、`GET /memories?session_id=`（输出处截断至 1200 字符并标注）、`POST /supersedes`、`POST /search`、`GET /health`、`GET /spaces`（对接层 discover），以及 admin 端点 `GET /admin/search`（`include_superseded=1`，模型路径不可达）、`POST /admin/rebuild`（完成后自动 FTS5 optimize）、`POST /admin/checkpoint`（默认 `PASSIVE`；支持 `?mode=TRUNCATE`）、`POST /admin/canary`、`POST /admin/backup`。

对接层（第三层，`plugin/`）的 shim 侧约定（docs/06 v3）：

- **`GET /spaces`**：`[{"id": "<FACTLAYER_SPACE_ID>", "name": "factlayer"}]`，只读配置、O(1)，不查库。适配器的 `externalId` 与 `connection.memoryBodyId` 都取这个值。
- **`GET /health` 是后台快照**：后台线程每 `HEALTH_REFRESH_SECONDS`（默认 5）刷新 pending / 索引缺口 / 备份状态 / 熔断 / 金丝雀，请求只读缓存（O(1)）；快照未就绪时返回 `{"status": "starting"}`，重建期带 `rebuilding: true`。快照还包含 `sqlite`（`db_bytes` / `wal_bytes` / `wal_autocheckpoint` / `last_checkpoint_at/mode/error`）与 `fts_optimize`（`last_at` / `last_error`）两个运维段。
- **重建期不静默返回部分结果**：`POST /admin/rebuild` 触发即置 rebuilding 标志，期间 `POST /search` 与 `/memories`（读/写）返回 `503 {"error":"rebuilding","message":"索引重建中，请稍后"}`，完成/失败后恢复。
- **`/search` 不按 `session_id` 过滤**（session 只用于写入分组与会话展开），因此 A 机写入、B 机也能搜到。

admin 端点由 `ADMIN_TOKEN` 保护：非空时要求 `X-Admin-Token: <token>` 或 `Authorization: Bearer <token>`；为空时只接受回环访问（含 TestClient）。

## 配置要点（v8）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `RETRIEVAL_ROUTES` | `vector` | 默认向量单路；bigram 仅在向量不可用/未配置时降级（需 `EMBEDDING_MODEL`） |
| `FINAL_K` | `4` | 契约 strict-v1 默认 maxRankedResults=4 |
| `ITEM_MAX_CHARS` / `TOTAL_MAX_CHARS` | `1200` / `4800` | 单条 / 总字符预算 |
| `EXPAND_ENABLED` | `false` | 会话展开默认关；开启时只占 FINAL_K 名额内 |
| `RETRIEVAL_AUTO_DEGRADE` | `false` | 设计 §12 不做运行时质量自动降级；仅离线评测复现时显式打开 |
| `ADMIN_TOKEN` | 空 | 空则 admin 端点仅回环 |
| `EMBEDDING_MAX_SPEND_USD` | `0.50` | 向量重建预算上限 |
| `FACTLAYER_SPACE_ID` | `factlayer-default` | provider discover 的固定空间标识；`remember` 的 `session_id` 同值 |
| `HEALTH_REFRESH_SECONDS` | `5` | `/health` 后台快照刷新间隔；请求路径 O(1) 只读缓存 |
| `DSH_WAL_AUTOCHECKPOINT_PAGES` | `1000` | 每个连接的 `PRAGMA wal_autocheckpoint`（页；0 = 关闭自动检查点） |
| `DSH_WAL_CHECKPOINT_INTERVAL_SECONDS` | `300` | 后台 TRUNCATE 检查点最小间隔（0 = 关闭） |
| `DSH_WAL_CHECKPOINT_IDLE_SECONDS` | `60` | 距上次写入超过该秒数才执行后台 TRUNCATE |
| `DSH_FTS_OPTIMIZE_INTERVAL_SECONDS` | `86400` | 后台 FTS5 optimize 间隔（0 = 关闭；重建后仍会 optimize） |
| `DSH_DERIVE_BATCH_SIZE` | `500` | 派生重建 executemany 每批行数 |

## 迁移说明

- **`gseq` 主键迁移（v8）**：`memories` 从 `id TEXT PRIMARY KEY` 重建为
  `gseq INTEGER PRIMARY KEY AUTOINCREMENT`（rowid 别名；`id TEXT UNIQUE NOT NULL`
  保留为对外标识；`seq` 语义不变）。SQLite 改主键必须重建表，`init_db()` 的
  `migrate_memories_gseq()` 会自动：DROP 防写/FTS 触发器 → 建新表 →
  `INSERT … SELECT` 按旧 rowid 显式填 `gseq` → 重建 FTS（external content rowid 稳定）→
  重建触发器与索引 → 更新 `sqlite_sequence`。
- **保留旧库升级时必须完成派生重建（P0，绝不静默丢数据）**：`migrate_memories_gseq()`
  只重建了原文 `memories_fts`；检索层后加的 `memories_ngrams` / `memories_fts_bigram`
  （bigram 派生）在 legacy 库迁移后是空表，**旧记忆会永久搜不到**。因此 `init_db()`
  在检测到「刚完成 memories 迁移」或「`memories_ngrams` / `memories_fts_bigram`
  行数 != `memories` 行数」时，会**在启动时强制跑一次完整派生重建**（等价
  `POST /admin/rebuild` 的 ngram + FTS 派生；`EMBEDDING_MODEL` 未配置时跳过
  embedding 并记录原因，不报错）：
  - 重建成功：`/health` 的 `migration_needs_rebuild=false`、`migration.status=ok`
    （`migration.embedding_skipped` 记录跳过原因），`index.memories == index.ngrams ==
    index.bigram_fts`。
  - 重建失败：服务端 ERROR 日志 + `/health` 的 `migration_needs_rebuild: true`、
    `alerts` 含 `pending fts > 0`、`migration.status=failed`。此时**不要直接上线**，
    处理磁盘/权限等问题后用 admin 端点**手动补跑**：

    ```bash
    curl -X POST http://127.0.0.1:8000/admin/rebuild \
         -H "X-Admin-Token: $ADMIN_TOKEN"      # ADMIN_TOKEN 为空时用回环地址即可
    ```

    重建后确认 `/health.index.gap == 0`（即 `memories_ngrams` 与
    `memories_fts_bigram` 行数等于 `memories` 行数），再验证一条中文查询。
  - `DSH_DERIVE_LOOP_ENABLED` 默认在 `RETRIEVAL_ROUTES` 含 `vector` 且
    embedding 已配置时开启（否则写入后 embedding 永久 pending、向量路线永久
    降级）；显式设 `0` 才关闭。`/derive/tick` 每轮只处理 50 条，**不能**当作
    迁移补数手段；旧库升级必须完成上面这次重建。
  - 无生产数据时也可以**删库 + 派生重建**，但同样必须重跑 `POST /admin/rebuild`
    （或让启动时自动重建跑完）补齐派生层。
- **supersedes 主键迁移**：早期版本将 `(from_id, to_id, recorded_at)` 作为主键，
  同毫秒 `revoke` 后再 `supersede` 会撞主键。现在主键为自增 `id`，最新边**只按
  `id DESC`**（不依赖 `recorded_at`，时钟回拨不会选错边）；旧表在 `init_db()` 启动时
  自动检测、重建并保留全部边记录。
- **embedding 列迁移**：`embeddings` 自动补 `model_id` / `dim` 列（旧行回填
  `dim = len(vector)/4`、`model_id = meta.embedding_model`）。
- 数据库文件位置由 `DSH_FACT_DB_PATH` 控制；自动备份写入同目录 `backups/`。

## 基线回归

检索层 v8 的 golden fixture 由 `research/recall/make_golden_fixture.py` 生成，
提交在 `tests/fixtures/recall_baseline.json`（40 题基线查询串 + 预期 top-6 +
sha256 + 基线 commit；含 2/40 空结果题的 id 与查询串）。`tests/test_recall_baseline.py`
只读 fixture，重算并断言查询串逐字节相等、R@6 = 0.4875（容差 ≤0.01）。

```bash
python -m pytest tests/test_recall_baseline.py -q
python research/recall/baseline_r2.py     # 原型 + 出厂实现分别跑 R@6
```

## 文档

- [docs/01-事实层设计.md](docs/01-事实层设计.md) —— 事实层（第一层）
- [docs/02-检索层设计.md](docs/02-检索层设计.md) —— 检索层（第二层，v8）
- [docs/03-对接层契约映射.md](docs/03-对接层契约映射.md) —— 检索输出到 mnemon provider 契约的映射
- [docs/05-provider接口契约.md](docs/05-provider接口契约.md) —— provider 完整接口契约（第三层实现手册）
- [docs/06-对接层设计.md](docs/06-对接层设计.md) —— 对接层设计 v3（装配形状 / 能力键名 / 密钥路径 / session_id / O(1) health / 重建期语义）
- [plugin/README.md](plugin/README.md) —— `dsh-mnemon-provider-factlayer` 安装、配置与两台机器一致性说明
