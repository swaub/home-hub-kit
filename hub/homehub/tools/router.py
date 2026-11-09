import asyncio
import json
import socket
import threading
import time
import tomllib
import urllib.parse

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

PROMPT = ("router_devices lists every device the home router sees right now: its name, address, MAC and whether it's wired or on "
          "Wi-Fi. Use it first to find a device by name (someone's phone or laptop), to tell whether someone's phone is home, and to get "
          "an address before ping_host or wake_computer. When you identify a household device in the list, save its address and MAC "
          "with remember_device.")
NOT_SET_UP = "The router isn't set up: add a [router] table with url and password to secrets.toml in the data folder."
CACHE_SECONDS = 30
KINDS = ("tplink_ex", "tplink")
BANDS = {"2g": "2.4 GHz", "5g": "5 GHz", "5g_2": "5 GHz", "6g": "6 GHz", "mlo": "multi-band", "mlo_2g": "2.4 GHz", "mlo_5g": "5 GHz",
         "mlo_6g": "6 GHz"}


def connection_words(kind):
    if kind == "wired":
        return "wired"
    group, _, band = kind.partition("_")
    if group == "host" and band.startswith("mlo"):
        return f"Wi-Fi {BANDS.get(band, band)}"
    label = {"host": "Wi-Fi", "guest": "guest Wi-Fi", "iot": "IoT Wi-Fi"}.get(group)
    return f"{label} {BANDS.get(band, band)}" if label else kind


def load_settings(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        table = tomllib.load(f).get("router") or {}
    if not table.get("url") or not table.get("password"):
        return None
    kind = table.get("kind", "tplink_ex")
    if kind not in KINDS:
        raise ValueError(f"router kind must be one of {', '.join(KINDS)}, not {kind!r}")
    return {"url": str(table["url"]).rstrip("/"), "password": str(table["password"]), "username": str(table.get("username", "")), "kind": kind}


def client_for(settings):
    if settings["kind"] == "tplink_ex":
        from tplinkrouterc6u.client.ex import TPLinkEXClient

        if settings["username"]:
            return TPLinkEXClient(settings["url"], settings["password"], username=settings["username"])
        return TPLinkEXClient(settings["url"], settings["password"])
    from tplinkrouterc6u import TplinkRouterProvider

    return TplinkRouterProvider.get_client(settings["url"], settings["password"])


def merge(devices, leases):
    rows = {}
    for lease in leases:
        mac = str(lease.macaddr).upper().replace("-", ":")
        rows[mac] = {"name": lease.hostname or "", "ip": str(lease.ipaddr), "mac": mac, "connection": "", "online": False}
    for device in devices:
        mac = str(device.macaddr).upper().replace("-", ":")
        row = rows.setdefault(mac, {"name": "", "ip": "", "mac": mac, "connection": "", "online": False})
        row["name"] = device.hostname or row["name"]
        row["ip"] = str(device.ipaddr) if device.ipaddr else row["ip"]
        kind = getattr(device.type, "value", str(device.type))
        row["connection"] = connection_words(kind)
        row["online"] = bool(getattr(device, "active", True))
    return sorted(rows.values(), key=lambda r: (not r["online"], (r["name"] or "~").lower(), r["ip"]))


def describe(rows):
    online = [r for r in rows if r["online"]]
    lines = [f"{len(online)} devices connected to the router now ({len(rows) - len(online)} more have an address but aren't connected)."]
    lines.append("name\tIP\tMAC\tconnection")
    for r in rows:
        lines.append(f"{r['name'] or '(no name)'}\t{r['ip']}\t{r['mac']}\t{r['connection'] if r['online'] else 'not connected'}")
    return "\n".join(lines)


ROUTER_LOCK = threading.RLock()


class RouterReader:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = ROUTER_LOCK
        self.cached_at = 0.0
        self.rows = None

    def read(self):
        settings = load_settings(self.cfg)
        if settings is None:
            return None
        with self.lock:
            if self.rows is not None and time.monotonic() - self.cached_at < CACHE_SECONDS:
                return self.rows
            client = client_for(settings)
            client.authorize()
            try:
                rows = merge(client.get_status().devices, client.get_ipv4_dhcp_leases())
            finally:
                client.logout()
            self.rows, self.cached_at = rows, time.monotonic()
            return rows


FORWARD_PREFIX = "Hub: "
FORWARD_OID = "DEV2_PORTMAPPING"
WAN_CONNECTION = "ipoe_0_0_d"


def unchunk(raw):
    head, _, body = raw.partition(b"\r\n\r\n")
    if b"chunked" not in head.lower():
        return body
    out = b""
    while body:
        size_line, _, body = body.partition(b"\r\n")
        size = int(size_line.split(b";")[0] or b"0", 16)
        if size == 0:
            break
        out += body[:size]
        body = body[size + 2:]
    return out


class PortForwards:
    def __init__(self, settings, hub_address=None):
        self.settings = settings
        self.host = urllib.parse.urlsplit(settings["url"]).hostname
        self.hub_address = hub_address or self.lan_address()
        self.lock = ROUTER_LOCK

    def lan_address(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((self.host, 80))
            return probe.getsockname()[0]

    def session(self):
        client = client_for(self.settings)
        client.authorize()
        return client

    def read(self, client):
        _, values = client.req_act([client.ActItem(client.ActItem.GL, FORWARD_OID)])
        rows = values[0] if values and isinstance(values[0], list) else []
        return [{"stack": r.get("stack", ""), "description": r.get("description", ""), "protocol": r.get("protocol", ""),
                 "external_port": int(r.get("externalPort") or 0), "internal_port": int(r.get("internalPort") or 0),
                 "client": r.get("internalClient", ""), "enabled": r.get("enable") == "1",
                 "ours": r.get("description", "").startswith(FORWARD_PREFIX)} for r in rows]

    def list(self):
        with self.lock:
            client = self.session()
            try:
                return self.read(client)
            finally:
                client.logout()

    def write(self, client, operation, data):
        body = json.dumps({"data": data, "operation": operation, "oid": FORWARD_OID, "isuseractive": True}, separators=(",", ":")) + "\r\n"
        sign, encrypted = client._prepare_data(body, False)
        payload = f"sign={sign}\r\ndata={encrypted}\r\n".encode()
        cookies = "; ".join(f"{k}={v}" for k, v in client.req.cookies.items())
        head = (f"POST /cgi_gdpr?9 HTTP/1.1\r\nHost: {self.host}\r\nReferer: http://{self.host}/\r\nTokenID: {client._token}\r\n"
                f"Cookie: {cookies}\r\nContent-Type: text/plain\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode()
        raw = b""
        with socket.create_connection((self.host, 80), timeout=15) as sock:
            sock.sendall(head + payload)
            while True:
                part = sock.recv(65536)
                if not part:
                    break
                raw += part
        try:
            reply = json.loads(client._encryption.aes_decrypt(unchunk(raw).decode().strip()))
        except Exception:
            raise RuntimeError("the router gave no readable answer") from None
        if reply.get("success") or reply.get("errorcode") == 0:
            return reply
        raise RuntimeError(f"the router refused it (error {reply.get('errorcode')})")

    def open(self, description, protocol, port):
        if not description.startswith(FORWARD_PREFIX) or protocol not in ("TCP", "UDP", "TCP or UDP") or not 1024 <= int(port) <= 65535:
            raise ValueError("only hub forwards on ports 1024 to 65535 can be opened from here")
        with self.lock:
            client = self.session()
            try:
                rows = self.read(client)
                if any(r["description"] == description for r in rows):
                    return False
                if any(r["external_port"] == int(port) and r["protocol"] in (protocol, "TCP or UDP") for r in rows):
                    raise RuntimeError(f"port {port} is already forwarded on the router")
                self.write(client, "ao", {"stack": "0,0,0,0,0,0", "pstack": "0,0,0,0,0,0", "enable": "1", "X_TP_ConnName": WAN_CONNECTION,
                                          "protocol": protocol, "description": description, "X_TP_AddrType": "0",
                                          "internalClient": self.hub_address, "externalPort": str(port), "externalPortEndRange": "0",
                                          "internalPort": str(port), "X_TP_InternalPortEndRange": "0"})
                return True
            finally:
                client.logout()

    def close(self, description):
        if not description.startswith(FORWARD_PREFIX):
            raise ValueError("only forwards the hub made can be removed from here")
        with self.lock:
            client = self.session()
            try:
                removed = 0
                for row in self.read(client):
                    if row["description"] == description and row["ours"]:
                        self.write(client, "do", {"stack": row["stack"], "pstack": "0,0,0,0,0,0"})
                        removed += 1
                return removed
            finally:
                client.logout()


def build_module(cfg):
    reader = RouterReader(cfg)

    @tool("router_devices", "List every device the home router knows: name, IP address, MAC, and wired or Wi-Fi band, connected ones "
          "first. Read-only; it never changes router settings.", {"type": "object", "properties": {}})
    async def router_devices(args):
        try:
            rows = await asyncio.to_thread(reader.read)
        except Exception as e:
            return text_result(f"Couldn't read the router: {type(e).__name__}: {e}", error=True)
        if rows is None:
            return text_result(NOT_SET_UP, error=True)
        return text_result(describe(rows))

    return ToolModule(server="router", prompt=PROMPT, tools=[
        ToolSpec(fn=router_devices, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="the router's device list"),
    ])
