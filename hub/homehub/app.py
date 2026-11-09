import asyncio
import logging
import os
import subprocess
import threading
import time

from homehub import household, power
from homehub.brain.costs import CostTracker
from homehub.brain.permissions import Permissions
from homehub.brain.session import auth_status, describe_auth, scrub_environment
from homehub.bus import Bus
from homehub.instance import InstanceLock, StatusFile
from homehub.logging_setup import setup_logging
from homehub.service import Notifier
from homehub.storage import Storage
from homehub.tools import Registry
from homehub.tools import minecraft as minecraft_tools
from homehub.tools import (adguard, bluetooth, builtins, cast, cozi, directions, discord, finance, google, google_tasks, home_assistant, hub,
                           identity, immich, internet, jellyfin, lights, lists, memory, movies, network, news, notify, paperless, pc_remote,
                           phone_adb, phone_kdeconnect, proxmox, ps5, psn, roku, router, shopping, sports, spotify, steam, timers, tracking, tv,
                           wake_on_lan, weather)
from homehub.tools.timers import Pusher, Scheduler
from homehub.version import describe
from homehub.remote import RemoteApp, RemoteChat, parse_apps, parse_peers
from homehub.vpn import Vpn
from homehub.minecraft import Minecraft
from homehub.packages import PackageTracker
from homehub.sysinfo import SystemStats
from homehub.web import StatusPage

log = logging.getLogger("homehub.app")


def tool_modules(cfg, scheduler, storage, phone=None, minecraft=None, forwards=None, power=None, apps=()):
    caster = cast.Caster(cfg) if cfg.cast.device.strip() else None
    extra = [minecraft_tools.build_module(minecraft)] if minecraft is not None else []
    return [
        timers.build_module(scheduler, storage),
        memory.build_module(storage),
        lists.build_module(storage),
        shopping.build_module(storage, notify.Ntfy(cfg.notify.url, cfg.notify.topic)),
        network.build_module(cfg.features.network_scans),
        router.build_module(cfg),
        tv.build_module(cfg),
        roku.build_module(cfg),
        adguard.build_module(cfg),
        proxmox.build_module(cfg),
        lights.build_module(cfg),
        cast.build_module(cfg, caster),
        jellyfin.build_module(cfg, caster),
        ps5.build_module(cfg),
        psn.build_module(cfg),
        steam.build_module(cfg),
        bluetooth.build_module(),
        internet.build_module(),
        weather.build_module(storage),
        news.build_module(cfg),
        sports.build_module(),
        finance.build_module(),
        movies.build_module(cfg),
        tracking.build_module(cfg, storage),
        cozi.build_module(cfg),
        directions.build_module(cfg, storage),
        spotify.build_module(cfg),
        discord.build_module(cfg),
        google_tasks.build_module(cfg),
        wake_on_lan.build_module(storage),
        identity.build_module(storage),
        phone_kdeconnect.build_module(storage, phone),
        phone_adb.build_module(screen_only=cfg.features.kdeconnect),
        pc_remote.build_module(storage, cfg, notify.Ntfy(cfg.notify.url, cfg.notify.topic)),
        notify.build_module(cfg),
        home_assistant.build_module(cfg),
        hub.build_module(power=power, forwards=forwards, apps=apps),
        *extra,
        google.MODULE,
        builtins.WEB,
        builtins.FILES,
        builtins.SHELL,
        builtins.HIDDEN_BUILTINS,
    ]


class App:
    def __init__(self, cfg, console=False, notifier=None):
        self.cfg = cfg
        self.notifier = notifier or Notifier()
        self.last_heartbeat = 0.0
        self.console = console
        self.stop = threading.Event()
        self.bus = Bus()
        self.storage = None
        self.registry = None
        self.permissions = None
        self.costs = None
        self.scheduler = None
        self.pusher = None
        self.phone = None
        self.lock = None
        self.status_file = None
        self.started_at = None
        self.started_wall = time.time()
        self.ready_wall = None
        self.checks = {}
        self.ntfy = notify.Ntfy(cfg.notify.url, cfg.notify.topic)
        self.web = None
        self.remote = None
        self.remote_chat = None
        self.minecraft = None
        self.forwards = None
        self.packages = None

    def emit(self, name, **payload):
        self.bus.publish(name, **payload)

    def prepare(self):
        removed = scrub_environment()
        setup_logging(self.cfg.state_dir(), self.cfg.logging.retention_days, console=self.console)
        self.lock = InstanceLock(self.cfg.state_dir()).acquire()
        self.status_file = StatusFile(self.cfg.state_dir())
        self.storage = Storage(self.cfg.data_dir() / "homehub.db")
        household.sync_household(self.cfg, self.storage)
        self.pusher = Pusher(self.ntfy, self.storage, emit=self.emit)
        self.scheduler = Scheduler(self.storage, emit=self.emit, on_due=self.pusher.push)
        self.phone = phone_kdeconnect.KdeConnect(emit=self.emit, storage=self.storage) if self.cfg.features.kdeconnect else None
        self.build_minecraft()
        self.registry = Registry(self.cfg, tool_modules(self.cfg, self.scheduler, self.storage, self.phone, self.minecraft, self.forwards,
                                                        self.power, parse_apps(self.cfg.remote.apps)), emit=self.emit)
        self.permissions = Permissions(self.registry, self.storage)
        self.costs = CostTracker(self.storage, emit=self.emit)
        self.checks["environment"] = f"removed {', '.join(removed)}" if removed else "clean"
        return self

    async def start(self):
        self.started_at = time.perf_counter()
        self.bus.start()
        self.bus.attach_logging()
        if self.cfg.web.port:
            self.web = StatusPage(self.cfg.web.host, self.cfg.web.port, lambda: {"app": "running" if self.remote is not None else "off"},
                                  describe(), lists=self.current_lists).start()
            self.bus.subscribe("transcript.line", self.web.on_event)
        self.emit("service.starting", version=describe())
        self.checks["claude"] = describe_auth(auth_status())[1]
        self.scheduler.start()
        self.checks["reminders"] = "pushed to the phone" if self.ntfy.enabled else "shown in the app only; set [notify] to push them to the phone"
        if self.phone is not None:
            await self.start_phone()
        self.start_packages()
        if self.cfg.remote.port:
            self.start_remote()
        ready = round(time.perf_counter() - self.started_at, 1)
        self.ready_wall = time.time()
        self.emit("service.ready", startup_seconds=ready, checks=self.checks)
        self.write_status()
        self.notifier.ready(f"ready in {ready} s")
        self.last_heartbeat = time.monotonic()
        return ready

    async def heartbeat(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_heartbeat < self.notifier.interval():
            return False
        self.last_heartbeat = now
        return self.notifier.watchdog()

    async def start_phone(self):
        try:
            if await self.phone.available():
                await self.phone.listen()
                self.checks["phone"] = "KDE Connect connected"
            else:
                self.checks["phone"] = "KDE Connect not running"
                self.emit("service.warning", message="KDE Connect isn't running; phone tools will say so until it is")
        except Exception as e:
            self.checks["phone"] = f"KDE Connect error: {e}"
            self.emit("service.warning", message=f"KDE Connect: {e}")

    def start_packages(self):
        owner = next((p["id"] for p in self.storage.people() if p["role"] == "owner"), None)
        if owner is None:
            return
        push = lambda title, text, url: self.ntfy.send(text or title, title=title, tags=("package",), click=url or None) if self.ntfy.enabled else None
        self.packages = PackageTracker(self.cfg, self.storage, owner, emit=self.emit, notify=push)
        if self.cfg.packages.scan_minutes:
            self.packages.start(self.cfg.packages.scan_minutes)
            self.checks["packages"] = f"shipping emails checked every {self.cfg.packages.scan_minutes} min" + (
                "" if self.packages.gmail.ready() else " once Gmail is connected")

    def start_remote(self):
        self.remote_chat = RemoteChat(self.cfg, self.registry, self.permissions, self.storage, self.costs, self.emit)
        status = lambda: {"version": describe(), "lists": self.current_lists(), "timers": self.current_timers()}
        stats = SystemStats()
        system = lambda: {**stats.sample(), "now": round(time.time()), "assistant": {"started_at": round(self.started_wall),
                                                          "ready_at": round(self.ready_wall) if self.ready_wall else None,
                                                          "checks": self.checks, "version": describe()}}
        if self.minecraft is not None:
            self.minecraft.watch()
            self.checks["minecraft"] = f"servers in {self.cfg.minecraft.root}"
        panels = self.panels()
        peers = {**parse_peers(self.cfg.remote.peers), **{ip: person for ip, person in panels["vpn"].owners().items() if person}}
        self.remote = RemoteApp(self.cfg.remote.host, self.cfg.remote.port, self.cfg.remote.address, peers,
                                self.remote_chat, asyncio.get_running_loop(), status, system, self.power,
                                parse_apps(self.cfg.remote.apps), self.minecraft, self.forwards, self.packages, self.remote_tls(),
                                panels).start()
        panels["vpn"].on_change = self.remote.peer_changed
        self.checks["remote"] = f"private app on {self.cfg.remote.address}:{self.remote.port} for {len(self.remote.peers)} device(s)"

    def panels(self):
        found = {"vpn": Vpn(self.cfg)}
        if self.cfg.proxmox.host:
            found["proxmox"] = proxmox.Proxmox(self.cfg)
        if tv.load_settings(self.cfg) is not None:
            found["tv"] = tv.TvPanel(self.cfg)
        if self.cfg.roku.devices:
            found["roku"] = roku.RokuPanel(self.cfg)
        if jellyfin.load_settings(self.cfg) is not None:
            found["jellyfin"] = jellyfin.JellyfinPanel(self.cfg, cast.Caster(self.cfg) if self.cfg.cast.device.strip() else None, found.get("roku"))
        if immich.load_settings(self.cfg) is not None:
            found["photos"] = immich.ImmichPanel(self.cfg)
        if paperless.load_settings(self.cfg) is not None:
            found["docs"] = paperless.PaperlessPanel(self.cfg)
        if self.cfg.media.folder:
            from homehub.media import MediaLibrary, jellyfin_settings

            found["media"] = MediaLibrary(self.cfg, jellyfin_settings(self.cfg))
        if psn.load_login(self.cfg) is not None:
            found["psn"] = psn.Psn(self.cfg, notify=self.psn_reminder).warm()
        guard = adguard.AdGuard(self.cfg) if adguard.load_login(self.cfg) is not None else None
        found["hub"] = hub.HubPanel(self.cfg, guard, self.device_names, bluetooth.Bluetooth() if self.cfg.features.bluetooth else None)
        return found

    def psn_reminder(self, title, text):
        self.emit("service.warning", message=f"{title}. {text}")
        if self.ntfy.enabled:
            try:
                self.ntfy.send(text, title=title, tags=("video_game",))
            except Exception as e:
                self.emit("service.warning", message=f"PlayStation reminder push failed: {type(e).__name__}: {e}")

    def device_names(self):
        try:
            rows = router.RouterReader(self.cfg).read() or []
        except Exception:
            return {}
        return {row["ip"]: row["name"] for row in rows if row.get("ip") and row.get("name") and row["name"] != "Unknown"}

    def build_minecraft(self):
        settings = router.load_settings(self.cfg)
        if settings is not None:
            try:
                self.forwards = router.PortForwards(settings)
            except OSError as e:
                self.emit("service.warning", message=f"router port forwards unavailable: {e}")
        if self.cfg.minecraft.root:
            self.minecraft = Minecraft(self.cfg.minecraft.root, vpn_address=self.cfg.remote.address, cpus=self.cfg.minecraft.cpus,
                                       max_memory_gb=self.cfg.minecraft.max_memory_gb, emit=self.emit, forwards=self.forwards,
                                       public_host=self.cfg.minecraft.public_host, secrets_path=self.cfg.data_dir() / "secrets.toml")

    def power(self, action, person_id, wake_at=None):
        if action in ("hub", "off"):
            verb = "reboot" if action == "hub" else "poweroff"
            if not power.sudo_allows("/usr/bin/systemctl", verb):
                return False, f"The hub doesn't allow the assistant to {'restart' if action == 'hub' else 'shut'} it{' down' if action == 'off' else ''}."
            try:
                if action == "off" and wake_at is not None:
                    wake_at = power.check_wake(wake_at)
                    power.set_wake(wake_at)
                    message = f"Shutting down the hub. It turns itself back on {power.when(wake_at)}"
                elif action == "off":
                    power.clear_wake()
                    message = "Shutting down the hub. It stays off until someone presses its power button"
                else:
                    message = "Restarting the hub"
            except power.PowerError as e:
                return False, str(e)
            command = ["sudo", "-n", "/usr/bin/systemctl", verb]
            reason = "hub reboot" if action == "hub" else "hub shutdown"
        else:
            if not os.environ.get("INVOCATION_ID"):
                return False, "The assistant isn't running as a service here, so it can't restart itself."
            command = ["systemctl", "--user", "--no-block", "restart", "homehub"]
            reason, message = "assistant restart", "Restarting the assistant"
        self.emit("service.stopping", reason=f"{reason} by {person_id}")
        threading.Timer(1.0, subprocess.run, args=(command,), kwargs={"timeout": 30}).start()
        return True, message

    def remote_tls(self):
        remote = self.cfg.remote
        if not remote.tls_port:
            return None
        return {"port": remote.tls_port, "cert": os.path.expanduser(remote.tls_cert), "key": os.path.expanduser(remote.tls_key),
                "name": remote.public_name, "host": remote.address}

    def current_timers(self):
        return [{"id": i["id"], "kind": i["kind"], "label": i["label"], "due": i["due"], "repeat": i.get("repeat")}
                for i in self.scheduler.upcoming()]

    def current_lists(self):
        return {name: [i["text"] for i in self.storage.list_items(name)] for name, _ in self.storage.list_names()}

    def write_status(self):
        return self.status_file.write(state={"app": "running" if self.remote is not None else "off"}, last_error=None,
                                      checks=self.checks, costs=self.costs.summary() if self.costs else "")

    async def run(self):
        try:
            ready = await self.start()
        except Exception:
            await self.close()
            raise
        try:
            while not self.stop.is_set():
                self.write_status()
                await self.heartbeat()
                await asyncio.sleep(1.0)
        finally:
            await self.close()
        return ready

    async def close(self):
        self.stop.set()
        self.notifier.stopping()
        self.emit("service.stopping", reason="stop requested")
        if self.scheduler is not None:
            self.scheduler.stop()
        if self.phone is not None:
            self.phone.close()
        if self.web is not None:
            self.web.stop()
        if self.remote is not None:
            self.remote.stop()
        if self.minecraft is not None:
            self.minecraft.stop()
        if self.packages is not None:
            self.packages.stop()
        if self.remote_chat is not None:
            await self.remote_chat.close()
        try:
            await self.bus.drain(timeout=2.0)
        except TimeoutError:
            pass
        await self.bus.close()
        if self.storage is not None:
            self.storage.close()
        if self.lock is not None:
            self.lock.release()
