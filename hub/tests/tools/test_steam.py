import asyncio
import time

import pytest

from homehub.config import load_config, parse_config
from homehub.tools import result_texts
from homehub.tools.steam import NOT_SET_UP, Steam, build_module, load_settings, price_words, store_price


def test_prices_read_naturally():
    assert price_words({"final_formatted": "$29.99", "discount_percent": 0}) == "$29.99, not on sale"
    assert price_words({"final_formatted": "$14.99", "discount_percent": 50, "initial_formatted": "$29.99"}) == "$14.99, 50% off (normally $29.99)"
    assert price_words(None).startswith("no price listed")


def test_account_features_need_the_key_but_prices_do_not(tmp_path):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    assert load_settings(cfg) is None and build_module(cfg).available is None
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg).tools}
    assert result_texts(asyncio.run(tools["steam_recent"]({})))[0] == NOT_SET_UP


@pytest.mark.live
def test_real_store_prices():
    hades = store_price("Hades II")
    assert hades.startswith("Hades II on Steam: $") and ("not on sale" in hades or "% off" in hades)
    assert store_price("Counter-Strike 2") == "Counter-Strike 2 is free on Steam."


@pytest.mark.live
def test_the_real_account_when_set_up():
    cfg = load_config()
    if load_settings(cfg) is None:
        pytest.skip("no [steam] in secrets.toml here")
    assert " on Steam." in Steam(cfg).status() or " is playing " in Steam(cfg).status()


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.slow
def test_claude_checks_steam_prices(harness, example_config):
    h = harness([build_module(example_config)])

    async def scenario():
        async with h:
            started = time.perf_counter()
            reply = await h.ask("How much is Hades II on Steam right now?")
            return reply, h.tools_called(), time.perf_counter() - started

    reply, called, seconds = asyncio.run(scenario())
    print(f"\n  steam -> {called} in {seconds:.1f} s\n      reply: {reply}")
    assert "steam_price" in called and "WebSearch" not in called and "$" in reply
