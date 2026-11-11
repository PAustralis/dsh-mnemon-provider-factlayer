# recall 评测原型（一次性，非产品代码）

回答一个问题：**四路召回里哪几路真的有用，RRF / MMR 带来多少提升。**
用真实语料 119 条 + 人工题库 40 题，主指标 recall@6。结论在 [`REPORT.md`](REPORT.md)。

> 本目录不属于产品代码，不参与构建，不修改 `app/` 或 `tests/`。

## 文件

| 文件 | 作用 |
|---|---|
| `run_real_embedding.py` | **真实 embedding 重测**（§9.3 门槛）：119 语料 + 40 题取真实向量 → R1/R2/RRF/MMR + 配对 bootstrap；另用最终产品代码复算 R@6。向量缓存 `.embedding_cache.json`（gitignore，重跑 0 费用） |
| `real_embedding_results.json` | 真实 embedding 重测的全部指标、CI、成本、逐题明细、产品路径复算结果 |
| `recall_eval.py` | 主实验：建临时 SQLite（FTS5 三套索引）→ 五路召回 → 31 种组合消融 → k/λ/T 扫描 → 自己写 `results.json` |
| `make_report.py` | 把 `results.json` 填进 `REPORT.template.md` 的 `{{G1}}`…`{{G11}}` 占位符，渲染成 `REPORT.md` |
| `REPORT.template.md` | 报告的**叙述部分**（人写）+ 表格占位符。改结论改这个文件 |
| `REPORT.md` | 最终报告（渲染产物，含全部数字表） |
| `map_golden.py` | 题库 ↔ 语料映射诊断，产出 `diag_mapping.md` |
| `diag_mapping.md` | 映射诊断结果（119/119 命中、无关项、日期一致性） |
| `results.json` | 全量明细：每路每题的 top-50、每配置每题的 recall@6/@10/MRR |

## 怎么跑

```powershell
cd E:\dsh-mnemon-provider-graphiti\research\recall
python recall_eval.py     # ~1 秒，重建 _tmp_eval.sqlite 并写 results.json
python make_report.py     # 渲染 REPORT.md
python map_golden.py      # 可选：重跑映射诊断
python run_real_embedding.py   # 真实 embedding 重测（联网、计费；向量按文本缓存，重跑 0 费用）
```

依赖：Python 3.7+（本机 3.14.7）、标准库 `sqlite3`（**需要 FTS5 且支持 `tokenize='trigram'`**，本机 SQLite 3.50.4）、`numpy`（`run_real_embedding.py` 另需 `httpx` 与仓库 `.env` 的 `EMBEDDING_*`）。
除 `run_real_embedding.py` 外无网络请求、不下载模型、不装重依赖；该脚本的向量缓存在 `.embedding_cache.json`（已 gitignore），删掉才会重新计费。

数据路径写死在脚本顶部（**都在另一个 checkout 下，本仓库里没有**，这是刻意的：只读真实语料，不复制一份进来）：

- 语料 `CORPUS` = `E:\Tools\projects\dsh-mnemon-provider-graphiti\research\phase2\corpus.json`
- 题库 `QBANK` = `C:\Users\ZhaoY\Desktop\读者测试40-题库.md`

换机器/换路径时改这两个常量即可。

自检 FTS5 是否可用：

```powershell
python -c "import sqlite3;print(sqlite3.sqlite_version);sqlite3.connect(':memory:').execute(\"CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')\")"
```

## 关键实现约定

- **query = 题库的【用户问】原文**，只做 NFKC 归一化。不做查询改写、不做停用词剔除、不做同义扩展——避免在测试集上调参。代价是两个文本召回都处于"未优化"状态（偏悲观）。
- **每路固定取 top-50**；RRF `Σ 1/(60+rank)`；MMR λ=0.7、候选池 20、选 10 取前 6。
- **R4（最近 50）** 用 `corpus.raw_date` 降序。`raw_date` 是内容日期而非记录写入时间，12 条 `日期不详` 按最旧处理（因此进不了 top-50）。
- **R1 是向量路的替身**：本机没有 fastembed / sentence_transformers / onnxruntime / 任何 bge 缓存，按约定不联网下载，改用字符 2-3gram TF-IDF 余弦。**它本质上是词面匹配，不代表真实向量召回能力**，报告里所有涉及 R1 的结论都带这个限定。
- **真实 embedding 重测见 `run_real_embedding.py`**（R1=0.8729，bigram=0.4875，等权融合=0.6833）；本 README/`REPORT.md` 的 R1 数字是 TF-IDF 替身口径，结论已被 `docs/04-embedding重测报告.md` 取代。
- 融合、MMR、指标都是纯函数式实现，没有随机性；唯一随机处是 `results.json` 里 bootstrap 的重采样（固定种子 7）。

## 指标口径

- `recall@6` = |top6 ∩ golden| / |golden|，40 题平均（主指标）。
- `recall@10`、`MRR`（第一个 golden 的倒数排名，取不到记 0）、`平均命中数@6`（绝对条数）。
- `候选池覆盖@50` = 该路 top-50 里能捞到多少比例的 golden——把"能不能召回"和"排得好不好"分开看。
- 随机排序基线与 oracle 上界都由脚本实测（蒙特卡洛 300 次，种子 20261111）。

## 结论速览

> 下面三行是 **TF-IDF 替身**口径的历史结论；真实 embedding 重测后出厂已改为 `vector` 单路
> （R@6=0.8729，融合 0.6833 为负增益，MMR 负增益），见 `docs/04-embedding重测报告.md`。

见 `REPORT.md`。三行版：

1. 四路里只有 **R3 二元组**（0.4875）和 **R1 向量替身**（0.4583）真的有用；R4 最近 50（0.0667）≈ 随机且在融合里显著有害；R5 中文基线（0.0000）完全失效。
2. **RRF 在 recall@6 上零增益**（31 种组合没有一个超过最好单路），唯一正收益是 MRR +0.0079；**MMR 是空操作**（0 个组合变好）。
3. 主因是规模：119 条语料 + top-50 候选 = 42% 的库都在池子里，R1 与 R3 的覆盖是包含关系，融合没有可互补的信息。
