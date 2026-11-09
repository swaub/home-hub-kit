import asyncio
import dataclasses

import pytest

from homehub.tools import CallContext, Registry, current_context, result_texts
from homehub.tools.home_assistant import NOT_SET_UP, HomeAssistant, build_module, describe_state, match_entities, slug, to_fahrenheit

STATES = [
    {"entity_id": "light.bedroom_lamp", "state": "on", "attributes": {"friendly_name": "Bedroom Lamp", "brightness": 128}},
    {"entity_id": "light.kitchen_strip", "state": "off", "attributes": {"friendly_name": "Kitchen Strip"}},
    {"entity_id": "sensor.bedroom_thermometer_temperature", "state": "20.5", "attributes": {"friendly_name": "Bedroom Thermometer Temperature", "unit_of_measurement": "°C", "device_class": "temperature"}},
    {"entity_id": "sensor.bedroom_thermometer_humidity", "state": "41", "attributes": {"friendly_name": "Bedroom Thermometer Humidity", "unit_of_measurement": "%", "device_class": "humidity"}},
    {"entity_id": "media_player.bedroom_tv", "state": "off", "attributes": {"friendly_name": "Bedroom TV"}},
]
BEDROOM = ["light.bedroom_lamp", "sensor.bedroom_thermometer_temperature", "sensor.bedroom_thermometer_humidity", "media_player.bedroom_tv"]


def text(result):
    return result_texts(result)[0]


def call(tools, name, args, room="bedroom"):
    token = current_context.set(CallContext(room=room, person_id="alex"))
    try:
        return asyncio.run(tools[name](args))
    finally:
        current_context.reset(token)


def test_matching_and_wording():
    assert [s["entity_id"] for s in match_entities(STATES, "light", BEDROOM)] == ["light.bedroom_lamp"]
    assert [s["entity_id"] for s in match_entities(STATES, "light", [])] == ["light.bedroom_lamp", "light.kitchen_strip"]
    assert [s["entity_id"] for s in match_entities(STATES, "light", [], "kitchen")] == ["light.kitchen_strip"]
    assert match_entities(STATES, "light", BEDROOM, "garage") == []
    assert [s["entity_id"] for s in match_entities(STATES, "light", ["sensor.other"], "")] == ["light.bedroom_lamp", "light.kitchen_strip"]
    assert describe_state(STATES[0]) == "Bedroom Lamp: on at 50%" and describe_state(STATES[2]) == "Bedroom Thermometer Temperature: 20.5 °C"
    assert to_fahrenheit("20.5", "°C") == (68.9, "°F") and to_fahrenheit("72", "°F") == (72.0, "°F") and to_fahrenheit("unknown", "°C") == ("unknown", "°C")
    assert slug("Bedroom Lamp!") == "bedroom_lamp"


def test_tools_say_when_home_assistant_is_not_set_up(example_config):
    module = build_module(example_config, HomeAssistant("", ""))
    tools = {spec.name: spec.fn.handler for spec in module.tools}
    specs = {spec.name: spec for spec in module.tools}
    assert set(specs) == {"set_light", "read_sensor", "home_devices", "run_home_service"}
    assert specs["run_home_service"].policy == "confirm" and specs["run_home_service"].describe({"domain": "switch", "service": "toggle", "entity_id": "switch.fan"}) == "run switch.toggle on switch.fan"
    assert specs["set_light"].policy == "auto" and specs["set_light"].scope.kind == "room"
    for name, args in (("set_light", {"state": "off"}), ("read_sensor", {"what": "temperature"}), ("home_devices", {}), ("run_home_service", {"domain": "light", "service": "toggle"})):
        reply = call(tools, name, args)
        assert reply["is_error"] is True and text(reply) == NOT_SET_UP, name
    assert module.is_available(example_config) is False
    on = dataclasses.replace(example_config, features=dataclasses.replace(example_config.features, home_assistant=True))
    assert module.is_available(on) is True and "mcp__home__set_light" in Registry(on, [module]).specs


def test_unreachable_server_gives_a_plain_error(example_config):
    client = HomeAssistant("http://127.0.0.1:1", "token-for-nothing")
    assert client.enabled
    module = build_module(example_config, client)
    tools = {spec.name: spec.fn.handler for spec in module.tools}
    reply = call(tools, "set_light", {"state": "on", "brightness": 40})
    assert reply["is_error"] is True and "not reachable" in text(reply)
    reply = call(tools, "read_sensor", {"what": "temperature", "room": "Bedroom"})
    assert reply["is_error"] is True and "not reachable" in text(reply)
    assert call(tools, "read_sensor", {"what": "mood"})["is_error"] is True
    assert call(tools, "set_light", {"state": "dim"})["is_error"] is True


@pytest.mark.live
def test_live_home_assistant_reads_states(example_config, laptop_config):
    from homehub.config import load_config
    from homehub.tools.home_assistant import load_token

    cfg = load_config()
    if not cfg.features.home_assistant or not cfg.home_assistant.url:
        pytest.skip("Home Assistant is not configured here")
    client = HomeAssistant(cfg.home_assistant.url, load_token(cfg))
    assert client.ok()["message"] == "API running."
    states = client.states()
    assert isinstance(states, list) and states
    print(f"\n  {len(states)} entities; lights: {[s['entity_id'] for s in states if s['entity_id'].startswith('light.')][:5]}")
