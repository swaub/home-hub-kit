import asyncio
import hashlib
import http.client
import json
import ssl
import subprocess
import time
import tomllib
import urllib.parse

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result
from homehub.tools.hub import degrees
from homehub.tools.network import check_port
from homehub.tools.wake_on_lan import Waker

PROMPT = ("The Proxmox server is a separate mini PC that runs virtual machines and containers. proxmox_status says whether it's on "
          "and, when on, its uptime, CPU, memory, temperatures, storage, pending updates and each guest (virtual machine or container) "
          "with its state. proxmox_power turns the server on over the network, restarts it or shuts it down cleanly; everything on it "
          "stops with it. proxmox_guest starts, shuts down, restarts or force-stops one guest by name or number.")
KEY_FILE = "proxmox_key"
KNOWN_HOSTS = "proxmox_known_hosts"
WEB_PORT = 8006
API_TIMEOUT = 10
SSH_TIMEOUT = 15
TASK_SECONDS = 70
GUEST_ACTIONS = {"lxc": ("start", "shutdown", "reboot", "stop"), "qemu": ("start", "shutdown", "stop", "reset")}
ACTION_WORDS = {"start": "Started", "shutdown": "Shut down", "reboot": "Restarted", "stop": "Force-stopped", "reset": "Hard-reset"}
CONTROL_SCRIPT = """#!/bin/sh
case "$SSH_ORIGINAL_COMMAND" in
  status)
    echo "uptime=$(cut -d. -f1 /proc/uptime)"
    echo "load=$(cut -d' ' -f1 /proc/loadavg)"
    free -b | awk '/^Mem:/ {print "mem_used=" $3; print "mem_total=" $2}'
    echo "vms_running=$(qm list 2>/dev/null | awk 'NR>1 && $3=="running"' | wc -l)"
    echo "vms=$(qm list 2>/dev/null | awk 'NR>1' | wc -l)"
    echo "cts_running=$(pct list 2>/dev/null | awk 'NR>1 && $2=="running"' | wc -l)"
    echo "cts=$(pct list 2>/dev/null | awk 'NR>1' | wc -l)"
    ;;
  temps)
    for h in /sys/class/hwmon/hwmon*; do
      case "$(cat "$h/name" 2>/dev/null)" in
        coretemp|k10temp) echo "cpu_temp=$(cat "$h/temp1_input")" ;;
        nvme) echo "disk_temp=$(cat "$h/temp1_input")" ;;
      esac
    done
    ;;
  upgrade)
    systemctl is-active --quiet hub-upgrade && { echo running; exit 0; }
    systemd-run --unit=hub-upgrade --collect --setenv=DEBIAN_FRONTEND=noninteractive /bin/sh -c 'apt-get update -qq && apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade > /var/log/hub-upgrade.log 2>&1; echo "exit=$?" >> /var/log/hub-upgrade.log' >/dev/null && echo started
    ;;
  upgrade-status)
    systemctl is-active --quiet hub-upgrade && echo "running=1" || echo "running=0"
    echo "result=$(grep -o 'exit=[0-9]*' /var/log/hub-upgrade.log 2>/dev/null | tail -1 | cut -d= -f2)"
    newest=$(ls /boot/vmlinuz-* 2>/dev/null | sed 's#/boot/vmlinuz-##' | sort -V | tail -1)
    [ -n "$newest" ] && [ "$newest" != "$(uname -r)" ] && echo "reboot_needed=1" || echo "reboot_needed=0"
    ;;
  poweroff)
    systemd-run --on-active=2 --collect systemctl poweroff >/dev/null && echo ok
    ;;
  *)
    echo "only status, temps, upgrade, upgrade-status and poweroff are allowed"
    exit 1
    ;;
esac
"""


class ProxmoxError(RuntimeError):
    pass


def parse_status(text):
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and value.strip().isdigit():
            values[key.strip()] = int(value.strip())
        elif sep:
            try:
                values[key.strip()] = float(value.strip())
            except ValueError:
                continue
    return values


def took(seconds):
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days} day{'s' if days != 1 else ''} {hours} h"
    return f"{hours} h {rest // 60} min" if hours else f"{rest // 60} min"


def gb(n):
    return f"{n / 2**30:.1f}"


def describe(name, view):
    if not view["on"]:
        return f"The {name} is off."
    node = view["node"]
    parts = [f"The {name} is on, up {took(node['uptime'])}, CPU {node['cpu']}%, using {gb(node['mem_used'])} of {gb(node['mem_total'])} GB of memory"]
    if node.get("cpu_temp") is not None:
        parts.append(f"CPU at {degrees(node['cpu_temp'])}")
    lines = [", ".join(parts) + "."]
    if view["guests"]:
        lines.append("Guests: " + "; ".join(f"{g['name']} ({'virtual machine' if g['kind'] == 'qemu' else 'container'} {g['vmid']}) {g['status']}"
                                             for g in view["guests"]) + ".")
    else:
        lines.append("It has no virtual machines or containers yet.")
    if view["updates"]:
        lines.append(f"{view['updates']} update{'s' if view['updates'] != 1 else ''} waiting.")
    return " ".join(lines)


def load_secrets(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f).get("proxmox") or {}


class Proxmox:
    def __init__(self, cfg, waker=None, secrets=None):
        self.cfg = cfg
        self.waker = waker or Waker(check_ip=False)
        self.secrets = secrets
        self.node_name = None

    @property
    def name(self):
        return self.cfg.proxmox.name or "Proxmox server"

    def token(self):
        found = self.secrets if self.secrets is not None else load_secrets(self.cfg)
        if not found.get("token_id") or not found.get("token_secret") or not found.get("fingerprint"):
            raise ProxmoxError(f"The hub has no Proxmox login for the {self.name} yet (token_id, token_secret and fingerprint under [proxmox] in secrets.toml).")
        return found

    def reachable(self):
        return check_port(self.cfg.proxmox.host, WEB_PORT, timeout=2.0)[0]

    def api(self, method, path, fields=None):
        login = self.token()
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(self.cfg.proxmox.host, WEB_PORT, timeout=API_TIMEOUT, context=context)
        try:
            conn.connect()
            seen = hashlib.sha256(conn.sock.getpeercert(True)).hexdigest().upper()
            if ":".join(seen[i:i + 2] for i in range(0, 64, 2)) != login["fingerprint"].upper():
                raise ProxmoxError(f"The {self.name} answered with a different certificate than the one the hub trusts, so I didn't send anything.")
            body = urllib.parse.urlencode(fields or {}) if method != "GET" else None
            headers = {"Authorization": f"PVEAPIToken={login['token_id']}={login['token_secret']}"}
            if body is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            conn.request(method, f"/api2/json{path}", body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8", "replace")
        except OSError as e:
            raise ProxmoxError(f"The {self.name} didn't answer: {type(e).__name__}") from e
        finally:
            conn.close()
        if response.status == 403:
            raise ProxmoxError(f"The {self.name} doesn't allow the hub to do that.")
        if response.status != 200:
            raise ProxmoxError(f"The {self.name} refused that: {response.status} {raw[:160]}")
        return json.loads(raw).get("data")

    def node(self):
        if self.node_name is None:
            self.node_name = self.api("GET", "/nodes")[0]["node"]
        return self.node_name

    def ssh(self, command):
        key = self.cfg.data_dir() / KEY_FILE
        if not key.is_file():
            raise ProxmoxError(f"The hub has no key for the {self.name} yet, so it can't do that.")
        try:
            done = subprocess.run(["ssh", "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                   "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={self.cfg.data_dir() / KNOWN_HOSTS}",
                                   f"root@{self.cfg.proxmox.host}", command], capture_output=True, text=True, timeout=SSH_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ProxmoxError(f"The {self.name} didn't answer: {type(e).__name__}") from e
        if done.returncode != 0:
            raise ProxmoxError(f"The {self.name} refused that: {(done.stderr or done.stdout).strip()[:200]}")
        return done.stdout

    def overview(self, who=None):
        if not self.reachable():
            return {"on": False, "name": self.name}
        node = self.node()
        status = self.api("GET", f"/nodes/{node}/status")
        temps = {}
        try:
            temps = parse_status(self.ssh("temps"))
        except ProxmoxError:
            pass
        guests = []
        for kind in ("qemu", "lxc"):
            for g in self.api("GET", f"/nodes/{node}/{kind}"):
                guests.append({"kind": kind, "vmid": int(g["vmid"]), "name": g.get("name") or f"{kind} {g['vmid']}", "status": g.get("status", "unknown"),
                               "cpu": round(100 * float(g.get("cpu") or 0), 1), "cpus": g.get("cpus"), "mem": g.get("mem") or 0,
                               "maxmem": g.get("maxmem") or 0, "uptime": g.get("uptime") or 0, "actions": list(GUEST_ACTIONS[kind])})
        guests.sort(key=lambda g: g["vmid"])
        storage = [{"name": s["storage"], "used": s.get("used") or 0, "total": s.get("total") or 0, "content": s.get("content", "")}
                   for s in self.api("GET", f"/nodes/{node}/storage") if s.get("active")]
        updates = len(self.api("GET", f"/nodes/{node}/apt/update") or [])
        tasks = [{"type": t.get("type"), "id": t.get("id"), "user": t.get("user"), "start": t.get("starttime"), "status": t.get("status", "running")}
                 for t in self.api("GET", f"/nodes/{node}/tasks?limit=8")]
        history = [{"t": p["time"], "cpu": round(100 * float(p.get("cpu") or 0), 1), "mem": p.get("memused") or 0}
                   for p in self.api("GET", f"/nodes/{node}/rrddata?timeframe=hour") if "cpu" in p]
        memory = status["memory"]
        root = status.get("rootfs") or {}
        return {"on": True, "name": self.name, "node": {
            "uptime": status["uptime"], "cpu": round(100 * float(status.get("cpu") or 0), 1), "cpus": (status.get("cpuinfo") or {}).get("cpus"),
            "model": (status.get("cpuinfo") or {}).get("model"), "load": [float(x) for x in status.get("loadavg", [])],
            "mem_used": memory["used"], "mem_total": memory["total"], "root_used": root.get("used", 0), "root_total": root.get("total", 0),
            "cpu_temp": temps["cpu_temp"] / 1000 if "cpu_temp" in temps else None,
            "disk_temp": temps["disk_temp"] / 1000 if "disk_temp" in temps else None,
            "version": status.get("pveversion", ""), "kernel": (status.get("current-kernel") or {}).get("release", "")},
            "guests": guests, "storage": storage, "updates": updates, "tasks": tasks, "history": history}

    def wait(self, upid):
        node = self.node()
        deadline = time.monotonic() + TASK_SECONDS
        while time.monotonic() < deadline:
            task = self.api("GET", f"/nodes/{node}/tasks/{urllib.parse.quote(upid, safe='')}/status")
            if task.get("status") == "stopped":
                return task.get("exitstatus") or "OK"
            time.sleep(1)
        return None

    def power(self, action):
        up = self.reachable()
        if action == "on":
            if up:
                return f"The {self.name} is already on."
            if not self.cfg.proxmox.mac:
                raise ProxmoxError(f"The {self.name} has no MAC address in the config, so I can't wake it.")
            self.waker.send(self.cfg.proxmox.mac)
            return f"I sent the wake signal; the {self.name} takes about a minute to start."
        if action not in ("shutdown", "reboot"):
            raise ProxmoxError("The server can be turned on, restarted or shut down.")
        if not up:
            return f"The {self.name} is already off."
        self.api("POST", f"/nodes/{self.node()}/status", {"command": action})
        if action == "reboot":
            return f"The {self.name} is restarting; it's back in about a minute."
        return f"The {self.name} is shutting down; anything running on it stops cleanly first."

    def find_guest(self, which):
        wanted = str(which).strip().lower()
        node = self.node()
        for kind in ("qemu", "lxc"):
            for g in self.api("GET", f"/nodes/{node}/{kind}"):
                if wanted in (str(g["vmid"]), str(g.get("name", "")).lower()):
                    return kind, int(g["vmid"]), g.get("name") or str(g["vmid"])
        raise ProxmoxError(f"The {self.name} has no virtual machine or container called {which}.")

    def guest(self, which, action):
        kind, vmid, name = self.find_guest(which)
        if action not in GUEST_ACTIONS[kind]:
            words = ", ".join(GUEST_ACTIONS[kind])
            raise ProxmoxError(f"{name} is a {'virtual machine' if kind == 'qemu' else 'container'}; it can {words}.")
        upid = self.api("POST", f"/nodes/{self.node()}/{kind}/{vmid}/status/{action}", {})
        result = self.wait(upid)
        if result is None:
            return f"{name} is still working on it."
        if result != "OK":
            if action == "shutdown":
                return f"{name} didn't shut down by itself ({result}). Force stop turns it off right away."
            raise ProxmoxError(f"{name}: {result}")
        return f"{ACTION_WORDS[action]} {name}."

    def check_updates(self):
        upid = self.api("POST", f"/nodes/{self.node()}/apt/update", {})
        self.wait(upid)
        count = len(self.api("GET", f"/nodes/{self.node()}/apt/update") or [])
        return f"{count} update{'s' if count != 1 else ''} waiting." if count else "Everything is up to date."

    def install_updates(self):
        answer = self.ssh("upgrade").strip()
        if answer == "running":
            return "Updates are already installing."
        if answer != "started":
            raise ProxmoxError(f"The {self.name} didn't start installing: {answer[:120]}")
        return "Installing updates; this takes a few minutes. Anything running keeps running."

    def update_state(self):
        values = parse_status(self.ssh("upgrade-status"))
        return {"running": bool(values.get("running")), "result": values.get("result"), "reboot_needed": bool(values.get("reboot_needed"))}

    def act(self, body, who=None):
        action = str(body.get("action", ""))
        if action in ("on", "shutdown", "reboot"):
            return {"text": self.power(action)}
        if action == "guest":
            return {"text": self.guest(body.get("vmid"), str(body.get("do", "")))}
        if action == "check_updates":
            return {"text": self.check_updates()}
        if action == "install_updates":
            return {"text": self.install_updates()}
        if action == "update_state":
            return self.update_state()
        raise ProxmoxError("unknown Proxmox action")


def build_module(cfg, server=None):
    machine = server or Proxmox(cfg)

    def run(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except (ProxmoxError, OSError) as e:
                return text_result(str(e), error=True)
        return call

    proxmox_status = tool("proxmox_status", "Whether the Proxmox server is on, and if so its uptime, CPU, memory, temperature, pending updates "
                          "and every virtual machine and container with its state.", {"type": "object", "properties": {}})(
        run(lambda a: describe(machine.name, machine.overview())))
    proxmox_power = tool("proxmox_power", "Turn the Proxmox server on over the network (action 'on'), restart it ('reboot') or shut it down "
                         "cleanly ('shutdown').", {"type": "object", "properties": {"action": {"type": "string", "enum": ["on", "reboot", "shutdown"]}},
                                                   "required": ["action"]})(run(lambda a: machine.power(str(a.get("action")))))
    proxmox_guest = tool("proxmox_guest", "Start, shut down, restart or force-stop one virtual machine or container on the Proxmox server, by "
                         "name or number. Containers: start, shutdown, reboot, stop. Virtual machines: start, shutdown, stop, reset.",
                         {"type": "object", "properties": {"guest": {"type": "string"},
                                                           "action": {"type": "string", "enum": ["start", "shutdown", "reboot", "stop", "reset"]}},
                          "required": ["guest", "action"]})(run(lambda a: machine.guest(a.get("guest"), str(a.get("action")))))
    return ToolModule(server="proxmox", prompt=PROMPT, available=lambda c: bool(c.proxmox.host), tools=[
        ToolSpec(fn=proxmox_status, policy="auto", scope=Scope.household()),
        ToolSpec(fn=proxmox_power, policy="auto", scope=Scope.household()),
        ToolSpec(fn=proxmox_guest, policy="auto", scope=Scope.household())])
