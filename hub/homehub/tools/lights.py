import tomllib

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

PROMPT = ("Lights and switches: lights_status says which of the household's lights are on (and brightness); light_set turns one on or "
          "off by name and can set brightness 1-100 on dimmers and bulbs.")
DISCOVERY_SECONDS = 4


class LightError(RuntimeError):
    pass


def load_login(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        table = tomllib.load(f).get("kasa") or {}
    if table.get("username") and table.get("password"):
        return str(table["username"]), str(table["password"])
    return None


def allowed(cfg, alias):
    names = [n.strip().lower() for n in cfg.lights.devices if n.strip()]
    return alias.strip().lower() in names


def pick(cfg, aliases, name):
    wanted = name.strip().lower()
    mine = [a for a in aliases if allowed(cfg, a)]
    match = [a for a in mine if a.lower() == wanted] or [a for a in mine if wanted in a.lower()]
    if not match:
        raise LightError(f"No light called {name}. Lights: {', '.join(mine) or 'none found'}.")
    return match[0]


def describe(device):
    from kasa import Module

    state = "on" if device.is_on else "off"
    light = device.modules.get(Module.Light) if hasattr(device, "modules") else None
    if device.is_on and light is not None and getattr(light, "brightness", None) is not None and light.is_dimmable:
        state += f" at {light.brightness}%"
    return f"{device.alias}: {state}"


class Lights:
    def __init__(self, cfg):
        self.cfg = cfg

    async def devices(self):
        from kasa import Discover

        login = load_login(self.cfg)
        found = await Discover.discover(discovery_timeout=DISCOVERY_SECONDS, username=login[0] if login else None, password=login[1] if login else None)
        devices = []
        for device in found.values():
            try:
                await device.update()
            except Exception:
                continue
            if allowed(self.cfg, device.alias or ""):
                devices.append(device)
        return devices

    async def status(self):
        devices = await self.devices()
        try:
            return "; ".join(describe(d) for d in devices) if devices else "None of the listed lights answered; are they on the Wi-Fi?"
        finally:
            for d in devices:
                await d.disconnect()

    async def set(self, name, on, brightness=None):
        from kasa import Module

        devices = await self.devices()
        try:
            alias = pick(self.cfg, [d.alias or "" for d in devices], name)
            device = next(d for d in devices if d.alias == alias)
            if on:
                await device.turn_on()
                light = device.modules.get(Module.Light)
                if brightness is not None and light is not None and light.is_dimmable:
                    await light.set_brightness(max(1, min(100, int(brightness))))
            else:
                await device.turn_off()
            await device.update()
            return describe(device) + "."
        finally:
            for d in devices:
                await d.disconnect()


def build_module(cfg, client=None):
    lights = client or Lights(cfg)

    def run(make):
        async def call(args):
            try:
                return text_result(await make(args))
            except ImportError:
                return text_result("Light control needs the lights extra: pip install -e '.[lights]'.", error=True)
            except (LightError, OSError) as e:
                return text_result(str(e), error=True)
            except Exception as e:
                return text_result(f"The light didn't answer: {type(e).__name__}: {e}", error=True)
        return call

    lights_status = tool("lights_status", "Which of the household's lights and switches are on, with brightness.", {"type": "object", "properties": {}})(
        run(lambda a: lights.status()))
    light_set = tool("light_set", "Turn a light or switch on or off by name; brightness 1-100 for dimmers and bulbs.",
                     {"type": "object", "properties": {"name": {"type": "string"}, "on": {"type": "boolean"}, "brightness": {"type": "integer"}},
                      "required": ["name", "on"]})(run(lambda a: lights.set(a["name"], bool(a["on"]), a.get("brightness"))))
    return ToolModule(server="lights", prompt=PROMPT, available=lambda c: any(n.strip() for n in c.lights.devices), tools=[
        ToolSpec(fn=lights_status, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="device names"),
        ToolSpec(fn=light_set, policy="auto", scope=Scope.household()),
    ])
