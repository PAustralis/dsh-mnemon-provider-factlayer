"""provider adapter 契约声明（v8 §5 / 03 §6 P0）。

本 shim 即 mnemon 的 provider adapter；返回值必须让上层质量策略按
``scoreSemantics: provider-native`` 处理：按 Provider 返回顺序排序、不做归一化，
每条结果都带数值 ``score``（RRF 分，仅用于定档，不承载语义）。
"""

from __future__ import annotations

from .config import (
    FINAL_K_DEFAULT,
    ITEM_MAX_CHARS_DEFAULT,
    SCORE_SEMANTICS,
    TOTAL_MAX_CHARS_DEFAULT,
)

PROVIDER_MANIFEST = {
    "name": "factlayer",
    "scoreSemantics": SCORE_SEMANTICS,
    "finalK": FINAL_K_DEFAULT,
    "itemMaxChars": ITEM_MAX_CHARS_DEFAULT,
    "totalMaxChars": TOTAL_MAX_CHARS_DEFAULT,
    "createdAtFormat": "iso-8601-utc",
    "statusChannels": {
        "empty": "hint（以 NO_MATCH 开头）",
        "non_empty": "tags + 首条 content 尾行",
    },
    "includeSuperseded": "admin-only",
}
