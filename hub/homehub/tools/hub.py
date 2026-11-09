import asyncio
import json
import subprocess
import time
from datetime import datetime

from claude_agent_sdk import tool

from homehub.sysinfo import SystemStats
from homehub.tools import Scope, ToolModule, ToolSpec, current_context, text_result

PROMPT = ("hub_status reports the hub mini PC itself, as on the System page of the private app: CPU, GPU, memory, disk, temperatures, "
          "network speed, uptime, and the services it runs (Immich photos, Paperless documents, Syncthing, Minecraft, the nightly "
          "backups, DuckDNS and the HTTPS certificate). hub_power restarts the assistant, reboots the whole hub or shuts it down, "
          "optionally turning itself back on at a set time; once it's off without one, only its power button turns it on, and the "
          "assistant, the private app and the VPN are off with it, so say that when they ask to shut it down. router_forwards lists "
          "the router's port forwards.")
UNITS = (("syncthing", "Syncthing file sync"), ("homehub-backup", "nightly assistant backup"),
         ("hub-services-backup", "nightly photos, documents, Minecraft and settings backup"), ("duckdns-update", "DuckDNS address update"),
         ("cert-renew", "HTTPS certificate renewal"))
CPU_WINDOW = 0.5


def size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def rate(n):
    return "–" if n is None else f"{n * 8 / 1e6:.1f} Mbit/s"


def took(seconds):
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    return (f"{days} d " if days else "") + f"{hours} h {rest // 60} min"


def degrees(c):
    return f"{c * 9 / 5 + 32:.0f}°F" if c is not None else "–"


def describe_stats(s):
    cpu, mem, disk = s["cpu"], s["memory"], s["disk"]
    lines = [f"Up {took(s['uptime_seconds'])} (since {datetime.fromtimestamp(s['booted_at']):%a %b %d %I:%M %p})",
             f"CPU {cpu['percent']}% of {cpu['cores']} threads, load {', '.join(str(v) for v in cpu['load'])}, {degrees(cpu['temp_c'])}",
             f"Memory {size(mem['used'])} of {size(mem['total'])}",
             f"Disk {size(disk['used'])} of {size(disk['total'])} used, {degrees(disk['temp_c'])}"]
    if s["gpu"]:
        gpu = s["gpu"]
        lines.append(f"GPU {gpu['percent']}% busy" + (f", {size(gpu['memory_used'])} of {size(gpu['memory_total'])} video memory"
                                                    if gpu["memory_used"] is not None else "") + f", {degrees(gpu['temp_c'])}")
    for net in s["network"]:
        signal = f", Wi-Fi signal {net['signal_dbm']} dBm" if net["signal_dbm"] is not None else ""
        role = " (internet)" if net["internet"] else ""
        lines.append(f"{net['name']}{role}: down {rate(net['rx_rate'])}, up {rate(net['tx_rate'])}{signal}")
    return "\n".join(lines)


def run(command, timeout=15):
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def containers():
    out = run(["docker", "ps", "-a", "--format", "{{json .}}"])
    if out is None:
        return ["Docker: not reachable"]
    groups = {}
    for line in out.splitlines():
        row = json.loads(line)
        labels = dict(part.split("=", 1) for part in row.get("Labels", "").split(",") if "=" in part)
        name = row["Names"]
        group = labels.get("com.docker.compose.project") or (f"Minecraft {name.removeprefix('mc-')}" if name.startswith("mc-") else name)
        groups.setdefault(group, []).append(row)
    lines = []
    for group, rows in sorted(groups.items()):
        up = [r for r in rows if r["State"] == "running"]
        sick = [r["Names"] for r in rows if "unhealthy" in r["Status"]]
        state = "running" if len(up) == len(rows) else f"{len(up)} of {len(rows)} parts running"
        lines.append(f"{group.capitalize() if group.islower() else group}: {state}" + (f", unhealthy: {', '.join(sick)}" if sick else ""))
    return lines


def units():
    lines = []
    for unit, label in UNITS:
        out = run(["systemctl", "--user", "show", f"{unit}.service", "-p", "ActiveState,LoadState"])
        if out is None:
            continue
        info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if info.get("LoadState") != "loaded":
            continue
        if unit == "syncthing":
            lines.append(f"{label}: {info.get('ActiveState')}")
            continue
        lines.append(f"{label}: {last_run(unit)}")
    return lines


def last_run(unit):
    out = run(["journalctl", "--user", "-u", f"{unit}.service", "--since", "-8d", "--no-pager", "-o", "short-iso"]) or ""
    for line in reversed(out.splitlines()):
        stamp, _, text = line.partition(" ")
        if "Finished " in text or "Failed with result" in text or "Failed to start" in text:
            try:
                when = datetime.fromisoformat(stamp).strftime("%a %b %d %I:%M %p")
            except ValueError:
                when = stamp
            return f"last run {when}, " + ("worked" if "Finished " in text else "failed")
    return "no run in the last week"


def describe_power(args):
    if args.get("what") == "off":
        return "shut down the hub" + (f" until {args['wake_at']}" if args.get("wake_at") else ", so it stays off until its power button is pressed")
    return "reboot the whole hub" if args.get("what") == "hub" else "restart the assistant"


def build_module(stats=None, power=None, forwards=None, apps=()):
    stats = stats or SystemStats()

    def status(args):
        stats.sample()
        time.sleep(CPU_WINDOW)
        lines = [describe_stats(stats.sample()), "Services:", *containers(), *units()]
        if apps:
            lines.append("Links in the app: " + ", ".join(f"{a['name']} {a['url']}" for a in apps))
        return "\n".join(lines)

    def restart(args):
        if power is None:
            return "Restarting isn't available here."
        ctx = current_context.get()
        what = args.get("what")
        action = {"hub": "hub", "off": "off"}.get(what, "assistant")
        wake_at = None
        if action == "off" and args.get("wake_at"):
            try:
                wake_at = int(datetime.strptime(str(args["wake_at"]).strip(), "%Y-%m-%d %H:%M").timestamp())
            except ValueError:
                return "The turn-on time has to look like 2026-11-05 07:00."
        ok, message = power(action, (ctx.person_id if ctx else None) or "assistant", wake_at)
        if not ok:
            return message
        return message + (". It's back in about a minute." if action == "hub" else ".")

    def forward_list(args):
        if forwards is None:
            return "The router isn't set up for the hub."
        rows = forwards.list()
        if not rows:
            return "The router has no port forwards."
        return "\n".join(f"{r['description'] or 'unnamed'}: {r['protocol']} {r['external_port']} to {r['client']}:{r['internal_port']}"
                         f"{'' if r['enabled'] else ' (off)'}{' (made by the hub)' if r['ours'] else ''}" for r in rows)

    def wrap(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except Exception as e:
                return text_result(f"{type(e).__name__}: {e}", error=True)
        return call

    hub_status = tool("hub_status", "The hub mini PC's health right now: CPU, GPU, memory, disk, temperatures, network speeds, uptime, "
                      "and whether Immich, Paperless, Syncthing, Minecraft servers and the nightly jobs are running and working.",
                      {"type": "object", "properties": {}})(wrap(status))
    hub_power = tool("hub_power", "Restart the assistant program (what 'assistant', about 15 seconds), reboot the whole hub mini PC "
                     "(what 'hub', about a minute; everything on it, Minecraft included, goes down meanwhile), or shut the hub down (what "
                     "'off'). With 'off', wake_at 'YYYY-MM-DD HH:MM' in local time makes it turn itself back on then (at least 3 minutes "
                     "and at most 30 days away); without it, it stays off until its power button is pressed.",
                     {"type": "object", "properties": {"what": {"type": "string", "enum": ["assistant", "hub", "off"]},
                                                       "wake_at": {"type": "string"}}, "required": ["what"]})(wrap(restart))
    router_forwards = tool("router_forwards", "List the home router's port forwards: name, protocol, outside port and where it points. "
                           "Read-only; Minecraft servers open and close their own through minecraft_public.",
                           {"type": "object", "properties": {}})(wrap(forward_list))
    return ToolModule(server="hub", prompt=PROMPT, tools=[
        ToolSpec(fn=hub_status, policy="auto", scope=Scope.household()),
        ToolSpec(fn=hub_power, policy="confirm", scope=Scope.household(), describe=describe_power, ask_each_time=True),
        ToolSpec(fn=router_forwards, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="the router"),
    ])


FRIENDLY = {"immich": "Immich photos", "paperless": "Paperless documents", "adguard": "AdGuard ad blocking",
            "isponsorblocktv": "YouTube ad skipper"}
BACKUPS = (("homehub-backup", "Assistant (database and settings)"), ("hub-services-backup", "Photos, documents, Minecraft and settings"))
SKIPPER = "isponsorblocktv"
PAIR_SECONDS = 60
NAMES_SECONDS = 600


class HubError(RuntimeError):
    pass


def compose_projects():
    out = run(["docker", "ps", "-a", "--format", "{{json .}}"])
    if out is None:
        raise HubError("Docker isn't answering on the hub.")
    projects = {}
    for line in out.splitlines():
        row = json.loads(line)
        labels = dict(part.split("=", 1) for part in row.get("Labels", "").split(",") if "=" in part)
        project = labels.get("com.docker.compose.project")
        if not project:
            continue
        entry = projects.setdefault(project, {"id": project, "name": FRIENDLY.get(project, project.capitalize()),
                                              "folder": labels.get("com.docker.compose.project.working_dir", ""), "parts": []})
        entry["parts"].append({"name": row["Names"], "state": row["State"], "status": row["Status"]})
    for entry in projects.values():
        up = [p for p in entry["parts"] if p["state"] == "running"]
        sick = [p["name"] for p in entry["parts"] if "unhealthy" in p["status"]]
        entry["state"] = "running" if len(up) == len(entry["parts"]) and not sick else "problem" if sick or up else "stopped"
        entry["detail"] = f"{len(up)} of {len(entry['parts'])} parts running" + (f", unhealthy: {', '.join(sick)}" if sick else "")
    return sorted(projects.values(), key=lambda p: p["name"].lower())


def timer_next(unit):
    out = run(["systemctl", "--user", "show", f"{unit}.timer", "-p", "NextElapseUSecRealtime"]) or ""
    value = out.strip().partition("=")[2]
    return value or None


def unit_state(unit):
    out = run(["systemctl", "--user", "show", f"{unit}.service", "-p", "ActiveState,LoadState,Result"]) or ""
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


class HubPanel:
    def __init__(self, cfg, adguard=None, names=None, bluetooth=None):
        self.cfg = cfg
        self.adguard = adguard
        self.bluetooth = bluetooth
        self.lookup = names or (lambda: {})
        self.known = ({}, 0.0)

    def names(self):
        if time.monotonic() - self.known[1] > NAMES_SECONDS:
            self.known = (self.lookup(), time.monotonic())
        return self.known[0]

    def skipper_folder(self):
        project = next((p for p in compose_projects() if p["id"] == SKIPPER), None)
        if project is None or not project["folder"]:
            raise HubError("The YouTube ad skipper isn't installed on the hub.")
        return project["folder"]

    def skipper(self):
        try:
            folder = self.skipper_folder()
        except HubError:
            return None
        try:
            config = json.loads(open(f"{folder}/data/config.json", encoding="utf-8").read())
        except (OSError, ValueError):
            return {"devices": [], "skips": []}
        return {"devices": [d.get("name") or "TV" for d in config.get("devices", [])], "skips": config.get("skip_categories", []),
                "mute_ads": bool(config.get("mute_ads")), "skip_ads": bool(config.get("skip_ads"))}

    def overview(self, who=None):
        backups = []
        for unit, label in BACKUPS:
            state = unit_state(unit)
            if state.get("LoadState") != "loaded":
                continue
            backups.append({"id": unit, "name": label, "last": last_run(unit), "next": timer_next(unit),
                            "running": state.get("ActiveState") in ("activating", "active")})
        sync = unit_state("syncthing")
        view = {"services": compose_projects(), "backups": backups, "skipper": self.skipper(),
                "syncthing": sync.get("ActiveState") if sync.get("LoadState") == "loaded" else None, "adguard": None, "bluetooth": None}
        if self.bluetooth is not None:
            try:
                view["bluetooth"] = [{"mac": d["mac"], "name": d.get("alias") or d["name"], "kind": d.get("icon", ""), "connected": d.get("connected") == "yes"}
                                     for d in self.bluetooth.paired()]
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                view["bluetooth"] = None
        if self.adguard is not None:
            try:
                view["adguard"] = self.adguard.overview(self.names())
            except RuntimeError as e:
                view["adguard"] = {"error": str(e)}
        return view

    def restart_service(self, project_id):
        project = next((p for p in compose_projects() if p["id"] == project_id), None)
        if project is None or not project["folder"]:
            raise HubError("The hub has no such service.")
        try:
            done = subprocess.run(["docker", "compose", "--project-directory", project["folder"], "-p", project["id"], "restart"],
                                  capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired as e:
            raise HubError(f"{project['name']} took too long to restart.") from e
        if done.returncode != 0:
            raise HubError(f"{project['name']} didn't restart: {(done.stderr or done.stdout).strip()[-200:]}")
        return f"Restarted {project['name']}."

    def backup_now(self, unit):
        label = dict(BACKUPS).get(unit)
        if label is None:
            raise HubError("The hub has no such backup.")
        if unit_state(unit).get("ActiveState") in ("activating", "active"):
            return f"The {label} backup is already running."
        if run(["systemctl", "--user", "start", "--no-block", f"{unit}.service"]) is None:
            raise HubError(f"The {label} backup didn't start.")
        return f"Backing up {label} now; it shows here when it's done."

    def pair_skipper(self, code, name):
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        name = str(name or "").strip()[:40]
        if len(digits) != 12:
            raise HubError("The TV code is 12 digits, from YouTube on the TV: Settings, Link with TV code.")
        if not name:
            raise HubError("Give the TV a name, like Bedroom TV.")
        folder = self.skipper_folder()
        try:
            done = subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{folder}/data:/app/data", "-v", f"{folder}/pair.py:/pair.py:ro",
                                   "--entrypoint", "python", "ghcr.io/dmunozv04/isponsorblocktv:latest", "/pair.py", digits, name],
                                  capture_output=True, text=True, timeout=PAIR_SECONDS)
        except subprocess.TimeoutExpired as e:
            raise HubError("Pairing took too long; get a fresh code on the TV and try again.") from e
        if done.returncode != 0:
            raise HubError("The TV didn't accept that code; get a fresh one from YouTube on the TV and try again.")
        if run(["docker", "restart", "isponsorblocktv"], timeout=60) is None:
            raise HubError(f"Paired {name}, but the ad skipper didn't restart; restart it from the services list.")
        return f"Paired {name}; the ad skipper now skips sponsors and ads there."

    def act(self, body, who=None):
        action = str(body.get("action", ""))
        if action == "restart":
            return {"text": self.restart_service(str(body.get("id", "")))}
        if action == "backup":
            return {"text": self.backup_now(str(body.get("id", "")))}
        if action == "pair":
            return {"text": self.pair_skipper(body.get("code"), body.get("name"))}
        if action in ("bt_connect", "bt_disconnect"):
            if self.bluetooth is None:
                raise HubError("Bluetooth isn't available on the hub.")
            device = next((d for d in self.bluetooth.paired() if d["mac"] == str(body.get("mac", ""))), None)
            if device is None:
                raise HubError("That Bluetooth device isn't paired with the hub.")
            return {"text": self.bluetooth.connect(device["name"]) if action == "bt_connect" else self.bluetooth.disconnect(device["name"])}
        if action in ("adguard_pause", "adguard_allow", "adguard_block", "adguard_remove"):
            if self.adguard is None:
                raise HubError("AdGuard isn't set up for the hub.")
            if action == "adguard_pause":
                return {"text": self.adguard.pause(body.get("minutes", 0))}
            if action == "adguard_remove":
                return {"text": self.adguard.remove_rule(body.get("domain"))}
            return {"text": self.adguard.set_rule(body.get("domain"), action == "adguard_allow")}
        raise HubError("unknown hub action")
