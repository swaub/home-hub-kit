import json
import os
import sys
from datetime import datetime
from pathlib import Path

from homehub.version import describe

LOCK_FILE = "lock"
STATUS_FILE = "status.json"


class AlreadyRunning(RuntimeError):
    pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def process_alive(pid):
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def try_lock(fd):
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            raise BlockingIOError("locked") from None
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock(fd):
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


class InstanceLock:
    def __init__(self, state_dir):
        self.path = Path(state_dir).expanduser() / LOCK_FILE
        self.fd = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try_lock(fd)
        except BlockingIOError:
            holder = self.holder_pid(self.path.parent)
            os.close(fd)
            raise AlreadyRunning(f"another homehub is already running (pid {holder})" if holder else "another homehub is already running") from None
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid():<16}\n".encode())
        os.fsync(fd)
        self.fd = fd
        return self

    def release(self):
        if self.fd is not None:
            unlock(self.fd)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()

    @staticmethod
    def holder_pid(state_dir):
        path = Path(state_dir).expanduser() / LOCK_FILE
        try:
            fd = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            return None
        try:
            try:
                try_lock(fd)
            except BlockingIOError:
                os.lseek(fd, 0, os.SEEK_SET)
                text = os.read(fd, 32).decode(errors="replace").strip()
                return int(text) if text.isdigit() else -1
            unlock(fd)
            return None
        finally:
            os.close(fd)


class StatusFile:
    def __init__(self, state_dir):
        self.path = Path(state_dir).expanduser() / STATUS_FILE
        self.started = now_iso()
        self.version = describe()

    def write(self, **fields):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"pid": os.getpid(), "started": self.started, "updated": now_iso(), "version": self.version, **fields}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, self.path)
        return data

    @staticmethod
    def read(state_dir):
        path = Path(state_dir).expanduser() / STATUS_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        data["alive"] = process_alive(data.get("pid"))
        return data
