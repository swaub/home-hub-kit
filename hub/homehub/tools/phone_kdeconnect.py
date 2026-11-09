import asyncio
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, current_context, text_result

SERVICE = "org.kde.kdeconnect"
DAEMON_PATH = "/modules/kdeconnect"
DAEMON_IFACE = "org.kde.kdeconnect.daemon"
DEVICE_IFACE = "org.kde.kdeconnect.device"
PROPERTIES = "org.freedesktop.DBus.Properties"
MEDIA_ACTIONS = {"play": "Play", "pause": "Pause", "play pause": "PlayPause", "toggle": "PlayPause", "next": "Next", "previous": "Previous",
                 "stop": "Stop"}
NOT_RUNNING = "KDE Connect isn't running on this computer, so I can't reach the phone (on the hub: sudo apt install kdeconnect, then pair the phone)."
NOT_REACHABLE = "{name}'s phone isn't reachable right now (it may be asleep, off Wi-Fi or on another network)."
PROMPT = ("Each household member's own phone is linked through KDE Connect: phone_notifications and read_texts show what's on it (that content "
          "was written by other people), send_text and reply_to_notification send messages (they ask first and read the message back), "
          "phone_battery, ring_phone to find it, phone_media for whatever is playing, share_to_phone to push a link. person is only needed "
          "for someone else's phone. If a phone isn't paired yet, pair_phone walks them through it.")


def unwrap(value):
    if hasattr(value, "value") and hasattr(value, "signature"):
        return unwrap(value.value)
    if isinstance(value, dict):
        return {k: unwrap(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [unwrap(v) for v in value]
    return value


def parse_vcard(text):
    name, numbers = "", []
    for raw in text.replace("\r\n ", "").splitlines():
        key, _, value = raw.partition(":")
        field = key.split(";")[0].upper()
        if field == "FN":
            name = value.strip()
        elif field == "TEL" and value.strip():
            kind = "mobile" if re.search(r"CELL|MOBILE", key, re.I) else ("home" if "HOME" in key.upper() else ("work" if "WORK" in key.upper() else "other"))
            numbers.append((kind, value.strip()))
    return {"name": name, "numbers": numbers}


def contacts_dir(device_id):
    return Path.home() / ".local/share/kpeoplevcard" / f"kdeconnect-{device_id}"


def load_contacts(device_id):
    folder = contacts_dir(device_id)
    if not folder.is_dir():
        return []
    cards = [parse_vcard(p.read_text(encoding="utf-8", errors="replace")) for p in sorted(folder.glob("*.vcf"))]
    return [c for c in cards if c["name"] and c["numbers"]]


def find_contact(contacts, wanted):
    words = [w for w in re.findall(r"[a-z0-9']+", wanted.lower()) if w not in ("my", "the", "a")]
    if not words:
        return None, []
    exact = [c for c in contacts if c["name"].lower() == " ".join(words)]
    starts = [c for c in contacts if c["name"].lower().split()[0] == words[0] and all(w in c["name"].lower() for w in words)]
    loose = [c for c in contacts if all(w in c["name"].lower() for w in words)]
    matches = sorted(exact or starts or loose, key=lambda c: c["name"].lower())
    return (matches[0] if len(matches) == 1 else None), matches


def best_number(contact):
    order = {"mobile": 0, "other": 1, "home": 2, "work": 3}
    return sorted(contact["numbers"], key=lambda n: order.get(n[0], 9))[0][1]


def resolve_number(device_id, who):
    return pick_number(load_contacts(device_id), who)


def pick_number(contacts, who):
    if sum(ch.isdigit() for ch in who) >= 7:
        return who.strip(), re.sub(r"[^\d+]", "", who)
    contact, matches = find_contact(contacts, who)
    if contact is None and matches:
        raise ValueError(f"Several contacts match {who}: {', '.join(c['name'] for c in matches[:5])}. Which one?")
    if contact is None:
        raise ValueError(f"I can't find {who} in your phone's contacts; say the number.")
    return contact["name"], re.sub(r"[^\d+]", "", best_number(contact))


def device_path(device_id):
    return f"{DAEMON_PATH}/devices/{device_id}"


def ago(seconds):
    if seconds is None:
        return ""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"


def format_notifications(items, want=""):
    want = (want or "").lower()
    shown = [i for i in items if (i.get("title") or i.get("text")) and (not want or any(want in str(i.get(k, "")).lower() for k in ("app", "title", "text")))]
    lines = [f"{i['app']}: {i['title']}" + (f" - {i['text'][:300]}" if i.get("text") else "") + (" (can reply)" if i.get("reply_id") else "") for i in shown[:25]]
    return "\n".join(lines) if lines else "No notifications" + (f" matching '{want}'." if want else "."), len(shown)


def parse_messages(raw, now=None):
    now = time.time() if now is None else now
    messages = []
    for item in unwrap(raw) or []:
        if not isinstance(item, dict):
            continue
        addresses = [a.get("address") if isinstance(a, dict) else str(a) for a in (item.get("addresses") or [])]
        date = item.get("date")
        seconds = (now - date / 1000) if isinstance(date, (int, float)) and date > 0 else None
        messages.append({"thread_id": item.get("thread_id"), "id": item.get("_id"), "from": ", ".join(a for a in addresses if a) or "unknown",
                         "body": str(item.get("body") or ""), "sent": int(item.get("type") or 0) == 2, "read": bool(item.get("read", True)),
                         "date": date, "ago": ago(seconds)})
    messages.sort(key=lambda m: -(m["date"] or 0))
    return messages


def format_messages(messages, unread_only=False, contact="", limit=20):
    contact = (contact or "").lower()
    chosen = [m for m in messages if (not unread_only or (not m["read"] and not m["sent"])) and (not contact or contact in m["from"].lower() or contact in m["body"].lower())]
    lines = [("You" if m["sent"] else m["from"]) + f": {m['body'][:300]}" + (f" ({m['ago']})" if m["ago"] else "") for m in chosen[:limit]]
    if not lines:
        return "No texts" + (" unread" if unread_only else "") + (f" with {contact}" if contact else "") + ".", 0
    return "\n".join(lines), len(chosen)


def format_battery(charge, charging):
    if charge is None or charge < 0:
        return "The phone hasn't reported its battery yet."
    return f"Battery {charge}%" + (", charging." if charging else ".")


def announce_text(event):
    app, title, text = event.get("app") or "the phone", event.get("title") or "", (event.get("text") or "").strip()
    if title and text:
        return f"{title} on {app}: {text[:140]}"
    return f"{app}: {title or text[:140]}"


def pair_target(devices, wanted_name=""):
    candidates = [d for d in devices if d["reachable"] and not d["paired"]]
    wanted = (wanted_name or "").lower()
    if wanted:
        named = [d for d in candidates if wanted in d["name"].lower() or d["name"].lower() in wanted]
        if len(named) == 1:
            return named[0], candidates
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def resolve_phone(storage, ctx, person_arg=""):
    who = (person_arg or "").strip()
    if not who or who.lower() in ("me", "my", "mine", "myself", "my phone"):
        person = storage.person(ctx.person_id) if ctx is not None and ctx.person_id else None
        if person is None:
            return None, None, "I need to know who's asking before I can get to their phone."
    else:
        person = storage.find_person(who)
        if person is None:
            return None, None, f"I don't know anyone called {who} in this household."
    phones = [d for d in storage.devices(owner_id=person["id"], kind="phone") if "kdeconnect" in d.get("capabilities", [])]
    if not phones:
        return person, None, f"{person['name']} doesn't have a phone linked with KDE Connect in the household file."
    return person, phones[0], None


class KdeConnect:
    def __init__(self, emit=None, storage=None, cli=None):
        self.emit = emit
        self.storage = storage
        self.cli = cli
        self.bus = None
        self.loop = None
        self.cache = {}
        self.listening = False

    def publish(self, name, **payload):
        if self.emit is not None:
            self.emit(name, **payload)

    def call_received(self, device_id, body):
        person_id, registry_id = self.person_for_device(device_id)
        self.publish("phone.call", person_id=person_id, device_id=registry_id or device_id, event=str(body[0]),
                     number=str(body[1]), caller=str(body[2]), untrusted=True)

    def bus_address(self):
        return os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")

    async def connect(self):
        loop = asyncio.get_running_loop()
        if self.bus is not None and self.loop is not loop:
            self.close()
        if self.bus is None:
            if not self.bus_address():
                raise RuntimeError("no D-Bus session bus")
            from dbus_next.aio import MessageBus
            from dbus_next import BusType

            self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
            self.loop = loop
        return self.bus

    async def call(self, path, interface, member, signature="", body=None):
        from dbus_next import Message, MessageType

        bus = await self.connect()
        reply = await bus.call(Message(destination=SERVICE, path=path, interface=interface, member=member, signature=signature, body=body or []))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.error_name} {' '.join(str(b) for b in reply.body)}")
        return unwrap(reply.body)

    async def prop(self, path, interface, name):
        return (await self.call(path, PROPERTIES, "Get", "ss", [interface, name]))[0]

    async def available(self):
        try:
            from dbus_next import Message
        except ImportError:
            return False
        try:
            bus = await self.connect()
        except Exception:
            return False
        reply = await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus", interface="org.freedesktop.DBus",
                                       member="NameHasOwner", signature="s", body=[SERVICE]))
        return bool(reply.body and reply.body[0])

    async def devices(self, only_reachable=False, only_paired=False):
        ids = (await self.call(DAEMON_PATH, DAEMON_IFACE, "devices", "bb", [only_reachable, only_paired]))[0]
        found = []
        for device_id in ids:
            path = device_path(device_id)
            found.append({"id": device_id, "name": await self.prop(path, DEVICE_IFACE, "name"), "type": await self.prop(path, DEVICE_IFACE, "type"),
                          "reachable": bool(await self.prop(path, DEVICE_IFACE, "isReachable")), "paired": bool(await self.prop(path, DEVICE_IFACE, "isPaired"))})
        return found

    async def device_state(self, device_id):
        path = device_path(device_id)
        return {"id": device_id, "name": await self.prop(path, DEVICE_IFACE, "name"), "reachable": bool(await self.prop(path, DEVICE_IFACE, "isReachable")),
                "paired": bool(await self.prop(path, DEVICE_IFACE, "isPaired"))}

    async def request_pair(self, device_id):
        await self.call(device_path(device_id), DEVICE_IFACE, "requestPairing")

    async def battery(self, device_id):
        path = device_path(device_id) + "/battery"
        charge = await self.prop(path, "org.kde.kdeconnect.device.battery", "charge")
        charging = await self.prop(path, "org.kde.kdeconnect.device.battery", "isCharging")
        return int(charge), bool(charging)

    async def notification(self, device_id, nid):
        path = f"{device_path(device_id)}/notifications/{nid}"
        iface = "org.kde.kdeconnect.device.notifications.notification"
        item = {"id": nid}
        for key, name in (("app", "appName"), ("title", "title"), ("text", "text"), ("ticker", "ticker"), ("reply_id", "replyId")):
            try:
                item[key] = str(await self.prop(path, iface, name) or "")
            except RuntimeError:
                item[key] = ""
        if not item["text"] and item["ticker"]:
            item["text"] = item["ticker"]
        return item

    async def notifications(self, device_id):
        path = device_path(device_id) + "/notifications"
        ids = (await self.call(path, "org.kde.kdeconnect.device.notifications", "activeNotifications"))[0]
        items = [await self.notification(device_id, nid) for nid in ids]
        self.cache[("notifications", device_id)] = (time.time(), items)
        return items

    async def reply(self, device_id, reply_id, message):
        await self.call(device_path(device_id) + "/notifications", "org.kde.kdeconnect.device.notifications", "sendReply", "ss", [reply_id, message])

    async def ring(self, device_id):
        await self.call(device_path(device_id) + "/findmyphone", "org.kde.kdeconnect.device.findmyphone", "ring")

    async def media(self, device_id, action):
        await self.call(device_path(device_id) + "/mprisremote", "org.kde.kdeconnect.device.mprisremote", "sendAction", "s", [action])

    async def share_url(self, device_id, url):
        await self.call(device_path(device_id) + "/share", "org.kde.kdeconnect.device.share", "shareUrl", "s", [url])

    async def share_text(self, device_id, text):
        await self.call(device_path(device_id) + "/share", "org.kde.kdeconnect.device.share", "shareText", "s", [text])

    async def conversations(self, device_id, wait_seconds=1.5):
        path = device_path(device_id)
        iface = "org.kde.kdeconnect.device.conversations"
        await self.call(path, iface, "requestAllConversationThreads")
        await asyncio.sleep(wait_seconds)
        raw = (await self.call(path, iface, "activeConversations"))[0]
        return parse_messages(raw)

    async def conversation(self, device_id, thread_id, wait_seconds=1.5, newest=30):
        path = device_path(device_id)
        iface = "org.kde.kdeconnect.device.conversations"
        await self.call(path, iface, "requestConversation", "xii", [int(thread_id), 0, int(newest)])
        await asyncio.sleep(wait_seconds)
        raw = (await self.call(path, iface, "activeConversations"))[0]
        return [m for m in parse_messages(raw) if str(m["thread_id"]) == str(thread_id)]

    def send_sms(self, device_id, destination, message):
        exe = self.cli or shutil.which("kdeconnect-cli")
        if not exe:
            raise RuntimeError("kdeconnect-cli is not installed")
        out = subprocess.run([exe, "-d", device_id, "--send-sms", message, "--destination", destination], capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise RuntimeError((out.stderr or out.stdout).strip() or f"kdeconnect-cli exited {out.returncode}")
        return (out.stdout or "").strip()

    def person_for_device(self, device_id):
        if self.storage is None:
            return None, None
        for device in self.storage.devices(kind="phone"):
            if device.get("kdeconnect_id") == device_id:
                return device.get("owner_id"), device["id"]
        return None, None

    async def listen(self):
        from dbus_next import Message, MessageType

        if self.listening:
            return
        bus = await self.connect()
        await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus", interface="org.freedesktop.DBus", member="AddMatch",
                               signature="s", body=[f"type='signal',sender='{SERVICE}'"]))
        loop = asyncio.get_running_loop()

        def handler(message):
            if message.message_type != MessageType.SIGNAL or not (message.path or "").startswith(DAEMON_PATH + "/devices/"):
                return
            parts = message.path[len(DAEMON_PATH) + 9:].split("/")
            device_id = parts[0]
            person_id, registry_id = self.person_for_device(device_id)
            if message.member == "notificationPosted" and message.body:
                loop.create_task(self.announce_notification(device_id, person_id, registry_id, str(message.body[0])))
            elif message.member == "callReceived" and len(message.body) >= 3:
                self.call_received(device_id, message.body)
            elif message.member in ("reachableChanged", "reachableStatusChanged") and message.body:
                self.publish("phone.reachable", person_id=person_id, device_id=registry_id or device_id, reachable=bool(message.body[0]))
            elif message.member == "refreshed" and len(message.body) >= 2:
                self.publish("phone.battery", person_id=person_id, device_id=registry_id or device_id, percent=int(message.body[1]), charging=bool(message.body[0]))

        bus.add_message_handler(handler)
        self.listening = True

    async def announce_notification(self, device_id, person_id, registry_id, nid):
        try:
            item = await self.notification(device_id, nid)
        except Exception as e:
            self.publish("service.warning", message=f"phone notification {nid}: {e}")
            return
        self.publish("phone.notification", person_id=person_id, device_id=registry_id or device_id, app=item["app"], title=item["title"], text=item["text"],
                     can_reply=bool(item["reply_id"]), untrusted=True)

    def close(self):
        if self.bus is not None:
            try:
                self.bus.disconnect()
            except Exception:
                pass
            self.bus = None
            self.loop = None
            self.listening = False


def build_module(storage, client=None):
    client = client or KdeConnect(storage=storage)

    async def phone_for(args):
        ctx = current_context.get()
        person, device, problem = resolve_phone(storage, ctx, args.get("person"))
        if problem:
            return None, None, None, text_result(problem, error=True)
        if not await client.available():
            return person, device, None, text_result(NOT_RUNNING, error=True)
        kid = device.get("kdeconnect_id")
        if not kid:
            paired = [d for d in await client.devices(only_paired=True) if d["name"].lower() in {device["name"].lower(), *(a.lower() for a in device.get("aliases", []))}]
            if not paired:
                return person, device, None, text_result(f"{person['name']}'s phone isn't paired with KDE Connect yet; ask me to pair it.", error=True)
            kid = paired[0]["id"]
            storage.set_device_runtime(device["id"], kdeconnect_id=kid)
        state = await client.device_state(kid)
        if not state["paired"]:
            return person, device, kid, text_result(f"{person['name']}'s phone isn't paired with KDE Connect yet; ask me to pair it.", error=True)
        if not state["reachable"]:
            return person, device, kid, text_result(NOT_REACHABLE.format(name=person["name"]), error=True)
        return person, device, kid, None

    def guarded(fn):
        async def run(args):
            person, device, kid, problem = await phone_for(args)
            if problem:
                return problem
            try:
                return await fn(person, device, kid, args)
            except RuntimeError as e:
                return text_result(f"The phone didn't answer: {e}", error=True)

        return run

    @tool("phone_notifications", "Read the notifications currently on a household member's phone (messages, Discord, email and so on). Optional "
          "filter: an app ('Discord') or a person or word ('Mom'). person only for someone else's phone. Summarize aloud: who and what.",
          {"type": "object", "properties": {"person": {"type": "string"}, "filter": {"type": "string"}}})
    @guarded
    async def phone_notifications(person, device, kid, args):
        text, _ = format_notifications(await client.notifications(kid), args.get("filter"))
        return text_result(text)

    @tool("read_texts", "Read recent text messages (SMS) on a household member's phone, newest first. unread_only for just the unread ones; "
          "from_contact to narrow to one person. person only for someone else's phone.",
          {"type": "object", "properties": {"person": {"type": "string"}, "unread_only": {"type": "boolean"}, "from_contact": {"type": "string"}}})
    @guarded
    async def read_texts(person, device, kid, args):
        text, _ = format_messages(await client.conversations(kid), bool(args.get("unread_only")), args.get("from_contact"))
        return text_result(text)

    @tool("text_conversation", "Read the whole text conversation with one contact (name or number) on a household member's phone.",
          {"type": "object", "properties": {"person": {"type": "string"}, "contact": {"type": "string"}}, "required": ["contact"]})
    @guarded
    async def text_conversation(person, device, kid, args):
        messages = await client.conversations(kid)
        matching = [m for m in messages if args["contact"].lower() in m["from"].lower()]
        if not matching:
            return text_result(f"No conversation with {args['contact']}.")
        thread = await client.conversation(kid, matching[0]["thread_id"])
        text, _ = format_messages(thread or matching, limit=40)
        return text_result(text)

    @tool("send_text", "Send a text message (SMS) from a household member's phone. to is the recipient's number or a name from a recent "
          "conversation. The assistant asks the person first and reads the message back.",
          {"type": "object", "properties": {"person": {"type": "string"}, "to": {"type": "string"}, "message": {"type": "string"}}, "required": ["to", "message"]})
    @guarded
    async def send_text(person, device, kid, args):
        to = args["to"].strip()
        if not any(ch.isdigit() for ch in to):
            try:
                _, to = resolve_number(kid, to)
            except ValueError:
                messages = await client.conversations(kid)
                match = next((m for m in messages if to.lower() in m["from"].lower()), None)
                if match is None:
                    return text_result(f"I don't have a number for {args['to'].strip()}; say the number.", error=True)
                to = match["from"].split(",")[0].strip()
        await asyncio.to_thread(client.send_sms, kid, to, args["message"])
        return text_result(f"Sent to {to}: {args['message']}")

    @tool("reply_to_notification", "Reply inside a notification that allows replies (chat apps such as Discord, WhatsApp, Messages). "
          "app_or_sender picks the notification; the assistant asks first.",
          {"type": "object", "properties": {"person": {"type": "string"}, "app_or_sender": {"type": "string"}, "message": {"type": "string"}},
           "required": ["app_or_sender", "message"]})
    @guarded
    async def reply_to_notification(person, device, kid, args):
        want = args["app_or_sender"].lower()
        items = [i for i in await client.notifications(kid) if i.get("reply_id") and (want in i["app"].lower() or want in i["title"].lower())]
        if not items:
            return text_result(f"No notification from {args['app_or_sender']} that I can reply to.", error=True)
        await client.reply(kid, items[0]["reply_id"], args["message"])
        return text_result(f"Replied to {items[0]['title'] or items[0]['app']}: {args['message']}")

    @tool("phone_battery", "The phone's battery level and whether it's charging. person only for someone else's phone.",
          {"type": "object", "properties": {"person": {"type": "string"}}})
    @guarded
    async def phone_battery(person, device, kid, args):
        charge, charging = await client.battery(kid)
        return text_result(format_battery(charge, charging))

    @tool("ring_phone", "Make the phone ring loudly so it can be found, even on silent. person only for someone else's phone.",
          {"type": "object", "properties": {"person": {"type": "string"}}})
    @guarded
    async def ring_phone(person, device, kid, args):
        await client.ring(kid)
        return text_result(f"{person['name']}'s phone is ringing.")

    @tool("phone_media", "Control what's playing on the phone: play, pause, next, previous, stop.",
          {"type": "object", "properties": {"person": {"type": "string"}, "action": {"type": "string"}}, "required": ["action"]})
    @guarded
    async def phone_media(person, device, kid, args):
        action = MEDIA_ACTIONS.get(args["action"].lower().strip().replace("_", " "))
        if not action:
            return text_result(f"action must be one of {', '.join(sorted(MEDIA_ACTIONS))}.", error=True)
        await client.media(kid, action)
        return text_result(f"Sent {args['action']} to the phone.")

    @tool("share_to_phone", "Send a link or a piece of text to the phone (it opens or shows it there).",
          {"type": "object", "properties": {"person": {"type": "string"}, "url": {"type": "string"}, "text": {"type": "string"}}})
    @guarded
    async def share_to_phone(person, device, kid, args):
        if args.get("url"):
            await client.share_url(kid, args["url"].strip())
            return text_result(f"Sent {args['url'].strip()} to {person['name']}'s phone.")
        if args.get("text"):
            await client.share_text(kid, args["text"])
            return text_result(f"Sent the text to {person['name']}'s phone.")
        return text_result("Give a url or a text to share.", error=True)

    @tool("call_on_phone", "Start a phone call from a household member's phone: opens the phone's dialer with the number of a contact (by "
          "name) or a number they say, and they tap Call. It never places the call by itself.",
          {"type": "object", "properties": {"person": {"type": "string"}, "to": {"type": "string"}}, "required": ["to"]})
    @guarded
    async def call_on_phone(person, device, kid, args):
        try:
            name, number = resolve_number(kid, args["to"])
        except ValueError as e:
            return text_result(str(e), error=True)
        await client.share_url(kid, f"tel:{number}")
        return text_result(f"Opened the dialer on {person['name']}'s phone with {name} ({number}); they tap Call to start it.")

    @tool("pair_phone", "Pair a household member's phone with KDE Connect (first time only). The KDE Connect app must be open on the phone on the "
          "home Wi-Fi. The assistant asks first, sends the request, and tells them to accept it on the phone.",
          {"type": "object", "properties": {"person": {"type": "string"}}})
    async def pair_phone(args):
        ctx = current_context.get()
        person, device, problem = resolve_phone(storage, ctx, args.get("person"))
        if problem:
            return text_result(problem, error=True)
        if not await client.available():
            return text_result(NOT_RUNNING, error=True)
        try:
            found = await client.devices(only_reachable=True)
        except RuntimeError as e:
            return text_result(f"KDE Connect didn't answer: {e}", error=True)
        target, candidates = pair_target(found, device["name"])
        if target is None:
            names = ", ".join(d["name"] for d in candidates) or "none"
            return text_result(f"I can't tell which phone is {person['name']}'s. Reachable unpaired devices: {names}. Open KDE Connect on the phone "
                               "on the home Wi-Fi and try again.", error=True)
        try:
            await client.request_pair(target["id"])
        except RuntimeError as e:
            return text_result(f"Pairing request failed: {e}", error=True)
        for _ in range(60):
            await asyncio.sleep(1.0)
            if (await client.device_state(target["id"]))["paired"]:
                storage.set_device_runtime(device["id"], kdeconnect_id=target["id"])
                return text_result(f"Paired with {target['name']}; it's now {person['name']}'s phone.")
        return text_result(f"I sent the pairing request to {target['name']}; accept it on the phone within a minute, then ask again.", error=True)

    texts, notes, phone = Scope.personal("texts"), Scope.personal("notifications"), Scope.personal("phone")
    specs = [
        ToolSpec(fn=phone_notifications, policy="auto", scope=notes, untrusted_output=True, untrusted_source="phone notifications"),
        ToolSpec(fn=read_texts, policy="auto", scope=texts, untrusted_output=True, untrusted_source="text messages"),
        ToolSpec(fn=text_conversation, policy="auto", scope=texts, untrusted_output=True, untrusted_source="text messages"),
        ToolSpec(fn=send_text, policy="confirm", scope=texts, outward=True, describe=lambda a: f"text {a.get('to', 'someone')} saying: {a.get('message', '')}"),
        ToolSpec(fn=reply_to_notification, policy="confirm", scope=notes, outward=True,
                 describe=lambda a: f"reply to {a.get('app_or_sender', 'a notification')} saying: {a.get('message', '')}"),
        ToolSpec(fn=phone_battery, policy="auto", scope=phone), ToolSpec(fn=ring_phone, policy="auto", scope=phone),
        ToolSpec(fn=phone_media, policy="auto", scope=phone), ToolSpec(fn=share_to_phone, policy="auto", scope=phone),
        ToolSpec(fn=call_on_phone, policy="auto", scope=phone),
        ToolSpec(fn=pair_phone, policy="confirm", scope=phone, describe=lambda a: f"pair {a.get('person') or 'your'} phone with KDE Connect"),
    ]
    return ToolModule(server="phone", tools=specs, available=lambda cfg: cfg.features.kdeconnect, prompt=PROMPT)
