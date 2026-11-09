import asyncio
import urllib.parse

import pytest

from homehub.config import load_config, parse_config
from homehub.tools import result_texts
from homehub.tools.jellyfin import NOT_SET_UP, Jellyfin, build_module, label, load_settings, stream_url

SETTINGS = {"url": "http://192.168.1.250:8096", "api_key": "k123", "user": "alex"}


def test_items_are_named_for_speech():
    assert label({"Type": "Movie", "Name": "Dune", "ProductionYear": 2021}) == "Dune (2021)"
    assert label({"Type": "Movie", "Name": "Clip (2026)", "ProductionYear": 2026}) == "Clip (2026)"
    assert label({"Type": "Episode", "Name": "Pilot", "SeriesName": "Lost", "ParentIndexNumber": 1, "IndexNumber": 1}) == "Lost season 1 episode 1, Pilot"
    assert label({"Type": "Audio", "Name": "Song", "Artists": ["A", "B", "C"]}) == "Song by A, B"


def test_streams_are_tv_friendly():
    video = urllib.parse.urlparse(stream_url(SETTINGS, {"Type": "Movie", "Id": "abc"}))
    assert video.path == "/Videos/abc/master.m3u8"
    query = urllib.parse.parse_qs(video.query)
    assert query["ApiKey"] == ["k123"] and "api_key" not in query and query["VideoCodec"] == ["h264"] and query["TranscodingMaxAudioChannels"] == ["2"]
    audio = urllib.parse.urlparse(stream_url(SETTINGS, {"Type": "Audio", "Id": "s1"}))
    assert audio.path == "/Audio/s1/universal" and urllib.parse.parse_qs(audio.query)["TranscodingContainer"] == ["mp3"]


def test_jellyfin_is_off_until_its_key_is_saved_and_play_needs_casting(tmp_path):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    assert load_settings(cfg) is None and not build_module(cfg).available(cfg)
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg).tools}
    assert result_texts(asyncio.run(tools["jellyfin_recent"]({})))[0] == NOT_SET_UP
    assert "needs casting" in result_texts(asyncio.run(tools["jellyfin_play"]({"query": "Dune"})))[0]


@pytest.mark.live
def test_the_real_server_answers_with_its_key():
    cfg = load_config()
    if load_settings(cfg) is None:
        pytest.skip("no [jellyfin] in secrets.toml here")
    server = Jellyfin(cfg)
    assert isinstance(server.recent(), list) and isinstance(server.search("a"), list)


def test_the_app_page_shows_cards_with_progress_and_knows_where_it_can_play(tmp_path):
    from homehub.tools.jellyfin import JellyfinError, JellyfinPanel, poster_source, web_address

    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")},
                        "remote": {"apps": ["Immich=http://10.77.0.1:2283", "Jellyfin (movies)=http://jellyfin.home/"]}})
    panel = JellyfinPanel(cfg)
    episode = {"Id": "e1", "Type": "Episode", "Name": "Pilot", "SeriesName": "Lost", "ParentIndexNumber": 1, "IndexNumber": 2,
               "RunTimeTicks": 25_800_000_000, "UserData": {"PlayedPercentage": 41.6}}
    card = panel.card(episode)
    assert card == {"id": "e1", "kind": "episode", "title": "Lost", "detail": "S01E02 · Pilot", "minutes": 43, "progress": 42, "watched": False,
                    "poster": None, "rating": "", "score": None}
    movie = panel.card({"Id": "m1", "Type": "Movie", "Name": "Dune", "ProductionYear": 2021, "UserData": {"Played": True}, "CommunityRating": 8.1})
    assert movie["detail"] == "2021" and movie["watched"] is True and movie["score"] == 8.1
    assert poster_source({"Id": "m1", "ImageTags": {"Primary": "t"}}) == ("m1", "t")
    assert poster_source({"Id": "e1", "ImageTags": {}, "SeriesId": "s1", "SeriesPrimaryImageTag": "st"}) == ("s1", "st")
    assert poster_source({"Id": "x", "ImageTags": {}}) is None
    assert web_address(cfg, SETTINGS) == "http://jellyfin.home"
    assert web_address(parse_config({"paths": {"data_dir": str(tmp_path / "d")}}), SETTINGS) == SETTINGS["url"]
    assert panel.targets() == [] and panel.act({"action": "search", "query": "  "}) == {"results": []}
    with pytest.raises(JellyfinError, match="unknown Jellyfin action"):
        panel.act({"action": "delete"})
    with pytest.raises(JellyfinError, match="Nothing to stop there"):
        panel.act({"action": "stop", "target": "cast"})
    with pytest.raises(JellyfinError, match="isn't set up"):
        panel.overview()


@pytest.mark.live
def test_the_real_library_page_has_posters_and_details():
    from homehub.tools.jellyfin import JellyfinPanel

    cfg = load_config()
    if load_settings(cfg) is None:
        pytest.skip("no [jellyfin] in secrets.toml here")
    panel = JellyfinPanel(cfg)
    view = panel.overview()
    assert set(view["counts"]) == {"movies", "shows", "episodes", "songs"} and view["web"].startswith("http")
    if not view["latest"]:
        pytest.skip("the library is empty")
    first = view["latest"][0]
    assert first["poster"] is None or first["poster"].startswith("data:image/")
    details = panel.act({"action": "details", "id": first["id"]})
    assert details["id"] == first["id"] and "overview" in details
    assert any(r["id"] == first["id"] for r in panel.act({"action": "search", "query": first["title"]})["results"])
