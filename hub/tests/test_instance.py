import json
import os
import subprocess
import sys

import pytest

from homehub.instance import AlreadyRunning, InstanceLock, StatusFile, process_alive

HOLD = ("import sys, time\nfrom homehub.instance import InstanceLock, AlreadyRunning\n"
        "try:\n    lock = InstanceLock(sys.argv[1]).acquire()\nexcept AlreadyRunning as e:\n    print(e); sys.exit(7)\n"
        "print('held', flush=True)\ntime.sleep(float(sys.argv[2]))\nlock.release()\n")


def test_lock_is_exclusive_across_processes(tmp_path):
    with InstanceLock(tmp_path) as lock:
        assert lock.fd is not None
        assert (tmp_path / "lock").read_text().strip() == str(os.getpid())
        assert InstanceLock.holder_pid(tmp_path) == os.getpid()
        out = subprocess.run([sys.executable, "-c", HOLD, str(tmp_path), "0"], capture_output=True, text=True, timeout=60)
        assert out.returncode == 7
        assert out.stdout.strip() == f"another homehub is already running (pid {os.getpid()})"
    assert InstanceLock.holder_pid(tmp_path) is None
    out = subprocess.run([sys.executable, "-c", HOLD, str(tmp_path), "0"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "held"


def test_second_acquire_in_this_process_sees_the_holder(tmp_path):
    holder = subprocess.Popen([sys.executable, "-c", HOLD, str(tmp_path), "20"], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(AlreadyRunning, match=f"pid {holder.pid}"):
            InstanceLock(tmp_path).acquire()
        assert InstanceLock.holder_pid(tmp_path) == holder.pid
    finally:
        holder.kill()
        holder.wait(timeout=10)
    assert InstanceLock.holder_pid(tmp_path) is None


def test_holder_pid_without_a_lock_file(tmp_path):
    assert InstanceLock.holder_pid(tmp_path / "nowhere") is None


def test_status_file_round_trip_and_liveness(tmp_path):
    status = StatusFile(tmp_path / "state")
    data = status.write(state={"bedroom": "waiting"}, mic={"bedroom": "peak 0.03 gain 4.1"}, last_error=None)
    assert data["pid"] == os.getpid() and data["started"] == status.started
    read = StatusFile.read(tmp_path / "state")
    assert read["state"] == {"bedroom": "waiting"} and read["alive"] is True
    assert read["version"].startswith("homehub ")
    assert not list((tmp_path / "state").glob("*.tmp"))
    assert StatusFile.read(tmp_path / "missing") is None
    (tmp_path / "state" / "status.json").write_text("{not json", encoding="utf-8")
    assert StatusFile.read(tmp_path / "state") is None


def test_process_alive_for_an_exited_process():
    proc = subprocess.run([sys.executable, "-c", "pass"], capture_output=True)
    assert proc.returncode == 0
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    assert process_alive(os.getpid()) is True
    assert process_alive(child.pid) is False
    assert process_alive(0) is False and process_alive(None) is False


def test_status_write_is_atomic_json(tmp_path):
    status = StatusFile(tmp_path)
    for i in range(50):
        status.write(state={"bedroom": f"turn {i}"})
    assert json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))["state"] == {"bedroom": "turn 49"}
