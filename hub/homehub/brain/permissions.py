import dataclasses

from homehub.tools import input_matches

ALLOWED_TO_APPROVE = ("owner", "adult")
IDENTIFIED = ("vpn_key",)


@dataclasses.dataclass
class Decision:
    allow: bool
    how: str
    ask: bool = False
    summary: str = ""
    message: str = ""


class Permissions:
    def __init__(self, registry, storage):
        self.registry = registry
        self.storage = storage
        registry.permission_check = self.scope_check

    def describe(self, tool_name, tool_input):
        spec = self.registry.spec(tool_name)
        if spec is not None and spec.describe is not None:
            return spec.describe(tool_input or {})
        short = tool_name.split("__")[-1].replace("_", " ")
        detail = (tool_input or {}).get("description") or (tool_input or {}).get("query") or ""
        return f"{short} {detail}".strip()

    def identified(self, ctx):
        return ctx is not None and ctx.person_id is not None and ctx.confidence in IDENTIFIED

    def someones_device(self, spec, tool_input):
        if spec.scope.kind != "device":
            return False
        target = (tool_input or {}).get(spec.scope.detail)
        device = self.storage.find_device(target) if target else None
        return device is not None and device["owner_id"] is not None

    def scope_check(self, tool_name, tool_input, ctx):
        spec = self.registry.spec(tool_name)
        if spec is None:
            return False, f"{tool_name} is not enabled on the hub."
        if spec.policy == "hidden":
            return False, f"{tool_name} is not available."
        scope = spec.scope
        if scope.kind == "personal":
            owner = (tool_input or {}).get("person")
            owner_id = self.storage.find_person(owner)["id"] if owner and self.storage.find_person(owner) else (ctx.person_id if ctx else None)
            if ctx is None or ctx.person_id is None:
                return False, "I need to know who's asking before I can get into personal data."
            if owner_id == ctx.person_id and not self.identified(ctx):
                return False, "I'm not sure it's you, so I can't get into your personal data right now."
            if owner_id != ctx.person_id and not self.storage.has_grant(ctx.person_id, scope.detail):
                person = self.storage.person(owner_id)
                name = person["name"] if person else "someone else"
                return False, f"That's {name}'s, so I can only do that for them."
        if scope.kind == "device" and ctx is not None:
            target = (tool_input or {}).get(scope.detail)
            device = self.storage.find_device(target) if target else None
            if device is not None and device["owner_id"] not in (None, ctx.person_id):
                if ctx.role != "owner" and not self.storage.has_grant(ctx.person_id, f"device:{device['id']}"):
                    owner = self.storage.person(device["owner_id"])
                    return False, f"That's {owner['name'] if owner else 'someone else'}'s device, so I can only do that for them."
            if device is None and target is None and ctx.person_id is None and spec.policy == "confirm":
                return False, "I need to know who's asking before I can control a device."
        if ctx is not None and ctx.role in ("child", "guest") and (spec.policy == "confirm" or scope.kind == "personal"):
            return False, "I can only do that for an adult in the household."
        return True, ""

    def decide(self, tool_name, tool_input, ctx):
        spec = self.registry.spec(tool_name)
        if spec is None:
            return Decision(False, "policy_deny", message=f"{tool_name} isn't enabled on the hub.")
        if spec.policy == "hidden":
            return Decision(False, "policy_deny", message=f"{tool_name} isn't available.")
        needs_identity = spec.policy == "confirm" or spec.scope.kind == "personal" or self.someones_device(spec, tool_input)
        if needs_identity and (ctx is None or ctx.person_id is None):
            return Decision(False, "unknown_speaker", message="This device isn't linked to anyone in the household, so I can't get into personal data or someone's device from it.")
        ok, message = self.scope_check(tool_name, tool_input, ctx)
        if not ok:
            return Decision(False, "scope_deny", message=message)
        summary = self.describe(tool_name, tool_input)
        if spec.outward and ctx is not None and ctx.untrusted:
            matches = input_matches(ctx.untrusted, tool_input)
            if matches:
                what = ", ".join(m.partition(":")[2] for m in matches[:2])
                return Decision(False, "untrusted", ask=True,
                                summary=f"{summary}. That uses {what}, which came from a message, email or page I read, not from you")
        if spec.policy == "auto":
            return Decision(True, "auto", summary=summary)
        if ctx is not None and ctx.approved and not spec.ask_each_time:
            return Decision(True, "ask_once", summary=summary)
        if ctx is not None and ctx.role not in ALLOWED_TO_APPROVE:
            return Decision(False, "scope_deny", message="I can only do that for an adult in the household.")
        return Decision(False, "confirm", ask=True, summary=summary)

    async def can_use_tool(self, tool_name, tool_input, sdk_context, ctx, confirm):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        decision = self.decide(tool_name, tool_input, ctx)
        if decision.ask and decision.how in ("confirm", "untrusted"):
            ctx.publish("permission.asked", room=ctx.room, conversation_id=ctx.conversation_id, tool=tool_name, summary=decision.summary,
                        person_id=ctx.person_id, reason=decision.how)
            answer = await confirm(decision.summary, decision.how == "confirm")
            if answer:
                if decision.how == "confirm":
                    ctx.approved = True
                decision = Decision(True, "approved", summary=decision.summary)
            else:
                decision = Decision(False, "declined", summary=decision.summary,
                                    message="The user declined this action when asked to confirm. Don't retry it.")
        self.record(tool_name, tool_input, decision, ctx)
        if decision.allow:
            return PermissionResultAllow()
        return PermissionResultDeny(message=decision.message or f"{decision.summary} was not allowed.")

    def record(self, tool_name, tool_input, decision, ctx):
        if ctx is None:
            return
        ctx.publish("permission.decided", room=ctx.room, tool=tool_name, allowed=decision.allow, how=decision.how)
        if ctx.conversation_id and self.storage.conversation(ctx.conversation_id):
            self.storage.add_tool_call(ctx.conversation_id, tool_name, tool_input or {}, decision.how, ctx.person_id)
