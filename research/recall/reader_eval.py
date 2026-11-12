# -*- coding: utf-8 -*-
"""读者级端到端指标：用真实检索的 top-4 喂 reader，再按既有三档判分口径打分。

这条链路回答的问题
------------------
证据级 R@4 只说明"该看的记录有没有排进前 4"；它不说明 reader 能不能用这 4 条
答对。本脚本把 top-4 原样喂给 deepseek-flash，让它在"只根据这些记忆"的前提下
回答，再用答案文件的三档标准（✅/⚠️/❌）自动判分。两者相减，就是
"检索到读者"这一段损耗。

检索
----
**不联网、不调 embedding**：直接读 ``.embedding_cache.json`` 里的 qwen 向量，
按产品 vector 单路的余弦 + stable argsort 取 top-4。脚本内置闸门：
40 题 R@4 必须复现 0.8125，否则退出（说明缓存/语料/映射不一致）。

Reader
------
模型 deepseek-flash，温度 0；prompt 沿用 ``research/phase2/run_ds.py`` 的
PREAMBLE（题库原话："根据这些记忆回答用户的问题"），记录格式与题库一致：
``[raw_date] text``。只喂 top-4，不喂 gold，不喂答案文件。

判分
----
复用 ``读者测试40-答案.md`` 的三档口径（``grades.py`` / ``grades_new.py`` 就是
按它落的表）：
  ✅ 正确；⚠️ 合格但非正确（指出冲突/无法确定/明确说没有）；
  ❌ 自信答错（含编造记忆里不存在的事实）。
C/G 类没有 ✅，只有 ⚠️；E 类"明确说没有"是 ✅；I 类"说没有"是 ❌；
D 类问"当时以为"，答后来事实是 ❌；J 类答倾向+指出无日期是 ✅，
自信给结论不提日期是 ❌。判分也走 deepseek-flash、温度 0，并缓存。

缓存
----
``research/recall/.llm_cache_reader.json`` / ``.llm_cache_reader_judge.json``，
键含 (qid, 记录哈希, prompt 版本, 模型)。重跑 0 调用；``--force`` 才重新计费。

用法::

    python research/recall/reader_eval.py --qids 1,2,7,10,33   # 5 题试水
    python research/recall/reader_eval.py                      # 全量 40 题
    python research/recall/reader_eval.py --show-prompts       # 只打印 prompt
    python research/recall/reader_eval.py --force              # 忽略缓存

产物：``research/recall/reader_eval.json``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
REPO_ROOT = BASE.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(BASE))

from evidence_labels import (  # noqa: E402
    DEFAULT_ANSWER_KEY,
    MODEL,
    _extract_json,
    _load_cache,
    _save_cache,
    _sha,
    _usage_sum,
    ask_llm,
    load_key,
    load_rankings,
    parse_answer_key,
    recall_at,
    setup_proxy,
)
from run_real_embedding import load_inputs  # noqa: E402

DEFAULT_LABELS = BASE / "evidence_labels.json"
DEFAULT_JSON = BASE / "reader_eval.json"
CACHE_READER = BASE / ".llm_cache_reader.json"
CACHE_JUDGE = BASE / ".llm_cache_reader_judge.json"

READER_PROMPT_VERSION = "reader-v1"
GRADER_PROMPT_VERSION = "reader-grade-v1"
EXPECTED_R4 = 0.8125
FINAL_K = 4

# 与 research/phase2/run_ds.py 的 PREAMBLE 逐字一致：题库的原始答题说明。
PREAMBLE = (
    "你是一个助手。下面是用户提问，附若干条从记忆库里检索出来的记录。"
    "每条记忆前面的方括号里是这条记录的记录时间。\n"
    "请根据这些记忆回答用户的问题。直接给出答案，不要调用工具。"
)

GRADER_CLASS_RULES = {
    "C": "C 类（晚但记错）：标准答案就是“无法确定”。没有 ✅；只要给出任何自信的"
         "具体答案（无论选旧的还是新的）一律 ❌；指出冲突/无法确定才是 ⚠️。",
    "G": "G 类（同日期冲突）：标准答案就是“无法确定”。没有 ✅；任何自信的具体"
         "答案一律 ❌；指出同日冲突/无法确定才是 ⚠️。",
    "E": "E 类（配额截断）：需要的记忆确实不在给出的记录里。明确说“记忆里没有/"
         "没记录/无法得知”记 ✅；编造具体数值、名称、日期记 ❌。",
    "I": "I 类（无关干扰）：答案就在记录里。说“记忆里没有”属于事实错误，记 ❌，"
         "不能按 ⚠️ 处理。",
    "D": "D 类（历史信念）：问的是“当时以为”。答后来查明的事实，即使正确也记 ❌。",
    "J": "J 类（无日期对照）：答出倾向（如 Pixel 9/深圳/ThinkPad）并指出记录无"
         "日期、先后不可考，记 ✅；直接说无法确定/需确认，记 ⚠️；自信给结论且"
         "完全不提日期缺失，记 ❌。",
    "H": "H 类（粗粒度日期）：年份/月份/上下半年判断错误即 ❌。",
    "A": "A 类（正常更正）：答出最新/正确的状态记 ✅；指出冲突记 ⚠️；自信答旧状态记 ❌。",
    "B": "B 类（补录旧事实）：答出现在的事实、并正确区分旧记录记 ✅；被旧记录带偏记 ❌。",
    "F": "F 类（近似重复）：记录说法一致，答出该结论记 ✅；说互相矛盾记 ⚠️；答错记 ❌。",
}


def question_records(
    rankings: dict[int, list[int]], corpus: list[dict], sid: int, k: int = FINAL_K
) -> list[dict]:
    return [
        {
            "doc": doc,
            "raw_date": corpus[doc].get("raw_date") or "日期不详",
            "text": corpus[doc]["text"],
            "scenario": corpus[doc].get("scenario"),
        }
        for doc in rankings[sid][:k]
    ]


def format_records(records: list[dict]) -> str:
    return "\n".join(f"[{row['raw_date']}] {row['text']}" for row in records)


def build_reader_prompt(question: str, records: list[dict]) -> str:
    return (
        PREAMBLE
        + "\n\n【用户问】\n"
        + question
        + "\n\n【检索到的记忆】\n"
        + format_records(records)
    )


def build_grader_prompt(
    sid: int,
    cls: str,
    question: str,
    records: list[dict],
    section,
    reader_answer: str,
) -> str:
    class_rule = GRADER_CLASS_RULES.get(cls, "按三档标准判分。")
    return (
        "你是严格的判分员。请按《读者测试40-答案.md》的三档标准，给下面这个"
        "回答定档。只判最终回答，不要替回答者补全。\n\n"
        "【三档标准】\n"
        "✅ 正确：答出标准答案/可接受答案，意思一致即可，不要求逐字相同。\n"
        "⚠️ 合格但非正确：指出记忆冲突、说明无法确定、或明确说记忆里没有这条。\n"
        "❌ 自信答错：给出明确结论但结论错误，且没有标注任何不确定性；"
        "或编造记忆里不存在的具体事实（日期、型号、名称、数字）。\n"
        "注意：回答里同时给出答案和不确定性时，若推荐的答案正确记 ✅；"
        "若推荐错误但指出了冲突，记 ⚠️，不算 ❌。\n\n"
        f"【类别与类别规则】\n{cls} 类。{class_rule}\n\n"
        f"【用户问】\n{question}\n\n"
        f"【读者看到的记忆（top-4，原样）】\n{format_records(records)}\n\n"
        f"【标准答案】\n{section.standard}\n"
        f"【可接受】\n{section.acceptable}\n"
        f"【不合格】\n{section.unacceptable}\n\n"
        f"【读者回答】\n{reader_answer}\n\n"
        "只输出 JSON，不要 markdown 代码块：\n"
        '{"verdict":"✅|⚠️|❌",'
        '"answer_state":"answered|abstained|conflict_flagged|fabricated",'
        '"fabricated":true|false,'
        '"reason":"一句话，指出判分依据"}\n'
        "answer_state 的含义：answered=给出了答案；abstained=说记忆里没有/无法得知；"
        "conflict_flagged=指出记录冲突/无法确定，但没给结论；"
        "fabricated=编造了记录里不存在的具体事实。"
    )


def norm_verdict(text: str) -> str:
    text = (text or "").strip()
    if "✅" in text or text in {"正确", "对", "correct"}:
        return "✅"
    if "⚠" in text or text in {"合格", "部分正确", "uncertain"}:
        return "⚠️"
    if "❌" in text or text in {"错误", "错", "wrong", "incorrect"}:
        return "❌"
    return "?"


def norm_state(text: str, verdict: str, cls: str, fabricated: bool) -> str:
    state = (text or "").strip().lower()
    if state in {"answered", "abstained", "conflict_flagged", "fabricated"}:
        return state
    if fabricated:
        return "fabricated"
    if verdict == "✅":
        return "answered"
    if cls in "CGJ":
        return "conflict_flagged"
    if cls == "E":
        return "abstained"
    return "abstained"


def novel_numbers(answer: str, records: list[dict], question: str) -> list[str]:
    """答案里出现、但问题与 top-4 记录里都没有的数字。

    只作为 E 类"是否编造"的机械交叉核对：judge 说没编、但答案里冒出
    "尿酸 420" 这种数字时，这里会把它列出来给人看。不改变 judge 判定。
    """
    allowed = set(
        re.findall(r"\d+(?:\.\d+)?", question + " " + " ".join(
            f"{row['text']} {row['raw_date']}" for row in records
        ))
    )
    found = re.findall(r"\d+(?:\.\d+)?", answer or "")
    return sorted({num for num in found if num not in allowed and len(num) >= 2})


def _cache_usage(cache: dict) -> dict:
    """缓存里所有条目累计的 token（含历史运行；用于报告总成本）。"""
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for entry in cache.values():
        usage = entry.get("usage") or {}
        for key in total:
            total[key] += int(usage.get(key) or 0)
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument("--answer-key", default=DEFAULT_ANSWER_KEY)
    parser.add_argument("--json", default=str(DEFAULT_JSON))
    parser.add_argument("--qids", default="", help="逗号分隔的题号；默认全部")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题")
    parser.add_argument("--force", action="store_true", help="忽略缓存重新调用")
    parser.add_argument("--show-prompts", action="store_true", help="只打印 prompt，不调用")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args(argv)

    answer_path = Path(args.answer_key)
    sections = parse_answer_key(answer_path)
    corpus, queries, _scenarios = load_inputs()
    rankings, cache_meta = load_rankings(corpus, queries)

    # 检索闸门：必须复现产品口径 R@4=0.8125
    gold_r4 = float(
        np.mean(
            [recall_at(rankings[item["sid"]], set(item["gold"]), FINAL_K) for item in queries]
        )
    )
    print(
        f"检索：语料 {len(corpus)} 条 / 题 {len(queries)} 道；"
        f"缓存 {cache_meta['cache_hits']} 条（0 次 API）；R@4={gold_r4:.4f}"
    )
    if abs(gold_r4 - EXPECTED_R4) > 5e-4:
        raise SystemExit(
            f"检索闸门失败：R@4={gold_r4:.4f} != {EXPECTED_R4}；"
            "缓存/语料/映射与 rank_metrics 不一致，停止"
        )

    labels = {}
    if Path(args.labels).exists():
        payload = json.loads(Path(args.labels).read_text(encoding="utf-8"))
        labels = {
            row["sid"]: {
                "strict": set(row["strict"]["docs"]),
                "judge": set(row["judge"]["docs"]) if row.get("judge") else set(),
                "union": set(row["union_docs"]),
                "cls": row["cls"],
                "unanswerable": row.get("unanswerable", False),
            }
            for row in payload.get("per_question", [])
        }
        print(f"证据标注：{Path(args.labels).name}（{len(labels)} 题）")
    else:
        print(f"! 证据标注不存在：{args.labels}；只做读者指标，不做证据覆盖分解")

    selected = queries
    if args.qids:
        wanted = {int(x) for x in args.qids.split(",") if x.strip()}
        selected = [item for item in queries if item["sid"] in wanted]
    if args.limit:
        selected = selected[: args.limit]
    print(f"本次题数：{len(selected)}")

    setup_proxy()
    key = load_key()
    reader_cache = _load_cache(CACHE_READER)
    judge_cache = _load_cache(CACHE_JUDGE)
    reader_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    judge_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    reader_calls = judge_calls = 0

    def one(item: dict) -> dict:
        nonlocal reader_usage, judge_usage, reader_calls, judge_calls
        sid = item["sid"]
        section = sections[sid]
        records = question_records(rankings, corpus, sid)
        reader_prompt = build_reader_prompt(item["q"], records)
        records_hash = _sha(
            MODEL, READER_PROMPT_VERSION, str(sid), item["q"],
            json.dumps(records, ensure_ascii=False, sort_keys=True),
        )
        if not args.force and records_hash in reader_cache:
            reader_answer = reader_cache[records_hash]["answer"]
            reader_meta = {"cached": True, "usage": {}}
        else:
            reader_answer, usage = ask_llm(key, reader_prompt, max_tokens=3000)
            reader_calls += 1
            reader_usage = _usage_sum([reader_usage, usage])
            reader_cache[records_hash] = {
                "sid": sid,
                "answer": reader_answer,
                "usage": usage,
                "model": MODEL,
                "prompt_version": READER_PROMPT_VERSION,
                "records": records,
            }
            reader_meta = {"cached": False, "usage": usage}

        grader_prompt = build_grader_prompt(
            sid, section.cls, item["q"], records, section, reader_answer
        )
        grade_hash = _sha(
            MODEL, GRADER_PROMPT_VERSION, str(sid), reader_answer,
            json.dumps(records, ensure_ascii=False, sort_keys=True),
        )
        if not args.force and grade_hash in judge_cache:
            grade = judge_cache[grade_hash]
            grade_meta = {"cached": True, "usage": {}}
        else:
            raw, usage = ask_llm(key, grader_prompt, max_tokens=1800)
            judge_calls += 1
            judge_usage = _usage_sum([judge_usage, usage])
            data = _extract_json(raw) or {}
            fabricated = bool(data.get("fabricated"))
            verdict = norm_verdict(str(data.get("verdict", "")))
            state = norm_state(
                str(data.get("answer_state", "")), verdict, section.cls, fabricated
            )
            grade = {
                "sid": sid,
                "verdict": verdict,
                "answer_state": state,
                "fabricated": fabricated,
                "reason": str(data.get("reason") or "")[:500],
                "raw": raw[:1000],
                "usage": usage,
                "model": MODEL,
                "prompt_version": GRADER_PROMPT_VERSION,
            }
            judge_cache[grade_hash] = grade
            grade_meta = {"cached": False, "usage": usage}

        return {
            "sid": sid,
            "cls": section.cls,
            "cls_name": section.cls_name,
            "question": item["q"],
            "gold": item["gold"],
            "top4": records,
            "reader_answer": reader_answer,
            "reader_meta": reader_meta,
            "grade": grade,
            "grade_meta": grade_meta,
            "novel_numbers": novel_numbers(reader_answer, records, item["q"]),
        }

    if args.show_prompts:
        for item in selected:
            sid = item["sid"]
            records = question_records(rankings, corpus, sid)
            print("=" * 70)
            print(build_reader_prompt(item["q"], records))
            print("-" * 70)
            print(
                build_grader_prompt(
                    sid, sections[sid].cls, item["q"], records,
                    sections[sid], "<reader answer>",
                )
            )
        return 0

    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for row in pool.map(one, selected):
            results.append(row)
            # 每条落一次缓存：中途失败也能续跑，不重复计费
            _save_cache(CACHE_READER, reader_cache)
            _save_cache(CACHE_JUDGE, judge_cache)
            print(
                f"  scn{row['sid']:02d} {row['cls']} "
                f"{row['grade']['verdict']} {row['grade']['answer_state']:<16} "
                f"{row['reader_answer'][:60]!r}"
            )

    # 缓存落盘（每条都写一次，断点续跑不丢）
    _save_cache(CACHE_READER, reader_cache)
    _save_cache(CACHE_JUDGE, judge_cache)

    # ---------------- 汇总 ----------------
    total = len(results)
    verdicts = Counter(row["grade"]["verdict"] for row in results)
    correct = verdicts.get("✅", 0)
    passable = correct + verdicts.get("⚠️", 0)
    by_class = defaultdict(lambda: Counter())
    for row in results:
        by_class[row["cls"]][row["grade"]["verdict"]] += 1

    unanswerable = [
        row for row in results
        if labels.get(row["sid"], {}).get("unanswerable") or row["cls"] == "E"
    ]
    unanswerable_sids = {row["sid"] for row in unanswerable}
    answerable = [row for row in results if row["sid"] not in unanswerable_sids]
    fabricated_unanswerable = [
        row for row in unanswerable
        if row["grade"].get("fabricated") or row["grade"]["answer_state"] == "fabricated"
    ]
    abstained_unanswerable = [
        row for row in unanswerable
        if row["grade"]["answer_state"] == "abstained"
    ]
    answered_unanswerable = [
        row for row in unanswerable if row["grade"]["answer_state"] == "answered"
    ]
    abstained_answerable = [
        row for row in answerable
        if row["grade"]["answer_state"] == "abstained" and row["cls"] not in "CGJ"
    ]
    wrong_answerable = [
        row for row in answerable if row["grade"]["verdict"] == "❌"
    ]

    # 证据覆盖分解：只看有非空证据的题（E 类没有证据）
    coverage_rows = []
    for row in answerable:
        lab = labels.get(row["sid"])
        if not lab or not lab["union"]:
            continue
        top4 = {item["doc"] for item in row["top4"]}
        evidence = lab["union"]
        hit = top4 & evidence
        coverage_rows.append(
            {
                "sid": row["sid"],
                "cls": row["cls"],
                "evidence_n": len(evidence),
                "evidence_in_top4": len(hit),
                "evidence_complete": evidence <= top4,
                "evidence_recall": len(hit) / len(evidence),
                "verdict": row["grade"]["verdict"],
            }
        )
    complete_correct = [
        x for x in coverage_rows if x["evidence_complete"] and x["verdict"] == "✅"
    ]
    complete_not_correct = [
        x for x in coverage_rows if x["evidence_complete"] and x["verdict"] != "✅"
    ]
    incomplete_correct = [
        x for x in coverage_rows if not x["evidence_complete"] and x["verdict"] == "✅"
    ]
    incomplete_not_correct = [
        x for x in coverage_rows if not x["evidence_complete"] and x["verdict"] != "✅"
    ]

    summary = {
        "n": total,
        "verdicts": {"✅": correct, "⚠️": verdicts.get("⚠️", 0), "❌": verdicts.get("❌", 0)},
        "correct_rate": correct / total if total else None,
        "pass_rate": passable / total if total else None,
        "by_class": {
            cls: {
                "n": sum(counts.values()),
                "✅": counts.get("✅", 0),
                "⚠️": counts.get("⚠️", 0),
                "❌": counts.get("❌", 0),
            }
            for cls, counts in sorted(by_class.items())
        },
        "unanswerable": {
            "n": len(unanswerable),
            "sids": [row["sid"] for row in unanswerable],
            "abstained_total": len(abstained_unanswerable),
            "abstained_correct": sum(
                1 for row in abstained_unanswerable if row["grade"]["verdict"] == "✅"
            ),
            "fabricated": len(fabricated_unanswerable),
            "fabricated_sids": [row["sid"] for row in fabricated_unanswerable],
            "novel_number_sids": [
                row["sid"] for row in unanswerable if row.get("novel_numbers")
            ],
            "novel_numbers": {
                row["sid"]: row["novel_numbers"]
                for row in unanswerable if row.get("novel_numbers")
            },
            "answered_without_fabrication_flag": len(answered_unanswerable),
            "note": "E 类 / 证据标注判为无解的题；单独统计读者是否编造",
        },
        "answerable": {
            "n": len(answerable),
            "wrong_sids": [row["sid"] for row in wrong_answerable],
            "abstained_sids": [row["sid"] for row in abstained_answerable],
            "note": "该答却没答 = abstained_sids（C/G/J 的“无法确定”是合格行为，不计）",
        },
        "evidence_coverage": {
            "n_with_evidence": len(coverage_rows),
            "evidence_recall_top4_macro": float(
                np.mean([x["evidence_recall"] for x in coverage_rows])
            )
            if coverage_rows
            else None,
            "complete_top4": sum(1 for x in coverage_rows if x["evidence_complete"]),
            "reader_correct_given_complete": len(complete_correct),
            "reader_correct_rate_given_complete": (
                len(complete_correct) / (len(complete_correct) + len(complete_not_correct))
                if (complete_correct or complete_not_correct)
                else None
            ),
            "reader_correct_given_incomplete": len(incomplete_correct),
            "reader_correct_rate_given_incomplete": (
                len(incomplete_correct)
                / (len(incomplete_correct) + len(incomplete_not_correct))
                if (incomplete_correct or incomplete_not_correct)
                else None
            ),
            "complete_not_correct_sids": [x["sid"] for x in complete_not_correct],
            "incomplete_not_correct_sids": [x["sid"] for x in incomplete_not_correct],
            "incomplete_correct_sids": [x["sid"] for x in incomplete_correct],
            "note": "complete=union 证据全部在 top-4；complete 但读者没答对=读者/判分问题，"
                    "incomplete 且没答对=检索问题",
        },
        "usage": {
            "reader": reader_usage,
            "judge": judge_usage,
            "reader_api_calls": reader_calls,
            "judge_api_calls": judge_calls,
            "reader_cache_total": _cache_usage(reader_cache),
            "judge_cache_total": _cache_usage(judge_cache),
            "reader_cache_entries": len(reader_cache),
            "judge_cache_entries": len(judge_cache),
            "note": "reader/judge 是本次运行的调用与 token（缓存命中为 0）；"
                    "reader_cache_total/judge_cache_total 是缓存里全部历史条目累计，"
                    "报告成本用后者；token 取自 API usage，未乘价格",
        },
    }

    # 证据级指标对照（从 evidence_labels.json 读）
    reference = None
    if Path(args.labels).exists():
        payload = json.loads(Path(args.labels).read_text(encoding="utf-8"))
        metrics = payload.get("metrics") or {}
        reference = {
            "strict_r4": (metrics.get("strict") or {}).get("r4_macro"),
            "judge_r4": (metrics.get("judge") or {}).get("r4_macro"),
            "union_r4": (metrics.get("union") or {}).get("r4_macro"),
            "strict_r20": (metrics.get("strict") or {}).get("r20_macro"),
            "union_r20": (metrics.get("union") or {}).get("r20_macro"),
        }

    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "retrieval": {
            "method": "cached qwen3.7-text-embedding 向量 + 余弦 + stable argsort，top-4",
            "cache": cache_meta,
            "r4_gold": gold_r4,
            "r4_expected": EXPECTED_R4,
            "gate_pass": abs(gold_r4 - EXPECTED_R4) <= 5e-4,
        },
        "reader": {
            "model": MODEL,
            "temperature": 0,
            "prompt_version": READER_PROMPT_VERSION,
            "preamble": PREAMBLE,
            "max_tokens": 3000,
        },
        "grader": {
            "model": MODEL,
            "temperature": 0,
            "prompt_version": GRADER_PROMPT_VERSION,
            "rubric": "读者测试40-答案.md 的三档标准 + 类别规则",
        },
        "evidence_reference": reference,
        "summary": summary,
        "per_question": results,
    }

    Path(args.json).write_text(
        json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # ---------------- stdout ----------------
    print("\n== 读者级（top-4 记忆 → deepseek-flash → 三档判分） ==")
    print(
        f"  正确 ✅ {correct}/{total} = {correct/total:.1%}；"
        f"合格 ✅+⚠️ {passable}/{total} = {passable/total:.1%}；"
        f"自信答错 ❌ {verdicts.get('❌', 0)}"
    )
    print("\n  按类别：")
    for cls, counts in sorted(by_class.items()):
        n = sum(counts.values())
        print(
            f"    {cls}: n={n} ✅{counts.get('✅',0)} ⚠️{counts.get('⚠️',0)} "
            f"❌{counts.get('❌',0)}"
        )
    print(
        f"\n  无解（E 类）：{len(unanswerable)} 题；明确说没有 {len(abstained_unanswerable)}"
        f"（其中判 ✅ {summary['unanswerable']['abstained_correct']}）；"
        f"编造 {len(fabricated_unanswerable)} "
        f"{[row['sid'] for row in fabricated_unanswerable]}"
    )
    print(
        f"  该答却没答：{len(abstained_answerable)} 题 "
        f"{[row['sid'] for row in abstained_answerable]}"
    )
    if coverage_rows:
        print(
            f"\n  证据覆盖：top-4 含全部证据 {summary['evidence_coverage']['complete_top4']}"
            f"/{len(coverage_rows)} 题；证据召回"
            f"{summary['evidence_coverage']['evidence_recall_top4_macro']:.4f}"
        )
        print(
            f"    证据全在 top-4 且答对 {len(complete_correct)}；"
            f"全在但没答对 {len(complete_not_correct)} "
            f"{[x['sid'] for x in complete_not_correct]}"
        )
        print(
            f"    证据不全但答对 {len(incomplete_correct)} "
            f"{[x['sid'] for x in incomplete_correct]}；"
            f"证据不全且没答对 {len(incomplete_not_correct)} "
            f"{[x['sid'] for x in incomplete_not_correct]}"
        )
    if reference:
        def _fmt(value):
            return f"{value:.4f}" if isinstance(value, (int, float)) else "n/a"

        print(
            f"\n  对照：证据级 R@4 strict={_fmt(reference['strict_r4'])} "
            f"judge={_fmt(reference['judge_r4'])} union={_fmt(reference['union_r4'])}；"
            f"读者正确率={correct/total:.4f}"
        )
    print(
        f"\n  花费：reader {reader_calls} 次 / {reader_usage['total_tokens']} tokens；"
        f"judge {judge_calls} 次 / {judge_usage['total_tokens']} tokens"
    )
    print(f"  JSON 写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(errors="replace")
    raise SystemExit(main())
