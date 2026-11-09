import asyncio
import time

import pytest

from homehub import household
from homehub.config import load_config
from homehub.household import sync_household
from homehub.tools import CallContext, current_context, result_texts
from homehub.tools.weather import CONDITIONS, build_module, describe, find_place, forecast, format_report


def test_weather_codes_read_as_words():
    assert describe(0) == "clear" and describe(61) == "light rain" and describe(95) == "thunderstorms"
    assert describe(1234) == "mixed conditions"
    assert all(isinstance(k, int) and v for k, v in CONDITIONS.items())


@pytest.mark.live
def test_places_are_found_by_town_and_state_or_country():
    springfield = find_place("Springfield, Illinois")
    assert abs(springfield["latitude"] - 39.80) < 0.2 and abs(springfield["longitude"] + 89.65) < 0.3
    assert springfield["label"] == "Springfield, Illinois"
    assert find_place("springfield,  IL")["label"] == "Springfield, Illinois"
    assert abs(find_place("Springfield, IL")["latitude"] - 39.8) < 0.3
    assert abs(find_place("Springfield, Missouri")["latitude"] - 37.2) < 0.3
    assert abs(find_place("Paris, France")["latitude"] - 48.85) < 0.2
    assert find_place("Qzxqzxqzx Nowhere") is None


@pytest.mark.live
def test_forecast_is_fast_and_units_agree():
    place = find_place("Springfield, Illinois")
    started = time.perf_counter()
    imperial, u_imp = forecast(place, "imperial", 3)
    assert time.perf_counter() - started < 5
    metric, u_met = forecast(place, "metric", 3)
    high_f, high_c = imperial["daily"]["temperature_2m_max"][0], metric["daily"]["temperature_2m_max"][0]
    assert abs(high_f - (high_c * 9 / 5 + 32)) < 1.5
    assert -40 < high_f < 130 and len(imperial["daily"]["time"]) == 3
    report = format_report(place, imperial, u_imp)
    lines = report.splitlines()
    assert lines[0].startswith("Springfield, Illinois right now: ") and "°F" in lines[0]
    assert lines[1].startswith("Today (") and lines[2].startswith("Tomorrow (") and len(lines) == 4
    assert "°C" in format_report(place, metric, u_met) and "km/h" in format_report(place, metric, u_met)


@pytest.mark.live
def test_the_tool_uses_the_household_town_and_names_unknown_places(temp_storage, tmp_path):
    people = tmp_path / "people.toml"
    people.write_text(household.EXAMPLE_PATH.read_text(encoding="utf-8").replace("[household]\n", "[household]\ntown = \"Springfield, Illinois\"\n", 1),
                      encoding="utf-8")
    sync_household(load_config(), temp_storage, people)
    tools = {spec.name: spec.fn.handler for spec in build_module(temp_storage).tools}
    token = current_context.set(CallContext(room="bedroom", conversation_id="c1", person_id="alex", person_name="Alex", role="owner",
                                            confidence="vpn_key"))
    try:
        home = result_texts(asyncio.run(tools["get_weather"]({})))[0]
        elsewhere = result_texts(asyncio.run(tools["get_weather"]({"place": "Paris, France", "days": 1})))[0]
        missing = result_texts(asyncio.run(tools["get_weather"]({"place": "Qzxqzxqzx Nowhere"})))[0]
    finally:
        current_context.reset(token)
    assert home.startswith("Springfield, Illinois right now: ") and "°F" in home
    assert elsewhere.startswith("Paris") and len(elsewhere.splitlines()) == 2
    assert "couldn't find a place called Qzxqzxqzx Nowhere" in missing


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.slow
def test_claude_answers_weather_from_the_tool_not_a_web_search(harness, temp_storage):
    h = harness([build_module(temp_storage)])

    async def scenario():
        async with h:
            started = time.perf_counter()
            reply = await h.ask("What's the weather going to be like in Springfield, Illinois today?")
            return reply, h.tools_called(), time.perf_counter() - started

    reply, called, seconds = asyncio.run(scenario())
    print(f"\n  weather -> {called} in {seconds:.1f} s\n      reply: {reply}")
    assert "get_weather" in called and "WebSearch" not in called
    assert len(reply.split()) <= 45 and seconds < 10
