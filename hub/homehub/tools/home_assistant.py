import asyncio
import json
import re
import tomllib
import urllib.error
import urllib.request

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

PROMPT = ("Home Assistant runs the smart home: set_light turns lights on or off or dims them in a room or by name, read_sensor gives the "
          "temperature or humidity in a room, home_devices lists what a room has, run_home_service does anything else Home Assistant can "
          "(after asking). Ask which room when it isn't clear.")
NOT_SET_UP = "Home Assistant isn't set up (home_assistant.url in the config and its token in secrets.toml)."
SENSOR_WORDS = {"temperature": ("temperature",), "temp": ("temperature",), "humidity": ("humidity",), "battery": ("battery",), "motion": ("motion", "occupancy"),
                "door": ("door", "opening"), "light level": ("illuminance",), "brightness": ("illuminance",)}


def load_token(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return ""
    with open(path, "rb") as f:
        return str((tomllib.load(f).get("home_assistant") or {}).get("token", ""))


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


def describe_state(state):
    attrs = state.get("attributes", {})
    name = attrs.get("friendly_name") or state["entity_id"]
    value = state.get("state")
    unit = attrs.get("unit_of_measurement")
    if state["entity_id"].startswith("light.") and value == "on" and attrs.get("brightness") is not None:
        return f"{name}: on at {round(int(attrs['brightness']) * 100 / 255)}%"
    return f"{name}: {value}" + (f" {unit}" if unit else "")


def to_fahrenheit(value, unit):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value, unit
    if (unit or "").strip().upper().endswith("C"):
        return round(number * 9 / 5 + 32, 1), "°F"
    return number, unit


class HomeAssistant:
    def __init__(self, url, token, timeout=15):
        self.url, self.token, self.timeout = (url or "").rstrip("/"), token or "", timeout

    @property
    def enabled(self):
        return bool(self.url and self.token)

    def request(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(f"{self.url}{path}", data=data, method=method,
                                         headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Home Assistant answered {e.code} for {path}: {e.read().decode('utf-8', 'replace')[:200]}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RuntimeError(f"Home Assistant at {self.url} is not reachable: {getattr(e, 'reason', e)}")
        try:
            return json.loads(body) if body else None
        except ValueError:
            return body

    def ok(self):
        return self.request("GET", "/api/")

    def states(self):
        return self.request("GET", "/api/states")

    def template(self, text):
        return self.request("POST", "/api/template", {"template": text})

    def area_entities(self, area):
        raw = self.template("{{ area_entities('" + area.replace("'", "") + "') | to_json }}")
        return json.loads(raw) if isinstance(raw, str) else (raw or [])

    def call(self, domain, service, data):
        return self.request("POST", f"/api/services/{domain}/{service}", data)


def match_entities(states, domain, area_entities, name=""):
    wanted = slug(name)
    pool = [s for s in states if s["entity_id"].startswith(domain + ".")]
    if area_entities:
        pool = [s for s in pool if s["entity_id"] in set(area_entities)] or pool
    if wanted:
        named = [s for s in pool if wanted in slug(s.get("attributes", {}).get("friendly_name", "")) or wanted in slug(s["entity_id"].split(".", 1)[1])]
        return named
    return pool


def build_module(cfg, client=None):
    client = client or HomeAssistant(cfg.home_assistant.url, load_token(cfg))

    def room_area(args):
        return (args.get("room") or "").strip()

    async def ready():
        if not client.enabled:
            return text_result(NOT_SET_UP, error=True)
        return None

    @tool("set_light", "Turn a light on or off, or dim it. room is where ('bedroom'; leave empty for every room), name picks one light when "
          "the room has several, state is on or off, brightness is 1 to 100 percent (optional).",
          {"type": "object", "properties": {"room": {"type": "string"}, "name": {"type": "string"}, "state": {"type": "string"}, "brightness": {"type": "integer"}}})
    async def set_light(args):
        problem = await ready()
        if problem:
            return problem
        state = (args.get("state") or "on").lower().strip()
        if state not in ("on", "off"):
            return text_result("state must be on or off.", error=True)
        area = room_area(args)
        try:
            states = await asyncio.to_thread(client.states)
            in_area = await asyncio.to_thread(client.area_entities, area) if area else []
            lights = match_entities(states, "light", in_area, args.get("name", ""))
            if not lights:
                return text_result(f"No light called '{args.get('name')}'" if args.get("name") else f"No lights found in {area or 'that room'}.", error=True)
            data = {"entity_id": [s["entity_id"] for s in lights]}
            if state == "on" and args.get("brightness") is not None:
                data["brightness_pct"] = max(1, min(100, int(args["brightness"])))
            await asyncio.to_thread(client.call, "light", f"turn_{state}", data)
        except RuntimeError as e:
            return text_result(str(e), error=True)
        names = ", ".join(s.get("attributes", {}).get("friendly_name") or s["entity_id"] for s in lights)
        return text_result(f"Turned {state} {names}" + (f" at {data['brightness_pct']}%" if "brightness_pct" in data else "") + ".")

    @tool("read_sensor", "Read a sensor in a room: what is temperature, humidity, battery, motion, door or light level.",
          {"type": "object", "properties": {"room": {"type": "string"}, "what": {"type": "string"}}, "required": ["what"]})
    async def read_sensor(args):
        problem = await ready()
        if problem:
            return problem
        kinds = SENSOR_WORDS.get((args.get("what") or "").lower().strip())
        if not kinds:
            return text_result(f"what must be one of {', '.join(SENSOR_WORDS)}.", error=True)
        area = room_area(args)
        try:
            states = await asyncio.to_thread(client.states)
            in_area = await asyncio.to_thread(client.area_entities, area) if area else []
        except RuntimeError as e:
            return text_result(str(e), error=True)
        found = [s for s in states if s["entity_id"].split(".")[0] in ("sensor", "binary_sensor") and (not in_area or s["entity_id"] in in_area)
                 and any(k in (s.get("attributes", {}).get("device_class") or "").lower() or k in s["entity_id"].lower() for k in kinds)]
        if not found:
            return text_result(f"No {args['what']} sensor in {area or 'that room'}.", error=True)
        lines = []
        for s in found[:5]:
            value, unit = s.get("state"), s.get("attributes", {}).get("unit_of_measurement")
            if kinds == ("temperature",):
                value, unit = to_fahrenheit(value, unit)
            lines.append(f"{s.get('attributes', {}).get('friendly_name') or s['entity_id']}: {value}" + (f" {unit}" if unit else ""))
        return text_result("\n".join(lines))

    @tool("home_devices", "List what Home Assistant has in a room (lights, switches, sensors, media players) with their current state.",
          {"type": "object", "properties": {"room": {"type": "string"}}})
    async def home_devices(args):
        problem = await ready()
        if problem:
            return problem
        area = room_area(args)
        try:
            states = await asyncio.to_thread(client.states)
            in_area = set(await asyncio.to_thread(client.area_entities, area)) if area else set()
        except RuntimeError as e:
            return text_result(str(e), error=True)
        shown = [s for s in states if (not in_area or s["entity_id"] in in_area) and s["entity_id"].split(".")[0] in
                 ("light", "switch", "sensor", "binary_sensor", "media_player", "climate", "fan", "cover", "camera")]
        if not shown:
            return text_result(f"Home Assistant has nothing in {area or 'that room'} yet.")
        return text_result("\n".join(describe_state(s) for s in shown[:40]))

    @tool("run_home_service", "Call any Home Assistant service, for example media_player.turn_off on media_player.bedroom_tv, or switch.toggle. domain and "
          "service are Home Assistant's names; entity_id is the target. The assistant asks the person first.",
          {"type": "object", "properties": {"domain": {"type": "string"}, "service": {"type": "string"}, "entity_id": {"type": "string"}, "data": {"type": "object"}},
           "required": ["domain", "service"]})
    async def run_home_service(args):
        problem = await ready()
        if problem:
            return problem
        data = dict(args.get("data") or {})
        if args.get("entity_id"):
            data["entity_id"] = args["entity_id"]
        try:
            result = await asyncio.to_thread(client.call, slug(args["domain"]), slug(args["service"]), data)
        except RuntimeError as e:
            return text_result(str(e), error=True)
        changed = ", ".join(s.get("entity_id", "") for s in (result or []) if isinstance(s, dict))
        return text_result(f"Called {args['domain']}.{args['service']}" + (f"; changed {changed}" if changed else "") + ".")

    specs = [ToolSpec(fn=set_light, policy="auto", scope=Scope.room()), ToolSpec(fn=read_sensor, policy="auto", scope=Scope.room()),
             ToolSpec(fn=home_devices, policy="auto", scope=Scope.room()),
             ToolSpec(fn=run_home_service, policy="confirm", scope=Scope.room(), describe=lambda a: f"run {a.get('domain')}.{a.get('service')} on {a.get('entity_id') or 'the home'}")]
    return ToolModule(server="home", tools=specs, available=lambda c: c.features.home_assistant, prompt=PROMPT)
