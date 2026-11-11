# factlayer

dsh-mnemon 的记忆 provider 后端：一个 FastAPI shim，对上暴露 mnemon provider 的 HTTP 契约（remember / recall / forget 等，接口文档后续单独一版），对内采用「事实层 SQLite（只追加）+ 派生层 FalkorDB / embeddings / FTS5」双层存储。存储引擎可换，对外接口稳定。

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
│ 事实层 (SQLite) │        │ 派生层 (可弃)    │        │ 检索四路 → 融合   │
│ 只追加 INSERT   │───────▶│ FalkorDB 实体图  │        │ 向量/FTS5/图/近期 │
│ 永不 UPDATE     │        │ embeddings 表    │        │ RRF → MMR → 展开 │
│ 永不 DELETE     │        │ FTS5 索引        │        │                  │
└────────────────┘        └─────────────────┘        └─────────────────┘
   永远不动                 删光重跑零损失
```

主轴：**派生层出错删掉重跑，事实层永远不动。**

## 当前状态

第一层（事实层）已实现：建库（schema 含只追加触发器与 FTS5）、写入路径（服务端 id/seq、client_write_id 幂等、409）、读取（按 id 全文 / 按会话截断输出）、supersedes 作废边、派生水位循环骨架（stub）、自动备份调度（后台线程，VACUUM INTO + 原子替换）、/health。设计定稿见 [docs/01-事实层设计.md](docs/01-事实层设计.md)。

## 运行

```bash
python -m pip install fastapi uvicorn pytest httpx
cp .env.example .env   # 填 DSH_FACT_DB_PATH 等
python -m uvicorn app.main:create_app --factory --workers 1   # 固定单 worker（写路径依赖进程内单写锁）
python -m app.main     # 等价入口，同样用工厂创建 app
python -m pytest       # 运行测试
```

`app.main` 不再在模块级创建 `app`（工厂模式）：`import app.main` 无副作用，不会在当前目录生成 `facts.db`。启动后后台线程每 `DSH_BACKUP_INTERVAL_SECONDS`（默认 86400，设 0 关闭）检查一次 `last_backup_at`，到期自动备份；备份失败会在 `/health` 的 `last_backup_error` / `backup_consecutive_failures` 暴露，完整堆栈只写服务端日志。

主要端点：`POST /memories`、`GET /memories/{id}`（全文）、`GET /memories?session_id=`（输出处截断至 1200 字符并标注）、`POST /supersedes`、`GET /health`、`POST /derive/tick`、`POST /admin/rebuild`、`POST /admin/backup`。

## 迁移说明

- **supersedes 主键迁移**：早期版本将 `(from_id, to_id, recorded_at)` 作为主键，同毫秒 `revoke` 后再 `supersede` 会撞主键。现在主键为自增 `id`，旧表结构在 `init_db()` 启动时自动检测、重建并保留全部边记录，无需人工操作；本项目尚无生产数据，也可直接删除旧库重建。
- 数据库文件位置由 `DSH_FACT_DB_PATH` 控制；自动备份写入同目录 `backups/`。


## 文档

- [docs/01-事实层设计.md](docs/01-事实层设计.md) —— 事实层（第一层）定稿设计
