import asyncio

import pytest

from homehub.brain.permissions import Permissions
from homehub.config import load_config, parse_config
from homehub.tools import CallContext, Registry, result_texts
from homehub.tools.discord import NOT_SET_UP, Discord, DiscordError, build_module, load_token


def world(tmp_path, secrets_text=None):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    if secrets_text is not None:
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "secrets.toml").write_text(secrets_text, encoding="utf-8")
    return cfg


def test_discord_is_off_until_a_bot_token_is_saved(tmp_path):
    cfg = world(tmp_path)
    assert load_token(cfg) == "" and not build_module(cfg).available(cfg)
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg).tools}
    assert result_texts(asyncio.run(tools["discord_channels"]({})))[0] == NOT_SET_UP


def test_posting_always_asks_first(tmp_path, temp_storage):
    cfg = world(tmp_path, '[discord]\nbot_token = "x"\n')
    temp_storage.upsert_room("bedroom", "Bedroom")
    temp_storage.upsert_person("alex", "Alex", "owner", default_room="bedroom")
    registry = Registry(cfg, [build_module(cfg)])
    ctx = CallContext(room="bedroom", conversation_id="c1", person_id="alex", person_name="Alex", role="owner", confidence="vpn_key")
    decision = Permissions(registry, temp_storage).decide("mcp__discord__discord_send", {"channel": "#general", "message": "hi all"}, ctx)
    assert decision.ask and decision.how == "confirm" and decision.summary == "post in #general on Discord: hi all"
    assert Permissions(registry, temp_storage).decide("mcp__discord__discord_read", {"channel": "general"}, ctx).allow


@pytest.mark.live
def test_a_bad_token_is_rejected_by_discord(tmp_path):
    cfg = world(tmp_path, '[discord]\nbot_token = "not-a-real-token"\n')
    with pytest.raises(DiscordError, match="rejected the bot token"):
        Discord(cfg).channels()


@pytest.mark.live
def test_the_real_bot_lists_its_channels():
    cfg = load_config()
    if not load_token(cfg):
        pytest.skip("no [discord] bot_token in secrets.toml here")
    assert isinstance(Discord(cfg).channels(), list)
