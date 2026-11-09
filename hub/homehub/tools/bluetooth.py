import asyncio
import re
import shutil
import subprocess

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

PROMPT = ("Bluetooth on this hub: bt_devices lists paired speakers and headphones and whether they're connected; bt_connect and "
          "bt_disconnect switch one by name; bt_pair pairs a new one that is in pairing mode (it asks first).")
DEVICE_LINE = re.compile(r"^Device ((?:[0-9A-F]{2}:){5}[0-9A-F]{2}) (.+)$", re.M)
SCAN_SECONDS = 12


class BluetoothError(RuntimeError):
    pass


def parse_devices(text):
    return [{"mac": mac, "name": name.strip()} for mac, name in DEVICE_LINE.findall(text)]


def parse_info(text):
    info = {}
    for line in text.splitlines():
        key, _, value = line.strip().partition(": ")
        if key in ("Name", "Alias", "Paired", "Trusted", "Connected", "Icon"):
            info[key.lower()] = value.strip()
    return info


class Bluetooth:
    def __init__(self, exe=None):
        self.exe = exe or shutil.which("bluetoothctl")

    def run(self, *args, timeout=20):
        if not self.exe:
            raise BluetoothError("Bluetooth tools aren't installed on this computer.")
        out = subprocess.run([self.exe, *args], capture_output=True, text=True, timeout=timeout)
        return out.stdout + out.stderr

    def paired(self):
        found = parse_devices(self.run("devices", "Paired"))
        for d in found:
            d.update(parse_info(self.run("info", d["mac"])))
        return found

    def find(self, name, pool=None):
        wanted = name.strip().lower()
        options = pool if pool is not None else self.paired()
        match = [d for d in options if d["name"].lower() == wanted] or [d for d in options if wanted in d["name"].lower()]
        if not match:
            raise BluetoothError(f"No paired Bluetooth device called {name}. Paired: {', '.join(d['name'] for d in options) or 'none'}.")
        return match[0]

    def connect(self, name):
        device = self.find(name)
        out = self.run("connect", device["mac"], timeout=25)
        if "Connection successful" not in out and parse_info(self.run("info", device["mac"])).get("connected") != "yes":
            raise BluetoothError(f"{device['name']} didn't connect; make sure it's on and nearby.")
        return f"Connected to {device['name']}."

    def disconnect(self, name):
        device = self.find(name)
        self.run("disconnect", device["mac"])
        return f"Disconnected {device['name']}."

    def pair(self, name):
        self.run("--timeout", str(SCAN_SECONDS), "scan", "on", timeout=SCAN_SECONDS + 5)
        device = self.find(name, parse_devices(self.run("devices")))
        out = self.run("pair", device["mac"], timeout=30)
        if "Pairing successful" not in out and "AlreadyExists" not in out:
            raise BluetoothError(f"Pairing with {device['name']} didn't work; put it in pairing mode and try again.")
        self.run("trust", device["mac"])
        self.run("connect", device["mac"], timeout=25)
        return f"Paired and connected {device['name']}."


def build_module(bt=None):
    bluetooth = bt or Bluetooth()

    def run(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except (BluetoothError, OSError, subprocess.TimeoutExpired) as e:
                return text_result(str(e), error=True)
        return call

    def listing(args):
        found = bluetooth.paired()
        if not found:
            return "No Bluetooth devices are paired with the hub."
        return "; ".join(f"{d['name']} ({'connected' if d.get('connected') == 'yes' else 'not connected'})" for d in found)

    bt_devices = tool("bt_devices", "List Bluetooth devices paired with the hub and whether each is connected.", {"type": "object", "properties": {}})(run(listing))
    bt_connect = tool("bt_connect", "Connect a paired Bluetooth speaker or headphones by name.",
                      {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})(run(lambda a: bluetooth.connect(a["name"])))
    bt_disconnect = tool("bt_disconnect", "Disconnect a Bluetooth device by name.",
                         {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})(run(lambda a: bluetooth.disconnect(a["name"])))
    bt_pair = tool("bt_pair", "Pair a new Bluetooth speaker or headphones that is in pairing mode, by its name.",
                   {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})(run(lambda a: bluetooth.pair(a["name"])))
    return ToolModule(server="bluetooth", prompt=PROMPT, available=lambda c: c.features.bluetooth and shutil.which("bluetoothctl") is not None, tools=[
        ToolSpec(fn=bt_devices, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="Bluetooth device names"),
        ToolSpec(fn=bt_connect, policy="auto", scope=Scope.household()),
        ToolSpec(fn=bt_disconnect, policy="auto", scope=Scope.household()),
        ToolSpec(fn=bt_pair, policy="confirm", scope=Scope.household(), describe=lambda a: f"pair the hub with the Bluetooth device {a.get('name', '')}"),
    ])
