import base64
import json
import os
import platform
import re
import subprocess
import tempfile
import time
from pathlib import Path

from homehub.pc_agent import AGENT_VERSION

IS_WINDOWS = platform.system() == "Windows"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
SCREENSHOT_MAX_SIDE = 1568
NOTICE_SECONDS = 2.5
READ_ONLY = ("agent.ping", "agent.info", "diagnostics.summary", "disk.health", "crashes.recent", "updates.pending", "startup.list", "screen.capture")
FIXES = ("fix.restore_point", "fix.clear_temp", "fix.sfc", "fix.dism", "fix.restart_service", "fix.disable_startup", "fix.reboot", "fix.cancel_reboot")
OPERATIONS = READ_ONLY + FIXES
STARTED = time.time()


class OpError(Exception):
    pass


def powershell(command, timeout=60):
    if not IS_WINDOWS:
        raise OpError("this operation needs Windows")
    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True,
                         timeout=timeout, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if out.returncode != 0 and not out.stdout.strip():
        raise OpError((out.stderr or "").strip()[:500] or f"PowerShell exited {out.returncode}")
    return out.stdout


def ps_json(command, timeout=60):
    raw = powershell(command, timeout).strip()
    if not raw:
        return []
    data = json.loads(raw)
    return data if isinstance(data, list) else [data]


def agent_ping(args):
    return {"pong": True, "time": time.time()}


def agent_info(args):
    return {"name": platform.node(), "system": platform.system(), "release": platform.release(), "machine": platform.machine(),
            "version": f"homehub-agent {AGENT_VERSION}", "uptime_seconds": round(time.time() - STARTED, 1), "operations": list(OPERATIONS)}


def diagnostics_summary(args):
    os_info = ps_json("Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, @{n='LastBootUpTime';e={$_.LastBootUpTime.ToString('yyyy-MM-dd HH:mm')}}, TotalVisibleMemorySize, FreePhysicalMemory | ConvertTo-Json -Compress")[0]
    cpu = ps_json("Get-CimInstance Win32_Processor | Select-Object Name, LoadPercentage, NumberOfLogicalProcessors | ConvertTo-Json -Compress")[0]
    disks = ps_json("Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select-Object DeviceID, Size, FreeSpace | ConvertTo-Json -Compress")
    top = ps_json("Get-Process | Sort-Object CPU -Descending | Select-Object -First 8 Name, CPU, @{n='MemoryMB';e={[math]::Round($_.WorkingSet64/1MB)}} | ConvertTo-Json -Compress")
    boot = str(os_info.get("LastBootUpTime", ""))
    total_mb = int(os_info.get("TotalVisibleMemorySize") or 0) // 1024
    free_mb = int(os_info.get("FreePhysicalMemory") or 0) // 1024
    return {"os": f"{os_info.get('Caption', '').strip()} {os_info.get('Version', '')}".strip(), "last_boot": boot,
            "cpu": {"name": (cpu.get("Name") or "").strip(), "load_percent": cpu.get("LoadPercentage"), "cores": cpu.get("NumberOfLogicalProcessors")},
            "memory": {"total_mb": total_mb, "free_mb": free_mb, "used_percent": round(100 * (1 - free_mb / total_mb)) if total_mb else None},
            "disks": [{"drive": d.get("DeviceID"), "size_gb": round(int(d.get("Size") or 0) / 1e9, 1), "free_gb": round(int(d.get("FreeSpace") or 0) / 1e9, 1),
                       "free_percent": round(100 * int(d.get("FreeSpace") or 0) / int(d.get("Size") or 1))} for d in disks],
            "top_processes": [{"name": p.get("Name"), "cpu_seconds": round(float(p.get("CPU") or 0), 1), "memory_mb": p.get("MemoryMB")} for p in top]}


def disk_health(args):
    disks = ps_json("Get-PhysicalDisk | Select-Object FriendlyName, MediaType, HealthStatus, OperationalStatus, @{n='SizeGB';e={[math]::Round($_.Size/1GB)}} | ConvertTo-Json -Compress")
    reliability = []
    try:
        reliability = ps_json("Get-PhysicalDisk | Get-StorageReliabilityCounter | Select-Object DeviceId, Temperature, Wear, ReadErrorsTotal, WriteErrorsTotal, PowerOnHours | ConvertTo-Json -Compress")
    except (OpError, json.JSONDecodeError):
        reliability = []
    return {"disks": disks, "reliability": reliability}


def crashes_recent(args):
    days = int(args.get("days") or 7)
    events = ps_json("$ErrorActionPreference = 'SilentlyContinue'; "
                     f"$e = @(Get-WinEvent -FilterHashtable @{{LogName='System'; StartTime=(Get-Date).AddDays(-{days}); Id=41,1001,6008}}); "
                     "if ($e.Count -eq 0) { '[]' } else { $e | Select-Object -First 20 TimeCreated, Id, ProviderName, "
                     "@{n='Message';e={$_.Message.Substring(0, [math]::Min(300, $_.Message.Length))}} | ConvertTo-Json -Compress }")
    kinds = {41: "unexpected power loss or hard reset (Kernel-Power 41)", 1001: "blue screen (BugCheck)", 6008: "unexpected shutdown"}
    return {"days": days, "count": len(events), "events": [{"time": str(e.get("TimeCreated")), "kind": kinds.get(int(e.get("Id") or 0), str(e.get("Id"))),
                                                               "source": e.get("ProviderName"), "message": e.get("Message")} for e in events]}


def updates_pending(args):
    raw = powershell("$s = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher(); $r = $s.Search('IsInstalled=0 and IsHidden=0'); "
                     "$r.Updates | Select-Object Title, IsMandatory, @{n='SizeMB';e={[math]::Round($_.MaxDownloadSize/1MB)}} | ConvertTo-Json -Compress", timeout=240).strip()
    items = json.loads(raw) if raw else []
    items = items if isinstance(items, list) else [items]
    return {"count": len(items), "updates": [{"title": u.get("Title"), "mandatory": bool(u.get("IsMandatory")), "size_mb": u.get("SizeMB")} for u in items]}


def startup_list(args):
    items = ps_json("Get-CimInstance Win32_StartupCommand | Select-Object Name, Command, Location, User | ConvertTo-Json -Compress")
    return {"count": len(items), "items": [{"name": i.get("Name"), "command": i.get("Command"), "location": i.get("Location"), "user": i.get("User")} for i in items]}


def powershell_file(script, arguments, timeout=60):
    if not IS_WINDOWS:
        raise OpError("this operation needs Windows")
    path = str(Path(__file__).with_name(script))
    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path, *arguments],
                         capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if out.returncode != 0 and not out.stdout.strip():
        raise OpError((out.stderr or "").strip()[:500] or f"PowerShell exited {out.returncode}")
    return out.stdout


def show_notice(text, seconds=NOTICE_SECONDS):
    try:
        powershell_file("notice.ps1", ["-Text", text, "-Ms", str(int(seconds * 1000))], timeout=seconds + 20)
    except (OpError, subprocess.TimeoutExpired):
        return


def screen_capture(args):
    if not IS_WINDOWS:
        raise OpError("this operation needs Windows")
    if args.get("notice", True):
        show_notice("Home Hub is taking a look at this screen to help.")
    handle, path = tempfile.mkstemp(prefix="homehub-screen-", suffix=".jpg")
    os.close(handle)
    try:
        info = json.loads(powershell_file("capture.ps1", ["-Out", path, "-MaxSide", str(SCREENSHOT_MAX_SIDE)], timeout=60).strip())
        data = Path(path).read_bytes()
    finally:
        Path(path).unlink(missing_ok=True)
    return {"width": info["width"], "height": info["height"], "scale": info["scale"], "jpeg_base64": base64.b64encode(data).decode()}


def fix_restore_point(args):
    description = str(args.get("description") or "Home Hub before fix")[:60].replace('"', "")
    out = powershell(f'Checkpoint-Computer -Description "{description}" -RestorePointType MODIFY_SETTINGS; "ok"', timeout=600)
    return {"created": "ok" in out, "description": description, "output": out.strip()[-300:]}


def fix_clear_temp(args):
    removed_bytes, removed_files, failures = 0, 0, 0
    for folder in {tempfile.gettempdir(), os.path.expandvars(r"%SystemRoot%\Temp")}:
        root = Path(folder)
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            try:
                if path.is_file() and time.time() - path.stat().st_mtime > 3600:
                    size = path.stat().st_size
                    path.unlink()
                    removed_bytes += size
                    removed_files += 1
            except OSError:
                failures += 1
    return {"removed_files": removed_files, "removed_mb": round(removed_bytes / 1e6, 1), "skipped_in_use": failures}


def fix_sfc(args):
    out = powershell("sfc /scannow", timeout=3600)
    text = out.replace("\x00", "").strip()
    return {"output": text[-600:], "found_problems": "found corrupt files" in text.lower(), "repaired": "successfully repaired" in text.lower()}


def fix_dism(args):
    out = powershell("DISM /Online /Cleanup-Image /RestoreHealth", timeout=3600)
    return {"output": out.strip()[-600:], "completed": "completed successfully" in out.lower()}


def fix_restart_service(args):
    name = str(args.get("name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\- ]{1,80}", name):
        raise OpError("give the service's name")
    out = powershell(f'Restart-Service -Name "{name}" -Force -ErrorAction Stop; (Get-Service -Name "{name}").Status', timeout=120)
    return {"service": name, "status": out.strip().splitlines()[-1] if out.strip() else "unknown"}


def fix_disable_startup(args):
    name = str(args.get("name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\- ()]{1,80}", name):
        raise OpError("give the startup item's name")
    out = powershell("$k='HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run'; "
                     f'if (Get-ItemProperty -Path $k -Name "{name}" -ErrorAction SilentlyContinue) {{ Remove-ItemProperty -Path $k -Name "{name}"; "removed" }} else {{ "not found" }}', timeout=60)
    return {"item": name, "result": out.strip()}


def fix_reboot(args):
    seconds = max(30, min(600, int(args.get("seconds") or 60)))
    show_notice(f"Home Hub is restarting this computer in {seconds} seconds. Save your work.", seconds=4)
    out = powershell(f'shutdown /r /t {seconds} /c "Home Hub is restarting this computer to finish a fix"; "scheduled"', timeout=30)
    return {"seconds": seconds, "scheduled": "scheduled" in out}


def fix_cancel_reboot(args):
    powershell('shutdown /a; "cancelled"', timeout=30)
    return {"cancelled": True}


HANDLERS = {"agent.ping": agent_ping, "agent.info": agent_info, "diagnostics.summary": diagnostics_summary, "disk.health": disk_health,
            "crashes.recent": crashes_recent, "updates.pending": updates_pending, "startup.list": startup_list, "screen.capture": screen_capture,
            "fix.restore_point": fix_restore_point, "fix.clear_temp": fix_clear_temp, "fix.sfc": fix_sfc, "fix.dism": fix_dism,
            "fix.restart_service": fix_restart_service, "fix.disable_startup": fix_disable_startup, "fix.reboot": fix_reboot,
            "fix.cancel_reboot": fix_cancel_reboot}


def run_operation(name, args):
    handler = HANDLERS.get(name)
    if handler is None:
        raise KeyError(name)
    return handler(args or {})
