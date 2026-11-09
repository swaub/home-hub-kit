import collections
import re
import threading
import time
from datetime import datetime, timedelta

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, current_context, text_result

MISSED_GRACE = timedelta(hours=12)
MISSED_AFTER = timedelta(seconds=90)
MAX_TIMER_SECONDS = 7 * 86400
REPEATS = ("daily", "weekdays")
PROMPT = ("You can set timers, reminders and alarms; when one is due the hub pushes it to the phone through the notification app "
          "and shows it in the app, even after this conversation ends. Work out seconds or the clock time yourself from the current time. "
          "A reminder for someone else in the household (for_person) is pushed with their name on it.")


def spoken_time(when, now=None):
    now = now or datetime.now()
    clock = when.strftime("%I:%M %p").lstrip("0")
    if when.date() == now.date():
        day = "today"
    elif when.date() == (now + timedelta(days=1)).date():
        day = "tomorrow"
    else:
        day = when.strftime("%A %B %d")
    seconds = (when - now).total_seconds()
    if 0 <= seconds < 3600:
        minutes, secs = divmod(int(round(seconds)), 60)
        left = f"{minutes} min {secs} s" if minutes else f"{secs} s"
        return f"{clock} {day} (in {left})"
    return f"{clock} {day}"


def next_repeat(when, repeat):
    nxt = when + timedelta(days=1)
    if repeat == "weekdays":
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
    return nxt


def parse_clock(text, now=None):
    now = now or datetime.now()
    text = text.strip().lower().replace(".", "")
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if not m:
        raise ValueError(f"Couldn't understand the time '{text}'. Use HH:MM (24-hour), '7:30 am', or an ISO date-time.")
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if half == "pm" and hour < 12:
        hour += 12
    if half == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError(f"'{text}' isn't a valid time.")
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= now:
        when += timedelta(days=1)
    return when


def due_of(item):
    return datetime.fromisoformat(item["due"])


def describe(item, now=None):
    label = f" '{item['label']}'" if item["label"] else ""
    repeat = f", repeats {item['repeat']}" if item.get("repeat") else ""
    return f"{item['kind']}{label} at {spoken_time(due_of(item), now)}{repeat} [id {item['id']}]"


def announcement(item, now=None):
    now = now or datetime.now()
    label = (item.get("label") or "").strip().rstrip(".")
    if item["kind"] == "timer":
        return f"Your {label} timer is done." if label else "Your timer is done."
    if item["kind"] == "reminder":
        return ("You missed a reminder earlier: " if item.get("missed") else "Reminder: ") + (label or "this is your reminder") + "."
    clock = now.strftime("%I:%M %p").lstrip("0")
    return f"It's {clock}. " + (f"{label}." if label else "Time to wake up.")


class Scheduler:
    def __init__(self, storage, emit=None, on_due=None, poll_seconds=0.5):
        self.storage = storage
        self.emit = emit
        self.on_due = on_due
        self.poll_seconds = poll_seconds
        self.rang = collections.deque(maxlen=20)
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = None

    def publish(self, name, **payload):
        if self.emit is not None:
            self.emit(name, **payload)

    def start(self):
        if self.thread is None or not self.thread.is_alive():
            self.stopped.clear()
            self.thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
            self.thread.start()
        return self

    def stop(self):
        self.stopped.set()
        if self.thread is not None and self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=2.0)

    def add(self, kind, due, label="", repeat=None, owner_id=None, action="added"):
        item = self.storage.add_reminder(kind, due, label, repeat if repeat in REPEATS else None, owner_id, None, True, at_home=False)
        self.publish("reminder.changed", id=item["id"], action=action, kind=kind, label=item["label"], due=item["due"])
        return item

    def upcoming(self, owner_id=None):
        return self.storage.reminders(owner_id)

    def cancel(self, which, owner_id=None):
        removed = self.storage.cancel_reminders(which, owner_id)
        for item in removed:
            self.publish("reminder.changed", id=item["id"], action="cancelled", kind=item["kind"], label=item["label"], due=item["due"])
        return removed

    def due_now(self, now=None):
        now = now or datetime.now()
        fired = []
        with self.lock:
            for item in self.storage.reminders_due(now):
                due = due_of(item)
                missed = now - due > MISSED_AFTER
                stale = missed and (item["kind"] == "timer" or now - due > MISSED_GRACE)
                if not stale:
                    fired.append({**item, "missed": missed})
                if item.get("repeat"):
                    while due <= now:
                        due = next_repeat(due, item["repeat"])
                    self.storage.reschedule_reminder(item["id"], due)
                    self.publish("reminder.changed", id=item["id"], action="rescheduled", kind=item["kind"], label=item["label"],
                                 due=due.isoformat(timespec="seconds"))
                else:
                    self.storage.delete_reminder(item["id"])
        return fired

    def _run(self):
        while not self.stopped.is_set():
            try:
                due = self.due_now()
            except Exception as e:
                self.publish("service.warning", message=f"scheduler: {type(e).__name__}: {e}")
                due = []
            for item in due:
                threading.Thread(target=self._fire, args=(item,), name=f"due:{item['id']}", daemon=True).start()
            self.stopped.wait(self.poll_seconds)

    def _fire(self, item):
        with self.lock:
            self.rang.append({"at": time.time(), "kind": item["kind"], "label": item.get("label") or ""})
        self.publish("reminder.due", id=item["id"], kind=item["kind"], label=item["label"], owner_id=item.get("owner_id"),
                     missed=bool(item.get("missed")))
        try:
            if self.on_due is not None:
                self.on_due(item)
        except Exception as e:
            self.publish("service.warning", message=f"pushing {item['kind']} {item['id']}: {type(e).__name__}: {e}")

    def recent(self):
        with self.lock:
            return list(self.rang)


TITLES = {"timer": "Timer done", "alarm": "Alarm", "reminder": "Reminder"}


class Pusher:
    def __init__(self, ntfy, storage, emit=None):
        self.ntfy = ntfy
        self.storage = storage
        self.emit = emit

    def publish(self, name, **payload):
        if self.emit is not None:
            self.emit(name, **payload)

    def message(self, item):
        text = announcement(item)
        owner = self.storage.person(item["owner_id"]) if item.get("owner_id") else None
        if owner is not None and owner["role"] != "owner":
            text = f"For {owner['name']}: {text}"
        return text

    def push(self, item):
        text = self.message(item)
        self.publish("transcript.line", room="app", kind="action", text=f"{item['kind']} due: {text}")
        if not self.ntfy.enabled:
            return False
        self.ntfy.send(text, title=TITLES.get(item["kind"], "Reminder"), priority=5 if item["kind"] == "alarm" else 4, tags=("alarm_clock",))
        return True


def call_context():
    return current_context.get()


def target(storage, args):
    owner = getattr(call_context(), "person_id", None)
    name = (args.get("for_person") or "").strip()
    if not name:
        return owner, False, None
    person = storage.find_person(name)
    if person is None:
        return owner, False, f"I don't know anyone called {name} in this household."
    if person["id"] == owner:
        return owner, False, None
    return person["id"], True, None


def build_module(scheduler, storage):
    @tool("set_timer", "Start a countdown timer. seconds is the total length (e.g. 20 minutes = 1200). label is optional (e.g. 'pizza').",
          {"type": "object", "properties": {"seconds": {"type": "integer"}, "label": {"type": "string"}}, "required": ["seconds"]})
    async def set_timer(args):
        seconds = int(args["seconds"])
        if not 1 <= seconds <= MAX_TIMER_SECONDS:
            return text_result("Timers can be from 1 second to 7 days.", error=True)
        owner, _, _ = target(storage, {})
        item = scheduler.add("timer", datetime.now() + timedelta(seconds=seconds), args.get("label", ""), None, owner)
        return text_result(f"Timer set: {describe(item)}.")

    @tool("set_reminder", "Remind someone of something at a time. Give either in_seconds (relative, e.g. 'in 20 minutes' = 1200) "
          "or at (a clock time like '19:00' / '7 pm', or an ISO date-time like 2026-10-23T09:30 for another day). repeat can be "
          "'daily' or 'weekdays'. for_person is the household member it's for when it isn't the person asking (e.g. 'Mom').",
          {"type": "object", "properties": {"message": {"type": "string"}, "in_seconds": {"type": "integer"}, "at": {"type": "string"},
                                            "repeat": {"type": "string"}, "for_person": {"type": "string"}},
           "required": ["message"]})
    async def set_reminder(args):
        try:
            if args.get("in_seconds"):
                due = datetime.now() + timedelta(seconds=int(args["in_seconds"]))
            elif args.get("at"):
                due = parse_clock(args["at"])
            else:
                return text_result("Give either in_seconds or at.", error=True)
        except ValueError as e:
            return text_result(str(e), error=True)
        if due <= datetime.now():
            return text_result("That time has already passed.", error=True)
        owner, other, problem = target(storage, args)
        if problem:
            return text_result(problem, error=True)
        item = scheduler.add("reminder", due, args["message"], args.get("repeat"), owner)
        who = f" for {args['for_person'].strip()}" if other else ""
        return text_result(f"Reminder set{who}: {describe(item)}.")

    @tool("set_alarm", "Set an alarm at a clock time ('7:00', '6:30 am', '22:15', or an ISO date-time). It is pushed to the phone at "
          "that time. repeat can be 'daily' or 'weekdays'. label is optional.",
          {"type": "object", "properties": {"at": {"type": "string"}, "label": {"type": "string"}, "repeat": {"type": "string"}},
           "required": ["at"]})
    async def set_alarm(args):
        try:
            due = parse_clock(args["at"])
        except ValueError as e:
            return text_result(str(e), error=True)
        owner, _, _ = target(storage, {})
        item = scheduler.add("alarm", due, args.get("label", ""), args.get("repeat"), owner)
        return text_result(f"Alarm set: {describe(item)}.")

    @tool("list_timers", "List all pending timers, reminders and alarms with how long until each.", {"type": "object", "properties": {}})
    async def list_timers(args):
        items = scheduler.upcoming()
        if not items:
            return text_result("Nothing is scheduled.")
        return text_result("\n".join(describe(i) for i in items))

    @tool("cancel_timer", "Cancel timers, reminders or alarms. which is an id, part of a label (e.g. 'pizza'), a kind ('timer', 'reminder', "
          "'alarm'), or 'all'.", {"type": "object", "properties": {"which": {"type": "string"}}, "required": ["which"]})
    async def cancel_timer(args):
        removed = scheduler.cancel(args["which"])
        if not removed:
            return text_result(f"Nothing matched '{args['which']}'.", error=True)
        return text_result("Cancelled: " + "; ".join(describe(i) for i in removed))

    specs = [ToolSpec(fn=fn, policy="auto", scope=Scope.household()) for fn in (set_timer, set_reminder, set_alarm, list_timers, cancel_timer)]
    return ToolModule(server="time", tools=specs, prompt=PROMPT)
