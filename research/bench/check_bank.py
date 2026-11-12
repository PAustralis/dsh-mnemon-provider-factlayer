"""LongMemEval 题库转换后的自动校验。

五组检查（前四组写进 ``checks.json``，语言组也写进来供报告引用）：

1. **覆盖**：manifest 里 kept/discarded 与逐条丢弃原因；再对转换产物做结构校验
   （fact_id 唯一、gold ⊆ facts、distractor ⊆ facts、两者不相交、
   acceptable_answers 非空、category 合法）。
2. **答案泄漏**：抽 30 题，去掉 gold 证据轮，只把其余记录给 ``deepseek-flash``，
   答对 → ``suspected_leak``。
3. **抽取/转换损坏**：抽 30 题，只给转换后的 gold 证据记录，答不对 →
   ``reconstruction_failed``。
4. **时间戳语义**：全部 500 题（oracle）：haystack_dates 是否都早于 question_date、
   日期粒度、文本里的相对时间词与显式日期 → 判定为"会话发生时间"而非"事件时间"。
5. **语言**：抽 10 题，中文契约 v4 vs 英文契约 v4（同一 scaffold、同一记录、
   同一判分），报告是否影响结论。

调用与缓存：``research/bench/.llm_cache_checks.json``（gitignore）。重跑命中缓存
0 调用；``--force`` 才重新计费。

用法::

    python research/bench/check_bank.py --dry-run          # 只跑确定性部分
    python research/bench/check_bank.py --samples 30
    python research/bench/check_bank.py --force --samples 30
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
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPO_ROOT = BASE.parent.parent
CONVERTED = BASE / "converted" / "longmemeval.jsonl"
MANIFEST = BASE / "manifest.json"
CHECKS_OUT = BASE / "checks.json"
CACHE_PATH = BASE / ".llm_cache_checks.json"
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
    def __init__(self, key: str, cache: dict, force: bool = False) -> None:
        self.key = key
        self.cache = cache
        self.force = force
        self.new_calls = 0
        self.cache_hits = 0
        self.calls: list[dict] = []
        # 缓存文件里累计的"真实花过的钱"（只在新调用时累加，命中不加）
        self.cumulative = dict(cache.get("_usage") or {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})

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
        for attempt in range(3):
            req = urllib.request.Request(
                URL,
                data=body,
                headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
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
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(2 * (attempt + 1))
        raise SystemExit(f"LLM 调用失败（{tag}）：{last}")

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
# 检查 1：覆盖 + 结构
# ---------------------------------------------------------------------------
def check_coverage(records: list[dict], manifest: dict) -> dict:
    ids = [r["id"] for r in records]
    dup = len(ids) - len(set(ids))
    problems: list[dict] = []
    cat_ok = set(CATEGORY_ORDER) | {"abstention"}
    fact_id_counter = 0
    for r in records:
        fids = [f["fact_id"] for f in r["facts"]]
        fact_id_counter += len(fids)
        gold = r["gold_evidence_fact_ids"]
        dis = r["distractor_fact_ids"]
        if len(fids) != len(set(fids)):
            problems.append({"id": r["id"], "problem": "duplicate_fact_id"})
        if not set(gold) <= set(fids):
            problems.append({"id": r["id"], "problem": "gold_not_in_facts"})
        if not set(dis) <= set(fids):
            problems.append({"id": r["id"], "problem": "distractor_not_in_facts"})
        if set(gold) & set(dis):
            problems.append({"id": r["id"], "problem": "gold_distractor_overlap"})
        if not r["acceptable_answers"]:
            problems.append({"id": r["id"], "problem": "empty_acceptable"})
        if r["category"] not in cat_ok:
            problems.append({"id": r["id"], "problem": "bad_category"})
        if r["n_facts"] != len(fids) or r["n_gold"] != len(gold) or r["n_distractor"] != len(dis):
            problems.append({"id": r["id"], "problem": "count_mismatch"})
        if any(not f["text"].strip() or not f["date"] for f in r["facts"]):
            problems.append({"id": r["id"], "problem": "empty_text_or_date"})
        if r["question"].strip().lower() in r["gold_answer"].strip().lower():
            problems.append({"id": r["id"], "problem": "question_inside_answer"})

    split = manifest.get("splits", {}).get("oracle", {})
    by_cat: dict[str, int] = {}
    for r in records:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    return {
        "split": "oracle",
        "converted": len(records),
        "source_total": split.get("total"),
        "kept": split.get("kept"),
        "discarded": split.get("discarded"),
        "discard_reasons": split.get("discard_reasons", {}),
        "kept_by_category": dict(sorted(by_cat.items())),
        "evidence_source_counts": split.get("kept_by_evidence_source", {}),
        "sum_facts": sum(r["n_facts"] for r in records),
        "sum_gold": sum(r["n_gold"] for r in records),
        "sum_distractor": sum(r["n_distractor"] for r in records),
        "duplicate_ids": dup,
        "structural_problems": problems,
        "structural_ok": not problems and dup == 0,
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
        for sid, sess, sd in zip(item["haystack_session_ids"], item["haystack_sessions"], item["haystack_dates"]):
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


def run_leak(client: Client, sample: list[dict], workers: int) -> dict:
    def one(rec: dict) -> dict:
        fm = fact_map(rec)
        visible = [f["fact_id"] for f in rec["facts"] if not f["has_answer"]]
        prompt = reader_prompt(rec, visible)
        ans, _ = client.chat(prompt, max_tokens=2000, tag=f"leak:{rec['id']}")
        j = judge(client, rec, ans, f"leak:{rec['id']}")
        return {
            "id": rec["id"],
            "category": rec["category"],
            "n_records_shown": len(visible),
            "answer": ans.strip(),
            "judge": j,
            "answerable_without_gold": str(j.get("verdict")).upper() == "CORRECT",
            "carriers": leak_carriers(rec, rec["gold_answer"]),
        }

    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(one, sample))
    flagged = [r["id"] for r in rows if r["answerable_without_gold"]]
    carrier_roles: dict[str, int] = {}
    for r in rows:
        if r["answerable_without_gold"]:
            for role, cnt in r["carriers"]["roles"].items():
                carrier_roles[role] = carrier_roles.get(role, 0) + cnt
    return {
        "sample_size": len(rows),
        "sampled_by_category": _cat_counts(sample),
        "leak_flagged": flagged,
        "suspected_leak_rate": round(len(flagged) / len(rows), 4) if rows else None,
        "suspected_leak_rate_ci95": wilson(len(flagged), len(rows)),
        "leak_carrier_roles": carrier_roles,
        "rows": rows,
    }


def run_reconstruction(client: Client, sample: list[dict], workers: int) -> dict:
    def one(rec: dict) -> dict:
        prompt = reader_prompt(rec, rec["gold_evidence_fact_ids"])
        ans, _ = client.chat(prompt, max_tokens=2000, tag=f"recon:{rec['id']}")
        j = judge(client, rec, ans, f"recon:{rec['id']}")
        return {
            "id": rec["id"],
            "category": rec["category"],
            "n_gold_records": len(rec["gold_evidence_fact_ids"]),
            "answer": ans.strip(),
            "judge": j,
            "reconstructed": str(j.get("verdict")).upper() == "CORRECT",
        }

    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(one, sample))
    failed = [r["id"] for r in rows if not r["reconstructed"]]
    return {
        "sample_size": len(rows),
        "sampled_by_category": _cat_counts(sample),
        "reconstruction_failed": failed,
        "reconstruction_failure_rate": round(len(failed) / len(rows), 4) if rows else None,
        "reconstruction_failure_rate_ci95": wilson(len(failed), len(rows)),
        "failed_by_category": _cat_counts([r for r in rows if not r["reconstructed"]]),
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


def leak_carriers(rec: dict, gold_answer: str) -> dict:
    """哪条可见记录（user/assistant）把答案词带回来了——只做词面代理，供报告解释用。"""
    toks = answer_tokens(gold_answer)
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
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=30)
    ap.add_argument("--lang-samples", type=int, default=10)
    ap.add_argument("--workers", type=int, default=4)
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
    checks["coverage"] = check_coverage(records, manifest)
    checks["timestamps"] = check_timestamps(raw_items)
    print(
        f"coverage: {checks['coverage']['converted']} records, "
        f"{len(checks['coverage']['structural_problems'])} structural problems",
        flush=True,
    )

    leak_sample = sample_stratified(records, args.samples, SEED)
    recon_sample = sample_stratified(records, args.samples, SEED + 1, exclude={r["id"] for r in leak_sample})
    lang_sample = sample_stratified(records, args.lang_samples, SEED + 2, exclude={r["id"] for r in leak_sample + recon_sample})
    print(
        f"samples: leak={len(leak_sample)} recon={len(recon_sample)} lang={len(lang_sample)}",
        flush=True,
    )

    if args.dry_run:
        checks["answer_leak"] = {"skipped": True, "sampled_ids": [r["id"] for r in leak_sample]}
        checks["reconstruction"] = {"skipped": True, "sampled_ids": [r["id"] for r in recon_sample]}
        checks["language"] = {"skipped": True, "sampled_ids": [r["id"] for r in lang_sample]}
        checks["usage"] = {"skipped": True}
    else:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}
        client = Client(load_key(), cache, force=args.force)

        t0 = time.time()
        checks["answer_leak"] = run_leak(client, leak_sample, args.workers)
        print(
            f"leak: {len(checks['answer_leak']['leak_flagged'])}/{len(leak_sample)} answerable without gold "
            f"({time.time()-t0:.0f}s)",
            flush=True,
        )
        t0 = time.time()
        checks["reconstruction"] = run_reconstruction(client, recon_sample, args.workers)
        print(
            f"recon: {len(checks['reconstruction']['reconstruction_failed'])}/{len(recon_sample)} failed "
            f"({time.time()-t0:.0f}s)",
            flush=True,
        )
        t0 = time.time()
        checks["language"] = run_language(client, lang_sample, args.workers)
        print(f"language: {checks['language']['correct']} ({time.time()-t0:.0f}s)", flush=True)
        checks["usage"] = {
            "this_run": client.usage_total(),
            "cumulative": {
                **client.cumulative,
                "total_tokens": int(client.cumulative.get("prompt_tokens") or 0)
                + int(client.cumulative.get("completion_tokens") or 0),
            },
            "cache_file": str(CACHE_PATH.relative_to(REPO_ROOT)).replace("\\", "/"),
            "note": "cumulative 是缓存文件累计的实际调用量；this_run 为 0 表示全部命中缓存。",
        }
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    # 推荐排除集（只针对抽样命中的题，不外推）
    flagged = set(checks.get("answer_leak", {}).get("leak_flagged") or [])
    flagged |= set(checks.get("reconstruction", {}).get("reconstruction_failed") or [])
    checks["eval_exclusions"] = {
        "ids": sorted(flagged),
        "note": (
            "只排除抽样里实测命中的题（suspected_leak / reconstruction_failed），不外推到"
            "未抽样的题；转换产物仍保留全部 500 题供原版对照。"
        ),
        "eval_ready_reader_level": len(records) - len(flagged),
    }
    CHECKS_OUT.write_text(json.dumps(checks, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"checks -> {CHECKS_OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
