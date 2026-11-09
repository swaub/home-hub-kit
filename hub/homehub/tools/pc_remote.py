import asyncio
import concurrent.futures
import json
import socket
import tomllib
import urllib.error
import urllib.request

from claude_agent_sdk import tool

from homehub.pc_agent import auth
from homehub.pc_agent.server import DEFAULT_PORT
from homehub.tools import Scope, ToolModule, ToolSpec, text_result

PROBE_SECONDS = 2
RESOLVE_SECONDS = 3
FIX_WORDS = {"restore_point": "create a restore point", "clear_temp": "clear temporary files", "sfc": "run the Windows system file checker",
             "dism": "repair the Windows image with DISM", "restart_service": "restart a Windows service", "disable_startup": "turn off a startup program",
             "reboot": "restart the computer", "cancel_reboot": "cancel the restart"}
PROMPT = ("Family computers with the Home Hub agent can be checked and fixed from here: pc_check reads a summary (CPU, memory, disks, recent "
          "crashes, startup programs, pending updates), pc_screen looks at what's on their screen, pc_fix applies one allowlisted fix after asking "
          "(a restore point is made first for anything that changes the system). Explain findings in plain words, one or two sentences on what's "
          "wrong and then simple steps, and offer to do the fix. When hardware is failing, search the web for today's replacement prices.")


def load_secrets(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def agent_entry(cfg, device_id):
    entry = (load_secrets(cfg).get("pc_agents") or {}).get(device_id)
    if entry is None:
        return None
    if isinstance(entry, str):
        return {"key": entry, "port": DEFAULT_PORT, "address": None}
    return {"key": entry.get("key", ""), "port": int(entry.get("port") or DEFAULT_PORT), "address": entry.get("address")}


def agent_key(cfg, device_id):
    entry = agent_entry(cfg, device_id)
    return entry["key"] if entry else None


class PcAgentClient:
    def __init__(self, address, key, port=DEFAULT_PORT, timeout=30):
        self.address, self.key, self.port, self.timeout = address, key, int(port), timeout

    def url(self, path):
        host = f"[{self.address}]" if ":" in self.address and not self.address.startswith("[") else self.address
        return f"http://{host}:{self.port}{path}"

    def health(self):
        with urllib.request.urlopen(self.url("/health"), timeout=5) as response:
            return json.loads(response.read().decode())

    def probe(self, timeout=PROBE_SECONDS):
        try:
            with urllib.request.urlopen(self.url("/health"), timeout=timeout) as response:
                return json.loads(response.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None

    def call(self, operation, timeout=None, **args):
        path = f"/op/{operation}"
        body = json.dumps(args).encode()
        headers = {"Content-Type": "application/json", **auth.signed_headers(self.key, "POST", path, body)}
        request = urllib.request.Request(self.url(path), data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode())
            except ValueError:
                return {"ok": False, "error": f"HTTP {e.code}"}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return {"ok": False, "error": f"{getattr(e, 'reason', e)}"}


def resolve_pc(storage, cfg, name):
    device = storage.find_device((name or "").strip())
    if device is None or device["kind"] != "pc":
        return None, None, f"I don't know a computer called '{name}' in the household."
    if "pc_agent" not in device.get("capabilities", []):
        return device, None, f"{device['name']} doesn't have the Home Hub agent installed, so I can only wake it or ping it."
    entry = agent_entry(cfg, device["id"])
    hostname = device.get("hostname") or ""
    candidates = [a for a in ((entry or {}).get("address"), device.get("tailscale_name"), device.get("last_ip"), hostname,
                              f"{hostname}.local" if hostname and "." not in hostname else "") if a]
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        return device, None, f"I don't have an address for {device['name']} yet (its IP, hostname or Tailscale name)."
    if not entry or not entry["key"]:
        return device, None, f"The agent key for {device['name']} isn't in secrets.toml under [pc_agents]."
    for address in candidates:
        for ip in ipv4_addresses(address, entry["port"]):
            client = PcAgentClient(ip, entry["key"], entry["port"])
            if client.probe(PROBE_SECONDS):
                if ip != device.get("last_ip") and not ip.startswith("127."):
                    storage.set_device_runtime(device["id"], last_ip=ip)
                return device, client, None
    return device, PcAgentClient(candidates[0], entry["key"], entry["port"]), None


def ipv4_addresses(address, port, timeout=RESOLVE_SECONDS):
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(socket.getaddrinfo, address, port, socket.AF_INET, socket.SOCK_STREAM)
    pool.shutdown(wait=False)
    try:
        infos = future.result(timeout)
    except (OSError, concurrent.futures.TimeoutError):
        return []
    return list(dict.fromkeys(info[4][0] for info in infos))


def summarize(result):
    cpu, mem = result.get("cpu", {}), result.get("memory", {})
    load = f"at {cpu['load_percent']}% load" if cpu.get("load_percent") is not None else "load not reported"
    lines = [f"{result.get('os', 'Windows')}, last boot {result.get('last_boot', '')}",
             f"CPU {cpu.get('name', '')} {load}; memory {mem.get('used_percent')}% used of {mem.get('total_mb', 0) // 1024} GB"]
    for d in result.get("disks", []):
        lines.append(f"Drive {d['drive']} {d['free_gb']} GB free of {d['size_gb']} GB ({d['free_percent']}% free)")
    top = ", ".join(f"{p['name']} ({p['memory_mb']} MB)" for p in result.get("top_processes", [])[:5])
    if top:
        lines.append(f"Busiest programs: {top}")
    return "\n".join(lines)


def build_module(storage, cfg, notifier=None):
    async def target(args):
        device, client, problem = resolve_pc(storage, cfg, args.get("device"))
        if problem:
            return device, None, text_result(problem, error=True)
        return device, client, None

    async def perform(client, operation, **kwargs):
        return await asyncio.to_thread(client.call, operation, **kwargs)

    @tool("pc_check", "Read a household computer's health through its Home Hub agent: what is one of summary (CPU, memory, disks, busiest programs), "
          "disk (drive health), crashes (blue screens and power losses in the last days), startup (programs that start with Windows) or updates "
          "(pending Windows updates, slow). device is the computer's household name.",
          {"type": "object", "properties": {"device": {"type": "string"}, "what": {"type": "string"}}, "required": ["device"]})
    async def pc_check(args):
        device, client, problem = await target(args)
        if problem:
            return problem
        what = (args.get("what") or "summary").lower().strip()
        operation = {"summary": "diagnostics.summary", "disk": "disk.health", "disks": "disk.health", "crashes": "crashes.recent", "startup": "startup.list",
                     "updates": "updates.pending"}.get(what)
        if operation is None:
            return text_result("what must be summary, disk, crashes, startup or updates.", error=True)
        reply = await perform(client, operation, timeout=300 if operation == "updates.pending" else 60)
        if not reply.get("ok"):
            return text_result(f"Couldn't reach {device['name']}'s agent: {reply.get('error')}", error=True)
        result = reply["result"]
        return text_result(summarize(result) if operation == "diagnostics.summary" else json.dumps(result, indent=1)[:4000])

    @tool("pc_screen", "Look at what is on a household computer's screen right now (shows a notice on their screen while doing it).",
          {"type": "object", "properties": {"device": {"type": "string"}}, "required": ["device"]})
    async def pc_screen(args):
        device, client, problem = await target(args)
        if problem:
            return problem
        reply = await perform(client, "screen.capture", timeout=60)
        if not reply.get("ok"):
            return text_result(f"Couldn't see {device['name']}'s screen: {reply.get('error')}", error=True)
        return {"content": [{"type": "image", "data": reply["result"]["jpeg_base64"], "mimeType": "image/jpeg"}]}

    @tool("pc_fix", "Apply one fix on a household computer through its agent, after the assistant has asked. fix is one of: restore_point, clear_temp, sfc, "
          "dism, restart_service (name required), disable_startup (name required), reboot, cancel_reboot. Anything that changes the system gets a "
          "restore point first.", {"type": "object", "properties": {"device": {"type": "string"}, "fix": {"type": "string"}, "name": {"type": "string"}},
                                   "required": ["device", "fix"]})
    async def pc_fix(args):
        device, client, problem = await target(args)
        if problem:
            return problem
        fix = (args.get("fix") or "").lower().strip()
        if fix not in FIX_WORDS:
            return text_result(f"fix must be one of {', '.join(FIX_WORDS)}.", error=True)
        steps = []
        if fix in ("sfc", "dism", "restart_service", "disable_startup"):
            point = await perform(client, "fix.restore_point", timeout=600, description=f"Home Hub before {fix}")
            steps.append(f"restore point: {'created' if point.get('ok') and point['result'].get('created') else point.get('error') or 'not created'}")
        reply = await perform(client, f"fix.{fix}", timeout=3600 if fix in ("sfc", "dism") else 120, **({"name": args.get("name")} if args.get("name") else {}))
        if not reply.get("ok"):
            return text_result(f"The fix failed on {device['name']}: {reply.get('error')}", error=True)
        steps.append(f"{FIX_WORDS[fix]}: {json.dumps(reply['result'])[:600]}")
        text = f"Done on {device['name']}. " + "; ".join(steps)
        if notifier is not None and notifier.enabled:
            try:
                owner = storage.person(device["owner_id"]) if device.get("owner_id") else None
                notifier.send(f"{FIX_WORDS[fix].capitalize()} on {device['name']}" + (f" ({owner['name']}'s)" if owner else ""), title="Home Hub fixed something")
            except Exception:
                pass
        return text_result(text)

    pc = Scope.device("device")
    specs = [ToolSpec(fn=pc_check, policy="auto", scope=pc), ToolSpec(fn=pc_screen, policy="auto", scope=pc, untrusted_output=True, untrusted_source="that computer's screen"),
             ToolSpec(fn=pc_fix, policy="confirm", scope=pc, describe=lambda a: f"{FIX_WORDS.get((a.get('fix') or '').lower(), 'change something')} on {a.get('device', 'a computer')}")]
    return ToolModule(server="pc_remote", tools=specs, prompt=PROMPT)
