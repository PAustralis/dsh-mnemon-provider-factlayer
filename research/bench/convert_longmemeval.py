"""LongMemEval (cleaned) → 本仓库题库格式（JSONL）。

原始数据：``research/bench/raw/longmemeval/longmemeval_{oracle,s_cleaned,m_cleaned}.json``
（HF ``xiaowu0162/longmemeval-cleaned``，MIT，500 题/文件，逐个文件是 JSON 数组）。

设计要点（与 ``docs/10`` 的证据级/读者级口径对齐）：

- **每题独立语料**：LongMemEval 的 haystack 是 per-question 的，不能拼成大库。
- **事实 = 对话轮**：``{fact_id, text, date, date_kind, session_id, turn_index,
  has_answer, role}``，``text`` 只放对话原文（user/assistant 的 content），
  不改写、不把答案塞进去。
- **gold 证据 = has_answer:true 的轮**（轮级定位，不是会话级 id）。
  证据只可能落在 ``answer_session_ids`` 里（本次数据 896/896 成立，见 checks）。
- **abstention（``_abs``）**：21/30 题在整份文件里没有任何 ``has_answer:true`` 轮，
  这类题改用 ``answer_session_ids`` 的**全部轮**做证据，并标
  ``evidence_source="session_fallback"``；其余 9 题仍按轮级。这个回落会写进报告，
  不隐藏。
- **时间戳**：``haystack_dates`` 是会话发生时间（session_time），不是文本里提到的
  事件时间；事实层把 ``date`` 当 event_time 的代理，``date_kind`` 如实标记。

用法::

    python research/bench/convert_longmemeval.py --split oracle
    python research/bench/convert_longmemeval.py --split s --out research/bench/converted/local/longmemeval_s.jsonl
    python research/bench/convert_longmemeval.py --split all --manifest-only

产物：
- ``converted/longmemeval.jsonl``（默认 oracle 全量，入库）
- ``manifest.json``：逐条映射（kept/discarded + 原因 + fact 计数 + split 用途）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Iterator

BASE = Path(__file__).resolve().parent
REPO_ROOT = BASE.parent.parent
RAW_DIR = BASE / "raw" / "longmemeval"
CONVERTED_DIR = BASE / "converted"

# 入库策略：oracle 15.4MB（原始）+ 16.5MB（转换）入库；s/m 超过 50MB 只留哈希与样本。
COMMITTED = {"oracle": True, "s": False, "m": False}
DATASET = {
    "name": "LongMemEval (cleaned)",
    "hf_repo": "xiaowu0162/longmemeval-cleaned",
    "revision": "98d7416c24c778c2fee6e6f3006e7a073259d48f",
    "license": "MIT",
    "paper": "Wu et al., LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory, ICLR 2025, arXiv:2410.10813",
    "download": "python research/bench/fetch_raw.py --files oracle s m",
}

SPLITS = {
    "oracle": "longmemeval_oracle.json",
    "s": "longmemeval_s_cleaned.json",
    "m": "longmemeval_m_cleaned.json",
}

# question_type → 本项目 category
TYPE_TO_CATEGORY = {
    "temporal-reasoning": "temporal",
    "multi-session": "multi-session",
    "knowledge-update": "knowledge-update",
    "single-session-preference": "preference",
    "single-session-assistant": "assistant",
    "single-session-user": "other",
}

RE_DATE = re.compile(r"^(\d{4})/(\d{2})/(\d{2}) \(([A-Za-z]{3})\) (\d{2}):(\d{2})$")


# ---------------------------------------------------------------------------
# 流式读 JSON 数组：m 版 2.7GB，不能整文件 json.load
# ---------------------------------------------------------------------------
def iter_json_array(path: Path, chunk: int = 4 << 20) -> Iterator[dict]:
    dec = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as fh:
        buf = fh.read(chunk)
        start = buf.find("[")
        if start < 0:
            raise ValueError(f"{path} 不是 JSON 数组")
        buf = buf[start + 1:]
        while True:
            buf = buf.lstrip()
            while buf.startswith(","):
                buf = buf[1:].lstrip()
            if buf.startswith("]"):
                return
            while True:
                try:
                    obj, end = dec.raw_decode(buf)
                    break
                except json.JSONDecodeError:
                    more = fh.read(chunk)
                    if not more:
                        raise
                    buf += more
            yield obj
            buf = buf[end:]


def parse_ts(raw: str) -> tuple[str | None, str | None]:
    """'2023/04/10 (Mon) 17:50' → ('2023-04-10', '17:50')；解析不了返回 (None, None)。"""
    if not isinstance(raw, str):
        return None, None
    m = RE_DATE.match(raw.strip())
    if not m:
        return None, None
    y, mo, d, _wd, hh, mm = m.groups()
    return f"{y}-{mo}-{d}", f"{hh}:{mm}"


def _strip_wrap(text: str) -> str:
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'“”":
        t = t[1:-1].strip()
    return t


def derive_acceptable(answer: str) -> list[str]:
    """从 ``answer`` 保守派生可接受答案：原样 + 归一化变体 + 括号里的备选。

    只做机械可验证的改写（去引号、去句末句号、拆分 "X. Y is also acceptable"、
    括号里的 "or X" / "X is also acceptable"），不猜语义。
    """
    out: list[str] = []

    def add(s: str) -> None:
        s = s.strip()
        if s and s not in out:
            out.append(s)

    raw = _strip_wrap(answer)
    add(raw)
    add(_strip_wrap(raw.rstrip(".")))

    # "7 days. 8 days (including the last day) is also acceptable."
    alt = re.match(r"^(?P<a>.+?)\.?\s*(?P<b>.+?)\s+is also acceptable\.?$", raw, re.S)
    if alt:
        add(_strip_wrap(alt.group("a").rstrip(".")))
        b = _strip_wrap(alt.group("b").rstrip("."))
        add(b)
        add(re.sub(r"\s*\(.*?\)", "", b).strip())

    # "25 minutes and 50 seconds (or 25:50)" / "Alex (or Alex Smith)"
    for m in re.finditer(r"\((?:(?:or|a\.k\.a\.)\s*)?([^()]{1,80})\)", raw):
        inner = m.group(1).strip()
        if re.match(r"^(or|also|including|i\.e\.)", inner, re.I):
            inner = re.sub(r"^(or|also|including|i\.e\.)[\s:]*", "", inner, flags=re.I)
        if inner and not re.search(r"\b(is|was|the|that|which|because)\b", inner, re.I):
            add(inner)
    # "X, also known as Y" 不做，交给 judge

    # 数值答案（int 已转 str）补一种带单位/不带单位的写法
    if re.fullmatch(r"\d+", raw):
        pass
    return out


def normalise_turns(session: list, session_id: str, date: str, date_raw: str) -> list[dict]:
    facts: list[dict] = []
    for idx, turn in enumerate(session):
        if not isinstance(turn, dict):
            continue
        text = turn.get("content")
        if not isinstance(text, str) or not text.strip():
            continue
        facts.append(
            {
                "fact_id": f"{session_id}#t{idx}",
                "text": text,
                "date": date,
                "date_kind": "session_time",
                "date_raw": date_raw,
                "session_id": session_id,
                "turn_index": idx,
                "role": turn.get("role"),
                "has_answer": bool(turn.get("has_answer")),
            }
        )
    return facts


def convert_item(item: dict, split: str, seq: int) -> tuple[dict | None, dict]:
    """返回 (converted_record | None, manifest_entry)。"""
    qid = item.get("question_id")
    meta: dict = {
        "id": f"lme-{split}-{qid}",
        "source_item_id": qid,
        "split": split,
        "seq": seq,
        "status": "kept",
        "discard_reason": None,
    }
    missing = [
        k
        for k in (
            "question_id",
            "question",
            "answer",
            "question_type",
            "haystack_sessions",
            "haystack_session_ids",
            "haystack_dates",
            "answer_session_ids",
        )
        if k not in item
    ]
    if missing:
        meta.update(status="discarded", discard_reason="missing_field:" + ",".join(missing))
        return None, meta

    question = item["question"]
    answer = item["answer"]
    qtype = item["question_type"]
    sessions = item["haystack_sessions"]
    session_ids = item["haystack_session_ids"]
    session_dates = item["haystack_dates"]
    answer_sids = item["answer_session_ids"]

    if not isinstance(question, str) or not question.strip():
        meta.update(status="discarded", discard_reason="empty_question")
        return None, meta
    if answer is None or (isinstance(answer, str) and not answer.strip()):
        meta.update(status="discarded", discard_reason="empty_answer")
        return None, meta
    if not isinstance(sessions, list) or not sessions:
        meta.update(status="discarded", discard_reason="empty_haystack")
        return None, meta
    if not (len(sessions) == len(session_ids) == len(session_dates)):
        meta.update(status="discarded", discard_reason="haystack_length_mismatch")
        return None, meta
    if not isinstance(answer_sids, list) or not answer_sids:
        meta.update(status="discarded", discard_reason="empty_answer_session_ids")
        return None, meta

    is_abs = str(qid).endswith("_abs")
    category = "abstention" if is_abs else TYPE_TO_CATEGORY.get(qtype, "other")

    # 去重：同一 session_id 在 haystack_session_ids 里出现多次时只展开首次出现。
    # 否则 fact_id="{session_id}#t{idx}" 会在题内重复，破坏事实层主键（s split 实测 13 题）。
    # 重复会话都是干扰会话（0 个 answer session），去重不改变 gold，只减少 n_facts。
    first_by_sid: dict[str, object] = {}
    dedup_ids: list[str] = []
    dedup_sessions: list = []
    dedup_dates: list[str] = []
    duplicate_sessions: list[dict] = []
    duplicate_mismatch = 0
    for sid, session, date_raw in zip(session_ids, sessions, session_dates):
        if sid in first_by_sid:
            if first_by_sid[sid] != session:
                duplicate_mismatch += 1
            duplicate_sessions.append(
                {"session_id": sid, "turns": len(session) if isinstance(session, list) else 0}
            )
            continue
        first_by_sid[sid] = session
        dedup_ids.append(sid)
        dedup_sessions.append(session)
        dedup_dates.append(date_raw)

    facts: list[dict] = []
    bad_ts = 0
    for sid, session, date_raw in zip(dedup_ids, dedup_sessions, dedup_dates):
        date, _time = parse_ts(date_raw)
        if date is None:
            bad_ts += 1
            continue
        facts.extend(normalise_turns(session, sid, date, date_raw))
    if bad_ts:
        meta.update(status="discarded", discard_reason="bad_timestamp")
        meta["bad_timestamp_sessions"] = bad_ts
        return None, meta
    if not facts:
        meta.update(status="discarded", discard_reason="no_turns")
        return None, meta

    sid_set = set(dedup_ids)
    answer_sid_set = set(answer_sids)
    unknown = answer_sid_set - sid_set
    if unknown:
        meta.update(status="discarded", discard_reason="answer_session_not_in_haystack")
        meta["unknown_session_ids"] = sorted(unknown)[:5]
        return None, meta

    gold = [f["fact_id"] for f in facts if f["has_answer"] and f["session_id"] in answer_sid_set]
    evidence_source = "turn:has_answer"
    if not gold:
        if is_abs:
            gold = [f["fact_id"] for f in facts if f["session_id"] in answer_sid_set]
            evidence_source = "session_fallback"
        if not gold:
            meta.update(status="discarded", discard_reason="evidence_cannot_locate")
            return None, meta
    gold_set = set(gold)
    distractors = [f["fact_id"] for f in facts if f["fact_id"] not in gold_set]

    gold_answer = answer if isinstance(answer, str) else str(answer)
    converted = {
        "id": meta["id"],
        "source": "longmemeval",
        "source_item_id": qid,
        "split": split,
        "category": category,
        "source_question_type": qtype,
        "is_abstention": is_abs,
        "question": question,
        "question_date": item.get("question_date"),
        "gold_answer": gold_answer,
        "acceptable_answers": derive_acceptable(gold_answer),
        "facts": facts,
        "gold_evidence_fact_ids": gold,
        "distractor_fact_ids": distractors,
        "evidence_source": evidence_source,
        "n_facts": len(facts),
        "n_gold": len(gold),
        "n_distractor": len(distractors),
    }
    meta.update(
        category=category,
        source_question_type=qtype,
        is_abstention=is_abs,
        n_facts=len(facts),
        n_gold=len(gold),
        n_distractor=len(distractors),
        evidence_source=evidence_source,
        n_sessions=len(dedup_ids),
    )
    if duplicate_sessions:
        meta["duplicate_sessions_removed"] = len(duplicate_sessions)
        meta["duplicate_session_ids"] = [d["session_id"] for d in duplicate_sessions]
        meta["duplicate_session_turns_dropped"] = sum(d["turns"] for d in duplicate_sessions)
        if duplicate_mismatch:
            meta["duplicate_session_content_mismatches"] = duplicate_mismatch
    return converted, meta


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_split(split: str, out_path: Path | None, manifest_only: bool = False) -> dict:
    raw = RAW_DIR / SPLITS[split]
    if not raw.exists():
        raise SystemExit(f"缺少原始文件：{raw}（先跑 fetch_raw.py --files {split}）")
    out_path = (out_path or (CONVERTED_DIR / f"longmemeval{'' if split == 'oracle' else '_' + split}.jsonl")).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    kept: list[dict] = []
    entries: list[dict] = []
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    # 显式 LF：数据文件在 .gitattributes 里是 -text，换行必须由写入方固定，
    # 否则 Windows 默认换行会让 SHA256 随平台变化（哈希不可复算）。
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        seq = 0
        for item in iter_json_array(raw):
            seq += 1
            rec, meta = convert_item(item, split, seq)
            entries.append(meta)
            if rec is None:
                continue
            kept.append(rec)
            if not manifest_only:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    out_size = 0
    out_sha = None
    if manifest_only:
        tmp.unlink(missing_ok=True)
    else:
        tmp.replace(out_path)
        out_size = out_path.stat().st_size
        out_sha = sha256_file(out_path)

    by_cat: dict[str, int] = {}
    for rec in kept:
        by_cat[rec["category"]] = by_cat.get(rec["category"], 0) + 1
    reasons: dict[str, int] = {}
    for e in entries:
        if e["status"] != "kept":
            reasons[e["discard_reason"]] = reasons.get(e["discard_reason"], 0) + 1
    summary = {
        "split": split,
        "committed_to_repo": COMMITTED[split],
        "local_only": not COMMITTED[split],
        "raw_file": raw.name,
        "raw_bytes": raw.stat().st_size,
        "raw_sha256": sha256_file(raw),
        "converted_file": None if manifest_only else str(out_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "converted_bytes": out_size,
        "converted_sha256": out_sha,
        "total": seq,
        "kept": len(kept),
        "discarded": sum(1 for e in entries if e["status"] != "kept"),
        "discard_reasons": reasons,
        "kept_by_category": dict(sorted(by_cat.items())),
        "evidence_source_counts": _count(entries, "evidence_source"),
        "kept_by_evidence_source": _count(kept, "evidence_source"),
        "sum_facts": sum(r["n_facts"] for r in kept),
        "sum_gold": sum(r["n_gold"] for r in kept),
        "sum_distractor": sum(r["n_distractor"] for r in kept),
        "sum_sessions": sum(e.get("n_sessions", 0) for e in entries if e["status"] == "kept"),
        "duplicate_sessions_removed": sum(e.get("duplicate_sessions_removed", 0) for e in entries),
        "questions_with_duplicate_sessions": sum(
            1 for e in entries if e.get("duplicate_sessions_removed")
        ),
    }
    return {"summary": summary, "entries": entries, "kept": kept}


def _count(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="oracle", choices=["oracle", "s", "m", "all"])
    ap.add_argument("--out", type=Path, default=None, help="仅 --split 非 all 时有效")
    ap.add_argument("--manifest", type=Path, default=BASE / "manifest.json")
    ap.add_argument("--sample-dir", type=Path, default=BASE / "converted" / "samples")
    ap.add_argument("--sample-size", type=int, default=20)
    ap.add_argument("--no-manifest-write", action="store_true")
    args = ap.parse_args()

    splits = ["oracle", "s", "m"] if args.split == "all" else [args.split]
    old: dict = {}
    if args.manifest.exists():
        try:
            old = json.loads(args.manifest.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            old = {}
    manifest: dict = {
        **old,
        "generated_by": "research/bench/convert_longmemeval.py",
        "source": "LongMemEval cleaned (xiaowu0162/longmemeval-cleaned)",
        "dataset": DATASET,
        "splits": old.get("splits", {}),
        "records": old.get("records", {}),
    }
    # 转换产物一旦变化，旧 clean_set / leak_screening 的哈希就不再对应；
    # 这些字段由 check_bank.py 在筛查后重新写入，避免 manifest 里留下失效引用。
    for stale in ("clean_set", "leak_screening", "eval_exclusions"):
        manifest.pop(stale, None)

    for split in splits:
        out = args.out if args.split != "all" else None
        print(f"== convert {split} ==", flush=True)
        result = run_split(split, out)
        s = result["summary"]
        print(
            f"   total={s['total']} kept={s['kept']} discarded={s['discarded']} "
            f"facts={s['sum_facts']} gold={s['sum_gold']} out={s['converted_bytes']/1e6:.1f}MB"
            + (f" sha={s['converted_sha256'][:12]}" if s["converted_sha256"] else ""),
            flush=True,
        )
        if s["discard_reasons"]:
            print(f"   discard_reasons={s['discard_reasons']}", flush=True)
        if s["duplicate_sessions_removed"]:
            print(
                f"   dedupe: {s['duplicate_sessions_removed']} duplicate sessions in "
                f"{s['questions_with_duplicate_sessions']} questions removed",
                flush=True,
            )
        manifest["splits"][split] = s
        for e in result["entries"]:
            manifest["records"][e["id"]] = e
        # 大 split：把前 N 条转换结果留下做样本（原始数据不入库时仍可核对格式）
        if split in ("s", "m"):
            args.sample_dir.mkdir(parents=True, exist_ok=True)
            sample_path = args.sample_dir / f"longmemeval_{split}_first{args.sample_size}.jsonl"
            with sample_path.open("w", encoding="utf-8", newline="\n") as fh:
                for rec in result["kept"][: args.sample_size]:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            s["sample_file"] = str(sample_path.relative_to(REPO_ROOT)).replace("\\", "/")
            s["sample_size"] = min(args.sample_size, len(result["kept"]))
            print(f"   sample -> {s['sample_file']} ({s['sample_size']} 条)", flush=True)

    manifest["split_layers"] = {
        "oracle": "读者级（reader-level）：每题只含证据会话，测 reader 对证据的读取与冲突/弃权处理",
        "s": "证据级（evidence-level）：约 115k tokens/题（~40 会话），含干扰，测检索 R@k",
        "m": "证据级（evidence-level，大规模）：约 500 会话/题，测长上下文检索上限",
    }
    if not args.no_manifest_write:
        args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"manifest -> {args.manifest} ({len(manifest['records'])} records)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
