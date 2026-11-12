# -*- coding: utf-8 -*-
"""证据级自动标注：从结构化判分要点 + 元数据匹配，零人工地标出"回答本题要用的记录"。

为什么需要这个脚本
==================
``docs/08`` 已经证明：把题库里"检索到的记忆"整包当 gold，会把刻意植入的干扰项
也算成漏召回（33 个漏项里 29 个是标注噪声）。真正的证据级指标要回答的是
"回答这道题，必须看到哪几条记录"，而不是"这个场景出现过哪几条记录"。

监督信号从哪来（按优先级）
--------------------------
1. **结构化监督（首选）**：题库旁边有 ``读者测试40-答案.md``，逐题给出
   ``标准答案 / 可接受 / 不合格`` 三段，并给出 A–J 类别与类别判分细则。
   这就是"什么算答对"的原始编码。老仓库 ``research/phase2/grades.py`` /
   ``grades_new.py`` 只是把这些判分结果落成 ✅/⚠️/❌ 表，本身不含逐记录标签；
   它们的 ``CLASS`` 映射与答案文件的类别表一致，可作交叉校验。
2. **确定性匹配**：把答案要点里的"答案串/实体/数字/日期"与**完整记录**
   （正文 + ``raw_date`` / ``y-m-d`` / ``precision`` / ``scenario``）做匹配。
   日期要点只在正文要点一个都匹配不上时才单独使用（否则"2026 年"会把同年的
   无关记录全拉进来）。
3. **LLM 兜底抽取（可选，缓存）**：仅当确定性规则一个要点都提不出/匹配不上，
   且不是 E 类无解题时，用 deepseek-flash 从"题目 + 场景记录 + 判分要点"里
   抽取 1–3 个"预期答案要点"，再走同一套确定性匹配。默认开启（成本极低、
   缓存后 0 调用），``--no-llm-extract`` 可关。
4. **LLM 判官（可选，默认关）**：``--judge`` 时对每条候选记录问
   "仅凭这条记录能否推出该答案"，温度 0、3 次投票，按
   ``(qid, 记录哈希, 判官模型版本)`` 缓存。仅用于评测，不参与产品路径。

三个口径
--------
- ``strict``：确定性匹配（+ 兜底抽取）得到的证据集；
- ``judge``：LLM 判官多数票得到的证据集（未开 ``--judge`` 时为空）；
- ``union``：两者并集。

分别算 R@4 / R@20（宏平均 + pair 级），并统计 strict 与 judge 的一致率。
**分歧只计数、不裁决**——脚本不会把任何一方改成"正确答案"。

口径边界（写进报告，不要当成实现细节）
--------------------------------------
- 只做词面/数字/日期匹配，**不做语义改写**：``戒了`` vs ``没再喝过``、
  ``改地铁`` vs ``改成坐地铁`` 这类会漏；脚本用一张很小的谓词归一表兜底，
  但仍然是规则，不是理解。
- 数字换算（``三岁`` vs ``3 岁``、``六点半`` vs ``6:30``）只覆盖答案文件
  同时写了两种写法的情形；没写的会漏。
- 多跳（"2026 年上半年"要先看 2025-11 的搬家记录）靠日期/正文双匹配，
  不保证覆盖所有推理链。
- **只看正文会洗掉"答案在元数据里"的失败**：场景 3/13/23/33 的年份约束、
  场景 10/20/30/40 的"日期不详"都只在 ``raw_date`` 里。所以匹配范围必须含
  元数据；本脚本把日期要点与 ``y/m/d`` 对齐，并在报告里单列这类题。
- E 类（7/17/27/37）问的实体在全语料里不存在，证据集按定义为空；它们不进入
  R@4/R@20 的分母，单独统计。把 E 类算成"有证据"会让指标虚高。

用法::

    python research/recall/evidence_labels.py                 # 确定性标注 + 兜底抽取
    python research/recall/evidence_labels.py --dry-run       # 只打印要点/匹配，不落盘
    python research/recall/evidence_labels.py --judge         # 加 LLM 判官（3 票，缓存）
    python research/recall/evidence_labels.py --no-llm-extract
    python research/recall/evidence_labels.py --limit 5       # 先跑 5 题

产物：``research/recall/evidence_labels.json``。LLM 缓存：
``research/recall/.llm_cache_answer_points.json`` /
``.llm_cache_evidence_judge.json``（已 gitignore）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
REPO_ROOT = BASE.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(BASE))

from recall_eval import norm, parse_qbank  # noqa: E402
from run_real_embedding import CACHE_PATH, _cache_key, load_inputs  # noqa: E402

DEFAULT_ANSWER_KEY = os.environ.get(
    "FACTLAYER_ANSWER_KEY",
    str(Path(os.path.expanduser("~")) / "Desktop" / "读者测试40-答案.md"),
)
DEFAULT_JSON = BASE / "evidence_labels.json"
CACHE_POINTS = BASE / ".llm_cache_answer_points.json"
CACHE_JUDGE = BASE / ".llm_cache_evidence_judge.json"

MODEL = "deepseek-flash"
PROMPT_VERSION = "evidence-v1"
JUDGE_PROMPT_VERSION = "judge-v1"
CRED = Path(os.path.expanduser("~")) / ".dsh" / ".credentials.yaml"
ENVFILE = Path(os.path.expanduser("~")) / ".dsh" / ".env"
URL = "https://api.deepseek.com/chat/completions"
KS = (4, 20)
FINAL_K_PROD = 4


# ======================================================================
# 一、结构化监督：解析答案文件
# ======================================================================
@dataclass
class AnswerSection:
    sid: int
    cls: str
    cls_name: str
    standard: str = ""
    acceptable: str = ""
    unacceptable: str = ""
    body: str = ""


SCENE_HEAD = re.compile(
    r"^###\s*场景\s*(\d+)\s*[｜|]\s*([A-J])\s*类\s*[·・]\s*(.+?)\s*$", re.M
)


def parse_answer_key(path: Path) -> dict[int, AnswerSection]:
    text = path.read_text(encoding="utf-8")
    heads = list(SCENE_HEAD.finditer(text))
    if not heads:
        raise SystemExit(f"答案文件里没有解析到任何场景标题：{path}")
    out: dict[int, AnswerSection] = {}
    for i, match in enumerate(heads):
        start = match.start()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[start:end]
        section = AnswerSection(
            sid=int(match.group(1)),
            cls=match.group(2),
            cls_name=match.group(3).strip(),
            body=body,
        )
        for key, attr in (
            ("标准答案", "standard"),
            ("可接受", "acceptable"),
            ("不合格", "unacceptable"),
        ):
            m = re.search(
                rf"^-\s*\*\*{key}\*\*\s*[：:]\s*(.*)$", body, re.M
            )
            if m:
                setattr(section, attr, m.group(1).strip())
        out[section.sid] = section
    return out


def audit_supervision(
    answer_path: Path, sections: dict[int, AnswerSection]
) -> dict:
    """把"找过哪些结构化监督"落成可审计的事实，而不是报告里的一句断言。"""
    from run_real_embedding import QBANK

    bank_text = Path(QBANK).read_text(encoding="utf-8")
    answer_text = answer_path.read_text(encoding="utf-8")
    keyword_scan = {
        key: (key in bank_text)
        for key in ("supporting_facts", "supporting facts", "答案", "评分", "标准答案")
    }
    grades_path = Path(
        r"E:\Tools\projects\dsh-mnemon-provider-graphiti\research\phase2\grades.py"
    )
    crosscheck = None
    if grades_path.exists():
        raw = grades_path.read_text(encoding="utf-8", errors="replace")
        # grades.py 的 CLASS 用 "A": [9, 19, ...] 形式编码
        found: dict[int, str] = {}
        for cls, nums in re.findall(
            r'"([A-J])":\s*\[([\d,\s]+)\]', raw
        ):
            for n in re.split(r"[,\s]+", nums.strip()):
                if n:
                    found[int(n)] = cls
        mismatches = [
            (sid, sec.cls, found.get(sid))
            for sid, sec in sections.items()
            if found and found.get(sid) != sec.cls
        ]
        crosscheck = {
            "path": str(grades_path),
            "parsed": len(found),
            "mismatches": mismatches,
            "match": not mismatches,
            "note": "只做类别交叉校验；grades.py 的 ✅/⚠️/❌ 是回答判分表，不是逐记录标签",
        }
    return {
        "answer_key": str(answer_path),
        "answer_key_fields": [
            "标准答案", "可接受", "不合格", "类别", "类别判分细则",
        ],
        "answer_key_scenes_parsed": len(sections),
        "question_bank": str(QBANK),
        "question_bank_keyword_scan": keyword_scan,
        "question_bank_has_supporting_facts": keyword_scan.get("supporting_facts", False),
        "grades_class_crosscheck": crosscheck,
        "note": "题库本身没有 supporting_facts/答案字段；逐题判分要点在答案文件里。"
                "grades.py / grades_new.py 只有回答级 ✅/⚠️/❌ 表与 CLASS 映射，"
                "不能当逐记录证据标签。",
    }


# ======================================================================
# 二、答案要点抽取（确定性）
# ======================================================================
# 这些词只描述"怎么答/怎么判"，不是答案本身。抽取时剥掉。
META_WORDS = {
    "标准答案", "可接受", "不合格", "无法确定", "无法判断", "不确定", "不能确定",
    "不可知", "不可考", "记忆里没有", "记忆中没有", "没有记录", "没有答案",
    "建议", "确认", "用户", "问题", "信息", "答案", "记录", "记忆", "内容",
    "说法", "结论", "日期", "时间", "先后", "顺序", "同一天", "两条", "三条",
    "四条", "相反", "矛盾", "冲突", "事实", "错误", "正确", "需要", "可能",
    "应该", "倾向", "但是", "但", "如果", "或者", "还是", "以及", "包括",
    "例如", "比如", "等", "这类", "之类", "现在", "当时", "后来", "实际",
    "其实", "基本", "一直", "已经", "曾经", "目前", "最新", "旧", "以前",
    "之前", "之后", "同时", "同一天", "其中", "这个", "那个", "这些", "那些",
    "什么", "怎么", "为什么", "哪", "几", "多少", "是否", "不是", "就是",
    # 本题库里只描述"怎么答"、不足以单独指认答案的泛词
    "确定", "可知", "设置", "判断", "用户确认", "冲突", "相反", "矛盾",
    "顺序", "颜色", "型号", "澄清", "一致", "结论", "发现", "公司", "报告",
    "位置", "数字", "名称", "机票", "住宿信息", "状态", "手机", "车",
    "过敏", "点前", "最后", "最终", "信息", "原因", "事情", "时候", "东西",
    "地方", "城市", "次数", "那次", "这次", "哪条", "那条", "这条", "两地",
    "牵扯", "还留", "可考", "确定最终", "去年", "今年", "明年", "医生",
    "处方", "电话", "保费", "上涨", "保单", "期日", "原件", "酸值", "怀疑",
    "体重", "种类", "花费", "方式", "名字", "问题", "答案",
}

# 含这些子串的长片段基本是判分说明，不是答案实体。
META_SUBSTRINGS = (
    "无法", "不确定", "不能确定", "不可知", "不可考", "记忆里没有",
    "没有记录", "没有答案", "建议", "确认", "记录", "记忆", "说法",
    "结论", "冲突", "矛盾", "相反", "顺序", "日期", "信息", "颜色",
    "型号", "位置", "原因", "设置", "状态", "数字", "名称", "机票",
    "住宿", "报告", "公司", "手机", "点前", "发现", "确定", "最后",
    "最终", "事情", "东西", "地方", "城市", "次数", "那次", "这次",
    "哪条", "那条", "这条", "判断",
)

# 答案要点常见的动词前缀/包裹词。剥掉它们，"改地铁" → "地铁"，
# "搬到南边" → "南边"，"换成了静音茶轴" → "静音茶轴"。
LEAD_PATTERNS = [
    r"^(?:标准答案|可接受|不合格)\s*[：:]",
    r"^(?:说|答|指出|建议|明确|回答|说明|按|把|以|即|就是|倾向|可接受|"
    r"或|例如|比如|如|如果|若|需要|应该|可以|直接|只|仅|并|且|但|而|还|"
    r"又|才|都|也|就|是|为|有|没有|就是|其实|实际上|当时|现在|目前|"
    r"后来|已经|曾经|一直|基本|主要|确实|真的|应该|可能|大概|约|大约|"
    r"以为|觉得|自己家|自己|先)\s*",
    r"^(?:搬到|搬回|搬去|改到|改成|换成|调到|买了|卖了|开了|坐了|用了|"
    r"去了|来了|到了|入手|添加|戒掉|开始|保持|觉得|认为|怀疑|发现|"
    r"检查|复查|体检|医生|让|给|向|从|到|在|将|被|叫|取名|命名|指出|答|说)\s*",
]

TAIL_PATTERNS = [
    r"(?:的话|的事|的问题|的情况|的原因|的时候|这类|之类|等等|什么的)$",
    r"(?:了|着|过|的|地|得|吧|呢|啊|呀|嘛)$",
]

SPLIT_CLAUSE = re.compile(r"[。；;！!？?\n]")
SPLIT_PART = re.compile(r"[，,、/]|以及|或者|还是|和|与|及|或|vs|VS|对比")
SPLIT_PRED = re.compile(r"(?:是|有|在|把|被|让|给|向|从|到|为|了|着|过|的|上床|睡觉|起床|出门|上班)")
DATE_RE = re.compile(
    r"(?:(20\d{2})\s*[年./-]\s*)?(\d{1,2})\s*[月./-]\s*(\d{1,2})?\s*日?"
)
YEAR_RE = re.compile(r"(20\d{2})\s*年")
ALNUM_RE = re.compile(r"[A-Za-z][A-Za-z0-9+._-]*|\d+(?:\.\d+)?\s*(?:mg|度|点|次|岁|杯|个月|号|区|楼|层|公里|kg|cm|mm|GB|TB)")
QUOTE_RE = re.compile(r"[「“\"]([^」”\"]{1,20})[」”\"]")

# 谓词归一：答案文件写"戒了"，记录写"没再喝过/基本不碰了"；不归一就会漏。
# 只收本题库里真实出现、且语义等价的几组，宁可少收，不扩大匹配面。
PREDICATE_EQUIV = {
    "不喝": ("不喝", "没再喝", "没喝", "戒酒", "戒了", "不碰", "没碰"),
    "不碰": ("不碰", "没碰", "不喝", "没再喝", "戒酒", "戒了"),
    "没卖": ("没卖", "不卖", "留着"),
    "卖了": ("卖了", "卖掉", "把车卖"),
    "坐地铁": ("坐地铁", "地铁"),
    "改地铁": ("改地铁", "坐地铁", "地铁"),
    "地铁": ("地铁",),
    "戒咖啡": ("戒咖啡", "不喝咖啡", "没喝咖啡"),
}


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"^[⚠️✅❌\s]+", "", text)
    return text.strip()


def _norm_match(text: str) -> str:
    """匹配用归一：NFKC + 小写 + 去掉所有空白。标点保留（中文标点不参与）。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"\s+", "", text)


def _strip_wrappers(text: str) -> str:
    text = _clean(text)
    changed = True
    while changed:
        changed = False
        for pattern in LEAD_PATTERNS:
            new = re.sub(pattern, "", text).strip()
            if new != text:
                text, changed = new, True
        for pattern in TAIL_PATTERNS:
            new = re.sub(pattern, "", text).strip()
            if new != text:
                text, changed = new, True
    return text.strip(" 　，,。；;：:、（）()「」\"'“”")


def _is_meta(text: str) -> bool:
    t = _norm_match(text)
    if not t:
        return True
    if t in META_WORDS:
        return True
    if len(t) <= 1 and t not in {"虾", "车", "猫", "狗", "茶", "血"}:
        return True
    # 整段都是元词/日期/数字 → 不是实体答案
    if all(ch in "0123456789.-/年月日 " for ch in t):
        return True
    # 长片段里带判分说明 → 不是答案实体（短实体词如"公司"已在 META_WORDS 里）。
    # 含数字的时间/剂量（"23 点前"/"5mg"）是答案本身，不按说明词处理。
    if (
        len(t) >= 4
        and not any(ch.isdigit() for ch in t)
        and any(sub in t for sub in META_SUBSTRINGS)
    ):
        return True
    return False


def _fragments(text: str) -> list[str]:
    """把一段判分要点切成候选答案串。"""
    text = _clean(text)
    out: list[str] = []

    # 引号里的内容优先（往往是"换了新手机""把车卖了"这类原文锚点）
    for quote in QUOTE_RE.findall(text):
        q = _strip_wrappers(quote)
        if q and not _is_meta(q):
            out.append(q)

    # 括号里的补充先单独递归处理（"（路由器坏了）"/"（开车 / 改地铁）"），
    # 不能先删括号：scene 40 的答案要点整段都在括号里。
    for inner in re.findall(r"[（(]([^）)]*)[）)]", text):
        out.extend(_fragments(inner))

    # 去掉引号/括号后再切，避免同一内容重复
    main = QUOTE_RE.sub(" ", text)
    main = re.sub(r"[（(][^）)]*[）)]", " ", main)
    for clause in SPLIT_CLAUSE.split(main):
        for part in SPLIT_PART.split(clause):
            part = _strip_wrappers(part)
            if not part:
                continue
            # 先整体保留（短片段）
            if 1 <= len(part) <= 12 and not _is_meta(part):
                out.append(part)
            # 再按"是/有/在..."切出主语/宾语（长片段）
            for sub in SPLIT_PRED.split(part):
                sub = _strip_wrappers(sub)
                if 1 <= len(sub) <= 12 and not _is_meta(sub):
                    out.append(sub)
            # 中文短语的尾部 2 字往往是名词核（"只喝绿茶"→"绿茶"、
            # "改地铁"→"地铁"、"先搬到南边"→"南边"）。含数字的短语不加，
            # 否则 "2026 年 3 月" 会被拆成 "3月" 这种弱要点。
            compact = re.sub(r"\s+", "", part)
            if (
                not re.search(r"\d", compact)
                and re.fullmatch(r"[\u4e00-\u9fff]+", compact)
                and len(compact) >= 3
                and not _is_meta(compact[-2:])
            ):
                out.append(compact[-2:])
            # 英文/数字/单位 token 只在"整段就是型号/数字"时单独收；
            # "SaaS 公司离职" 里的 SaaS 是背景词，不是答案。
            if re.fullmatch(r"[A-Za-z0-9 ._:+\-/%]+", part):
                for token in ALNUM_RE.findall(part):
                    token = token.strip()
                    if token and not _is_meta(token):
                        out.append(token)
    # 去重、保序
    seen, uniq = set(), []
    for item in out:
        key = _norm_match(item)
        if key and key not in seen:
            seen.add(key)
            uniq.append(item)
    return uniq


def extract_date_points(text: str, default_year: int | None = None) -> list[tuple[int, int, int | None]]:
    """抽取 (year, month, day) 日期要点；只保留有月份的（精确到年太弱）。"""
    text = _clean(text)
    if default_year is None:
        m = YEAR_RE.search(text)
        default_year = int(m.group(1)) if m else None
    out: list[tuple[int, int, int | None]] = []
    for m in DATE_RE.finditer(text):
        year, month, day = m.group(1), m.group(2), m.group(3)
        month_i = int(month)
        if not (1 <= month_i <= 12):
            continue
        year_i = int(year) if year else default_year
        if year_i is None:
            continue
        day_i = int(day) if day else None
        if day_i is not None and not (1 <= day_i <= 31):
            day_i = None
        out.append((year_i, month_i, day_i))
    # 去重（None 不能直接参与排序）
    return sorted(set(out), key=lambda item: (item[0], item[1], item[2] or 0))


def extract_negative_points(section: AnswerSection) -> set[str]:
    """从"不合格"里抽否定要点；含"和/都/与/及"的组合句跳过。

    原因：scenario 16 的"芒果和尘螨都过敏"是"答错组合"，不是"芒果错了"；
    直接拆开会把标准答案"芒果"误杀。单点否定（"答尘螨""尿酸偏高"）保留。
    """
    if section.cls in "CGJ":
        return set()
    out: set[str] = set()
    for clause in SPLIT_CLAUSE.split(section.unacceptable):
        if any(ch in clause for ch in "和都与及"):
            continue
        for frag in _fragments(clause):
            key = _norm_match(frag)
            if key:
                out.add(key)
    # 去掉"动词短语的尾部 2 字"这种派生否定：例如否定"吃香菜"会派生出
    # "香菜"，再拿它去删正向的"香菜"就把正确答案本身删了（scene 8）。
    pruned = set()
    for neg in out:
        derived = False
        for other in out:
            if other == neg:
                continue
            if neg in other:
                prefix = other[: other.index(neg)]
                if prefix and prefix[0] in NEG_LEAD_VERBS:
                    derived = True
                    break
        if not derived:
            pruned.add(neg)
    return pruned


NEG_LEAD_VERBS = (
    "吃", "喝", "用", "开", "坐", "买", "卖", "养", "叫", "住", "去", "来",
    "说", "答", "抽", "穿", "看", "听", "玩", "做", "在", "有", "是", "换",
    "搬", "调", "改", "选", "拿", "放", "带",
)


def is_positive_substring_of_negative(point: str, negatives: set[str]) -> bool:
    """判断一个正向要点是否应被"不合格"里的单点否定吃掉。

    关键区别：
    - 否定是**实体**（"速腾"/"光猫"/"B2 区"/"尿酸偏高"）→ 吃掉正向里的同名实体；
    - 否定是**动词短语**（"吃香菜"/"喝红茶"）→ 不能吃掉正向里的实体"香菜/红茶"，
      否则会把正确答案本身删掉（scene 8 的坑）。
    """
    key = _match_norm(point)
    if not key:
        return False
    for neg in negatives:
        if key == neg:
            return True
        if key in neg:
            prefix = neg[: neg.index(key)]
            if prefix and prefix[0] in NEG_LEAD_VERBS:
                continue
            return True
        if neg in key:
            return True
    return False


@dataclass
class AnswerPoints:
    sid: int
    cls: str
    content: list[str] = field(default_factory=list)
    dates: list[tuple[int, int, int | None]] = field(default_factory=list)
    source: str = "deterministic"
    negative_filtered: list[str] = field(default_factory=list)


def build_answer_points(
    section: AnswerSection, question: str
) -> AnswerPoints:
    default_year = None
    m = YEAR_RE.search(question) or YEAR_RE.search(section.standard)
    if m:
        default_year = int(m.group(1))

    negatives = extract_negative_points(section)
    content: list[str] = []
    filtered: list[str] = []
    for field_name in (section.standard, section.acceptable):
        for frag in _fragments(field_name):
            if section.cls not in "CGJ" and is_positive_substring_of_negative(frag, negatives):
                filtered.append(frag)
                continue
            content.append(frag)
    # 去重
    seen, uniq = set(), []
    for item in content:
        key = _norm_match(item)
        if key and key not in seen:
            seen.add(key)
            uniq.append(item)
    content = uniq
    dates = extract_date_points(
        section.standard + "\n" + section.acceptable, default_year=default_year
    )
    return AnswerPoints(
        sid=section.sid,
        cls=section.cls,
        content=content,
        dates=dates,
        negative_filtered=filtered,
    )


# ======================================================================
# 三、确定性匹配：正文 + 元数据
# ======================================================================
def _row_text(row: dict) -> str:
    return _match_norm(row.get("text") or "")


def _match_norm(text: str) -> str:
    """正文匹配归一：去空白 + 去掉结构助词"的"。

    "白色的 Model 3" 与答案要点 "白色 Model 3" 必须能对上；"成都的房子"
    与 "成都房子还留" 同理。只去"的"，不去"了/着/过"（那两个会改变时态）。
    """
    return _norm_match(text).replace("的", "")


def _match_content(point: str, row: dict) -> bool:
    key = _match_norm(point)
    if not key:
        return False
    hay = _row_text(row)
    if key in hay:
        return True
    # 谓词归一：只对确切的等价组生效
    for canonical, variants in PREDICATE_EQUIV.items():
        if key == _norm_match(canonical):
            if any(_norm_match(v) in hay for v in variants):
                return True
    # 数字/单位：允许 "5mg" 与 "5 mg"、"26 度" 与 "26度"（_norm_match 已去空格）
    return False


def _match_date(point: tuple[int, int, int | None], row: dict) -> bool:
    year, month, day = point
    if row.get("y") != year:
        return False
    if row.get("m") != month:
        return False
    if day is not None and row.get("d") != day:
        return False
    return True


def match_records(
    points: AnswerPoints, rows: list[dict]
) -> tuple[set[int], dict[int, dict]]:
    """返回 (证据行号集合, 每行匹配依据)。"""
    matched: set[int] = set()
    why: dict[int, dict] = {}
    content_hits: dict[int, list[str]] = defaultdict(list)
    date_hits: dict[int, list[tuple]] = defaultdict(list)

    # 每个要点的命中行；用来做"裸数字"去冗余（"一杯" vs "一杯低因"）。
    point_matches = {
        point: {
            index for index, row in enumerate(rows) if _match_content(point, row)
        }
        for point in points.content
    }
    dropped: set[str] = set()
    for point in points.content:
        # 只对裸计数/剂量去冗余：如果更长的要点（"一杯低因"）能命中，
        # 裸"一杯"就把"各一杯"这种旧记录也拉进来，属于过匹配。
        # 名词不做这个处理（"香菜"是泛实体，不能被"讨厌香菜"吃掉）。
        if not re.fullmatch(
            r"[0-9一二三四五六七八九十百两]+(?:杯|次|岁|个月|点|度|mg|个|家|人|天|年|周)",
            _norm_match(point),
        ):
            continue
        for other in points.content:
            if other == point:
                continue
            if (
                _norm_match(point) in _norm_match(other)
                and len(_norm_match(other)) >= len(_norm_match(point)) + 2
                and point_matches.get(other)
            ):
                dropped.add(point)
                break

    for index, row in enumerate(rows):
        for point in points.content:
            if point in dropped:
                continue
            if _match_content(point, row):
                content_hits[index].append(point)
        for point in points.dates:
            if _match_date(point, row):
                date_hits[index].append(point)
    # 正文要点优先；只有正文一个都没命中时才用日期要点，避免"2026 年"拉进
    # 同年的无关记录（scene 13/33 的典型过匹配）。
    used_dates = False
    if content_hits:
        for index, hits in content_hits.items():
            matched.add(index)
            why[index] = {"content": hits, "date": []}
    else:
        used_dates = True
        for index, hits in date_hits.items():
            matched.add(index)
            why[index] = {"content": [], "date": hits}
    # 正文命中时，日期只作为"同一行"的补充依据，不新增行
    if not used_dates:
        for index in list(matched):
            if date_hits.get(index):
                why[index]["date"] = date_hits[index]
    return matched, why


# ======================================================================
# 四、LLM 客户端 / 缓存 / 兜底抽取 / 判官
# ======================================================================
def load_key() -> str:
    raw = CRED.read_text(encoding="utf-8")
    m = re.search(r"(?m)^\s+DEEPSEEK_API_KEY:\s*(.+?)\s*$", raw)
    if not m:
        raise SystemExit("credentials 里没有 DEEPSEEK_API_KEY")
    return m.group(1).strip().strip('"').strip("'")


def setup_proxy() -> None:
    if not ENVFILE.exists():
        return
    for line in ENVFILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip().lower().endswith("_proxy") and v.strip():
            os.environ.setdefault(k.strip(), v.strip())


def _sha(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 坏缓存直接重建
        return {}


def _save_cache(path: Path, cache: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    os.replace(tmp, path)


def _usage_sum(usages: list[dict]) -> dict:
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for usage in usages:
        for key in total:
            total[key] += int((usage or {}).get(key) or 0)
    return total


def ask_llm(
    key: str,
    prompt: str,
    *,
    max_tokens: int = 2500,
    retries: int = 3,
) -> tuple[str, dict]:
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
    ).encode("utf-8")
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(
            URL,
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            text = (msg.get("content") or "").strip()
            usage = data.get("usage") or {}
            if text:
                return text, usage
            last = f"empty content (usage={usage})"
        except urllib.error.HTTPError as e:  # noqa: PERF203
            last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(2 * (attempt + 1))
    return f"<ERROR> {last}", {}


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return None


def llm_extract_points(
    key: str,
    cache: dict,
    sid: int,
    question: str,
    rows: list[dict],
    section: AnswerSection,
) -> tuple[list[str], dict]:
    """兜底：从题目 + 场景记录 + 判分要点里抽 1–3 个预期答案要点。"""
    cache_key = _sha(
        MODEL, PROMPT_VERSION, str(sid), question,
        section.standard, section.acceptable, section.unacceptable,
        json.dumps(rows, ensure_ascii=False, sort_keys=True),
    )
    if cache_key in cache:
        entry = cache[cache_key]
        return list(entry.get("points") or []), {
            "cached": True,
            "usage": entry.get("usage") or {},
        }
    records = "\n".join(
        f"[{row.get('raw_date')}] {row.get('text')}" for row in rows
    )
    prompt = (
        "你在为一份记忆问答评测做自动标注。只做一件事：从给定的判分要点里，"
        "抽出回答这个问题所必需的**答案要点**（实体、数字、日期、状态），"
        "不要写解释，不要判断哪条记录是证据。\n"
        "要求：\n"
        "1. 只输出 JSON，形如 {\"points\": [\"...\", \"...\"]}；\n"
        "2. 每个要点是一个短串（2–12 字），优先抄判分要点里的原文；\n"
        "3. 如果标准答案是“记忆里没有”，points 返回空数组；\n"
        "4. 最多 4 个要点，不要包含“无法确定/建议确认”这类判分用语。\n\n"
        f"【问题】\n{question}\n\n"
        f"【场景记录】\n{records}\n\n"
        f"【标准答案】\n{section.standard}\n"
        f"【可接受】\n{section.acceptable}\n"
        f"【不合格】\n{section.unacceptable}\n"
    )
    text, usage = ask_llm(key, prompt, max_tokens=1500)
    data = _extract_json(text) or {}
    points = [
        str(item).strip()
        for item in (data.get("points") or [])
        if str(item).strip()
    ][:4]
    cache[cache_key] = {
        "sid": sid,
        "points": points,
        "raw": text[:1000],
        "usage": usage,
        "model": MODEL,
        "prompt_version": PROMPT_VERSION,
    }
    return points, {"cached": False, "usage": usage}


JUDGE_CLASS_RULES = {
    "C": "C 类（晚但记错）：标准答案就是“无法确定”。没有 ✅；任何自信的具体答案记 ❌。",
    "G": "G 类（同日期冲突）：标准答案就是“无法确定”。没有 ✅；任何自信的具体答案记 ❌。",
    "E": "E 类（配额截断）：答案不在记忆里。明确说“记忆里没有”记 ✅；编造具体值/名称记 ❌。",
    "I": "I 类（无关干扰）：答案就在记忆里。说“记忆里没有”记 ❌，不能按 ⚠️ 处理。",
    "D": "D 类（历史信念）：问的是“当时以为”。答后来查明的事实记 ❌。",
    "J": "J 类（无日期对照）：答出倾向并指出记录无日期/无法确定先后记 ✅；"
         "自信给结论且不提日期缺失记 ❌；直接说无法确定记 ⚠️。",
    "H": "H 类（粗粒度日期）：年份/月份判断错误即 ❌。",
}


def llm_extract_judge_point(
    key: str,
    cache: dict,
    sid: int,
    question: str,
    row: dict,
    section: AnswerSection,
) -> dict:
    """对一条候选记录问：仅凭这条记录能否推出该答案。返回投票结果。"""
    cache_key = _sha(
        MODEL, JUDGE_PROMPT_VERSION, str(sid), row.get("text") or "",
        row.get("raw_date") or "", question, section.standard,
        section.acceptable, section.unacceptable,
    )
    if cache_key in cache and "err" not in cache[cache_key].get("votes", []):
        return cache[cache_key]
    class_rule = JUDGE_CLASS_RULES.get(
        section.cls,
        "A/B/F/H 类：答出标准答案记 ✅；指出冲突/不确定记 ⚠️；自信答错记 ❌。",
    )
    prompt = (
        "你是记忆问答评测的证据判官。只判断一条记录：**仅凭这条记录，"
        "能否推出/支撑回答这个问题所需的答案要点？**\n"
        "判 YES 的情形：这条记录直接给出答案实体/数字/日期/状态，"
        "或它是构成“记忆冲突”的必需一方。\n"
        "判 NO 的情形：它只是同场景的无关背景；它只提到答案词但说的是别的事；"
        "它需要和另一条记录合起来才能推出答案；问题问的实体在记录里不存在。\n"
        "注意：如果问题本身在记忆里无解（问的实体根本不存在），任何记录都判 NO。\n"
        f"类别规则：{class_rule}\n\n"
        f"【问题】\n{question}\n\n"
        f"【标准答案】\n{section.standard}\n"
        f"【可接受】\n{section.acceptable}\n"
        f"【不合格】\n{section.unacceptable}\n\n"
        f"【候选记录】\n[{row.get('raw_date')}] {row.get('text')}\n\n"
        "只输出 JSON：{\"evidence\": \"yes\" 或 \"no\", \"reason\": \"一句话\"}。"
    )
    votes: list[str] = []
    usages: list[dict] = []
    raws: list[str] = []
    for _ in range(3):
        text, usage = ask_llm(key, prompt, max_tokens=1500)
        data = _extract_json(text) or {}
        vote = str(data.get("evidence", "")).strip().lower()
        votes.append("yes" if vote.startswith("y") else "no" if vote.startswith("n") else "err")
        usages.append(usage)
        raws.append(text[:300])
    yes = votes.count("yes")
    result = {
        "sid": sid,
        "doc_text": row.get("text"),
        "raw_date": row.get("raw_date"),
        "votes": votes,
        "yes": yes,
        "no": votes.count("no"),
        "majority": "yes" if yes >= 2 else "no" if votes.count("no") >= 2 else "err",
        "confidence": round(yes / 3.0, 3),
        "usage": _usage_sum(usages),
        "raw": raws[0],
        "model": MODEL,
        "prompt_version": JUDGE_PROMPT_VERSION,
    }
    cache[cache_key] = result
    return result


# ======================================================================
# 五、检索（只读向量缓存，与产品 vector 单路同序）
# ======================================================================
def load_rankings(corpus: list[dict], queries: list[dict]) -> tuple[dict[int, list[int]], dict]:
    if not CACHE_PATH.exists():
        raise SystemExit(f"缓存不存在：{CACHE_PATH}")
    payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    model = payload.get("model")
    vectors = payload.get("vectors") or {}
    if not model or not vectors:
        raise SystemExit(f"缓存不完整：{CACHE_PATH}")
    doc_texts = [norm(row["text"]) for row in corpus]
    query_texts = [item["q"] for item in queries]
    keys = [_cache_key(model, text) for text in doc_texts + query_texts]
    missing = [
        text for key, text in zip(keys, doc_texts + query_texts) if key not in vectors
    ]
    if missing:
        raise SystemExit(
            f"缓存缺 {len(missing)} 条，例如 {missing[:2]!r}；本脚本不联网补算"
        )
    matrix = np.asarray([vectors[key] for key in keys], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix = matrix / norms
    doc_matrix = matrix[: len(corpus)]
    query_matrix = matrix[len(corpus) :]
    rankings: dict[int, list[int]] = {}
    for index, item in enumerate(queries):
        sims = doc_matrix @ query_matrix[index]
        rankings[item["sid"]] = [int(i) for i in np.argsort(-sims, kind="stable")]
    return rankings, {
        "cache_path": str(CACHE_PATH),
        "model": model,
        "dim": int(doc_matrix.shape[1]),
        "docs": len(corpus),
        "queries": len(queries),
        "cache_hits": len(keys),
        "api_calls": 0,
    }


def recall_at(ranking: list[int], evidence: set[int], k: int) -> float:
    if not evidence:
        return 0.0
    return len(set(ranking[:k]) & evidence) / len(evidence)


def macro_metrics(
    rankings: dict[int, list[int]],
    labels: dict[int, set[int]],
    *,
    skip_empty: bool = True,
) -> dict:
    rows = []
    total_ev, total_hit4, total_hit20 = 0, 0, 0
    for sid, evidence in sorted(labels.items()):
        if skip_empty and not evidence:
            continue
        ranking = rankings[sid]
        r4 = recall_at(ranking, evidence, 4)
        r20 = recall_at(ranking, evidence, 20)
        hit4 = len(set(ranking[:4]) & evidence)
        hit20 = len(set(ranking[:20]) & evidence)
        rows.append(
            {
                "sid": sid,
                "evidence_n": len(evidence),
                "r4": r4,
                "r20": r20,
                "hit4": hit4,
                "hit20": hit20,
                "evidence_in_top4": sorted(set(ranking[:4]) & evidence),
                "evidence_missing_top4": sorted(evidence - set(ranking[:4])),
            }
        )
        total_ev += len(evidence)
        total_hit4 += hit4
        total_hit20 += hit20
    return {
        "n_questions": len(rows),
        "evidence_pairs": total_ev,
        "r4_macro": float(np.mean([row["r4"] for row in rows])) if rows else None,
        "r20_macro": float(np.mean([row["r20"] for row in rows])) if rows else None,
        "r4_pair": total_hit4 / total_ev if total_ev else None,
        "r20_pair": total_hit20 / total_ev if total_ev else None,
        "hits4": total_hit4,
        "hits20": total_hit20,
        "per_question": rows,
    }


# ======================================================================
# 六、主流程
# ======================================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answer-key", default=DEFAULT_ANSWER_KEY)
    parser.add_argument("--json", default=str(DEFAULT_JSON))
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（试水）")
    parser.add_argument("--judge", action="store_true", help="开 LLM 判官（3 票，缓存）")
    parser.add_argument("--no-llm-extract", action="store_true", help="关兜底抽取")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args(argv)

    answer_path = Path(args.answer_key)
    if not answer_path.exists():
        raise SystemExit(f"答案文件不存在：{answer_path}")
    sections = parse_answer_key(answer_path)
    corpus, queries, _scenarios = load_inputs()
    if args.limit:
        queries = queries[: args.limit]
        keep = {item["sid"] for item in queries}
        sections = {sid: sec for sid, sec in sections.items() if sid in keep}

    rankings, cache_meta = load_rankings(corpus, queries)
    print(
        f"语料 {len(corpus)} 条 / 题 {len(queries)} 道；答案文件 {answer_path.name}；"
        f"缓存命中 {cache_meta['cache_hits']} 条（0 次 API 调用）"
    )

    # 场景记录：从题库的 mem 列表映射回 corpus；优先同 scenario
    by_norm: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(corpus):
        by_norm[norm(row["text"])].append(index)

    key = ""
    points_cache = _load_cache(CACHE_POINTS) if not args.no_llm_extract else {}
    judge_cache = _load_cache(CACHE_JUDGE) if args.judge else {}
    if args.judge or not args.no_llm_extract:
        setup_proxy()
        key = load_key()

    labels_strict: dict[int, set[int]] = {}
    labels_judge: dict[int, set[int]] = {}
    labels_union: dict[int, set[int]] = {}
    detail: dict[int, dict] = {}
    unanswerable: set[int] = set()
    fallback_used: list[int] = []
    judge_usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    extract_usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    for item in queries:
        sid = item["sid"]
        section = sections.get(sid)
        if section is None:
            print(f"  ! 场景 {sid} 在答案文件里没有对应段落，跳过")
            continue
        scene_indices = sorted(
            {
                min(
                    by_norm[norm(mem["text"])],
                    key=lambda i: (corpus[i].get("scenario") != sid, i),
                )
                for mem in next(s for s in _scenarios if s["sid"] == sid)["mems"]
            }
        )
        rows = [corpus[i] for i in scene_indices]

        points = build_answer_points(section, item["q"])
        is_e = section.cls == "E" or "记忆里没有" in section.standard
        if is_e:
            unanswerable.add(sid)

        matched, why = match_records(points, rows)
        strict_docs = {scene_indices[i] for i in matched}
        # 兜底：确定性要点为空、或一个记录都匹配不上时，用 LLM 抽一次要点再匹配。
        # 只抽一次；E 类不抽（答案本来就不在记忆里）。
        if not is_e and not args.no_llm_extract and not strict_docs:
            extra, meta = llm_extract_points(
                key, points_cache, sid, item["q"], rows, section
            )
            extract_usage_total = _usage_sum(
                [extract_usage_total, meta.get("usage") or {}]
            )
            if extra:
                points.content.extend(extra)
                points.source = "deterministic+llm_extract"
                fallback_used.append(sid)
                matched, why = match_records(points, rows)
                strict_docs = {scene_indices[i] for i in matched}

        # 判官：每条候选记录 3 票；不同记录并发，单条内部 3 票顺序（便于缓存/重试）
        judge_docs: set[int] = set()
        judge_rows: list[dict] = []
        if args.judge:
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                results = list(
                    pool.map(
                        lambda row: llm_extract_judge_point(
                            key, judge_cache, sid, item["q"], row, section
                        ),
                        rows,
                    )
                )
            for offset, result in enumerate(results):
                judge_rows.append(result)
                if result["majority"] == "yes":
                    judge_docs.add(scene_indices[offset])
                judge_usage_total = _usage_sum(
                    [judge_usage_total, result.get("usage") or {}]
                )
            _save_cache(CACHE_JUDGE, judge_cache)
        union_docs = strict_docs | judge_docs
        judge_docs_audit = set(judge_docs)
        if is_e:
            # E 类按定义没有证据；判官若仍标出记录，只留在 detail 里审计，
            # 不进入任何证据指标的分母。
            strict_docs = set()
            judge_docs = set()
            union_docs = set()
        labels_strict[sid] = strict_docs
        labels_judge[sid] = judge_docs
        labels_union[sid] = union_docs

        detail[sid] = {
            "sid": sid,
            "cls": section.cls,
            "cls_name": section.cls_name,
            "question": item["q"],
            "answer_points": {
                "content": points.content,
                "dates": [list(d) for d in points.dates],
                "source": points.source,
                "negative_filtered": points.negative_filtered,
            },
            "candidate_docs": [
                {
                    "doc": doc,
                    "scenario": corpus[doc].get("scenario"),
                    "raw_date": corpus[doc].get("raw_date"),
                    "text": corpus[doc]["text"],
                }
                for doc in scene_indices
            ],
            "strict": {
                "docs": sorted(strict_docs),
                "match_reasons": {
                    str(scene_indices[i]): why.get(i, {}) for i in why
                },
            },
            "judge": {
                "docs": sorted(judge_docs_audit),
                "votes": judge_rows,
            } if args.judge else None,
            "union_docs": sorted(union_docs),
            "unanswerable": is_e,
            "top4": rankings[sid][:4],
            "top4_detail": [
                {
                    "doc": doc,
                    "scenario": corpus[doc].get("scenario"),
                    "raw_date": corpus[doc].get("raw_date"),
                    "text": corpus[doc]["text"],
                    "strict_evidence": doc in strict_docs,
                    "judge_evidence": doc in judge_docs_audit,
                }
                for doc in rankings[sid][:4]
            ],
            "evidence_in_top4": sorted(set(rankings[sid][:4]) & union_docs),
        }

        if args.dry_run:
            print(f"\n场景 {sid} [{section.cls}] {item['q']}")
            print(f"  要点: {points.content}")
            print(f"  日期: {points.dates}")
            print(f"  候选: {scene_indices}")
            print(f"  strict: {sorted(strict_docs)}  依据: {why}")
            if args.judge:
                print(f"  judge: {sorted(judge_docs)}")

    # 保存缓存
    if not args.dry_run:
        if not args.no_llm_extract:
            _save_cache(CACHE_POINTS, points_cache)
        if args.judge:
            _save_cache(CACHE_JUDGE, judge_cache)

    # 指标：E 类不进入分母；同时给一个"包含 E 类（空集记 0）"的对照
    strict_metrics = macro_metrics(rankings, labels_strict)
    judge_metrics = macro_metrics(rankings, labels_judge) if args.judge else None
    union_metrics = macro_metrics(rankings, labels_union)
    strict_all = macro_metrics(
        rankings,
        {sid: (set() if sid in unanswerable else ev) for sid, ev in labels_strict.items()},
        skip_empty=False,
    )
    union_all = macro_metrics(
        rankings,
        {sid: (set() if sid in unanswerable else ev) for sid, ev in labels_union.items()},
        skip_empty=False,
    )

    # 与既有手工标注（rank_metrics.EVIDENCE）对照：只做参照，不参与判定
    manual_ref = None
    try:
        from rank_metrics import EVIDENCE  # type: ignore

        manual_labels = {sid: set(docs) for sid, docs in EVIDENCE.items()}
        manual_metrics = macro_metrics(rankings, manual_labels)
        manual_ref = {
            "note": "rank_metrics.py 里的手工 EVIDENCE，仅作参照，不参与本次标注",
            "metrics": manual_metrics,
            "strict_agreement_pairs": _pair_agreement(
                labels_strict, manual_labels, corpus
            ),
        }
    except Exception as exc:  # noqa: BLE001
        manual_ref = {"error": f"{type(exc).__name__}: {exc}"}

    # strict vs judge 一致率（只在判官开启时）
    agreement = None
    if args.judge:
        agreement = _agreement(labels_strict, labels_judge, detail, unanswerable)

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "supervision": audit_supervision(answer_path, sections),
        "method": {
            "strict": "答案要点（标准答案+可接受，去掉不合格里的单点否定）与正文+元数据匹配；"
                      "正文要点优先，正文零命中时才单用日期要点",
            "judge": f"deepseek-flash，温度 0，3 票多数；prompt={JUDGE_PROMPT_VERSION}" if args.judge else "未开启",
            "union": "strict ∪ judge",
            "unanswerable": sorted(unanswerable),
            "fallback_llm_extract": sorted(set(fallback_used)),
        },
        "cache": cache_meta,
        "metrics": {
            "strict": strict_metrics,
            "judge": judge_metrics,
            "union": union_metrics,
            "strict_all40_with_empty_as_zero": strict_all,
            "union_all40_with_empty_as_zero": union_all,
            "manual_reference": manual_ref,
        },
        "agreement": agreement,
        "usage": {
            "judge": judge_usage_total if args.judge else None,
            "fallback_extract": extract_usage_total,
        },
        "per_question": [detail[sid] for sid in sorted(detail)],
    }

    if not args.dry_run:
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    print("\n== 证据级指标（E 类不进分母） ==")
    for name, m in (
        ("strict", strict_metrics),
        ("judge", judge_metrics),
        ("union", union_metrics),
    ):
        if m is None:
            continue
        print(
            f"  {name:<7} n={m['n_questions']:2d}  pairs={m['evidence_pairs']:3d}  "
            f"R@4={m['r4_macro']:.4f} (pair {m['r4_pair']:.4f})  "
            f"R@20={m['r20_macro']:.4f} (pair {m['r20_pair']:.4f})"
        )
    if manual_ref and "metrics" in manual_ref:
        m = manual_ref["metrics"]
        print(
            f"  手工参照 R@4={m['r4_macro']:.4f} (pair {m['r4_pair']:.4f})  "
            f"R@20={m['r20_macro']:.4f} (pair {m['r20_pair']:.4f})"
        )
    if agreement:
        print(
            f"\n== strict vs judge 一致率 ==\n"
            f"  pair 级：一致 {agreement['pair_agree']}/{agreement['pair_total']} "
            f"= {agreement['pair_agree_rate']:.4f}；"
            f"strict-only {agreement['strict_only']} / judge-only {agreement['judge_only']}\n"
            f"  题级：集合完全相同 {agreement['same_set']}/{agreement['n_questions']}；"
            f"平均 Jaccard {agreement['mean_jaccard']:.4f}"
        )
        print(f"  分歧题：{agreement['disagreements']}")
    print(f"\nE 类无解：{sorted(unanswerable)}")
    print(f"兜底 LLM 抽取：{sorted(set(fallback_used)) or '未使用'}")
    if not args.dry_run:
        print(f"JSON 写入 {args.json}")
    return 0


def _pair_agreement(
    left: dict[int, set[int]], right: dict[int, set[int]], corpus: list[dict]
) -> dict:
    docs = set()
    for sid in left:
        docs |= left[sid] | right.get(sid, set())
    total = agree = 0
    for sid in left:
        # 只比两方候选并集，避免把全库 119 条都算成"一致否"
        for doc in left[sid] | right.get(sid, set()):
            total += 1
            if (doc in left[sid]) == (doc in right.get(sid, set())):
                agree += 1
    return {
        "pairs": total,
        "agree": agree,
        "rate": agree / total if total else None,
    }


def _agreement(
    strict: dict[int, set[int]],
    judge: dict[int, set[int]],
    detail: dict[int, dict],
    unanswerable: set[int],
) -> dict:
    pair_total = pair_agree = 0
    strict_only = judge_only = 0
    same = 0
    jaccards = []
    disagreements = []
    for sid in sorted(strict):
        s, j = strict[sid], judge.get(sid, set())
        # pair 级：只在该题候选记录上比
        candidates = {row["doc"] for row in detail[sid]["candidate_docs"]}
        for doc in candidates:
            pair_total += 1
            in_s, in_j = doc in s, doc in j
            if in_s == in_j:
                pair_agree += 1
            elif in_s:
                strict_only += 1
            else:
                judge_only += 1
        if s == j:
            same += 1
        union = s | j
        jaccards.append(len(s & j) / len(union) if union else 1.0)
        if s != j:
            disagreements.append(
                {
                    "sid": sid,
                    "strict_only": sorted(s - j),
                    "judge_only": sorted(j - s),
                    "unanswerable": sid in unanswerable,
                }
            )
    return {
        "pair_total": pair_total,
        "pair_agree": pair_agree,
        "pair_agree_rate": pair_agree / pair_total if pair_total else None,
        "strict_only": strict_only,
        "judge_only": judge_only,
        "n_questions": len(strict),
        "same_set": same,
        "mean_jaccard": float(np.mean(jaccards)) if jaccards else None,
        "disagreements": disagreements,
        "note": "只计数，不裁决；strict-only/judge-only 不自动改判",
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(errors="replace")
    raise SystemExit(main())
