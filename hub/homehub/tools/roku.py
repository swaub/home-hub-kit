import asyncio
import base64
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result
from homehub.tools.router import RouterReader
from homehub.tools.tv import best_match

PROMPT = ("Roku players are controlled over the home network. roku_status says what a Roku is showing (an app, the home screen "
          "or the screensaver) and what's playing; roku_app opens an app by name; roku_remote presses remote buttons; roku_search opens "
          "the Roku's search with words already typed in, the way to find a show or movie across its apps; roku_type types text into a "
          "box that's already open, like a sign-in field. The remote's Search button starts voice search, so don't press it to type. "
          "The Sony TV has its own tv_ tools.")
PORT = 8060
TIMEOUT = 5
MAX_PRESSES = 20
MAX_TEXT = 100
MAX_ICON = 300_000
PANEL_KEYS = ("Home", "Back", "Up", "Down", "Left", "Right", "Select", "Play", "Rev", "Fwd", "InstantReplay", "Info",
              "VolumeUp", "VolumeDown", "VolumeMute", "Backspace", "Enter")
KEYS = ("Home", "Back", "Up", "Down", "Left", "Right", "Select", "Play", "Rev", "Fwd", "InstantReplay", "Info", "Search", "Backspace",
        "Enter", "VolumeUp", "VolumeDown", "VolumeMute", "PowerOn", "PowerOff", "FindRemote")
ALIASES = {"ok": "Select", "confirm": "Select", "return": "Back", "playpause": "Play", "pause": "Play", "rewind": "Rev",
           "forward": "Fwd", "fastforward": "Fwd", "replay": "InstantReplay", "options": "Info", "star": "Info", "mute": "VolumeMute",
           "on": "PowerOn", "off": "PowerOff"}
LIMITED = ("The Roku only allows limited control: on the Roku, open Settings, System, Advanced system settings, Control by mobile apps, "
           "Network access, and pick Enabled (Default on older Rokus).")
MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
POWER = {"PowerOn": "on", "DisplayOff": "on with the screen off", "Ready": "asleep", "Headless": "on with no TV signal"}


class RokuError(RuntimeError):
    pass


def parse_devices(entries):
    devices = {}
    for entry in entries:
        name, _, host = entry.partition("=")
        name, host = name.strip(), host.strip()
        if not name or not host:
            raise ValueError(f"roku devices are written 'Name=MAC' or 'Name=address', not {entry!r}")
        mac = host.upper().replace("-", ":")
        devices[name] = mac if MAC.match(mac) else host
    return devices


def key_name(key):
    wanted = key.strip()
    for name in KEYS:
        if name.lower() == wanted.lower():
            return name
    return ALIASES.get(wanted.lower().replace(" ", "").replace("_", ""))


def plain(name):
    return name.replace("+", " plus ")


def find_app(name, options):
    return best_match(plain(name), [{**o, "title": plain(o["title"])} for o in options])


def text_of(root, tag):
    node = root.find(tag)
    return (node.text or "").strip() if node is not None else ""


class Locator:
    def __init__(self, router, path=None):
        self.router = router
        self.path = path
        self.lock = threading.Lock()
        self.found = {}

    def saved(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) if self.path else {}
        except (OSError, ValueError):
            return {}

    def remember(self, mac, ip):
        self.found[mac] = ip
        if self.path is not None and self.saved().get(mac) != ip:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({**self.saved(), mac: ip}, indent=2), encoding="utf-8")

    def address(self, name, mac):
        with self.lock:
            if mac in self.found:
                return self.found[mac]
            try:
                rows = self.router.read()
                problem = None if rows is not None else f"The {name} is listed by its MAC address, and the router isn't set up to look it up."
            except Exception as e:
                rows, problem = None, f"I couldn't ask the router where the {name} is: {type(e).__name__}: {e}"
            for row in rows or []:
                if row["mac"] == mac and row["ip"]:
                    self.remember(mac, row["ip"])
                    return row["ip"]
            last = self.saved().get(mac)
            if last:
                self.found[mac] = last
                return last
            raise RokuError(problem or f"The router doesn't see the {name} on the network right now.")

    def forget(self, mac):
        with self.lock:
            self.found.pop(mac, None)


class Roku:
    def __init__(self, name, where, locator=None):
        self.name = name
        self.where = where
        self.locator = locator

    @property
    def host(self):
        if MAC.match(self.where):
            if self.locator is None:
                raise RokuError(f"The {self.name} is listed by its MAC address, and the router isn't set up to look it up.")
            return self.locator.address(self.name, self.where)
        return self.where

    def call(self, path, method="GET"):
        host = self.host
        request = urllib.request.Request(f"http://{host}:{PORT}{path}", data=b"" if method == "POST" else None, method=method)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if "Limited mode" in body or e.code == 403:
                raise RokuError(LIMITED) from e
            raise RokuError(f"The {self.name} refused that (HTTP {e.code}).") from e
        except OSError as e:
            if self.locator is not None:
                self.locator.forget(self.where)
            raise RokuError(f"The {self.name} didn't answer at {host}: {e}") from e
        if "Limited mode" in body:
            raise RokuError(LIMITED)
        return body

    def query(self, what):
        try:
            return ET.fromstring(self.call(f"/query/{what}"))
        except ET.ParseError as e:
            raise RokuError(f"The {self.name} sent an unreadable answer to {what}.") from e

    def info(self):
        root = self.query("device-info")
        return {"name": text_of(root, "user-device-name"), "model": text_of(root, "friendly-model-name"),
                "power": text_of(root, "power-mode"), "control": text_of(root, "ecp-setting-mode"), "is_tv": text_of(root, "is-tv") == "true"}

    def active(self):
        root = self.query("active-app")
        app, saver = root.find("app"), root.find("screensaver")
        return {"app": (app.text or "").strip() if app is not None else "", "app_id": app.get("id", "") if app is not None else "",
                "type": app.get("type", "") if app is not None else "", "screensaver": (saver.text or "").strip() if saver is not None else ""}

    def player(self):
        root = self.query("media-player")
        position = text_of(root, "position").split()[0] if text_of(root, "position") else ""
        duration = text_of(root, "duration").split()[0] if text_of(root, "duration") else ""
        return {"state": root.get("state", ""), "position": int(position) // 1000 if position.isdigit() else None,
                "duration": int(duration) // 1000 if duration.isdigit() else None}

    def apps(self):
        return [{"title": (a.text or "").strip(), "uri": a.get("id", "")} for a in self.query("apps").findall("app")]

    def press(self, key):
        self.call(f"/keypress/{urllib.parse.quote(key, safe='')}", "POST")

    def launch(self, app_id):
        self.call(f"/launch/{urllib.parse.quote(app_id, safe='')}", "POST")

    def icon(self, app_id):
        request = urllib.request.Request(f"http://{self.host}:{PORT}/query/icon/{urllib.parse.quote(app_id, safe='')}")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                kind = response.headers.get_content_type()
                data = response.read(MAX_ICON + 1)
        except OSError as e:
            raise RokuError(f"The {self.name} didn't send that app's icon: {e}") from e
        if not kind.startswith("image/") or len(data) > MAX_ICON:
            raise RokuError(f"The {self.name} sent something other than an icon.")
        return f"data:{kind};base64,{base64.b64encode(data).decode()}"

    def search(self, text):
        self.call(f"/search/browse?keyword={urllib.parse.quote(text, safe='')}", "POST")

    def type_text(self, text):
        for ch in text:
            self.call(f"/keypress/Lit_{urllib.parse.quote(ch, safe='')}", "POST")


def clock(seconds):
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def describe(roku):
    info = roku.info()
    power = POWER.get(info["power"], info["power"] or "on")
    if info["power"] in ("Ready", "Headless"):
        return f"The {roku.name} ({info['model']}) is {power}."
    if info["control"] == "limited":
        return f"The {roku.name} ({info['model']}) is {power}. " + LIMITED
    active = roku.active()
    if active["screensaver"]:
        showing = f"the screensaver ({active['screensaver']}) over {active['app'] or 'the home screen'}"
    elif not active["app"] or active["app"] == "Roku":
        showing = "the home screen"
    else:
        showing = active["app"]
    line = f"The {roku.name} ({info['model']}) is {power}, showing {showing}."
    if active["app"] and active["app"] != "Roku" and not active["screensaver"]:
        play = roku.player()
        if play["state"] in ("play", "pause", "buffer"):
            where = ""
            if play["position"] is not None:
                where = f" at {clock(play['position'])}" + (f" of {clock(play['duration'])}" if play["duration"] else "")
            line += f" Video is {'paused' if play['state'] == 'pause' else 'playing'}{where}."
    return line


class RokuPanel:
    def __init__(self, cfg, router=None):
        self.cfg = cfg
        self.locator = Locator(router or RouterReader(cfg), cfg.data_dir() / "roku-addresses.json")
        self.icons = {}

    def roku(self):
        devices = parse_devices(self.cfg.roku.devices)
        if not devices:
            raise RokuError("No Roku is set up: add one to [roku] devices in config.toml.")
        name, where = next(iter(devices.items()))
        return Roku(name, where, self.locator)

    def overview(self, who=None):
        roku = self.roku()
        info = roku.info()
        view = {"name": roku.name, "model": info["model"], "power": POWER.get(info["power"], info["power"]), "on": info["power"] in ("PowerOn", "DisplayOff"),
                "limited": info["control"] == "limited", "apps": [], "active": None, "screensaver": "", "player": None}
        if view["limited"]:
            view["notice"] = LIMITED
            return view
        view["apps"] = [{"id": a["uri"], "title": a["title"]} for a in roku.apps()]
        active = roku.active()
        view["screensaver"] = active["screensaver"]
        if active["app"] and active["app"] != "Roku" and active["type"] != "home":
            view["active"] = {"id": active["app_id"], "title": active["app"]}
            play = roku.player()
            if play["state"] in ("play", "pause", "buffer"):
                view["player"] = play
        return view

    def act(self, body, who=None):
        roku = self.roku()
        action = str(body.get("action", ""))
        if action == "key":
            key = str(body.get("key", ""))
            if key not in PANEL_KEYS:
                raise RokuError("That button isn't on the app's Roku remote.")
            roku.press(key)
            return {"text": f"Pressed {key}."}
        if action == "launch":
            chosen = next((a for a in roku.apps() if a["uri"] == str(body.get("id", ""))), None)
            if chosen is None:
                raise RokuError(f"The {roku.name} has no such app.")
            roku.launch(chosen["uri"])
            return {"text": f"Opened {chosen['title']}."}
        if action == "search":
            text = " ".join(str(body.get("text") or "").split())
            if not text or len(text) > MAX_TEXT:
                raise RokuError(f"Search for one to {MAX_TEXT} characters.")
            roku.search(text)
            return {"text": f"Searching the {roku.name} for {text}."}
        if action == "type":
            text = str(body.get("text") or "")
            if not text.strip() or len(text) > MAX_TEXT:
                raise RokuError(f"Type one to {MAX_TEXT} characters.")
            roku.type_text(text)
            return {"text": f"Typed {text!r}."}
        if action == "icons":
            for app in roku.apps():
                if app["uri"] not in self.icons:
                    self.icons[app["uri"]] = roku.icon(app["uri"])
            return {"icons": self.icons}
        raise RokuError("unknown Roku action")


def build_module(cfg, router=None):
    locator = Locator(router or RouterReader(cfg), cfg.data_dir() / "roku-addresses.json")

    def rokus():
        return [Roku(name, where, locator) for name, where in parse_devices(cfg.roku.devices).items()]

    def pick(which):
        players = rokus()
        if not players:
            raise RokuError("No Roku is set up: add one to [roku] devices in config.toml.")
        if len(players) == 1:
            return players[0]
        names = ", ".join(p.name for p in players)
        if not str(which or "").strip():
            raise RokuError(f"Which Roku? There are {names}.")
        chosen = best_match(str(which), [{"title": p.name, "uri": i} for i, p in enumerate(players)])
        if chosen is None:
            raise RokuError(f"No Roku called {which}. There are {names}.")
        return players[chosen["uri"]]

    def run(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except RokuError as e:
                return text_result(str(e), error=True)
        return call

    def status(args):
        return describe(pick(args.get("roku")))

    def remote(args):
        roku = pick(args.get("roku"))
        keys = [k for k in (args.get("keys") or []) if str(k).strip()][:MAX_PRESSES]
        if not keys:
            raise RokuError("Name at least one button.")
        names = []
        for key in keys:
            name = key_name(str(key))
            if name is None:
                raise RokuError(f"The Roku remote has no {key} button. Buttons: {', '.join(KEYS)}.")
            names.append(name)
        for name in names:
            roku.press(name)
            time.sleep(0.3)
        return f"Pressed {', '.join(names)} on the {roku.name}."

    def open_app(args):
        roku = pick(args.get("roku"))
        options = roku.apps()
        chosen = find_app(str(args.get("name", "")), options)
        if chosen is None:
            raise RokuError(f"The {roku.name} has no app matching {args.get('name')!r}. Apps: {', '.join(sorted(o['title'] for o in options))}.")
        roku.launch(chosen["uri"])
        title = next(o["title"] for o in options if o["uri"] == chosen["uri"])
        return f"Opened {title} on the {roku.name}."

    def type_text(args):
        roku = pick(args.get("roku"))
        text = str(args.get("text") or "")
        if not text.strip() or len(text) > MAX_TEXT:
            raise RokuError(f"Give one to {MAX_TEXT} characters to type.")
        roku.type_text(text)
        return f"Typed {text!r} on the {roku.name}."

    def search(args):
        roku = pick(args.get("roku"))
        text = " ".join(str(args.get("text") or "").split())
        if not text or len(text) > MAX_TEXT:
            raise RokuError(f"Search for one to {MAX_TEXT} characters.")
        roku.search(text)
        return f"Opened search on the {roku.name} with {text!r} typed in."

    which = {"roku": {"type": "string", "description": "Which Roku, by name; leave out when there's only one."}}
    roku_status = tool("roku_status", "What a Roku is doing: on or asleep, which app or screensaver is showing, and whether video is "
                       "playing or paused and where.", {"type": "object", "properties": which})(run(status))
    roku_app = tool("roku_app", "Open an app on a Roku by name, like Netflix, YouTube, Disney+, Prime Video, Hulu or Max.",
                    {"type": "object", "properties": {**which, "name": {"type": "string"}}, "required": ["name"]})(run(open_app))
    roku_remote = tool("roku_remote", f"Press Roku remote buttons in order (up to {MAX_PRESSES}): {', '.join(KEYS)}. Play toggles play "
                       "and pause. Volume and power buttons reach the TV only when the TV accepts them from the Roku.",
                       {"type": "object", "properties": {**which, "keys": {"type": "array", "items": {"type": "string"}}},
                        "required": ["keys"]})(run(remote))
    roku_type = tool("roku_type", f"Type text (up to {MAX_TEXT} characters) into a text box that's already open on a Roku, like a sign-in "
                     "field.", {"type": "object", "properties": {**which, "text": {"type": "string"}}, "required": ["text"]})(run(type_text))
    roku_search = tool("roku_search", f"Open a Roku's search with words (up to {MAX_TEXT} characters) already typed in, to find a show, movie "
                       "or actor across its apps.", {"type": "object", "properties": {**which, "text": {"type": "string"}}, "required": ["text"]})(run(search))
    specs = [ToolSpec(fn=fn, policy="auto", scope=Scope.household()) for fn in (roku_status, roku_app, roku_remote, roku_type, roku_search)]
    return ToolModule(server="roku", prompt=PROMPT, tools=specs, available=lambda c: bool(c.roku.devices))
