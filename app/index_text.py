"""索引文本构造（检索层派生层的纯函数，docs/02 v9）。

事实层有两条时间：

- ``event_time``：用户自报，可空；按精度存 ``2025`` / ``2025-03`` / ``2025-03-14``；
- ``recorded_at``：服务端 Unix 毫秒，非空。

索引文本 = **确定性日期前缀 + 明确分隔符 + 原文**：

    event_time="2025-03-14"  ->  "2025-03-14 2025年3月14日\\n{content}"
    event_time="2025-03"     ->  "2025-03 2025年3月\\n{content}"
    event_time="2025"        ->  "2025 2025年\\n{content}"
    event_time=None          ->  "{recorded_at 的 UTC 日期} {中文日期}\\n{content}"

为什么同时给 ISO 与中文形式：embedding 模型与 BM25 二元组对数字/中文的切分不同，
两种形式都进同一段索引文本，才能让「问 2025 年」在两条路上都命中。分隔符
``\\n`` 不产 token，保证日期 token 与正文首字不粘连（例如不会把「年体」连成一个
CJK 段）。

**纯函数**：输出只由记录字段决定，不读时钟、不读配置、无随机；因此派生重建
结果与写入时索引逐字节一致（``INDEX_TEXT_VERSION`` 用于校验这一点）。

**事实层不动**：本模块只构造派生索引文本；``memories.content`` 与输出给模型的
``content`` 永远保持原文。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Mapping

# 索引文本构造规则版本：1 = 只有 content（历史隐式版本）；2 = 日期前缀 + content。
# 版本写 meta.index_text_version；启动时库中版本低于当前且有记录 → 后台重建派生层。
INDEX_TEXT_VERSION = 2
INDEX_TEXT_VERSION_KEY = "index_text_version"
INDEX_TEXT_REBUILD_KEY = "index_text_rebuild"

# 前缀与正文之间的分隔符：换行不产 CJK/Latin token，日期与正文不会粘连。
INDEX_TEXT_SEPARATOR = "\n"

_YEAR_RE = re.compile(r"^(\d{4})$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")

_PRECISIONS = ("year", "month", "day", "instant")


def _format_iso_year(year: int) -> str:
    return f"{year:04d}"


def _format_iso_month(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _format_iso_day(year: int, month: int, day: int) -> str:
    return f"{year:04d}-{month:02d}-{day:02d}"


def _make_day(year: int, month: int, day: int) -> date:
    """构造日期并做真实日历校验（2 月 30 日等必须显式报错，不静默接受）。"""
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError(
            f"event_time 不是合法日期：{year:04d}-{month:02d}-{day:02d}"
        ) from exc


def _infer_precision(text: str) -> str | None:
    """按字符串形状推断精度；不合法返回 None（由调用方决定报错/回退）。"""
    if _YEAR_RE.match(text):
        return "year"
    if _MONTH_RE.match(text):
        return "month"
    if _DAY_RE.match(text):
        return "day"
    return None


def _expected_precision(precision: str) -> str:
    """``instant`` 与 ``day`` 在事实层都存完整日期（见 write_path.validate_event_time）。"""
    return "day" if precision == "instant" else precision


def _format_prefix(year: int, month: int | None, day: int | None) -> str:
    """ISO + 中文自然形式；月份/日期不补零（与常见中文写法一致）。"""
    if day is not None and month is not None:
        iso = _format_iso_day(year, month, day)
        return f"{iso} {year}年{month}月{day}日"
    if month is not None:
        iso = _format_iso_month(year, month)
        return f"{iso} {year}年{month}月"
    iso = _format_iso_year(year)
    return f"{iso} {year}年"


def _parse_event_time(text: str, precision: str | None) -> str:
    """把非空 event_time 解析成日期前缀；非法/精度不符 → ``ValueError``。"""
    inferred = _infer_precision(text)
    if inferred is None:
        raise ValueError(f"event_time 格式非法：{text!r}")
    if precision is not None:
        if precision not in _PRECISIONS:
            raise ValueError(f"event_time_precision 非法：{precision!r}")
        if _expected_precision(precision) != inferred:
            raise ValueError(
                f"event_time={text!r} 与 precision={precision!r} 不匹配"
                f"（推断为 {inferred!r}）"
            )
    if inferred == "year":
        year = int(_YEAR_RE.match(text).group(1))  # type: ignore[union-attr]
        if year < 1:
            raise ValueError(f"event_time 年份非法：{text!r}")
        return _format_prefix(year, None, None)
    if inferred == "month":
        match = _MONTH_RE.match(text)
        year, month = int(match.group(1)), int(match.group(2))  # type: ignore[union-attr]
        if year < 1 or not 1 <= month <= 12:
            raise ValueError(f"event_time 月份非法：{text!r}")
        return _format_prefix(year, month, None)
    match = _DAY_RE.match(text)
    year, month, day = (  # type: ignore[union-attr]
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
    )
    valid = _make_day(year, month, day)
    return _format_prefix(valid.year, valid.month, valid.day)


def _recorded_at_date(recorded_at: Any) -> date | None:
    """服务端毫秒 → UTC 日期；缺失/非法返回 ``None``（索引退化为无前缀）。"""
    if recorded_at is None:
        return None
    try:
        millis = int(recorded_at)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def date_prefix(
    event_time: str | None,
    event_time_precision: str | None,
    recorded_at: int | None,
) -> str:
    """生成确定性日期前缀（不含分隔符）；没有可用日期时返回 ``""``。

    - 有 ``event_time`` 用它（精度由 ``event_time_precision`` 校验；缺省按形状推断）；
    - 否则用 ``recorded_at`` 的 UTC 日期；
    - 两者都没有 → 空串（调用方直接返回 content）。
    """
    text = (event_time or "").strip()
    if text:
        return _parse_event_time(text, event_time_precision)
    fallback = _recorded_at_date(recorded_at)
    if fallback is None:
        return ""
    return _format_prefix(fallback.year, fallback.month, fallback.day)


def index_text_for(memory: Mapping[str, Any]) -> str:
    """记录 → 索引文本（纯函数）；``content`` 原样追加，绝不被改写。"""
    content = memory.get("content")
    content = "" if content is None else str(content)
    prefix = date_prefix(
        memory.get("event_time"),
        memory.get("event_time_precision"),
        memory.get("recorded_at"),
    )
    if not prefix:
        return content
    return f"{prefix}{INDEX_TEXT_SEPARATOR}{content}"
