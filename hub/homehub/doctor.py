import os
import shutil
import subprocess
from datetime import datetime

from homehub import household
from homehub.brain.session import SECRET_ENV_VARS, auth_status, describe_auth
from homehub.config import ConfigError
from homehub.instance import InstanceLock, StatusFile
from homehub.storage import Storage

MIN_FREE_BYTES = 1 << 30


def check_household(cfg):
    path = household.people_path()
    try:
        parsed, _ = household.load_household(path)
    except ConfigError as e:
        return "fail", str(e)
    return "ok", f"{len(parsed['people'])} people, {len(parsed['devices'])} devices in {path}"


def check_storage(cfg):
    path = cfg.data_dir() / "homehub.db"
    try:
        store = Storage(path)
    except Exception as e:
        return "fail", f"cannot open {path}: {e}"
    try:
        return "ok", f"schema v{store.version()}, {len(store.people())} people, {len(store.memory_facts())} memory facts, {len(store.reminders())} reminders in {path}"
    finally:
        store.close()


def check_claude():
    status, message = describe_auth(auth_status())
    leaked = [name for name in SECRET_ENV_VARS if os.environ.get(name)]
    if leaked and status == "ok":
        return "warn", message + f"; {', '.join(leaked)} is set in the environment and will be removed at startup"
    return status, message


def check_time():
    if not shutil.which("timedatectl"):
        return "warn", "timedatectl not available; cannot confirm the clock is synchronized"
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "warn", f"timedatectl failed: {e}"
    value = out.stdout.strip()
    if value == "yes":
        return "ok", f"clock synchronized (NTP), local time {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    if value == "no":
        return "fail", "clock is not synchronized; alarms and reminders cannot be trusted until NTP works"
    return "warn", f"unexpected timedatectl output {value!r}"


def check_disk(cfg):
    target = cfg.data_dir()
    while not target.exists() and target.parent != target:
        target = target.parent
    usage = shutil.disk_usage(target)
    free_gb = usage.free / (1 << 30)
    if usage.free < MIN_FREE_BYTES:
        return "fail", f"only {free_gb:.1f} GB free on {target}; the database and logs need at least 1 GB"
    return "ok", f"{free_gb:.1f} GB free on {target}"


def check_instance(cfg):
    state_dir = cfg.state_dir()
    pid = InstanceLock.holder_pid(state_dir)
    status = StatusFile.read(state_dir)
    if pid:
        return "ok", f"assistant running (pid {pid}), state file updated {status['updated'] if status else 'never'}"
    return "ok", f"assistant not running; state directory {state_dir}"


def check_kdeconnect(cfg):
    if not cfg.features.kdeconnect:
        return "skip", "feature off in config"
    cli = shutil.which("kdeconnect-cli")
    if not cli:
        return "fail", "kdeconnect-cli not installed; on the hub run: sudo apt install kdeconnect"
    try:
        out = subprocess.run([cli, "-l"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "fail", f"kdeconnect-cli failed: {e}"
    if out.returncode != 0:
        return "fail", f"kdeconnectd not reachable: {(out.stderr or out.stdout).strip()}"
    return "ok", (out.stdout.strip().splitlines() or ["no devices"])[-1]


def check_backup(cfg):
    from homehub.backup import newest_backup_age

    newest, age = newest_backup_age(cfg.backup.dir)
    if newest is None:
        return "warn", f"no backup yet in {cfg.backup.dir}; run 'homehub backup' or install the nightly timer"
    hours = age / 3600
    if hours > 48:
        return "warn", f"newest backup {newest.name} is {hours / 24:.1f} days old; the nightly timer may not be running"
    return "ok", f"newest backup {newest.name}, {hours:.1f} h old" + (f", also copied to {cfg.backup.copy_to}" if cfg.backup.copy_to else "")


def check_service():
    if not shutil.which("systemctl"):
        return "skip", "no systemd on this machine"
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", "homehub"], capture_output=True, text=True, timeout=10)
        timer = subprocess.run(["systemctl", "--user", "is-active", "homehub-backup.timer"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "skip", f"systemctl unavailable: {e}"
    state, timer_state = out.stdout.strip(), timer.stdout.strip()
    if "not been booted" in (out.stderr or "") or "Failed to connect" in (out.stderr or ""):
        return "skip", "systemd is not running here"
    if state == "active":
        return "ok", f"homehub service active; backup timer {timer_state or 'not installed'}"
    if state in ("failed", "activating", "deactivating"):
        return "warn", f"homehub service is {state}; backup timer {timer_state or 'not installed'}"
    return "skip", "service not installed (run scripts/install_service.sh on the hub)"


def run_checks(cfg):
    checks = [
        ("config", lambda: ("ok", f"loaded {cfg.source}")),
        ("household", lambda: check_household(cfg)),
        ("storage", lambda: check_storage(cfg)),
        ("claude", check_claude),
        ("time", check_time),
        ("disk", lambda: check_disk(cfg)),
        ("instance", lambda: check_instance(cfg)),
        ("kdeconnect", lambda: check_kdeconnect(cfg)),
        ("service", check_service),
        ("backup", lambda: check_backup(cfg)),
    ]
    results = {}
    for name, check in checks:
        try:
            results[name] = check()
        except Exception as e:
            results[name] = ("fail", f"{type(e).__name__}: {e}")
    return results


def format_results(results):
    lines = [f"{status.upper():<5} {name}: {message}" for name, (status, message) in results.items()]
    failures = sum(1 for status, _ in results.values() if status == "fail")
    warnings = sum(1 for status, _ in results.values() if status == "warn")
    if failures:
        lines.append(f"{failures} problem(s), {warnings} warning(s)")
    elif warnings:
        lines.append(f"no problems, {warnings} warning(s)")
    else:
        lines.append("all checks passed")
    return "\n".join(lines), failures
