"""research/recall/reader_eval.py 的确定性回退（不联网、不读题库）。

重点：E 类（配额截断）的 ✅ 语义是「明确说记忆里没有」，回退必须记
``abstained``；否则会被 ``answered_unanswerable``（编造）统计误算。
"""

from __future__ import annotations

import sys
from pathlib import Path

RECALL_DIR = Path(__file__).resolve().parent.parent / "research" / "recall"
if str(RECALL_DIR) not in sys.path:
    sys.path.insert(0, str(RECALL_DIR))

from reader_eval import norm_state  # noqa: E402


def test_e_class_correct_verdict_falls_back_to_abstained():
    # E 类没有「answered」这种正确形态：✅ = 明确说没有 = abstained
    assert norm_state("", "✅", "E", False) == "abstained"
    assert norm_state("", "⚠️", "E", False) == "abstained"
    assert norm_state("", "❌", "E", False) == "abstained"


def test_non_e_class_verdict_fallback_unchanged():
    assert norm_state("", "✅", "A", False) == "answered"
    assert norm_state("", "⚠️", "C", False) == "conflict_flagged"
    assert norm_state("", "⚠️", "G", False) == "conflict_flagged"
    assert norm_state("", "⚠️", "J", False) == "conflict_flagged"
    assert norm_state("", "❌", "B", False) == "abstained"


def test_fabricated_wins_and_explicit_state_passes_through():
    assert norm_state("", "✅", "E", True) == "fabricated"
    assert norm_state("abstained", "✅", "E", False) == "abstained"
    assert norm_state("answered", "✅", "E", False) == "answered"  # 判官显式结论优先
    assert norm_state("conflict_flagged", "⚠️", "C", False) == "conflict_flagged"
