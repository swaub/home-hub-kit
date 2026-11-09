import asyncio
import base64
import json
import socket
import urllib.parse
import re
import time
import tomllib
import urllib.request

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result
from homehub.tools.router import RouterReader
from homehub.tools.wake_on_lan import Waker

PROMPT = ("The TV is a Sony Bravia: tv_status says whether it's on and what it's showing; tv_power turns it on or off; tv_input "
          "switches inputs by name or label (like 'PC' or 'HDMI 4'); tv_app opens an app (Netflix, YouTube, Disney+...); tv_remote "
          "presses remote keys, including VolumeUp, VolumeDown and Mute, which also reach the soundbar. For 'turn it up a little' "
          "press VolumeUp about three times. tv_settings changes picture mode, brightness, the light sensor, whether sound comes from the "
          "soundbar or the TV speakers, turns the screen off while sound keeps playing, or closes the TV's apps.")
NOT_SET_UP = "The TV isn't set up: add a [tv] table with psk and mac (and host as a fallback) to secrets.toml in the data folder."
TIMEOUT = 6
MAX_PRESSES = 20
IRCC_BODY = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
             's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:X_SendIRCC '
             'xmlns:u="urn:schemas-sony-com:service:IRCC:1"><IRCCCode>{code}</IRCCCode></u:X_SendIRCC></s:Body></s:Envelope>')


class TvError(RuntimeError):
    pass


def load_settings(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        table = tomllib.load(f).get("tv") or {}
    if not table.get("psk") or not (table.get("mac") or table.get("host")):
        return None
    return {"psk": str(table["psk"]), "mac": str(table.get("mac", "")).upper().replace("-", ":"), "host": str(table.get("host", ""))}


def simple(text):
    return re.sub(r"[^a-z0-9+]+", "", text.lower().replace("&amp;", "and").replace("&", "and"))


def best_match(name, options):
    wanted = simple(name)
    if not wanted:
        return None
    exact = [o for o in options if wanted in (simple(o["title"]), simple(o.get("label", "")))]
    if exact:
        return exact[0]
    loose = [o for o in options if wanted in simple(o["title"]) or (o.get("label") and wanted in simple(o["label"]))]
    return min(loose, key=lambda o: len(o["title"])) if loose else None


class Bravia:
    def __init__(self, cfg, router=None):
        self.cfg = cfg
        self.router = router or RouterReader(cfg)
        self.codes = None
        self.host = None

    def settings(self):
        settings = load_settings(self.cfg)
        if settings is None:
            raise TvError(NOT_SET_UP)
        return settings

    def address(self, settings):
        if self.host:
            return self.host
        if settings["mac"]:
            try:
                rows = self.router.read() or []
            except Exception:
                rows = []
            for row in rows:
                if row["mac"] == settings["mac"] and row["ip"]:
                    self.host = row["ip"]
                    return self.host
        if settings["host"]:
            return settings["host"]
        raise TvError("I can't find the TV on the network right now.")

    def post(self, path, body, headers):
        settings = self.settings()
        host = self.address(settings)
        request = urllib.request.Request(f"http://{host}{path}", data=body, headers={"X-Auth-PSK": settings["psk"], **headers}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read().decode("utf-8")
        except OSError as e:
            self.host = None
            raise TvError(f"The TV didn't answer at {host}: {e}") from e

    def rpc(self, service, method, params=None, version="1.0"):
        body = json.dumps({"method": method, "id": 1, "params": [params] if params is not None else [], "version": version}).encode()
        reply = json.loads(self.post(f"/sony/{service}", body, {"Content-Type": "application/json"}) or "{}")
        if "error" in reply:
            code, message = (reply["error"] + ["", ""])[:2]
            raise TvError(f"{method}: error {code} {message}".strip())
        return reply.get("result") or []

    def power(self):
        return self.rpc("system", "getPowerStatus")[0]["status"]

    def set_power(self, on):
        settings = self.settings()
        if on and settings["mac"]:
            Waker().send(settings["mac"])
        try:
            self.rpc("system", "setPowerStatus", {"status": bool(on)})
        except TvError:
            if not on:
                raise
        deadline = time.monotonic() + (8 if on else 3)
        while time.monotonic() < deadline:
            try:
                if (self.power() == "active") == bool(on):
                    return True
            except TvError:
                pass
            time.sleep(0.5)
        return False

    def inputs(self):
        return self.rpc("avContent", "getCurrentExternalInputsStatus", None, "1.1")[0]

    def apps(self):
        return [{"title": a["title"].replace("&amp;", "&"), "uri": a["uri"]} for a in self.rpc("appControl", "getApplicationList")[0]]

    def playing(self):
        try:
            info = self.rpc("avContent", "getPlayingContentInfo")[0]
        except TvError:
            return "an app or the home screen"
        return info.get("title") or info.get("uri", "something")

    def key_codes(self):
        if self.codes is None:
            self.codes = {c["name"].lower(): c["value"] for c in self.rpc("system", "getRemoteControllerInfo")[1]}
        return self.codes

    def press(self, key):
        code = self.key_codes().get(key.lower())
        if code is None:
            raise TvError(f"The remote has no key called {key}.")
        self.post("/sony/IRCC", IRCC_BODY.format(code=code).encode(),
                  {"Content-Type": 'text/xml; charset="UTF-8"', "SOAPACTION": '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"'})


PICTURE_MODES = {"standard": "Standard", "vivid": "Vivid", "cinema": "Cinema", "movieFilmmakerExperienceProfessional": "Filmmaker",
                 "professional": "Professional", "calm": "Calm", "photo": "Photo", "imaxEnhanced": "IMAX Enhanced", "gameStandard": "Game",
                 "gameFps": "Game (shooters)", "gameRts": "Game (strategy)", "pcStandard": "PC"}
SYSTEM_APPS = {"BRAVIA notifications", "Calman for BRAVIA", "Eco Dashboard", "Help", "Living Decor", "Media Player", "My BRAVIA", "Settings",
               "Sony Select", "SONY PICTURES CORE", "TV Control with Smart Speakers", "Timers & Clock", "TV", "Play Games", "Play Movies & TV",
               "Music"}
OUTPUTS = {"audioSystem": "Soundbar", "speaker": "TV speakers"}


def friendly_input(row):
    title = row.get("title") or ""
    label = row.get("label") or ""
    return {"title": title, "label": label, "uri": row["uri"], "connected": bool(row.get("connection")), "active": False, "device": ""}


def tv_overview(tv):
    power = tv.power()
    on = power == "active"
    inputs = [friendly_input(i) for i in tv.inputs() if i["uri"].startswith("extInput:hdmi")]
    seen, apps = set(), []
    for app in tv.apps():
        if app["title"] not in SYSTEM_APPS and app["title"] not in seen:
            seen.add(app["title"])
            apps.append(app)
    view = {"on": on, "inputs": inputs, "apps": sorted(apps, key=lambda a: a["title"].lower()), "showing": None}
    if not on:
        return view
    try:
        info = tv.rpc("avContent", "getPlayingContentInfo")[0]
        uri = info.get("uri", "")
        port = re.search(r"port=(\d+)", uri)
        match = next((i for i in inputs if port and i["uri"] == f"extInput:hdmi?port={port.group(1)}"), None)
        device = info.get("title") if info.get("title") and not info.get("title", "").startswith("HDMI") else ""
        if match:
            match["active"] = True
            if device and match["connected"]:
                match["device"] = device
        view["showing"] = {"uri": uri, "title": device or (match["title"] if match else uri)}
    except TvError:
        pass
    view.update({"picture": None, "output": None, "screen_off": False, "settling": False})
    try:
        settings = {s["target"]: s for s in tv.rpc("video", "getPictureQualitySettings", {"target": ""})[0]}
        mode = settings.get("pictureMode", {})
        offered = [c.get("value") for c in mode.get("candidate", []) if c.get("value") in PICTURE_MODES]
        brightness = settings.get("brightness", {})
        limits = (brightness.get("candidate") or [{}])[0]
        view["picture"] = {"mode": mode.get("currentValue"), "modes": [{"value": m, "name": PICTURE_MODES[m]} for m in PICTURE_MODES if m in offered],
                           "brightness": int(brightness["currentValue"]) if brightness.get("isAvailable") else None,
                           "brightness_min": limits.get("min", 0), "brightness_max": limits.get("max", 50),
                           "light_sensor": settings.get("lightSensor", {}).get("currentValue") if settings.get("lightSensor", {}).get("isAvailable") else None}
        view["output"] = tv.rpc("audio", "getSoundSettings", {"target": "outputTerminal"}, "1.1")[0][0]["currentValue"]
        view["screen_off"] = tv.rpc("system", "getPowerSavingMode")[0]["mode"] == "pictureOff"
    except TvError:
        view["settling"] = True
    return view


def tv_act(tv, body):
    action = str(body.get("action", ""))
    if action == "power":
        on = bool(body.get("on"))
        if tv.set_power(on):
            return {"text": "The TV is on." if on else "The TV is off."}
        raise TvError("The TV didn't respond to the power signal; turn on Remote start in its network settings.")
    if tv.power() != "active":
        raise TvError("The TV is off. Turn it on first.")
    if action == "input":
        chosen = next((i for i in tv.inputs() if i["uri"] == body.get("uri") and i["uri"].startswith("extInput:hdmi")), None)
        if chosen is None:
            raise TvError("The TV has no such input.")
        tv.rpc("avContent", "setPlayContent", {"uri": chosen["uri"]})
        return {"text": f"Switched to {chosen['title']}."}
    if action == "app":
        chosen = next((a for a in tv.apps() if a["uri"] == body.get("uri")), None)
        if chosen is None:
            raise TvError("The TV has no such app.")
        tv.rpc("appControl", "setActiveApp", {"uri": chosen["uri"]})
        return {"text": f"Opened {chosen['title']}."}
    if action == "close_apps":
        tv.rpc("appControl", "terminateApps")
        return {"text": "Closed the TV's apps."}
    if action == "picture_mode":
        value = str(body.get("value", ""))
        if value not in PICTURE_MODES:
            raise TvError("Unknown picture mode.")
        tv.rpc("video", "setPictureQualitySettings", {"settings": [{"target": "pictureMode", "value": value}]})
        return {"text": f"Picture mode: {PICTURE_MODES[value]}."}
    if action == "brightness":
        value = int(body.get("value"))
        if not 0 <= value <= 50:
            raise TvError("Brightness goes from 0 to 50.")
        tv.rpc("video", "setPictureQualitySettings", {"settings": [{"target": "brightness", "value": str(value)}]})
        return {"text": f"Brightness {value}."}
    if action == "light_sensor":
        value = "on" if body.get("on") else "off"
        tv.rpc("video", "setPictureQualitySettings", {"settings": [{"target": "lightSensor", "value": value}]})
        return {"text": f"Light sensor {value}."}
    if action == "output":
        value = str(body.get("value", ""))
        if value not in OUTPUTS:
            raise TvError("Sound goes to the soundbar or the TV speakers.")
        tv.rpc("audio", "setSoundSettings", {"settings": [{"target": "outputTerminal", "value": value}]}, "1.1")
        return {"text": f"Sound from the {OUTPUTS[value].lower()}."}
    if action == "screen":
        off = bool(body.get("off"))
        tv.rpc("system", "setPowerSavingMode", {"mode": "pictureOff" if off else "off"})
        return {"text": "Screen off; sound keeps playing. Press any button on the remote to bring it back." if off else "Screen on."}
    raise TvError("unknown TV action")


PNG_END = b"IEND\xaeB`\x82"
MAX_ICON = 300_000


def dechunk(raw):
    body, rest = b"", raw
    while rest:
        size_line, sep, rest = rest.partition(b"\r\n")
        if not sep:
            break
        try:
            size = int(size_line.split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        body, rest = body + rest[:size], rest[size + 2:]
    return body


def fetch_icon(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        raise TvError("The TV gave an icon address the hub won't follow.")
    raw = b""
    with socket.create_connection((parts.hostname, parts.port or 80), timeout=TIMEOUT) as sock:
        sock.sendall(f"GET {parts.path} HTTP/1.1\r\nHost: {parts.hostname}\r\nConnection: close\r\n\r\n".encode())
        while PNG_END not in raw and len(raw) < MAX_ICON:
            try:
                piece = sock.recv(65536)
            except socket.timeout:
                break
            if not piece:
                break
            raw += piece
    head, _, body = raw.partition(b"\r\n\r\n")
    if not head.startswith(b"HTTP/1.1 200"):
        raise TvError("The TV didn't send that app's icon.")
    if b"chunked" in head.lower():
        body = dechunk(body)
    end = body.find(PNG_END)
    if not body.startswith(b"\x89PNG") or end < 0:
        raise TvError("The TV sent an incomplete icon.")
    return "data:image/png;base64," + base64.b64encode(body[:end + len(PNG_END)]).decode()


class TvPanel:
    def __init__(self, cfg):
        self.tv = Bravia(cfg)
        self.icons = {}

    def overview(self, who=None):
        return tv_overview(self.tv)

    def act(self, body, who=None):
        if body.get("action") == "icons":
            for app in self.tv.rpc("appControl", "getApplicationList")[0]:
                if app["uri"] not in self.icons and app.get("icon") and app["title"] not in SYSTEM_APPS:
                    try:
                        self.icons[app["uri"]] = fetch_icon(app["icon"])
                    except (TvError, OSError):
                        continue
            return {"icons": self.icons}
        return tv_act(self.tv, body)


def build_module(cfg, bravia=None):
    tv = bravia or Bravia(cfg)

    def run(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except TvError as e:
                return text_result(str(e), error=True)
        return call

    def status(args):
        if tv.power() != "active":
            return "The TV is off (standby)."
        view = tv_overview(tv)
        showing = view["showing"]["title"] if view["showing"] else "an app or the home screen"
        picture = view["picture"]
        if picture is None:
            return f"The TV is on, showing {showing}; it's still starting up."
        extra = [f"picture mode {PICTURE_MODES.get(picture['mode'], picture['mode'])}", f"sound from the {OUTPUTS.get(view['output'], view['output']).lower()}"]
        if view["screen_off"]:
            extra.append("screen off with sound on")
        return f"The TV is on, showing {showing}; " + ", ".join(extra) + "."

    def settings_change(args):
        return tv_act(tv, args)["text"]

    def power(args):
        on = bool(args.get("on"))
        done = tv.set_power(on)
        if done:
            return "The TV is on." if on else "The TV is off."
        return ("I sent the power-on signal but the TV hasn't woken; turn on Remote start in the TV's network settings."
                if on else "I asked the TV to turn off but it still reports on.")

    def switch_input(args):
        options = tv.inputs()
        chosen = best_match(args.get("name", ""), options)
        if chosen is None:
            names = "; ".join(f"{o['title']}" + (f" ({o['label']})" if o.get("label") else "") for o in options)
            raise TvError(f"No input matches {args.get('name')!r}. Inputs: {names}.")
        tv.rpc("avContent", "setPlayContent", {"uri": chosen["uri"]})
        return f"Switched the TV to {chosen['title']}" + (f" ({chosen['label']})." if chosen.get("label") else ".")

    def open_app(args):
        options = tv.apps()
        chosen = best_match(args.get("name", ""), options)
        if chosen is None:
            raise TvError(f"No app matches {args.get('name')!r}. Apps: {', '.join(sorted({o['title'] for o in options}))}.")
        tv.rpc("appControl", "setActiveApp", {"uri": chosen["uri"]})
        return f"Opened {chosen['title']} on the TV."

    def remote(args):
        keys = [k for k in (args.get("keys") or []) if k.strip()][:MAX_PRESSES]
        if not keys:
            raise TvError("Name at least one key.")
        for key in keys:
            tv.press(key.strip())
            time.sleep(0.25)
        return f"Pressed {', '.join(keys)}."

    tv_status = tool("tv_status", "Is the TV on, and what input or app is it showing.", {"type": "object", "properties": {}})(run(status))
    tv_power = tool("tv_power", "Turn the TV on (on=true) or off to standby (on=false).",
                    {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]})(run(power))
    tv_input = tool("tv_input", "Switch the TV input by name or label, like 'HDMI 4', 'PC' or the soundbar's input.",
                    {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})(run(switch_input))
    tv_app = tool("tv_app", "Open an app on the TV by name, like Netflix, YouTube, Disney+, Prime Video, Spotify or Crunchyroll.",
                  {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})(run(open_app))
    tv_remote = tool("tv_remote", f"Press TV remote keys in order (up to {MAX_PRESSES}): VolumeUp, VolumeDown, Mute, Home, Return, Up, Down, "
                     "Left, Right, Confirm, Play, Pause, PlayPause, Stop, Forward, Rewind, ChannelUp, ChannelDown, Num0-Num9.",
                     {"type": "object", "properties": {"keys": {"type": "array", "items": {"type": "string"}}}, "required": ["keys"]})(run(remote))
    tv_settings = tool("tv_settings", "Change a TV setting: action 'picture_mode' with value standard, vivid, cinema, "
                       "movieFilmmakerExperienceProfessional, professional, calm, photo, imaxEnhanced, gameStandard, gameFps, gameRts or pcStandard; "
                       "'brightness' with value 0-50; 'light_sensor' with on true/false; 'output' with value audioSystem (soundbar) or speaker (TV "
                       "speakers); 'screen' with off true to turn the picture off while sound keeps playing, false to bring it back; 'close_apps'.",
                       {"type": "object", "properties": {"action": {"type": "string", "enum": ["picture_mode", "brightness", "light_sensor", "output", "screen", "close_apps"]},
                                                         "value": {"type": ["string", "integer"]}, "on": {"type": "boolean"}, "off": {"type": "boolean"}},
                        "required": ["action"]})(run(settings_change))
    specs = [ToolSpec(fn=fn, policy="auto", scope=Scope.household()) for fn in (tv_status, tv_power, tv_input, tv_app, tv_remote, tv_settings)]
    return ToolModule(server="tv", prompt=PROMPT, tools=specs, available=lambda c: load_settings(c) is not None)
