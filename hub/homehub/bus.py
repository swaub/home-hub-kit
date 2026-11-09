import asyncio
import inspect
import logging
import re
import threading
from collections import deque
from datetime import datetime

from homehub.logging_setup import log_event

log = logging.getLogger("homehub.bus")
EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
PATTERN = re.compile(r"^(\*|[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*(\.\*)?)$")
DROPPABLE_PREFIX = "audio."
RESERVED_KEYS = ("name", "ts")


class Subscription:
    def __init__(self, pattern, handler, max_queue):
        self.pattern = pattern
        self.handler = handler
        self.max_queue = max_queue
        self.queue = deque()
        self.droppable = 0
        self.wakeup = asyncio.Event()
        self.busy = False
        self.delivered = 0
        self.dropped = 0
        self.errors = 0
        self.task = None

    def matches(self, name):
        if self.pattern == "*":
            return True
        if self.pattern.endswith(".*"):
            return name.startswith(self.pattern[:-1])
        return name == self.pattern

    def offer(self, event):
        if len(self.queue) >= self.max_queue and self.droppable:
            for i, queued in enumerate(self.queue):
                if queued["name"].startswith(DROPPABLE_PREFIX):
                    del self.queue[i]
                    self.droppable -= 1
                    self.dropped += 1
                    break
        self.queue.append(event)
        if event["name"].startswith(DROPPABLE_PREFIX):
            self.droppable += 1
        self.wakeup.set()

    @property
    def pending(self):
        return len(self.queue) + (1 if self.busy else 0)

    async def run(self):
        while True:
            await self.wakeup.wait()
            self.wakeup.clear()
            while self.queue:
                event = self.queue.popleft()
                if event["name"].startswith(DROPPABLE_PREFIX):
                    self.droppable -= 1
                self.busy = True
                try:
                    result = self.handler(event)
                    if inspect.isawaitable(result):
                        await result
                    self.delivered += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.errors += 1
                    log.exception("subscriber %s failed on %s", getattr(self.handler, "__name__", self.handler), event["name"])
                finally:
                    self.busy = False


class Bus:
    def __init__(self):
        self.loop = None
        self.thread_id = None
        self.subscriptions = []
        self.published = 0

    def start(self, loop=None):
        self.loop = loop or asyncio.get_running_loop()
        self.thread_id = threading.get_ident()
        return self

    def subscribe(self, pattern, handler, max_queue=1000):
        if self.loop is None:
            raise RuntimeError("bus.start() must run inside the event loop before subscribing")
        if not PATTERN.match(pattern):
            raise ValueError(f"bad subscription pattern {pattern!r}: use an event name, a prefix like speech.*, or *")
        sub = Subscription(pattern, handler, max_queue)
        sub.task = self.loop.create_task(sub.run(), name=f"bus:{pattern}")
        self.subscriptions.append(sub)
        return sub

    def unsubscribe(self, sub):
        if sub in self.subscriptions:
            self.subscriptions.remove(sub)
            sub.task.cancel()

    def publish(self, name, /, **payload):
        if not EVENT_NAME.match(name):
            raise ValueError(f"bad event name {name!r}: use lowercase dotted names like speech.final")
        for key in RESERVED_KEYS:
            if key in payload:
                raise ValueError(f"payload key {key!r} is reserved")
        if self.loop is None:
            raise RuntimeError("bus.start() must run inside the event loop before publishing")
        event = {"name": name, "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"), **payload}
        self.published += 1
        if threading.get_ident() == self.thread_id:
            self._dispatch(event)
        else:
            self.loop.call_soon_threadsafe(self._dispatch, event)
        return event

    def _dispatch(self, event):
        for sub in list(self.subscriptions):
            if sub.matches(event["name"]):
                sub.offer(event)

    async def drain(self, timeout=5.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        await asyncio.sleep(0)
        while any(sub.pending for sub in self.subscriptions):
            if loop.time() > deadline:
                raise TimeoutError("bus subscribers did not finish in time")
            await asyncio.sleep(0.001)

    async def close(self):
        subs, self.subscriptions = self.subscriptions, []
        for sub in subs:
            sub.task.cancel()
        await asyncio.gather(*(sub.task for sub in subs), return_exceptions=True)

    def attach_logging(self):
        return self.subscribe("*", lambda event: log_event(event["name"], {k: v for k, v in event.items() if k not in RESERVED_KEYS}))
