# -*- coding: utf-8 -*-
"""契约 v2 对读者级的增益：A（题库原 preamble）/ B（+契约）/ C（+契约+日期强调）三组对照。

要回答的问题
------------
``docs/10`` 已经把差距定位到读者：证据 R@4 = 0.9583（union）、34/36 题证据全在
top-4，但读者可答题正确率只有 22/36 = 61.1%；歧义组 C/G/J 的 12 题证据 12/12
全在 top-4，却只对 1 题。那一轮 reader 用的是**题库原 preamble**，没有告诉它
"冲突要列出来问我、没有就说没有"。本脚本注入一段**契约 v2**（见下），量一下
读者级差距能被补上多少。

三组只差 prompt，检索、题集、判分口径、模型全部相同
------------------------------------------------------
- A 组：题库原 preamble（``reader_eval.PREAMBLE``），记录格式 ``[raw_date] text``。
  逐字复现 ``docs/10`` 的 61.1%：直接迁移上一轮 reader/judge 缓存，本次 0 次调用，
  并在报告里给出"40/40 与 ``reader_eval.json`` 完全一致"的复现校验。
- B 组：preamble + 契约 v2 全文（逐字），记录格式同 A。
- C 组：preamble + 契约 v2，记录格式改成 ``[记录于 raw_date] text``（日期强调；
  ``raw_date`` 缺失时 ``reader_eval.question_records`` 已回落 ``日期不详``，这里
  同样回落，见 ``raw_date_stats``）。

检索（不联网、不调 embedding）
------------------------------
复用 ``evidence_labels.load_rankings``：读本地 ``.embedding_cache.json`` 的 qwen
向量，余弦 + stable argsort 取 top-4，闸门 R@4 必须 = 0.8125。

**语料来源的坑（必须写在报告里）**：``run_real_embedding.CORPUS`` 指向的
``research/phase2/corpus.json`` 在本机已不存在（老 checkout 被删）。本脚本因此支持
两条路：``--corpus`` 指到真实 corpus.json 时直接用；否则从
``tests/fixtures/recall_baseline.json``（119 条规范化语料，顺序 = corpus 下标）+
题库 JSON 的 ``[date]`` 重建 119 条语料，并做三重校验：R@4 = 0.8125、
40 题 top-4 与上一轮 ``reader_eval.json`` 逐题相同、160 个记录槽的文本/日期
逐字相同。任一校验不过直接退出。

判分与缓存
------------
判分复用 ``reader_eval.GRADER_CLASS_RULES`` + 三档口径（✅/⚠️/❌），deepseek-flash、
温度 0，先跑 5 题（1,2,7,10,33）确认 JSON 格式，再跑全量 40 题。所有结果写进
``.llm_cache_reader_contract.json``（单文件，``reader``/``judge`` 两段，已 gitignore）；
重跑 0 调用，``--force`` 才重新计费。A 组的缓存键与 ``reader_eval.py`` 完全一致，
所以迁移的是"上一轮真实结果"，不是重算。

报告要回答的四个问题都在 ``--json`` 产物里
------------------------------------------
1. 三组总体 ✅/⚠️/❌、可答题正确率，以及 C/G/J（12 题）与 A/B/D/F/H/I（24 题）
   的分组对比；
2. B 相对 A 的增益与**逐题配对**（变好/变差）；
3. E 类（库里无答案）三组"说没有"的比例、编造与机械 novel_numbers 核对；
4. 成本：三组各自的生成/判分 token，以及缓存命中后复跑 0 调用。

另附**契约执行度**机械核对（是否列日期、是否指出两种说法、是否反问、是否弃权），
用来把 C/G/J 的剩余失败分成"契约没被执行"与"证据本身有歧义、只能到 ⚠️"。

用法::

    python research/recall/reader_contract_eval.py --qids 1,2,7,10,33 --groups B,C
    python research/recall/reader_contract_eval.py                      # 全量 40 题 × 3 组
    python research/recall/reader_contract_eval.py --groups B --show-prompts
    python research/recall/reader_contract_eval.py --force              # 忽略缓存（计费）

产物：``research/recall/reader_contract_eval.json``。
"""

from __future__ import annotations

import argparse
import json
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
from reader_eval import (  # noqa: E402
    EXPECTED_R4,
    FINAL_K,
    GRADER_CLASS_RULES,
    GRADER_PROMPT_VERSION,
    PREAMBLE,
    READER_PROMPT_VERSION,
    build_grader_prompt,
    build_reader_prompt,
    format_records,
    norm_state,
    norm_verdict,
    novel_numbers,
    question_records,
)
from run_real_embedding import DEFAULT_CORPUS, DEFAULT_QBANK  # noqa: E402
from recall_eval import norm, parse_qbank  # noqa: E402

DEFAULT_LABELS = BASE / "evidence_labels.json"
DEFAULT_JSON = BASE / "reader_contract_eval.json"
DEFAULT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "recall_baseline.json"
PREV_JSON = BASE / "reader_eval.json"
CACHE = BASE / ".llm_cache_reader_contract.json"
PREV_CACHE_READER = BASE / ".llm_cache_reader.json"
PREV_CACHE_JUDGE = BASE / ".llm_cache_reader_judge.json"

# 契约 v2 原文（逐字使用，缩进与换行都不改）。
CONTRACT_V2 = (
    "回答这些关于我本人记忆的问题时，请遵守：\n"
    "1. 直接给出答案；如果答案来自记忆，说明依据的是哪一天记的那条。\n"
    "2. 如果多条记录对同一件事给了不同的说法，把每一种说法和它对应的日期都列出来，\n"
    "   并说明无法确定哪一条反映我当前的真实情况，然后问我哪一个是对的。\n"
    "   但有一种例外：如果其中某条记录是「已确认」「已作废」这种由我本人裁决过的结论，\n"
    "   说明这件事我已经拍板了，以那条为准直接回答，不要再把旧说法摆出来问我。\n"
    "3. 如果记忆里根本没有答案，直接说没有，不要编。\n"
    "4. 如果所有记录说法一致，直接回答，不要把同一件事换个说法重复一遍。\n"
)

GROUPS = ("A", "B", "C")
GROUP_LABEL = {
    "A": "基线：题库原 preamble",
    "B": "契约 v2",
    "C": "契约 v2 + 日期强调（[记录于 YYYY-MM-DD]）",
}
READER_VERSIONS = {
    "A": READER_PROMPT_VERSION,          # reader-v1：与 reader_eval 同键，可迁移
    "B": "reader-contract-v2",
    "C": "reader-contract-v2-date",
}
GRADER_VERSIONS = {
    "A": GRADER_PROMPT_VERSION,          # reader-grade-v1：同上
    "B": "reader-grade-contract-v2",
    "C": "reader-grade-contract-v2-date",
}

# 报告口径：答案文件里的类别分组
AMBIGUOUS_CLASSES = "CGJ"        # 歧义/无日期：标准答案多为"无法确定"
FACT_CLASSES = "ABDFHI"          # 单点事实
UNANSWERABLE_CLASS = "E"         # 全语料无答案

VERDICT_SCORE = {"✅": 2, "⚠️": 1, "❌": 0, "?": -1}

# 契约执行度的机械核对（只用于把失败分类，不参与判分）
RE_DATE_CN = re.compile(r"\d{4}-\d{2}-\d{2}|\d{4}-\d{2}|\d{4}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*日")
RE_CONFLICT = re.compile(
    r"冲突|矛盾|不一致|不同说法|说法不同|两种说法|前后不一|互相矛盾|"
    r"无法确定|无法判断|不能确定|难以确定|无法确认"
)
RE_ASK = re.compile(
    r"哪一(?:个|条|种|种说法|条记录)|请你?确认|你(?:能|可以)?告诉我|需要你|"
    r"请问你|你希望我|你是指|以哪(?:条|个)为准"
)
RE_ABSENT = re.compile(
    r"没有(?:记录|提到|相关|具体|明确|这条|任何)|未(?:记录|提及)|查无|无记录|"
    r"无法得知|记忆里没有|记忆中没有|记忆中没有相关"
)


# ======================================================================
# 一、语料：真实 corpus.json 优先，否则用 fixture + 题库重建（带三重校验）
# ======================================================================
def reconstruct_corpus(qbank: str, fixture: str) -> tuple[list[dict], dict]:
    """从 fixture 的 119 条语料文本 + 题库的 [date] 重建 corpus 行。

    fixture（``tests/fixtures/recall_baseline.json``）的 ``corpus`` 是
    ``recall_eval.norm`` 之后的文本、**顺序与 corpus.json 下标一致**；题库给出每题
    的 ``[date] text``。二者按规范化文本一一对应，因此能还原 ``text``/``raw_date``/
    ``scenario`` 三个后面要用的字段。任何一条对不上或多解都直接退出。
    """
    fixture_payload = json.loads(Path(fixture).read_text(encoding="utf-8"))
    texts = fixture_payload.get("corpus") or []
    scenarios = parse_qbank(qbank)

    keyed: dict[str, tuple[int, str, str]] = {}
    duplicates: list[str] = []
    for scenario in scenarios:
        for mem in scenario["mems"]:
            key = norm(mem["text"])
            if key in keyed:
                duplicates.append(f"{keyed[key][0]}/{scenario['sid']}:{mem['text'][:24]}")
                continue
            keyed[key] = (
                scenario["sid"],
                (mem.get("date_label") or "日期不详"),
                mem["text"],
            )

    corpus: list[dict] = []
    missing: list[int] = []
    for index, text in enumerate(texts):
        hit = keyed.get(norm(text))
        if hit is None:
            missing.append(index)
            continue
        # text 用题库原文（非 fixture 的规范化文本），与 corpus.json 逐字一致
        corpus.append(
            {"text": hit[2], "raw_date": hit[1], "scenario": hit[0]}
        )
    if missing or len(corpus) != len(texts):
        raise SystemExit(
            f"语料重建失败：fixture {len(texts)} 条，匹配 {len(corpus)} 条；"
            f"缺 {missing[:5]}"
        )
    if duplicates:
        raise SystemExit(f"题库记录重复（无法一一映射）：{duplicates[:3]}")

    audit = {
        "source": "reconstructed",
        "why": "research/phase2/corpus.json（旧 checkout）在本机不存在；"
               "从 fixture 语料顺序 + 题库 [date] 重建，并做三重校验",
        "fixture": str(fixture),
        "fixture_n_docs": len(texts),
        "qbank": str(qbank),
        "qbank_scenarios": len(scenarios),
        "qbank_mems": sum(len(s["mems"]) for s in scenarios),
        "docs_matched": len(corpus),
        "docs_missing": missing,
        "duplicate_texts": duplicates,
        "text_source": "qbank 原文（fixture 只用来定下标顺序）",
        "raw_date_stats": dict(
            Counter(
                "日期不详" if row["raw_date"] == "日期不详" else "有日期"
                for row in corpus
            )
        ),
    }
    return corpus, audit


def load_corpus(corpus_path: str, qbank: str, fixture: str) -> tuple[list[dict], dict]:
    path = Path(corpus_path) if corpus_path else None
    if path and path.exists():
        corpus = json.loads(path.read_text(encoding="utf-8"))
        return corpus, {
            "source": str(path),
            "docs": len(corpus),
            "raw_date_stats": dict(
                Counter(
                    "日期不详" if not row.get("raw_date") or row.get("raw_date") == "日期不详"
                    else "有日期"
                    for row in corpus
                )
            ),
        }
    return reconstruct_corpus(qbank, fixture)


def build_queries(corpus: list[dict], scenarios: list[dict]) -> tuple[list[dict], list]:
    """与 ``run_real_embedding.load_inputs`` 同口径：题库记忆 → corpus 下标。"""
    by_norm: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(corpus):
        by_norm[norm(row["text"])].append(index)
    queries, unmatched = [], []
    for scenario in scenarios:
        gold: set[int] = set()
        for mem in scenario["mems"]:
            candidates = by_norm.get(norm(mem["text"]))
            if not candidates:
                unmatched.append((scenario["sid"], mem["text"]))
                continue
            gold.add(
                min(
                    candidates,
                    key=lambda i: (corpus[i].get("scenario") != scenario["sid"], i),
                )
            )
        queries.append(
            {
                "sid": scenario["sid"],
                "q": norm(scenario["question"]),
                "gold": sorted(gold),
                "mem_n": len(scenario["mems"]),
            }
        )
    return queries, unmatched


# ======================================================================
# 二、三组 prompt
# ======================================================================
def render_records(group: str, records: list[dict]) -> str:
    """记录渲染：A/B 与题库一致 ``[date] text``；C 强调日期 ``[记录于 date] text``。"""
    if group == "C":
        return "\n".join(
            f"[记录于 {row['raw_date']}] {row['text']}" for row in records
        )
    return format_records(records)


def build_reader_prompt_group(group: str, question: str, records: list[dict]) -> str:
    if group == "A":
        # 与 reader_eval 逐字相同，保证 A 组缓存键可迁移
        return build_reader_prompt(question, records)
    return (
        PREAMBLE
        + "\n\n"
        + CONTRACT_V2
        + "\n\n【用户问】\n"
        + question
        + "\n\n【检索到的记忆】\n"
        + render_records(group, records)
    )


def build_grader_prompt_group(
    group: str, sid: int, cls: str, question: str, records: list[dict], section, answer: str
) -> str:
    """判分 prompt：与 reader_eval 逐字相同，只把"读者看到的记忆"换成该组渲染方式。

    A/B 组用 ``reader_eval.build_grader_prompt``（记录格式本来就一样），C 组需要
    ``[记录于 date]``，所以这里保留一份模板；启动时会自动断言 A/B 与 reader_eval
    的输出逐字相同（见 ``verify_prompt_templates``）。
    """
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
        f"【读者看到的记忆（top-4，原样）】\n{render_records(group, records)}\n\n"
        f"【标准答案】\n{section.standard}\n"
        f"【可接受】\n{section.acceptable}\n"
        f"【不合格】\n{section.unacceptable}\n\n"
        f"【读者回答】\n{answer}\n\n"
        "只输出 JSON，不要 markdown 代码块：\n"
        '{"verdict":"✅|⚠️|❌",'
        '"answer_state":"answered|abstained|conflict_flagged|fabricated",'
        '"fabricated":true|false,'
        '"reason":"一句话，指出判分依据"}\n'
        "answer_state 的含义：answered=给出了答案；abstained=说记忆里没有/无法得知；"
        "conflict_flagged=指出记录冲突/无法确定，但没给结论；"
        "fabricated=编造了记录里不存在的具体事实。"
    )


def verify_prompt_templates(sid, cls, question, records, section) -> dict:
    """A/B 的判分 prompt 必须与 reader_eval 逐字相同；C 只差记录块。"""
    a_text = build_grader_prompt_group("A", sid, cls, question, records, section, "<answer>")
    ref = build_grader_prompt(sid, cls, question, records, section, "<answer>")
    b_text = build_grader_prompt_group("B", sid, cls, question, records, section, "<answer>")
    c_text = build_grader_prompt_group("C", sid, cls, question, records, section, "<answer>")
    c_expected = ref.replace(format_records(records), render_records("C", records))
    return {
        "A_equals_reader_eval": a_text == ref,
        "B_equals_reader_eval": b_text == ref,
        "C_differs_only_in_record_block": c_text == c_expected,
        "A_reader_prompt_equals_reader_eval": build_reader_prompt_group(
            "A", question, records
        ) == build_reader_prompt(question, records),
    }


# ======================================================================
# 三、缓存（单文件两段）与键
# ======================================================================
def load_bundle() -> dict:
    payload = _load_cache(CACHE)
    return {
        "reader": payload.get("reader") or {},
        "judge": payload.get("judge") or {},
    }


def save_bundle(bundle: dict) -> None:
    _save_cache(
        CACHE,
        {
            "schema": 1,
            "reader": bundle["reader"],
            "judge": bundle["judge"],
        },
    )


def reader_key(group: str, sid: int, question: str, prompt: str, records: list[dict]) -> str:
    if group == "A":
        # 与 reader_eval.py 完全一致：可以命中上一轮缓存，A 是"真复现"而不是重算
        return _sha(
            MODEL,
            READER_VERSIONS[group],
            str(sid),
            question,
            json.dumps(records, ensure_ascii=False, sort_keys=True),
        )
    return _sha(MODEL, READER_VERSIONS[group], str(sid), question, prompt)


def grader_key(group: str, sid: int, answer: str, prompt: str, records: list[dict]) -> str:
    if group == "A":
        return _sha(
            MODEL,
            GRADER_VERSIONS[group],
            str(sid),
            answer,
            json.dumps(records, ensure_ascii=False, sort_keys=True),
        )
    return _sha(MODEL, GRADER_VERSIONS[group], str(sid), answer, prompt)


# ======================================================================
# 四、契约执行度（机械核对，不参与判分）
# ======================================================================
def contract_flags(answer: str) -> dict:
    text = answer or ""
    dates = sorted(set(RE_DATE_CN.findall(text)))
    return {
        "cites_date": bool(dates),
        "n_date_mentions": len(dates),
        "flags_conflict": bool(RE_CONFLICT.search(text)),
        "asks_back": bool(RE_ASK.search(text)),
        "says_absent": bool(RE_ABSENT.search(text)),
        # 契约第 2 条的可观察核心：把"两种说法"摆出来（≥2 个日期锚点 + 冲突措辞）
        "lists_both_sides": bool(dates) and len(dates) >= 2 and bool(RE_CONFLICT.search(text)),
        # C/G 的合格行为（⚠️）：指出冲突/无法确定；J 的 ✅ 还要求给出倾向
        "compliant_cg": bool(RE_CONFLICT.search(text)),
    }


def said_no(answer: str) -> bool:
    return bool(RE_ABSENT.search(answer or ""))


# ======================================================================
# 五、单组执行
# ======================================================================
def run_group(
    group: str,
    selected: list[dict],
    ctx: dict,
    *,
    force: bool,
    concurrency: int,
    verbose: bool,
) -> tuple[list[dict], dict]:
    bundle = ctx["bundle"]
    key = ctx["key"]
    sections = ctx["sections"]
    records_by_sid = ctx["records_by_sid"]
    calls = {"reader": 0, "judge": 0}
    usage = {"reader": _usage_sum([]), "judge": _usage_sum([])}
    rows: list[dict] = []

    def one(item: dict) -> dict:
        sid = item["sid"]
        section = sections[sid]
        records = records_by_sid[sid]
        prompt = build_reader_prompt_group(group, item["q"], records)
        rkey = reader_key(group, sid, item["q"], prompt, records)
        if not force and rkey in bundle["reader"]:
            entry = bundle["reader"][rkey]
            answer = entry["answer"]
            reader_meta = {
                "cached": True,
                "origin": entry.get("origin", "cache"),
                "usage": {},
                "api_calls": 0,
            }
        else:
            answer, rusage = ask_llm(key, prompt, max_tokens=3000)
            if answer.startswith("<ERROR>"):
                return {
                    "sid": sid,
                    "cls": section.cls,
                    "cls_name": section.cls_name,
                    "question": item["q"],
                    "error": answer[:300],
                    "reader_answer": answer,
                    "grade": {
                        "verdict": "?",
                        "answer_state": "error",
                        "fabricated": False,
                        "reason": "reader 调用失败，未缓存",
                    },
                    "reader_meta": {"cached": False, "origin": "error", "usage": {}},
                    "grade_meta": {"cached": False, "origin": "error", "usage": {}},
                    "contract": {},
                    "novel_numbers": [],
                }
            bundle["reader"][rkey] = {
                "group": group,
                "sid": sid,
                "answer": answer,
                "usage": rusage,
                "model": MODEL,
                "prompt_version": READER_VERSIONS[group],
                "records": records,
                "origin": "fresh",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            reader_meta = {"cached": False, "origin": "fresh", "usage": rusage, "api_calls": 1}

        gprompt = build_grader_prompt_group(
            group, sid, section.cls, item["q"], records, section, answer
        )
        gkey = grader_key(group, sid, answer, gprompt, records)
        if not force and gkey in bundle["judge"]:
            grade = bundle["judge"][gkey]
            judge_meta = {
                "cached": True,
                "origin": grade.get("origin", "cache"),
                "usage": {},
                "api_calls": 0,
            }
        else:
            raw, gusage = ask_llm(key, gprompt, max_tokens=1800)
            data = _extract_json(raw) or {}
            fabricated = bool(data.get("fabricated"))
            verdict = norm_verdict(str(data.get("verdict", "")))
            state = norm_state(
                str(data.get("answer_state", "")), verdict, section.cls, fabricated
            )
            grade = {
                "group": group,
                "sid": sid,
                "verdict": verdict,
                "answer_state": state,
                "fabricated": fabricated,
                "reason": str(data.get("reason") or "")[:500],
                "raw": raw[:1000],
                "usage": gusage,
                "model": MODEL,
                "prompt_version": GRADER_VERSIONS[group],
                "origin": "fresh",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            bundle["judge"][gkey] = grade
            judge_meta = {"cached": False, "origin": "fresh", "usage": gusage, "api_calls": 1}

        return {
            "sid": sid,
            "cls": section.cls,
            "cls_name": section.cls_name,
            "question": item["q"],
            "reader_answer": answer,
            "grade": grade,
            "reader_meta": reader_meta,
            "grade_meta": judge_meta,
            "contract": contract_flags(answer),
            "novel_numbers": novel_numbers(answer, records, item["q"]),
        }

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for row in pool.map(one, selected):
            rows.append(row)
            calls["reader"] += int(row["reader_meta"].get("api_calls") or 0)
            calls["judge"] += int(row["grade_meta"].get("api_calls") or 0)
            usage["reader"] = _usage_sum([usage["reader"], row["reader_meta"].get("usage") or {}])
            usage["judge"] = _usage_sum([usage["judge"], row["grade_meta"].get("usage") or {}])
            # 每条落一次缓存：中途失败可续跑，不重复计费
            save_bundle(bundle)
            if verbose:
                mark = row["grade"]["verdict"]
                note = "" if not row.get("error") else f" ! {row['error'][:60]}"
                print(
                    f"  [{group}] scn{row['sid']:02d} {row['cls']} {mark} "
                    f"{row['grade']['answer_state']:<16} "
                    f"{row['reader_answer'][:56]!r}{note}"
                )
    return rows, {
        "calls": calls,
        "usage": usage,
    }


# ======================================================================
# 六、汇总
# ======================================================================
def stats(rows: list[dict]) -> dict:
    ok = [row for row in rows if row["grade"]["verdict"] in VERDICT_SCORE]
    errors = [row for row in rows if row["grade"]["verdict"] == "?"]
    counts = Counter(row["grade"]["verdict"] for row in ok)
    n = len(ok)
    return {
        "n": n,
        "errors": len(errors),
        "error_sids": [row["sid"] for row in errors],
        "✅": counts.get("✅", 0),
        "⚠️": counts.get("⚠️", 0),
        "❌": counts.get("❌", 0),
        "correct_rate": counts.get("✅", 0) / n if n else None,
        "pass_rate": (counts.get("✅", 0) + counts.get("⚠️", 0)) / n if n else None,
    }


def split_stats(rows: list[dict], predicate) -> dict:
    return stats([row for row in rows if predicate(row)])


def by_class(rows: list[dict]) -> dict:
    out: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        out[row["cls"]][row["grade"]["verdict"]] += 1
    return {
        cls: {
            "n": sum(c.values()),
            "✅": c.get("✅", 0),
            "⚠️": c.get("⚠️", 0),
            "❌": c.get("❌", 0),
        }
        for cls, c in sorted(out.items())
    }


def paired_compare(rows_a: list[dict], rows_b: list[dict], name: str) -> dict:
    by_a = {row["sid"]: row for row in rows_a}
    by_b = {row["sid"]: row for row in rows_b}
    improved, worsened, same, shifted = [], [], [], []
    for sid in sorted(set(by_a) & set(by_b)):
        a, b = by_a[sid], by_b[sid]
        va = a["grade"]["verdict"]
        vb = b["grade"]["verdict"]
        item = {
            "sid": sid,
            "cls": a["cls"],
            "A": va,
            "B": vb,
            "delta": VERDICT_SCORE.get(vb, -1) - VERDICT_SCORE.get(va, -1),
        }
        if item["delta"] > 0:
            improved.append(item)
        elif item["delta"] < 0:
            worsened.append(item)
        else:
            same.append(item)
        if va != vb:
            shifted.append(item)
    n = len(improved) + len(worsened) + len(same)
    return {
        "pair": name,
        "n": n,
        "improved": improved,
        "worsened": worsened,
        "verdict_changed": shifted,
        "same_n": len(same),
        "net_verdict_score": sum(x["delta"] for x in improved + worsened + same),
        "improved_n": len(improved),
        "worsened_n": len(worsened),
    }


def e_class_summary(rows: list[dict]) -> dict:
    e_rows = [row for row in rows if row["cls"] == UNANSWERABLE_CLASS]
    abstained = [row for row in e_rows if row["grade"]["answer_state"] == "abstained"]
    no_flag = [row for row in e_rows if said_no(row["reader_answer"])]
    fabricated = [row for row in e_rows if row["grade"].get("fabricated")]
    answered = [row for row in e_rows if row["grade"]["answer_state"] == "answered"]
    return {
        "n": len(e_rows),
        "sids": [row["sid"] for row in e_rows],
        "abstained": len(abstained),
        "abstained_rate": len(abstained) / len(e_rows) if e_rows else None,
        "mechanical_said_no": len(no_flag),
        "mechanical_said_no_sids": [row["sid"] for row in no_flag],
        "fabricated": len(fabricated),
        "fabricated_sids": [row["sid"] for row in fabricated],
        "answered_without_abstain": [row["sid"] for row in answered],
        "novel_numbers": {
            row["sid"]: row["novel_numbers"] for row in e_rows if row["novel_numbers"]
        },
        "detail": [
            {
                "sid": row["sid"],
                "verdict": row["grade"]["verdict"],
                "answer_state": row["grade"]["answer_state"],
                "said_no": said_no(row["reader_answer"]),
                "novel_numbers": row["novel_numbers"],
                "answer": row["reader_answer"],
            }
            for row in e_rows
        ],
    }


def compliance_summary(rows: list[dict]) -> dict:
    ambiguous = [row for row in rows if row["cls"] in AMBIGUOUS_CLASSES]
    fact = [row for row in rows if row["cls"] in FACT_CLASSES]
    def agg(items):
        n = len(items)
        return {
            "n": n,
            "flags_conflict": sum(1 for row in items if row["contract"].get("flags_conflict")),
            "lists_both_sides": sum(1 for row in items if row["contract"].get("lists_both_sides")),
            "cites_date": sum(1 for row in items if row["contract"].get("cites_date")),
            "asks_back": sum(1 for row in items if row["contract"].get("asks_back")),
            "says_absent": sum(1 for row in items if row["contract"].get("says_absent")),
        }
    return {
        "ambiguous_CGJ": agg(ambiguous),
        "fact_ABDFHI": agg(fact),
        "detail_CGJ": [
            {
                "sid": row["sid"],
                "cls": row["cls"],
                "verdict": row["grade"]["verdict"],
                "answer_state": row["grade"]["answer_state"],
                **row["contract"],
            }
            for row in ambiguous
        ],
    }


def cache_totals(bundle: dict) -> dict:
    def agg(section: dict, group: str) -> dict:
        entries = [e for e in section.values() if e.get("group") == group]
        usage = _usage_sum([e.get("usage") or {} for e in entries])
        return {"entries": len(entries), "usage": usage}

    return {
        group: {"reader": agg(bundle["reader"], group), "judge": agg(bundle["judge"], group)}
        for group in GROUPS
    }


# ======================================================================
# 七、main
# ======================================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qbank", default=DEFAULT_QBANK)
    parser.add_argument("--corpus", default=DEFAULT_CORPUS, help="真实 corpus.json；不存在则从 fixture 重建")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument("--answer-key", default=DEFAULT_ANSWER_KEY)
    parser.add_argument("--json", default=str(DEFAULT_JSON))
    parser.add_argument("--groups", default="A,B,C", help="逗号分隔，默认 A,B,C")
    parser.add_argument("--qids", default="", help="逗号分隔的题号；默认全部 40 题")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="忽略缓存重新调用（计费）")
    parser.add_argument("--show-prompts", action="store_true", help="只打印三组 prompt，不调用")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args(argv)

    groups = [g.strip().upper() for g in args.groups.split(",") if g.strip()]
    bad = [g for g in groups if g not in GROUPS]
    if bad:
        raise SystemExit(f"未知分组：{bad}；可选 {GROUPS}")

    # ---------------- 数据 ----------------
    corpus, corpus_audit = load_corpus(args.corpus, args.qbank, args.fixture)
    scenarios = parse_qbank(args.qbank)
    queries, unmatched = build_queries(corpus, scenarios)
    if unmatched:
        raise SystemExit(f"题库记忆映射失败 {len(unmatched)} 条：{unmatched[:3]}")
    print(
        f"语料：{len(corpus)} 条（{corpus_audit['source']}）/ 题 {len(queries)} 道；"
        f"日期缺失 {corpus_audit['raw_date_stats'].get('日期不详', 0)} 条"
    )

    rankings, cache_meta = load_rankings(corpus, queries)
    gold_r4 = float(
        np.mean(
            [recall_at(rankings[item["sid"]], set(item["gold"]), FINAL_K) for item in queries]
        )
    )
    print(f"检索：向量缓存 {cache_meta['cache_hits']} 条（0 次 API）；R@4={gold_r4:.4f}")
    if abs(gold_r4 - EXPECTED_R4) > 5e-4:
        raise SystemExit(
            f"检索闸门失败：R@4={gold_r4:.4f} != {EXPECTED_R4}；语料/缓存/映射不一致，停止"
        )

    # 与上一轮 reader_eval.json 的 top-4 逐题对齐（语料重建的最强校验）
    prev_audit: dict = {"reader_eval_json": None}
    if PREV_JSON.exists():
        prev = json.loads(PREV_JSON.read_text(encoding="utf-8"))
        doc_mismatch, text_mismatch, date_mismatch = [], [], []
        prev_by_sid = {row["sid"]: row for row in prev.get("per_question", [])}
        slots = 0
        for item in queries:
            sid = item["sid"]
            expected = prev_by_sid.get(sid)
            if not expected:
                continue
            got = rankings[sid][: len(expected["top4"])]
            exp_docs = [row["doc"] for row in expected["top4"]]
            if got != exp_docs:
                doc_mismatch.append({"sid": sid, "got": got, "expected": exp_docs})
                continue
            for doc, row in zip(got, expected["top4"]):
                slots += 1
                if corpus[doc]["text"] != row["text"]:
                    text_mismatch.append({"sid": sid, "doc": doc})
                if (corpus[doc].get("raw_date") or "日期不详") != row["raw_date"]:
                    date_mismatch.append({"sid": sid, "doc": doc})
        prev_audit = {
            "reader_eval_json": str(PREV_JSON),
            "top4_doc_mismatch": doc_mismatch,
            "record_slots_checked": slots,
            "text_mismatch": text_mismatch,
            "raw_date_mismatch": date_mismatch,
            "gate_pass": not doc_mismatch and not text_mismatch and not date_mismatch,
        }
        print(
            f"复现校验：top-4 逐题一致 {len(queries) - len(doc_mismatch)}/{len(queries)}；"
            f"记录槽 {slots} 个，文本/日期不一致 {len(text_mismatch)}/{len(date_mismatch)}"
        )
        if not prev_audit["gate_pass"]:
            raise SystemExit("语料重建与上一轮 reader_eval.json 不一致，停止")

    # ---------------- 题集 ----------------
    selected = queries
    if args.qids:
        wanted = {int(x) for x in args.qids.split(",") if x.strip()}
        selected = [item for item in queries if item["sid"] in wanted]
    if args.limit:
        selected = selected[: args.limit]
    print(f"本次题数：{len(selected)}；分组：{groups}")

    sections = parse_answer_key(Path(args.answer_key))
    labels: dict = {}
    if Path(args.labels).exists():
        payload = json.loads(Path(args.labels).read_text(encoding="utf-8"))
        labels = {
            row["sid"]: {
                "cls": row["cls"],
                "unanswerable": row.get("unanswerable", False),
            }
            for row in payload.get("per_question", [])
        }
        print(f"证据标注：{Path(args.labels).name}（{len(labels)} 题）")

    records_by_sid = {
        item["sid"]: question_records(rankings, corpus, item["sid"]) for item in queries
    }

    if args.show_prompts:
        for item in selected:
            sid = item["sid"]
            print("=" * 78)
            for group in groups:
                print(f"----- {group} 组：{GROUP_LABEL[group]} -----")
                print(
                    build_reader_prompt_group(group, item["q"], records_by_sid[sid])
                )
                print("-" * 60)
        return 0

    # prompt 模板自检（A/B 必须与 reader_eval 逐字相同，否则缓存键/可比性都不成立）
    sample = selected[0]
    checks = verify_prompt_templates(
        sample["sid"],
        sections[sample["sid"]].cls,
        sample["q"],
        records_by_sid[sample["sid"]],
        sections[sample["sid"]],
    )
    print(
        "prompt 自检："
        + "；".join(f"{k}={v}" for k, v in checks.items())
    )
    if not all(checks.values()):
        raise SystemExit(f"prompt 模板自检失败：{checks}")

    setup_proxy()
    key = load_key()
    bundle = load_bundle()

    # ---------------- A 组迁移：上一轮的真实结果（0 调用） ----------------
    migration = {"migrated_reader": 0, "migrated_judge": 0, "missing": []}
    if "A" in groups:
        old_reader = _load_cache(PREV_CACHE_READER)
        old_judge = _load_cache(PREV_CACHE_JUDGE)
        for item in selected:
            sid = item["sid"]
            records = records_by_sid[sid]
            prompt = build_reader_prompt_group("A", item["q"], records)
            rkey = reader_key("A", sid, item["q"], prompt, records)
            if rkey not in bundle["reader"] and rkey in old_reader:
                entry = dict(old_reader[rkey])
                entry["group"] = "A"
                entry["origin"] = "reader_eval_cache"
                bundle["reader"][rkey] = entry
                migration["migrated_reader"] += 1
            answer = bundle["reader"].get(rkey, {}).get("answer")
            if answer:
                gprompt = build_grader_prompt_group(
                    "A", sid, sections[sid].cls, item["q"], records, sections[sid], answer
                )
                gkey = grader_key("A", sid, answer, gprompt, records)
                if gkey not in bundle["judge"] and gkey in old_judge:
                    entry = dict(old_judge[gkey])
                    entry["group"] = "A"
                    entry["origin"] = "reader_eval_cache"
                    bundle["judge"][gkey] = entry
                    migration["migrated_judge"] += 1
            if rkey not in bundle["reader"]:
                migration["missing"].append(sid)
        save_bundle(bundle)
        print(
            f"A 组缓存迁移：reader {migration['migrated_reader']}/{len(selected)}，"
            f"judge {migration['migrated_judge']}/{len(selected)}（0 次调用）"
        )

    # ---------------- 跑三组 ----------------
    results: dict[str, list[dict]] = {}
    costs: dict[str, dict] = {}
    for group in groups:
        print(f"\n== {group} 组：{GROUP_LABEL[group]} ==")
        rows, cost = run_group(
            group,
            selected,
            {
                "bundle": bundle,
                "key": key,
                "sections": sections,
                "records_by_sid": records_by_sid,
            },
            force=args.force,
            concurrency=args.concurrency,
            verbose=True,
        )
        results[group] = rows
        costs[group] = cost
        save_bundle(bundle)
        s = stats(rows)
        print(
            f"  {group}：✅{s['✅']} ⚠️{s['⚠️']} ❌{s['❌']}"
            f"（正确率 {s['correct_rate']:.1%}，合格率 {s['pass_rate']:.1%}）"
            f"；本次 reader {cost['calls']['reader']} 次 / judge {cost['calls']['judge']} 次"
        )

    # ---------------- 汇总 ----------------
    answerable = [
        row
        for row in results[groups[0]]
        if row["cls"] != UNANSWERABLE_CLASS
        and not labels.get(row["sid"], {}).get("unanswerable")
    ]
    answerable_sids = {row["sid"] for row in answerable}

    def summarize(rows: list[dict]) -> dict:
        overall = stats(rows)
        ans = stats([row for row in rows if row["sid"] in answerable_sids])
        return {
            "overall": overall,
            "answerable": ans,
            "ambiguous_CGJ": split_stats(rows, lambda r: r["cls"] in AMBIGUOUS_CLASSES),
            "fact_ABDFHI": split_stats(rows, lambda r: r["cls"] in FACT_CLASSES),
            "unanswerable_E": split_stats(rows, lambda r: r["cls"] == UNANSWERABLE_CLASS),
            "by_class": by_class(rows),
            "e_class": e_class_summary(rows),
            "compliance": compliance_summary(rows),
        }

    summaries = {group: summarize(rows) for group, rows in results.items()}

    paired = {}
    base = "A"
    for group in groups:
        if group != base and base in results:
            paired[f"{base}_vs_{group}"] = paired_compare(results[base], results[group], f"A→{group}")
    if "B" in results and "C" in results:
        paired["B_vs_C"] = paired_compare(results["B"], results["C"], "B→C")

    # A 组复现校验：与上一轮 reader_eval.json 逐题一致
    replication = None
    if "A" in results and PREV_JSON.exists():
        prev = json.loads(PREV_JSON.read_text(encoding="utf-8"))
        prev_by_sid = {row["sid"]: row for row in prev.get("per_question", [])}
        diffs = []
        for row in results["A"]:
            old = prev_by_sid.get(row["sid"])
            if not old:
                diffs.append({"sid": row["sid"], "why": "missing_in_previous"})
                continue
            if old["reader_answer"] != row["reader_answer"]:
                diffs.append({"sid": row["sid"], "why": "answer_diff"})
            elif old["grade"]["verdict"] != row["grade"]["verdict"]:
                diffs.append({"sid": row["sid"], "why": "verdict_diff"})
            elif old["grade"]["answer_state"] != row["grade"]["answer_state"]:
                diffs.append({"sid": row["sid"], "why": "state_diff"})
        replication = {
            "reference": str(PREV_JSON),
            "compared": len(results["A"]),
            "identical": not diffs,
            "diffs": diffs,
            "note": "A 组直接命中上一轮缓存（同模型/同 prompt/同记录），"
                    "这里逐题比对回答与判分，证明基线是真复现而非重算",
        }

    totals = cache_totals(bundle)
    provenance = {
        "reader_entries_from_reader_eval": sum(
            1 for entry in bundle["reader"].values()
            if entry.get("origin") == "reader_eval_cache"
        ),
        "judge_entries_from_reader_eval": sum(
            1 for entry in bundle["judge"].values()
            if entry.get("origin") == "reader_eval_cache"
        ),
        "note": "A 组 40 题的回答与判分直接沿用上一轮 reader_eval.py 的缓存"
                "（同模型 / 同 preamble / 同记录），因此本次 0 次生成、0 次判分；"
                "「迁移数」在第一次运行时统计，条目已在缓存里时记为 0",
    }
    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "partial": len(selected) != len(queries),
        "n_questions_selected": len(selected),
        "data": {
            "corpus": corpus_audit,
            "retrieval": {
                **cache_meta,
                "method": "cached qwen3.7-text-embedding 向量 + 余弦 + stable argsort，top-4",
                "r4_gold": gold_r4,
                "r4_expected": EXPECTED_R4,
                "gate_pass": abs(gold_r4 - EXPECTED_R4) <= 5e-4,
            },
            "reproduction": prev_audit,
            "labels": str(args.labels) if labels else None,
            "answer_key_sections": len(sections),
        },
        "groups": {
            group: {
                "label": GROUP_LABEL[group],
                "reader_prompt_version": READER_VERSIONS[group],
                "grader_prompt_version": GRADER_VERSIONS[group],
                "records_format": "[记录于 raw_date] text" if group == "C" else "[raw_date] text",
                "contract_injected": group in ("B", "C"),
                "this_run": costs[group],
                "cache_total": totals[group],
            }
            for group in groups
        },
        "contract": {
            "version": "v2",
            "text": CONTRACT_V2,
            "preamble": PREAMBLE,
            "placement": "PREAMBLE + 契约全文 + 【用户问】 + 【检索到的记忆】",
            "note": "A 组不注入；B/C 注入同一段逐字文本，C 只把记录前缀从 [date] 改成 [记录于 date]",
        },
        "summary": summaries,
        "paired": paired,
        "replication_A": replication,
        "migration_A": migration if "A" in groups else None,
        "baseline_provenance": provenance if "A" in groups else None,
        "prompt_self_check": checks,
        "per_question": [
            {
                "sid": item["sid"],
                "cls": sections[item["sid"]].cls,
                "cls_name": sections[item["sid"]].cls_name,
                "question": item["q"],
                "top4": records_by_sid[item["sid"]],
                "groups": {
                    group: next(
                        (row for row in results[group] if row["sid"] == item["sid"]), None
                    )
                    for group in groups
                },
            }
            for item in selected
        ],
    }
    Path(args.json).write_text(
        json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # ---------------- stdout ----------------
    def fmt_rate(value) -> str:
        return f"{value:.1%}" if isinstance(value, (int, float)) else "n/a"

    print("\n== 三组对照（top-4 记忆 → deepseek-flash → 三档判分） ==")
    print(f"{'组':<4}{'✅':>4}{'⚠️':>4}{'❌':>4}{'正确率':>9}{'合格率':>9}  可答题 ✅/n")
    for group in groups:
        s = summaries[group]
        o, a = s["overall"], s["answerable"]
        print(
            f"{group:<5}{o['✅']:>4}{o['⚠️']:>4}{o['❌']:>4}"
            f"{fmt_rate(o['correct_rate']):>9}{fmt_rate(o['pass_rate']):>9}"
            f"  {a['✅']}/{a['n']} = {fmt_rate(a['correct_rate'])}"
        )
    print("\n  分组：")
    for group in groups:
        s = summaries[group]
        f, g, e = s["fact_ABDFHI"], s["ambiguous_CGJ"], s["unanswerable_E"]
        print(
            f"    {group}: 单点事实 {f['✅']}/{f['n']}（{fmt_rate(f['correct_rate'])}）"
            f"｜歧义 {g['✅']}/{g['n']}（{fmt_rate(g['correct_rate'])}，"
            f"合格 {fmt_rate(g['pass_rate'])}）｜E {e['✅']}/{e['n']}"
        )
    for key_name, cmp_ in paired.items():
        print(
            f"\n  配对 {cmp_['pair']}：变好 {cmp_['improved_n']}、变差 {cmp_['worsened_n']}、"
            f"不变 {cmp_['same_n']}；净分 {cmp_['net_verdict_score']:+d}"
        )
        if cmp_["improved"]:
            print(f"    变好：{[(x['sid'], x['A'], x['B']) for x in cmp_['improved']]}")
        if cmp_["worsened"]:
            print(f"    变差：{[(x['sid'], x['A'], x['B']) for x in cmp_['worsened']]}")
    if replication:
        print(
            f"\n  A 组复现：{replication['compared']} 题逐题比对，"
            f"{'完全一致' if replication['identical'] else replication['diffs']}"
        )
    print("\n  成本（本次调用）：")
    for group in groups:
        c = costs[group]
        print(
            f"    {group}: reader {c['calls']['reader']} 次 / {c['usage']['reader']['total_tokens']} tok；"
            f"judge {c['calls']['judge']} 次 / {c['usage']['judge']['total_tokens']} tok"
        )
    print(f"\n  JSON 写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(errors="replace")
    raise SystemExit(main())
