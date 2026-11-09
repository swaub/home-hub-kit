from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, current_context, text_result

PROMPT = ("Lists (shopping, to do, or any name they give) are kept here with add_to_list, read_list, remove_from_list and clear_list; "
          "'grocery' means the shopping list. Add each thing as its own item. These lists live on this assistant, not on Amazon or "
          "any store: if they mention Amazon or a cart, say it's on the assistant's own list. To send a list to their phone, read "
          "it and pass it to notify_owner, or use shop_list_to_phone for store links, if those tools are available.")
ITEMS = {"type": "array", "items": {"type": "string"}}


def spoken_list(items):
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def build_module(storage):
    @tool("add_to_list", "Add one or more items to a named list (for example shopping or to do).",
          {"type": "object", "properties": {"list": {"type": "string"}, "items": ITEMS}, "required": ["list", "items"]})
    async def add_to_list(args):
        ctx = current_context.get()
        try:
            key, added, already = storage.add_list_items(args["list"], args.get("items") or [], getattr(ctx, "person_id", None))
        except ValueError as e:
            return text_result(str(e), error=True)
        parts = []
        if added:
            parts.append(f"Added to the {key} list: {spoken_list(added)}.")
        if already:
            parts.append(f"Already on it: {spoken_list(already)}.")
        parts.append(f"The {key} list has {len(storage.list_items(key))} items.")
        return text_result(" ".join(parts))

    @tool("read_list", "Read a named list, oldest item first. Leave list empty to hear which lists exist.",
          {"type": "object", "properties": {"list": {"type": "string"}}})
    async def read_list(args):
        name = (args.get("list") or "").strip()
        if not name:
            names = storage.list_names()
            if not names:
                return text_result("There are no lists yet.")
            return text_result("Lists: " + "; ".join(f"{n} ({count} items)" for n, count in names) + ".")
        items = storage.list_items(name)
        key = storage.list_key(name)
        if not items:
            return text_result(f"The {key} list is empty.")
        return text_result(f"The {key} list ({len(items)} items): " + "; ".join(i["text"] for i in items) + ".")

    @tool("remove_from_list", "Remove items from a named list (when bought or done). Items can be named loosely, like 'milk' for '2% milk'.",
          {"type": "object", "properties": {"list": {"type": "string"}, "items": ITEMS}, "required": ["list", "items"]})
    async def remove_from_list(args):
        key, removed, missing = storage.remove_list_items(args["list"], args.get("items") or [])
        parts = []
        if removed:
            parts.append(f"Removed from the {key} list: {spoken_list(removed)}.")
        if missing:
            parts.append(f"Not on the {key} list: {spoken_list(missing)}.")
        return text_result(" ".join(parts) or "Nothing to remove.")

    @tool("clear_list", "Remove every item from a named list. Only when they clearly ask to clear or empty the whole list.",
          {"type": "object", "properties": {"list": {"type": "string"}}, "required": ["list"]})
    async def clear_list(args):
        key, count = storage.clear_list(args["list"])
        return text_result(f"Cleared the {key} list ({count} items removed).")

    specs = [ToolSpec(fn=fn, policy="auto", scope=Scope.household()) for fn in (add_to_list, read_list, remove_from_list, clear_list)]
    return ToolModule(server="lists", prompt=PROMPT, tools=specs)
