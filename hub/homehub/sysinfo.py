import os
import shutil
import socket
import threading
import time
from pathlib import Path

PROC = Path("/proc")
SYS = Path("/sys/class")
SKIP_INTERFACES = ("lo",)


def read(path, default=None):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return default


def number(path, scale=1.0):
    text = read(path)
    try:
        return float(text) / scale
    except (TypeError, ValueError):
        return None


def first_number(text):
    try:
        return float(text.split()[0])
    except (AttributeError, IndexError, ValueError):
        return None


def cpu_times():
    parts = (read(PROC / "stat", "") or "").splitlines()[0].split()[1:]
    values = [int(v) for v in parts]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def meminfo():
    info = {}
    for line in (read(PROC / "meminfo", "") or "").splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields:
            info[key] = int(fields[0]) * 1024
    return info


def net_counters():
    counters = {}
    for line in (read(PROC / "net" / "dev", "") or "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        fields = rest.split()
        if name and name not in SKIP_INTERFACES and len(fields) >= 9:
            counters[name] = (int(fields[0]), int(fields[8]))
    return counters


def hwmon(name):
    for entry in sorted(SYS.joinpath("hwmon").glob("hwmon*")):
        if read(entry / "name") == name:
            return entry
    return None


def temperature(name):
    entry = hwmon(name)
    return round(number(entry / "temp1_input", 1000.0), 1) if entry is not None and number(entry / "temp1_input") is not None else None


def gpu():
    for card in sorted(SYS.joinpath("drm").glob("card[0-9]")):
        busy = number(card / "device" / "gpu_busy_percent")
        if busy is None:
            continue
        used, total = number(card / "device" / "mem_info_vram_used"), number(card / "device" / "mem_info_vram_total")
        return {"percent": round(busy), "memory_used": int(used) if used is not None else None,
                "memory_total": int(total) if total is not None else None, "temp_c": temperature("amdgpu")}
    return None


def wifi_signal(interface):
    for line in (read(PROC / "net" / "wireless", "") or "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        fields = rest.split()
        if name.strip() == interface and len(fields) >= 3:
            try:
                return round(float(fields[2].rstrip(".")))
            except ValueError:
                return None
    return None


def default_interface():
    for line in (read(PROC / "net" / "route", "") or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000":
            return fields[0]
    return None


def own_interface(name):
    return (SYS / "net" / name / "device").exists() or name.startswith("wg")


def interface_up(name):
    return read(SYS / "net" / name / "operstate") in ("up", "unknown")


def port_open(host, port, timeout=0.3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class SystemStats:
    def __init__(self, disk_path="/"):
        self.disk_path = disk_path
        self.lock = threading.Lock()
        self.last = None

    def sample(self):
        now = time.monotonic()
        total, idle = cpu_times()
        net = net_counters()
        with self.lock:
            previous, self.last = self.last, (now, total, idle, net)
        cpu_percent, rates = None, {}
        if previous is not None and now > previous[0]:
            elapsed = now - previous[0]
            spent, rested = total - previous[1], idle - previous[2]
            cpu_percent = round(100.0 * (spent - rested) / spent, 1) if spent > 0 else 0.0
            for name, (rx, tx) in net.items():
                if name in previous[3]:
                    old_rx, old_tx = previous[3][name]
                    rates[name] = (max(0, rx - old_rx) / elapsed, max(0, tx - old_tx) / elapsed)
        mem = meminfo()
        disk = shutil.disk_usage(self.disk_path)
        uptime = first_number(read(PROC / "uptime")) or 0.0
        load = os.getloadavg()
        internet = default_interface()
        return {
            "uptime_seconds": round(uptime),
            "booted_at": round(time.time() - uptime),
            "cpu": {"percent": cpu_percent, "cores": os.cpu_count(), "load": [round(v, 2) for v in load],
                    "temp_c": temperature("k10temp") or temperature("coretemp")},
            "gpu": gpu(),
            "memory": {"used": mem.get("MemTotal", 0) - mem.get("MemAvailable", 0), "total": mem.get("MemTotal", 0)},
            "disk": {"used": disk.used, "total": disk.total, "temp_c": temperature("nvme")},
            "network": [{"name": name, "up": interface_up(name), "internet": name == internet, "rx_total": rx, "tx_total": tx,
                         "rx_rate": round(rates[name][0]) if name in rates else None,
                         "tx_rate": round(rates[name][1]) if name in rates else None,
                         "signal_dbm": wifi_signal(name)}
                        for name, (rx, tx) in sorted(net.items(), key=lambda n: (n[0] != internet, n[0]))
                        if (own_interface(name) or name == internet) and interface_up(name)],
        }
