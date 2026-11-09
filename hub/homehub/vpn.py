import base64
import ipaddress
import json
import re
import subprocess
import time
import tomllib

WG_DIR = "/etc/wireguard"
CLIENTS = f"{WG_DIR}/clients"
IFACE = "wg0"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
PEERS_FILE = "vpn-peers.json"
TIMEOUT = 15


class VpnError(RuntimeError):
    pass


def sudo(*args, data=None):
    try:
        done = subprocess.run(["sudo", "-n", *args], input=data, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise VpnError(f"The hub couldn't run {args[0]}: {type(e).__name__}") from e
    if done.returncode != 0:
        raise VpnError(f"The hub couldn't run {args[0]}: {(done.stderr or done.stdout).strip()[:160]}")
    return done.stdout


def wg_tool(*args, data=None):
    done = subprocess.run(["wg", *args], input=data, capture_output=True, text=True, timeout=TIMEOUT)
    if done.returncode != 0:
        raise VpnError(f"wg {args[0]} failed: {done.stderr.strip()[:120]}")
    return done.stdout.strip()


def parse_dump(text):
    lines = [line.split("\t") for line in text.strip().splitlines()]
    if not lines:
        raise VpnError("The VPN isn't running on the hub.")
    head = lines[0]
    peers = []
    for row in lines[1:]:
        if len(row) < 8:
            continue
        allowed = row[3].split(",")[0]
        peers.append({"public": row[0], "endpoint": "" if row[2] == "(none)" else row[2], "ip": allowed.split("/")[0],
                      "handshake": int(row[4] or 0), "received": int(row[5] or 0), "sent": int(row[6] or 0)})
    return {"public": head[1], "port": int(head[2]), "peers": peers}


def without_peer(conf, public):
    blocks = re.split(r"(?m)^(?=\[)", conf)
    kept = [b for b in blocks if not (b.startswith("[Peer]") and re.search(rf"(?m)^PublicKey\s*=\s*{re.escape(public)}\s*$", b))]
    return "".join(kept)


def client_conf(private, address, server_public, psk, endpoint, subnet):
    return (f"[Interface]\nPrivateKey = {private}\nAddress = {address}/32\n\n[Peer]\nPublicKey = {server_public}\nPresharedKey = {psk}\n"
            f"Endpoint = {endpoint}\nAllowedIPs = {subnet}\nPersistentKeepalive = 25\n")


class Vpn:
    def __init__(self, cfg, on_change=None):
        self.cfg = cfg
        self.on_change = on_change

    def subnet(self):
        return ipaddress.ip_network(f"{self.cfg.remote.address}/24", strict=False)

    def endpoint(self, port):
        path = self.cfg.data_dir() / "secrets.toml"
        home = ""
        if path.is_file():
            with open(path, "rb") as f:
                home = str((tomllib.load(f).get("duckdns") or {}).get("home") or "")
        if not home:
            raise VpnError("New devices need the home DuckDNS name ([duckdns] home in secrets.toml) so they keep working if the home address changes.")
        return f"{home}.duckdns.org:{port}"

    def owners(self):
        path = self.cfg.data_dir() / PEERS_FILE
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_owners(self, owners):
        path = self.cfg.data_dir() / PEERS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(owners, indent=2), encoding="utf-8")

    def names(self):
        found = {}
        for path in sudo("find", CLIENTS, "-maxdepth", "1", "-name", "*.pub").split():
            name = path.rsplit("/", 1)[-1].removesuffix(".pub")
            if NAME.match(name):
                found[sudo("cat", path).strip()] = name
        return found

    def overview(self, who=None):
        state = parse_dump(sudo("wg", "show", IFACE, "dump"))
        names = self.names()
        peers = {p.partition("=")[0].strip(): p.partition("=")[2].strip() for p in self.cfg.remote.peers}
        peers.update(self.owners())
        now = time.time()
        devices = [{"name": names.get(p["public"]) or p["ip"], "ip": p["ip"], "managed": p["public"] in names, "person": peers.get(p["ip"], ""),
                    "last_seen": p["handshake"] or None, "online": bool(p["handshake"]) and now - p["handshake"] < 180,
                    "received": p["received"], "sent": p["sent"], "this_device": bool(who) and who.get("ip") == p["ip"]}
                   for p in state["peers"]]
        devices.sort(key=lambda d: ipaddress.ip_address(d["ip"]))
        return {"devices": devices, "port": state["port"], "can_manage": bool(who) and who.get("role") == "owner"}

    def require_owner(self, who):
        if not who or who.get("role") != "owner":
            raise VpnError("Only the household's owner can add or remove VPN devices.")

    def add(self, name, person, who):
        self.require_owner(who)
        name = str(name or "").strip().lower().replace(" ", "-")
        if not NAME.match(name):
            raise VpnError("Name the device with letters, numbers and dashes, like spare-phone.")
        if name in self.names().values():
            raise VpnError(f"There's already a device called {name}.")
        state = parse_dump(sudo("wg", "show", IFACE, "dump"))
        net = self.subnet()
        server = ipaddress.ip_address(self.cfg.remote.address)
        used = {ipaddress.ip_address(p["ip"]) for p in state["peers"]} | {server}
        free = next((ip for ip in net.hosts() if ip not in used), None)
        if free is None:
            raise VpnError("The VPN has no free addresses left.")
        endpoint = self.endpoint(state["port"])
        private = wg_tool("genkey")
        public = wg_tool("pubkey", data=private + "\n")
        psk = wg_tool("genpsk")
        conf = client_conf(private, free, state["public"], psk, endpoint, str(net))
        before = sudo("cat", f"{WG_DIR}/{IFACE}.conf")
        try:
            for suffix, content in (("key", private), ("pub", public), ("psk", psk), ("conf", conf)):
                sudo("tee", f"{CLIENTS}/{name}.{suffix}", data=content if content.endswith("\n") else content + "\n")
                sudo("chmod", "600", f"{CLIENTS}/{name}.{suffix}")
            sudo("tee", "-a", f"{WG_DIR}/{IFACE}.conf", data=f"\n[Peer]\nPublicKey = {public}\nPresharedKey = {psk}\nAllowedIPs = {free}/32\n")
            sudo("wg", "set", IFACE, "peer", public, "preshared-key", f"{CLIENTS}/{name}.psk", "allowed-ips", f"{free}/32")
        except VpnError:
            self.undo(name, public, before)
            raise
        owner = str(person or who.get("person") or "")
        owners = self.owners()
        owners[str(free)] = owner
        self.save_owners(owners)
        if self.on_change:
            self.on_change(str(free), owner)
        return {"text": f"Added {name} at {free}. Scan the code with the WireGuard app on that device.", "name": name, "ip": str(free),
                "conf": conf, "qr": self.qr(conf)}

    def undo(self, name, public, conf):
        for step in (lambda: sudo("tee", f"{WG_DIR}/{IFACE}.conf", data=conf), lambda: sudo("wg", "set", IFACE, "peer", public, "remove"),
                     *[lambda suffix=suffix: sudo("rm", "-f", f"{CLIENTS}/{name}.{suffix}") for suffix in ("conf", "key", "psk", "pub")]):
            try:
                step()
            except VpnError:
                continue

    def qr(self, conf):
        done = subprocess.run(["qrencode", "-t", "SVG", "-m", "2", "-o", "-"], input=conf.encode(), capture_output=True, timeout=TIMEOUT)
        if done.returncode != 0:
            raise VpnError("The hub couldn't draw the setup code (qrencode).")
        return "data:image/svg+xml;base64," + base64.b64encode(done.stdout).decode()

    def remove(self, name, who):
        self.require_owner(who)
        name = str(name or "").strip()
        names = {v: k for k, v in self.names().items()}
        if name not in names or not NAME.match(name):
            raise VpnError("The VPN has no device by that name.")
        public = names[name]
        state = parse_dump(sudo("wg", "show", IFACE, "dump"))
        peer = next((p for p in state["peers"] if p["public"] == public), None)
        if peer and who.get("ip") == peer["ip"]:
            raise VpnError("That's the device you're using right now; remove it from another device so you don't lock yourself out.")
        conf = sudo("cat", f"{WG_DIR}/{IFACE}.conf")
        sudo("cp", "-p", f"{WG_DIR}/{IFACE}.conf", f"{WG_DIR}/{IFACE}.conf.before-removing-{name}")
        sudo("tee", f"{WG_DIR}/{IFACE}.conf", data=without_peer(conf, public))
        sudo("wg", "set", IFACE, "peer", public, "remove")
        for suffix in ("conf", "key", "psk", "pub"):
            sudo("rm", "-f", f"{CLIENTS}/{name}.{suffix}")
        if peer:
            owners = self.owners()
            owners.pop(peer["ip"], None)
            self.save_owners(owners)
            if self.on_change:
                self.on_change(peer["ip"], None)
        return {"text": f"Removed {name}; it can't reach the hub anymore."}

    def act(self, body, who=None):
        action = str(body.get("action", ""))
        if action == "add":
            return self.add(body.get("name"), body.get("person"), who)
        if action == "remove":
            return self.remove(body.get("name"), who)
        raise VpnError("unknown VPN action")
