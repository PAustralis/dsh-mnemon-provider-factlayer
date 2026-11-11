"""文本归一化与二元组/trigram 构造（检索层 R2 / R3 的纯函数部分）。

规则（docs/02-检索层设计.md §3.1，v3.1）：

1. NFKC 归一化：全角转半角、兼容字符折叠；**不做繁简转换**。
2. 剔除零宽字符（U+200B..U+200D / U+2060 / U+FEFF / U+180E）：它们既不可见，
   又会让「看起来相同」的查询命中不到索引。
3. 分段：CJK 连续段（含扩展 A / 兼容区）与 Latin/数字连续段各成一段；
   标点**不产出 token**（也不跨越标点拼双字）——与 `research/recall/recall_eval.py`
   的 `RE_SEG` 分段完全一致。
4. 索引侧（派生表 memories_ngrams）：CJK 段按位置输出「单字序列 + 双字序列」，
   Latin/数字连续段原样保留为**单 token**（小写）；全部空格分隔。
5. 查询侧（**冻结基线**）：逐字复刻 recall_eval.py 的 `bigrams_of`——
   CJK 段长度 1 时取该单字，长度 ≥2 时只取双字；Latin/数字段取小写单 token；
   去重保序，再拼成 FTS5 表达式 `"ab" OR "bc" ...`。
   短语 0 命中时回退到单字（调用方负责计数 + 日志，见 retrieval.bigram_route）。

「不切标点」的实现口径：标点不产出 token、也不作为可拼字的位置（即不产生跨标点的
双字），因为标点属于「非词字符」；这与实测拿到 0.4875 的原型分段一致。
"""

from __future__ import annotations

import re
import unicodedata

# 与 research/recall/recall_eval.py 的 CJK 类完全一致（含扩展 A 与兼容表意区）
CJK_CLASS = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"

# Latin / 数字连续段：允许中缀 . - _ （与原型一致，NFKC 后全角句点/连字符已折叠）
RE_SEGMENT = re.compile(r"[%s]+|[A-Za-z0-9]+(?:[.\-_][A-Za-z0-9]+)*" % CJK_CLASS)

ZERO_WIDTH_CHARS = "\u200b\u200c\u200d\u2060\ufeff\u180e"
_ZERO_WIDTH_TABLE = dict.fromkeys(map(ord, ZERO_WIDTH_CHARS), None)


def normalize(text: str) -> str:
    """NFKC → 去零宽 → 全角空格转半角 → 压缩空白。幂等。"""
    s = unicodedata.normalize("NFKC", text or "")
    s = s.translate(_ZERO_WIDTH_TABLE)
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def segments(text: str) -> list[tuple[str, str]]:
    """切分为 [("cjk"|"latin", 段文本)]，保持原顺序；Latin 段统一小写。

    标点、空白、其他符号不产出段（与原型 RE_SEG.finditer 行为一致）。
    """
    out: list[tuple[str, str]] = []
    for match in RE_SEGMENT.finditer(normalize(text)):
        seg = match.group(0)
        if seg[0].isascii():
            out.append(("latin", seg.lower()))
        else:
            out.append(("cjk", seg))
    return out


def ngram_tokens(text: str, *, include_unigrams: bool = True) -> list[str]:
    """索引侧 token 序列：CJK 段单字 + 双字（按位置，保留原顺序）；Latin/数字段单 token。

    ``include_unigrams=False`` 仅用于基线对比（复刻原型的「只双字」索引；
    单字 CJK 段仍产出该字，与原型一致）。
    """
    tokens: list[str] = []
    for kind, seg in segments(text):
        if kind == "cjk":
            if len(seg) == 1:
                tokens.append(seg)
                continue
            if include_unigrams:
                tokens.extend(seg)
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
        else:
            tokens.append(seg)
    return tokens


def ngram_text(text: str, *, include_unigrams: bool = True) -> str:
    """memories_ngrams.ngram_text：空格分隔的 token 串。"""
    return " ".join(ngram_tokens(text, include_unigrams=include_unigrams))


def bigram_query_tokens(text: str) -> list[str]:
    """R2 查询构造器（**冻结基线**，逐字复刻 recall_eval.py 的 bigrams_of）。

    - CJK 段长度 1 → 该单字；长度 ≥2 → 只取双字（不取单字）
    - Latin / 数字段 → 小写单 token
    """
    tokens: list[str] = []
    for kind, seg in segments(text):
        if kind == "cjk":
            if len(seg) == 1:
                tokens.append(seg)
            else:
                tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
        else:
            tokens.append(seg)
    return _dedupe(tokens)


def unigram_query_tokens(text: str) -> list[str]:
    """短语 0 命中时的回退构造：CJK 全部单字 + Latin/数字 token。"""
    tokens: list[str] = []
    for kind, seg in segments(text):
        if kind == "cjk":
            tokens.extend(seg)
        else:
            tokens.append(seg)
    return _dedupe(tokens)


def trigram_tokens(text: str) -> list[str]:
    """R3 查询 token：CJK 段取三字滑窗；Latin/数字段取单 token。

    任何一段长度 < 3 都不会产出 token；全部为空时该路不可用（1–2 字查询）。
    """
    tokens: list[str] = []
    for kind, seg in segments(text):
        if kind == "cjk":
            tokens.extend(seg[i : i + 3] for i in range(len(seg) - 2))
        else:
            tokens.append(seg)
    return _dedupe(tokens)


def _dedupe(tokens: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def fts_match_expr(tokens: list[str]) -> str | None:
    """把 token 列表拼成 FTS5 表达式 `"a" OR "b"`（逐字复刻原型的 fts_match_expr）。"""
    seen: set[str] = set()
    uniq: list[str] = []
    for tok in tokens:
        tok = tok.replace('"', "")
        if tok and tok not in seen:
            seen.add(tok)
            uniq.append(tok)
    if not uniq:
        return None
    return " OR ".join('"%s"' % tok for tok in uniq)
