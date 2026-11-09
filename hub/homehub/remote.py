import asyncio
import collections
import http.client
import ipaddress
import json
import os
import re
import shutil
import ssl
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from homehub.brain.prompt import build_system_prompt
from homehub.brain.session import SessionError, SessionPool, build_options
from homehub.minecraft import McError, UPLOAD_LIMIT
from homehub.packages import PackageError
from homehub.quick import Quick
from homehub.sysinfo import port_open
from homehub.tools import CallContext, result_texts

CONFIRM_SECONDS = 120
CONTEXT_WAIT_SECONDS = 30
MAX_BODY = 8192
FILE_BODY = 1_200_000
KEEP_LINES = 200
DOWN_KINDS = ("offline", "unreachable", "usage_limit", "auth", "billing")
BUSY_TEXT = "I'm still working on your last message."
POWER_ACTIONS = ("assistant", "hub", "off")
TV_KEYS = ("VolumeUp", "VolumeDown", "Mute", "Home", "Return", "Up", "Down", "Left", "Right", "Confirm", "PlayPause")
ROKU_KEYS = ("Home", "Back", "Up", "Down", "Left", "Right", "Select", "Play", "Rev", "Fwd", "InstantReplay", "Info", "VolumeUp",
             "VolumeDown", "VolumeMute")
MUSIC_ACTIONS = ("pause", "resume", "next", "previous")
CONTROL_SECONDS = 60
APP_CHECK_SECONDS = 2
PROXY_DOWN = (502, 503, 504)


def text_arg(body, key, limit=200):
    value = " ".join(str(body.get(key) or "").split())
    if not value or len(value) > limit:
        raise ValueError(f"{key} is missing or too long")
    return value


def control_call(action, body):
    if action == "timer.cancel":
        return "mcp__time__cancel_timer", {"which": text_arg(body, "id", 64)}
    if action in ("list.add", "list.remove"):
        tool = "mcp__lists__add_to_list" if action == "list.add" else "mcp__lists__remove_from_list"
        return tool, {"list": text_arg(body, "list", 60), "items": [text_arg(body, "item")]}
    if action == "tv.power":
        return "mcp__tv__tv_power", {"on": bool(body.get("on"))}
    if action == "tv.key":
        if body.get("key") not in TV_KEYS:
            raise ValueError("unknown TV key")
        return "mcp__tv__tv_remote", {"keys": [body["key"]]}
    if action == "tv.app":
        return "mcp__tv__tv_app", {"name": text_arg(body, "name", 40)}
    if action == "roku.key":
        if body.get("key") not in ROKU_KEYS:
            raise ValueError("unknown Roku button")
        return "mcp__roku__roku_remote", {"keys": [body["key"]]}
    if action == "roku.now":
        return "mcp__roku__roku_status", {}
    if action == "roku.app":
        return "mcp__roku__roku_app", {"name": text_arg(body, "name", 40)}
    if action == "music.control":
        if body.get("do") in MUSIC_ACTIONS:
            return "mcp__spotify__spotify_control", {"action": body["do"]}
        if body.get("do") == "volume":
            return "mcp__spotify__spotify_control", {"action": "volume", "value": str(max(0, min(100, int(body.get("value")))))}
        raise ValueError("unknown music action")
    if action == "music.now":
        return "mcp__spotify__spotify_now", {}
    if action == "ps5.now":
        return "mcp__ps5__ps5_status", {}
    if action == "ps5.power":
        return "mcp__ps5__ps5_power", {"on": bool(body.get("on"))}
    if action == "proxmox.now":
        return "mcp__proxmox__proxmox_status", {}
    if action == "proxmox.power":
        return "mcp__proxmox__proxmox_power", {"action": "on" if body.get("on") else "shutdown"}
    if action == "weather.now":
        return "mcp__weather__get_weather", {"days": 1}
    raise ValueError("unknown control")
ASSETS = ("app.html", "app.js", "mc.js", "packages.js", "sw.js", "register.js", "app.css", "manifest.json", "icon.svg", "icon-192.png", "icon-512.png", "icon-maskable.png",
          "apple-touch-icon.png")
TYPES = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8",
         "json": "application/manifest+json", "svg": "image/svg+xml", "png": "image/png"}


def parse_peers(entries):
    peers = {}
    for entry in entries:
        address, _, person = entry.partition("=")
        if not address.strip() or not person.strip():
            raise ValueError(f"remote peer {entry!r} must look like 10.77.0.2=person_id")
        peers[address.strip()] = person.strip()
    return peers


def app_answers(app):
    try:
        ipaddress.ip_address(app["host"])
        return port_open(app["host"], app["port"])
    except ValueError:
        pass
    unchecked = ssl.create_default_context()
    unchecked.check_hostname = False
    unchecked.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=unchecked))
    request = urllib.request.Request(app["url"], headers={"User-Agent": "homehub-app-check"})
    try:
        with opener.open(request, timeout=APP_CHECK_SECONDS) as response:
            return response.status not in PROXY_DOWN
    except HTTPError as e:
        return e.code not in PROXY_DOWN
    except (URLError, TimeoutError, OSError, ValueError, http.client.HTTPException):
        return False


def parse_apps(entries):
    apps = []
    for entry in entries:
        name, _, url = entry.partition("=")
        parts = urlsplit(url.strip())
        if not name.strip() or parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"remote app {entry!r} must look like Name=https://10.77.0.1:9090")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        apps.append({"name": name.strip(), "url": url.strip(), "host": parts.hostname, "port": port})
    return apps


class RemoteChat:
    def __init__(self, cfg, registry, permissions, storage, costs, emit, quick=None, warm=False):
        self.cfg, self.registry, self.permissions, self.storage, self.costs = cfg, registry, permissions, storage, costs
        self.emit = emit
        self.quick = quick if quick is not None else Quick(cfg, registry, permissions, storage)
        self.warm = warm
        self.pools = {}
        self.claude_down = None
        self.last_at = {}
        self.contexts = {}
        self.busy = set()
        self.pending = {}
        self.lines = collections.defaultdict(lambda: collections.deque(maxlen=KEEP_LINES))
        self.lock = threading.Lock()

    def line(self, person_id, kind, text, **extra):
        entry = {"at": datetime.now().strftime("%H:%M"), "kind": kind, "text": text, **extra}
        with self.lock:
            self.lines[person_id].append(entry)
        self.emit("transcript.line", room="remote", kind=kind if kind in ("you", "claude", "info", "error") else "action", text=text)

    def state(self, person_id):
        with self.lock:
            lines = list(self.lines[person_id])
        pending = self.pending.get(person_id)
        return {"lines": lines, "busy": person_id in self.busy, "claude_down": self.claude_down,
                "confirm": {"id": pending["id"], "summary": pending["summary"]} if pending else None}

    def context(self, person, conversation_id):
        ctx = CallContext(room=None, conversation_id=conversation_id, person_id=person["id"], person_name=person["name"],
                          role=person["role"], confidence="vpn_key", emit=self.emit)
        self.emit("person.identified", room="remote", person_id=person["id"], how="vpn_key")
        return ctx

    async def confirm(self, person_id, summary, ask_once):
        future = asyncio.get_running_loop().create_future()
        ask_id = uuid.uuid4().hex[:8]
        self.pending[person_id] = {"id": ask_id, "summary": summary, "future": future}
        self.line(person_id, "confirm", f"OK to {summary}?")
        try:
            answer = await asyncio.wait_for(future, CONFIRM_SECONDS)
        except TimeoutError:
            answer = False
        finally:
            self.pending.pop(person_id, None)
        self.line(person_id, "action", "approved" if answer else "declined")
        return answer

    def answer(self, person_id, ask_id, yes):
        pending = self.pending.get(person_id)
        if pending is None or pending["id"] != ask_id or pending["future"].done():
            return False
        pending["future"].get_loop().call_soon_threadsafe(lambda: pending["future"].done() or pending["future"].set_result(bool(yes)))
        return True

    def options_factory(self, person):
        prompt = build_system_prompt(self.cfg, self.registry, self.storage, person)

        async def can_use_tool(tool_name, tool_input, sdk_context):
            ctx = self.contexts.get(person["id"])
            if ctx is None:
                from claude_agent_sdk import PermissionResultDeny

                return PermissionResultDeny(message="No chat message is being handled.")
            return await self.permissions.can_use_tool(tool_name, tool_input, sdk_context, ctx,
                                                       lambda summary, once: self.confirm(person["id"], summary, once))

        return build_options(self.cfg, self.registry, prompt, can_use_tool)

    async def pool_for(self, person):
        pool = self.pools.get(person["id"])
        idle = time.monotonic() - self.last_at.get(person["id"], 0.0)
        if pool is not None and idle > self.cfg.brain.context_minutes * 60:
            await pool.rotate()
        if pool is None:
            pool = SessionPool(lambda: self.options_factory(person), emit=self.emit, keep_spare=False, warm=self.warm)
            self.pools[person["id"]] = pool
            await pool.start()
        return pool

    async def claim_tools(self, ctx):
        deadline = time.monotonic() + CONTEXT_WAIT_SECONDS
        while self.registry.context is not None and self.registry.context is not ctx:
            if time.monotonic() > deadline:
                return False
            await asyncio.sleep(0.2)
        self.registry.context = ctx
        return True

    async def ask(self, person_id, text):
        person = self.storage.person(person_id)
        text = " ".join(str(text).split())
        if person is None or not text:
            return None
        if person_id in self.busy:
            self.line(person_id, "info", BUSY_TEXT)
            return None
        self.busy.add(person_id)
        conversation_id = uuid.uuid4().hex[:8]
        ctx = self.context(person, conversation_id)
        self.line(person_id, "you", text)
        started = time.perf_counter()
        reason = "answered"
        self.storage.start_conversation(conversation_id, "remote", person_id, None)
        try:
            if not await self.claim_tools(ctx):
                self.line(person_id, "error", "The hub is busy with another request. Try again in a minute.")
                reason = "busy"
                return None
            self.contexts[person_id] = ctx
            found = await self.quick.handle(text, ctx)
            if found is not None:
                intent, reply = found
                latency = {"total_ms": round((time.perf_counter() - started) * 1000)}
                self.costs.record(conversation_id, person_id, "local", text, reply, 0, 0.0, latency)
                self.emit("quick.reply", room="remote", conversation_id=conversation_id, intent=intent, text=reply, latency_ms=latency)
                self.line(person_id, "claude", reply)
                return reply
            pool = await self.pool_for(person)
            self.emit("brain.request", room="remote", conversation_id=conversation_id, text=text, person_id=person_id)
            try:
                reply = await pool.ask(text, None, ctx)
            except SessionError as e:
                self.emit("brain.error", room="remote", kind=e.kind, message=str(e), resets_at=e.resets_at)
                if e.kind in DOWN_KINDS:
                    self.claude_down = e.spoken
                self.line(person_id, "error", e.spoken)
                reason = "error"
                return None
            self.claude_down = None
            last = getattr(pool.active, "info", {}).get("last", {})
            latency = {"total_ms": round((time.perf_counter() - started) * 1000)}
            self.costs.record(conversation_id, person_id, last.get("model"), text, reply, last.get("steps"), last.get("usd"), latency)
            self.emit("brain.reply", room="remote", conversation_id=conversation_id, text=reply, steps=last.get("steps"),
                      cost_usd=last.get("usd"), latency_ms=latency)
            self.line(person_id, "claude", reply)
            return reply
        except Exception as e:
            self.line(person_id, "error", f"Something went wrong: {e}")
            reason = "error"
            return None
        finally:
            if self.registry.context is ctx:
                self.registry.context = None
            self.contexts.pop(person_id, None)
            self.storage.end_conversation(conversation_id, reason)
            self.last_at[person_id] = time.monotonic()
            self.busy.discard(person_id)

    def controls(self):
        has = lambda name: self.registry.spec(name) is not None
        return {"timers": has("mcp__time__cancel_timer"), "lists": has("mcp__lists__add_to_list"),
                "tv": has("mcp__tv__tv_remote"), "roku": has("mcp__roku__roku_remote"), "music": has("mcp__spotify__spotify_control"),
                "ps5": has("mcp__ps5__ps5_power"), "proxmox": has("mcp__proxmox__proxmox_power"),
                "weather": has("mcp__weather__get_weather")}

    async def control(self, person_id, action, body):
        try:
            name, args = control_call(action, body)
        except (TypeError, ValueError) as e:
            return False, str(e)
        if action == "timer.cancel" and self.storage.reminder(args["which"]) is None:
            return False, "That one already finished or was cancelled."
        spec = self.registry.spec(name)
        person = self.storage.person(person_id)
        if spec is None or person is None:
            return False, "That isn't set up on the hub."
        ctx = self.context(person, uuid.uuid4().hex[:8])
        decision = self.permissions.decide(name, args, ctx)
        self.permissions.record(name, args, decision, ctx)
        if not decision.allow:
            return False, decision.message or "Not allowed."
        if not await self.claim_tools(ctx):
            return False, "The hub is busy with another request. Try again in a minute."
        try:
            result = await self.registry.wrap(spec, name.split("__")[1]).handler(args)
        finally:
            if self.registry.context is ctx:
                self.registry.context = None
        texts = result_texts(result)
        text = texts[0] if texts else ""
        if spec.untrusted_output and not result.get("is_error"):
            text = text.split("\n", 1)[-1]
        return not result.get("is_error", False), text

    async def close(self):
        for pool in self.pools.values():
            await pool.close()
        self.pools.clear()


class RemoteApp:
    def __init__(self, host, port, address, peers, chat, loop, status=None, system=None, power=None, apps=(), minecraft=None, forwards=None, packages=None, tls=None,
                 devices=None):
        self.host, self.port, self.address = host, port, address
        self.peers = dict(peers)
        self.chat = chat
        self.loop = loop
        self.status = status or (lambda: {})
        self.system = system or (lambda: {})
        self.power = power
        self.apps = list(apps)
        self.minecraft = minecraft
        self.forwards = forwards
        self.packages = packages
        self.devices = dict(devices or {})
        self.tls = tls
        self.tls_server = None
        self.tls_thread = None
        self.tls_cache = (None, None)
        self.assets = {name: resources.files("homehub.remote_app").joinpath(name).read_bytes() for name in ASSETS}
        self.server = None
        self.thread = None

    def person_for(self, peer, local):
        if local != self.address:
            return None
        return self.peers.get(peer)

    def known_host(self, header):
        name = re.sub(r":\d+$", "", (header or "").strip().lower())
        return name in {self.address.lower(), (self.tls or {}).get("name", "").lower()} - {""}

    def app_list(self):
        if not self.apps:
            return []
        with ThreadPoolExecutor(max_workers=len(self.apps)) as pool:
            ups = list(pool.map(app_answers, self.apps))
        return [{"name": a["name"], "url": a["url"], "up": up} for a, up in zip(self.apps, ups)]

    def tls_context(self):
        stamp = tuple(os.stat(self.tls[k]).st_mtime_ns for k in ("cert", "key"))
        if self.tls_cache[0] != stamp:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.tls["cert"], self.tls["key"])
            self.tls_cache = (stamp, context)
        return self.tls_cache[1]

    def https_url(self, path="/"):
        port = "" if self.tls["port"] == 443 else f":{self.tls['port']}"
        return f"https://{self.tls['name']}{port}{path}"

    def person_name(self, person_id):
        person = self.chat.storage.person(person_id)
        return person["name"] if person else person_id

    def mc_get(self, parts, query):
        mc = self.minecraft
        one = lambda key, default="": query.get(key, [default])[0]
        if parts == []:
            return {"servers": mc.list()}
        if parts == ["versions"]:
            return {"versions": mc.catalog.loader_versions(one("loader"))}
        if parts == ["mods"]:
            return mc.catalog.mods(one("loader"), one("version"), one("q"), int(one("offset", "0") or 0))
        if len(parts) >= 2 and parts[0] == "s":
            slug, rest = parts[1], parts[2:]
            if rest == []:
                return mc.detail(slug)
            if rest == ["log"]:
                return {"text": mc.log(slug)}
            if rest == ["files"]:
                return mc.files(slug, one("path"))
            if rest == ["modfiles"]:
                return mc.mod_files(slug)
        return None

    def mc_post(self, parts, body, person):
        mc = self.minecraft
        if parts == []:
            return mc.create(body, person)
        if len(parts) == 3 and parts[0] == "s":
            slug, what = parts[1], parts[2]
            if what == "action":
                return {"ok": mc.action(slug, str(body.get("action", "")), person, str(body.get("confirm", "")))}
            if what == "command":
                chat_as = self.person_name(person) if body.get("chat") else None
                return {"ok": True, "reply": mc.command(slug, body.get("text", ""), chat_as)}
            if what == "settings":
                return mc.update(slug, body, person)
            if what == "whitelist":
                return mc.change_whitelist(slug, str(body.get("name", "")), bool(body.get("add", True)), person)
            if what == "public":
                return {"public": mc.set_public(slug, bool(body.get("on")), person)}
            if what == "file":
                op, path = body.get("op"), str(body.get("path", ""))
                if op == "write":
                    mc.write_text(slug, path, str(body.get("text", "")))
                elif op == "delete":
                    mc.delete_path(slug, path)
                elif op == "mkdir":
                    mc.make_dir(slug, path)
                else:
                    raise McError("unknown file operation")
                return {"ok": True}
        return None

    def package_action(self, body):
        tracker, action = self.packages, body.get("action")
        if action == "refresh":
            return {"found": tracker.scan(), **tracker.state()}
        if action == "add":
            tracker.add(str(body.get("number", "")), str(body.get("what", "")))
            return tracker.state()
        if action == "hide":
            if not tracker.hide(str(body.get("key", ""))):
                raise PackageError("No such package")
            return tracker.state()
        if action == "connect_url":
            return {"url": tracker.connect_url()}
        if action == "connect":
            return {"found": tracker.connect(str(body.get("address", ""))), **tracker.state()}
        raise PackageError("unknown package action")

    def who(self, person_id, ip):
        person = self.chat.storage.person(person_id) if self.chat is not None else None
        return {"person": person_id, "role": (person or {}).get("role", ""), "ip": ip}

    def peer_changed(self, ip, person_id):
        if person_id:
            self.peers[ip] = person_id
        else:
            self.peers.pop(ip, None)

    def submit(self, person_id, text):
        return asyncio.run_coroutine_threadsafe(self.chat.ask(person_id, text), self.loop)

    def start(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def who(self):
                person = app.person_for(self.client_address[0], self.connection.getsockname()[0])
                if person is None:
                    self.reply(403, "text/plain; charset=utf-8", b"This page is only available through the household VPN.")
                elif not app.known_host(self.headers.get("Host")):
                    self.reply(403, "text/plain; charset=utf-8", b"This page only answers to its own name.")
                    return None
                return person

            def do_GET(self):
                person = self.who()
                if person is None:
                    return
                path = self.path.split("?", 1)[0]
                name = "app.html" if path in ("/", "/index.html") else path.lstrip("/")
                if app.tls and name == "app.html" and not isinstance(self.connection, ssl.SSLSocket):
                    self.send_response(308)
                    self.send_header("Location", app.https_url())
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if name in app.assets:
                    self.reply(200, TYPES[name.rsplit(".", 1)[1]], app.assets[name])
                elif path == "/api/state":
                    body = {"person": person, "chat": app.chat.state(person), "home": app.status(), "controls": app.chat.controls()}
                    self.reply(200, "application/json", json.dumps(body).encode("utf-8"))
                elif path == "/api/system":
                    body = {**app.system(), "power": app.power is not None}
                    self.reply(200, "application/json", json.dumps(body).encode("utf-8"))
                elif path == "/api/apps":
                    self.reply(200, "application/json", json.dumps({"apps": app.app_list()}).encode("utf-8"))
                elif path == "/api/features":
                    self.send_json(200, {"minecraft": app.minecraft is not None, "router": app.forwards is not None,
                                         "devices": sorted(app.devices),
                                         "packages": app.packages is not None and app.packages.owner_id == person})
                elif path == "/api/packages" and app.packages is not None:
                    if app.packages.owner_id != person:
                        self.send_json(403, {"error": "Packages come from the owner's email"})
                    else:
                        self.send_json(200, app.packages.state())
                elif path.startswith("/api/dev/") and path.removeprefix("/api/dev/") in app.devices:
                    self.guarded(lambda: app.devices[path.removeprefix("/api/dev/")].overview(app.who(person, self.client_address[0])))
                elif path == "/api/router/forwards" and app.forwards is not None:
                    self.guarded(lambda: {"forwards": app.forwards.list()})
                elif path.startswith("/api/mc") and app.minecraft is not None:
                    parts, query = self.mc_parts(path), parse_qs(urlsplit(self.path).query)
                    if len(parts) == 3 and parts[0] == "s" and parts[2] == "download":
                        self.download(parts, query)
                    else:
                        self.guarded(lambda: app.mc_get(parts, query))
                else:
                    self.reply(404, "text/plain; charset=utf-8", b"not found")

            def mc_parts(self, path):
                return [p for p in path.removeprefix("/api/mc").split("/") if p]

            def send_json(self, code, value):
                self.reply(code, "application/json", json.dumps(value).encode("utf-8"))

            def guarded(self, work):
                try:
                    result = work()
                except McError as e:
                    self.send_json(400, {"error": str(e)})
                    return
                except (URLError, TimeoutError, OSError) as e:
                    self.send_json(502, {"error": f"Couldn't reach it: {e}"})
                    return
                except (RuntimeError, ValueError) as e:
                    self.send_json(400, {"error": str(e)})
                    return
                if result is None:
                    self.send_json(404, {"error": "not found"})
                else:
                    self.send_json(200, result)

            def download(self, parts, query):
                try:
                    target = app.minecraft.download_path(parts[1], query.get("path", [""])[0])
                except (McError, IndexError) as e:
                    self.send_json(404, {"error": str(e)})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(target.stat().st_size))
                self.send_header("Content-Disposition", f"attachment; filename=\"{target.name}\"")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                with open(target, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)

            def do_PUT(self):
                person = self.who()
                if person is None:
                    return
                if self.path.startswith("/api/media/upload/"):
                    self.media_upload(person)
                    return
                if urlsplit(self.path).path == "/api/docs/upload":
                    self.docs_upload(person)
                    return
                parts = self.mc_parts(urlsplit(self.path).path)
                if app.minecraft is None or len(parts) != 3 or parts[0] != "s" or parts[2] != "upload":
                    self.send_json(404, {"error": "not found"})
                    return
                length = self.upload_length()
                if length is None:
                    return
                if length <= 0 or length > UPLOAD_LIMIT:
                    self.send_json(413, {"error": "Files up to 512 MB"})
                    self.close_connection = True
                    return
                query = parse_qs(urlsplit(self.path).query)
                self.continue_body()
                self.guarded(lambda: {"saved": app.minecraft.upload(parts[1], query.get("dir", [""])[0], query.get("name", [""])[0],
                                                                    self.rfile, length)})

            def media_upload(self, person):
                if "media" not in app.devices:
                    self.send_json(404, {"error": "not found"})
                    return
                length = self.upload_length()
                if length is None:
                    return
                upload_id = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
                try:
                    offset = int(parse_qs(urlsplit(self.path).query).get("offset", ["-1"])[0])
                except ValueError:
                    self.send_json(400, {"error": "bad upload request"})
                    return
                self.continue_body()
                self.guarded(lambda: app.devices["media"].write(upload_id, offset, self.rfile, length, app.who(person, self.client_address[0])))

            def docs_upload(self, person):
                if "docs" not in app.devices:
                    self.send_json(404, {"error": "not found"})
                    return
                length = self.upload_length()
                if length is None:
                    return
                name = parse_qs(urlsplit(self.path).query).get("name", [""])[0]
                self.continue_body()
                self.guarded(lambda: app.devices["docs"].upload(name, self.rfile, length, app.who(person, self.client_address[0])))

            def upload_length(self, wrong_type="send the file as application/octet-stream"):
                origin = self.headers.get("Origin")
                if origin and origin.split("://", 1)[-1] != self.headers.get("Host", ""):
                    self.send_json(403, {"error": "cross-site request refused"})
                    return None
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/octet-stream":
                    self.send_json(415, {"error": wrong_type})
                    return None
                try:
                    return int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self.send_json(400, {"error": "bad upload request"})
                    return None

            def continue_body(self):
                if self.headers.get("Expect", "").strip().lower() == "100-continue":
                    self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                    self.wfile.flush()

            def do_POST(self):
                person = self.who()
                if person is None:
                    return
                big = self.path.startswith("/api/mc/") and self.path.endswith("/file")
                body = self.json_body(FILE_BODY if big else MAX_BODY)
                if body is None:
                    return
                if self.path == "/api/chat":
                    text = str(body.get("text", "")).strip()
                    if not text:
                        self.reply(400, "application/json", b'{"error": "empty message"}')
                        return
                    app.submit(person, text)
                    self.reply(202, "application/json", b'{"ok": true}')
                elif self.path == "/api/confirm":
                    ok = app.chat.answer(person, str(body.get("id", "")), bool(body.get("yes")))
                    self.reply(200 if ok else 409, "application/json", json.dumps({"ok": ok}).encode("utf-8"))
                elif self.path == "/api/control":
                    future = asyncio.run_coroutine_threadsafe(app.chat.control(person, str(body.get("action", "")), body), app.loop)
                    try:
                        ok, text = future.result(CONTROL_SECONDS)
                    except TimeoutError:
                        future.cancel()
                        ok, text = False, "That took too long. Try again."
                    self.reply(200 if ok else 422, "application/json", json.dumps({"ok": ok, "text": text}).encode("utf-8"))
                elif self.path.startswith("/api/mc") and app.minecraft is not None:
                    self.guarded(lambda: app.mc_post(self.mc_parts(self.path), body, person))
                elif self.path == "/api/packages" and app.packages is not None:
                    if app.packages.owner_id != person:
                        self.send_json(403, {"error": "Packages come from the owner's email"})
                    else:
                        self.guarded(lambda: app.package_action(body))
                elif self.path.startswith("/api/dev/") and self.path.removeprefix("/api/dev/") in app.devices:
                    self.guarded(lambda: app.devices[self.path.removeprefix("/api/dev/")].act(body, app.who(person, self.client_address[0])))
                elif self.path == "/api/power":
                    action = body.get("action")
                    if app.power is None or action not in POWER_ACTIONS:
                        self.reply(400, "application/json", b'{"ok": false, "message": "unknown action"}')
                        return
                    ok, message = app.power(action, person, body.get("wake_at") if action == "off" else None)
                    self.reply(202 if ok else 503, "application/json", json.dumps({"ok": ok, "message": message}).encode("utf-8"))
                else:
                    self.reply(404, "text/plain; charset=utf-8", b"not found")

            def json_body(self, limit=MAX_BODY):
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    self.reply(415, "text/plain; charset=utf-8", b"send JSON")
                    return None
                origin = self.headers.get("Origin")
                if origin and origin.split("://", 1)[-1] != self.headers.get("Host", ""):
                    self.reply(403, "text/plain; charset=utf-8", b"cross-site request refused")
                    return None
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > limit:
                    self.reply(413, "text/plain; charset=utf-8", b"message too long")
                    return None
                self.continue_body()
                try:
                    body = json.loads(self.rfile.read(length))
                except ValueError:
                    self.reply(400, "text/plain; charset=utf-8", b"bad JSON")
                    return None
                if not isinstance(body, dict):
                    self.reply(400, "text/plain; charset=utf-8", b"bad JSON")
                    return None
                return body

            def reply(self, code, kind, body):
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
                if isinstance(self.connection, ssl.SSLSocket):
                    self.send_header("Strict-Transport-Security", "max-age=31536000")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, name="remote-app", daemon=True)
        self.thread.start()
        if self.tls:
            self.tls_context()

            class TlsServer(ThreadingHTTPServer):
                def get_request(self):
                    sock, address = self.socket.accept()
                    sock.settimeout(20)
                    return app.tls_context().wrap_socket(sock, server_side=True, do_handshake_on_connect=False), address

                def finish_request(self, request, client_address):
                    try:
                        request.do_handshake()
                    except (ssl.SSLError, OSError):
                        return
                    super().finish_request(request, client_address)

            self.tls_server = TlsServer((self.tls.get("host", self.address), self.tls["port"]), Handler)
            self.tls["port"] = self.tls_server.server_address[1]
            self.tls_thread = threading.Thread(target=self.tls_server.serve_forever, name="remote-app-tls", daemon=True)
            self.tls_thread.start()
        return self

    def stop(self):
        for server in (self.server, self.tls_server):
            if server is not None:
                server.shutdown()
                server.server_close()
        self.server = self.tls_server = None
