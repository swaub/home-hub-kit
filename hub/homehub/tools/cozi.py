import asyncio
import json
import re
import threading
import time
import tomllib
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

BASE = "https://rest.cozi.com/api/ext"
LOGIN = f"{BASE}/2207/auth/login?apikey=coziwc|v251_production"
CALENDAR = BASE + "/2004/{account}/calendar/{year}/{month}"
PEOPLE = BASE + "/2004/{account}/account/person/"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
           "Origin": "https://my.cozi.com", "Referer": "https://my.cozi.com/", "Accept": "application/json"}
TIMEOUT = 15
MAX_DAYS = 31
PROMPT = ("Cozi is the family's shared calendar. cozi_events reads it for a day or a span of days (today, tomorrow, this week, a date). "
          "cozi_add puts a new appointment on it after the person says yes; read back the title, day, time and who when asking. "
          "Use Cozi for the family calendar; their own Google Calendar is separate.")
NOT_SET_UP = "Cozi isn't set up: add [cozi] with username and password to secrets.toml in the data folder."


class CoziError(RuntimeError):
    pass


def load_login(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        table = tomllib.load(f).get("cozi") or {}
    if not table.get("username") or not table.get("password"):
        return None
    return {"username": str(table["username"]), "password": str(table["password"])}


def clock(text):
    hours, minutes = (int(x) for x in str(text).split(":")[:2])
    suffix = "AM" if hours < 12 else "PM"
    hours = hours % 12 or 12
    return f"{hours}:{minutes:02d} {suffix}" if minutes else f"{hours} {suffix}"


def all_day(item):
    return (item.get("startTime") or "00:00:00").startswith("00:00") and (item.get("endTime") or "00:00:00").startswith("00:00")


def day_entries(calendar, day):
    items = calendar.get("items") or {}
    found = []
    for ref in (calendar.get("days") or {}).get(day, []):
        key = ref.get("id", "")
        item = items.get(key) or next((v for k, v in items.items() if key.endswith(k) or k.endswith(key)), None)
        if item is not None:
            found.append(item)
    found.sort(key=lambda i: (not all_day(i), i.get("startTime") or ""))
    return found


def describe_entry(item):
    title = " ".join(str(item.get("description") or "untitled").split())
    if all_day(item):
        return f"all day: {title}"
    return f"{clock(item['startTime'])} to {clock(item['endTime'])}: {title}" if item.get("endTime") else f"{clock(item['startTime'])}: {title}"


def parse_day(text, today=None):
    today = today or date.today()
    text = str(text or "").strip().lower()
    if text in ("", "today"):
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    if text in days:
        return today + timedelta(days=(days.index(text) - today.weekday()) % 7)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise CoziError(f"I couldn't read {text!r} as a day; use a date like 2026-11-06, today, tomorrow or a weekday.") from None


def parse_time(text):
    found = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*", str(text or ""), re.I)
    if not found:
        raise CoziError(f"I couldn't read {text!r} as a time; use something like 15:30 or 3:30 pm.")
    hours, minutes = int(found.group(1)), int(found.group(2) or 0)
    if found.group(3):
        hours = hours % 12 + (12 if found.group(3).lower().startswith("p") else 0)
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise CoziError(f"{text!r} isn't a time of day.")
    return f"{hours:02d}:{minutes:02d}:00"


class Cozi:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.session = None

    def request(self, url, body=None, method="GET", token=None):
        headers = dict(HEADERS)
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and token:
                self.session = None
            raise CoziError(f"Cozi said {e.code}: {e.read().decode('utf-8', 'replace')[:160]}") from None

    def login(self):
        with self.lock:
            if self.session and time.time() < self.session["expires"]:
                return self.session
            account = load_login(self.cfg)
            if account is None:
                raise CoziError(NOT_SET_UP)
            reply = self.request(LOGIN, {"username": account["username"], "password": account["password"], "issueRefresh": True}, "POST")
            if not reply.get("accessToken"):
                raise CoziError("Cozi didn't accept the sign-in.")
            self.session = {"token": reply["accessToken"], "account": reply["accountId"],
                            "expires": time.time() + int(reply.get("expiresIn") or 3600) - 120}
            return self.session

    def call(self, url_template, body=None, method="GET", **parts):
        for attempt in (1, 2):
            session = self.login()
            try:
                return self.request(url_template.format(account=session["account"], **parts), body, method, session["token"])
            except CoziError:
                if attempt == 2 or self.session is not None:
                    raise

    def month(self, year, month):
        return self.call(CALENDAR, year=year, month=month)

    def people(self):
        return {p["name"]: p["accountPersonId"] for p in self.call(PEOPLE) if p.get("name")}

    def events(self, start, days):
        days = max(1, min(int(days), MAX_DAYS))
        months, lines = {}, []
        for offset in range(days):
            day = start + timedelta(days=offset)
            key = (day.year, day.month)
            if key not in months:
                months[key] = self.month(*key)
            entries = day_entries(months[key], day.isoformat())
            if entries:
                lines.append(f"{day:%a %b} {day.day}: " + "; ".join(describe_entry(e) for e in entries))
        span = f"{start:%a %b} {start.day}" + (f" to {start + timedelta(days=days - 1):%a %b} {(start + timedelta(days=days - 1)).day}" if days > 1 else "")
        return "\n".join(lines) if lines else f"Nothing on the Cozi calendar for {span}."

    def add(self, title, day, start="", end="", who=(), location="", notes=""):
        title = " ".join(str(title or "").split())
        if not title:
            raise CoziError("An appointment needs a title.")
        start_time = parse_time(start) if start else "00:00:00"
        if start and end:
            end_time = parse_time(end)
        elif start:
            end_time = (datetime.strptime(start_time, "%H:%M:%S") + timedelta(hours=1)).strftime("%H:%M:%S")
        else:
            end_time = "00:00:00"
        people = self.people() if who else {}
        attendees = []
        for name in who:
            match = next((pid for full, pid in people.items() if full.lower().split()[0] == str(name).lower().split()[0]), None)
            if match is None:
                raise CoziError(f"{name} isn't in the Cozi family; people there: {', '.join(people)}.")
            attendees.append(match)
        payload = [{"itemType": "appointment", "create": {"startDay": day.isoformat(), "details": {
            "startTime": start_time, "endTime": end_time, "dateSpan": 0, "attendeeSet": attendees, "location": location,
            "notes": notes, "subject": title}}}]
        self.call(CALENDAR, payload, "POST", year=day.year, month=day.month)
        when = "all day" if start_time == "00:00:00" else f"{clock(start_time)} to {clock(end_time)}"
        return f"Added to Cozi: {title} on {day:%a %b} {day.day}, {when}" + (f", for {', '.join(who)}" if who else "") + "."


def build_module(cfg, client=None):
    cozi = client or Cozi(cfg)

    def run(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except (CoziError, OSError, ValueError, KeyError) as e:
                return text_result(str(e), error=True)
        return call

    cozi_events = tool("cozi_events", "Read the family's Cozi calendar. day is today, tomorrow, a weekday or YYYY-MM-DD (default today); "
                       "days is how many days from there (1 for one day, 7 for a week, up to 31).",
                       {"type": "object", "properties": {"day": {"type": "string"}, "days": {"type": "integer"}}})(
        run(lambda a: cozi.events(parse_day(a.get("day")), a.get("days") or 1)))
    cozi_add = tool("cozi_add", "Add an appointment to the family's Cozi calendar. day is today, tomorrow, a weekday or YYYY-MM-DD; "
                    "start and end are times like 15:30 or 3:30 pm (leave both out for all day; end defaults to an hour after start); "
                    "who lists family first names it's for; location and notes are optional.",
                    {"type": "object", "properties": {"title": {"type": "string"}, "day": {"type": "string"}, "start": {"type": "string"},
                                                      "end": {"type": "string"}, "who": {"type": "array", "items": {"type": "string"}},
                                                      "location": {"type": "string"}, "notes": {"type": "string"}},
                     "required": ["title", "day"]})(
        run(lambda a: cozi.add(a["title"], parse_day(a["day"]), a.get("start") or "", a.get("end") or "", a.get("who") or [],
                               a.get("location") or "", a.get("notes") or "")))

    def describe_add(tool_input):
        when = str(tool_input.get("day") or "")
        if tool_input.get("start"):
            when += f" at {tool_input['start']}"
        who = tool_input.get("who") or []
        return f"add '{tool_input.get('title', '')}' to the Cozi calendar on {when}" + (f" for {', '.join(who)}" if who else "")

    return ToolModule(server="cozi", prompt=PROMPT, available=lambda c: load_login(c) is not None, tools=[
        ToolSpec(fn=cozi_events, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="the Cozi calendar"),
        ToolSpec(fn=cozi_add, policy="confirm", scope=Scope.household(), outward=True, describe=describe_add)])
