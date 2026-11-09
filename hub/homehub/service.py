import os
import socket
from collections import deque


class Notifier:
    def __init__(self, address=None, watchdog_usec=None):
        self.address = os.environ.get("NOTIFY_SOCKET", "") if address is None else (address or "")
        usec = os.environ.get("WATCHDOG_USEC") if watchdog_usec is None else watchdog_usec
        self.watchdog_seconds = int(usec) / 1e6 if usec else None
        self.sent = deque(maxlen=20)
        self.failures = 0

    @property
    def enabled(self):
        return bool(self.address)

    def interval(self, default=20.0):
        if self.watchdog_seconds:
            return max(1.0, min(default, self.watchdog_seconds / 2))
        return default

    def send(self, *lines):
        if not self.enabled:
            return False
        payload = "\n".join(lines).encode()
        address = "\0" + self.address[1:] if self.address.startswith("@") else self.address
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.connect(address)
                sock.send(payload)
        except OSError:
            self.failures += 1
            return False
        self.sent.append(payload.decode())
        return True

    def ready(self, message=""):
        return self.send("READY=1", *([f"STATUS={message[:200]}"] if message else []))

    def watchdog(self):
        return self.send("WATCHDOG=1")

    def status(self, text):
        return self.send(f"STATUS={text[:200]}")

    def stopping(self):
        return self.send("STOPPING=1")
