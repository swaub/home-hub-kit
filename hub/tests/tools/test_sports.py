import asyncio
import time

import pytest

from homehub.tools import result_texts
from homehub.tools.sports import Espn, build_module, league_path, local_time


def test_league_names_and_times():
    assert league_path("NFL") == "football/nfl" and league_path("football") == "football/nfl" and league_path("EPL") == "soccer/eng.1"
    with pytest.raises(ValueError):
        league_path("cricket")
    assert local_time("2026-11-01T18:00Z").startswith("Sun Nov 1, ") and local_time("nonsense") == ""


@pytest.mark.live
def test_real_scoreboards_and_a_team_schedule():
    espn = Espn()
    assert all(", " in line or " at " in line for line in espn.scores("nfl"))
    lines = espn.games("nfl", "Seahawks")
    assert lines[0] == "Seattle Seahawks" and any(l.startswith(("Last: ", "Next: ")) for l in lines[1:])
    assert espn.games("nfl", "SEA")[0] == "Seattle Seahawks"
    tools = {spec.name: spec.fn.handler for spec in build_module().tools}
    assert "Couldn't" in result_texts(asyncio.run(tools["team_games"]({"league": "nfl", "team": "Narwhals"})))[0]


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.slow
def test_claude_answers_scores_from_espn(harness):
    h = harness([build_module()])

    async def scenario():
        async with h:
            started = time.perf_counter()
            reply = await h.ask("When do the Seahawks play next, and how did they do last game?")
            return reply, h.tools_called(), time.perf_counter() - started

    reply, called, seconds = asyncio.run(scenario())
    print(f"\n  sports -> {called} in {seconds:.1f} s\n      reply: {reply}")
    assert "team_games" in called and "WebSearch" not in called and seconds < 15
