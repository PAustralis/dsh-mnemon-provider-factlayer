#!/usr/bin/env python
"""契约 v4 外部效度实验：LongMemEval 干净集（337 题）× 3 组 × 2 角色。

三组**同一批题**，只有 reader prompt 不同：

* **A** 基线：不给契约。
* **E** 中文契约 v4：逐字读取 ``plugin/contracts/reader-contract-v4.zh.txt``（不在本文件里手抄）。
* **F** 英文契约 v4：逐字读取 ``research/bench/contract_v4_en.txt``（v4 的忠实英文翻译，
  用来检验"中文契约在英文题上是否失效"）。

reader：给该题**全部** facts（逐条带日期），温度 0，``deepseek-flash``。
max_tokens 任务书给的 1024 实测不可用（deepseek-flash 默认 thinking 的 reasoning_tokens
计入 max_tokens，长 prompt 下会把 1024 吃成空回答），故 reader=4096、judge=2048，
thinking 保持默认；详见 README §7 与 ``docs/16`` §1.3。
judge：question + gold_answer + acceptable_answers + reader 回答，三档 ✅/⚠️/❌，
三组共用同一判分 prompt，温度 0。

缓存：``research/bench/.llm_cache_ext.json``（gitignore 覆盖 ``.llm_cache_*``），
键 = ``group|qid|role``；值里另存 prompt 的 sha256，命中但 prompt 变了算 miss（不静默复用）。
复跑 **0 调用**。

用法::

    # 5 题冒烟（三组全覆盖，30 次调用）
    python research/bench/contract_ext_eval.py --smoke

    # 全量（337×3×2 = 2022 次；复跑 0 调用）
    python research/bench/contract_ext_eval.py

    # 只看已落盘结果，不发任何请求
    python research/bench/contract_ext_eval.py --analyze-only

产物：``research/bench/contract_ext_eval.json``（逐题逐组回答/判分/用量 + 统计）。
报告：``docs/16-契约外部效度.md``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPO_ROOT = BASE.parent.parent
CLEAN = BASE / "converted" / "longmemeval_clean.jsonl"
CACHE_PATH = BASE / ".llm_cache_ext.json"
OUT_PATH = BASE / "contract_ext_eval.json"
CONTRACT_ZH = REPO_ROOT / "plugin" / "contracts" / "reader-contract-v4.zh.txt"
CONTRACT_EN = BASE / "contract_v4_en.txt"

GROUPS = ("A", "E", "F")
GROUP_LABEL = {
    "A": "基线（不给契约）",
    "E": "中文契约 v4",
    "F": "英文契约 v4",
}
CONTRACT_GROUP = {"E": "zh", "F": "en"}

MODEL = "deepseek-flash"
URL = "https://api.deepseek.com/chat/completions"
CRED = Path(os.path.expanduser("~")) / ".dsh" / ".credentials.yaml"

READER_MAX_TOKENS = 4096
JUDGE_MAX_TOKENS = 2048
TEMPERATURE = 0
# 任务书给的是 reader max_tokens=1024。实测（5 题冒烟，deepseek-flash 默认 thinking）：
# thinking 的 reasoning_tokens 也计入 max_tokens，长 prompt 下 3/15 条 reader 回答被 thinking
# 吃满 1024 → 空回答（E 2/5、F 1/5、A 0/5）；判官 800 也被吃满 3 条。
# 空回答是"预算效应"，不是契约效应，会把 E/F 系统性判死。故 reader 提到 4096、judge 提到 2048
# （最长题 72 facts / 22.7k prompt 实测 reasoning 仅 579–827，远小于 4096），thinking 保持默认
# ——与 docs/11、docs/15 的 reader/判官口径一致。见 docs/16 §1.3。
READER_THINKING = "default"
JUDGE_THINKING = "default"
READER_VERSION = "lme-ext-reader-v1"
JUDGE_VERSION = "lme-ext-judge-v1"
SCAFFOLD_VERSION = "lme-ext-scaffold-v3"

SEED = 20261112
BOOTSTRAP_B = 10000
VERDICTS = ("✅", "⚠️", "❌")
CATEGORY_ORDER = [
    "temporal",
    "multi-session",
    "knowledge-update",
    "preference",
    "assistant",
    "other",
    "abstention",
]
FOCUS_CATEGORIES = ("knowledge-update", "temporal", "multi-session")

# 官方 USD 价目（docs/15 / pilot_cost.json 同款；deepseek-flash = DeepSeek-V4.1-Flash-1017）
RATES = {
    "off_peak": {"input_hit": 0.003, "input_miss": 0.15, "output": 0.6},
    "peak": {"input_hit": 0.006, "input_miss": 0.3, "output": 1.2},
}
RATES_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing（2026-11-12 取；off-peak = peak 的一半）"

# 与 research/bench/check_bank.py 完全同一个 scaffold：三组只差契约文本这一块。
PREAMBLE_EN = (
    "You are an assistant. Below is the user's question, followed by several records "
    "retrieved from a memory store. The bracket before each record shows when it was "
    "recorded. Answer the user's question using only these records. Give the answer "
    "directly and do not call any tools. If the records do not contain the answer, "
    "say so explicitly."
)

# 三组共用同一个判分 prompt（唯一变量是 reader 的回答）。
JUDGE_PROMPT = """You grade one answer to a memory question against a gold answer.

Question: {question}
Gold answer: {gold}
Also acceptable: {acceptable}

Candidate answer:
{answer}

Use exactly three levels:

✅ CORRECT — conveys the key fact(s) of the gold answer. Paraphrase and extra detail are
fine; for a numeric answer a different unit with the same value counts; for a counting
question the number must match. Special case: if the gold answer says the information is
not enough / the question cannot be answered from the records, then ✅ means the candidate
clearly says the information is missing or cannot be determined, without inventing an
answer.

⚠️ PARTIAL — partially right, or materially hedged: gives only part of the gold answer, or
offers the gold answer alongside a conflicting alternative, or flags that the records
conflict / are insufficient while still not reaching the gold answer, or is too vague to
tell what it concludes. If the candidate recommends a wrong answer but explicitly says the
records conflict or are uncertain, use ⚠️ rather than ❌.

❌ INCORRECT — answers with a wrong value, name, date or number, or contradicts the gold
answer, or (when the gold answer says the information is not enough) confidently invents
an answer or asserts a fact the records do not support.

Reply with JSON only:
{{"verdict": "✅" or "⚠️" or "❌",
 "answer_state": "answered" or "abstained" or "conflict_flagged",
 "reason": "one short sentence"}}"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT)).replace("\\", "/")


def now_utc() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_key() -> str:
    raw = CRED.read_text(encoding="utf-8")
    m = re.search(r"(?m)^\s*DEEPSEEK_API_KEY:\s*(.+?)\s*$", raw)
    if not m:
        raise SystemExit("~/.dsh/.credentials.yaml 里没有 DEEPSEEK_API_KEY")
    return m.group(1).strip().strip('"').strip("'")


class LLMError(RuntimeError):
    """单次 LLM 调用重试后仍失败。"""


# ---------------------------------------------------------------------------
# LLM 客户端（缓存键 = group|qid|role；prompt 哈希校验）
# ---------------------------------------------------------------------------
class Client:
    def __init__(self, key: str, cache: dict, cache_path: Path, force: bool = False) -> None:
        self.key = key
        self.cache = cache
        self.cache_path = cache_path
        self.force = force
        self._flush_lock = threading.Lock()
        self.new_calls = 0
        self.cache_hits = 0
        self.prompt_mismatch = 0
        self.retries = 0
        self.failures: list[dict] = []
        self.calls: list[dict] = []
        self.cumulative = dict(
            cache.get("_usage") or {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        )

    def flush(self) -> None:
        with self._flush_lock:
            tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_path)

    def chat(self, prompt: str, *, max_tokens: int, tag: str, cache_key: str,
             thinking: str = "default") -> dict:
        sig = sha256_bytes(
            f"{MODEL}\x00{SCAFFOLD_VERSION}\x00{max_tokens}\x00{TEMPERATURE}\x00{thinking}"
            f"\x00{prompt}".encode("utf-8")
        )
        hit = self.cache.get(cache_key)
        if not self.force and isinstance(hit, dict) and hit.get("prompt_sha256") == sig:
            self.cache_hits += 1
            return {
                "text": hit["text"],
                "usage": hit.get("usage") or {},
                "finish_reason": hit.get("finish_reason"),
                "cache_key": cache_key,
                "cached": True,
            }
        if isinstance(hit, dict) and hit.get("prompt_sha256") != sig:
            # 同一 (group, qid, role) 但 prompt/参数变了：旧结果不能复用，记一笔。
            self.prompt_mismatch += 1

        body_dict = {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": TEMPERATURE,
        }
        if thinking == "disabled":
            body_dict["thinking"] = {"type": "disabled"}
        body = json.dumps(body_dict).encode("utf-8")
        last = ""
        for attempt in range(5):
            if attempt:
                self.retries += 1
            req = urllib.request.Request(
                URL,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.key}",
                    "Content-Type": "application/json",
                },
            )
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                choice = data["choices"][0]
                text = choice["message"]["content"] or ""
                usage = data.get("usage", {}) or {}
                elapsed = round(time.time() - t0, 3)
                self.cache[cache_key] = {
                    "prompt_sha256": sig,
                    "model": data.get("model") or MODEL,
                    "max_tokens": max_tokens,
                    "temperature": TEMPERATURE,
                    "thinking": thinking,
                    "tag": tag,
                    "cache_schema": "group|qid|role",
                    "text": text,
                    "usage": usage,
                    "finish_reason": choice.get("finish_reason"),
                    "elapsed_s": elapsed,
                    "created_at": now_utc(),
                }
                self.new_calls += 1
                self.calls.append({"tag": tag, "usage": usage, "max_tokens": max_tokens})
                self.cumulative["calls"] = int(self.cumulative.get("calls") or 0) + 1
                self.cumulative["prompt_tokens"] = int(self.cumulative.get("prompt_tokens") or 0) + int(
                    usage.get("prompt_tokens") or 0
                )
                self.cumulative["completion_tokens"] = int(
                    self.cumulative.get("completion_tokens") or 0
                ) + int(usage.get("completion_tokens") or 0)
                self.cache["_usage"] = self.cumulative
                return {
                    "text": text,
                    "usage": usage,
                    "finish_reason": choice.get("finish_reason"),
                    "cache_key": cache_key,
                    "cached": False,
                }
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}: {exc.read()[:300]!r}"
                if exc.code in (400, 401, 403, 404):
                    break
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(min(30, 2 * (attempt + 1)))
        self.flush()
        msg = f"{tag or 'llm'} 调用失败：{last}"
        self.failures.append({"cache_key": cache_key, "tag": tag, "error": msg})
        raise LLMError(msg)


# ---------------------------------------------------------------------------
# 数据 / prompt
# ---------------------------------------------------------------------------
def load_records(limit: int = 0) -> list[dict]:
    recs = []
    with CLEAN.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    if limit:
        recs = recs[:limit]
    return recs


def smoke_selection(records: list[dict], n: int = 5) -> list[dict]:
    """按 category 轮转取前 n 题（覆盖 focus 类 + abstention），可复现。"""
    order = ["knowledge-update", "temporal", "multi-session", "abstention", "preference", "other", "assistant"]
    picked: list[dict] = []
    for cat in order:
        pool = [r for r in records if r["category"] == cat]
        if pool and len(picked) < n:
            picked.append(pool[0])
    for r in records:
        if len(picked) >= n:
            break
        if r not in picked:
            picked.append(r)
    return picked[:n]


def render_facts(rec: dict) -> str:
    rows = sorted(rec["facts"], key=lambda f: (f["date"], f["session_id"], f["turn_index"]))
    return "\n".join(f"[{f['date']}] ({f['role']}) {f['text']}" for f in rows)


def reader_prompt(rec: dict, group: str, contracts: dict[str, str]) -> str:
    body = PREAMBLE_EN
    if group in CONTRACT_GROUP:
        body += "\n\n" + contracts[CONTRACT_GROUP[group]]
    body += "\n\nQuestion:\n" + rec["question"] + "\n\nRecords:\n" + render_facts(rec)
    return body


def question_block(rec: dict) -> str:
    """三组必须逐字相同的部分（题面 + 全部记录）。"""
    return "\n\nQuestion:\n" + rec["question"] + "\n\nRecords:\n" + render_facts(rec)


def judge_prompt(rec: dict, answer: str) -> str:
    return JUDGE_PROMPT.format(
        question=rec["question"],
        gold=rec["gold_answer"],
        acceptable="; ".join(rec["acceptable_answers"]),
        answer=answer.strip()[:4000],
    )


def norm_verdict(text: str) -> str:
    t = (text or "").strip()
    for v in VERDICTS:
        if v in t:
            return v
    up = t.upper()
    if "CORRECT" in up or "正确" in t:
        return "✅"
    if "PARTIAL" in up or "合格" in t or "部分正确" in t:
        return "⚠️"
    if "INCORRECT" in up or "错误" in t or "错" in t:
        return "❌"
    return "?"


def parse_judge(text: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*", "", (text or "").strip())
    t = re.sub(r"\s*```$", "", t)
    raw = None
    try:
        raw = json.loads(t)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                raw = json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                raw = None
    if not isinstance(raw, dict):
        return {"verdict": "?", "answer_state": None, "reason": "judge output unparsable",
                "raw": (text or "")[:300]}
    verdict = norm_verdict(str(raw.get("verdict") or ""))
    state = str(raw.get("answer_state") or "").strip().lower()
    if state not in {"answered", "abstained", "conflict_flagged"}:
        state = None
    return {"verdict": verdict, "answer_state": state,
            "reason": str(raw.get("reason") or "")[:300], "raw": str(raw.get("verdict"))[:20]}


def load_contracts() -> dict[str, str]:
    zh = CONTRACT_ZH.read_text(encoding="utf-8").strip()
    en = CONTRACT_EN.read_text(encoding="utf-8").strip()
    if not zh or not en:
        raise SystemExit("契约文件为空")
    return {"zh": zh, "en": en}


# ---------------------------------------------------------------------------
# 自检：契约逐字 / 三组只差契约 / 中英规则平行
# ---------------------------------------------------------------------------
def contract_shape(text: str, exception_pattern: str) -> dict:
    return {
        "rules": len(re.findall(r"(?m)^\d+\.", text)),
        "sub_rules_2a_2b_2c": len(re.findall(r"(?m)^\s{3}[abc]\.", text)),
        "has_exception": bool(re.search(exception_pattern, text)),
        "non_empty_lines": len([ln for ln in text.splitlines() if ln.strip()]),
    }


def self_check(records: list[dict], contracts: dict[str, str]) -> dict:
    zh, en = contracts["zh"], contracts["en"]
    shape_zh = contract_shape(zh, "例外")
    shape_en = contract_shape(en, r"(?im)^\s*exception")
    parity = {
        "zh": shape_zh,
        "en": shape_en,
        "rules_parallel": shape_zh["rules"] == shape_en["rules"] == 6,
        "sub_rules_parallel": shape_zh["sub_rules_2a_2b_2c"] == shape_en["sub_rules_2a_2b_2c"] == 3,
        "exception_both": shape_zh["has_exception"] and shape_en["has_exception"],
    }

    sample = smoke_selection(records, 5)
    # 逐题验证构造不变式（全量 337 题；渲染很快）：三组只差契约块，题面/记录逐字相同。
    checks = {
        "A_has_no_contract": True,
        "E_has_zh_verbatim": True,
        "F_has_en_verbatim": True,
        "A_excludes_en_contract": True,
        "question_block_identical_across_groups": True,
        "A_is_E_minus_contract": True,
        "A_is_F_minus_contract": True,
        "verified_questions": len(records),
        "sample_verified": len(sample),
    }
    zh_first = zh.splitlines()[0].strip()
    en_first = en.splitlines()[0].strip()
    for rec in records:
        pa = reader_prompt(rec, "A", contracts)
        pe = reader_prompt(rec, "E", contracts)
        pf = reader_prompt(rec, "F", contracts)
        block = question_block(rec)
        if zh in pa or en in pa:
            checks["A_has_no_contract"] = False
            checks["A_has_no_contract@"] = rec["id"]
        if zh_first in pa:
            checks["A_has_no_contract"] = False
        if zh not in pe:
            checks["E_has_zh_verbatim"] = False
            checks["E_has_zh_verbatim@"] = rec["id"]
        if en not in pf:
            checks["F_has_en_verbatim"] = False
            checks["F_has_en_verbatim@"] = rec["id"]
        if en_first in pa or en in pa:
            checks["A_excludes_en_contract"] = False
        if not (pa.endswith(block) and pe.endswith(block) and pf.endswith(block)):
            checks["question_block_identical_across_groups"] = False
            checks["question_block_identical_across_groups@"] = rec["id"]
        if pe != PREAMBLE_EN + "\n\n" + zh + block:
            checks["A_is_E_minus_contract"] = False
        if pf != PREAMBLE_EN + "\n\n" + en + block:
            checks["A_is_F_minus_contract"] = False
        if pa != PREAMBLE_EN + block:
            checks["A_is_E_minus_contract"] = False
    ok = all(v for k, v in checks.items() if not k.endswith("@")) and all(
        parity[k] for k in ("rules_parallel", "sub_rules_parallel", "exception_both")
    )
    return {
        "checks": checks,
        "parity": parity,
        "ok": ok,
        "contracts": {
            "zh": {"path": rel(CONTRACT_ZH), "sha256": sha256_file(CONTRACT_ZH),
                   "chars": len(zh), "lines": len(zh.splitlines())},
            "en": {"path": rel(CONTRACT_EN), "sha256": sha256_file(CONTRACT_EN),
                   "chars": len(en), "lines": len(en.splitlines()),
                   "provenance": "v4 的忠实英文翻译；规则 1-6 / 2a-2c / 例外逐条对应（见 parity）"},
        },
    }


# ---------------------------------------------------------------------------
# 跑：一题 × 三组 → reader + judge
# ---------------------------------------------------------------------------
def run_one(rec: dict, groups: tuple[str, ...], contracts: dict[str, str], client: Client) -> dict:
    row = {
        "qid": rec["id"],
        "source_item_id": rec["source_item_id"],
        "category": rec["category"],
        "is_abstention": rec["is_abstention"],
        "n_facts": len(rec["facts"]),
        "question": rec["question"],
        "gold_answer": rec["gold_answer"][:400],
        "groups": {},
    }
    block = question_block(rec)
    for group in groups:
        prompt = reader_prompt(rec, group, contracts)
        entry: dict = {}
        try:
            r = client.chat(
                prompt,
                max_tokens=READER_MAX_TOKENS,
                tag=f"reader:{group}:{rec['id']}",
                cache_key=f"{group}|{rec['id']}|reader",
                thinking=READER_THINKING,
            )
            entry.update(
                answer=r["text"].strip(),
                answer_chars=len(r["text"]),
                answer_truncated=(r["finish_reason"] == "length"),
                reader_usage=compact_usage(r["usage"]),
                reader_cached=r["cached"],
            )
        except LLMError as exc:
            entry.update(error=str(exc), verdict="?")
            row["groups"][group] = entry
            continue
        try:
            j = client.chat(
                judge_prompt(rec, entry["answer"]),
                max_tokens=JUDGE_MAX_TOKENS,
                tag=f"judge:{group}:{rec['id']}",
                cache_key=f"{group}|{rec['id']}|judge",
                thinking=JUDGE_THINKING,
            )
            parsed = parse_judge(j["text"])
            entry.update(
                verdict=parsed["verdict"],
                answer_state=parsed["answer_state"],
                judge_reason=parsed["reason"],
                judge_raw_verdict=parsed["raw"],
                judge_raw=(j["text"] or "")[:600] if parsed["verdict"] == "?" else None,
                judge_usage=compact_usage(j["usage"]),
                judge_cached=j["cached"],
            )
        except LLMError as exc:
            entry.update(error=str(exc), verdict="?")
        row["groups"][group] = entry
    row["n_contract_chars"] = {g: (0 if g == "A" else len(contracts[CONTRACT_GROUP[g]])) for g in groups}
    return row


def compact_usage(u: dict | None) -> dict:
    u = u or {}
    return {
        "prompt_tokens": int(u.get("prompt_tokens") or 0),
        "completion_tokens": int(u.get("completion_tokens") or 0),
        "prompt_cache_hit_tokens": int(u.get("prompt_cache_hit_tokens") or 0),
        "prompt_cache_miss_tokens": int(
            u.get("prompt_cache_miss_tokens")
            if u.get("prompt_cache_miss_tokens") is not None
            else (u.get("prompt_tokens") or 0)
        ),
        "reasoning_tokens": int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
    }


def run(records: list[dict], groups: tuple[str, ...], contracts: dict[str, str],
        client: Client, workers: int) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    t0 = time.time()
    total = len(records)
    print(f"run: {total} questions × {len(groups)} groups {groups} on {workers} workers", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(run_one, rec, groups, contracts, client): rec for rec in records}
        for i, fut in enumerate(as_completed(futures), start=1):
            rows.append(fut.result())
            if i % 20 == 0 or i == total:
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed else 0
                print(
                    f"  {i}/{total} ({elapsed:.0f}s, ~{(total - i) / rate if rate else 0:.0f}s left, "
                    f"new={client.new_calls} hit={client.cache_hits})",
                    flush=True,
                )
                client.flush()
    client.flush()
    rows.sort(key=lambda r: r["qid"])
    meta = {
        "wall_clock_s": round(time.time() - t0, 1),
        "new_calls": client.new_calls,
        "cache_hits": client.cache_hits,
        "prompt_mismatch": client.prompt_mismatch,
        "retries": client.retries,
    }
    return rows, meta


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def v(row: dict, group: str) -> str:
    return ((row.get("groups") or {}).get(group) or {}).get("verdict") or "?"


def rate(k: int, n: int) -> float | None:
    return (k / n) if n else None


def pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def summarise(rows: list[dict], groups: tuple[str, ...]) -> dict:
    out: dict = {}
    for g in groups:
        c = {k: 0 for k in VERDICTS}
        invalid = 0
        for r in rows:
            verdict = v(r, g)
            if verdict in c:
                c[verdict] += 1
            else:
                invalid += 1
        n = len(rows)
        out[g] = {
            **c,
            "invalid": invalid,
            "n": n,
            "accuracy": rate(c["✅"], n),
            "qualified": rate(c["✅"] + c["⚠️"], n),
        }
    return out


def summarise_by_category(rows: list[dict], groups: tuple[str, ...]) -> dict:
    cats = sorted({r["category"] for r in rows}, key=lambda c: (CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99, c))
    out: dict = {}
    for cat in cats:
        sub = [r for r in rows if r["category"] == cat]
        out[cat] = summarise(sub, groups)
    return out


def mcnemar(b: int, c: int) -> dict:
    """配对二分类：b = 前者对/后者错，c = 前者错/后者对。返回精确 p 与连续性校正 p。"""
    n = b + c
    if n == 0:
        return {"b": b, "c": c, "n_discordant": 0, "p_exact": 1.0, "p_chi2_cc": 1.0,
                "odds_ratio": None, "net": 0}
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    p_exact = min(1.0, 2.0 * tail)
    chi2 = (abs(b - c) - 1) ** 2 / n
    p_cc = math.erfc(math.sqrt(max(chi2, 0.0) / 2.0))
    return {
        "b": b,
        "c": c,
        "n_discordant": n,
        "p_exact": p_exact,
        "p_chi2_cc": p_cc,
        "odds_ratio": (c / b) if b else None,
        "net": c - b,
    }


def paired_bootstrap(rows: list[dict], ga: str, gb: str, metric: str,
                     seed: int = SEED, B: int = BOOTSTRAP_B) -> dict:
    """配对 bootstrap：gb 减 ga 的比例差的 95% CI 与双侧 p。metric: accuracy | qualified | non_error。"""
    def ok(row: dict, g: str) -> bool:
        verdict = v(row, g)
        if metric == "accuracy":
            return verdict == "✅"
        if metric == "qualified":
            return verdict in ("✅", "⚠️")
        if metric == "non_error":
            return verdict in ("✅", "⚠️")
        raise ValueError(metric)

    a = [1 if ok(r, ga) else 0 for r in rows]
    b = [1 if ok(r, gb) else 0 for r in rows]
    n = len(rows)
    pa, pb = sum(a) / n, sum(b) / n
    diffs = [x - y for x, y in zip(b, a)]
    try:  # numpy 在时向量化（快 100 倍）；不在时退回纯 random，seed 同义。
        import numpy as np

        rng = np.random.default_rng(seed)
        idx = rng.integers(0, n, size=(B, n))
        boots = np.asarray(diffs, dtype=float)[idx].mean(axis=1)
        lo = float(np.quantile(boots, 0.025))
        hi = float(np.quantile(boots, 0.975))
        p = float(min(1.0, 2 * min((boots <= 0).mean(), (boots >= 0).mean())))
    except ImportError:  # pragma: no cover
        rng = random.Random(seed)
        boots = []
        for _ in range(B):
            s = 0
            for _ in range(n):
                s += diffs[rng.randrange(n)]
            boots.append(s / n)
        boots.sort()
        lo = boots[int(0.025 * (B - 1))]
        hi = boots[int(0.975 * (B - 1))]
        le0 = sum(1 for x in boots if x <= 0) / B
        ge0 = sum(1 for x in boots if x >= 0) / B
        p = min(1.0, 2 * min(le0, ge0))
    return {
        "metric": metric,
        "rate_a": pa,
        "rate_b": pb,
        "diff": pb - pa,
        "ci95": [lo, hi],
        "p_bootstrap": p,
        "boot_B": B,
        "boot_seed": seed,
    }


def compare(rows: list[dict], ga: str, gb: str, label: str, metric: str) -> dict:
    b = sum(1 for r in rows if v(r, ga) == "✅" and v(r, gb) != "✅")
    c = sum(1 for r in rows if v(r, ga) != "✅" and v(r, gb) == "✅")
    mc = mcnemar(b, c)
    bs = paired_bootstrap(rows, ga, gb, metric)
    n = len(rows)
    return {
        "comparison": f"{ga} -> {gb}",
        "label": label,
        "metric": metric,
        "n": n,
        "test": "McNemar exact (binomial) on ✅ vs not-✅; paired bootstrap for the difference",
        "b_only_a_correct": b,
        "c_only_b_correct": c,
        "p_exact": mc["p_exact"],
        "p_chi2_cc": mc["p_chi2_cc"],
        "odds_ratio_improve": mc["odds_ratio"],
        "net_improve": mc["net"],
        "diff_rate": bs["diff"],
        "diff_rate_ci95": bs["ci95"],
        "p_bootstrap": bs["p_bootstrap"],
        "accuracy_a": bs["rate_a"],
        "accuracy_b": bs["rate_b"],
    }


def compare_error_reduction(rows: list[dict], ga: str, gb: str, label: str) -> dict:
    """安全侧：❌ 是否减少（success = not ❌）。"""
    b = sum(1 for r in rows if v(r, ga) != "❌" and v(r, gb) == "❌")  # A 好 B 坏
    c = sum(1 for r in rows if v(r, ga) == "❌" and v(r, gb) != "❌")  # A 坏 B 好
    mc = mcnemar(b, c)
    n = len(rows)
    err_a = sum(1 for r in rows if v(r, ga) == "❌")
    err_b = sum(1 for r in rows if v(r, gb) == "❌")
    return {
        "comparison": f"{ga} -> {gb}",
        "label": label,
        "metric": "❌ vs not-❌ (b = A fine / B wrong, c = A wrong / B fine)",
        "n": n,
        "errors_a": err_a,
        "errors_b": err_b,
        "errors_a_rate": rate(err_a, n),
        "errors_b_rate": rate(err_b, n),
        "b_new_errors": b,
        "c_fixed_errors": c,
        "p_exact": mc["p_exact"],
        "p_chi2_cc": mc["p_chi2_cc"],
        "net_fixed": c - b,
    }


def bh_adjust(named: list[tuple[str, float]]) -> dict:
    """Benjamini–Hochberg FDR。"""
    m = len(named)
    ordered = sorted(named, key=lambda kv: kv[1])
    adj: dict[str, float] = {}
    prev = 1.0
    for i in range(m - 1, -1, -1):
        name, p = ordered[i]
        val = min(prev, p * m / (i + 1))
        adj[name] = min(1.0, val)
        prev = val
    return {name: {"p_raw": p, "p_bh": adj[name]} for name, p in named}


def transitions(rows: list[dict], ga: str, gb: str) -> dict:
    counts: dict[str, int] = {}
    changed: list[dict] = []
    for r in rows:
        a, b = v(r, ga), v(r, gb)
        key = f"{a}->{b}"
        counts[key] = counts.get(key, 0) + 1
        if a != b:
            changed.append({
                "qid": r["qid"],
                "category": r["category"],
                "is_abstention": r["is_abstention"],
                "from": a,
                "to": b,
                "improved": a != "✅" and b == "✅",
                "worsened": a == "✅" and b != "✅",
                "reason_a": (r["groups"][ga].get("judge_reason") or "")[:160],
                "reason_b": (r["groups"][gb].get("judge_reason") or "")[:160],
            })
    improved = [c for c in changed if c["improved"]]
    worsened = [c for c in changed if c["worsened"]]
    return {
        "counts": dict(sorted(counts.items())),
        "n_changed": len(changed),
        "n_improved": len(improved),
        "n_worsened": len(worsened),
        "improved": improved,
        "worsened": worsened,
        "changed": changed,
    }


def wilson(k: int, n: int) -> list[float]:
    """Wilson 95% CI（比例）。"""
    if n == 0:
        return [0.0, 1.0]
    z = 1.959963984540054
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [max(0.0, centre - half), min(1.0, centre + half)]


def emit_markdown(payload: dict, groups: tuple[str, ...]) -> str:
    """把已落盘 JSON 渲染成报告要用的 markdown 表（0 调用）。"""
    rows = payload["rows"]
    an = payload["analysis"]
    cost = payload["cost"]
    out: list[str] = []

    out.append("### 总表\n")
    out.append("| 组 | ✅ | ⚠️ | ❌ | 判分不可解析 | 正确率（✅/n） | 95% CI | 合格率（✅+⚠️） |")
    out.append("|---|---:|---:|---:|---:|---:|---|---:|")
    for g in groups:
        c = an["overall"][g]
        lo, hi = wilson(c["✅"], c["n"])
        out.append(
            f"| **{g}**（{GROUP_LABEL[g]}） | {c['✅']} | {c['⚠️']} | {c['❌']} | {c['invalid']} | "
            f"{pct(c['accuracy'])} | {100 * lo:.1f}–{100 * hi:.1f}% | {pct(c['qualified'])} |"
        )

    out.append("\n### 按 category\n")
    out.append("| category | n | " + " | ".join(f"{g} ✅/⚠️/❌（正确率）" for g in groups) + " |")
    out.append("|---|---:|" + "---|" * len(groups))
    for cat, cells in an["by_category"].items():
        n = cells[groups[0]]["n"]
        line = f"| {cat} | {n} | "
        line += " | ".join(
            f"{cells[g]['✅']}/{cells[g]['⚠️']}/{cells[g]['❌']}（{pct(cells[g]['accuracy'])}）" for g in groups
        )
        out.append(line + " |")

    out.append("\n### 配对检验（✅ vs 非✅，McNemar 精确检验 + 配对 bootstrap）\n")
    out.append("| 对比 | b（仅前者✅） | c（仅后者✅） | Δ正确率 | 95% CI（配对 bootstrap） | p（精确） | p（bootstrap） | p（BH） | OR |")
    out.append("|---|---:|---:|---:|---|---:|---:|---:|---:|")
    bh = an["multiplicity_bh"]
    for t in an["tests"]["overall"]:
        key = f"overall:{t['comparison']}"
        adj = bh.get(key, {}).get("p_bh")
        out.append(
            f"| {t['comparison']} | {t['b_only_a_correct']} | {t['c_only_b_correct']} | "
            f"{100 * t['diff_rate']:+.1f}pp | "
            f"[{100 * t['diff_rate_ci95'][0]:+.1f}, {100 * t['diff_rate_ci95'][1]:+.1f}]pp | "
            f"{t['p_exact']:.4f} | {t['p_bootstrap']:.4f} | "
            f"{'—' if adj is None else f'{adj:.4f}'} | "
            f"{'—' if t['odds_ratio_improve'] is None else f'{t['odds_ratio_improve']:.2f}'} |"
        )

    out.append("\n### ❌ 安全侧（配对）\n")
    out.append("| 对比 | 前者 ❌ | 后者 ❌ | 修好（❌→非❌） | 新增（非❌→❌） | p（精确） |")
    out.append("|---|---:|---:|---:|---:|---:|")
    for t in an["tests"]["errors"]:
        out.append(
            f"| {t['comparison']} | {t['errors_a']} | {t['errors_b']} | {t['c_fixed_errors']} | "
            f"{t['b_new_errors']} | {t['p_exact']:.4f} |"
        )

    out.append("\n### 重点三类（契约 2a/2b 最相关）\n")
    out.append("| category | n | A ✅ | E ✅ | F ✅ | A→E b/c | A→E p | A→E Δ | A→F p | A→F Δ |")
    out.append("|---|---:|---:|---:|---:|---|---:|---:|---:|---:|")
    for cat, cell in an["focus_categories"].items():
        s = cell["summary"]
        ae, af = cell["A_vs_E"], cell["A_vs_F"]
        out.append(
            f"| {cat} | {cell['n']} | {pct(s['A']['accuracy'])} | {pct(s['E']['accuracy'])} | "
            f"{pct(s['F']['accuracy'])} | {ae['b_only_a_correct']}/{ae['c_only_b_correct']} | "
            f"{ae['p_exact']:.4f} | {100 * ae['diff_rate']:+.1f}pp | {af['p_exact']:.4f} | "
            f"{100 * af['diff_rate']:+.1f}pp |"
        )

    out.append("\n### 语言对照 E vs F\n")
    lang = an["language"]
    out.append(
        f"- E 独对 {lang['E_only_correct']}、F 独对 {lang['F_only_correct']}、都对 {lang['both_correct']}、"
        f"都错 {lang['neither_correct']}、判分相同 {lang['same_verdict']}/{an['n_questions']}"
    )
    out.append(
        f"- McNemar p={lang['E_vs_F']['p_exact']:.4f}，Δ={100 * lang['E_vs_F']['diff_rate']:+.1f}pp "
        f"（CI [{100 * lang['E_vs_F']['diff_rate_ci95'][0]:+.1f}, {100 * lang['E_vs_F']['diff_rate_ci95'][1]:+.1f}]）"
    )
    out.append("- 回答语言：" + "；".join(
        f"{g} 中文 {an['answer_language'][g]['mostly_chinese']}/{an['n_questions']}" for g in groups
    ))

    out.append("\n### 成本\n")
    out.append("| 组 | 调用 | prompt tokens | completion tokens | 合计 tokens | 估算费用（off-peak, USD） |")
    out.append("|---|---:|---:|---:|---:|---:|")
    for g in groups:
        c = cost["by_group"][g]
        out.append(
            f"| {g} | {c['calls']} | {c['prompt_tokens']:,} | {c['completion_tokens']:,} | "
            f"{c['total_tokens']:,} | ${c['cost_usd']['off_peak']:.4f} |"
        )
    t = cost["totals"]
    out.append(
        f"| **合计** | {t['calls']} | {t['prompt_tokens']:,} | {t['completion_tokens']:,} | "
        f"{t['total_tokens']:,} | ${cost['cost_usd']['off_peak']:.4f} |"
    )
    out.append(
        f"\n- 角色：reader {cost['by_role']['reader']['calls']} 次 / {cost['by_role']['reader']['total_tokens']:,} tokens；"
        f"judge {cost['by_role']['judge']['calls']} 次 / {cost['by_role']['judge']['total_tokens']:,} tokens"
    )
    out.append(
        f"- prompt cache：hit {t['prompt_cache_hit_tokens']:,} / miss {t['prompt_cache_miss_tokens']:,} tokens；"
        f"peak 价上限 ${cost['cost_usd']['peak_upper_bound']:.4f}"
    )

    for ga, gb in (("A", "E"), ("A", "F")):
        tr = an["transitions"][f"{ga}->{gb}"]
        out.append(f"\n### 附录：{ga} → {gb} 逐题变动（{tr['n_changed']} 题；✅ 相关 {tr['n_improved']} 好 / {tr['n_worsened']} 坏）\n")
        out.append(f"**变好**（{ga} 非✅ → {gb} ✅）\n")
        out.append(f"| qid | category | {ga}→{gb} | 判分理由（{gb}） |")
        out.append("|---|---|---|---|")
        for c in tr["improved"]:
            qid = c["qid"].replace("lme-oracle-", "")
            reason = (c["reason_b"] or "").replace("|", "/")[:110]
            out.append(f"| {qid} | {c['category']} | {c['from']}→{c['to']} | {reason} |")
        out.append(f"\n**变差**（{ga} ✅ → {gb} 非✅）\n")
        out.append(f"| qid | category | {ga}→{gb} | 判分理由（{gb}） |")
        out.append("|---|---|---|---|")
        for c in tr["worsened"]:
            qid = c["qid"].replace("lme-oracle-", "")
            reason = (c["reason_b"] or "").replace("|", "/")[:110]
            out.append(f"| {qid} | {c['category']} | {c['from']}→{c['to']} | {reason} |")
    return "\n".join(out)


def cost_block(rows: list[dict], cache: dict, groups: tuple[str, ...]) -> dict:
    def cost_of(usage: dict) -> dict:
        hit = usage.get("prompt_cache_hit_tokens") or 0
        miss = usage.get("prompt_cache_miss_tokens")
        if miss is None:
            miss = max(0, (usage.get("prompt_tokens") or 0) - hit)
        out = usage.get("completion_tokens") or 0
        return {
            "off_peak": hit / 1e6 * RATES["off_peak"]["input_hit"]
            + miss / 1e6 * RATES["off_peak"]["input_miss"]
            + out / 1e6 * RATES["off_peak"]["output"],
            "peak": hit / 1e6 * RATES["peak"]["input_hit"]
            + miss / 1e6 * RATES["peak"]["input_miss"]
            + out / 1e6 * RATES["peak"]["output"],
        }

    totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
              "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0}
    by_group: dict[str, dict] = {g: dict(totals, calls=0) for g in groups}
    by_role: dict[str, dict] = {"reader": dict(totals, calls=0), "judge": dict(totals, calls=0)}
    costs = {"off_peak": 0.0, "peak": 0.0}

    for r in rows:
        for g in groups:
            entry = (r.get("groups") or {}).get(g) or {}
            for role, key in (("reader", "reader_usage"), ("judge", "judge_usage")):
                usage = entry.get(key)
                if not usage:
                    continue
                for k in totals:
                    if k == "calls":
                        totals[k] += 1
                        by_group[g][k] += 1
                        by_role[role][k] += 1
                    else:
                        totals[k] += int(usage.get(k) or 0)
                        by_group[g][k] += int(usage.get(k) or 0)
                        by_role[role][k] += int(usage.get(k) or 0)
                cc = cost_of(usage)
                costs["off_peak"] += cc["off_peak"]
                costs["peak"] += cc["peak"]
                by_group[g].setdefault("cost_usd", {"off_peak": 0.0, "peak": 0.0})
                by_group[g]["cost_usd"]["off_peak"] += cc["off_peak"]
                by_group[g]["cost_usd"]["peak"] += cc["peak"]
                by_role[role].setdefault("cost_usd", {"off_peak": 0.0, "peak": 0.0})
                by_role[role]["cost_usd"]["off_peak"] += cc["off_peak"]
                by_role[role]["cost_usd"]["peak"] += cc["peak"]

    totals["total_tokens"] = totals["prompt_tokens"] + totals["completion_tokens"]
    for d in [by_group, by_role]:
        for vv in d.values():
            vv["total_tokens"] = vv["prompt_tokens"] + vv["completion_tokens"]

    cache_usage = dict(cache.get("_usage") or {})
    return {
        "model": MODEL,
        "endpoint": URL,
        "rates_usd_per_1m_tokens": RATES,
        "rates_source": RATES_SOURCE,
        "totals": totals,
        "cost_usd": {"off_peak": round(costs["off_peak"], 4), "peak_upper_bound": round(costs["peak"], 4)},
        "by_group": by_group,
        "by_role": by_role,
        "cache_ledger": {
            "calls": int(cache_usage.get("calls") or 0),
            "prompt_tokens": int(cache_usage.get("prompt_tokens") or 0),
            "completion_tokens": int(cache_usage.get("completion_tokens") or 0),
            "note": "缓存文件里的累计账本（只在新调用时累加，命中不加）",
        },
        "note": "cost = cache-hit 输入 × hit 单价 + cache-miss 输入 × miss 单价 + completion × 输出单价；"
                "thinking 的 reasoning_tokens 计入 completion。peak 为上界（同一批 token 按 peak 单价）。",
    }


# ---------------------------------------------------------------------------
# 分析（也用于 --analyze-only）
# ---------------------------------------------------------------------------
def cjk_ratio(text: str) -> float:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return cjk / max(1, cjk + latin)


def answer_language(rows: list[dict], groups: tuple[str, ...]) -> dict:
    """契约是中文，模型可能顺着契约用中文回答；这里量化，避免把语言效应当契约效应。"""
    out: dict = {}
    for g in groups:
        zh = en = mixed = 0
        for r in rows:
            text = (r.get("groups", {}).get(g) or {}).get("answer") or ""
            ratio = cjk_ratio(text)
            if ratio >= 0.2:
                zh += 1
            elif ratio <= 0.001:
                en += 1
            else:
                mixed += 1
        out[g] = {"mostly_chinese": zh, "english_only": en, "mixed": mixed,
                  "mostly_chinese_rate": rate(zh, len(rows))}
    return out


def analyse(rows: list[dict], groups: tuple[str, ...], cache: dict) -> dict:
    overall = summarise(rows, groups)
    by_cat = summarise_by_category(rows, groups)
    n = len(rows)

    tests: dict[str, list[dict]] = {"overall": [], "errors": []}
    for gb in ("E", "F"):
        tests["overall"].append(compare(rows, "A", gb, f"A vs {gb}（全量 {n} 题）", "accuracy"))
        tests["errors"].append(compare_error_reduction(rows, "A", gb, f"A vs {gb}（全量 {n} 题）"))
    tests["overall"].append(compare(rows, "E", "F", f"E vs F（语言对照，全量 {n} 题）", "accuracy"))

    focus: dict[str, dict] = {}
    for cat in FOCUS_CATEGORIES:
        sub = [r for r in rows if r["category"] == cat]
        if not sub:
            continue
        focus[cat] = {
            "n": len(sub),
            "summary": summarise(sub, groups),
            "A_vs_E": compare(sub, "A", "E", f"A vs E（{cat}）", "accuracy"),
            "A_vs_F": compare(sub, "A", "F", f"A vs F（{cat}）", "accuracy"),
            "E_vs_F": compare(sub, "E", "F", f"E vs F（{cat}）", "accuracy"),
            "A_vs_E_errors": compare_error_reduction(sub, "A", "E", f"A vs E ❌（{cat}）"),
            "transitions_A_E": transitions(sub, "A", "E"),
            "transitions_A_F": transitions(sub, "A", "F"),
        }

    by_cat_tests: dict[str, dict] = {}
    for cat, cell in by_cat.items():
        sub = [r for r in rows if r["category"] == cat]
        if len(sub) < 5:
            continue
        by_cat_tests[cat] = {
            "n": len(sub),
            "A_vs_E": compare(sub, "A", "E", f"A vs E（{cat}）", "accuracy"),
            "A_vs_F": compare(sub, "A", "F", f"A vs F（{cat}）", "accuracy"),
            "E_vs_F": compare(sub, "E", "F", f"E vs F（{cat}）", "accuracy"),
        }

    family: list[tuple[str, float]] = []
    for t in tests["overall"]:
        family.append((f"overall:{t['comparison']}", t["p_exact"]))
    for cat, cell in focus.items():
        family.append((f"{cat}:A_vs_E", cell["A_vs_E"]["p_exact"]))
        family.append((f"{cat}:A_vs_F", cell["A_vs_F"]["p_exact"]))
    bh = bh_adjust(family)

    lang = {
        "E_vs_F": tests["overall"][2],
        "E_vs_F_errors": compare_error_reduction(rows, "E", "F", "E vs F（❌ 对照）"),
        "E_only_correct": sum(1 for r in rows if v(r, "E") == "✅" and v(r, "F") != "✅"),
        "F_only_correct": sum(1 for r in rows if v(r, "F") == "✅" and v(r, "E") != "✅"),
        "both_correct": sum(1 for r in rows if v(r, "E") == "✅" and v(r, "F") == "✅"),
        "neither_correct": sum(1 for r in rows if v(r, "E") != "✅" and v(r, "F") != "✅"),
        "same_verdict": sum(1 for r in rows if v(r, "E") == v(r, "F")),
    }

    return {
        "n_questions": n,
        "verdict_counts": {
            g: {k: sum(1 for r in rows if v(r, g) == k) for k in VERDICTS} for g in groups
        },
        "overall": overall,
        "by_category": by_cat,
        "by_category_tests": by_cat_tests,
        "focus_categories": focus,
        "tests": tests,
        "multiplicity_bh": bh,
        "language": lang,
        "transitions": {"A->E": transitions(rows, "A", "E"), "A->F": transitions(rows, "A", "F"),
                        "E->F": transitions(rows, "E", "F")},
        "answer_states": {
            g: {s: sum(1 for r in rows if ((r["groups"].get(g) or {}).get("answer_state") == s))
                for s in ("answered", "abstained", "conflict_flagged", None)}
            for g in groups
        },
        "truncated_answers": {
            g: sum(1 for r in rows if (r["groups"].get(g) or {}).get("answer_truncated")) for g in groups
        },
        "empty_answers": {
            g: sum(1 for r in rows if not ((r["groups"].get(g) or {}).get("answer") or "").strip())
            for g in groups
        },
        "mean_answer_chars": {
            g: round(statistics.mean([len((r["groups"].get(g) or {}).get("answer") or "") for r in rows]), 1)
            for g in groups
        },
        "answer_language": answer_language(rows, groups),
    }


def failure_report(rows: list[dict], groups: tuple[str, ...]) -> dict:
    out = {}
    for g in groups:
        errors = [r["qid"] for r in rows if (r["groups"].get(g) or {}).get("error")]
        invalid = [r["qid"] for r in rows if v(r, g) == "?" and not (r["groups"].get(g) or {}).get("error")]
        empty = [r["qid"] for r in rows if not ((r["groups"].get(g) or {}).get("answer") or "").strip()]
        out[g] = {
            "n": len(rows),
            "call_failures": len(errors),
            "call_failure_rate": rate(len(errors), len(rows)),
            "invalid_judge": len(invalid),
            "invalid_judge_rate": rate(len(invalid), len(rows)),
            "empty_answers": len(empty),
            "empty_answer_rate": rate(len(empty), len(rows)),
        }
    return out


# ---------------------------------------------------------------------------
# 打印
# ---------------------------------------------------------------------------
def print_summary(analysis: dict, groups: tuple[str, ...]) -> None:
    print("\n== 总体 ==")
    print(f"{'组':<3}{'✅':>5}{'⚠️':>5}{'❌':>5}{'?':>4}{'正确率':>9}{'合格率':>9}")
    for g in groups:
        c = analysis["overall"][g]
        print(f"{g:<3}{c['✅']:>5}{c['⚠️']:>5}{c['❌']:>5}{c['invalid']:>4}"
              f"{pct(c['accuracy']):>9}{pct(c['qualified']):>9}")

    print("\n== 按 category ==")
    for cat, cells in analysis["by_category"].items():
        line = f"{cat:<16}"
        for g in groups:
            c = cells[g]
            line += f"  {g}: {c['✅']:>3}/{c['⚠️']:>3}/{c['❌']:>3} {pct(c['accuracy']):>7}"
        print(line)

    print("\n== 回答语言（中文契约可能把回答语言也带过去） ==")
    for g in groups:
        a = analysis["answer_language"][g]
        print(f"  {g}: 中文 {a['mostly_chinese']} 英文 {a['english_only']} 混合 {a['mixed']}")

    print("\n== 截断 / 空回答 ==")
    for g in groups:
        print(f"  {g}: truncated={analysis['truncated_answers'][g]} empty={analysis['empty_answers'][g]}")

    print("\n== 配对检验（✅ vs 非✅，McNemar 精确检验） ==")
    for t in analysis["tests"]["overall"]:
        print(f"{t['comparison']:<12} b={t['b_only_a_correct']:<4} c={t['c_only_b_correct']:<4} "
              f"Δ={100 * t['diff_rate']:+.1f}pp CI=[{100 * t['diff_rate_ci95'][0]:+.1f},"
              f"{100 * t['diff_rate_ci95'][1]:+.1f}] p_exact={t['p_exact']:.4g} "
              f"p_boot={t['p_bootstrap']:.4g} OR={t['odds_ratio_improve']}")

    print("\n== 安全侧（❌ 计数） ==")
    for t in analysis["tests"]["errors"]:
        print(f"{t['comparison']:<12} ❌ {t['errors_a']}→{t['errors_b']} "
              f"fixed={t['c_fixed_errors']} new={t['b_new_errors']} p_exact={t['p_exact']:.4g}")

    print("\n== 重点三类 ==")
    for cat, cell in analysis["focus_categories"].items():
        print(f"{cat} (n={cell['n']})")
        for k in ("A_vs_E", "A_vs_F", "E_vs_F"):
            t = cell[k]
            print(f"  {t['comparison']}: b={t['b_only_a_correct']} c={t['c_only_b_correct']} "
                  f"p={t['p_exact']:.4g} Δ={100 * t['diff_rate']:+.1f}pp")

    print("\n== 语言对照 E vs F ==")
    lang = analysis["language"]
    print(f"  E only ✅ {lang['E_only_correct']}, F only ✅ {lang['F_only_correct']}, "
          f"both ✅ {lang['both_correct']}, neither ✅ {lang['neither_correct']}, "
          f"同判 {lang['same_verdict']}/{analysis['n_questions']}")
    print(f"  p_exact={lang['E_vs_F']['p_exact']:.4g} Δ={100 * lang['E_vs_F']['diff_rate']:+.1f}pp")

    print("\n== BH 校正（family = 3 个总体 + 重点三类 × A-vs-E/A-vs-F） ==")
    for name, cell in analysis["multiplicity_bh"].items():
        print(f"  {name:<28} p={cell['p_raw']:.4g} p_bh={cell['p_bh']:.4g}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="契约 v4 外部效度实验（LongMemEval 干净集）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（调试）")
    ap.add_argument("--smoke", action="store_true", help="5 题冒烟（按类轮转），打印逐题明细")
    ap.add_argument("--groups", default="A,E,F", help="逗号分隔的组，默认 A,E,F")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--force", action="store_true", help="忽略缓存重跑（默认命中缓存 0 调用）")
    ap.add_argument("--analyze-only", action="store_true", help="只读已落盘 JSON 做分析，不发请求")
    ap.add_argument("--markdown", action="store_true", help="只读已落盘 JSON，输出报告用的 markdown 表（0 调用）")
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args(argv)

    groups = tuple(x.strip().upper() for x in args.groups.split(",") if x.strip())
    for g in groups:
        if g not in GROUPS:
            raise SystemExit(f"未知组 {g!r}，可选 {GROUPS}")
    out_path = Path(args.out).resolve()
    if args.smoke and out_path == OUT_PATH:
        # 冒烟不能覆盖全量产物
        out_path = OUT_PATH.with_name("contract_ext_eval.smoke.json")

    if args.markdown:
        if not out_path.exists():
            raise SystemExit(f"{out_path} 不存在；先跑全量")
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        print(emit_markdown(payload, groups))
        return 0

    if args.analyze_only:
        if not out_path.exists():
            raise SystemExit(f"{out_path} 不存在；先跑全量")
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        rows = payload["rows"]
        an = payload.get("analysis") or analyse(rows, groups, {})
        print_summary(an, groups)
        return 0 if payload.get("self_check", {}).get("ok", True) else 4

    contracts = load_contracts()
    all_records = load_records(0)
    records = load_records(args.limit)

    if args.smoke:
        records = smoke_selection(records, 5)
        print(f"smoke: {len(records)} questions: {[r['id'] for r in records]}")

    checks = self_check(all_records, contracts)
    print(f"契约自检: {'PASS' if checks['ok'] else 'FAIL'}  {json.dumps(checks['checks'], ensure_ascii=False)}")
    print(f"规则平行: {json.dumps(checks['parity'], ensure_ascii=False)}")
    if not checks["ok"]:
        raise SystemExit("契约自检失败：E/F 组 prompt 没有逐字带上契约，或三组题面不一致")

    cache: dict = {}
    if CACHE_PATH.exists() and not args.force:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    client = Client(load_key(), cache, CACHE_PATH, force=args.force)

    rows, meta = run(records, groups, contracts, client, args.workers)
    cache = client.cache

    fail = failure_report(rows, groups)
    print(f"\n失败率: {json.dumps(fail, ensure_ascii=False)}")
    stop = False
    for g, f in fail.items():
        if (f["call_failure_rate"] or 0) > 0.05:
            print(f"[STOP] {g} 组调用失败率 {pct(f['call_failure_rate'])} > 5%", flush=True)
            stop = True
        if (f["invalid_judge_rate"] or 0) > 0.05:
            print(f"[STOP] {g} 组判分不可解析率 {pct(f['invalid_judge_rate'])} > 5%", flush=True)
            stop = True
        if (f["empty_answer_rate"] or 0) > 0.05:
            print(f"[STOP] {g} 组空回答率 {pct(f['empty_answer_rate'])} > 5%（max_tokens/thinking 预算问题）",
                  flush=True)
            stop = True

    analysis = analyse(rows, groups, cache)
    payload = {
        "generated_at": now_utc(),
        "experiment": "契约 v4 外部效度（LongMemEval 干净集 337 题 × 3 组 × 2 角色）",
        "design": {
            "groups": {g: {"label": GROUP_LABEL[g], "contract": CONTRACT_GROUP.get(g, None)} for g in groups},
            "scaffold_version": SCAFFOLD_VERSION,
            "preamble_en": PREAMBLE_EN,
            "reader": {"model": MODEL, "temperature": TEMPERATURE, "max_tokens": READER_MAX_TOKENS,
                       "thinking": f"{READER_THINKING}（任务书 1024 实测会被 thinking 吃成空回答，见 §1.3）",
                       "records": "该题全部 facts，按 (date, session_id, turn) 排序，逐条带 [date] (role)"},
            "judge": {"model": MODEL, "temperature": TEMPERATURE, "max_tokens": JUDGE_MAX_TOKENS,
                      "thinking": JUDGE_THINKING,
                      "prompt_same_across_groups": True, "prompt": JUDGE_PROMPT},
            "cache": {"path": rel(CACHE_PATH), "key_schema": "group|qid|role",
                      "note": "键含 group|qid|role；值里另存 prompt sha256，prompt 变了按 miss 处理"},
        },
        "self_check": checks,
        "data": {
            "path": rel(CLEAN),
            "sha256": sha256_file(CLEAN),
            "n_questions": len(all_records),
            "n_questions_run": len(rows),
            "by_category": {c: sum(1 for r in all_records if r["category"] == c)
                            for c in sorted({r["category"] for r in all_records})},
            "n_abstention": sum(1 for r in all_records if r["is_abstention"]),
            "n_facts_total": sum(r["n_facts"] for r in all_records),
        },
        "run": {
            **meta,
            "groups": list(groups),
            "workers": args.workers,
            "smoke": bool(args.smoke),
            "limit": args.limit,
            "failures": fail,
            "call_failure_total": len(client.failures),
            "failure_detail": client.failures[:20],
        },
        "analysis": analysis,
        "cost": cost_block(rows, cache, groups),
        "rows": rows,
    }
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out_path)
    print(f"\n写出 {rel(out_path)}  ({out_path.stat().st_size / 1024:.0f} KB)")
    print(f"缓存 {rel(CACHE_PATH)}：new_calls={meta['new_calls']} hits={meta['cache_hits']} "
          f"mismatch={meta['prompt_mismatch']}")
    cost = payload["cost"]
    print(f"tokens: prompt={cost['totals']['prompt_tokens']} completion={cost['totals']['completion_tokens']} "
          f"hit={cost['totals']['prompt_cache_hit_tokens']} miss={cost['totals']['prompt_cache_miss_tokens']}")
    print(f"cost: off-peak ${cost['cost_usd']['off_peak']}  peak-upper ${cost['cost_usd']['peak_upper_bound']}")

    print_summary(analysis, groups)

    if args.smoke:
        print("\n== 冒烟明细（格式检查） ==")
        for r in rows:
            print(f"\n[{r['qid']}] {r['category']} (n_facts={r['n_facts']})")
            print(f"  Q: {r['question'][:150]}")
            for g in groups:
                e = r["groups"][g]
                print(f"  {g}: {e.get('verdict')} state={e.get('answer_state')} "
                      f"trunc={e.get('answer_truncated')} chars={e.get('answer_chars')} "
                      f"reason={e.get('judge_reason')}")
                print(f"     A: {(e.get('answer') or '')[:180]}")

    if stop:
        print("\n[STOP] 有组失败率超过 5%，按约束停止并报告（结果已落盘，不据此下结论）", flush=True)
        return 3
    print("\nOK：0 异常" if not stop else "\n有异常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
