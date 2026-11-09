import asyncio
import urllib.parse

import pytest

from homehub.config import load_config, parse_config
from homehub.tools import result_texts
from homehub.tools.spotify import NOT_SET_UP, Spotify, SpotifyError, build_module, load_app, ready, token_path


def world(tmp_path, secrets_text=None):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    if secrets_text is not None:
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "secrets.toml").write_text(secrets_text, encoding="utf-8")
    return cfg


def test_spotify_stays_off_until_the_app_and_sign_in_exist(tmp_path):
    cfg = world(tmp_path)
    assert load_app(cfg) is None and not ready(cfg) and not build_module(cfg).available(cfg)
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg).tools}
    assert result_texts(asyncio.run(tools["spotify_now"]({})))[0] == NOT_SET_UP
    cfg = world(tmp_path, '[spotify]\nclient_id = "abc"\nclient_secret = "def"\n')
    assert load_app(cfg)["redirect_uri"] == "http://127.0.0.1:8888/callback" and not ready(cfg)


def test_the_sign_in_link_asks_for_playback_scopes(tmp_path):
    cfg = world(tmp_path, '[spotify]\nclient_id = "abc"\nclient_secret = "def"\n')
    query = urllib.parse.parse_qs(urllib.parse.urlparse(Spotify(cfg).auth_url("xyz")).query)
    assert query["client_id"] == ["abc"] and query["response_type"] == ["code"] and query["state"] == ["xyz"]
    assert "user-modify-playback-state" in query["scope"][0] and query["redirect_uri"] == ["http://127.0.0.1:8888/callback"]


@pytest.mark.live
def test_a_bad_sign_in_is_refused_by_spotify_with_a_readable_message(tmp_path):
    cfg = world(tmp_path, '[spotify]\nclient_id = "not-a-real-client"\nclient_secret = "nope"\n')
    with pytest.raises(SpotifyError, match="Spotify refused the sign-in"):
        Spotify(cfg).login("http://127.0.0.1:8888/callback?code=bogus&state=x")
    with pytest.raises(SpotifyError, match="no sign-in code"):
        Spotify(cfg).login("http://127.0.0.1:8888/callback?error=access_denied")
    assert not token_path(cfg).exists()


@pytest.mark.live
def test_the_real_account_reports_what_is_playing():
    cfg = load_config()
    if not ready(cfg):
        pytest.skip("Spotify isn't connected here")
    assert Spotify(cfg).now().startswith(("Playing:", "Paused:", "Nothing is playing"))


@pytest.mark.live
def test_your_own_playlists_are_found_by_name_before_searching_the_catalog():
    from homehub.config import load_config
    from homehub.tools.spotify import Spotify, ready

    cfg = load_config()
    if not ready(cfg):
        pytest.skip("Spotify isn't connected here")
    spotify = Spotify(cfg)
    mine = spotify.my_playlists()
    if not mine:
        pytest.skip("this account has no playlists")
    name = mine[0]["name"]
    assert spotify.own_playlist(name.upper())["id"] == mine[0]["id"]
    assert spotify.own_playlist("no playlist is called this 9f3a") is None
