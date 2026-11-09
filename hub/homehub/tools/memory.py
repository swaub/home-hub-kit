from claude_agent_sdk import tool

import re

from homehub.tools import Scope, ToolModule, ToolSpec, current_context, text_result
from homehub.tools.wake_on_lan import format_mac, parse_mac

PROMPT = ("You have long-term memory that lasts across conversations and restarts: use remember when they ask you to remember "
          "something or tell you a lasting fact worth keeping (their devices and computers and whose they are, people, "
          "preferences), recall to look something up, and forget when asked. Facts are shared with the household unless they "
          "say it's private or just for them. Never claim you can't remember things. Facts about a device (a computer, phone, TV, "
          "console, camera, speaker, light or sensor, whose it is, its MAC or IP) go into the device registry with remember_device, "
          "not into remember, so the other tools can find the device by name.")
DEVICE_KINDS = ("pc", "phone", "tv", "console", "camera", "speaker", "light", "sensor", "other")
NO_IDENTITY = "This room has no default person, so I can't keep a private memory here; save it for the household instead."


def viewer():
    ctx = current_context.get()
    return getattr(ctx, "person_id", None), getattr(ctx, "conversation_id", None) or None


def line(fact):
    private = " (private)" if fact.get("visibility") == "private" else ""
    return f"{fact['text']}{private} [id {fact['id']}]"


def device_id_for(name):
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "device"


def describe_device(storage, device):
    owner = storage.person(device["owner_id"])["name"] + "'s" if device.get("owner_id") and storage.person(device["owner_id"]) else "shared"
    details = [d for d in (f"MAC {device['mac']}" if device.get("mac") else "", f"IP {device['last_ip']}" if device.get("last_ip") else "",
                           f"host {device['hostname']}" if device.get("hostname") else "") if d]
    aliases = f", also called {', '.join(device['aliases'])}" if device.get("aliases") else ""
    return f"{device['name']} ({device['kind']}, {owner}{aliases})" + (": " + ", ".join(details) if details else "")


def build_module(storage):
    @tool("remember", "Save a fact to long-term memory so it's known in future conversations, even after a restart. Use when the user "
          "asks you to remember something, or tells you a lasting fact worth keeping (names of their devices and computers and whose "
          "they are, people, preferences, where things are). Write it as a clear standalone sentence, e.g. \"Dad's office computer is "
          "named OFFICE-PC.\" Set private to true only when they say it's private or just for them.",
          {"type": "object", "properties": {"fact": {"type": "string"}, "private": {"type": "boolean"}}, "required": ["fact"]})
    async def remember(args):
        person_id, conversation_id = viewer()
        private = bool(args.get("private"))
        if private and person_id is None:
            return text_result(NO_IDENTITY, error=True)
        try:
            fact, new = storage.remember(args["fact"], owner_id=person_id, visibility="private" if private else "household",
                                        conversation_id=conversation_id)
        except ValueError as e:
            return text_result(str(e), error=True)
        return text_result(("Saved" + (" privately" if private else "") + ": " if new else "Already remembered: ") + fact["text"])

    @tool("recall", "Search long-term memory for facts about something (e.g. 'dad computer', 'wifi', 'pizza').",
          {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})
    async def recall(args):
        person_id, _ = viewer()
        found = storage.recall(args["query"], viewer_id=person_id)
        return text_result("\n".join(line(f) for f in found) or "Nothing remembered about that.")

    @tool("list_memories", "List everything in long-term memory.", {"type": "object", "properties": {}})
    async def list_memories(args):
        person_id, _ = viewer()
        return text_result("\n".join(line(f) for f in storage.memory_facts(person_id)) or "Memory is empty.")

    @tool("forget", "Remove a fact from long-term memory, by its id or by describing it (e.g. 'favorite pizza').",
          {"type": "object", "properties": {"which": {"type": "string"}}, "required": ["which"]})
    async def forget(args):
        person_id, _ = viewer()
        removed, ambiguous = storage.forget(args["which"], viewer_id=person_id)
        if ambiguous:
            return text_result("Several facts match; say which one: " + "; ".join(line(f) for f in ambiguous), error=True)
        if not removed:
            return text_result("Nothing in memory matches that.", error=True)
        return text_result("Forgot: " + "; ".join(f["text"] for f in removed))

    @tool("remember_device", "Save or update a device in the household's device registry so other tools can find it by name (for example "
          "wake_computer). Use it instead of remember whenever the fact is about a device. name is the plain name the user says for it "
          "(e.g. 'gaming desktop'); kind is one of pc, phone, tv, console, camera, speaker, light, sensor, other; owner is the household "
          "member it belongs to ('me' for the speaker, or a name), or empty when it's shared; aliases are other names for it; mac, ip and "
          "hostname are optional network details.",
          {"type": "object", "properties": {"name": {"type": "string"}, "kind": {"type": "string"}, "owner": {"type": "string"},
                                            "aliases": {"type": "array", "items": {"type": "string"}}, "mac": {"type": "string"},
                                            "ip": {"type": "string"}, "hostname": {"type": "string"}}, "required": ["name"]})
    async def remember_device(args):
        person_id, _ = viewer()
        name = (args.get("name") or "").strip()
        if not name:
            return text_result("Give the device a name.", error=True)
        existing = storage.find_device(name)
        kind = (args.get("kind") or (existing["kind"] if existing else "other")).strip().lower()
        if kind not in DEVICE_KINDS:
            return text_result(f"kind must be one of {', '.join(DEVICE_KINDS)}.", error=True)
        owner = (args.get("owner") or "").strip()
        owner_id = existing["owner_id"] if existing else None
        if owner:
            if owner.lower() in ("me", "my", "mine", "myself"):
                if person_id is None:
                    return text_result("I don't know who's speaking, so say whose device it is by name.", error=True)
                owner_id = person_id
            else:
                person = storage.find_person(owner)
                if person is None:
                    return text_result(f"I don't know anyone called {owner} in this household.", error=True)
                owner_id = person["id"]
        device_id = existing["id"] if existing else device_id_for(name)
        aliases = [a.strip() for a in (args.get("aliases") or []) if a and a.strip() and a.strip().lower() != name.lower()]
        if existing:
            aliases = list(dict.fromkeys(existing.get("aliases", []) + aliases))
            capabilities = set(existing.get("capabilities", []))
        else:
            capabilities = set()
        runtime = {}
        if args.get("mac"):
            try:
                runtime["mac"] = format_mac(parse_mac(args["mac"]))
            except ValueError as e:
                return text_result(str(e), error=True)
            capabilities.add("wake_on_lan")
        if args.get("ip"):
            runtime["last_ip"] = args["ip"].strip()
        if args.get("hostname"):
            runtime["hostname"] = args["hostname"].strip()
        try:
            device = storage.upsert_device(device_id, kind, existing["name"] if existing else name, owner_id, existing.get("room_id") if existing else None,
                                           sorted(capabilities), aliases, "voice", **runtime)
        except ValueError as e:
            return text_result(str(e), error=True)
        return text_result(("Updated" if existing else "Saved") + " device: " + describe_device(storage, device))

    @tool("list_devices", "List the household's known devices with whose they are and their network details.", {"type": "object", "properties": {}})
    async def list_devices(args):
        devices = storage.devices()
        return text_result("\n".join(describe_device(storage, d) for d in devices) or "No devices are registered yet.")

    specs = [ToolSpec(fn=fn, policy="auto", scope=Scope.household()) for fn in (remember, recall, list_memories, forget, remember_device, list_devices)]
    return ToolModule(server="memory", tools=specs, prompt=PROMPT)
