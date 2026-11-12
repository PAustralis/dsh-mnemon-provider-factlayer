# research/bench — LongMemEval 题库（第 2 步产物）

> 只做**数据下载 / 转换 / 自动校验**：没有改 `app/`、没有改 profile、没有跑评测。
> 转换器：[`convert_longmemeval.py`](convert_longmemeval.py)　校验器：[`check_bank.py`](check_bank.py)
> 报告：[`docs/14-LongMemEval题库转换报告.md`](../../docs/14-LongMemEval题库转换报告.md)

## 1. 来源、版本、许可

| 项 | 值 |
|---|---|
| 数据集 | LongMemEval (cleaned)，`xiaowu0162/longmemeval-cleaned` |
| 固定版本 | revision `98d7416c24c778c2fee6e6f3006e7a073259d48f`（2025-09-19） |
| 许可 | **MIT**（dataset card） |
| 论文 | Wu et al., *LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory*, ICLR 2025，[arXiv:2410.10813](https://arxiv.org/abs/2410.10813) |
| 规模 | 500 题；三份语料 `_oracle`（1–6 会话）/ `_s`（约 48 会话）/ `_m`（约 500 会话） |
| 语言 | 英文 |

**下载方式**（[`fetch_raw.py`](fetch_raw.py)：先直连，失败退 `hf-mirror.com`；`requests.trust_env=False` 忽略系统代理）：

```powershell
python research/bench/fetch_raw.py --files oracle s m
```

本次实测：直连可用（约 0.9 MB/s），oracle 15.4MB / 17s，s 277.4MB / 309s；内容长度与 HF 侧完全一致。

**哈希口径**：HF 的 `ETag` 是 **xetHash，不是 sha256**（实测 oracle/s 的 ETag 与本地 sha256 都不同，见 `checks.json` 无关）。
权威 sha256 是 HF LFS oid，已用 tree API 核对：oracle/s 的 oid **等于**本地实测 sha256（`821a2034…` / `d6f21ea9…`），可作来源证明。
`m` **没有下载**（2,737,100,077 bytes；step 2 不需要，转换产物约 3GB 无法入库），其权威 sha256 记录在
[`raw/longmemeval/SHA256SUMS`](raw/longmemeval/SHA256SUMS) 的注释里（取自 LFS oid，本地未复算）。
需要时一条命令即可下载并转换：

```powershell
python research/bench/fetch_raw.py --files m
python research/bench/convert_longmemeval.py --split m --out research/bench/converted/local/longmemeval_m.jsonl
```

## 2. 目录与入库策略

| 文件 | 大小 | 入库 | 说明 |
|---|---:|---|---|
| `raw/longmemeval/longmemeval_oracle.json` | 15.4 MB | ✅ | 原始文件，只读不改 |
| `raw/longmemeval/longmemeval_s_cleaned.json` | 277.4 MB | ❌ gitignore | 原始文件（>50MB） |
| `raw/longmemeval/SHA256SUMS` | — | ✅ | oracle/s 本地实测，m 为 LFS oid |
| `converted/longmemeval.jsonl` | 16.5 MB | ✅ | **读者级题库**（oracle，500 题） |
| `converted/local/longmemeval_s.jsonl` | 307.4 MB | ❌ gitignore | **证据级题库**（s，500 题）；`converted/local/` 整目录不入库 |
| `converted/samples/longmemeval_s_first20.jsonl` | 11.8 MB | ✅ | s 的前 20 条转换结果（格式样本） |
| `manifest.json` | 0.4 MB | ✅ | 逐题映射：kept/discarded、原因、fact 计数、split 用途、数据集元信息 |
| `checks.json` | 70 KB | ✅ | 五组自动检查结果（覆盖 / 泄漏 / 重建 / 时间戳 / 语言） |
| `.llm_cache_checks.json` | — | ❌ gitignore | LLM 调用缓存（160 次真实调用） |

大文件按仓库既有约定放 gitignore，仓库里留**哈希 + manifest + 样本**；README 给出重新下载/转换命令。

## 3. 转换口径

每题一条记录，字段：

```jsonc
{
  "id": "lme-oracle-gpt4_2655b836",   // lme-{split}-{question_id}
  "source": "longmemeval",
  "source_item_id": "gpt4_2655b836",  // 数据集原始 question_id
  "split": "oracle",                  // oracle | s | m
  "category": "temporal",             // 见下表（abstention 优先）
  "source_question_type": "temporal-reasoning",
  "is_abstention": false,
  "question": "...",
  "question_date": "2023/04/10 (Mon) 23:07",  // 提问时刻；可用作 as-of 过滤
  "gold_answer": "GPS system not functioning correctly",
  "acceptable_answers": ["..."],      // 原样 + 机械归一化变体
  "facts": [                          // 对话轮 = 事实
    {"fact_id": "answer_4be1b6b4_2#t0", "text": "对话原文", "date": "2023-04-10",
     "date_kind": "session_time", "date_raw": "2023/04/10 (Mon) 17:50",
     "session_id": "answer_4be1b6b4_2", "turn_index": 0, "role": "user", "has_answer": true}
  ],
  "gold_evidence_fact_ids": ["answer_4be1b6b4_2#t0", "..."],  // 轮级，不是会话级
  "distractor_fact_ids": ["..."],
  "evidence_source": "turn:has_answer",   // 或 session_fallback
  "n_facts": 36, "n_gold": 2, "n_distractor": 34
}
```

- `text` 只保留对话原文（user/assistant 的 `content`），不改写、不把答案塞进事实。
- `date` 是**会话发生时间**（`date_kind=session_time`），不是文本里的事件时间；详见 `checks.json.timestamps`。
- `gold_evidence_fact_ids` 由 `has_answer:true` 的**轮**定位；`answer_session_ids` 只用于校验落点。
  21 道 `_abs` 题在整份数据里没有任何 `has_answer:true` 轮，这类题改用 `answer_session_ids` 的
  全部轮做证据并标 `evidence_source=session_fallback`（共 21 题，占 4.2%）。
- `acceptable_answers`：原样 + 去包裹引号 + 去句末句号 + 拆 `"X. Y is also acceptable"` +
  取括号里的 `(or Y)`；纯机械规则，不做语义猜测（160/500 题有多个变体）。

**category 映射**（`question_type` → 本项目 category；`_abs` 后缀一律 `abstention`）：

| LME question_type | category | 题数（含 abs） | 去 abs 后 |
|---|---:|---:|---:|
| temporal-reasoning | temporal | 133 | 127 |
| multi-session | multi-session | 133 | 121 |
| knowledge-update | knowledge-update | 78 | 72 |
| single-session-user | other | 70 | 64 |
| single-session-assistant | assistant | 56 | 56 |
| single-session-preference | preference | 30 | 30 |
| `*_abs`（任意类型） | abstention | 30 | — |

## 4. split 用法（`manifest.json.split_layers` 同款）

| split | 用途 | 每题规模 | 是否入库 |
|---|---|---|---|
| `oracle` | **读者级**：只含证据会话，测 reader 读证据 + 冲突/弃权处理 | 948 会话 / 10,960 轮（中位 23 轮/题，中位 2 条 gold） | ✅ |
| `s` | **证据级**：含干扰，测检索 R@k（约 115k tokens/题） | 23,854 会话 / 246,738 轮（中位 48 会话、491 轮/题） | ❌ 本地 |
| `m` | 证据级（大规模，约 500 会话/题） | 2.7GB 原始；未转换 | ❌ 未下载 |

**每个 split 都是 500 题、同一批 `question_id`**；gold 证据在三个 split 里完全一致
（`gold_evidence_fact_ids` 500/500 相同，`oracle` 的会话是 `s` 的子集 500/500 —— 已交叉校验）。

## 5. 重新生成

```powershell
# 下载（oracle+s；m 可选）
python research/bench/fetch_raw.py --files oracle s

# 转换：oracle → converted/longmemeval.jsonl；s → converted/local/（大文件，样例写到 converted/samples/）
python research/bench/convert_longmemeval.py --split oracle
python research/bench/convert_longmemeval.py --split s --out research/bench/converted/local/longmemeval_s.jsonl

# 自动校验（--dry-run 只跑确定性部分；命中缓存 0 调用）
python research/bench/check_bank.py --dry-run
python research/bench/check_bank.py --samples 30
```

## 6. 自动校验结果（摘要）

| 检查 | 结果 |
|---|---|
| 覆盖 | 500/500 转换成功，**0 丢弃**；结构校验 0 问题；21 题 abstention 用会话级回落（已标注） |
| 答案泄漏（n=30，去 gold 轮后能答对） | **12/30 = 40%**（95% CI 24.6%–57.7%）；答案词主要来自 **assistant 复述**（44 处 vs user 24 处） |
| 转换损坏（n=30，只给 gold 轮） | **6/30 = 20%**（95% CI 9.5%–37.3%）：2 道 multi-session 计数（gold 轮不足以推出答案）、4 道 preference（gold 答案是偏好元描述，通用判分偏严） |
| 时间戳语义 | `session_time`：948/948 星期与日历一致、0 条晚于 `question_date`、386/387 条文本显式日期 ≠ 会话日期 |
| 语言（n=10，中/英契约） | 中文契约 7/10、英文契约 6/10；一致 9/10（both correct 6、both wrong 3、zh-only 1），未观察到系统性语言效应 |

调用与成本：首轮 **160 次** deepseek-flash 调用、271,535 prompt + 42,595 completion = **314,130 tokens**；
重跑命中缓存 0 调用。逐题明细见 [`checks.json`](checks.json)，分析见 `docs/14`。
