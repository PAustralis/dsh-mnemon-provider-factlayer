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

第一层（事实层）已实现：建库（schema 含只追加触发器与 FTS5）、写入路径（服务端 id/seq、client_write_id 幂等、409）、读取（按 id 全文 / 按会话截断输出）、supersedes 作废边、派生水位循环骨架（stub）、/health、VACUUM INTO 备份。设计定稿见 [docs/01-事实层设计.md](docs/01-事实层设计.md)。

## 运行

```bash
python -m pip install fastapi uvicorn pytest httpx
cp .env.example .env   # 填 DSH_FACT_DB_PATH 等
python -m uvicorn app.main:app --workers 1   # 固定单 worker（写路径依赖进程内单写锁）
python -m pytest       # 运行测试
```

主要端点：`POST /memories`、`GET /memories/{id}`（全文）、`GET /memories?session_id=`（输出处截断至 1200 字符并标注）、`POST /supersedes`、`GET /health`、`POST /derive/tick`、`POST /admin/rebuild`、`POST /admin/backup`。

## 文档

- [docs/01-事实层设计.md](docs/01-事实层设计.md) —— 事实层（第一层）定稿设计
