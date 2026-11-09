import json
import os
import platform
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from homehub.pc_agent import auth, ops

DEFAULT_PORT = 7781
MAX_BODY = 1 << 20


def default_config_path():
    if platform.system() == "Windows":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "HomeHub" / "agent.toml"
    return Path.home() / ".local/share/homehub/agent.toml"


class AgentConfig:
    def __init__(self, key, bind="0.0.0.0", port=DEFAULT_PORT, name=None, allow_fixes=True):
        if not key or len(key) < 32:
            raise ValueError("the agent key must be at least 32 characters; run the installer to create one")
        self.key, self.bind, self.port, self.name, self.allow_fixes = key, bind, int(port), name or platform.node(), bool(allow_fixes)

    @classmethod
    def load(cls, path=None):
        path = Path(path or default_config_path())
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        agent = data.get("agent", data)
        return cls(agent.get("key", ""), agent.get("bind", "0.0.0.0"), agent.get("port", DEFAULT_PORT), agent.get("name"), agent.get("allow_fixes", True))

    def write(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'[agent]\nname = "{self.name}"\nbind = "{self.bind}"\nport = {self.port}\nallow_fixes = {"true" if self.allow_fixes else "false"}\nkey = "{self.key}"\n', encoding="utf-8")
        return path


class AgentServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config, log=None):
        super().__init__((config.bind, config.port), AgentHandler)
        self.config = config
        self.guard = auth.ReplayGuard()
        self.log = log or (lambda line: None)
        self.lock = threading.Lock()
        self.calls = []


class AgentHandler(BaseHTTPRequestHandler):
    server_version = "HomeHubAgent/1"

    def log_message(self, fmt, *args):
        self.server.log(fmt % args)

    def respond(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self.respond(200, {"ok": True, "name": self.server.config.name, "uptime_seconds": round(time.time() - ops.STARTED, 1)})
        return self.respond(404, {"error": "unknown path"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self.respond(413, {"error": "body too large"})
        body = self.rfile.read(length) if length else b""
        ok, why = auth.verify(self.server.config.key, self.headers, "POST", self.path, body, self.server.guard)
        if not ok:
            return self.respond(401, {"error": why})
        if not self.path.startswith("/op/"):
            return self.respond(404, {"error": "unknown path"})
        name = self.path[4:]
        if name not in ops.OPERATIONS:
            return self.respond(404, {"error": f"unknown operation {name}"})
        if name in ops.FIXES and not self.server.config.allow_fixes:
            return self.respond(403, {"error": "fixes are turned off on this computer"})
        try:
            args = json.loads(body.decode() or "{}")
        except ValueError:
            return self.respond(400, {"error": "body must be JSON"})
        started = time.perf_counter()
        try:
            result = ops.run_operation(name, args)
            status, payload = 200, {"ok": True, "operation": name, "result": result}
        except ops.OpError as e:
            status, payload = 422, {"ok": False, "operation": name, "error": str(e)}
        except Exception as e:
            status, payload = 500, {"ok": False, "operation": name, "error": f"{type(e).__name__}: {e}"}
        payload["ms"] = round((time.perf_counter() - started) * 1000)
        with self.server.lock:
            self.server.calls.append((time.time(), name, status))
        self.server.log(f"{name} -> {status} in {payload['ms']} ms")
        return self.respond(status, payload)


def serve(config, log=None):
    server = AgentServer(config, log)
    thread = threading.Thread(target=server.serve_forever, name="pc-agent", daemon=True)
    thread.start()
    return server


def main(argv=None):
    import argparse
    import logging

    parser = argparse.ArgumentParser(prog="homehub-agent", description="Home Hub PC agent: answers the home hub's diagnostics and fixes on this computer")
    parser.add_argument("--config", help="agent.toml (default: ProgramData\\HomeHub\\agent.toml)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    config = AgentConfig.load(args.config)
    server = serve(config, logging.getLogger("pc_agent").info)
    logging.getLogger("pc_agent").info(f"{config.name}: listening on {config.bind}:{config.port}, fixes {'allowed' if config.allow_fixes else 'off'}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
    return 0
