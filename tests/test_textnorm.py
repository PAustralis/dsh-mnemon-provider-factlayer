"""R2 文本构造：NFKC、零宽、单字+双字、标点/Latin/数字、查询构造器冻结。"""

from app import textnorm

from tests.retrieval_helpers import add_memory, build_derived, make_service


def test_nfkc_fullwidth_to_halfwidth():
    assert textnorm.normalize("ＡＢＣ１２３") == "ABC123"
    assert textnorm.normalize("ｉＰｈｏｎｅ１５") == "iPhone15"
    assert textnorm.normalize("２０２６年") == "2026年"


def test_zero_width_chars_removed():
    raw = "喝\u200b茶\u200d\u2060\ufeff"
    assert textnorm.normalize(raw) == "喝茶"
    assert textnorm.bigram_query_tokens(raw) == ["喝茶"]


def test_ngram_tokens_singles_and_bigrams_per_position():
    assert textnorm.ngram_tokens("我爱北京") == [
        "我",
        "爱",
        "北",
        "京",
        "我爱",
        "爱北",
        "北京",
    ]


def test_single_char_cjk_segment():
    assert textnorm.ngram_tokens("药") == ["药"]
    assert textnorm.ngram_tokens("吃药") == ["吃", "药", "吃药"]


def test_latin_and_digit_runs_are_single_tokens():
    assert textnorm.ngram_tokens("iPhone15 型号") == ["iphone15", "型", "号", "型号"]
    assert textnorm.ngram_tokens("版本 v2.1.3") == [
        "版",
        "本",
        "版本",
        "v2.1.3",
    ]


def test_punctuation_not_tokenized_and_blocks_crossing_bigrams():
    assert textnorm.segments("你好，世界！") == [("cjk", "你好"), ("cjk", "世界")]
    # 不跨标点拼双字
    assert textnorm.ngram_tokens("你好，世界") == [
        "你",
        "好",
        "你好",
        "世",
        "界",
        "世界",
    ]


def test_query_builder_matches_frozen_baseline():
    # recall_eval.py bigrams_of 的行为：CJK 段 ≥2 只取双字，Latin 小写
    assert textnorm.bigram_query_tokens("吃药了吗") == ["吃药", "药了", "了吗"]
    assert textnorm.bigram_query_tokens("药") == ["药"]
    assert textnorm.bigram_query_tokens("iPhone15 型号") == ["iphone15", "型号"]
    # 标点切段、不跨段拼双字
    assert textnorm.bigram_query_tokens("你好，世界") == ["你好", "世界"]


def test_unigram_query_tokens():
    assert textnorm.unigram_query_tokens("吃药了吗") == ["吃", "药", "了", "吗"]
    assert textnorm.unigram_query_tokens("iPhone15 型号") == [
        "iphone15",
        "型",
        "号",
    ]


def test_fts_match_expr_dedupes_and_strips_quotes():
    assert textnorm.fts_match_expr(["a", "a", 'b"c', ""]) == '"a" OR "bc"'
    assert textnorm.fts_match_expr([]) is None


def test_trigram_tokens():
    assert textnorm.trigram_tokens("我们明天去") == ["我们明", "们明天", "明天去"]
    assert textnorm.trigram_tokens("明天") == []
    assert textnorm.trigram_tokens("hello") == ["hello"]


# ---- 端到端：中文 1 字 / 2 字 / 标点 / Latin / 数字 / NFKC / 零宽 -------------


def _seed_db(db, contents: list[str], session_id: str = "s1") -> list[str]:
    ids = [add_memory(db, content, session_id=session_id) for content in contents]
    build_derived(db)
    return ids


def test_one_and_two_char_chinese_queries_hit(db):
    contents = ["明天去北京出差", "周末在家喝茶", "2026 年上半年我在上海"]
    ids = _seed_db(db, contents)
    service = make_service(db, routes=("bigram",))

    for query in ("京", "北京", "出差"):
        result = service.search(query)
        assert ids[0] in [item["id"] for item in result["items"]], query

    one_char = service.search("茶")
    assert ids[1] in [item["id"] for item in one_char["items"]]


def test_mixed_punctuation_latin_digits(db):
    ids = _seed_db(db, ["2026 年上半年我在上海，用 iPhone15 记录"])
    service = make_service(db, routes=("bigram",))

    for query in ("上海", "iPhone15", "２０２６", "上半年，用"):
        hit = [i for i in service.search(query)["items"] if i["id"] == ids[0]]
        assert hit, query


def test_zero_width_query_hits_clean_text(db):
    ids = _seed_db(db, ["我们周末一起去喝茶"])
    service = make_service(db, routes=("bigram",))
    result = service.search("喝\u200b茶")
    assert ids[0] in [item["id"] for item in result["items"]]
