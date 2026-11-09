import asyncio

import pytest

from homehub.config import load_config, parse_config
from homehub.tools import result_texts
from homehub.tools.tv import OUTPUTS, PNG_END, SYSTEM_APPS, Bravia, dechunk, fetch_icon, TvError, TvPanel, best_match, build_module, load_settings, simple

INPUTS = [{"title": "HDMI 1", "label": "PC", "uri": "extInput:hdmi?port=1"}, {"title": "HDMI 3 (eARC/ARC)", "label": "", "uri": "extInput:hdmi?port=3"},
          {"title": "HDMI 4", "label": "", "uri": "extInput:hdmi?port=4"}, {"title": "HW-QS750F", "label": "", "uri": "extInput:cec?port=3"}]
APPS = [{"title": "YouTube", "uri": "a"}, {"title": "YouTube Music", "uri": "b"}, {"title": "YouTube TV", "uri": "c"}, {"title": "Disney+", "uri": "d"},
        {"title": "Timers &amp; Clock", "uri": "e"}, {"title": "Prime Video", "uri": "f"}]


def test_names_match_titles_and_labels_loosely():
    assert simple("Disney Plus") == "disneyplus" and simple("Disney+") == "disney+" and simple("Timers &amp; Clock") == "timersandclock"
    assert best_match("pc", INPUTS)["uri"] == "extInput:hdmi?port=1"
    assert best_match("hdmi 4", INPUTS)["uri"] == "extInput:hdmi?port=4"
    assert best_match("HDMI 3", INPUTS)["uri"] == "extInput:hdmi?port=3"
    assert best_match("youtube", APPS)["uri"] == "a" and best_match("youtube tv", APPS)["uri"] == "c"
    assert best_match("prime", APPS)["uri"] == "f" and best_match("disney+", APPS)["uri"] == "d"
    assert best_match("hulu", APPS) is None and best_match("", APPS) is None


def test_the_tool_is_hidden_until_the_tv_is_set_up(tmp_path):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    assert load_settings(cfg) is None and not build_module(cfg).available(cfg)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secrets.toml").write_text('[tv]\npsk = "1234"\nmac = "02-00-00-00-00-02"\n', encoding="utf-8")
    assert load_settings(cfg) == {"psk": "1234", "mac": "02:00:00:00:00:02", "host": ""} and build_module(cfg).available(cfg)


@pytest.mark.live
def test_the_real_tv_reports_power_inputs_apps_and_remote_keys():
    cfg = load_config()
    if load_settings(cfg) is None:
        pytest.skip("no [tv] in secrets.toml here")
    tv = Bravia(cfg)
    assert tv.power() in ("active", "standby")
    assert any(i["uri"].startswith("extInput:hdmi") for i in tv.inputs())
    assert any(a["title"] == "Netflix" for a in tv.apps())
    assert {"volumeup", "volumedown", "mute", "home"} <= set(tv.key_codes())
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg).tools}
    assert result_texts(asyncio.run(tools["tv_status"]({})))[0].startswith("The TV is ")
    missing = result_texts(asyncio.run(tools["tv_input"]({"name": "hdmi 9"})))[0]
    assert missing.startswith("No input matches 'hdmi 9'. Inputs: ")


@pytest.mark.live
def test_the_real_tv_panel_reads_everything_and_rewrites_settings_to_what_they_already_are():
    cfg = load_config()
    if load_settings(cfg) is None:
        pytest.skip("no [tv] in secrets.toml here")
    panel = TvPanel(cfg)
    view = panel.overview()
    assert view["inputs"] and all(i["uri"].startswith("extInput:hdmi") for i in view["inputs"])
    titles = [a["title"] for a in view["apps"]]
    assert "Netflix" in titles and not SYSTEM_APPS & set(titles) and len(titles) == len(set(titles))
    if not view["on"]:
        with pytest.raises(TvError, match="The TV is off"):
            panel.act({"action": "picture_mode", "value": "vivid"})
        return
    with pytest.raises(TvError, match="unknown TV action"):
        panel.act({"action": "format"})
    if view["picture"] is None:
        assert view["settling"]
        pytest.skip("the TV is still waking up")
    picture = view["picture"]
    assert picture["mode"] in {m["value"] for m in picture["modes"]} and view["output"] in OUTPUTS
    assert panel.act({"action": "picture_mode", "value": picture["mode"]})["text"].startswith("Picture mode: ")
    if picture["brightness"] is not None:
        assert panel.act({"action": "brightness", "value": picture["brightness"]})["text"] == f"Brightness {picture['brightness']}."
    assert panel.act({"action": "output", "value": view["output"]})["text"].startswith("Sound from the ")
    after = panel.overview()["picture"]
    assert (after["mode"], after["brightness"]) == (picture["mode"], picture["brightness"])
    for bad in ({"action": "picture_mode", "value": "rainbow"}, {"action": "brightness", "value": 99}, {"action": "output", "value": "headphones"},
                {"action": "input", "uri": "extInput:hdmi?port=9"}, {"action": "app", "uri": "com.nothing"}):
        with pytest.raises(TvError):
            panel.act(bad)


def test_chunked_bodies_are_joined_and_an_unfinished_stream_keeps_what_arrived():
    png = b"\x89PNG\r\n\x1a\n" + b"data" * 10 + PNG_END
    raw = b"%x\r\n%s\r\n%x\r\n%s\r\n" % (12, png[:12], len(png) - 12, png[12:])
    assert dechunk(raw + b"0\r\n\r\n") == png and dechunk(raw) == png and dechunk(b"zz\r\nnope") == b""
    with pytest.raises(TvError, match="won't follow"):
        fetch_icon("file:///etc/passwd")


@pytest.mark.live
def test_the_real_tv_hands_over_app_icons_though_it_never_ends_the_download():
    cfg = load_config()
    if load_settings(cfg) is None:
        pytest.skip("no [tv] in secrets.toml here")
    icons = TvPanel(cfg).act({"action": "icons"})["icons"]
    assert icons and all(i.startswith("data:image/png;base64,iVBOR") for i in icons.values())
