"""LongMemEval 题库转换后的自动校验（glm 审查后修订版）。

五组检查：

1. **覆盖 + 结构**：oracle 与 **s** 两个 split 都做结构校验（fact_id 唯一、
   gold ⊆ facts、distractor = facts − gold、gold ∩ distractor = ∅、计数自洽、
   text/date 非空、acceptable 非空、category 合法）。s 逐行流式读取，不整库进内存。
2. **答案泄漏（全量）**：对**每道非 abs 题**去掉 ``has_answer:true`` 的轮，
   只把其余记录交给 ``deepseek-flash`` 作答，再用同一判分口径对照 gold。
   答对 → ``leak_suspected``。温度 0，缓存到
   ``research/bench/.llm_cache_bench_leak.json``（gitignore）。
3. **抽取/转换损坏（抽样）**：只给转换后的 gold 证据记录，答不对 →
   ``reconstruction_failed``。
4. **时间戳语义（oracle 全量）**：haystack_dates 是否都早于 question_date、
   日期粒度、文本里的相对时间词与显式日期 → 判定为"会话发生时间"而非"事件时间"。
5. **语言（抽样）**：中文契约 v4 vs 英文契约 v4（同一 scaffold、同一记录、
   同一判分），报告是否影响结论。

同时产出：

- ``converted/longmemeval_clean.jsonl``：排除 ``leak_suspected``（以及筛查报错的题）的干净子集；
- ``manifest.json`` 的 ``clean_set`` / ``leak_screening`` 字段；
- ``checks.json``：逐题 ``leak_suspected``、逐题 carrier 命中数、总泄漏率与 Wilson 95% CI。

调用与缓存：``.llm_cache_checks.json`` 保存抽样检查，``.llm_cache_bench_leak.json``
保存本次全量泄漏筛查。重跑命中缓存 0 调用；``--force`` 才重新计费。

用法::

    python research/bench/check_bank.py --dry-run          # 只跑确定性部分（含 s 结构校验）
    python research/bench/check_bank.py --samples 30       # 全量泄漏 + 抽样重建/语言
    python research/bench/check_bank.py --force --samples 30
    python research/bench/check_bank.py --leak-limit 3     # 调试：只筛 3 题，不写干净集
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPO_ROOT = BASE.parent.parent
CONVERTED = BASE / "converted" / "longmemeval.jsonl"
CONVERTED_S = BASE / "converted" / "local" / "longmemeval_s.jsonl"
CLEAN_OUT = BASE / "converted" / "longmemeval_clean.jsonl"
MANIFEST = BASE / "manifest.json"
CHECKS_OUT = BASE / "checks.json"
CACHE_PATH = BASE / ".llm_cache_checks.json"
LEAK_CACHE_PATH = BASE / ".llm_cache_bench_leak.json"
CONTRACT_ZH = REPO_ROOT / "plugin" / "contracts" / "reader-contract-v4.zh.txt"
CONTRACT_EN = BASE / "contracts" / "reader-contract-v4.en.txt"

MODEL = "deepseek-flash"
URL = "https://api.deepseek.com/chat/completions"
CRED = Path(os.path.expanduser("~")) / ".dsh" / ".credentials.yaml"
SEED = 20261112

# 检查用 scaffold（英文）：数据是英文，这里不做语言混杂；语言检查单独控契约语言。
PREAMBLE_EN = (
    "You are an assistant. Below is the user's question, followed by several records "
    "retrieved from a memory store. The bracket before each record shows when it was "
    "recorded. Answer the user's question using only these records. Give the answer "
    "directly and do not call any tools. If the records do not contain the answer, "
    "say so explicitly."
)

READER_VERSION = "lme-reader-v1"
JUDGE_VERSION = "lme-judge-v1"
LANGS = ("zh", "en")

CATEGORY_ORDER = [
    "temporal",
    "multi-session",
    "knowledge-update",
    "preference",
    "assistant",
    "other",
]


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
class LLMError(RuntimeError):
    """单次 LLM 调用在重试后仍失败。"""


def load_key() -> str:
    raw = CRED.read_text(encoding="utf-8")
    m = re.search(r"(?m)^\s+DEEPSEEK_API_KEY:\s*(.+?)\s*$", raw)
    if not m:
        raise SystemExit("~/.dsh/.credentials.yaml 里没有 DEEPSEEK_API_KEY")
    return m.group(1).strip().strip('"').strip("'")


def _key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class Client:
    def __init__(
        self,
        key: str,
        cache: dict,
        force: bool = False,
        cache_path: Path | None = None,
    ) -> None:
        self.key = key
        self.cache = cache
        self.force = force
        self.cache_path = cache_path
        self._flush_lock = threading.Lock()
        self.new_calls = 0
        self.cache_hits = 0
        self.calls: list[dict] = []
        # 缓存文件里累计的"真实花过的钱"（只在新调用时累加，命中不加）
        self.cumulative = dict(cache.get("_usage") or {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})

    def flush(self) -> None:
        """把缓存落盘（加锁，避免工作线程与主线程同时替换临时文件）。"""
        if not self.cache_path:
            return
        with self._flush_lock:
            tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_path)

    def chat(self, prompt: str, *, max_tokens: int = 2000, tag: str = "") -> tuple[str, dict]:
        ck = _key(MODEL, READER_VERSION, tag, prompt)
        if not self.force and ck in self.cache:
            self.cache_hits += 1
            return self.cache[ck]["text"], self.cache[ck].get("usage", {})
        body = json.dumps(
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0,
            }
        ).encode("utf-8")
        last = ""
        for attempt in range(5):
            req = urllib.request.Request(
                URL,
                data=body,
                headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                text = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                self.cache[ck] = {"text": text, "usage": usage, "tag": tag}
                self.new_calls += 1
                self.calls.append({"tag": tag, "usage": usage})
                self.cumulative["calls"] = int(self.cumulative.get("calls") or 0) + 1
                self.cumulative["prompt_tokens"] = int(self.cumulative.get("prompt_tokens") or 0) + int(
                    usage.get("prompt_tokens") or 0
                )
                self.cumulative["completion_tokens"] = int(self.cumulative.get("completion_tokens") or 0) + int(
                    usage.get("completion_tokens") or 0
                )
                self.cache["_usage"] = self.cumulative
                return text, usage
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}: {exc.read()[:300]!r}"
                if exc.code in (400, 401, 403, 404):
                    break
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(min(30, 2 * (attempt + 1)))
        self.flush()
        raise LLMError(f"{tag or 'llm'} 调用失败：{last}")

    def usage_total(self) -> dict:
        p = sum(int(c["usage"].get("prompt_tokens") or 0) for c in self.calls)
        q = sum(int(c["usage"].get("completion_tokens") or 0) for c in self.calls)
        return {
            "new_calls": self.new_calls,
            "cache_hits": self.cache_hits,
            "prompt_tokens": p,
            "completion_tokens": q,
            "total_tokens": p + q,
        }


def merge_usage(*items: dict) -> dict:
    out = {"new_calls": 0, "cache_hits": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for item in items:
        for k in out:
            out[k] += int(item.get(k) or 0)
    return out


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------
def load_records() -> list[dict]:
    recs = []
    with CONVERTED.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


def fact_map(rec: dict) -> dict[str, dict]:
    return {f["fact_id"]: f for f in rec["facts"]}


def render_facts(rec: dict, fact_ids: list[str]) -> str:
    fm = fact_map(rec)
    rows = [fm[i] for i in fact_ids if i in fm]
    rows.sort(key=lambda f: (f["date"], f["session_id"], f["turn_index"]))
    return "\n".join(f"[{f['date']}] ({f['role']}) {f['text']}" for f in rows)


def sample_stratified(records: list[dict], n: int, seed: int, exclude: set[str] | None = None) -> list[dict]:
    """按 category 轮转抽样：题少的类也会被覆盖，结果可复现。"""
    exclude = exclude or set()
    pool: dict[str, list[dict]] = {c: [] for c in CATEGORY_ORDER}
    for r in records:
        if r["is_abstention"] or r["id"] in exclude:
            continue
        pool.setdefault(r["category"], []).append(r)
    rng = random.Random(seed)
    for cat in pool:
        rng.shuffle(pool[cat])
    picked: list[dict] = []
    while len(picked) < n and any(pool.values()):
        for cat in CATEGORY_ORDER:
            if pool.get(cat) and len(picked) < n:
                picked.append(pool[cat].pop())
    return picked


# ---------------------------------------------------------------------------
# 检查 1：覆盖 + 结构（oracle 与 s 都跑）
# ---------------------------------------------------------------------------
def structural_problems(rec: dict) -> list[str]:
    """单题结构校验；返回问题名列表（空列表 = 通过）。"""
    problems: list[str] = []
    fids = [f["fact_id"] for f in rec["facts"]]
    gold = rec["gold_evidence_fact_ids"]
    dis = rec["distractor_fact_ids"]
    fset, gset, dset = set(fids), set(gold), set(dis)

    if len(fids) != len(fset):
        problems.append("duplicate_fact_id")
    if len(dis) != len(dset):
        problems.append("duplicate_distractor_id")
    if not gset <= fset:
        problems.append("gold_not_in_facts")
    if not dset <= fset:
        problems.append("distractor_not_in_facts")
    if gset & dset:
        problems.append("gold_distractor_overlap")
    if dset != fset - gset:
        problems.append("distractor_not_complement")
    if not rec["acceptable_answers"]:
        problems.append("empty_acceptable")
    if rec["category"] not in set(CATEGORY_ORDER) | {"abstention"}:
        problems.append("bad_category")
    if rec["n_facts"] != len(fids) or rec["n_gold"] != len(gold) or rec["n_distractor"] != len(dis):
        problems.append("count_mismatch")
    if any(not f["text"].strip() or not f["date"] for f in rec["facts"]):
        problems.append("empty_text_or_date")
    if rec["question"].strip().lower() in rec["gold_answer"].strip().lower():
        problems.append("question_inside_answer")
    return problems


def _split_meta(manifest: dict, split: str) -> dict:
    return manifest.get("splits", {}).get(split, {})


def check_coverage(records: list[dict], manifest: dict, split: str = "oracle") -> dict:
    ids = [r["id"] for r in records]
    dup = len(ids) - len(set(ids))
    problems: list[dict] = []
    duplicate_fact_ids = 0
    for r in records:
        fids = [f["fact_id"] for f in r["facts"]]
        duplicate_fact_ids += len(fids) - len(set(fids))
        for p in structural_problems(r):
            problems.append({"id": r["id"], "problem": p})

    split_info = _split_meta(manifest, split)
    by_cat: dict[str, int] = {}
    for r in records:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    return {
        "split": split,
        "converted": len(records),
        "source_total": split_info.get("total"),
        "kept": split_info.get("kept"),
        "discarded": split_info.get("discarded"),
        "discard_reasons": split_info.get("discard_reasons", {}),
        "kept_by_category": dict(sorted(by_cat.items())),
        "evidence_source_counts": split_info.get("kept_by_evidence_source", {}),
        "sum_facts": sum(r["n_facts"] for r in records),
        "sum_gold": sum(r["n_gold"] for r in records),
        "sum_distractor": sum(r["n_distractor"] for r in records),
        "sum_sessions": split_info.get("sum_sessions"),
        "duplicate_ids": dup,
        "duplicate_fact_ids": duplicate_fact_ids,
        "structural_problem_count": len(problems),
        "structural_problems": problems,
        "structural_ok": not problems and dup == 0,
    }


def check_coverage_s(path: Path, manifest: dict) -> dict:
    """s split 结构校验：逐行流式读取（307MB，不整库进内存）。"""
    if not path.exists():
        return {
            "split": "s",
            "skipped": True,
            "reason": f"缺少 {path}（先跑 convert_longmemeval.py --split s）",
        }
    n = 0
    ids: set[str] = set()
    duplicate_ids = 0
    duplicate_fact_ids = 0
    problems: list[dict] = []
    sum_facts = sum_gold = sum_distractor = sum_sessions = 0
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for line in fh:
            h.update(line)
            if not line.strip():
                continue
            r = json.loads(line)
            n += 1
            if r["id"] in ids:
                duplicate_ids += 1
            ids.add(r["id"])
            fids = [f["fact_id"] for f in r["facts"]]
            duplicate_fact_ids += len(fids) - len(set(fids))
            for p in structural_problems(r):
                problems.append({"id": r["id"], "problem": p})
            sum_facts += r["n_facts"]
            sum_gold += r["n_gold"]
            sum_distractor += r["n_distractor"]
            sum_sessions += len({f["session_id"] for f in r["facts"]})

    split_info = _split_meta(manifest, "s")
    problem_count = len(problems)
    problems = problems[:200]
    return {
        "split": "s",
        "converted": n,
        "source_total": split_info.get("total"),
        "kept": split_info.get("kept"),
        "discarded": split_info.get("discarded"),
        "discard_reasons": split_info.get("discard_reasons", {}),
        "sum_facts": sum_facts,
        "sum_gold": sum_gold,
        "sum_distractor": sum_distractor,
        "sum_sessions": sum_sessions,
        "converted_sha256": h.hexdigest(),
        "manifest_sum_facts_matches": split_info.get("sum_facts") == sum_facts,
        "manifest_sum_sessions_matches": split_info.get("sum_sessions") == sum_sessions,
        "duplicate_ids": duplicate_ids,
        "duplicate_fact_ids": duplicate_fact_ids,
        "structural_problem_count": problem_count,
        "structural_problems": problems,
        "structural_ok": problem_count == 0 and duplicate_ids == 0 and duplicate_fact_ids == 0,
    }


# ---------------------------------------------------------------------------
# 检查 4：时间戳语义
# ---------------------------------------------------------------------------
REL_TIME = re.compile(
    r"\b(yesterday|today|tomorrow|last (?:night|week|month|year|weekend)|"
    r"this (?:morning|afternoon|evening|week|month|year)|"
    r"(?:a|one|two|three|couple of) (?:days?|weeks?|months?|years?) ago|"
    r"next (?:week|month|year)|the other day|recently)\b",
    re.I,
)
EXPLICIT_DATE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2}(?:st|nd|rd|th)?\b|\b\d{4}-\d{2}-\d{2}\b",
    re.I,
)
MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
}


def explicit_date_matches_session(text: str, month: int, day: int) -> bool:
    """文本里的显式日历日期（'March 15th' / '2023-03-15'）是否就是会话当天。"""
    for em in EXPLICIT_DATE.finditer(text):
        raw = em.group(0)
        m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})", raw)
        if m and m.group(1).lower() in MONTHS:
            if MONTHS[m.group(1).lower()] == month and int(m.group(2)) == day:
                return True
        m2 = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
        if m2 and int(m2.group(2)) == month and int(m2.group(3)) == day:
            return True
    return False


DATE_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2}) \(([A-Za-z]{3})\) (\d{2}):(\d{2})$")


def check_timestamps(raw_items: list[dict]) -> dict:
    future = 0
    rel_hits = 0
    explicit_hits = 0
    explicit_same = 0
    explicit_differs = 0
    n_sessions = 0
    nonzero_minute = 0
    weekday_ok = 0
    qdate_relation: dict[str, int] = {"before_last_session": 0, "after_last_session": 0, "same_day": 0}
    for item in raw_items:
        qd = item["question_date"]
        m = DATE_RE.match(qd)
        qdate = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        last = None
        for _sid, sess, sd in zip(item["haystack_session_ids"], item["haystack_sessions"], item["haystack_dates"]):
            mm = DATE_RE.match(sd)
            d = f"{mm.group(1)}-{mm.group(2)}-{mm.group(3)}"
            n_sessions += 1
            if mm.group(6) != "00":
                nonzero_minute += 1
            try:
                weekday_ok += int(
                    datetime.date(int(mm.group(1)), int(mm.group(2)), int(mm.group(3))).strftime("%a")
                    == mm.group(4)
                )
            except ValueError:
                pass
            last = max(last or d, d)
            if d > qdate:
                future += 1
            for turn in sess:
                text = turn.get("content") or ""
                if REL_TIME.search(text):
                    rel_hits += 1
                for em in EXPLICIT_DATE.finditer(text):
                    explicit_hits += 1
                    if explicit_date_matches_session(em.group(0), int(mm.group(2)), int(mm.group(3))):
                        explicit_same += 1
                    else:
                        explicit_differs += 1
        if last:
            if last < qdate:
                qdate_relation["before_last_session"] += 1
            elif last == qdate:
                qdate_relation["same_day"] += 1
            else:
                qdate_relation["after_last_session"] += 1
    n_sessions = sum(len(x["haystack_sessions"]) for x in raw_items)
    return {
        "n_questions": len(raw_items),
        "n_session_timestamps": n_sessions,
        "timestamp_format": "YYYY/MM/DD (Ddd) HH:MM（全部带时分）",
        "sessions_with_nonzero_minute": nonzero_minute,
        "weekday_matches_calendar": f"{weekday_ok}/{n_sessions}",
        "sessions_after_question_date": future,
        "question_date_vs_last_session": qdate_relation,
        "turns_with_relative_time_phrase": rel_hits,
        "turns_with_explicit_calendar_date": explicit_hits,
        "explicit_date_same_as_session_date": explicit_same,
        "explicit_date_differs_from_session_date": explicit_differs,
        "semantics": "session_time",
        "conclusion": (
            "haystack_dates 是**会话发生时间**（精确到时分、星期与日历一致、全部不晚于 "
            "question_date），不是文本里提到的**事件时间**：文本用 yesterday / last week / "
            "March 15th 这类相对或显式日期指事件，说明事实层把 date 当 event_time 代理时"
            "会有 0–N 天偏移；本转换保留 date_kind=session_time 不做推断。"
        ),
    }


# ---------------------------------------------------------------------------
# 检查 2 / 3：泄漏 / 重建
# ---------------------------------------------------------------------------
JUDGE_PROMPT = """You grade a question-answering attempt against a gold answer.

Question: {question}
Gold answer: {gold}
Also acceptable: {acceptable}

Candidate answer:
{answer}

Decide whether the candidate answer is CORRECT. It is correct if it conveys the key
fact(s) of the gold answer (paraphrase and extra detail are fine; for numeric answers a
different unit with the same value counts; for counting questions the number must match).
If the candidate says the information is missing/unavailable, or gives a wrong value,
it is INCORRECT.

Reply with JSON only:
{{"verdict": "CORRECT" or "INCORRECT", "reason": "one short sentence"}}"""


def judge(client: Client, rec: dict, answer: str, tag: str) -> dict:
    prompt = JUDGE_PROMPT.format(
        question=rec["question"],
        gold=rec["gold_answer"],
        acceptable="; ".join(rec["acceptable_answers"]),
        answer=answer.strip()[:4000],
    )
    text, _ = client.chat(prompt, max_tokens=1200, tag=f"judge:{tag}")
    return parse_json(text)


def parse_json(text: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*", "", text.strip())
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                pass
    return {"verdict": "UNCLEAR", "reason": "judge output unparsable"}


def reader_prompt(rec: dict, fact_ids: list[str]) -> str:
    return (
        PREAMBLE_EN
        + "\n\nQuestion:\n"
        + rec["question"]
        + "\n\nRecords:\n"
        + render_facts(rec, fact_ids)
    )


def run_leak_full(
    client: Client,
    records: list[dict],
    workers: int,
    limit: int = 0,
    progress_every: int = 20,
) -> dict:
    """全量泄漏筛查：对每道非 abs 题，去掉 has_answer 轮后让模型作答并判分。"""
    targets_all = [r for r in records if not r["is_abstention"]]
    targets = targets_all[:limit] if limit else targets_all
    n_targets = len(targets)
    print(f"leak: screening {n_targets} non-abstention questions", flush=True)

    def one(rec: dict) -> dict:
        visible = [f["fact_id"] for f in rec["facts"] if not f["has_answer"]]
        row = {
            "id": rec["id"],
            "source_item_id": rec["source_item_id"],
            "category": rec["category"],
            "n_records_shown": len(visible),
            "n_gold_records_removed": len(rec["facts"]) - len(visible),
            "leak_suspected": None,
            "judge_verdict": None,
            "n_carrier_records": 0,
            "carrier_roles": {},
        }
        try:
            prompt = reader_prompt(rec, visible)
            ans, _ = client.chat(prompt, max_tokens=2000, tag=f"leak:{rec['id']}")
            j = judge(client, rec, ans, f"leak:{rec['id']}")
        except LLMError as exc:
            row["error"] = str(exc)
            return row
        verdict = str(j.get("verdict")).upper()
        carriers = leak_carriers(rec)
        row.update(
            answer=ans.strip(),
            answer_excerpt=ans.strip()[:300],
            judge=j,
            judge_verdict=verdict,
            leak_suspected=verdict == "CORRECT",
            answerable_without_gold=verdict == "CORRECT",
            n_carrier_records=len(carriers["visible_matches"]),
            carrier_roles=carriers["roles"],
        )
        return row

    rows: list[dict] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(one, r): r for r in targets}
        for i, fut in enumerate(as_completed(futures), start=1):
            rows.append(fut.result())
            if i % progress_every == 0 or i == n_targets:
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed else 0
                eta = (n_targets - i) / rate if rate else 0
                print(
                    f"  {i}/{n_targets} screened ({elapsed:.0f}s, ~{eta:.0f}s left)",
                    flush=True,
                )
                client.flush()
    client.flush()

    rows.sort(key=lambda r: r["id"])
    ok = [r for r in rows if r["leak_suspected"] is not None]
    flagged = [r for r in ok if r["leak_suspected"]]
    errors = [r for r in rows if r["leak_suspected"] is None]

    screened_by_cat = _cat_counts(ok)
    flagged_by_cat = _cat_counts(flagged)
    rate_by_cat = {}
    for cat in sorted(screened_by_cat):
        n = screened_by_cat[cat]
        k = flagged_by_cat.get(cat, 0)
        rate_by_cat[cat] = {
            "screened": n,
            "leak_suspected": k,
            "rate": round(k / n, 4) if n else None,
            "ci95": wilson(k, n),
        }

    carrier_roles: dict[str, int] = {}
    for r in flagged:
        for role, cnt in r["carrier_roles"].items():
            carrier_roles[role] = carrier_roles.get(role, 0) + cnt

    return {
        "mode": "full",
        "method": (
            "对每道非 abs 题：去掉 has_answer:true 的轮，其余记录交 deepseek-flash 作答；"
            "再用同一 judge prompt（deepseek-flash、温度 0）与 gold_answer 比对，"
            "判 CORRECT 即 leak_suspected。"
        ),
        "scope": "oracle split（500 题共享同一批 question_id / gold；s 的额外会话是干扰）",
        "total_questions": len(records),
        "total_non_abstention": len(targets_all),
        "screened": len(ok),
        "attempted": len(rows),
        "skipped_abstention": len(records) - len(targets_all),
        "partial": bool(limit),
        "leak_flagged": sorted(r["id"] for r in flagged),
        "leak_suspected_count": len(flagged),
        "leak_by_question": {r["id"]: r["leak_suspected"] for r in rows},
        "suspected_leak_rate": round(len(flagged) / len(ok), 4) if ok else None,
        "suspected_leak_rate_ci95": wilson(len(flagged), len(ok)),
        "hits_by_category": {
            "screened": screened_by_cat,
            "leak_suspected": flagged_by_cat,
            "rate": rate_by_cat,
        },
        "leak_carrier_roles": carrier_roles,
        "errors": [
            {"id": r["id"], "error": r.get("error", "")} for r in errors
        ],
        "rows": rows,
    }


def run_reconstruction(client: Client, sample: list[dict], workers: int) -> dict:
    def one(rec: dict) -> dict:
        row = {
            "id": rec["id"],
            "category": rec["category"],
            "n_gold_records": len(rec["gold_evidence_fact_ids"]),
            "reconstructed": None,
        }
        try:
            prompt = reader_prompt(rec, rec["gold_evidence_fact_ids"])
            ans, _ = client.chat(prompt, max_tokens=2000, tag=f"recon:{rec['id']}")
            j = judge(client, rec, ans, f"recon:{rec['id']}")
        except LLMError as exc:
            row["error"] = str(exc)
            return row
        row.update(
            answer=ans.strip(),
            judge=j,
            reconstructed=str(j.get("verdict")).upper() == "CORRECT",
        )
        return row

    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(one, sample))
    failed = [r["id"] for r in rows if r["reconstructed"] is False]
    errors = [r["id"] for r in rows if r["reconstructed"] is None]
    return {
        "sample_size": len(rows),
        "sampled_by_category": _cat_counts(sample),
        "reconstruction_failed": failed,
        "errors": errors,
        "reconstruction_failure_rate": round(len(failed) / len(rows), 4) if rows else None,
        "reconstruction_failure_rate_ci95": wilson(len(failed), len(rows)),
        "failed_by_category": _cat_counts([r for r in rows if r["reconstructed"] is False]),
        "rows": rows,
    }


def wilson(k: int, n: int) -> list[float]:
    """Wilson 95% 置信区间（小样本比例不夸大精度）。"""
    if n == 0:
        return [0.0, 0.0]
    z = 1.96
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


STOP = set(
    "the a an and or of to in on for with at by from as is are was were be been being it its this that "
    "these those my me i you your we our they their he she his her not no do does did have has had "
    "would could should will can may might must about into over under more most less least than then "
    "there here what which who whom how when where why".split()
)


def answer_tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9][a-z0-9'\-:.]{3,}", text.lower())
        if t not in STOP and not t.isdigit()
    }


def leak_carriers(rec: dict) -> dict:
    """哪条可见记录（user/assistant）把答案词带回来了——只做词面代理，供报告解释用。"""
    toks = answer_tokens(rec["gold_answer"])
    if not toks:
        return {"visible_matches": [], "roles": {}}
    hits = []
    for f in rec["facts"]:
        if f["has_answer"]:
            continue
        overlap = toks & answer_tokens(f["text"])
        if overlap:
            hits.append({"fact_id": f["fact_id"], "role": f["role"], "tokens": sorted(overlap)[:6]})
    roles: dict[str, int] = {}
    for h in hits:
        roles[h["role"]] = roles.get(h["role"], 0) + 1
    return {"visible_matches": hits, "roles": roles}


def _cat_counts(rows: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        out[r["category"]] = out.get(r["category"], 0) + 1
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# 检查 5：语言（中文契约 vs 英文契约）
# ---------------------------------------------------------------------------
def load_contract(lang: str) -> str:
    path = CONTRACT_ZH if lang == "zh" else CONTRACT_EN
    return path.read_text(encoding="utf-8").strip()


def run_language(client: Client, sample: list[dict], workers: int) -> dict:
    contract = {lang: load_contract(lang) for lang in LANGS}
    rows = []

    def one(rec: dict) -> dict:
        fact_ids = [f["fact_id"] for f in rec["facts"]]
        base = "\n\nQuestion:\n" + rec["question"] + "\n\nRecords:\n" + render_facts(rec, fact_ids)
        out = {"id": rec["id"], "category": rec["category"], "groups": {}}
        for lang in LANGS:
            prompt = PREAMBLE_EN + "\n\n" + contract[lang] + base
            ans, _ = client.chat(prompt, max_tokens=2000, tag=f"lang:{lang}:{rec['id']}")
            j = judge(client, rec, ans, f"lang:{lang}:{rec['id']}")
            out["groups"][lang] = {
                "answer": ans.strip(),
                "judge": j,
                "correct": str(j.get("verdict")).upper() == "CORRECT",
            }
        return out

    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(one, sample))
    zh = sum(1 for r in rows if r["groups"]["zh"]["correct"])
    en = sum(1 for r in rows if r["groups"]["en"]["correct"])
    both = sum(1 for r in rows if r["groups"]["zh"]["correct"] and r["groups"]["en"]["correct"])
    neither = sum(1 for r in rows if not r["groups"]["zh"]["correct"] and not r["groups"]["en"]["correct"])
    zh_only = sum(1 for r in rows if r["groups"]["zh"]["correct"] and not r["groups"]["en"]["correct"])
    en_only = sum(1 for r in rows if r["groups"]["en"]["correct"] and not r["groups"]["zh"]["correct"])
    return {
        "sample_size": len(rows),
        "sampled_by_category": _cat_counts(sample),
        "contract_zh_file": str(CONTRACT_ZH.relative_to(REPO_ROOT)).replace("\\", "/"),
        "contract_en_file": str(CONTRACT_EN.relative_to(REPO_ROOT)).replace("\\", "/"),
        "correct": {"zh_contract": zh, "en_contract": en},
        "agreement": {
            "both_correct": both,
            "both_wrong": neither,
            "zh_only_correct": zh_only,
            "en_only_correct": en_only,
        },
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# 干净子集 + manifest
# ---------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_clean_set(records: list[dict], excluded: dict[str, str], path: Path) -> dict:
    """按 excluded={id: reason} 写出干净子集；sha256 与题数写回 manifest。"""
    kept = [r for r in records if r["id"] not in excluded]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    # 与 convert_longmemeval.py 一致：数据文件固定 LF，保证 sha256 可跨平台复算。
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for r in kept:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)
    reasons: dict[str, int] = {}
    for reason in excluded.values():
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "path": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "count": len(kept),
        "sha256": sha256_file(path),
        "source": str(CONVERTED.relative_to(REPO_ROOT)).replace("\\", "/"),
        "source_count": len(records),
        "excluded_count": len(excluded),
        "excluded_by_reason": dict(sorted(reasons.items())),
        "excluded_ids": sorted(excluded),
        "note": "排除 leak_suspected；若筛查有报错，也保守排除（screening_error）。",
    }


def update_manifest(clean_set: dict, leak: dict, usage: dict, eval_exclusions: dict) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    leak_cum = usage.get("cumulative_by_cache", {}).get("leak", {})
    manifest["clean_set"] = clean_set
    manifest["leak_screening"] = {
        "scope": leak["scope"],
        "method": leak["method"],
        "total_questions": leak["total_questions"],
        "screened": leak["screened"],
        "skipped_abstention": leak["skipped_abstention"],
        "clean_questions": clean_set["count"],
        "suspected_leak": leak["leak_suspected_count"],
        "suspected_leak_rate": leak["suspected_leak_rate"],
        "suspected_leak_rate_ci95": leak["suspected_leak_rate_ci95"],
        "cache_path": str(LEAK_CACHE_PATH.relative_to(REPO_ROOT)).replace("\\", "/"),
        "calls": leak_cum.get("calls"),
        "prompt_tokens": leak_cum.get("prompt_tokens"),
        "completion_tokens": leak_cum.get("completion_tokens"),
        "tokens": leak_cum.get("total_tokens"),
        "new_calls_this_run": usage.get("leak_screening", {}).get("new_calls"),
        "cache_hits_this_run": usage.get("leak_screening", {}).get("cache_hits"),
        "errors": len(leak["errors"]),
        "partial": leak.get("partial", False),
    }
    manifest["eval_exclusions"] = eval_exclusions
    tmp = MANIFEST.with_suffix(MANIFEST.suffix + ".part")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(MANIFEST)


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=30, help="重建/语言抽样题数（泄漏检查始终全量）")
    ap.add_argument("--lang-samples", type=int, default=10)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--leak-limit", type=int, default=0, help="调试：只筛前 N 题（0=全量，不写干净集）")
    ap.add_argument("--dry-run", action="store_true", help="只跑确定性检查（不调 LLM）")
    ap.add_argument("--force", action="store_true", help="忽略 LLM 缓存")
    args = ap.parse_args()

    records = load_records()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    raw_items = json.loads((BASE / "raw" / "longmemeval" / "longmemeval_oracle.json").read_text(encoding="utf-8"))

    checks: dict = {
        "generated_by": "research/bench/check_bank.py",
        "model": MODEL,
        "seed": SEED,
        "converted_file": str(CONVERTED.relative_to(REPO_ROOT)).replace("\\", "/"),
        "converted_sha256": hashlib.sha256(CONVERTED.read_bytes()).hexdigest(),
    }
    checks["coverage"] = check_coverage(records, manifest, "oracle")
    checks["coverage_s"] = check_coverage_s(CONVERTED_S, manifest)
    checks["timestamps"] = check_timestamps(raw_items)
    print(
        f"coverage(oracle): {checks['coverage']['converted']} records, "
        f"{checks['coverage']['structural_problem_count']} structural problems",
        flush=True,
    )
    print(
        f"coverage(s): {checks['coverage_s'].get('converted', 0)} records, "
        f"{checks['coverage_s'].get('structural_problem_count', 0)} structural problems, "
        f"ok={checks['coverage_s'].get('structural_ok')}",
        flush=True,
    )

    # 重建/语言的抽样口径与首轮保持一致：首轮的 30 题泄漏样本虽已被全量筛查取代，
    # 但仍作为抽样排除集，保证 5.2/5.4 的题号可复现、结果只随泄漏口径变化。
    legacy_sample = sample_stratified(records, args.samples, SEED)
    recon_sample = sample_stratified(records, args.samples, SEED + 1, exclude={r["id"] for r in legacy_sample})
    lang_sample = sample_stratified(
        records,
        args.lang_samples,
        SEED + 2,
        exclude={r["id"] for r in legacy_sample + recon_sample},
    )
    print(
        f"samples: recon={len(recon_sample)} lang={len(lang_sample)}; leak=full non-abstention",
        flush=True,
    )

    clean_set: dict | None = None
    if args.dry_run:
        checks["answer_leak"] = {"skipped": True, "mode": "full", "note": "dry-run 不调 LLM"}
        checks["reconstruction"] = {"skipped": True, "sampled_ids": [r["id"] for r in recon_sample]}
        checks["language"] = {"skipped": True, "sampled_ids": [r["id"] for r in lang_sample]}
        checks["usage"] = {"skipped": True}
        checks["clean_set"] = {"skipped": True, "note": "dry-run 不生成干净集"}
    else:
        key = load_key()
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}
        leak_cache = (
            json.loads(LEAK_CACHE_PATH.read_text(encoding="utf-8")) if LEAK_CACHE_PATH.exists() else {}
        )
        other = Client(key, cache, force=args.force, cache_path=CACHE_PATH)
        leaker = Client(key, leak_cache, force=args.force, cache_path=LEAK_CACHE_PATH)

        t0 = time.time()
        checks["answer_leak"] = run_leak_full(leaker, records, args.workers, limit=args.leak_limit)
        print(
            f"leak: {checks['answer_leak']['leak_suspected_count']}/{checks['answer_leak']['screened']} "
            f"suspected ({checks['answer_leak']['suspected_leak_rate']}; "
            f"CI {checks['answer_leak']['suspected_leak_rate_ci95']}) "
            f"({time.time()-t0:.0f}s)",
            flush=True,
        )
        t0 = time.time()
        checks["reconstruction"] = run_reconstruction(other, recon_sample, args.workers)
        print(
            f"recon: {len(checks['reconstruction']['reconstruction_failed'])}/{len(recon_sample)} failed "
            f"({time.time()-t0:.0f}s)",
            flush=True,
        )
        t0 = time.time()
        checks["language"] = run_language(other, lang_sample, args.workers)
        print(f"language: {checks['language']['correct']} ({time.time()-t0:.0f}s)", flush=True)
        other.flush()
        leaker.flush()

        checks["usage"] = {
            "this_run": merge_usage(leaker.usage_total(), other.usage_total()),
            "leak_screening": leaker.usage_total(),
            "reconstruction_and_language": other.usage_total(),
            "cache_files": {
                "leak": str(LEAK_CACHE_PATH.relative_to(REPO_ROOT)).replace("\\", "/"),
                "other": str(CACHE_PATH.relative_to(REPO_ROOT)).replace("\\", "/"),
            },
            "cumulative_by_cache": {
                "leak": {
                    **leaker.cumulative,
                    "total_tokens": int(leaker.cumulative.get("prompt_tokens") or 0)
                    + int(leaker.cumulative.get("completion_tokens") or 0),
                },
                "other": {
                    **other.cumulative,
                    "total_tokens": int(other.cumulative.get("prompt_tokens") or 0)
                    + int(other.cumulative.get("completion_tokens") or 0),
                },
            },
            "note": (
                "this_run 为本次真实新调用；cache_hits 为命中缓存。"
                "cumulative_by_cache 是各自缓存文件累计（other 含首轮 160 次抽样调用）。"
            ),
        }

        flagged = set(checks["answer_leak"]["leak_flagged"])
        errored = {r["id"] for r in checks["answer_leak"]["errors"]}
        excluded = {rid: "leak_suspected" for rid in flagged}
        excluded.update({rid: "screening_error" for rid in errored})
        if args.leak_limit:
            checks["clean_set"] = {
                "skipped": True,
                "note": f"--leak-limit={args.leak_limit} 为调试模式，不写干净集",
            }
            print("clean set: skipped (--leak-limit)", flush=True)
        else:
            clean_set = write_clean_set(records, excluded, CLEAN_OUT)
            checks["clean_set"] = clean_set
            print(
                f"clean set: {clean_set['count']}/{clean_set['source_count']} "
                f"-> {clean_set['path']} sha={clean_set['sha256'][:12]}",
                flush=True,
            )

    # 严格排除集：全量泄漏命中 + 抽样重建失败（后者只覆盖抽样，不外推）
    flagged = set(checks.get("answer_leak", {}).get("leak_flagged") or [])
    flagged |= set(checks.get("reconstruction", {}).get("reconstruction_failed") or [])
    checks["eval_exclusions"] = {
        "ids": sorted(flagged),
        "note": (
            "泄漏为全量命中；reconstruction_failed 只来自抽样，未外推到未抽样的题。"
            "clean_set 只排除 leak_suspected（如筛查报错也保守排除）。"
        ),
        "eval_ready_reader_level": len(records) - len(flagged),
    }

    CHECKS_OUT.write_text(json.dumps(checks, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"checks -> {CHECKS_OUT}", flush=True)

    if clean_set is not None:
        update_manifest(clean_set, checks["answer_leak"], checks["usage"], checks["eval_exclusions"])
        print(f"manifest <- clean_set / leak_screening ({MANIFEST})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
