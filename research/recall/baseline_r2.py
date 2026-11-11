# -*- coding: utf-8 -*-
"""R2 二元组查询构造器基线回归（设计 §3.1 / §6 第 1 条）。

用途：
1. **查询构造器冻结校验**：出厂 ``app.textnorm.bigram_query_tokens`` 必须与
   ``recall_eval.py`` 的 ``bigrams_of`` 在 40 题 + 119 条语料上**逐 token 相同**。
2. **改前/改后各跑一次基线**：
   - ``prototype``：原型实现（recall_eval.py 的生成 + 查询 + FTS 检索），
     复现 REPORT 的 R2 单路 R@6 = 0.4875（本文编号 R2 = REPORT 的 R3）。
   - ``factory``：**出厂检索实现**（app.schema 建库 → write_path 写入 →
     derive stage=fts 生成 ngrams/写 FTS5 → retrieval.bigram_route），
     断言 R@6 ≥ 0.44（= 0.4875 − 0.05 噪声带宽）。

三处实现都**不联网**、不下载模型；数据路径写死在下方常量（与 recall_eval.py 一致，
语料在另一个 checkout 下，本仓库刻意不复制）。

用法：
    python baseline_r2.py                 # 跑两种实现并断言
    python baseline_r2.py --routes bigram # 出厂实现单路（默认）
    python baseline_r2.py --record-meta facts.db --routes bigram,trigram
        # 额外跑一次「出厂融合」，把 {best_single_route, best_single_r6, fusion_r6}
        # 写进该库 meta:retrieval:eval（retrieval 自动降级据此判定）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPO_ROOT = BASE.parent.parent
sys.path.insert(0, str(REPO_ROOT))

CORPUS = os.environ.get(
    "FACTLAYER_BASELINE_CORPUS",
    r"E:\Tools\projects\dsh-mnemon-provider-graphiti\research\phase2\corpus.json",
)
QBANK = os.environ.get(
    "FACTLAYER_BASELINE_QBANK",
    r"C:\Users\ZhaoY\Desktop\读者测试40-题库.md",
)

TOP_K = 50
FINAL_K = 6
ASSERT_R6 = 0.44

# ---------------------------------------------------------------- 原型参考实现
# 以下 5 个函数逐字取自 research/recall/recall_eval.py（norm/segments/bigrams_of/
# q_bigram/fts_match_expr/rank_fts），仅用于「改前」基线，不参与出厂代码路径。
CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
RE_SEG = re.compile(r"[%s]+|[A-Za-z0-9]+(?:[.\-_][A-Za-z0-9]+)*" % CJK)
RE_CJK_RUN = re.compile(r"[%s]+" % CJK)


def proto_norm(s):
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u3000", " ")
    return re.sub(r"\s+", " ", s).strip()


def proto_segments(s):
    out = []
    for m in RE_SEG.finditer(s):
        seg = m.group(0)
        out.append(("cjk" if RE_CJK_RUN.fullmatch(seg) else "latin", seg))
    return out


def proto_bigrams_of(s):
    toks = []
    for kind, seg in proto_segments(s):
        if kind == "cjk":
            if len(seg) == 1:
                toks.append(seg)
            else:
                toks.extend(seg[i : i + 2] for i in range(len(seg) - 1))
        else:
            toks.append(seg.lower())
    return toks


def proto_fts_match_expr(terms):
    seen, uniq = set(), []
    for t in terms:
        t = t.replace('"', "")
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return " OR ".join('"%s"' % t for t in uniq) if uniq else None


def proto_rank_fts(con, table, expr, limit=TOP_K):
    if not expr:
        return []
    sql = (
        "SELECT rowid FROM %s WHERE %s MATCH ? ORDER BY bm25(%s) ASC, rowid ASC LIMIT ?"
        % (table, table, table)
    )
    return [r[0] for r in con.execute(sql, (expr, limit)).fetchall()]


# ---------------------------------------------------------------- 题库 / 语料
def parse_qbank(path):
    raw = open(path, encoding="utf-8").read().replace("\r\n", "\n")
    blocks = re.split(r"^## 场景\s*(\d+)\s*$", raw, flags=re.M)
    scenarios = []
    for i in range(1, len(blocks), 2):
        sid, body = int(blocks[i]), blocks[i + 1]
        qm = re.search(r"\*\*用户问\*\*\s*[：:]\s*(.+)", body)
        question = qm.group(1).strip() if qm else ""
        code = re.search(r"```\s*\n(.*?)```", body, flags=re.S)
        mems = []
        if code:
            for line in code.group(1).split("\n"):
                line = line.strip()
                if not line:
                    continue
                m = re.match(r"^\[([^\]]+)\]\s*(.*)$", line)
                mems.append(
                    {
                        "date_label": m.group(1).strip() if m else None,
                        "text": (m.group(2) if m else line).strip(),
                    }
                )
        scenarios.append({"sid": sid, "question": question, "mems": mems})
    return scenarios


def load_inputs():
    if not Path(CORPUS).exists() or not Path(QBANK).exists():
        raise SystemExit(
            "缺少基线数据（另一个 checkout 下的 119 条语料 / 40 题题库）：\n"
            f"  CORPUS={CORPUS}\n  QBANK={QBANK}\n"
            "换机器时用 FACTLAYER_BASELINE_CORPUS / FACTLAYER_BASELINE_QBANK 指路。"
        )
    corpus = json.load(open(CORPUS, encoding="utf-8"))
    scenarios = parse_qbank(QBANK)
    by_norm = defaultdict(list)
    for index, row in enumerate(corpus):
        by_norm[proto_norm(row["text"])].append(index)
    queries, unmatched = [], []
    for scenario in scenarios:
        gold = set()
        for mem in scenario["mems"]:
            candidates = by_norm.get(proto_norm(mem["text"]))
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
                "query": proto_norm(scenario["question"]),
                "gold": sorted(gold),
            }
        )
    return corpus, queries, unmatched


def recall_at_6(ranking, gold):
    gold = set(gold)
    if not gold:
        return 0.0
    return len(set(ranking[:FINAL_K]) & gold) / len(gold)


# ---------------------------------------------------------------- 改前：原型
def run_prototype(corpus, queries):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "prototype.sqlite")
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA journal_mode=OFF")
        con.execute(
            "CREATE TABLE docs(docid INTEGER PRIMARY KEY, bg TEXT)"
        )
        con.execute(
            "CREATE VIRTUAL TABLE fts_bi USING fts5(bg, tokenize='unicode61')"
        )
        for i, row in enumerate(corpus):
            bg = " ".join(proto_bigrams_of(proto_norm(row["text"])))
            con.execute("INSERT INTO docs VALUES (?,?)", (i, bg))
            con.execute("INSERT INTO fts_bi(rowid, bg) VALUES (?,?)", (i, bg))
        con.commit()
        scores = []
        for item in queries:
            expr = proto_fts_match_expr(proto_bigrams_of(item["query"]))
            ranking = proto_rank_fts(con, "fts_bi", expr)
            scores.append(recall_at_6(ranking, item["gold"]))
        con.close()
    return sum(scores) / len(scores), scores


# ---------------------------------------------------------------- 改后：出厂实现
def build_factory_db(corpus):
    from app.db import Database
    from app.derive import DerivePipeline
    from app.schema import init_db
    from app.write_path import create_memory

    tmpdir = tempfile.TemporaryDirectory()
    db = Database(os.path.join(tmpdir.name, "factory.db"))
    init_db(db)
    ids: list[str] = []
    for i, row in enumerate(corpus):
        receipt = create_memory(
            db,
            client_write_id=f"baseline-{i}",
            content=row["text"],
            session_id=f"scenario-{row.get('scenario')}-{i}",
            source_client="baseline",
        )
        ids.append(receipt["id"])
    pipeline = DerivePipeline(db, stub_success=True)
    for _ in range(10):
        if pipeline.pending_count("fts") == 0:
            break
        pipeline.tick()
    assert pipeline.pending_count("fts") == 0, "fts 水位未补齐"
    return db, ids, tmpdir


def run_factory(corpus, queries, routes="bigram"):
    from dataclasses import replace

    from app.config import load_config
    from app.retrieval import RetrievalService

    db, ids, tmpdir = build_factory_db(corpus)
    try:
        cfg = replace(
            load_config(),
            retrieval_routes=tuple(routes.split(",")),
            route_top_k=TOP_K,
            final_k=FINAL_K,
            expand_max_items=FINAL_K,  # 基线只量「融合取 6」，不含会话展开
        )
        service = RetrievalService(db, cfg)
        id_to_index = {memory_id: i for i, memory_id in enumerate(ids)}
        scores = []
        empty = 0
        for item in queries:
            if cfg.route_names == ("bigram",):
                outcome = service.bigram_route(item["query"], TOP_K)
                ranking = [
                    id_to_index[mid]
                    for mid in outcome.raw_ids
                    if mid in id_to_index
                ]
            else:
                result = service.search(item["query"])
                ranking = [
                    id_to_index[row["id"]]
                    for row in result["items"]
                    if row["id"] in id_to_index
                ]
            empty += 0 if ranking else 1
            scores.append(recall_at_6(ranking, item["gold"]))
        return sum(scores) / len(scores), scores, empty
    finally:
        db.close()
        tmpdir.cleanup()


# ---------------------------------------------------------------- 冻结校验
def check_query_builder(corpus, queries):
    from app import textnorm

    mismatches = []
    for item in queries:
        # 查询侧：原型 bigrams_of 允许重复，真正进 FTS 的是去重后的表达式
        tokens, _truncated = textnorm.query_tokens(item["query"])
        factory = textnorm.fts_match_expr(tokens)
        proto = proto_fts_match_expr(proto_bigrams_of(item["query"]))
        if factory != proto:
            mismatches.append((item["sid"], item["query"], proto, factory))
    if mismatches:
        return mismatches
    # 索引侧：单混合列必须与原型 bigrams_of 逐 token 相同
    for row in corpus:
        factory = textnorm.ngram_tokens(row["text"])
        proto = proto_bigrams_of(proto_norm(row["text"]))
        if factory != proto:
            mismatches.append(
                ("<corpus>", row["text"][:40], proto[:8], factory[:8])
            )
            break
    return mismatches


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--routes", default="bigram", help="出厂实现的路线（逗号分隔），默认 bigram 单路"
    )
    parser.add_argument("--skip-prototype", action="store_true")
    parser.add_argument(
        "--record-meta",
        default=None,
        help="把评测快照写入该 SQLite 库的 meta:retrieval:eval（供自动降级判定）",
    )
    args = parser.parse_args(argv)

    corpus, queries, unmatched = load_inputs()
    print(f"docs={len(corpus)} queries={len(queries)} unmatched={len(unmatched)}")

    mismatches = check_query_builder(corpus, queries)
    if mismatches:
        print("查询构造器与原型不一致：")
        for row in mismatches[:5]:
            print("  ", row)
        raise SystemExit(1)
    print("查询构造器冻结校验：PASS（40 题逐 token 相同 + 语料双字 token 相同）")

    if not args.skip_prototype:
        proto_r6, _ = run_prototype(corpus, queries)
        print(f"[改前/原型] R2 单路 R@6 = {proto_r6:.4f}")
    factory_r6, _, empty = run_factory(corpus, queries, routes=args.routes)
    print(f"[改后/出厂] routes={args.routes} R@6 = {factory_r6:.4f}（空结果题 {empty}）")
    assert factory_r6 >= ASSERT_R6, (
        f"出厂 R2 单路 R@6={factory_r6:.4f} < 验收线 {ASSERT_R6}"
    )
    print(f"验收断言：R@6 {factory_r6:.4f} ≥ {ASSERT_R6} —— PASS")

    if args.record_meta:
        # fusion 需与单路同口径测量；这里只记录 --routes 指定的融合结果
        fusion_r6 = factory_r6 if len(args.routes.split(",")) > 1 else None
        payload = {
            "best_single_route": "bigram",
            "best_single_r6": round(factory_r6, 6),
            "fusion_r6": round(fusion_r6, 6) if fusion_r6 is not None else None,
            "routes": args.routes,
            "corpus_size": len(corpus),
            "query_count": len(queries),
        }
        if fusion_r6 is None:
            print("--record-meta 需要 --routes 至少两路才有 fusion_r6，已跳过写库")
        else:
            con = sqlite3.connect(args.record_meta)
            con.execute(
                "INSERT INTO meta(key, value) VALUES('retrieval:eval', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(payload, ensure_ascii=False),),
            )
            con.commit()
            con.close()
            print(f"已写入 {args.record_meta} meta:retrieval:eval = {payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
