import asyncio

import pytest

from homehub.config import parse_config
from homehub.tools import result_texts
from homehub.tools.directions import Router, build_module, miles, minutes_words


def test_times_and_distances_read_naturally():
    assert minutes_words(30) == "1 min" and minutes_words(2834) == "47 min" and minutes_words(5400) == "1 h 30 min" and minutes_words(7200) == "2 h"
    assert miles(50904) == "32 miles" and miles(3000) == "1.9 miles"


def test_without_a_home_it_asks_where_to_start(tmp_path, temp_storage):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg, temp_storage).tools}
    assert "I don't know where home is" in result_texts(asyncio.run(tools["directions"]({"destination": "Seattle, WA"})))[0]


@pytest.mark.live
def test_a_real_route_from_home_without_traffic_data(tmp_path, temp_storage):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}, "directions": {"home": "Springfield, IL"}})
    text = Router(cfg, temp_storage).route("Space Needle, Seattle")
    assert text.startswith("Space Needle: about ") and "by car without traffic" in text and "miles" in text


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.slow
def test_claude_answers_drive_times_with_the_tool(harness, temp_storage, example_config):
    import dataclasses
    import time

    cfg = dataclasses.replace(example_config, directions=dataclasses.replace(example_config.directions, home="Springfield, IL"))
    h = harness([build_module(cfg, temp_storage)])

    async def scenario():
        async with h:
            started = time.perf_counter()
            reply = await h.ask("How long would it take me to drive to the Space Needle?")
            return reply, h.tools_called(), time.perf_counter() - started

    reply, called, seconds = asyncio.run(scenario())
    print(f"\n  directions -> {called} in {seconds:.1f} s\n      reply: {reply}")
    assert "directions" in called and "WebSearch" not in called and ("min" in reply.lower() or "hour" in reply.lower())
