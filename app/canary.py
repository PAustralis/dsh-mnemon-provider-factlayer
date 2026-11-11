"""检索层金丝雀（v8 §9.12）。

- 用与产品一致的 NFKC + 分段构造器抽子串：单 CJK 段 ≥6 字、不跨标点。
- N=20（可配），只对 **bigram 路**严格断言：抽出的子串必须在原始 top-K 里命中该条。
- 标 canary 的查询**不计入**检索统计（直接调 ``bigram_route``，不经过 search）。
- 失败记录 id；连续两次失败率 ≥ 阈值（默认 20%）告警。
"""

from __future__ import annotations

import logging

from . import textnorm

logger = logging.getLogger("factlayer.canary")


def _longest_cjk_segment(content: str) -> str | None:
    best: str | None = None
    for kind, seg in textnorm.segments(content):
        if kind != "cjk" or len(seg) < 6:
            continue
        if best is None or len(seg) > len(best):
            best = seg
    return best


def run_canary(service, cfg) -> dict:
    """跑一轮金丝雀；返回可放 /health 的结构化报告。"""
    size = max(1, int(cfg.canary_size))
    rows = service.db.read_conn.execute(
        "SELECT id, content FROM memories ORDER BY gseq DESC LIMIT ?",
        (size * 5,),  # 多取一些，筛出含长 CJK 段的记录
    ).fetchall()
    samples: list[tuple[str, str]] = []
    for row in rows:
        segment = _longest_cjk_segment(row["content"])
        if segment is None:
            continue
        # 取段中央 6 字，避免总是开头（开头可能是「我」「今天」等高频词）
        start = max(0, (len(segment) - 6) // 2)
        query = segment[start : start + 6]
        samples.append((row["id"], query))
        if len(samples) >= size:
            break

    if not samples:
        return {
            "status": "insufficient_samples",
            "sampled": 0,
            "failures": 0,
            "failure_rate": None,
            "failed_ids": [],
            "alert": False,
        }

    failed_ids: list[str] = []
    for memory_id, query in samples:
        outcome = service.bigram_route(query, int(cfg.route_top_k))
        if memory_id not in outcome.raw_ids:
            failed_ids.append(memory_id)
    rate = len(failed_ids) / len(samples)
    logger.info(
        "canary: sampled=%d failures=%d rate=%.3f failed_ids=%s",
        len(samples),
        len(failed_ids),
        rate,
        failed_ids,
    )
    return {
        "status": "ok",
        "sampled": len(samples),
        "failures": len(failed_ids),
        "failure_rate": rate,
        "failed_ids": failed_ids,
        "alert": False,  # 由 RetrievalService 结合上一轮结果决定
    }
