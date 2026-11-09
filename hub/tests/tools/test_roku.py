import asyncio
import json

import pytest

from homehub.config import load_config, parse_config
from homehub.tools import result_texts
from homehub.tools.router import RouterReader
from homehub.tools.roku import KEYS, LIMITED, Locator, Roku, RokuError, RokuPanel, build_module, clock, find_app, key_name, parse_devices

APPS = [{"title": "Disney Plus", "uri": "291097"}, {"title": "HBO Max", "uri": "61322"}, {"title": "Prime Video", "uri": "13"},
        {"title": "Paramount Plus", "uri": "31440"}, {"title": "Peacock TV", "uri": "593099"}, {"title": "YouTube", "uri": "837"},
        {"title": "Tubi - Free Movies & TV", "uri": "41468"}]


def tools_for(tmp_path, devices):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}, "roku": {"devices": devices}})
    module = build_module(cfg)
    return cfg, module, {spec.name: spec.fn.handler for spec in module.tools}


def test_devices_are_name_equals_address():
    assert parse_devices(["Downstairs Roku=192.168.1.26", " Bonus room = 192.168.1.40 "]) == {
        "Downstairs Roku": "192.168.1.26", "Bonus room": "192.168.1.40"}
    assert parse_devices(["Downstairs Roku=02-00-00-00-00-06"]) == {"Downstairs Roku": "02:00:00:00:00:06"}
    for bad in (["192.168.1.26"], ["=192.168.1.26"], ["Roku="]):
        with pytest.raises(ValueError):
            parse_devices(bad)


def test_buttons_take_roku_names_and_everyday_words():
    assert key_name("select") == "Select" and key_name("OK") == "Select" and key_name("play pause") == "Play"
    assert key_name("back") == "Back" and key_name("return") == "Back" and key_name("fast forward") == "Fwd"
    assert key_name("mute") == "VolumeMute" and key_name("VolumeUp") == "VolumeUp" and key_name("replay") == "InstantReplay"
    assert key_name("Lit_a") is None and key_name("PowerOffNow") is None
    assert all(key_name(k) == k for k in KEYS)


def test_app_names_match_with_plus_spelled_either_way():
    assert find_app("Disney+", APPS)["uri"] == "291097" and find_app("disney plus", APPS)["uri"] == "291097"
    assert find_app("Paramount+", APPS)["uri"] == "31440" and find_app("max", APPS)["uri"] == "61322"
    assert find_app("prime", APPS)["uri"] == "13" and find_app("peacock", APPS)["uri"] == "593099" and find_app("tubi", APPS)["uri"] == "41468"
    assert find_app("crunchyroll", APPS) is None and find_app("", APPS) is None


def test_playback_times_read_like_a_clock():
    assert clock(5) == "0:05" and clock(1487) == "24:47" and clock(3725) == "1:02:05"


def test_the_tools_are_hidden_until_a_roku_is_listed(tmp_path):
    cfg, module, _ = tools_for(tmp_path, [])
    assert not module.available(cfg)
    cfg, module, _ = tools_for(tmp_path, ["Downstairs Roku=192.168.1.26"])
    assert module.available(cfg)


def test_with_two_rokus_the_tools_ask_which_and_refuse_unknown_names_and_buttons(tmp_path):
    _, _, tools = tools_for(tmp_path, ["Downstairs Roku=127.0.0.1", "Bonus Room Roku=127.0.0.1"])
    assert result_texts(asyncio.run(tools["roku_status"]({})))[0] == "Which Roku? There are Downstairs Roku, Bonus Room Roku."
    assert result_texts(asyncio.run(tools["roku_status"]({"roku": "garage"})))[0] == (
        "No Roku called garage. There are Downstairs Roku, Bonus Room Roku.")
    assert result_texts(asyncio.run(tools["roku_remote"]({"roku": "downstairs", "keys": ["Explode"]})))[0].startswith(
        "The Roku remote has no Explode button. Buttons: Home, Back,")
    assert result_texts(asyncio.run(tools["roku_type"]({"roku": "bonus", "text": "x" * 101})))[0] == "Give one to 100 characters to type."
    assert result_texts(asyncio.run(tools["roku_search"]({"roku": "bonus", "text": "x" * 101})))[0] == "Search for one to 100 characters."
    assert result_texts(asyncio.run(tools["roku_search"]({"roku": "bonus", "text": "   "})))[0] == "Search for one to 100 characters."


def test_a_roku_that_isnt_there_says_so(tmp_path):
    _, _, tools = tools_for(tmp_path, ["Downstairs Roku=127.0.0.1"])
    text = result_texts(asyncio.run(tools["roku_status"]({})))[0]
    assert text.startswith("The Downstairs Roku didn't answer at 127.0.0.1")


def test_a_roku_listed_by_mac_needs_the_router_to_find_it(tmp_path):
    _, _, tools = tools_for(tmp_path, ["Downstairs Roku=02:00:00:00:00:06"])
    assert result_texts(asyncio.run(tools["roku_status"]({})))[0] == (
        "The Downstairs Roku is listed by its MAC address, and the router isn't set up to look it up.")


def test_a_roku_found_once_is_remembered_when_the_router_is_gone_or_replaced(tmp_path):
    cfg, _, _ = tools_for(tmp_path, ["Downstairs Roku=02:00:00:00:00:06"])
    path = tmp_path / "roku-addresses.json"
    path.write_text(json.dumps({"02:00:00:00:00:06": "192.168.1.26"}))
    assert Locator(RouterReader(cfg), path).address("Downstairs Roku", "02:00:00:00:00:06") == "192.168.1.26"
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secrets.toml").write_text('[router]\nurl = "http://127.0.0.1:9"\npassword = "x"\n', encoding="utf-8")
    assert Locator(RouterReader(cfg), path).address("Downstairs Roku", "02:00:00:00:00:06") == "192.168.1.26"
    with pytest.raises(RokuError, match="couldn't ask the router where the Upstairs Roku is"):
        Locator(RouterReader(cfg), path).address("Upstairs Roku", "AA:BB:CC:DD:EE:FF")


@pytest.mark.live
def test_the_real_roku_reports_its_state_and_apps_without_pressing_anything():
    cfg = load_config()
    if not cfg.roku.devices:
        pytest.skip("no [roku] devices in this config")
    name, host = next(iter(parse_devices(cfg.roku.devices).items()))
    saved = cfg.data_dir() / "roku-addresses.json"
    roku = Roku(name, host, Locator(RouterReader(cfg), saved))
    try:
        info = roku.info()
    except RokuError as e:
        pytest.skip(f"the Roku isn't reachable from here: {e}")
    assert info["model"] and info["power"]
    if host.count(":") == 5:
        assert json.loads(saved.read_text())[host] == roku.host
    if info["control"] == "limited":
        with pytest.raises(RokuError, match="Network access"):
            roku.apps()
        assert LIMITED in describe_text(cfg)
        return
    apps = roku.apps()
    assert len(apps) > 3 and all(a["uri"].isdigit() for a in apps)
    assert describe_text(cfg).startswith(f"The {name} ({info['model']}) is ")
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg).tools}
    missing = result_texts(asyncio.run(tools["roku_app"]({"name": "zzz not an app"})))[0]
    assert missing.startswith(f"The {name} has no app matching 'zzz not an app'. Apps: ")


def describe_text(cfg):
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg).tools}
    return result_texts(asyncio.run(tools["roku_status"]({})))[0]


def test_the_panel_says_when_no_roku_is_set_up(tmp_path):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    with pytest.raises(RokuError, match="No Roku is set up"):
        RokuPanel(cfg).overview()


@pytest.mark.live
def test_the_real_roku_panel_lists_apps_with_their_icons_and_refuses_the_rest():
    cfg = load_config()
    if not cfg.roku.devices:
        pytest.skip("no Roku set up here")
    panel = RokuPanel(cfg)
    try:
        view = panel.overview()
    except RokuError as e:
        pytest.skip(f"the Roku isn't answering: {e}")
    if view["limited"]:
        pytest.skip("the Roku only allows limited control")
    assert view["apps"] and all(a["id"] and a["title"] for a in view["apps"])
    assert view["active"] is None or view["active"]["id"]
    icons = panel.act({"action": "icons"})["icons"]
    assert set(icons) == {a["id"] for a in view["apps"]} and all(i.startswith("data:image/") for i in icons.values())
    for bad, message in (({"action": "key", "key": "PowerOff"}, "isn't on the app's Roku remote"), ({"action": "launch", "id": "nope"}, "has no such app"),
                         ({"action": "type", "text": "x" * 101}, "Type one to"), ({"action": "search", "text": " "}, "Search for one to"),
                         ({"action": "key", "key": "Search"}, "isn't on the app's Roku remote"), ({"action": "nuke"}, "unknown Roku action")):
        with pytest.raises(RokuError, match=message):
            panel.act(bad)
