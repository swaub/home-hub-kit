import asyncio

import pytest

from homehub.tools import CallContext, current_context, result_texts
from homehub.tools.lists import build_module, spoken_list


def test_list_names_are_normalized(temp_storage):
    key = temp_storage.list_key
    assert key("Shopping") == key("my shopping list") == key("the grocery list") == key("Groceries") == "shopping"
    assert key("To-Do") == key("todo list") == "to do" and key("Camping trip") == "camping trip"


def test_items_are_added_once_read_in_order_removed_loosely_and_cleared(temp_storage):
    temp_storage.upsert_room("bedroom", "Bedroom")
    temp_storage.upsert_person("alex", "Alex", "owner", default_room="bedroom")
    key, added, already = temp_storage.add_list_items("grocery list", ["2% milk", "crickets", "  paper   towels "], "alex")
    assert key == "shopping" and added == ["2% milk", "crickets", "paper towels"] and already == []
    assert temp_storage.add_list_items("Shopping", ["CRICKETS", "eggs"])[1:] == (["eggs"], ["CRICKETS"])
    assert [i["text"] for i in temp_storage.list_items("my shopping list")] == ["2% milk", "crickets", "paper towels", "eggs"]
    assert temp_storage.list_names() == [("shopping", 4)]
    assert temp_storage.remove_list_items("shopping", ["milk", "bananas"]) == ("shopping", ["2% milk"], ["bananas"])
    assert temp_storage.clear_list("groceries") == ("shopping", 3) and temp_storage.list_items("shopping") == []
    with pytest.raises(ValueError):
        temp_storage.add_list_items("  ", ["x"])


def test_list_tools_speak_naturally(temp_storage):
    tools = {spec.name: spec.fn.handler for spec in build_module(temp_storage).tools}
    run = lambda name, args: result_texts(asyncio.run(tools[name](args)))[0]
    assert spoken_list(["a"]) == "a" and spoken_list(["a", "b"]) == "a and b" and spoken_list(["a", "b", "c"]) == "a, b, and c"
    token = current_context.set(CallContext(room="bedroom", conversation_id="c1", person_id=None))
    try:
        assert run("read_list", {}) == "There are no lists yet."
        assert run("add_to_list", {"list": "shopping", "items": ["crickets", "eggs"]}) == "Added to the shopping list: crickets and eggs. The shopping list has 2 items."
        assert run("add_to_list", {"list": "groceries", "items": ["eggs"]}) == "Already on it: eggs. The shopping list has 2 items."
        assert run("read_list", {"list": "grocery"}) == "The shopping list (2 items): crickets; eggs."
        assert run("read_list", {}) == "Lists: shopping (2 items)."
        assert run("remove_from_list", {"list": "shopping", "items": ["eggs", "kale"]}) == "Removed from the shopping list: eggs. Not on the shopping list: kale."
        assert run("clear_list", {"list": "shopping"}) == "Cleared the shopping list (1 items removed)."
        assert run("read_list", {"list": "to do"}) == "The to do list is empty."
    finally:
        current_context.reset(token)


@pytest.mark.claude
@pytest.mark.slow
def test_claude_keeps_the_shopping_list(harness, temp_storage):
    h = harness([build_module(temp_storage)])

    async def scenario():
        async with h:
            first = await h.ask("Add crickets and paper towels to the grocery list.")
            second = await h.ask("What's on my shopping list?")
            third = await h.ask("I bought the crickets, take them off.")
            return first, second, third, h.tools_called()

    first, second, third, called = asyncio.run(scenario())
    print(f"\n  {first}\n  {second}\n  {third}\n  {called}")
    assert [i["text"].lower() for i in temp_storage.list_items("shopping")] == ["paper towels"]
    assert "paper towels" in second.lower() and "crickets" in second.lower()
