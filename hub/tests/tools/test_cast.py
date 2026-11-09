import asyncio

import pytest

from homehub.config import load_config, parse_config
from homehub.tools import result_texts
from homehub.tools.cast import Caster, build_module, content_type, search_youtube, video_id


def test_video_ids_and_media_types_are_read_from_links():
    assert video_id("https://www.youtube.com/watch?v=jfKfPfyJRdk&t=10") == "jfKfPfyJRdk"
    assert video_id("https://youtu.be/jfKfPfyJRdk") == "jfKfPfyJRdk" and video_id("jfKfPfyJRdk") == "jfKfPfyJRdk"
    assert video_id("lofi hip hop radio") is None
    assert content_type("http://hub:8096/Videos/1/stream.mkv?x=1") == "video/x-matroska" and content_type("http://a/b.mp3") == "audio/mpeg"


def test_casting_stays_off_until_a_tv_is_named():
    cfg = parse_config({})
    assert not build_module(cfg).available(cfg)
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg).tools}
    assert "isn't set up" in result_texts(asyncio.run(tools["cast_status"]({})))[0]
    assert build_module(parse_config({"cast": {"device": "Bedroom TV"}})).available(parse_config({"cast": {"device": "Bedroom TV"}}))


@pytest.mark.live
def test_youtube_search_finds_a_video():
    vid, title = search_youtube("lofi hip hop radio beats to relax")
    assert len(vid) == 11 and title


@pytest.mark.live
def test_the_real_tv_reports_its_cast_status():
    cfg = load_config()
    if not cfg.cast.device:
        pytest.skip("no [cast] device in this config")
    assert Caster(cfg).status().startswith(f"{cfg.cast.device} is running ")
