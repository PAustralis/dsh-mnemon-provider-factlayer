"""R2 文本构造（v8 §3）：NFKC、零宽、单混合列基线 bigrams、查询构造器冻结。"""

from app import textnorm

from tests.retrieval_helpers import add_memory, build_derived, make_service


def test_nfkc_fullwidth_to_halfwidth():
    assert textnorm.normalize("ＡＢＣ１２３") == "ABC123"
    assert textnorm.normalize("ｉＰｈｏｎｅ１５") == "iPhone15"
    assert textnorm.normalize("２０２６年") == "2026年"


def test_zero_width_chars_removed():
    raw = "喝\u200b茶\u200d\u2060\ufeff"
    assert textnorm.normalize(raw) == "喝茶"
    assert textnorm.bigrams_of(raw) == ["喝茶"]


def test_index_side_is_exactly_baseline_bigrams_of():
    # CJK len>1 只产双字，不产单字；Latin 整段小写；全部同一列
    assert textnorm.ngram_tokens("我爱北京") == ["我爱", "爱北", "北京"]
    assert textnorm.ngram_text("我爱北京") == "我爱 爱北 北京"


def test_single_char_cjk_segment_only_when_segment_len_is_one():
    assert textnorm.ngram_tokens("药") == ["药"]
    # “吃药”是 len==2 的 CJK 段 → 只产双字（不产单字）
    assert textnorm.ngram_tokens("吃药") == ["吃药"]


def test_latin_and_digit_runs_are_single_tokens():
    assert textnorm.ngram_tokens("iPhone15 型号") == ["iphone15", "型号"]
    assert textnorm.ngram_tokens("版本 v2.1.3") == ["版本", "v2.1.3"]


def test_punctuation_not_tokenized_and_blocks_crossing_bigrams():
    assert textnorm.segments("你好，世界！") == [("cjk", "你好"), ("cjk", "世界")]
    assert textnorm.ngram_tokens("你好，世界") == ["你好", "世界"]


def test_query_builder_matches_frozen_baseline():
    # recall_eval.py bigrams_of：CJK 段 ≥2 只取双字，Latin 小写
    assert textnorm.query_tokens("吃药了吗")[0] == ["吃药", "药了", "了吗"]
    assert textnorm.query_tokens("药")[0] == ["药"]
    assert textnorm.query_tokens("iPhone15 型号")[0] == ["iphone15", "型号"]
    assert textnorm.query_tokens("你好，世界")[0] == ["你好", "世界"]


def test_query_token_limit_200_and_truncation_count():
    tokens = [f"t{i}" for i in range(250)]
    uniq, truncated = textnorm.unique_tokens(tokens)
    assert len(uniq) == 200
    assert truncated == 50
    assert textnorm.fts_match_expr(tokens) is not None


def test_fts_match_expr_escapes_quotes_and_never_builds_empty():
    assert textnorm.fts_match_expr(["a", "a", 'b"c', ""]) == '"a" OR "b""c"'
    assert textnorm.fts_match_expr([]) is None
    assert textnorm.fts_match_expr(["", " "]) is None


def test_trigram_tokens():
    assert textnorm.trigram_tokens("我们明天去") == ["我们明", "们明天", "明天去"]
    assert textnorm.trigram_tokens("明天") == []
    assert textnorm.trigram_tokens("hello") == ["hello"]


# ---- 端到端：中文 1 字 / 2 字 / 标点 / Latin / 数字 / NFKC / 零宽 -------------


def _seed_db(db, contents: list[str], session_id: str = "s1") -> list[str]:
    ids = [add_memory(db, content, session_id=session_id) for content in contents]
    build_derived(db)
    return ids


def test_two_char_and_longer_chinese_queries_hit(db):
    contents = ["明天去北京出差", "周末在家喝茶", "2026 年上半年我在上海"]
    ids = _seed_db(db, contents)
    service = make_service(db, routes=("bigram",))

    for query in ("北京", "出差", "北京出"):
        result = service.search(query)
        assert ids[0] in [item["id"] for item in result["items"]], query

    assert ids[1] in [item["id"] for item in service.search("喝茶")["items"]]


def test_one_char_query_cannot_hit_bigram_index(db):
    """基线只在 CJK 段长度恰为 1 时产单字；「京」查不到「北京」的双字索引。"""
    ids = _seed_db(db, ["明天去北京出差"])
    result = make_service(db, routes=("bigram",)).search("京")
    assert result["items"] == []
    assert result["hint"].startswith("NO_MATCH")
    assert ids[0] not in [item["id"] for item in result["items"]]


def test_mixed_punctuation_latin_digits(db):
    ids = _seed_db(db, ["2026 年上半年我在上海，用 iPhone15 记录"])
    service = make_service(db, routes=("bigram",))

    for query in ("上海", "iPhone15", "２０２６", "上半年"):
        hit = [i for i in service.search(query)["items"] if i["id"] == ids[0]]
        assert hit, query


def test_zero_width_query_hits_clean_text(db):
    ids = _seed_db(db, ["我们周末一起去喝茶"])
    result = make_service(db, routes=("bigram",)).search("喝\u200b茶")
    assert ids[0] in [item["id"] for item in result["items"]]


def test_punctuation_only_query_is_no_match_without_fts_error(db):
    _seed_db(db, ["北京记录"])
    result = make_service(db, routes=("bigram",)).search("，。！？")
    assert result["items"] == []
    assert result["hint"].startswith("NO_MATCH")
    assert result["token_count"] == 0


def test_fts_syntax_characters_do_not_raise_or_change_meaning(db):
    ids = _seed_db(db, ["NEAR AND NOT 只是普通词"])
    result = make_service(db, routes=("bigram",)).search('NEAR AND NOT * "')
    # 不报错；被包成短语后要么命中普通词，要么 NO_MATCH，绝不触发 FTS5 语法错误
    assert isinstance(result["items"], list)
    assert ids[0] in [item["id"] for item in result["items"]]
