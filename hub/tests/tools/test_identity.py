import asyncio

import pytest

from homehub import household
from homehub.household import sync_household
from homehub.tools import CallContext, current_context, result_texts
from homehub.tools.identity import build_module


def text(result):
    return result_texts(result)[0]


def call(tools, name, args, ctx):
    token = current_context.set(ctx)
    try:
        return asyncio.run(tools[name](args))
    finally:
        current_context.reset(token)


@pytest.fixture
def identity(example_config, temp_storage):
    sync_household(example_config, temp_storage, household.EXAMPLE_PATH)
    module = build_module(temp_storage)
    return module, {spec.name: spec.fn.handler for spec in module.tools}


def test_forget_me_erases_private_data_and_asks_first(identity, temp_storage):
    module, tools = identity
    specs = {spec.name: spec for spec in module.tools}
    assert list(specs) == ["forget_me"]
    assert specs["forget_me"].policy == "confirm" and specs["forget_me"].scope.detail == "memory"
    assert specs["forget_me"].describe({}).startswith("forget everything I know about you")
    temp_storage.remember("The garage code is 4471.", owner_id="alex", visibility="private")
    temp_storage.remember("The Wi-Fi network is called HomeWiFi.", owner_id="alex")
    temp_storage.start_conversation("c1", "bedroom", "alex")
    temp_storage.add_turn("c1", "hello", "hi")
    temp_storage.add_tool_call("c1", "mcp__time__set_timer", {"seconds": 5}, "auto", "alex")
    assert call(tools, "forget_me", {}, CallContext(room="bedroom"))["is_error"] is True
    ctx = CallContext(room="bedroom", person_id="alex", confidence="vpn_key")
    reply = text(call(tools, "forget_me", {}, ctx))
    assert reply == "Forgot everything about Alex: 1 private facts, 1 conversation turns and 1 tool calls. Household facts stay."
    assert [f["text"] for f in temp_storage.memory_facts("alex")] == ["The Wi-Fi network is called HomeWiFi."]
    assert temp_storage.conversations() == []
    assert temp_storage.person("alex")["name"] == "Alex"
