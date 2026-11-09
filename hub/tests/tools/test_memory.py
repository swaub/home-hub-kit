import asyncio

import pytest

from homehub import household
from homehub.household import sync_household
from homehub.storage import PROMPT_LIMIT_CHARS, Storage
from homehub.tools import CallContext, current_context, result_texts
from homehub.tools.memory import NO_IDENTITY, build_module


def text(result):
    return result_texts(result)[0]


def handlers(storage):
    return {spec.name: spec.fn.handler for spec in build_module(storage).tools}


def call(tools, name, args, ctx):
    token = current_context.set(ctx)
    try:
        return asyncio.run(tools[name](args))
    finally:
        current_context.reset(token)


def test_remember_recall_list_and_forget_through_the_tools(tmp_path):
    path = tmp_path / "homehub.db"
    store = Storage(path)
    tools = handlers(store)
    ctx = CallContext(room="bedroom", conversation_id="c1")
    first = text(call(tools, "remember", {"fact": "Sam's office computer is named OFFICE-PC."}, ctx))
    again = text(call(tools, "remember", {"fact": "sam's office computer is named office-pc."}, ctx))
    for fact in ("Riley's laptop is a Dell named MOM-LAPTOP.", "The Wi-Fi network is called HomeWiFi.", "Favorite pizza is pepperoni."):
        call(tools, "remember", {"fact": fact}, ctx)
    assert first.startswith("Saved: Sam's") and again.startswith("Already remembered: Sam's") and len(store.memory_facts()) == 4
    assert text(call(tools, "recall", {"query": "sam computer"}, ctx)).startswith("Sam's office computer")
    assert text(call(tools, "recall", {"query": "wifi"}, ctx)).startswith("The Wi-Fi") or text(call(tools, "recall", {"query": "wi-fi"}, ctx)).startswith("The Wi-Fi")
    assert text(call(tools, "recall", {"query": "zebras"}, ctx)) == "Nothing remembered about that."
    assert call(tools, "remember", {"fact": "   "}, ctx)["is_error"] is True
    store.close()
    store = Storage(path)
    tools = handlers(store)
    listed = text(call(tools, "list_memories", {}, ctx))
    assert listed.count("[id ") == 4 and "HomeWiFi" in listed
    forgot = text(call(tools, "forget", {"which": "pizza"}, ctx))
    assert forgot == "Forgot: Favorite pizza is pepperoni." and len(store.memory_facts()) == 3
    call(tools, "remember", {"fact": "Sam's phone is a Pixel 8."}, ctx)
    ambiguous = call(tools, "forget", {"which": "sam"}, ctx)
    assert ambiguous["is_error"] is True and text(ambiguous).startswith("Several facts match; say which one:") and text(ambiguous).count("[id ") == 2
    fact_id = store.recall("office computer")[0]["id"]
    assert text(call(tools, "forget", {"which": fact_id}, ctx)).startswith("Forgot: Sam's office computer") and len(store.memory_facts()) == 3
    assert call(tools, "forget", {"which": "unicorns"}, ctx)["is_error"] is True
    store.close()


def test_startup_memory_block_is_size_limited_and_newest_first(temp_storage):
    tools = handlers(temp_storage)
    ctx = CallContext(room="bedroom")
    for i in range(400):
        call(tools, "remember", {"fact": f"Test fact number {i} about something."}, ctx)
    block = temp_storage.memory_prompt_block()
    assert len(block) < PROMPT_LIMIT_CHARS + 300 and "older facts not shown" in block and "number 399" in block
    assert block.index("number 399") > block.index("number 398")
    assert text(call(tools, "list_memories", {}, ctx)).count("[id ") == 400


def test_private_memory_is_only_visible_to_its_owner(example_config, temp_storage):
    sync_household(example_config, temp_storage, household.EXAMPLE_PATH)
    tools = handlers(temp_storage)
    alex = CallContext(room="bedroom", conversation_id="c1", person_id="alex", person_name="Alex", role="owner")
    sam = CallContext(room="bedroom", conversation_id="c2", person_id="sam", person_name="Sam", role="adult")
    nobody = CallContext(room="bedroom", conversation_id="c3")
    assert text(call(tools, "remember", {"fact": "The garage code is 4471.", "private": True}, alex)) == "Saved privately: The garage code is 4471."
    assert text(call(tools, "remember", {"fact": "The Wi-Fi network is called HomeWiFi."}, sam)) == "Saved: The Wi-Fi network is called HomeWiFi."
    assert text(call(tools, "recall", {"query": "garage code"}, alex)) == "The garage code is 4471. (private) [id " + temp_storage.recall("garage", "alex")[0]["id"] + "]"
    assert text(call(tools, "recall", {"query": "garage code"}, sam)) == "Nothing remembered about that."
    assert "garage" not in text(call(tools, "list_memories", {}, sam)) and "HomeWiFi" in text(call(tools, "list_memories", {}, sam))
    assert "garage" not in text(call(tools, "list_memories", {}, nobody))
    assert call(tools, "forget", {"which": "garage code"}, sam)["is_error"] is True and len(temp_storage.memory_facts("alex")) == 2
    without = call(tools, "remember", {"fact": "My PIN is 1234.", "private": True}, nobody)
    assert without["is_error"] is True and text(without) == NO_IDENTITY
    assert "garage" in temp_storage.memory_prompt_block("alex") and "garage" not in temp_storage.memory_prompt_block("sam")
    assert "HomeWiFi" in temp_storage.memory_prompt_block("sam") and "HomeWiFi" in temp_storage.memory_prompt_block(None)
    assert text(call(tools, "forget", {"which": "garage code"}, alex)) == "Forgot: The garage code is 4471."


def test_devices_go_into_the_registry_not_into_facts(example_config, temp_storage):
    sync_household(example_config, temp_storage, household.EXAMPLE_PATH)
    tools = handlers(temp_storage)
    me = CallContext(room="bedroom", conversation_id="c1", person_id="alex")
    saved = text(call(tools, "remember_device", {"name": "gaming desktop", "kind": "pc", "owner": "me", "mac": "2C-F0-5D-11-22-33", "ip": "192.168.1.50"}, me))
    assert saved == "Saved device: gaming desktop (pc, Alex's): MAC 2c:f0:5d:11:22:33, IP 192.168.1.50"
    device = temp_storage.find_device("gaming desktop")
    assert device["id"] == "gaming_desktop" and device["owner_id"] == "alex" and device["capabilities"] == ["wake_on_lan"] and device["source"] == "voice"
    updated = text(call(tools, "remember_device", {"name": "Gaming Desktop", "aliases": ["the big PC", "gaming desktop"], "hostname": "GAMER"}, me))
    assert updated == "Updated device: gaming desktop (pc, Alex's, also called the big PC): MAC 2c:f0:5d:11:22:33, IP 192.168.1.50, host GAMER"
    assert temp_storage.find_device("the big PC")["id"] == "gaming_desktop" and len(temp_storage.devices()) == 3
    assert text(call(tools, "remember_device", {"name": "Living room TV", "kind": "tv"}, me)) == "Saved device: Living room TV (tv, shared)"
    assert text(call(tools, "remember_device", {"name": "Riley's tablet", "kind": "phone", "owner": "Ry"}, me)).startswith("Saved device: Riley's tablet (phone, Riley's)")
    assert call(tools, "remember_device", {"name": "x", "kind": "toaster"}, me)["is_error"] is True
    assert call(tools, "remember_device", {"name": "x", "owner": "Zed"}, me)["is_error"] is True
    assert call(tools, "remember_device", {"name": "x", "mac": "12:34"}, me)["is_error"] is True
    assert call(tools, "remember_device", {"name": "x", "owner": "me"}, CallContext(room="bedroom"))["is_error"] is True
    listed = text(call(tools, "list_devices", {}, me))
    assert "gaming desktop (pc, Alex's, also called the big PC)" in listed and "Android phone (phone, Alex's" in listed and "Living room TV (tv, shared)" in listed
    assert temp_storage.memory_facts() == []


@pytest.mark.claude
@pytest.mark.slow
def test_claude_remembers_across_sessions_without_a_lookup(harness, temp_storage):
    module = build_module(temp_storage)

    async def first_session():
        h = harness([module])
        async with h:
            out = []
            for q in ("Remember that my sam's office computer is called OFFICE-PC.", "Also remember my favorite pizza is pepperoni."):
                reply = await h.ask(q)
                out.append((q, reply, h.tools_called()))
            return out

    first = asyncio.run(first_session())
    for q, reply, called in first:
        print(f"\n  {q} -> {called}\n      reply: {reply}")
    assert "remember" in first[1][2] and ("remember" in first[0][2] or "remember_device" in first[0][2])
    facts = [f["text"] for f in temp_storage.memory_facts("alex")]
    registry_text = " ".join(f"{d['name']} {d.get('hostname') or ''} {' '.join(d['aliases'])}" for d in temp_storage.devices()).lower()
    assert any("pepperoni" in f.lower() for f in facts)
    assert any("office-pc" in f.lower() for f in facts) or "office-pc" in registry_text

    async def second_session():
        h = harness([module])
        assert "OFFICE-PC" in h.prompt.upper() and "pepperoni" in h.prompt.lower()
        async with h:
            out = []
            for q in ("What's my sam's computer called?", "What do you remember about me?", "Forget my favorite pizza."):
                reply = await h.ask(q)
                out.append((q, reply, h.tools_called()))
            return out

    second = asyncio.run(second_session())
    for q, reply, called in second:
        print(f"\n  {q} -> {called}\n      reply: {reply}")
    assert "office-pc" in second[0][1].lower() and second[0][2] == []
    assert "pepperoni" in second[1][1].lower()
    assert "forget" in second[2][2] and [f["text"] for f in temp_storage.memory_facts("alex")] == [f for f in facts if "pepperoni" not in f.lower()]
    print("\n  devices:", registry_text)
