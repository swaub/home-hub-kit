import subprocess
import time
from pathlib import Path

import pytest

from homehub import power

VIRTUAL = "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower() if Path("/proc/sys/kernel/osrelease").exists() else True

def test_turn_on_times_must_leave_room_to_shut_down_and_stay_within_a_month():
    now = 1_800_000_000
    assert power.check_wake(now + 3600, now) == now + 3600
    assert power.check_wake(str(now + 600), now) == now + 600
    for bad in (now + 60, now + 31 * 86400, "soon", None):
        with pytest.raises(power.PowerError):
            power.check_wake(bad, now)
    assert power.when(int(time.mktime((2026, 11, 5, 7, 0, 0, 0, 0, -1)))) == "Thu Nov 5 at 7:00 AM"


@pytest.mark.skipif(VIRTUAL or not power.RTC_ALARM.exists() or not power.sudo_allows(power.RTCWAKE, "-m", "show"),
                    reason="needs a real hardware clock (WSL's can't hold an alarm) and sudo")
def test_the_hubs_real_clock_takes_a_wake_time_and_lets_it_go():
    before = power.wake_alarm()
    target = int(time.time()) + 2 * 86400
    try:
        power.set_wake(target)
        assert abs(power.wake_alarm() - target) <= power.ALARM_SLACK
        shown = subprocess.run(["sudo", "-n", power.RTCWAKE, "-m", "show"], capture_output=True, text=True, timeout=15).stdout
        assert "alarm: on" in shown
        power.clear_wake()
        assert power.wake_alarm() is None
    finally:
        if before is not None:
            power.set_wake(before)
        else:
            power.clear_wake()
