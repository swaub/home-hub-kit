import asyncio
import json
import socket
import time

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result
from homehub.tools.google_auth import write_private

DDP_PORT = 9302
DDP_VERSION = "00030010"
PAIRING_FILE = "ps5.json"
TIMEOUT = 1.0
WAKE_SECONDS = 30
REST_SECONDS = 30
TV_SETTLE = 4
PROMPT = ("The PS5: ps5_status says whether it's on or in rest mode and what's running; ps5_power turns it on from rest mode "
          "(on=true) or puts it into rest mode (on=false), which goes through the TV's HDMI link and so turns the TV off too. It can't "
          "start a specific game or sign in; after waking, the PS button on the controller signs in.")
NOT_PAIRED = "The PS5 isn't paired for power control yet: run 'homehub ps5 pair' on the hub with the PS5's Remote Play code."


class Ps5Error(RuntimeError):
    pass


def message(kind, fields=None):
    text = f"{kind} * HTTP/1.1\n" + "".join(f"{k}:{v}\n" for k, v in (fields or {}).items())
    return (text + f"device-discovery-protocol-version:{DDP_VERSION}\n").encode()


def parse_reply(raw):
    lines = raw.decode("utf-8", "replace").strip().splitlines()
    if not lines or not lines[0].startswith("HTTP/1.1"):
        return {}
    code = lines[0].split()[1] if len(lines[0].split()) > 1 else ""
    reply = {"code": code, "on": code == "200"}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        reply[key.strip()] = value.strip()
    return reply


def search(targets, wait=3.0):
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.3)
        deadline = time.monotonic() + wait
        sent_at = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() - sent_at > TIMEOUT:
                for target in targets:
                    sock.sendto(message("SRCH"), (target, DDP_PORT))
                sent_at = time.monotonic()
            try:
                raw, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            reply = parse_reply(raw)
            if reply.get("host-type") == "PS5":
                reply["ip"] = addr[0]
                found[reply.get("host-id", addr[0])] = reply
                if len(targets) == 1 and targets[0] != "255.255.255.255":
                    break
    return list(found.values())


def credential(regist_key):
    return str(int.from_bytes(bytes.fromhex(bytes.fromhex(regist_key).decode()), "big"))


class Ps5:
    def __init__(self, cfg, tv=None):
        self.cfg = cfg
        self.tv = tv

    def pairing(self):
        path = self.cfg.data_dir() / PAIRING_FILE
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def save_pairing(self, data):
        write_private(self.cfg.data_dir() / PAIRING_FILE, data)

    def find(self):
        paired = self.pairing() or {}
        if paired.get("ip"):
            hits = search([paired["ip"]], wait=2.0)
            if hits:
                return hits[0]
        hits = []
        for _ in range(3):
            hits = search(["255.255.255.255"])
            if hits:
                break
        if paired.get("host_id"):
            hits = [h for h in hits if h.get("host-id") == paired["host_id"]] or hits
        if not hits:
            raise Ps5Error("I can't find the PS5 on the network; it may be fully off or unplugged.")
        if paired and hits[0]["ip"] != paired.get("ip"):
            paired["ip"] = hits[0]["ip"]
            self.save_pairing(paired)
        return hits[0]

    def status(self):
        ps5 = self.find()
        name = ps5.get("host-name", "The PS5")
        if ps5["on"]:
            playing = ps5.get("running-app-name")
            return f"{name} is on" + (f", running {playing}." if playing else ".")
        return f"{name} is in rest mode."

    def wake(self):
        paired = self.pairing()
        if not paired:
            raise Ps5Error(NOT_PAIRED)
        ps5 = self.find()
        if ps5["on"]:
            return "The PS5 is already on."
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            wake = message("WAKEUP", {"user-credential": credential(paired["regist_key"]), "client-type": "vr", "auth-type": "R",
                                      "model": "w", "app-type": "r"})
            for _ in range(3):
                sock.sendto(wake, (ps5["ip"], DDP_PORT))
                time.sleep(0.2)
        deadline = time.monotonic() + WAKE_SECONDS
        while time.monotonic() < deadline:
            time.sleep(2)
            hits = search([ps5["ip"]], wait=1.5)
            if hits and hits[0]["on"]:
                return "The PS5 is on; press the PS button on your controller to sign in."
        return "I sent the wake signal; the PS5 is still starting up."

    def rest(self):
        if not self.pairing():
            raise Ps5Error(NOT_PAIRED)
        ps5 = self.find()
        if not ps5["on"]:
            return "The PS5 is already in rest mode."
        if self.tv is None:
            raise Ps5Error("The PS5 goes into rest mode through the TV's HDMI link, and the TV isn't set up for the hub.")
        if self.tv.power() != "active":
            self.tv.set_power(True)
            time.sleep(TV_SETTLE)
        if not self.tv.set_power(False):
            raise Ps5Error("The TV didn't turn off, so the PS5 stayed on.")
        deadline = time.monotonic() + REST_SECONDS
        while time.monotonic() < deadline:
            time.sleep(3)
            hits = search([ps5["ip"]], wait=1.5)
            if hits and not hits[0]["on"]:
                return "The PS5 is in rest mode. It follows the TV over HDMI, so the TV is off too."
        return "I turned the TV off, which puts the PS5 to rest over HDMI; it's still settling."


def pair(cfg, redirect_url, pin, host=""):
    from pyremoteplay.oauth import get_user_account
    from pyremoteplay.profile import add_regist_data, format_user_account
    from pyremoteplay.register import register

    account = get_user_account(redirect_url.strip())
    if not account:
        raise Ps5Error("PlayStation Network didn't accept that sign-in; start again and paste the whole address.")
    user = format_user_account(account)
    target = search([host] if host else ["255.255.255.255"])
    if not target:
        raise Ps5Error("I can't find the PS5 on the network; make sure it's on.")
    ps5 = target[0]
    regist = register(ps5["ip"], user.id, str(pin).strip())
    if not regist:
        raise Ps5Error("The PS5 refused the pairing; check the code on the TV hasn't expired and try again.")
    add_regist_data(user.data, {"host-id": ps5["host-id"], "host-type": "PS5", "host-name": ps5.get("host-name", "PS5")}, regist)
    data = {"host_id": ps5["host-id"], "host_name": ps5.get("host-name", "PS5"), "ip": ps5["ip"],
            "regist_key": user.data["hosts"][ps5["host-id"]]["data"]["RegistKey"], "profile": {"name": user.name, **user.data}}
    Ps5(cfg).save_pairing(data)
    return data


def build_module(cfg, console=None):
    from homehub.tools.tv import Bravia, load_settings

    ps5 = console or Ps5(cfg, Bravia(cfg) if load_settings(cfg) is not None else None)

    def run(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except (Ps5Error, OSError) as e:
                return text_result(str(e), error=True)
        return call

    ps5_status = tool("ps5_status", "Is the PS5 on or in rest mode.", {"type": "object", "properties": {}})(run(lambda a: ps5.status()))
    ps5_power = tool("ps5_power", "Turn the PS5 on from rest mode (on=true) or put it into rest mode (on=false; this turns the TV off too).",
                     {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]})(run(lambda a: ps5.wake() if a.get("on") else ps5.rest()))
    return ToolModule(server="ps5", prompt=PROMPT, available=lambda c: c.features.ps5, tools=[
        ToolSpec(fn=ps5_status, policy="auto", scope=Scope.household()), ToolSpec(fn=ps5_power, policy="auto", scope=Scope.household())])
