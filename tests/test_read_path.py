"""读取路径：按会话列表截断 + 标注；按 id 取全文永不截断。"""

from tests.conftest import write_one

LONG = "字" * 3000
SHORT = "短内容"


def test_list_output_truncated_with_marker(client):
    resp = write_one(client, cwid="cw-long", content=LONG, session_id="s1")
    mid = resp.json()["id"]
    body = client.get("/memories", params={"session_id": "s1"}).json()
    item = body["items"][0]
    assert item["content_truncated"] is True
    assert item["content"].startswith("字" * 1200)
    assert "[已截断，原文 3000 字，id=" in item["content"]
    assert mid in item["content"]


def test_short_content_not_marked(client):
    write_one(client, cwid="cw-short", content=SHORT, session_id="s1")
    body = client.get("/memories", params={"session_id": "s1"}).json()
    item = body["items"][0]
    assert item["content"] == SHORT
    assert item["content_truncated"] is False


def test_get_by_id_returns_full_text(client):
    resp = write_one(client, cwid="cw-full", content=LONG, session_id="s1")
    mid = resp.json()["id"]
    full = client.get(f"/memories/{mid}")
    assert full.status_code == 200
    assert full.json()["content"] == LONG  # 全文，不截断


def test_get_by_id_404(client):
    assert client.get("/memories/nope").status_code == 404


def test_list_ordered_by_seq_and_paginates(client):
    for i in range(5):
        write_one(client, cwid=f"cw-ord{i}", content=f"m{i}", session_id="s-ord")
    page1 = client.get("/memories", params={"session_id": "s-ord", "limit": 3}).json()
    assert [it["seq"] for it in page1["items"]] == [0, 1, 2]
    page2 = client.get(
        "/memories", params={"session_id": "s-ord", "limit": 3, "offset": 3}
    ).json()
    assert [it["seq"] for it in page2["items"]] == [3, 4]
