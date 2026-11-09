import asyncio

import pytest

from homehub.config import parse_config
from homehub.tools import result_texts
from homehub.tools.lights import LightError, allowed, build_module, load_login, pick


def test_only_listed_lights_can_be_controlled():
    cfg = parse_config({"lights": {"devices": ["Bedroom Light", "Desk Lamp"]}})
    assert allowed(cfg, "bedroom light") and not allowed(cfg, "Kitchen Light")
    names = ["Kitchen Light", "Bedroom Light", "Desk Lamp"]
    assert pick(cfg, names, "bedroom") == "Bedroom Light" and pick(cfg, names, "desk lamp") == "Desk Lamp"
    with pytest.raises(LightError, match="No light called kitchen. Lights: Bedroom Light, Desk Lamp."):
        pick(cfg, names, "kitchen")


def test_lights_stay_off_until_some_are_listed(tmp_path):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    assert not build_module(cfg).available(cfg) and load_login(cfg) is None
    on = parse_config({"lights": {"devices": ["Bedroom Light"]}})
    assert build_module(on).available(on)


@pytest.mark.live
def test_discovery_with_no_listed_lights_finds_none(tmp_path):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}, "lights": {"devices": ["Nonexistent Lamp"]}})
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg).tools}
    assert result_texts(asyncio.run(tools["lights_status"]({})))[0] == "None of the listed lights answered; are they on the Wi-Fi?"
