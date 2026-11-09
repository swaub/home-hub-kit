import subprocess
import time
from datetime import datetime
from pathlib import Path

RTC_ALARM = Path("/sys/class/rtc/rtc0/wakealarm")
RTCWAKE = "/usr/sbin/rtcwake"
SOONEST_WAKE = 180
LATEST_WAKE_DAYS = 30
ALARM_SLACK = 2


class PowerError(Exception):
    pass


def sudo_allows(*command):
    try:
        return subprocess.run(["sudo", "-n", "-l", *command], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def wake_alarm():
    try:
        text = RTC_ALARM.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def check_wake(wake_at, now=None):
    now = time.time() if now is None else now
    try:
        wake_at = int(wake_at)
    except (TypeError, ValueError):
        raise PowerError("The turn-on time isn't a time") from None
    if wake_at < now + SOONEST_WAKE:
        raise PowerError("Pick a turn-on time at least 3 minutes from now, so it has time to shut down first")
    if wake_at > now + LATEST_WAKE_DAYS * 86400:
        raise PowerError(f"Pick a turn-on time within {LATEST_WAKE_DAYS} days")
    return wake_at


def set_wake(wake_at):
    if not sudo_allows(RTCWAKE, "-m", "no", "-u", "-t", str(wake_at)):
        raise PowerError("The hub doesn't allow the assistant to set its wake-up clock")
    done = subprocess.run(["sudo", "-n", RTCWAKE, "-m", "no", "-u", "-t", str(wake_at)], capture_output=True, text=True, timeout=15)
    stored = wake_alarm()
    if done.returncode != 0 or stored is None or abs(stored - wake_at) > ALARM_SLACK:
        raise PowerError("The hub's clock didn't take the wake-up time, so it stays on: " + (done.stderr.strip() or "the alarm reads back differently"))


def clear_wake():
    if wake_alarm() is None:
        return
    subprocess.run(["sudo", "-n", RTCWAKE, "-m", "disable"], capture_output=True, timeout=15)
    if wake_alarm() is not None:
        raise PowerError("The hub's old wake-up time couldn't be cleared")


def when(wake_at):
    moment = datetime.fromtimestamp(wake_at)
    return moment.strftime("%a %b %d at %I:%M %p").replace(" 0", " ")
