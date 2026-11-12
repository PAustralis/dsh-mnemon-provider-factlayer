# 14 · LongMemEval 题库转换报告（第 2 步）

> 时间：2026-11-12。范围：**只做数据下载、转换、自动校验**；没有改 `app/`、没有改 profile、没有跑评测。
>
> 产物：[`research/bench/`](../research/bench/)（`README.md`、`raw/`、`converted/longmemeval.jsonl`、
> `converted/local/longmemeval_s.jsonl`、`manifest.json`、`checks.json`、两个脚本）。
> 上游调研：[`docs/13`](13-契约交付与基准整合可行性.md) §2.2；评测口径：[`docs/10`](10-证据级标注与读者指标.md)
> （证据级 R@k + 读者级端到端）。
>
> 一句话（**修订前初版**，全量结果见 §10）：**500 题全部转换成功、0 丢弃；读者级（oracle）与证据级（s）各 500 题、gold 证据逐轮且两档完全一致。
> 抽样诊断发现两类真实问题：40% 的题在去掉 gold 轮后仍能答对（assistant 复述把答案带回来），
> 20% 的题只给 gold 轮答不对（multi-session 计数题 gold 不全 + preference 题的 gold 是偏好元描述）。
> 因此 500 题结构可用，但建议严格口径排除抽样命中的 18 题（482 题）。**
>
> **2026-11-12 glm 审查后的修订（以 §10 为准）**：s split 的 13 题重复会话已去重
> （重复 `fact_id` 143 条 → 0；s 事实 246,738 → 246,595，gold 1,364 不变），结构校验已覆盖 s（0 问题）；
> 泄漏检查从 30 题抽样扩到 **470 道非 abs 题全量**；干净子集与 manifest 字段见 §10。
> 下面 §1–§9 保留**修订前**的读数，凡与 §10 冲突的数字，以 §10 为准。

---

## 1. 规模与产物

| 项 | 值 |
|---|---|
| 数据集 | LongMemEval (cleaned)，`xiaowu0162/longmemeval-cleaned` @ `98d7416c24c778c2fee6e6f3006e7a073259d48f` |
| 许可 | MIT（dataset card）；论文 ICLR 2025 / arXiv:2410.10813 |
| 题数 | **500**（三份语料共用同一批 `question_id`） |
| 原始文件 | `oracle` 15.4MB（已下载）、`s` 277.4MB（已下载）、`m` 2.74GB（**未下载**，见 §7.6） |
| 转换产物 | `converted/longmemeval.jsonl` 16.5MB（oracle，500 题，入库）；`converted/local/longmemeval_s.jsonl` 307.3MB（s，500 题，gitignore + 样本入库）；`converted/longmemeval_clean.jsonl` 11.3MB（337 题，入库） |
| 事实数 | oracle 10,960 轮（948 会话）；s 246,595 轮（23,854 个唯一会话；修订前 246,738，见 §10.1） |
| gold 证据 | 1,364 个轮级 fact（896 个 `has_answer` 轮 + 468 个 abstention 会话级回落轮），**三个 split 完全一致** |
| 校验调用 | 抽样首轮 160 次 / 314,130 tokens；**全量泄漏 940 次 / 3,135,199 tokens**（§10.5），重跑 0 调用 |

字段与转换口径见 [`research/bench/README.md`](../research/bench/README.md) §3；`manifest.json` 逐题记录
`status`（kept/discarded）、`discard_reason`、`n_facts/n_gold/n_distractor`、`evidence_source`、`seq`。

**category 分布**（`_abs` 一律 abstention）：

| category | 题数 | 说明 |
|---|---:|---|
| temporal | 127 | 时间推理（含"多少次/多少天"） |
| multi-session | 121 | 跨会话聚合/计数 |
| knowledge-update | 72 | 知识更新（旧值 → 新值） |
| other | 64 | `single-session-user` |
| assistant | 56 | `single-session-assistant` |
| preference | 30 | 偏好类（gold 是偏好元描述） |
| abstention | 30 | `_abs` 后缀，答案"信息不足" |
| **合计** | **500** | |

---

## 2. 字段设计（与 docs/10 两档口径对齐）

每题一条 JSONL 记录；完整字段样例见 README §3。要点：

1. **`facts[]` 是对话轮**：`{fact_id, text, date, date_kind, date_raw, session_id, turn_index, role, has_answer}`，
   `text` 只放原文，不改写、不注入答案。`fact_id = "{session_id}#t{turn}"`，在题内唯一。
2. **`gold_evidence_fact_ids[]` 是轮级定位**，来自 `has_answer:true`；不落任何会话级 id。
   校验：896/896 个 `has_answer` 轮都落在 `answer_session_ids` 内；`answer_session_ids` 100% 能在
   `haystack_session_ids` 里找到（这正是"证据轮次定位失败"的两个来源，本次均为 0）。
3. **`distractor_fact_ids[]`** = 其余全部轮（本题语料内）。因此 oracle 的"干扰"是证据会话里的其它轮，
   s 的干扰还包括其余 ~46 个会话。
4. **`split`** 标明语料规模；**`evidence_source`** 区分 `turn:has_answer` 与 `session_fallback`。
5. **`acceptable_answers[]`** 只做机械派生（原样/去引号/去句末句号/拆 "X. Y is also acceptable"/取括号备选）；
   160/500 题有多个变体。不做语义扩展，宁可让判分少认一点。
6. **`question_date`** 保留原始提问时刻（题型可做 as-of 过滤：948 个会话时间**全部**不晚于 `question_date`）。

---

## 3. split 用法与写入位置

| split | 服务哪一层 | 每题规模（本次实测） | 入库 |
|---|---|---|---|
| `oracle` | **读者级**：只给证据会话，测 reader 读证据 + 冲突/弃权/计数 | 948 会话、10,960 轮；中位 23 轮/题、2 条 gold/题 | `converted/longmemeval.jsonl`（16.5MB） |
| `s` | **证据级**：含干扰，测检索 R@k | 23,854 个唯一会话、246,595 轮；中位 48 会话/491 轮/题 | `converted/local/longmemeval_s.jsonl`（307.3MB，gitignore）+ 前 20 条样本 |
| `m` | 证据级（大规模，~500 会话/题） | 原始 2.74GB；**未下载/未转换** | 无（README 给命令） |

`manifest.json.split_layers` 与 README 表一致。三档之间已交叉校验：

- `gold_evidence_fact_ids` 在 oracle 与 s 中 **500/500 完全相同**；
- oracle 的会话集合是 s 的子集 **500/500**；
- 两档 facts 数 10,960 vs 246,595（s 多出的全是干扰会话；修订前 s 为 246,738，去重减少 143 条）。

---

## 4. 丢弃统计（覆盖检查）

**500/500 转换成功，0 丢弃。** 逐条丢弃原因（缺字段/证据轮次定位失败/答案为空/时间戳缺失/空题干）
在 oracle 与 s 上都是 0；`checks.json.coverage.discard_reasons = {}`。

唯一需要显式说明的"非标准处理"：

| 情形 | 题数 | 处理 | 影响 |
|---|---:|---|---|
| `_abs` 题里没有任何 `has_answer:true` 轮 | **21**（30 道 abs 中的 21 道） | 用 `answer_session_ids` 的**全部轮**做 gold 证据，标 `evidence_source=session_fallback` | gold 数偏大（这 21 题贡献 468 条），证据级 R@k 的分母会偏保守；不当作转换失败 |
| `_abs` 题里有 `has_answer:true` 轮 | 9 | 正常轮级定位 | — |

另外两个"0 但值得记录"的定位失败检查：`has_answer` 轮落在 `answer_session_ids` 之外 = 0；
`answer_session_ids` 不在 `haystack_session_ids` 中 = 0。

结构校验（`checks.json.coverage.*` 与 `checks.json.coverage_s.*`）覆盖 oracle **和 s**：id 唯一、
fact_id 唯一、gold ⊆ facts、distractor ⊆ facts、distractor = facts − gold、gold ∩ distractor = ∅、
acceptable 非空、category 合法、计数自洽、text/date 非空 —— **两档均 0 问题**
（s 为逐行流式校验；修订前只跑 oracle，见 §10.2）。

---

## 5. 三类自动检查结果

采样：按 category 轮转、固定种子（`20261112`），abstention 不入泄漏/重建样本（其"正确答案"是弃权，
通用判分不适用），样本互斥。题数与题号都写进 `checks.json`，可复算。

### 5.1 答案泄漏（初版抽样 n=30；**已被 §10.3 全量筛查取代**）：12/30 = 40%（95% CI 24.6%–57.7%）

做法：去掉 `has_answer:true` 的轮，只把**其余记录**（同会话的其它轮）给 deepseek-flash，
问原题，用它自己的答案对照 gold（deepseek-flash 判分）。能答对 → `suspected_leak`。

- 命中 12 题，分布：knowledge-update 4、temporal 3、preference 2、other 2、assistant 1、multi-session 0。
- 词面代理（`checks.json.answer_leak.leak_carrier_roles`）：答案词在可见记录里出现 **assistant 44 处 / user 24 处**，
  即**主要泄漏通道是 assistant 的复述**。典型例子 `lme-oracle-gpt4_2655b836`：
  gold 轮只说"3 月 15 日首次保养"，但同会话的 assistant 轮写了 "the dealership was able to fix your **GPS** issue"，
  模型据此答对。
- **口径上限**：泄漏检查把证据会话的**全部**剩余轮都给了模型（约 34 轮/42K 字符）；产品读者只拿 top-4
  （4–8K 字符），实际暴露面更小。但对**证据级**评测这条是真实的：复述轮和 gold 轮在同一会话，
  检索会把它们一起带进 top-k，因此"gold 未命中但答对"会拉高读者正确率、低估证据召回。
- 结论：`has_answer` 是**轮级、非穷尽**的标签（只标"需要读的那一轮"，不标"相邻复述"）。
  gold 集合是"最小可读集"而非"答案唯一来源"。

### 5.2 抽取/转换损坏（n=30）：**6/30 = 20%**（95% CI 9.5%–37.3%）

做法：只把转换后的 `gold_evidence_fact_ids` 记录给 deepseek-flash，问原题，答不对 → `reconstruction_failed`。

| 题 | 类 | 失败原因（已逐题核对） |
|---|---|---|
| `46a3abf7` | multi-session | 计数 3 个鱼缸，但 gold 两轮只提到 20 加仑 + 5 加仑，"朋友孩子那个缸"不在 gold 轮里 → **gold 不全** |
| `37f165cf` | multi-session | 两本小说 416+440=856 页，但 gold 轮没写"1 月/3 月"这两个时间锚 → **gold 不全** |
| `afdc33df` / `06f04340` / `fca70973` | preference | gold 答案是"用户会偏好 X 类回答"的元描述，模型只看到偏好陈述、被"只用这些记录回答"约束，回答"记录里没有建议" → **题型/判分口径错配** |
| `1c0ddc50` | preference | 模型给出与 gold 偏好相反的建议（真实答错） |

读法：**20% 里只有 2 题是真正的 gold 缺失**（multi-session 计数），4 题是 preference 题型与通用判分错配
（官方 LongMemEval 对 preference 用专门的 judge prompt，我们没有照搬）。因此不建议把 4 道 preference
当作"转换损坏"丢弃，而是换判分口径重判；multi-session 计数题的 gold 需要补标或排除。

### 5.3 时间戳语义：**会话发生时间（session_time），不是事件时间**

| 证据 | 数值 |
|---|---|
| 时间戳格式 | `YYYY/MM/DD (Ddd) HH:MM`，全部带时分 |
| 星期与日历一致 | 948/948 |
| 带非零分钟 | 934/948（即精确到分钟，不是"某天"的粗粒度） |
| 晚于 `question_date` 的会话 | **0** |
| `question_date` 与最后一条会话 | 同日 124 题、晚于最后会话 376 题、早于 0 |
| 文本里的相对时间词（yesterday/last week/…） | 904 轮 |
| 文本里的显式日历日期 | 387 处，其中 **386 处 ≠ 会话日期**（1 处恰好同日） |

结论：`haystack_dates` 是**这段对话发生在哪一天**；对话里说的"yesterday / last week / March 15th"
才是事件时间，且通常早于会话日期。事实层若把 `date` 当 `event_time` 用，会有 0–N 天（有时数周）的系统性偏移。
本次转换**不做推断**：`date` 用工整 ISO 日期，`date_kind="session_time"` 显式标记，`date_raw` 保留原始时间戳，
事件时间只能从文本里解析（后续如需 event_time，应加一层抽取 + 校验，不能直接改字段语义）。

### 5.4 语言：中/英契约对照（n=10）

做法：同一 scaffold（英文 preamble + 英文记录）、同一 10 题（按类轮转）、同一判分；**唯一差别是契约文本**
（中文 v4 原文 vs 本次自译的英文 v4，`research/bench/contracts/reader-contract-v4.en.txt`）。

| 组 | 正确 | 说明 |
|---|---:|---|
| 中文契约 v4 | 7/10 | 英文数据 + 中文契约（产品真实形态） |
| 英文契约 v4 | 6/10 | — |
| 一致 | 9/10 | both correct 6、both wrong 3、zh-only 1、en-only 0 |

唯一的 discordant 是 `af8d2e46`（"几件 T 恤"）：中文契约组按 2023-05-27 记录答 7 件并指出"记录没说 5 天"，
英文组拒绝回答。这是判分边界，不是系统性语言退化。

**结论（n=10，置信区间很宽）**：没有观察到"中文契约 + 英文记忆"损害结论的证据；但 10 题只能发现大效应，
不足以证明无差异。建议后续用 40–100 题做配对检验（同一题两遍，McNemar），并把英文契约的**权威性**问题
定下来：本次英文版是我们自己的翻译，不是官方版本；它只用于这项对照，不随包交付、不进 DSH profile。

---

## 6. 与 docs/10 口径的对应

| docs/10 | 本转换 |
|---|---|
| 证据级：gold = "回答本题要用的证据"，逐记录 | `gold_evidence_fact_ids`（轮级，来自 `has_answer`） |
| 证据级：R@k = 命中证据对 / 证据总数 | 用 `gold_evidence_fact_ids` 直接算，E 类（abstention）按会话回落证据计入或单列 |
| 读者级：top-k 记录 → reader → 三档判分 | `oracle` 的 facts 可直接渲染成 `[date] text` 记录；reader 档建议按 `question_date` 做 as-of 过滤 |
| "整场景 gold ≠ 最小证据" | 本转换的 gold 是轮级最小集；但 §5.1 证明它**非穷尽**，读者级会因复述轮受益 |
| 跨场景污染 | LongMemEval 每题独立语料，**不存在** 40 题库那种跨题污染；代价是不能拼成一个大库 |

---

## 7. 已知偏差（随任何数字一起读）

1. **gold 非穷尽**：全量筛查（§10.3）显示 **163/470 = 34.68%** 的非 abs 题在去掉 gold 轮后仍可答对，
   主要靠 assistant 复述。证据级 R@k 应视为下界；读者级正确率会高于"仅证据可答"的水平。
2. **preference 题型错配**：30 题的 gold 是"偏好元描述"（"用户会偏好…"），通用判分会把"给建议"判错。
   正式评测应改用官方 preference judge，或把这 30 题单列。
3. **multi-session 计数 gold 不全**：抽样 2/30 命中（3 个鱼缸、856 页），说明计数题的 `has_answer` 只标了部分轮。
4. **abstention 的会话级回落**：21/30 题没有轮级标签，用整个 `answer_session_ids` 会话做证据；
   这 21 题的 gold 偏大（468 轮），证据级分母偏保守。
5. **时间戳是会话时间**：不是事件时间（§5.3）。任何"事件距今/先后"的推断都要先解析文本。
6. **`m` 未下载/未转换**：2.74GB，转换产物约 3GB，超出本步需要且无法入库；权威 sha256 已从 HF LFS oid 记录，
   转换命令在 README。证据级只用 `s` 一档。
7. **语言**：数据英文、契约中文；英文契约是自译，非官方；n=10 只够排除大效应。
8. **category 是我们自己的命名**（`single-session-user → other`），不是 LME 官方类别；映射表在 README。
9. **`acceptable_answers` 机械派生**：只覆盖答案串里写了两遍的情形；"三岁 vs 3 岁"这类不在答案串里的写法会漏。
10. **判分是单模型单次**：deepseek-flash、温度 0、无 3 票/人工复核；泄漏首版 40%、全量 34.68%、
    重建 20% 都是此 prompt 下的读数。
11. **其它格式**：LME 的 `question_date` 含星期与时分，我们保留原串；比较时不要直接字符串比大小。

---

## 8. 原版与改版的对照方式（供后续 glm 审阅）

**一句话：同一批 500 个 `question_id`，只换"喂给读者的上下文装配方式"与"判分主体"，把差异拆成
契约 / 检索 / 判分三个可归因的口径，绝对分数不可跨协议直接比，只比同协议内的配对差。**

### 8.1 字段映射（原版 → 改版）

| 原版字段 | 改版字段 | 处理 |
|---|---|---|
| `question_id` | `source_item_id` / `id`（加 split 前缀） | 原样保留，可回接官方结果 |
| `question` | `question` | 原样 |
| `answer` | `gold_answer` + `acceptable_answers` | 原样 + 机械变体 |
| `question_type` | `source_question_type` + `category` | 保留原始类型，另给类别 |
| `haystack_sessions[i][j]` | `facts[]`（+ `session_id`/`turn_index`/`role`） | 逐轮展开，text 不改写 |
| `haystack_session_ids[i]` / `haystack_dates[i]` | `session_id` / `date`+`date_raw` | 原样绑定到该会话每一轮 |
| `has_answer`（轮级） | `gold_evidence_fact_ids` | **使用原标签**，不重新标注 |
| `answer_session_ids` | 仅作落点校验；abstention 21 题作回落证据 | 不作为 gold 主体 |
| `question_date` | `question_date` | 原样（as-of 过滤用） |
| — | `split` | oracle / s / m，标明语料规模 |
| — | `distractor_fact_ids` | 其余全部轮 |

### 8.2 三组可比实验（建议顺序）

| 组 | 上下文 | 读者 | 判分 | 回答什么 |
|---|---|---|---|---|
| **A 原版复刻** | 官方 haystack 全量（每会话按原顺序、日期前缀） | deepseek-flash + 英文中性 preamble（不加契约） | deepseek-flash 二分类（或照搬官方 per-type judge prompt） | 作为本仓库内的"原版参照"；与官方论文数字**不可直接比**（读者/judge 都不同） |
| **B 读者级（改版）** | oracle 的 facts（可按 `question_date` as-of 过滤）→ top-4 或全量 | deepseek-flash + 契约 v4（中文/英文各一版） | 同 A | 契约与读者行为的差；与 A 配对可归因到"契约" |
| **C 证据级（改版）** | s 的 246,595 条 facts → 我们的检索 top-k | 同 B（读者与判分固定） | 同 A | 检索 R@k 与端到端差；与 B 配对可归因到"检索" |

控制变量：A/B/C 的题集、判分 prompt、温度、max_tokens 必须一致；B 与 C 只有上下文不同；A 与 B 只有契约有无不同。
建议同时报"干净口径"（§10.4 的 `converted/longmemeval_clean.jsonl`，排除全量泄漏命中的题）与"全量口径"
（500 题），并在两个口径下给出 A/B/C 的配对差（逐题 change：变好/变坏/不变）。

### 8.3 判分口径

- 原版官方脚本用 GPT-4o + 按题型定制的 judge prompt；本仓库没有 GPT-4o 预算，A 组用 deepseek-flash
  复刻，**只作同仓库内对照**，不要拿它去对论文数字。
- preference 30 题必须用"是否满足偏好"的判分（不是"是否复述 gold 元描述"），否则会系统性低估。
- abstention 30 题：明确说"记录里没有/无法确定"= 正确；给出具体值 = 错误（可另记"编造"）。
- multi-session 计数：判分只认数字；但注意 §7.3 的 gold 不全，建议对这 121 题单独复核 gold。

### 8.4 给 glm 的审阅清单

1. `manifest.json` 的 500 条 `records` 是否每条都能回接 `source_item_id`；`discard_reasons` 为空的断言是否接受。
2. 21 题 `session_fallback` 的处理是否接受（还是应把这些题排除出证据级分母）。
3. 泄漏已做全量（§10.3）：按"干净集排除 `leak_suspected`"还是"全量保留 + 证据级只报下界"。
4. preference 30 题的判分口径（官方 judge vs 通用 judge），以及是否单列。
5. 时间戳口径：接受 `date_kind=session_time`，还是必须先抽 event_time 再进事实层。
6. 英文契约是否作为"对照用临时资产"（不入包、不进 profile）。
7. `m` 不下载/不转换是否可接受（step 3 需要时一条命令生成）。
8. 语言 n=10 的结论强度是否够，是否要扩到 40–100 题配对。

---

## 9. 复现与成本

```powershell
cd E:\dsh-mnemon-provider-graphiti
python research/bench/fetch_raw.py --files oracle s
python research/bench/convert_longmemeval.py --split oracle
python research/bench/convert_longmemeval.py --split s --out research/bench/converted/local/longmemeval_s.jsonl
python research/bench/check_bank.py --samples 30     # 首次全量筛查 940 次；重跑命中缓存 0 调用；--force 才重新计费
```

- 校验调用（修订后）：抽样首轮 **160 次** / 314,130 tokens；全量泄漏 **940 次**
  （934 次新调用 + 6 次冒烟缓存）/ 2,851,565 prompt + 283,634 completion = **3,135,199 tokens**，
  缓存 `.llm_cache_bench_leak.json`（gitignore）。重跑全部命中缓存 0 调用。
- 转换不调用任何模型；下载只走 HF（直连优先，镜像兜底）。
- 产物哈希（LF 口径，`.gitattributes` 固定 `-text`）：`converted/longmemeval.jsonl` sha256
  `325d2534…`（16,501,956 bytes，与 HEAD 一致）；s 版 `11a4b952…`（307,273,235 bytes，去重后）；
  干净集 `54c80a06…`（337 题，11,299,069 bytes）；原始文件见 `raw/longmemeval/SHA256SUMS`。

---

## 10. glm 审查后的修订（2026-11-12）

> 本节是修订后的权威读数。§1–§9 中 `246,738`、`40%`、`482 题`、`18 题` 等均为修订前历史值。

### 10.1 重复 fact_id：13 题、实测 143 条

**现象**：s 的 `haystack_session_ids` 在 13 题里把同一个会话列了两次。转换器按
`{session_id}#t{idx}` 生成 `fact_id`，该会话被展开两次，题内主键重复；两次展开还会让
`distractor_fact_ids` 出现同 id 的两份，破坏"事实主键唯一"的读前提。

**数量校正**：审查结论写的是"12 条重复"；实测是 **143 条**（13 题 × 各 1 个重复会话，
重复 id 数 = 该会话的非空轮数，2–50 轮不等）。逐题：

| question_id | 重复 session_id | 去掉轮数 | s facts 前→后 | gold |
|---|---|---:|---:|---:|
| `58bf7951` | `07b7a667_1` | 12 | 616→604 | 1 |
| `1e043500` | `d5d1f9c4` | 12 | 510→498 | 1 |
| `001be529` | `sharegpt_SYbLHTK_0` | 6 | 514→508 | 1 |
| `d23cf73b` | `334ab2f1` | 12 | 512→500 | 4 |
| `caf03d32` | `sharegpt_YCfhENU_0` | 4 | 493→489 | 2 |
| `91b15a6e` | `sharegpt_fgnzLtE_0` | 2 | 489→487 | 2 |
| `078150f1` | `sharegpt_z7Ogy29_0` | 50 | 551→501 | 3 |
| `gpt4_4929293b` | `sharegpt_81riySf_0` | 2 | 469→467 | 1 |
| `gpt4_76048e76` | `8fcaf3a9_2` | 12 | 482→470 | 2 |
| `gpt4_c27434e8_abs` | `8c64ce26_3` | 12 | 482→470 | 1 |
| `18bc8abd` | `sharegpt_vyHqfrX_0` | 4 | 436→432 | 2 |
| `c7dc5443` | `6b8e8bb3_1` | 12 | 478→466 | 2 |
| `1d4da289` | `sharegpt_qktLFWS_21` | 3 | 506→503 | 1 |

**修法**：`convert_longmemeval.py` 在展开前按**首次出现顺序**对 `haystack_session_ids` 去重，
`sessions`/`dates` 同步对齐；manifest 逐题记录 `duplicate_sessions_removed`、
`duplicate_session_ids`、`duplicate_session_turns_dropped`，split 汇总记录
`duplicate_sessions_removed=13`、`questions_with_duplicate_sessions=13`。

**gold 不受影响**：13 个重复会话全部不是 `answer_session_ids`（0 个 answer session），
且重复副本内容逐字节相同（`duplicate_session_content_mismatches=0`）；去重后 gold 仍为
**1,364** 轮，逐题 `gold_evidence_fact_ids` 不变。

| 指标 | 修订前 | 修订后 |
|---|---:|---:|
| s 事实（轮） | 246,738 | **246,595**（−143） |
| s 唯一会话 | 23,854 | **23,854**（原始数组 23,867 条，13 条重复；唯一会话数不变） |
| 重复 `fact_id` | 143（13 题） | **0** |
| s 结构问题 | 未校验（只跑 oracle） | **0** |
| gold facts（oracle/s） | 1,364 | **1,364** |
| oracle facts / 唯一会话 | 10,960 / 948 | **10,960 / 948**（不变） |
| 产物 sha256 | 旧文档口径（CRLF）：oracle `b27714dd…`、s `51f0f71b…` | LF：oracle `325d2534…`、s `11a4b952…` |

> 行尾说明：`.gitattributes` 已声明数据文件 `-text`（固定字节），但旧转换器用 Windows 默认换行
> 写 CRLF，导致文档哈希与 HEAD 的 LF 文件不一致，也会让每次重跑产生 500 行换行 diff。
> 本次显式写 LF 后，oracle 哈希与 HEAD 一致；s 的哈希变化同时包含"去重"和"CRLF→LF"两个因素。

### 10.2 s 结构校验

`check_bank.py` 现在对 s 逐行流式校验（不整库进内存），检查项与 oracle 相同：
id 唯一、fact_id 唯一、gold ⊆ facts、distractor ⊆ facts、**`distractor = facts − gold`**、
gold ∩ distractor = ∅、acceptable 非空、category 合法、计数自洽、text/date 非空。

结果：`checks.json.coverage_s` 为 **500 records、246,595 facts、1,364 gold、
`duplicate_fact_ids=0`、`structural_problem_count=0`、`structural_ok=true`**；
`manifest_sum_facts_matches=true`、`manifest_sum_sessions_matches=true`。oracle 同时保持 0 问题。

### 10.3 全量泄漏筛查：163/470 = 34.68%

**做法**：对 470 道非 abs 题（30 道 abs 跳过：正确答案是"信息不足"，通用判分不适用），
去掉 `has_answer:true` 的轮，只把**其余记录**交给 `deepseek-flash` 作答；再用同一 judge prompt
（`deepseek-flash`、温度 0）与 `gold_answer`（含 `acceptable_answers`）比对，判 CORRECT 记
`leak_suspected`。逐题答案与判分缓存 `.llm_cache_bench_leak.json`（gitignore），**0 报错**。

| 口径 | 命中 | 总题数 | 泄漏率 | 95% CI |
|---|---:|---:|---:|---|
| 初版抽样（§5.1，保留为历史） | 12 | 30 | 40.0% | 24.6%–57.7% |
| **全量（本节）** | **163** | **470** | **34.68%** | **30.52%–39.09%** |

| category | 筛查 | 命中 | 泄漏率 | 95% CI |
|---|---:|---:|---:|---|
| knowledge-update | 72 | 39 | 54.17% | 42.74%–65.17% |
| other | 64 | 32 | 50.00% | 38.10%–61.90% |
| preference | 30 | 13 | 43.33% | 27.38%–60.80% |
| multi-session | 121 | 35 | 28.93% | 21.59%–37.55% |
| assistant | 56 | 16 | 28.57% | 18.42%–41.48% |
| temporal | 127 | 28 | 22.05% | 15.72%–30.02% |

- 全量 34.68% **低于**初版抽样的 40%，但初版 95% CI 很宽（24.6%–57.7%）；全量把 CI 收窄到
  30.52%–39.09%，两者并不矛盾，说明 40% 是抽样偏高。
- 类别差异大：knowledge-update（54.2%）与 other（50.0%）最高，temporal（22.1%）最低；
  与 §5.1 的"assistant 复述把答案带回来"一致，词面代理显示 assistant 528 处 vs user 262 处。
- 结论仍然是实质性的：**超过 1/3 的非 abs 题在去掉 gold 轮后仍能答对**；`has_answer` 是轮级、
  非穷尽的"最小可读集"标签。
- 口径边界：筛查在 **oracle** 语料上做（与初版一致，500 题共享同一 gold）；s 的额外约 46 个
  干扰会话不在本题读者上下文中，因此不应把 34.68% 直接当作 s 检索 top-k 后的泄漏率。

### 10.4 干净子集

```jsonc
// research/bench/converted/longmemeval_clean.jsonl
// 500 - 163 = 337 题；排除原因只有 leak_suspected（163）
// 30 道 abstention 保留（未做泄漏筛查），所以 337 里含 30 abs + 307 非 abs
```

- 题数 **337/500**；sha256 `54c80a06dc460ce411d010fc54fdbab0a2af2931b7c3d97621ab6bf45db3b25f`；
  11,299,069 bytes。
- `manifest.json.clean_set`：`{path,count,sha256,source,excluded_count,excluded_by_reason,excluded_ids}`。
- `manifest.json.leak_screening`：`{total_questions:500, screened:470, skipped_abstention:30,
  clean_questions:337, suspected_leak:163, rate:0.3468, ci95:[0.3052,0.3909], method, cache_path,
  calls:940, tokens:3135199, errors:0}`。
- 严格排除集 `checks.json.eval_exclusions.ids` 为 **166** 题（163 个全量泄漏 + 6 个抽样重建失败
  − 3 个重叠），reader-level 334 题；它比 clean set 多 3 题（抽样重建失败），但只覆盖抽样。

### 10.5 成本与复现

- 全量筛查：**940 次调用**（934 次新调用 + 6 次命中 3 题冒烟缓存），
  2,851,565 prompt + 283,634 completion = **3,135,199 tokens**。
- 抽样检查：首轮 160 次 / 314,130 tokens；本次另有 90 次抽样口径调试 / 155,605 tokens。
- 账户余额实测 **26.20 → 21.93 CNY（−4.27）**；21:24 之后本地 usage 账本记录的其它 DSH 模型调用
  约 **0.32 CNY**，故筛查脚本（含冒烟/口径调试）约 **3.9 CNY**。余额是账户级观测、含同机并发调用，
  只作量级参考。
- 复现：`python research/bench/check_bank.py --samples 30`（重跑全部命中缓存 **0 调用**）；
  `--force` 才重新计费；`--dry-run` 只跑 oracle+s 结构校验（0 调用）。

### 10.6 对结论的影响

- 严格口径从"抽样命中 18 题"改为"全量命中 163 题"；干净集 **337 题**（含 30 abs），
  非 abs 干净题 **307 题**。
- 原 §8.2 的 A/B/C 实验建议同时报两套口径：**全量 500 题**（与官方题集可比）和
  **干净 337 题**（严格读数）；干净口径的预期调用约 1,348 次 LLM + 337 次 embedding。
- s 去重只减少干扰轮，不改变 gold 与检索评价分母；oracle 的读者级题集完全不变。

---

**修订后可用于评测的题数：500 题（结构完整、0 丢弃；oracle 与 s 结构校验 0 问题）。
泄漏全量筛查命中 163/470 非 abs 题（34.68%，95% CI 30.52%–39.09%），干净集 337/500
（`converted/longmemeval_clean.jsonl`，含 30 道未筛查的 abs）。s 去重后 246,595 轮（−143），
gold 1,364 不变。预计评测调用次数：读者级 500 reader + 500 judge = 1,000 次、证据级 500 次
embedding + 500 reader + 500 judge = 1,000 次 LLM + 500 次 embedding，两档合计约 2,000 次 LLM +
500 次 embedding；干净口径（337 题）约 1,348 次 LLM + 337 次 embedding，不含重试/多 seed。**
