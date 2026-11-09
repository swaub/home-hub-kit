import pytest
from claude_agent_sdk import tool

from homehub import household
from homehub.brain.permissions import Permissions
from homehub.household import sync_household
from homehub.tools import CallContext, Registry, Scope, ToolModule, ToolSpec, text_result
from homehub.tools import builtins, google


@tool("read_texts", "Read texts.", {"type": "object", "properties": {"person": {"type": "string"}}})
async def read_texts(args):
    return text_result("texts")


@tool("send_text", "Send a text.", {"type": "object", "properties": {"to": {"type": "string"}, "message": {"type": "string"}}})
async def send_text(args):
    return text_result("sent")


@tool("wake_computer", "Wake a PC.", {"type": "object", "properties": {"device": {"type": "string"}}})
async def wake_computer(args):
    return text_result("woken")


@tool("set_timer", "Timer.", {"type": "object", "properties": {"seconds": {"type": "integer"}}})
async def set_timer(args):
    return text_result("set")


PHONE = ToolModule(server="phone", tools=[
    ToolSpec(fn=read_texts, policy="auto", scope=Scope.personal("texts"), untrusted_output=True, untrusted_source="texts"),
    ToolSpec(fn=send_text, policy="confirm", scope=Scope.personal("texts"), outward=True, describe=lambda a: f"text {a.get('to')} saying {a.get('message')}"),
])
NET = ToolModule(server="net", tools=[ToolSpec(fn=wake_computer, policy="auto", scope=Scope.device("device"))])
TIME = ToolModule(server="time", tools=[ToolSpec(fn=set_timer, policy="auto")])


@pytest.fixture
def world(example_config, temp_storage):
    sync_household(example_config, temp_storage, household.EXAMPLE_PATH)
    registry = Registry(example_config, [PHONE, NET, TIME, google.MODULE, builtins.SHELL, builtins.WEB])
    return registry, temp_storage, Permissions(registry, temp_storage)


def alex(**kw):
    return CallContext(room="bedroom", conversation_id="c1", person_id="alex", person_name="Alex", role="owner", confidence="vpn_key", **kw)


def sam(**kw):
    return CallContext(room="office", conversation_id="c2", person_id="sam", person_name="Sam", role="adult", confidence="vpn_key", **kw)


def test_auto_confirm_hidden_and_unknown_tools(world):
    registry, storage, perms = world
    assert perms.decide("mcp__time__set_timer", {"seconds": 60}, alex()).how == "auto"
    d = perms.decide("mcp__phone__send_text", {"to": "Riley", "message": "hi"}, alex())
    assert d.ask and d.how == "confirm" and d.summary == "text Riley saying hi"
    assert perms.decide("mcp__phone__send_text", {"to": "Riley", "message": "hi"}, alex(approved=True)).how == "ask_once"
    registry.spec("mcp__phone__send_text").ask_each_time = True
    d = perms.decide("mcp__phone__send_text", {"to": "Riley", "message": "hi"}, alex(approved=True))
    assert d.ask and d.how == "confirm"
    registry.spec("mcp__phone__send_text").ask_each_time = False
    assert perms.decide(google.GMAIL + "send_message", {}, alex()).how == "policy_deny"
    assert perms.decide("mcp__claude_ai_Slack__post", {}, alex()).how == "policy_deny"
    assert perms.decide("mcp__notes__nothing", {}, alex()).how == "policy_deny"
    assert perms.decide("Bash", {"command": "ls", "description": "list files"}, alex()).summary == "list files"
    assert perms.decide("WebSearch", {"query": "weather"}, alex()).how == "auto"


def test_personal_data_is_owner_only_unless_granted(world):
    registry, storage, perms = world
    assert perms.decide("mcp__phone__read_texts", {}, alex()).allow
    d = perms.decide("mcp__phone__read_texts", {"person": "Sam"}, alex())
    assert not d.allow and d.how == "scope_deny" and d.message == "That's Sam's, so I can only do that for them."
    storage.grant("alex", "texts", "sam")
    assert perms.decide("mcp__phone__read_texts", {"person": "Sam"}, alex()).allow
    assert perms.decide(google.GMAIL + "search_threads", {}, sam()).allow
    assert not perms.decide(google.GMAIL + "search_threads", {"person": "Alex"}, sam()).allow


def test_devices_follow_ownership(world):
    registry, storage, perms = world
    assert perms.decide("mcp__net__wake_computer", {"device": "the office computer"}, sam()).allow
    assert perms.decide("mcp__net__wake_computer", {"device": "the office computer"}, alex()).allow
    storage.upsert_device("mom_laptop", "pc", "Riley's laptop", owner_id="riley", aliases=["the silver laptop"])
    d = perms.decide("mcp__net__wake_computer", {"device": "the silver laptop"}, sam())
    assert not d.allow and "Riley's device" in d.message
    assert perms.decide("mcp__net__wake_computer", {"device": "the silver laptop"}, alex()).allow
    storage.grant("sam", "device:mom_laptop", "riley")
    assert perms.decide("mcp__net__wake_computer", {"device": "the silver laptop"}, sam()).allow
    assert perms.decide("mcp__net__wake_computer", {"device": "unknown box"}, sam()).allow


def test_without_a_linked_person_nothing_personal_is_available(world):
    registry, storage, perms = world
    ctx = CallContext(conversation_id="c3")
    d = perms.decide("mcp__phone__send_text", {"to": "Riley", "message": "hi"}, ctx)
    assert not d.allow and not d.ask and d.how == "unknown_speaker" and "isn't linked to anyone" in d.message
    assert not perms.decide("mcp__phone__read_texts", {}, ctx).allow
    assert not perms.decide("mcp__net__wake_computer", {"device": "the office computer"}, ctx).allow
    assert perms.decide("mcp__net__wake_computer", {"device": "unknown box"}, ctx).allow
    assert perms.decide("mcp__time__set_timer", {"seconds": 5}, ctx).allow


def test_a_stated_name_never_counts_as_identified(world):
    registry, storage, perms = world
    stated = CallContext(room="bedroom", conversation_id="c4", person_id="alex", person_name="Alex", role="owner", confidence="stated")
    assert not perms.identified(stated)
    assert not perms.decide("mcp__phone__read_texts", {}, stated).allow
    assert not perms.decide(google.GMAIL + "search_threads", {}, stated).allow
    assert perms.identified(alex()) and perms.decide("mcp__phone__read_texts", {}, alex()).allow
    assert perms.decide("mcp__net__wake_computer", {"device": "the office computer"}, alex()).allow
    denied = perms.decide("mcp__phone__read_texts", {"person": "Alex"}, sam())
    assert not denied.allow and denied.message == "That's Alex's, so I can only do that for them."
