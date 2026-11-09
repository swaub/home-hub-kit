import argparse
import sys
import time
from datetime import date, datetime

from homehub.config import ConfigError, load_config
from homehub.version import describe


def open_storage(cfg):
    from homehub.storage import Storage

    return Storage(cfg.data_dir() / "homehub.db")


def cmd_people(args):
    from homehub import household

    cfg = load_config(args.config)
    storage = open_storage(cfg)
    try:
        if args.people_command == "import":
            summary = household.sync_household(cfg, storage, args.file, force=True)
            print(f"Imported {summary['path']}: {summary['people']} people, {summary['devices']} devices, "
                  f"{summary['accounts']} accounts, {summary['grants']} grants.")
            for key, label in (("missing_people", "People"), ("missing_devices", "Devices")):
                if summary[key]:
                    print(f"{label} in the database but no longer in the file (kept): {', '.join(summary[key])}")
        else:
            household.sync_household(cfg, storage, args.file)
            print(household.describe_household(storage))
    finally:
        storage.close()
    return 0


def cmd_status(args):
    from homehub.brain.session import auth_status, describe_auth
    from homehub.instance import InstanceLock, StatusFile

    cfg = load_config(args.config)
    state_dir = cfg.state_dir()
    status = StatusFile.read(state_dir)
    pid = InstanceLock.holder_pid(state_dir)
    lines = []
    if status and status["alive"] and pid in (None, status["pid"]):
        started = datetime.fromisoformat(status["started"])
        up = datetime.now().astimezone() - started
        lines.append(f"running: yes (pid {status['pid']}, started {status['started']}, up {int(up.total_seconds() // 3600)}h {int(up.total_seconds() % 3600 // 60)}m)")
    elif status:
        lines.append(f"running: no (last status {status['updated']} from pid {status['pid']}, which has exited)")
    else:
        lines.append("running: no (never started on this machine)")
    lines.append(f"version: {status['version'] if status else describe()}")
    if status:
        for key in ("state", "last_error"):
            if key in status:
                value = status[key]
                if isinstance(value, dict):
                    value = ", ".join(f"{k}: {v}" for k, v in value.items())
                lines.append(f"{key.replace('_', ' ')}: {value}")
    auth_level, auth_message = describe_auth(auth_status())
    lines.append(f"claude: {auth_message}" + ("" if auth_level == "ok" else f" [{auth_level}]"))
    storage = open_storage(cfg)
    try:
        today = storage.costs_daily(day=date.today())
        turns = sum(r["turns"] for r in today)
        lines.append(f"cost today: ${sum(r['usd'] for r in today):.4f} API-equivalent over {turns} turn(s)")
    finally:
        storage.close()
    lines.append(f"state directory: {state_dir}")
    print("\n".join(lines))
    return 0


def cmd_logs(args):
    from homehub.logging_setup import log_file_name

    cfg = load_config(args.config)
    day = date.fromisoformat(args.date) if args.date else date.today()
    path = cfg.state_dir() / "logs" / log_file_name(day)
    if not path.exists():
        print(f"no log for {day.isoformat()} at {path}", file=sys.stderr)
        return 1
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
        for line in lines[-args.lines:] if args.lines else lines:
            sys.stdout.write(line)
        sys.stdout.flush()
        if not args.follow:
            return 0
        try:
            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.25)
        except KeyboardInterrupt:
            return 0


LINE_STYLE = {"you": ("[YOU]", "\033[92m"), "claude": ("[CLAUDE]", "\033[96m"), "action": ("[ACTION]", "\033[93m"),
              "error": ("[ERROR]", "\033[91m"), "info": ("[INFO]", "\033[90m")}


class Renderer:
    def __init__(self, cfg, out=None, color=True):
        self.cfg = cfg
        self.out = out or sys.stdout
        self.color = color

    def paint(self, text, code):
        return f"{code}{text}\033[0m" if self.color else text

    def format(self, event):
        name = event["name"]
        if name == "transcript.line":
            label, code = LINE_STYLE.get(event["kind"], ("[" + event["kind"].upper() + "]", ""))
            return self.paint(f"{label} {event['text']}", code)
        if name == "service.ready":
            return self.paint(f"Ready in {event['startup_seconds']} s", "\033[92m")
        if name == "service.starting":
            return self.paint("Starting up...", "\033[90m")
        if name == "service.warning":
            return self.paint(f"[WARN] {event['message']}", "\033[93m")
        if name == "brain.error":
            return self.paint(f"[ERROR] Claude ({event['kind']}): {event['message']}", "\033[91m")
        if name == "reminder.due":
            return self.paint(f"[REMINDER] {event['kind']}: {event['label']}", "\033[93m")
        if name == "person.identified":
            who = event.get("person_id") or "unknown device"
            return self.paint(f"[WHO] {who} ({str(event.get('how')).replace('_', ' ')})", "\033[95m")
        return None

    def handle(self, event):
        line = self.format(event)
        if line is not None:
            self.out.write(line + "\n")
            self.out.flush()


def cmd_run(args):
    import asyncio

    from homehub.app import App
    from homehub.instance import AlreadyRunning

    cfg = load_config(args.config)
    app = App(cfg, console=args.headless)
    if args.seconds:
        import threading

        threading.Timer(args.seconds, app.stop.set).start()
    try:
        app.prepare()
    except AlreadyRunning as e:
        print(f"error: {e}", file=sys.stderr)
        return 4
    renderer = None if args.headless else Renderer(cfg, color=sys.stdout.isatty())

    async def main():
        if sys.platform != "win32":
            import signal

            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, app.stop.set)
        app.bus.start()
        if renderer is not None:
            app.bus.subscribe("*", renderer.handle)
        await app.run()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye.")
    except LookupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    return 0


TEST_STAGES = {
    "config": "tests/test_config.py", "logging": "tests/test_logging_setup.py", "bus": "tests/test_bus.py", "storage": "tests/test_storage.py",
    "household": "tests/test_household.py", "people": "tests/test_household.py", "doctor": "tests/test_doctor.py", "cli": "tests/test_cli.py",
    "instance": "tests/test_instance.py", "tools": "tests/tools",
    "timers": "tests/tools/test_timers.py", "memory": "tests/tools/test_memory.py", "network": "tests/tools/test_network.py",
    "internet": "tests/tools/test_internet.py", "wake on lan": "tests/tools/test_wake_on_lan.py", "wol": "tests/tools/test_wake_on_lan.py",
    "google": "tests/tools/test_google.py", "phone": "tests/tools/test_phone_adb.py", "identity": "tests/tools/test_identity.py",
    "service": "tests/test_service.py", "backup": "tests/test_backup.py", "scripts": "tests/test_scripts.py",
    "kdeconnect": "tests/tools/test_phone_kdeconnect.py", "agent": "tests/pc_agent", "pc remote": "tests/tools/test_pc_remote.py", "notify": "tests/tools/test_notify.py",
    "home": "tests/tools/test_home_assistant.py", "remote": "tests/test_remote.py", "vpn": "tests/test_vpn.py",
    "permissions": "tests/brain/test_permissions.py", "prompt": "tests/brain/test_prompt_and_costs.py", "session": "tests/brain/test_session.py",
    "run": "tests/test_run_command.py",
}


def cmd_test(args):
    import subprocess

    from homehub.config import REPO_ROOT

    targets = []
    for stage in args.stages:
        if stage not in TEST_STAGES:
            print(f"error: unknown stage '{stage}'; stages: {', '.join(TEST_STAGES)}", file=sys.stderr)
            return 2
        targets.append(TEST_STAGES[stage])
    command = [sys.executable, "-m", "pytest", "-q", *(targets or ["tests"])]
    excluded = [m for m, flag in (("live", args.live), ("claude", args.claude)) if not flag]
    if excluded:
        command += ["-m", " and ".join(f"not {m}" for m in excluded)]
    print(" ".join(command), flush=True)
    return subprocess.run(command, cwd=REPO_ROOT).returncode


def cmd_spotify(args):
    from homehub.tools.spotify import NOT_SET_UP, Spotify, SpotifyError, ready

    cfg = load_config(args.config)
    client = Spotify(cfg)
    try:
        if args.action == "login":
            print("Open this link on your phone or laptop, approve, then paste the whole address the browser ends on")
            print("(the page itself may say it can't connect; that's expected):")
            print(client.auth_url())
            client.login(input("address: "))
            print("Spotify is connected. " + client.now())
        else:
            print(client.now() if ready(cfg) else NOT_SET_UP)
    except SpotifyError as e:
        print(e)
        return 1
    return 0


def cmd_google_tasks(args):
    from homehub.tools.google_tasks import NOT_SET_UP, GoogleTasks, TasksError, ready

    cfg = load_config(args.config)
    client = GoogleTasks(cfg)
    try:
        if args.action == "login":
            print("Open this link on your phone or laptop, sign in to Google and allow Tasks, then paste the whole address")
            print("the browser ends on (the page itself may say it can't connect; that's expected):")
            print(client.auth_url())
            client.login(input("address: "))
            print("Google Tasks is connected. " + client.read())
        else:
            print(client.read() if ready(cfg) else NOT_SET_UP)
    except TasksError as e:
        print(e)
        return 1
    return 0


def cmd_packages(args):
    from homehub.packages import PackageError, PackageTracker
    from homehub.storage import Storage

    cfg = load_config(args.config)
    storage = Storage(cfg.data_dir() / "homehub.db")
    owner = next((p["id"] for p in storage.people() if p["role"] == "owner"), None)
    tracker = PackageTracker(cfg, storage, owner)
    try:
        if args.action == "login":
            print("Open this link on your phone or laptop, sign in to Google and allow reading email, then paste the whole address")
            print("the browser ends on (the page itself may say it can't connect; that's expected):")
            print(tracker.connect_url())
            found = tracker.connect(input("address: "))
            print(f"Gmail is connected; {found} shipping emails read. " + tracker.describe())
        elif args.action == "scan":
            print(f"{tracker.scan()} new shipping emails. " + tracker.describe())
        else:
            print(tracker.describe())
    except PackageError as e:
        print(e)
        return 1
    finally:
        storage.close()
    return 0


def cmd_vpn(args):
    import subprocess

    from homehub.vpn import Vpn, VpnError

    cfg = load_config(args.config)
    vpn = Vpn(cfg)
    owner = {"person": args.person, "role": "owner", "ip": ""}
    try:
        if args.action == "add":
            out = vpn.add(args.name, args.person, owner)
            print(out["text"])
            subprocess.run(["qrencode", "-t", "ANSIUTF8", "-m", "2"], input=out["conf"].encode())
            print("Or import this text as a .conf file:\n" + out["conf"])
            print("The assistant gives this device app access after its next restart: systemctl --user restart homehub")
        elif args.action == "remove":
            print(vpn.remove(args.name, owner)["text"])
        else:
            for d in vpn.overview(owner)["devices"]:
                print(f"{d['name']:<16} {d['ip']:<12} {d['person'] or '-':<10} {'connected' if d['online'] else 'not connected'}")
    except VpnError as e:
        print(e)
        return 1
    return 0


def cmd_ps5(args):
    from homehub.tools.ps5 import Ps5, Ps5Error, pair

    cfg = load_config(args.config)
    try:
        if args.action == "pair":
            from pyremoteplay.oauth import get_login_url

            print("1. On the PS5: Settings > System > Remote Play > Link Device, and keep the 8-digit code on screen.")
            print("2. Open this link, sign in to PlayStation Network, then paste the whole address the browser ends on:")
            print(get_login_url())
            redirect = input("address: ")
            pin = input("PS5 code: ")
            data = pair(cfg, redirect, pin, args.host or "")
            print(f"Paired with {data['host_name']} at {data['ip']}.")
        else:
            print(Ps5(cfg).status())
    except ImportError:
        print("Pairing needs the ps5 extra: .venv/bin/pip install -e '.[ps5]'")
        return 1
    except Ps5Error as e:
        print(e)
        return 1
    return 0


def cmd_phone(args):
    import asyncio

    from homehub import household
    from homehub.tools.phone_kdeconnect import NOT_RUNNING, KdeConnect, pair_target, resolve_phone

    cfg = load_config(args.config)
    storage = open_storage(cfg)
    client = KdeConnect(storage=storage)

    async def run():
        household.sync_household(cfg, storage)
        if not client.bus_address():
            print("error: no D-Bus session bus here; KDE Connect needs the hub's user session", file=sys.stderr)
            return 3
        if not await client.available():
            print(f"error: {NOT_RUNNING}", file=sys.stderr)
            return 1
        if args.action == "list":
            devices = await client.devices()
            for d in devices:
                owner, registry_id = client.person_for_device(d["id"])
                print(f"{d['name']}  {d['type']}  {'paired' if d['paired'] else 'not paired'}  {'reachable' if d['reachable'] else 'away'}  {d['id']}"
                      + (f"  -> {registry_id} ({owner})" if registry_id else ""))
            print(f"{len(devices)} device(s) known to KDE Connect")
            return 0
        from homehub.tools import CallContext

        person, device, problem = resolve_phone(storage, CallContext(), args.person or "")
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return 2
        target, candidates = pair_target(await client.devices(only_reachable=True), device["name"])
        if target is None:
            print("error: can't tell which phone to pair; reachable unpaired devices: " + (", ".join(d["name"] for d in candidates) or "none"), file=sys.stderr)
            return 1
        await client.request_pair(target["id"])
        print(f"pairing request sent to {target['name']}; accept it on the phone")
        for _ in range(60):
            await asyncio.sleep(1.0)
            if (await client.device_state(target["id"]))["paired"]:
                storage.set_device_runtime(device["id"], kdeconnect_id=target["id"])
                print(f"paired: {target['name']} is now {person['name']}'s phone")
                return 0
        print("error: not accepted within a minute", file=sys.stderr)
        return 1

    try:
        return asyncio.run(run())
    finally:
        client.close()
        storage.close()


def cmd_backup(args):
    from homehub.backup import BackupError, list_backups, make_backup, restore_backup, verify_archive

    cfg = load_config(args.config)
    directory = args.to or cfg.backup.dir
    try:
        if args.list:
            backups = list_backups(directory)
            for path in backups:
                print(f"{path.name}  {path.stat().st_size / 1e6:.1f} MB")
            print(f"{len(backups)} backup(s) in {directory}")
            return 0
        if args.restore:
            manifest = verify_archive(args.restore)
            if not args.yes:
                try:
                    answer = input(f"This replaces the database in {cfg.data_dir()} with the one from {args.restore} (made {manifest['created']}); "
                                   "the current database is kept beside it. Continue? [y/N] ")
                except EOFError:
                    answer = ""
                if answer.strip().lower() not in ("y", "yes"):
                    print("not restored")
                    return 1
            result = restore_backup(cfg, args.restore)
            print(f"restored {result['database']} from {args.restore} (backup made {result['manifest']['created']} by {result['manifest']['version']})")
            if result["previous"]:
                print(f"the previous database is kept as {result['previous']}")
            if result["files_for_you"]:
                print(f"config files from the backup are in {result['files_dir']} for you to copy by hand: {', '.join(result['files_for_you'])}")
            return 0
        result = make_backup(cfg, dest=args.to, keep=args.keep, copy_to=args.copy_to)
        print(f"backed up to {result['path']} ({result['bytes'] / 1e6:.1f} MB, {result['seconds']} s): {', '.join(result['files'])}")
        if result["copied_to"]:
            print(f"copied to {result['copied_to']}")
        if result["removed"]:
            print(f"removed {len(result['removed'])} old backup(s)")
        return 0
    except BackupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def cmd_notify(args):
    from homehub.tools.notify import Ntfy

    cfg = load_config(args.config)
    ntfy = Ntfy(cfg.notify.url, cfg.notify.topic)
    if not ntfy.enabled:
        print("error: push notifications aren't set up (notify.url and notify.topic in the config)", file=sys.stderr)
        return 1
    try:
        ntfy.send(" ".join(args.message)[:1000], title=args.title or None, priority=args.priority)
    except (OSError, ValueError) as e:
        print(f"error: couldn't send the notification: {e}", file=sys.stderr)
        return 1
    print("sent")
    return 0


def cmd_doctor(args):
    from homehub.doctor import format_results, run_checks

    cfg = load_config(args.config)
    text, failures = format_results(run_checks(cfg))
    print(text)
    return 1 if failures else 0


def build_parser():
    parser = argparse.ArgumentParser(prog="homehub", description="Home Hub: the private app and Claude chat for your home server")
    parser.add_argument("--version", action="version", version=describe())
    parser.add_argument("--config", metavar="PATH", help="config file (default: config/config.toml, else config/config.example.toml)")
    parser.set_defaults(func=None)
    commands = parser.add_subparsers(dest="command")

    run = commands.add_parser("run", help="run the hub: the private app, reminders, package tracking and the status page (Ctrl+C to stop)")
    run.add_argument("--seconds", type=float, metavar="N", help="stop on its own after N seconds (for checks and demos)")
    run.add_argument("--headless", action="store_true", help="plain log lines instead of the live activity view (for systemd)")
    run.set_defaults(func=cmd_run)

    people = commands.add_parser("people", help="show or import the household (people, devices, accounts, grants)")
    people.add_argument("people_command", nargs="?", choices=["list", "import"], default="list")
    people.add_argument("--file", metavar="PATH", help="household file (default: config/people.toml, else config/people.example.toml)")
    people.set_defaults(func=cmd_people)

    test = commands.add_parser("test", help="run the test suite or named stages (config, bus, storage, timers, remote, session, ...)")
    test.add_argument("stages", nargs="*")
    test.add_argument("--live", action="store_true", help="include tests that need real devices")
    test.add_argument("--claude", action="store_true", help="include tests that make real Claude calls")
    test.set_defaults(func=cmd_test)

    status = commands.add_parser("status", help="is the hub running, login state, last error, today's cost")
    status.set_defaults(func=cmd_status)

    logs = commands.add_parser("logs", help="show or follow the day's log")
    logs.add_argument("-f", "--follow", action="store_true", help="keep printing new lines until Ctrl+C")
    logs.add_argument("-n", "--lines", type=int, default=0, help="only the last N lines")
    logs.add_argument("--date", metavar="YYYY-MM-DD", help="another day's log")
    logs.set_defaults(func=cmd_logs)

    phone = commands.add_parser("phone", help="KDE Connect phones: list them or pair one with a household member")
    phone.add_argument("action", choices=["list", "pair"])
    phone.add_argument("person", nargs="?", help="who the phone belongs to (for pair)")
    phone.set_defaults(func=cmd_phone)
    spotify = commands.add_parser("spotify", help="connect Spotify (login) or show what it's playing (status)")
    spotify.add_argument("action", choices=["login", "status"])
    spotify.set_defaults(func=cmd_spotify)
    google_tasks = commands.add_parser("google-tasks", help="connect Google Tasks (login) or show the default list (status)")
    google_tasks.add_argument("action", choices=["login", "status"])
    google_tasks.set_defaults(func=cmd_google_tasks)
    packages = commands.add_parser("packages", help="connect Gmail for package tracking (login), check email now (scan) or list packages")
    packages.add_argument("action", choices=["login", "scan", "list"])
    packages.set_defaults(func=cmd_packages)
    ps5 = commands.add_parser("ps5", help="pair the PS5 for power control (pair) or say whether it's on (status)")
    ps5.add_argument("action", choices=["pair", "status"])
    ps5.add_argument("--host", help="the PS5's address, if discovery can't find it")
    ps5.set_defaults(func=cmd_ps5)
    vpn = commands.add_parser("vpn", help="VPN devices: list them, add one and show its setup QR code in the terminal, or remove one (for recovery from the hub's own screen)")
    vpn.add_argument("action", choices=["list", "add", "remove"], nargs="?", default="list")
    vpn.add_argument("name", nargs="?", default="")
    vpn.add_argument("--person", default="", help="household member the new device belongs to (add)")
    vpn.set_defaults(func=cmd_vpn)
    backup = commands.add_parser("backup", help="back up the database and config files to an archive (the nightly timer runs this), list backups, or restore one")
    backup.add_argument("--to", metavar="DIR", help="where to write the archive (default: backup.dir from the config)")
    backup.add_argument("--keep", type=int, metavar="N", help="how many archives to keep (default: backup.keep)")
    backup.add_argument("--copy-to", metavar="TARGET", help="also copy the archive to a folder (USB drive) or an rsync target user@host:path")
    backup.add_argument("--list", action="store_true", help="list the archives")
    backup.add_argument("--restore", metavar="FILE", help="restore the database from this archive (asks first)")
    backup.add_argument("--yes", action="store_true", help="skip the restore confirmation")
    backup.set_defaults(func=cmd_backup)
    notify = commands.add_parser("notify", help="push a message to the owner's phone through [notify] (the notify-failure unit runs this when a hub job fails)")
    notify.add_argument("message", nargs="+")
    notify.add_argument("--title", default="", help="the notification's title")
    notify.add_argument("--priority", type=int, choices=range(1, 6), default=3, metavar="1-5", help="ntfy priority (default 3)")
    notify.set_defaults(func=cmd_notify)
    doctor = commands.add_parser("doctor", help="check the config, household, database, Claude login, clock, disk and the service")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.func is None:
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
