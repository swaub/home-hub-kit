import asyncio

import pytest

from homehub.config import load_config, parse_config
from homehub.tools import result_texts
from homehub.tools.movies import NOT_SET_UP, Tmdb, build_module, load_key


def test_movie_lookup_is_off_until_a_key_is_saved(tmp_path):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    assert load_key(cfg) == "" and not build_module(cfg).available(cfg)
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg).tools}
    assert result_texts(asyncio.run(tools["movie_lookup"]({"title": "Dune"})))[0] == NOT_SET_UP
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secrets.toml").write_text('[tmdb]\napi_key = "abc123"\n', encoding="utf-8")
    assert load_key(cfg) == "abc123" and build_module(cfg).available(cfg)


@pytest.mark.live
def test_the_real_tmdb_answers_lookups():
    cfg = load_config()
    if not load_key(cfg):
        pytest.skip("no [tmdb] api_key in secrets.toml here")
    tmdb = Tmdb(cfg)
    assert tmdb.lookup("The Matrix").startswith("The Matrix (1999, movie)")
    assert tmdb.providers("Breaking Bad").startswith("Breaking Bad: ")
    assert tmdb.now("trending").count(";") >= 3
