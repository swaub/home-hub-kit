import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

USER_AGENT = "homehub/0.1 (home server manager)"
TIMEOUT = 15
IMAGE = "itzg/minecraft-server"
JAVA_TAGS = (8, 11, 16, 17, 21, 25)
LOADERS = {"vanilla": "VANILLA", "paper": "PAPER", "fabric": "FABRIC", "forge": "FORGE", "neoforge": "NEOFORGE"}
MOD_FOLDERS = {"paper": "plugins", "fabric": "mods", "forge": "mods", "neoforge": "mods"}
MOD_KIND = {"paper": "plugin", "fabric": "mod", "forge": "mod", "neoforge": "mod"}
DIFFICULTIES = ("peaceful", "easy", "normal", "hard")
MODES = ("survival", "creative", "adventure")
FIRST_PORT = 25565
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
MOD_SLUG = re.compile(r"[A-Za-z0-9_.-]{1,64}(:[A-Za-z0-9]{8})?")
PLAYER = re.compile(r"[A-Za-z0-9_]{3,16}")
HOSTNAME = re.compile(r"(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
INIT_MEMORY = "512M"
JVM_XX_OPTS = ("-XX:+UseG1GC -XX:MaxGCPauseMillis=200 -XX:+ParallelRefProcEnabled -XX:+DisableExplicitGC "
               "-XX:G1PeriodicGCInterval=60000 -XX:MinHeapFreeRatio=10 -XX:MaxHeapFreeRatio=30")
DUCKDNS_UPDATE = "https://www.duckdns.org/update?domains={name}&token={token}&ip={ip}"
PROPERTIES = (("max_players", "max-players"), ("motd", "motd"), ("difficulty", "difficulty"), ("mode", "gamemode"),
              ("whitelist", "white-list"), ("whitelist", "enforce-whitelist"), ("online_mode", "online-mode"))
TICK_LINE = re.compile(r"Average time per tick: ([\d.]+)ms.*?P95: ([\d.]+)ms", re.S)
PLAYERS_LINE = re.compile(r"There are (\d+) of a max of (\d+) players online:?\s*(.*)")
CATALOG_SECONDS = 3600
DISK_SECONDS = 300
SIZE_SECONDS = 60
TEXT_LIMIT = 1_000_000
UPLOAD_LIMIT = 512 * 1024 * 1024
LOG_LINES = 300
QUIET_LOG = re.compile(r"Thread RCON Client .* (started|shutting down)$|ServerMain WARN Advanced terminal features")
SIZE_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4, "kB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3}
LABEL = "homehub.minecraft"
TRASH_DAYS = 7
TRASHED = re.compile(r".+-(\d{8}-\d{6})")
MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
PAPER = "https://fill.papermc.io/v3/projects/paper"
FABRIC = "https://meta.fabricmc.net/v2/versions/game"
FORGE = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
NEOFORGE = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
MODRINTH = "https://api.modrinth.com/v2"
PUBLIC_IP = "https://api.ipify.org?format=json"
PUBLIC_IP_SECONDS = 600
MOJANG_PROFILE = "https://api.mojang.com/users/profiles/minecraft/"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class McError(Exception):
    pass


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read())


def neoforge_game_version(version):
    if "-" in version:
        return None
    parts = version.split(".")
    if len(parts) < 3 or not all(p.isdigit() for p in parts[:3]):
        return None
    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    if major >= 26:
        return f"{major}.{minor}" + (f".{patch}" if patch else "")
    return f"1.{major}" + (f".{minor}" if minor else "")


def java_tag(required):
    for tag in JAVA_TAGS:
        if tag >= required:
            return tag
    return JAVA_TAGS[-1]


def size_bytes(text):
    found = re.fullmatch(r"\s*([\d.]+)\s*([A-Za-z]+)\s*", text or "")
    if not found or found.group(2) not in SIZE_UNITS:
        return None
    return int(float(found.group(1)) * SIZE_UNITS[found.group(2)])


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


def resolve(host):
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET)})
    except OSError:
        return []


def duckdns_token(secrets_path):
    import tomllib

    try:
        with open(secrets_path, "rb") as f:
            return str((tomllib.load(f).get("duckdns") or {}).get("token", "")).strip()
    except OSError:
        return ""


def mojang_profile(name):
    try:
        found = fetch_json(MOJANG_PROFILE + urllib.parse.quote(name))
    except urllib.error.HTTPError as e:
        if e.code in (204, 404):
            raise McError(f"There's no Minecraft account named {name}") from None
        raise
    raw = found["id"]
    return {"uuid": f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}", "name": found["name"]}


def local_timezone():
    zone = Path("/etc/localtime")
    if zone.is_symlink() and "zoneinfo/" in str(zone.readlink()):
        return str(zone.readlink()).split("zoneinfo/", 1)[1]
    return os.environ.get("TZ") or "UTC"


def read_properties(text):
    found = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith(("#", "!")):
            name, value = line.split("=", 1)
            found[name.strip()] = value.strip()
    return found


def property_text(meta, key):
    value = meta.get(key, True) if key in ("whitelist", "online_mode") else meta[key]
    return str(value).lower() if isinstance(value, bool) else str(value)


def slug_for(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:31]
    return slug if SLUG.fullmatch(slug or "") else None


class Catalog:
    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    def cached(self, key, make):
        with self.lock:
            hit = self.cache.get(key)
            if hit and time.monotonic() - hit[0] < CATALOG_SECONDS:
                return hit[1]
        value = make()
        with self.lock:
            self.cache[key] = (time.monotonic(), value)
        return value

    def manifest(self):
        return self.cached("manifest", lambda: [v for v in fetch_json(MANIFEST)["versions"] if v["type"] == "release"])

    def releases(self):
        return [v["id"] for v in self.manifest()]

    def versions(self, loader):
        if loader not in LOADERS:
            raise McError(f"unknown loader {loader!r}")
        releases = self.releases()
        if loader == "vanilla":
            return releases
        if loader == "paper":
            found = {v for group in fetch_json(PAPER)["versions"].values() for v in group if "-" not in v}
        elif loader == "fabric":
            found = {v["version"] for v in fetch_json(FABRIC) if v.get("stable")}
        elif loader == "forge":
            found = {key.rsplit("-", 1)[0] for key in fetch_json(FORGE)["promos"]}
        else:
            found = {neoforge_game_version(v) for v in fetch_json(NEOFORGE)["versions"]} - {None}
        return [v for v in releases if v in found]

    def loader_versions(self, loader):
        return self.cached(f"versions:{loader}", lambda: self.versions(loader))

    def java_for(self, version):
        entry = next((v for v in self.manifest() if v["id"] == version), None)
        if entry is None:
            raise McError(f"Minecraft {version} isn't a release")
        info = self.cached(f"java:{version}", lambda: fetch_json(entry["url"]).get("javaVersion") or {})
        return java_tag(int(info.get("majorVersion") or 8))

    def pin(self, entry, loader, version):
        slug = entry.split(":", 1)[0]
        params = urllib.parse.urlencode({"loaders": json.dumps([loader]), "game_versions": json.dumps([version])})
        found = fetch_json(f"{MODRINTH}/project/{urllib.parse.quote(slug)}/version?{params}")
        if not found:
            raise McError(f"{slug} has no version for {loader} {version}")
        if any(v.get("version_type") == "release" for v in found):
            return slug
        best = next((v for v in found if v.get("version_type") == "beta"), found[0])
        return f"{slug}:{best['id']}"

    def mods(self, loader, version, query="", offset=0):
        if loader not in MOD_FOLDERS:
            return {"total": 0, "hits": []}
        facets = [[f"categories:{loader}"], [f"versions:{version}"], [f"project_type:{MOD_KIND[loader]}"],
                  ["server_side:required", "server_side:optional"]]
        params = {"query": query.strip()[:80], "facets": json.dumps(facets), "limit": 20, "offset": max(0, int(offset)),
                  "index": "relevance" if query.strip() else "downloads"}
        data = fetch_json(f"{MODRINTH}/search?{urllib.parse.urlencode(params)}")
        hits = [{"slug": h["slug"], "title": h["title"], "description": h.get("description", "")[:160], "downloads": h.get("downloads", 0),
                 "icon": h.get("icon_url") or "", "client": h.get("client_side"), "server": h.get("server_side")} for h in data.get("hits", [])]
        return {"total": data.get("total_hits", 0), "hits": hits}


class Minecraft:
    def __init__(self, root, catalog=None, docker=("docker",), vpn_address="10.77.0.1", cpus=6, max_memory_gb=16, emit=None, forwards=None,
                 public_host="", secrets_path=None):
        self.root = Path(root)
        self.catalog = catalog or Catalog()
        self.docker_cmd = tuple(docker)
        self.vpn_address = vpn_address
        self.cpus = cpus
        self.max_memory_gb = max_memory_gb
        self.emit = emit or (lambda name, **payload: None)
        self.forwards = forwards
        self.public_host = public_host.strip()
        self.secrets_path = secrets_path
        self.lock = threading.Lock()
        self.busy = {}
        self.sizes = {}
        self.watcher = None
        self.stopping = threading.Event()
        self.public_ip_cache = (0.0, "")

    def docker(self, *args, timeout=60):
        try:
            done = subprocess.run([*self.docker_cmd, *args], capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise McError("Docker isn't installed on the hub") from None
        except subprocess.TimeoutExpired:
            raise McError(f"docker {args[0]} took too long") from None
        if done.returncode != 0:
            raise McError((done.stderr or done.stdout).strip().splitlines()[-1] if (done.stderr or done.stdout).strip() else f"docker {args[0]} failed")
        return done.stdout

    def folder(self, slug):
        if not SLUG.fullmatch(slug or ""):
            raise McError("unknown server")
        path = self.root / slug
        if not (path / "server.json").is_file():
            raise McError("unknown server")
        return path

    def meta(self, slug):
        return json.loads((self.folder(slug) / "server.json").read_text(encoding="utf-8"))

    def save(self, meta):
        path = self.root / meta["slug"] / "server.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        tmp.replace(path)

    def all_meta(self):
        if not self.root.is_dir():
            return []
        found = []
        for entry in sorted(self.root.iterdir()):
            if (entry / "server.json").is_file() and SLUG.fullmatch(entry.name):
                found.append(json.loads((entry / "server.json").read_text(encoding="utf-8")))
        return found

    def containers(self):
        out = self.docker("ps", "-a", "--filter", f"label={LABEL}", "--format", "{{json .}}", timeout=20)
        rows = {}
        for line in out.splitlines():
            row = json.loads(line)
            rows[row["Names"].removeprefix("mc-")] = row
        return rows

    @staticmethod
    def state_of(row):
        if row is None:
            return "stopped"
        status = row.get("Status", "")
        if row.get("State") == "restarting":
            return "problem"
        if row.get("State") != "running":
            return "stopped"
        if "health: starting" in status:
            return "starting"
        if "unhealthy" in status:
            return "problem"
        return "online"

    def size(self, slug, fresh=False):
        hit = self.sizes.get(slug)
        if hit and not fresh and time.monotonic() - hit[0] < SIZE_SECONDS:
            return hit[1]
        total = 0
        for base, _, files in os.walk(self.root / slug / "data"):
            for name in files:
                try:
                    total += os.lstat(os.path.join(base, name)).st_size
                except OSError:
                    pass
        self.sizes[slug] = (time.monotonic(), total)
        return total

    def summary(self, meta, row):
        return {"slug": meta["slug"], "name": meta["name"], "loader": meta["loader"], "version": meta["version"], "port": meta["port"],
                "public": meta.get("public", False), "state": self.busy.get(meta["slug"]) or self.state_of(row), "note": meta.get("note", ""),
                "memory_gb": meta["memory_gb"], "disk_gb": meta["disk_gb"], "max_players": meta["max_players"],
                "whitelist_on": meta.get("whitelist", True), "online_mode": meta.get("online_mode", True)}

    def empty_trash(self, now=None):
        trash = self.root / ".trash"
        if not trash.is_dir():
            return []
        now = now or datetime.now()
        removed = []
        for entry in trash.iterdir():
            found = TRASHED.fullmatch(entry.name)
            if not found or not entry.is_dir() or entry.is_symlink():
                continue
            if (now - datetime.strptime(found.group(1), "%Y%m%d-%H%M%S")).days >= TRASH_DAYS:
                shutil.rmtree(entry)
                removed.append(entry.name)
        return removed

    def drop_unused_image(self, image):
        if image and not self.docker("ps", "-a", "--filter", f"ancestor={image}", "--format", "{{.ID}}", timeout=20).strip():
            try:
                self.docker("image", "rm", image, timeout=120)
            except McError:
                pass

    def list(self):
        self.empty_trash()
        rows = self.containers()
        return [self.summary(m, rows.get(m["slug"])) for m in self.all_meta()]

    @staticmethod
    def forward_name(meta):
        return f"Hub: MC {meta['port']}"

    def check_port(self, meta, port):
        other = next((m["name"] for m in self.all_meta() if m["slug"] != meta["slug"] and m["port"] == port), None)
        if other:
            raise McError(f"{other} already uses port {port}")
        if meta.get("public"):
            if self.forwards is None:
                raise McError("The router isn't set up for the hub, so an open server can't move ports")
            if any(f["external_port"] == port and f["description"] != self.forward_name(meta) for f in self.forwards.list()):
                raise McError(f"The router already forwards port {port} to something else")
        if port_in_use(port):
            raise McError(f"Another program on the hub is using port {port}")

    def free_port(self, keep=None):
        mine = self.forward_name(keep) if keep else None
        used = {m["port"] for m in self.all_meta() if keep is None or m["slug"] != keep["slug"]}
        if self.forwards is not None:
            try:
                used |= {f["external_port"] for f in self.forwards.list() if f["description"] != mine}
            except Exception as e:
                self.emit("service.warning", message=f"couldn't read router forwards for a free port: {e}")
        for port in range(FIRST_PORT, FIRST_PORT + 200):
            if port not in used and not port_in_use(port):
                return port
        raise McError("No free game port between 25565 and 25764")

    def validate(self, spec, creating):
        clean = {}
        if creating:
            name = " ".join(str(spec.get("name") or "").split())[:40]
            slug = slug_for(name)
            if not name or slug is None:
                raise McError("Give the server a name with letters or numbers")
            if (self.root / slug).exists():
                raise McError("A server with that name already exists")
            loader = spec.get("loader")
            if loader not in LOADERS:
                raise McError("Pick a server type")
            version = str(spec.get("version") or "")
            if version not in self.catalog.loader_versions(loader):
                raise McError(f"{loader} doesn't have Minecraft {version}")
            if spec.get("eula") is not True:
                raise McError("The Minecraft EULA has to be accepted to run a server")
            clean.update(name=name, slug=slug, loader=loader, version=version)
        for key, low, high in (("memory_gb", 1, self.max_memory_gb), ("disk_gb", 1, 400), ("max_players", 1, 1000)):
            if key in spec:
                value = int(spec[key])
                if not low <= value <= high:
                    raise McError(f"{key.replace('_', ' ')} must be between {low} and {high}")
                clean[key] = value
        if "port" in spec:
            port = int(spec["port"])
            if not 1024 <= port <= 65535:
                raise McError("The port must be between 1024 and 65535")
            clean["port"] = port
        if "seed" in spec:
            seed = str(spec["seed"] or "").strip()
            if not re.fullmatch(r"-?[A-Za-z0-9_]{0,32}", seed):
                raise McError("A seed is up to 32 letters or digits")
            clean["seed"] = seed
        if "host" in spec:
            host = str(spec["host"] or "").strip().lower().rstrip(".")
            if host and not HOSTNAME.fullmatch(host):
                raise McError("That address isn't a valid domain name, like play.example.com")
            clean["host"] = host
        if "online_mode" in spec:
            if not isinstance(spec["online_mode"], bool):
                raise McError("online mode is on or off")
            clean["online_mode"] = spec["online_mode"]
        if "whitelist" in spec:
            if not isinstance(spec["whitelist"], bool):
                raise McError("whitelist is on or off")
            clean["whitelist"] = spec["whitelist"]
        if "motd" in spec:
            clean["motd"] = " ".join(str(spec["motd"]).split())[:59]
        if "difficulty" in spec:
            if spec["difficulty"] not in DIFFICULTIES:
                raise McError("unknown difficulty")
            clean["difficulty"] = spec["difficulty"]
        if "mode" in spec:
            if spec["mode"] not in MODES:
                raise McError("unknown game mode")
            clean["mode"] = spec["mode"]
        if "mods" in spec:
            mods = [str(m) for m in spec["mods"] or []]
            if not all(MOD_SLUG.fullmatch(m) for m in mods) or len(mods) > 200:
                raise McError("bad mod list")
            clean["mods"] = sorted(set(mods))
        if "players" in spec:
            players = [str(p) for p in spec["players"] or []]
            if not all(PLAYER.fullmatch(p) for p in players) or len(players) > 50:
                raise McError("Player names are 3 to 16 letters, numbers or _")
            clean["players"] = players
        return clean

    def create(self, spec, owner):
        clean = self.validate(spec, creating=True)
        with self.lock:
            port = self.free_port()
            meta = {"name": clean["name"], "slug": clean["slug"], "loader": clean["loader"], "version": clean["version"],
                    "memory_gb": clean.get("memory_gb", 4), "disk_gb": clean.get("disk_gb", 20), "max_players": clean.get("max_players", 10),
                    "motd": clean.get("motd") or clean["name"], "difficulty": clean.get("difficulty", "normal"), "mode": clean.get("mode", "survival"),
                    "mods": sorted({self.catalog.pin(m, clean["loader"], clean["version"]) for m in clean.get("mods", [])})
                    if clean["loader"] in MOD_FOLDERS else [], "port": port, "public": False,
                    "players": clean.get("players", []), "created": datetime.now().isoformat(timespec="seconds"), "owner": owner, "note": "",
                    "seed": clean.get("seed", ""), "online_mode": clean.get("online_mode", True)}
            (self.root / meta["slug"] / "data").mkdir(parents=True)
            self.save(meta)
            self.busy[meta["slug"]] = "installing"
        self.emit("minecraft.changed", slug=meta["slug"], action="created", by=owner)
        threading.Thread(target=self.install, args=(meta["slug"],), name=f"mc-{meta['slug']}", daemon=True).start()
        return self.summary(meta, None)

    def install(self, slug):
        try:
            meta = self.meta(slug)
            image = f"{IMAGE}:java{self.catalog.java_for(meta['version'])}"
            meta["note"] = "Downloading the server software"
            self.save(meta)
            self.docker("pull", "-q", image, timeout=900)
            meta["note"] = ""
            self.save(meta)
            self.run_container(meta, first=True)
        except Exception as e:
            meta = self.meta(slug)
            meta["note"] = f"Setup failed: {e}"
            self.save(meta)
        finally:
            self.busy.pop(slug, None)

    def env(self, meta, first):
        uid, gid = os.getuid(), os.getgid()
        env = {"EULA": "TRUE", "TYPE": LOADERS[meta["loader"]], "VERSION": meta["version"], "INIT_MEMORY": INIT_MEMORY,
               "MAX_MEMORY": f"{meta['memory_gb']}G", "JVM_XX_OPTS": JVM_XX_OPTS,
               "MAX_PLAYERS": str(meta["max_players"]), "MOTD": meta["motd"], "DIFFICULTY": meta["difficulty"], "MODE": meta["mode"],
               "ENABLE_WHITELIST": "TRUE" if meta.get("whitelist", True) else "FALSE",
               "ENFORCE_WHITELIST": "TRUE" if meta.get("whitelist", True) else "FALSE",
               "ONLINE_MODE": "TRUE" if meta.get("online_mode", True) else "FALSE", "UID": str(uid), "GID": str(gid),
               "TZ": local_timezone(), "ENABLE_RCON": "TRUE", "STOP_SERVER_ANNOUNCE_DELAY": "5"}
        if meta.get("seed"):
            env["SEED"] = meta["seed"]
        if meta["mods"]:
            env["MODRINTH_PROJECTS"] = ",".join(meta["mods"])
            env["MODRINTH_DOWNLOAD_DEPENDENCIES"] = "required"
        if first and meta.get("players"):
            env["WHITELIST"] = ",".join(meta["players"])
            env["OPS"] = meta["players"][0]
        return env

    def run_container(self, meta, first=False):
        name = f"mc-{meta['slug']}"
        self.docker("rm", "-f", name, timeout=90)
        image = f"{IMAGE}:java{self.catalog.java_for(meta['version'])}"
        bind = "0.0.0.0" if meta.get("public") else self.vpn_address
        args = ["run", "-d", "--name", name, "--label", f"{LABEL}={meta['slug']}", "--restart", "unless-stopped", "--stop-timeout", "60",
                "--memory", f"{meta['memory_gb'] + 1}g", "--cpus", str(self.cpus), "--cpu-shares", "512",
                "--security-opt", "no-new-privileges", "-p", f"{bind}:{meta['port']}:25565",
                "-v", f"{(self.root / meta['slug'] / 'data').resolve()}:/data"]
        for key, value in self.env(meta, first).items():
            args += ["-e", f"{key}={value}"]
        self.docker(*args, image, timeout=120)

    def claim_duckdns(self, host):
        token = duckdns_token(self.secrets_path) if self.secrets_path else ""
        if not token:
            raise McError("DuckDNS isn't set up on the hub, so it can't keep that name pointed at home")
        name = host.removesuffix(".duckdns.org")
        try:
            request = urllib.request.Request(DUCKDNS_UPDATE.format(name=urllib.parse.quote(name), token=token, ip=""),
                                             headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                answer = response.read().decode().strip()
        except OSError as e:
            raise McError(f"Couldn't reach DuckDNS: {e}") from None
        if answer != "OK":
            raise McError(f"DuckDNS refused {host}; add it on duckdns.org under your account first")

    def check_host(self, host):
        home = self.public_ip()
        found = resolve(host)
        if not found:
            return {"ok": False, "text": f"{host} doesn't point anywhere yet; new names can take a few minutes."}
        if home and home in found:
            return {"ok": True, "text": f"Points at your home ({home})."}
        return {"ok": False, "text": f"Points at {', '.join(found)}, not your home ({home or 'unknown'})."}

    def public_ip(self):
        at, ip = self.public_ip_cache
        if ip and time.monotonic() - at < PUBLIC_IP_SECONDS:
            return ip
        try:
            ip = str(fetch_json(PUBLIC_IP).get("ip", ""))
        except (OSError, ValueError):
            return ip
        self.public_ip_cache = (time.monotonic(), ip)
        return ip

    def detail(self, slug):
        meta = self.meta(slug)
        rows = self.containers()
        row = rows.get(slug)
        info = self.summary(meta, row)
        info.update(motd=meta["motd"], difficulty=meta["difficulty"], mode=meta["mode"], mods=meta["mods"], created=meta["created"],
                    disk_used=self.size(slug), vpn_address=f"{self.vpn_address}:{meta['port']}", status=row.get("Status", "") if row else "",
                    cpus=self.cpus, whitelist=self.whitelist(slug), operators=self.operators(slug))
        host = (meta.get("host") or self.public_host or self.public_ip()) if meta.get("public") else ""
        info["host"] = meta.get("host", "")
        info["host_check"] = self.check_host(meta["host"]) if meta.get("host") else None
        info["public_address"] = (host if meta["port"] == FIRST_PORT else f"{host}:{meta['port']}") if host else ""
        info["public_ip"] = self.public_ip() if meta.get("public") and self.public_host else ""
        info["players_online"], info["player_names"], info["cpu_percent"], info["memory_used"] = None, [], None, None
        info["mspt"], info["mspt_p95"], info["tps"] = None, None, None
        if row and row.get("State") == "running":
            try:
                stats = json.loads(self.docker("stats", "--no-stream", "--format", "{{json .}}", f"mc-{slug}", timeout=20))
                info["cpu_percent"] = float(stats["CPUPerc"].rstrip("%") or 0)
                info["memory_used"] = size_bytes(stats["MemUsage"].split("/")[0])
            except (McError, ValueError, KeyError):
                pass
            if info["state"] == "online":
                try:
                    found = PLAYERS_LINE.search(self.docker("exec", f"mc-{slug}", "rcon-cli", "list", timeout=10))
                    if found:
                        info["players_online"] = int(found.group(1))
                        info["player_names"] = [p.strip() for p in found.group(3).split(",") if p.strip()]
                    tick = TICK_LINE.search(ANSI.sub("", self.docker("exec", f"mc-{slug}", "rcon-cli", "tick", "query", timeout=10)))
                    if tick:
                        info["mspt"], info["mspt_p95"] = float(tick.group(1)), float(tick.group(2))
                        info["tps"] = round(min(20.0, 1000.0 / max(info["mspt"], 0.001)), 1)
                except McError:
                    pass
        return info

    def log(self, slug, lines=LOG_LINES):
        self.folder(slug)
        try:
            done = subprocess.run([*self.docker_cmd, "logs", "--tail", str(int(lines)), f"mc-{slug}"], capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            return ""
        if done.returncode != 0:
            return ""
        text = "\n".join(line for line in ANSI.sub("", done.stdout + done.stderr).splitlines() if not QUIET_LOG.search(line))
        return text[-200_000:]

    def action(self, slug, action, by, confirm=""):
        meta = self.meta(slug)
        if self.busy.get(slug):
            raise McError("The server is busy installing; try again shortly")
        if action == "start":
            meta["note"] = ""
            self.save(meta)
            if self.containers().get(slug):
                self.docker("start", f"mc-{slug}")
            else:
                self.run_container(meta)
        elif action == "stop":
            self.docker("stop", f"mc-{slug}", timeout=90)
        elif action == "restart":
            self.docker("restart", f"mc-{slug}", timeout=120)
        elif action == "delete":
            if confirm != meta["name"]:
                raise McError("Type the server's exact name to delete it")
            if meta.get("public"):
                self.set_public(slug, False, by)
            image = self.containers().get(slug, {}).get("Image", "")
            self.docker("rm", "-f", f"mc-{slug}", timeout=90)
            self.drop_unused_image(image)
            trash = self.root / ".trash"
            trash.mkdir(exist_ok=True)
            shutil.move(str(self.root / slug), str(trash / f"{slug}-{datetime.now():%Y%m%d-%H%M%S}"))
        else:
            raise McError("unknown action")
        self.emit("minecraft.changed", slug=slug, action=action, by=by)
        return True

    def update(self, slug, spec, by):
        clean = self.validate(spec, creating=False)
        meta = self.meta(slug)
        if "mods" in clean and meta["loader"] not in MOD_FOLDERS:
            raise McError("Vanilla servers don't take mods")
        clean.pop("players", None)
        clean.pop("seed", None)
        if clean.get("host", "").endswith(".duckdns.org"):
            self.claim_duckdns(clean["host"])
        if "mods" in clean:
            clean["mods"] = sorted({self.catalog.pin(m, meta["loader"], meta["version"]) for m in clean["mods"]})
        if clean.get("online_mode") is False and meta.get("public"):
            raise McError("Close it to friends first: without account checks anyone could join as anyone")
        old_forward = self.forward_name(meta)
        moving = "port" in clean and clean["port"] != meta["port"]
        if moving:
            self.check_port(meta, clean["port"])
        changed = {k for k, v in clean.items() if meta.get(k) != v}
        meta.update(clean)
        self.save(meta)
        self.write_properties(meta)
        if moving and meta.get("public"):
            self.forwards.close(old_forward)
            self.forwards.open(self.forward_name(meta), "TCP", meta["port"])
        if changed <= {"host", "disk_gb"}:
            self.emit("minecraft.changed", slug=slug, action="updated", by=by)
            return self.summary(meta, self.containers().get(slug))
        row = self.containers().get(slug)
        running = row is not None and row.get("State") in ("running", "restarting")
        self.run_container(meta) if running else self.docker("rm", "-f", f"mc-{slug}", timeout=90)
        self.emit("minecraft.changed", slug=slug, action="updated", by=by)
        return self.summary(meta, self.containers().get(slug))

    def write_properties(self, meta):
        path = self.folder(meta["slug"]) / "data" / "server.properties"
        if not path.is_file():
            return
        wanted = {prop: property_text(meta, key) for key, prop in PROPERTIES}
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            name = line.split("=", 1)[0].strip()
            if "=" in line and name in wanted and not line.lstrip().startswith(("#", "!")):
                line = f"{name}={wanted.pop(name)}"
            lines.append(line)
        lines += [f"{name}={value}" for name, value in wanted.items()]
        tmp = path.with_name(".server.properties.saving")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(path)

    def settings_in(self, text):
        found = read_properties(text)
        if found.get("server-port", "25565") != "25565":
            raise McError("Leave server-port at 25565: that's the port inside the server's container. Change the port in the server's settings")
        spec = {}
        for key, prop in PROPERTIES:
            if prop == "enforce-whitelist" or prop not in found:
                continue
            raw = found[prop]
            if key in ("whitelist", "online_mode"):
                if raw.lower() not in ("true", "false"):
                    raise McError(f"{prop} has to be true or false")
                spec[key] = raw.lower() == "true"
            elif key == "max_players":
                if not raw.isdigit():
                    raise McError("max-players has to be a number")
                spec[key] = int(raw)
            else:
                spec[key] = raw
        return self.validate(spec, creating=False)

    def player_file(self, slug, name):
        path = self.folder(slug) / "data" / name
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        except ValueError:
            return []

    def whitelist(self, slug):
        return [p["name"] for p in self.player_file(slug, "whitelist.json")]

    def operators(self, slug):
        return [p["name"] for p in self.player_file(slug, "ops.json")]

    def change_whitelist(self, slug, name, add, by):
        if not PLAYER.fullmatch(str(name or "")):
            raise McError("Minecraft usernames are 3 to 16 letters, numbers or _")
        state = self.state_of(self.containers().get(slug))
        if state == "online":
            verb = "add" if add else "remove"
            reply = self.docker("exec", f"mc-{slug}", "rcon-cli", "whitelist", verb, name, timeout=20).strip()
            if add and "does not exist" in reply:
                mojang_profile(name)
                for wait in (2, 5):
                    time.sleep(wait)
                    reply = self.docker("exec", f"mc-{slug}", "rcon-cli", "whitelist", verb, name, timeout=20).strip()
                    if "does not exist" not in reply:
                        break
                else:
                    raise McError(f"{name} is a real account, but the server couldn't reach Mojang to check it. Try again in a minute.")
        elif state == "stopped":
            entries = self.player_file(slug, "whitelist.json")
            if add:
                profile = mojang_profile(name)
                if not any(e["uuid"] == profile["uuid"] for e in entries):
                    entries.append(profile)
                reply = f"Added {profile['name']} to the whitelist"
            else:
                kept = [e for e in entries if e["name"].lower() != name.lower()]
                reply = f"Removed {name} from the whitelist" if len(kept) < len(entries) else f"{name} isn't on the whitelist"
                entries = kept
            path = self.folder(slug) / "data" / "whitelist.json"
            tmp = path.with_suffix(".saving")
            tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
            tmp.replace(path)
        else:
            raise McError("Wait until the server is online or stopped, then try again")
        self.emit("minecraft.changed", slug=slug, action="whitelist_add" if add else "whitelist_remove", by=by)
        return {"reply": reply, "whitelist": self.whitelist(slug)}

    def command(self, slug, text, chat_as=None):
        self.folder(slug)
        text = " ".join(str(text).split())[:400]
        if not text:
            raise McError("empty command")
        if chat_as:
            payload = json.dumps([{"text": f"<{chat_as}> ", "color": "aqua"}, {"text": text}])
            reply = self.docker("exec", f"mc-{slug}", "rcon-cli", "tellraw", "@a", payload, timeout=15).strip()
            return "Sent, but nobody is online to see it." if "No player was found" in reply else "Sent."
        return ANSI.sub("", self.docker("exec", f"mc-{slug}", "rcon-cli", *text.split(" "), timeout=15)).strip()

    def set_public(self, slug, on, by):
        meta = self.meta(slug)
        if self.forwards is None:
            raise McError("The router isn't set up for the hub")
        if on and not meta.get("online_mode", True):
            raise McError("Turn account checks (online mode) back on before opening it to friends")
        if on:
            taken = {f["external_port"] for f in self.forwards.list() if f["description"] != self.forward_name(meta)}
            if meta["port"] in taken:
                meta["port"] = self.free_port(keep=meta)
                self.save(meta)
            self.forwards.open(self.forward_name(meta), "TCP", meta["port"])
        else:
            self.forwards.close(self.forward_name(meta))
        meta["public"] = bool(on)
        self.save(meta)
        if self.state_of(self.containers().get(slug)) != "stopped":
            self.run_container(meta)
        self.emit("minecraft.changed", slug=slug, action="public" if on else "private", by=by)
        return meta["public"]

    def safe_path(self, slug, rel):
        base = (self.folder(slug) / "data").resolve()
        target = (base / str(rel or "").lstrip("/")).resolve()
        if target != base and base not in target.parents:
            raise McError("That path is outside the server's folder")
        return base, target

    def files(self, slug, rel=""):
        base, target = self.safe_path(slug, rel)
        if not target.exists():
            raise McError("Not found")
        if target.is_dir():
            entries = []
            for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
                stat = entry.lstat()
                entries.append({"name": entry.name, "dir": entry.is_dir(), "size": stat.st_size,
                                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="minutes")})
            return {"path": str(target.relative_to(base)) if target != base else "", "dir": True, "entries": entries}
        stat = target.stat()
        text = None
        if stat.st_size <= TEXT_LIMIT:
            try:
                text = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None and "\x00" in text:
                text = None
        return {"path": str(target.relative_to(base)), "dir": False, "size": stat.st_size, "text": text}

    def write_text(self, slug, rel, text):
        base, target = self.safe_path(slug, rel)
        if target == base or target.is_dir():
            raise McError("That's a folder")
        if len(text.encode("utf-8")) > TEXT_LIMIT:
            raise McError("Too big to edit here")
        settings = None
        if target == base / "server.properties":
            meta = self.meta(slug)
            settings = self.settings_in(text)
            if settings.get("online_mode") is False and meta.get("public"):
                raise McError("Close it to friends first: without account checks anyone could join as anyone")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.saving")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
        if settings:
            meta.update(settings)
            self.save(meta)

    def delete_path(self, slug, rel):
        base, target = self.safe_path(slug, rel)
        if target == base:
            raise McError("Can't delete the whole server folder here")
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        else:
            raise McError("Not found")

    def make_dir(self, slug, rel):
        _, target = self.safe_path(slug, rel)
        target.mkdir(parents=True, exist_ok=True)

    def upload(self, slug, rel_dir, filename, stream, length):
        name = os.path.basename(str(filename or ""))
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise McError("bad file name")
        if length <= 0 or length > UPLOAD_LIMIT:
            raise McError("Files up to 512 MB")
        _, folder = self.safe_path(slug, rel_dir)
        if not folder.is_dir():
            folder.mkdir(parents=True)
        _, target = self.safe_path(slug, f"{rel_dir}/{name}")
        tmp = target.with_name(f".{name}.uploading")
        left = length
        with open(tmp, "wb") as f:
            while left > 0:
                chunk = stream.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            tmp.unlink(missing_ok=True)
            raise McError("The upload was cut off")
        tmp.replace(target)
        return target.name

    def download_path(self, slug, rel):
        _, target = self.safe_path(slug, rel)
        if not target.is_file():
            raise McError("Not found")
        return target

    def mod_files(self, slug):
        meta = self.meta(slug)
        folder = MOD_FOLDERS.get(meta["loader"])
        if folder is None:
            return {"folder": None, "files": []}
        path = self.root / slug / "data" / folder
        files = sorted(e.name for e in path.iterdir() if e.is_file() and e.name.endswith(".jar")) if path.is_dir() else []
        return {"folder": folder, "files": files}

    def check_disk(self):
        rows = self.containers()
        for meta in self.all_meta():
            row = rows.get(meta["slug"])
            if self.state_of(row) == "stopped":
                continue
            used = self.size(meta["slug"], fresh=True)
            if used > meta["disk_gb"] * 1_000_000_000:
                self.docker("stop", f"mc-{meta['slug']}", timeout=90)
                meta["note"] = f"Stopped: it used {used / 1e9:.1f} GB, over its {meta['disk_gb']} GB limit. Raise the limit or free space, then start it."
                self.save(meta)
                self.emit("minecraft.changed", slug=meta["slug"], action="disk_limit", by="hub")

    def watch(self):
        def loop():
            while not self.stopping.wait(DISK_SECONDS):
                try:
                    self.check_disk()
                except Exception as e:
                    self.emit("service.warning", message=f"minecraft disk check: {type(e).__name__}: {e}")

        self.watcher = threading.Thread(target=loop, name="minecraft-disk", daemon=True)
        self.watcher.start()
        return self

    def stop(self):
        self.stopping.set()
