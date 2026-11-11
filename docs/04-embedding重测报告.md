# 04 · 真实 embedding 重测报告（R1 向量路 / §9.3 门槛）

> 日期：2026-11-11　模型：`qwen3.7-text-embedding`（dim=1024，OpenAI 兼容 `/embeddings`）
> 语料：119 条 / 题库 40 题（与 `research/recall/REPORT.md` 同一份）
> 结论：**R1 开，且按实测改为 `vector` 单路默认（不做 RRF 融合）；bigram 仅在向量不可用时降级；MMR 关。**
> 本报告取代 `docs/02` 旧版基于 TF-IDF 替身的 R1 结论。

## 1. 结论

| 项 | 结论 | 依据 |
|---|---|---|
| R1 向量 | **进默认，且是唯一默认路** | R1 单路 R@6 = **0.8729**，比 R2（0.4875）高 **+0.3854**，95% CI [+0.2667, +0.5042] |
| RRF 融合 | **不做**（显式配多路才走） | R1+R2 等权融合 0.6833，比 R1 单路低 **0.1896**，95% CI [−0.2896, −0.0979]（显著负增益） |
| bigram | **降级备用** | 向量未配置 / 熔断 / 库内无向量 / 查询异常时自动接管，`degraded` + `vector-unavailable` tag + hint 标注 |
| MMR | **关** | R1+R2 上加 MMR 后 R@6 从 0.6833 降到 0.6167（−0.0667，95% CI [−0.1167, −0.0207]） |

**一句话**：真实 embedding 下单路已经足够强；旧报告"融合无增益"是 TF-IDF 替身造成的假象，真实向量下融合是**显著负增益**。因此不是"把 R1 加进 bigram 的融合"，而是"用 vector 单路替换 bigram，bigram 只兜底"。

## 2. 端点探测

用产品 `app.embeddings.build_provider()`（`OpenAICompatProvider`）发真实请求；**不记录、不打印 base_url / API key**。

| 项 | 实测 |
|---|---|
| 协议 | HTTP 200，OpenAI `/embeddings` 兼容（`POST {EMBEDDING_BASE_URL}/embeddings`） |
| 请求 | `{"model": "qwen3.7-text-embedding", "input": [...]}`，`Authorization: Bearer <key>` |
| 返回顶层键 | `data` / `id` / `model` / `object` / `usage` |
| `data[i]` 键 | `embedding` / `index` / `object`；`index` 可用于对齐 |
| 维度 | **1024**（`EMBEDDING_DIM=auto` 由第一次调用推断） |
| 归一化 | 服务端已返回单位向量（L2 = 1.00000）；客户端仍按 §7 再归一化为 float32 |
| batch | **支持**：一次 16 条成功（`data` 16 条、`index` 0–15）；全量 159 条用 10 次调用完成 |
| `usage` | `{"prompt_tokens": N, "total_tokens": N}`，逐次可读（评测脚本据此计费） |
| 单条延迟 | 原始 HTTP 172 ms；经产品客户端 336 ms（含 httpx 建连） |
| batch16 延迟 | 原始 HTTP 415 ms；经产品客户端 472 ms |
| 全量延迟 | 10 批：median 476 ms / p95 552 ms / min 420 ms / max 569 ms；159 条总墙钟 **5.46 s**（含缓存写入） |
| 产品超时 | 评测沿用 `.env` 的 `EMBEDDING_TIMEOUT_SECONDS=3`；单批均 < 0.6 s，未触发 |

环境注记：本机 `NO_PROXY` 里含 `[::1]`，httpx 0.28.1 在解析代理环境时会抛 `InvalidURL`（产品客户端同样会中招）。评测脚本只在**进程内**把 `[::1]` 规范化为 `::1`，未改产品代码、未改用户环境；部署机若同样配置需注意。

## 3. 成本

价格未知（阿里云 MaaS 未确认公开单价），**只报实际 token，不编美元数**。

| 阶段 | 调用 | 文本 | input tokens |
|---|---|---|---|
| 端点探测 | 4（产品单条 + 原始单条 + 产品 batch16 + 原始 batch16） | 1 + 1 + 16 + 16 | **1 016** |
| 全量评测（只跑一次） | 10（batch 16/16/…/15） | 159 | **4 372** |
| **合计** | **14** | — | **5 388** |

- 评测均值：4 372 / 159 ≈ **27.5 tokens/条**（中文短句，约 1 token/字 + 少量开销）。
- 重复运行：缓存命中 159 条、新调用 0、**0 token / 0 费用**（已实测复跑）。
- 缓存：`research/recall/.embedding_cache.json`，按 `model + sha256(text)` 存原始向量，**已加入 `.gitignore`**；文件内同时记录构建用量（`usage.prompt_tokens=4372`），复跑时报告仍能显示真实花费。删缓存才会重新计费。
- 探测的 4 次调用里两次走产品客户端（不暴露 usage），token 取自**同 payload 的原始调用**（同文本、同 batch，分词确定），误差为 0。

## 4. 方法

- **数据**：`E:\Tools\projects\dsh-mnemon-provider-graphiti\research\phase2\corpus.json`（119 条，sha256 `2f04abf0…`）+ `读者测试40-题库.md`（40 题）。query = 【用户问】原文，只做 NFKC；golden = 题库记忆条目按 NFKC 精确映射回语料的记录（优先同 scenario）。映射失败 0 条，gold 合计 119 条。
- **R2（对照）**：产品 `app.textnorm` 构造单混合列（与基线逐 token 同构），FTS5 `unicode61` + `bm25 ASC, rowid ASC`，取 top-50。**R@6 复现 0.4875，与 §1 基线逐位一致**；查询/索引构造器与原型逐 token 相同的冻结校验 PASS。
- **R1**：产品 `OpenAICompatProvider.embed` 取 1024 维向量，客户端 L2 归一化，余弦（点积）排序取 top-50。
- **融合**：等权 RRF，`k=60`，`Σ 1/(60+rank)`；与 `docs/02 §5` 一致，无路权重（§12 明确不做 RRF 加权）。
- **MMR**：在 R1+R2 融合前 20 名里迭代选 10，`λ=0.7`，相关性用融合分 min-max 归一化，多样性用余弦。
- **指标**：R@6（主）、R@10、MRR、覆盖@50（该路 top-50 捞到 golden 的比例）、空结果数。评测按 §9.1/§9.3 的 **FINAL_K=6** 口径；产品契约默认 FINAL_K=4 未改。
- **配对 bootstrap**：对 40 题按题重采样 2000 次（配对差值），百分位 95% CI，种子 `20261111`。
- **泄漏核对**：把 query 向量随机置换后重算，R@6 = **0.0542**（随机基线 0.0505），原 R@6 = 0.8729 —— 排除索引错位/标签错位。
- **产品路径复算**：在临时库里用**最终产品代码**（`create_memory` → `DerivePipeline` → `RetrievalService`，routes=`vector`、FINAL_K=6）重跑 40 题；向量来自缓存，**0 次联网**。R@6 = **0.8729**，与原型逐位一致；`routes=[["vector"]]`、`degraded=False`、单路名次分校验通过。

## 5. 结果

40 题平均：

| 配置 | R@6 | R@10 | MRR | 覆盖@50 | 空结果 |
|---|---:|---:|---:|---:|---:|
| **R1 向量（单路）** | **0.8729** | **0.9083** | **0.9258** | **0.9437** | 0 |
| R2 二元组（单路） | 0.4875 | 0.5000 | 0.6269 | 0.5000 | 2 |
| R1+R2（RRF k=60） | 0.6833 | 0.8417 | 0.8038 | 0.9437 | 0 |
| R1+R2 + MMR（λ=0.7） | 0.6167 | 0.7292 | 0.7844 | 0.7292¹ | 0 |

¹ MMR 的"覆盖@50"实际只有选出的 10 条，和单路 top-50 不同口径，不能横比。

**逐题结构**（比均值更能说明问题）：

- R1 对 R2：**23 题更好、17 题持平、0 题更差**；R1 在 30/40 题上 R@6 = 1.00。
- R2 有 10 题 R@6 = 0（sid 1,3,13,17,20,21,22,23,29,38），R1 在这 10 题上全部 > 0。
- §3.2 记录的 2 道 bigram 空结果题：sid 23（`2026 年上半年我住在哪个城市?`）R1 R@6 = 0.67；sid 38（`我平时喝什么茶?`）R1 R@6 = 1.00。
- **融合对 R1 是纯拖累**：R1+R2 在 **13 题**上比 R1 单路差（sid 1,3,11,13,17,20,21,22,25,29,30,31,40），0 题更好。例：sid 3 / 20 / 22 从 1.00 掉到 0.00，sid 40 从 1.00 掉到 0.33——R2 的高分错误项把 R1 的正确答案挤出了 top-6。
- MMR 在 8 题上比无 MMR 差、2 题更好、30 题持平；多样性惩罚压掉了同场景的多条正确记忆。

## 6. 配对 bootstrap（95% CI，2000 次）

| 对比 | ΔR@6 | ΔR@10 | ΔMRR |
|---|---:|---:|---:|
| **R1 − R2**（§9.3 主判定） | **+0.3854** [+0.2667, +0.5042] | +0.4083 [+0.2896, +0.5188] | +0.2990 [+0.1646, +0.4409] |
| R1+R2 − R2（融合对照） | +0.1958 [+0.1166, +0.2792] | +0.3417 [+0.2313, +0.4480] | +0.1769 [+0.0865, +0.2844] |
| **R1+R2 − R1**（融合是否拖累） | **−0.1896** [−0.2896, −0.0979] | −0.0667 [−0.1333, −0.0166] | −0.1221 [−0.2219, −0.0311] |
| **MMR − 无 MMR** | **−0.0667** [−0.1167, −0.0207] | −0.1125 [−0.1708, −0.0583] | −0.0194 [−0.0396, −0.0025] |

三个主对比的 CI 都不含 0：R1 显著更好、融合显著更差、MMR 显著更差。

## 7. §9.3 门槛对照

§9.3：**R@6 提升 ≥ 0.05 且 bootstrap 95% CI 不含 0**。

| 判据 | 门槛 | 实测 | 判定 |
|---|---|---:|---|
| R1 单路 vs R2 | ΔR@6 ≥ 0.05，CI > 0 | +0.3854，CI [+0.2667, +0.5042] | **通过** |
| R1+R2 融合 vs R2 | 同上 | +0.1958，CI [+0.1166, +0.2792] | 也通过，但被否决：比 R1 单路低 0.1896（CI 全负） |
| MMR（R1+R2 上） | 同上 | −0.0667，CI [−0.1167, −0.0207] | **不通过（负增益）** |

**判定**：

- **R1 进默认**，并且默认路线取 **`RETRIEVAL_ROUTES=vector` 单路**（不是 `bigram,vector` 融合）。理由：融合虽然也能过"比 R2 高 0.05"的线，但显著低于 R1 单路；§12 明确不做 RRF 加权，因此没有"给 R1 更大权重"的折中项，直接单路是唯一不引入负增益的选择。
- **MMR 关**：负增益且 CI 不含 0；§9.3 的"同理"门槛不满足。

**关于旧结论的反转**：`docs/02 §1` 旧表里 R1（TF-IDF 替身）只有 0.4583，低于 R2，所以"融合无增益 / 31 种组合没一个超过最好单路"是合理的；真实 embedding 把 R1 拉到 0.8729 后，弱路 R2 的每一票都在把强路的正确项往下挤——**等权 RRF 的前提（两路质量接近）不成立**。这不是参数问题，是融合假设被数据推翻。

## 8. 产品落地（最终代码）

1. `app/config.py`：`DEFAULT_ROUTES = "vector"`（默认单路）。
2. `app/retrieval.py`：
   - 单路**跳过 RRF 求和**，直接按该路名次定序（score 仍 `1/(60+rank)`，契约数值语义不变）；
   - `routes == ("vector",)` 且向量不可用（provider 缺失 / 熔断 / 空索引 / 查询异常）时，本次查询追加 bigram、**不融合**；响应 `degraded` 置真，非空结果首条 `tags:["vector-unavailable"]`，空结果 hint 列出 `vector(unavailable:…) + bigram(ok)` 与 `degraded=…`；
   - 显式配置多路时仍按原 RRF 融合（兼容旧配置，不改语义）。
3. `app/main.py`：vector 路线但 embedding **未配置**（MODEL / BASE_URL 为空）时不再拒绝启动，记 warning 并让查询降级 bigram；**配置了但 build_provider 失败**仍在启动时报错（§8 硬规则 4 保留）。
4. 测试：`167 passed`。新增/改写覆盖：默认路 = vector；未配置启动降级；无 provider / 空索引 / 熔断降级；单路不融合、score = 1/61；多路融合与既有契约测试全部保持。

产品路径复算（最终代码、缓存 provider、0 联网）：**R@6 = 0.8729**，与原型一致；`routes=[["vector"]]`、`degraded=False`、单路名次分校验 `True`。

运维语义：默认部署需要 `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` /（视服务）`EMBEDDING_API_KEY`；不配也能启动，但每次查询会降级 bigram（`/health.retrieval` 可见）。`RETRIEVAL_ROUTES=bigram` 仍可显式回到纯词面模式。

## 9. 与旧报告对照

| 项 | 旧（TF-IDF 替身，`research/recall/REPORT.md`） | 新（真实 embedding） |
|---|---:|---:|
| R1 单路 R@6 | 0.4583 | **0.8729** |
| R2 单路 R@6 | 0.4875 | 0.4875（复现） |
| 最好单路 | R2 0.4875 | **R1 0.8729** |
| 融合 vs 最好单路 | 0 个组合超过 | 融合 0.6833，**比最好单路低 0.1896** |
| MMR | 0 增益 | **−0.0667（显著负）** |
| 出厂默认 | bigram | **vector 单路 + bigram 降级** |

## 10. 局限

- 规模：119 条 / 40 题，top-6 只有 6 个位置；一条翻转≈0.01。数千条必须按 `docs/02 §13` 重测。
- 语料与题库是人造场景，golden 是"相关记录"不是答案集；真实记忆的分布会更长尾。
- 只用了一个 embedding 模型/端点；没有做查询改写、混合检索、cross-encoder。
- 价格未知，只报 token；若后续拿到单价，直接用 5 388 input tokens 折算。
- 评测口径 FINAL_K=6（§9.1）；产品契约默认 FINAL_K=4，未改。

## 11. 复现

```powershell
cd E:\dsh-mnemon-provider-graphiti
# 首次：读 .env 的 EMBEDDING_*，159 条文本真实计费（本次 4372 tokens）
python research\recall\run_real_embedding.py
# 复跑：读 .embedding_cache.json（0 token / 0 费用），并跑最终产品代码复算
python research\recall\run_real_embedding.py --sanity
```

产物：

- `research/recall/run_real_embedding.py`（提交）：真实向量 + 缓存 + 指标 + bootstrap + 产品路径复算；
- `research/recall/real_embedding_results.json`（提交）：全部数字、CI、成本、逐题明细；
- `research/recall/.embedding_cache.json`（**gitignore**，不提交）：159 条向量 + 构建用量；
- `docs/02-检索层设计.md`（§0/§1/§2/§7/§9.3/§10/§11/§13 同步新默认）；
- `.env.example`（默认 `RETRIEVAL_ROUTES=vector` + 降级说明）、`README.md`、`docs/01`（默认描述同步）。
