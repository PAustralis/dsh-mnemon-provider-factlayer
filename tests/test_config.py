""".env 解析：成对引号去掉，且不覆盖已存在的环境变量。"""

import os

from app.config import _load_dotenv


def test_dotenv_strips_quotes(tmp_path, monkeypatch):
    env_file = tmp_path / "test.env"
    env_file.write_text(
        'FACTLAYER_TEST_A="double quoted value"\n'
        "FACTLAYER_TEST_B='single quoted value'\n"
        "FACTLAYER_TEST_C=plain\n"
        'FACTLAYER_TEST_D=""\n'
        'FACTLAYER_TEST_E="has=equals"\n'
        "# comment\n"
        "FACTLAYER_TEST_F=value # not a comment, keep inline\n",
        encoding="utf-8",
    )
    keys = [f"FACTLAYER_TEST_{c}" for c in "ABCDEF"]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    try:
        _load_dotenv(str(env_file))
        assert os.environ["FACTLAYER_TEST_A"] == "double quoted value"
        assert os.environ["FACTLAYER_TEST_B"] == "single quoted value"
        assert os.environ["FACTLAYER_TEST_C"] == "plain"
        assert os.environ["FACTLAYER_TEST_D"] == ""
        assert os.environ["FACTLAYER_TEST_E"] == "has=equals"
        assert os.environ["FACTLAYER_TEST_F"] == "value # not a comment, keep inline"
    finally:
        for key in keys:
            os.environ.pop(key, None)


def test_dotenv_does_not_override_existing_env(tmp_path, monkeypatch):
    env_file = tmp_path / "test.env"
    env_file.write_text('FACTLAYER_TEST_EXISTING="from file"\n', encoding="utf-8")
    monkeypatch.setenv("FACTLAYER_TEST_EXISTING", "from env")
    _load_dotenv(str(env_file))
    assert os.environ["FACTLAYER_TEST_EXISTING"] == "from env"
