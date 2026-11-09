from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, current_context, text_result

PROMPT = ("forget_me erases everything personal about the speaker (private facts and their conversation history) after asking them.")


def build_module(storage):
    @tool("forget_me", "Erase everything personal about the speaker: their private facts and their conversation history. Household facts "
          "and the household file stay. Use when they say 'forget everything about me'.",
          {"type": "object", "properties": {}})
    async def forget_me(args):
        ctx = current_context.get()
        person = storage.person(ctx.person_id) if ctx is not None and ctx.person_id else None
        if person is None:
            return text_result("I need to know who's asking before I can forget them.", error=True)
        counts = storage.forget_person(person["id"])
        return text_result(f"Forgot everything about {person['name']}: {counts['memory']} private facts, {counts['turns']} conversation turns "
                           f"and {counts['tool_calls']} tool calls. Household facts stay.")

    specs = [ToolSpec(fn=forget_me, policy="confirm", scope=Scope.personal("memory"),
                      describe=lambda a: "forget everything I know about you: your private facts and your conversation history")]
    return ToolModule(server="identity", tools=specs, prompt=PROMPT)
