"""文本归一化与二元组构造（检索层 R2 的纯函数部分，v8 §3）。

**索引侧与查询侧共用同一个函数**，逐字复刻基线
``research/recall/recall_eval.py::bigrams_of``：

1. NFKC 归一化：全角转半角、兼容字符折叠；**不做繁简转换**。
2. 剔除零宽字符（U+200B..U+200D / U+2060 / U+FEFF / U+180E）：它们既不可见，
   又会让「看起来相同」的查询命中不到索引。
3. 按码点切段：CJK 连续段 / Latin+数字连续段 / 其他（标点空白，不产 token）。
4. CJK 段 ``len == 1`` → 产该单字；``len > 1`` → **只产双字**（不产单字）。
5. Latin 段整段小写，作为一个 token。
6. 全部 token 进**同一个** ``bg`` 列 / 一个 FTS5(unicode61) 索引——不拆 uni/bi 两列。

「单字+双字」的早期设计作废：基线只在 CJK 段长度恰为 1 时产单字；这也是 2/40
空结果的来源。

查询表达式：每个 token 用双引号包成短语（转义内部双引号），因此 ``NEAR`` /
``AND`` / ``NOT`` / ``*`` 都只会被当作普通词面，不是 FTS5 语法。token 上限 200，
空 token 返回 ``None``（调用方直接 NO_MATCH，不构造空 MATCH 表达式）。
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

QUERY_TOKEN_LIMIT = 200


def normalize(text: str) -> str:
    """NFKC → 去零宽 → 全角空格转半角 → 压缩空白。幂等、对非法输入不抛异常。"""
    try:
        s = unicodedata.normalize("NFKC", text or "")
    except (TypeError, ValueError):  # 非法代理对等：退回原串，绝不因派生失败而丢写入
        s = str(text or "")
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


def bigrams_of(text: str) -> list[str]:
    """基线 ``bigrams_of`` 的逐字复刻——索引侧与查询侧**共用**此函数。"""
    tokens: list[str] = []
    for kind, seg in segments(text):
        if kind == "cjk":
            if len(seg) == 1:
                tokens.append(seg)
            else:
                tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
        else:
            tokens.append(seg.lower())
    return tokens


def ngram_tokens(text: str) -> list[str]:
    """兼容旧名：索引侧 token 序列（= 基线 bigrams_of，单混合列）。"""
    return bigrams_of(text)


def ngram_text(text: str) -> str:
    """memories_ngrams.ngram_text：空格分隔的单混合列 token 串。"""
    return " ".join(bigrams_of(text))


def _sanitize_token(token: str) -> str | None:
    """去掉控制字符、转义内部双引号（FTS5 短语内用两个双引号表示一个）。"""
    token = "".join(ch for ch in token if ch >= " " and ch != "\x7f")
    token = token.replace('"', '""')
    return token or None


def unique_tokens(tokens: list[str], *, limit: int = QUERY_TOKEN_LIMIT) -> tuple[list[str], int]:
    """去重保序 + token 上限截断；返回 (tokens, 截断数)。token 保持原样。"""
    seen: set[str] = set()
    uniq: list[str] = []
    for tok in tokens:
        if not tok or not tok.strip() or tok in seen:
            continue
        seen.add(tok)
        uniq.append(tok)
    truncated = max(0, len(uniq) - int(limit))
    return uniq[: int(limit)], truncated


def query_tokens(text: str, *, limit: int = QUERY_TOKEN_LIMIT) -> tuple[list[str], int]:
    """查询侧：与索引侧同一函数；返回 (token 列表, 超限截断数)。"""
    return unique_tokens(bigrams_of(text), limit=limit)


def fts_match_expr(
    tokens: list[str], *, limit: int = QUERY_TOKEN_LIMIT
) -> str | None:
    """把 token 列表拼成 FTS5 表达式 `"a" OR "b"`；空 token → ``None``。

    每个 token 都包成短语（转义内部双引号），因此 ``NEAR/AND/NOT/*`` 不会被当作
    FTS5 语法。
    """
    uniq, _truncated = unique_tokens(tokens, limit=limit)
    escaped: list[str] = []
    for tok in uniq:
        clean = _sanitize_token(tok)
        if clean is not None and clean not in escaped:
            escaped.append(clean)
    if not escaped:
        return None
    return " OR ".join('"%s"' % tok for tok in escaped)


def trigram_tokens(text: str) -> list[str]:
    """R3 查询 token（可选路，v8 默认不启用）：CJK 段三字滑窗；Latin/数字段单 token。"""
    tokens: list[str] = []
    for kind, seg in segments(text):
        if kind == "cjk":
            tokens.extend(seg[i : i + 3] for i in range(len(seg) - 2))
        else:
            tokens.append(seg)
    uniq, _truncated = unique_tokens(tokens)
    return uniq
