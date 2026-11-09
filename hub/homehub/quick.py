import asyncio
import re
import time
from datetime import datetime, timedelta

from homehub.household import household_facts
from homehub.tools import result_texts
from homehub.tools import weather as weather_api
from homehub.tools.timers import parse_clock

LEADING = re.compile(r"^(?:(?:please|can you|could you|would you|will you|hey|ok|okay|so|um|uh|and|now|go ahead and|i want you to|"
                     r"i'd like you to|i need you to)\s+)+")
WEATHER_QUESTION = re.compile(r"(?:what|what's|how|how's|will|is|does|do|tell me)\b")
TRAILING = re.compile(r"(?:\s+(?:please|for me|thanks|thank you|now|right now))+$")
ONES = {"zero": 0, "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
        "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
        "eighteen": 18, "nineteen": 19, "couple": 2, "few": 3}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
UNITS = {"second": 1, "seconds": 1, "sec": 1, "secs": 1, "minute": 60, "minutes": 60, "min": 60, "mins": 60, "hour": 3600, "hours": 3600,
         "hr": 3600, "hrs": 3600}
DURATION_FILLER = ("and", "of", "a", "an")
FORECAST_SECONDS = 1800
TV_APPS = ("netflix", "youtube", "youtube tv", "disney plus", "hulu", "prime video", "amazon prime", "max", "hbo max", "crunchyroll", "plex",
           "spotify", "jellyfin", "peacock", "paramount plus", "apple tv", "twitch", "tubi", "pluto tv")
NOT_SPOTIFY = re.compile(r"\b(?:tv|television|youtube|netflix|jellyfin|movie|show|episode|video|trailer|ps5|playstation|game)\b")
WEATHER_WORDS = re.compile(r"\b(?:weather|forecast|temperature|temp|rain|raining|reign|snow|snowing|umbrella)\b|^how (?:cold|hot|warm) is it\b|"
                           r"^is it (?:cold|hot|warm|chilly)\b|^do i need (?:a |an )?(?:jacket|coat|sweater)\b")
WEATHER_OTHER = re.compile(r"\b(?:in|at|near|for)\s+(?!the\b|today\b|tomorrow\b|tonight\b|now\b|here\b|outside\b)\w+|\b(?:week|weekend|monday|"
                           r"tuesday|wednesday|thursday|friday|saturday|sunday|next|yesterday|hour|hourly|sunrise|sunset|humidity|uv|"
                           r"pollen|air quality|wind)\b")
TIME = re.compile(r"(?:what(?:'s| is) the time|what time is it|what time it is|tell me the time|the time|time|current time)(?: now| right now)?")
DATE = re.compile(r"(?:what(?:'s| is) (?:the |today's )?date(?: today)?|what day is (?:it|today)(?: today)?|what(?:'s| is) today|"
                  r"what's the day|today's date)")
TIMER_SET = (re.compile(r"(?:set|start|make|put on|give me) (?:a |an |the |my )?(?P<label>(?!(?:a|an|the|my)\b)[a-z]+(?: [a-z]+)?) timer (?:for|of) (?P<d>.+)"),
             re.compile(r"(?:set|start|make|put on|give me) (?:a |an |the |my )?timer (?:for|of) (?P<d>.+?)(?: (?:called|named|for|labeled) (?P<label>[a-z ]+))?"),
             re.compile(r"(?:set|start|make|put on|give me) (?:a |an |the |my )?(?P<d>.+?) timer(?: (?:called|named|for|labeled) (?P<label>[a-z ]+))?"),
             re.compile(r"timer (?:for )?(?P<d>.+)"),
             re.compile(r"(?P<d>.+?) timer"))
TIMER_LEFT = re.compile(r"(?:how (?:much time|long) (?:is |'s )?(?:left|remaining)(?: on (?:the |my )?(?:timer|alarm)s?)?|"
                        r"(?:check|list) (?:the |my )?(?:timer|alarm|reminder)s?|what (?:timer|alarm|reminder)s? (?:do i have|are (?:set|running|on))|"
                        r"(?:any|are there any) (?:timer|alarm|reminder)s?(?: set| running)?|how(?:'s| is) (?:the|my) timer(?: doing)?)")
CANCEL = re.compile(r"(?:cancel|canceled|cancelled|delete|clear|remove|get rid of) (?P<all>all (?:of )?)?(?:the |my |of my |of the )?(?P<label>(?!(?:a|an|the|my)\b)[a-z]+ )?"
                    r"(?P<kind>timer|alarm|reminder)(?P<plural>s)?")
ALARM = re.compile(r"(?:set (?:an |the |my |a )?alarm|wake me(?: up)?)(?: for| at)?(?: tomorrow(?: morning)?(?: at)?)? (?P<t>\d{1,2}(?::\d{2})?(?: ?[ap]m)?)"
                   r"(?: tomorrow(?: morning)?| in the morning| tonight)?(?P<rep> every day| daily| on weekdays| every weekday| weekdays)?")
SPOTIFY = (("pause", re.compile(r"(?:pause|stop) (?:the )?(?:music|spotify|song|track)|pause spotify|pause music")),
           ("resume", re.compile(r"(?:resume|unpause|continue)(?: the)?(?: music| spotify| song| playing| playback)?|(?:play|resume) (?:the )?music|"
                                 r"(?:play|resume|start) spotify")),
           ("next", re.compile(r"(?:skip|next)(?: this| the)?(?: song| track)?|play (?:the )?next (?:song|track)|skip it")),
           ("previous", re.compile(r"(?:previous|last|go back(?: to the previous| to the last| a)?)(?: song| track)|play (?:the )?(?:previous|last) (?:song|track)")),
           ("now", re.compile(r"what(?:'s| is) (?:this song|playing|the song|this|this track|the song playing)(?: on spotify)?(?: called)?|"
                              r"what song is (?:this|playing|that)|who (?:sings|is singing|sang) (?:this|that)(?: song)?|what am i listening to")))
PLAY = re.compile(r"play (?:me )?(?P<q>.+?)(?: on spotify)?")
TV_POWER = re.compile(r"turn (?P<a>on|off) (?:the )?(?:tv|television)|turn (?:the )?(?:tv|television) (?P<b>on|off)|(?:tv|television) (?P<c>on|off)")
TV_APP = re.compile(r"(?:open|launch|start|put on|pull up|go to|switch to) (?P<app>[a-z+ ]+?)(?: on the (?:tv|television))?")
PS5 = re.compile(r"turn (?P<a>on|off) (?:the |my )?(?:ps5|ps 5|playstation(?: 5| five)?)|turn (?:the |my )?(?:ps5|ps 5|playstation(?: 5| five)?) (?P<b>on|off)|"
                 r"(?P<rest>(?:put|send) (?:the |my )?(?:ps5|ps 5|playstation(?: 5| five)?) (?:to|in|into) rest(?: mode)?)")
LIGHT = re.compile(r"turn (?P<a>on|off) (?:the |my )?(?P<name>.+?)|turn (?:the |my )?(?P<name2>.+?) (?P<b>on|off)")
LIST_ADD = re.compile(r"(?:add|put) (?P<items>.+?) (?:to|on|onto) (?:the |my |our )?(?P<list>[a-z' -]+?) list")
LIST_READ = re.compile(r"(?:what(?:'s| is) on|read(?: me)?|what do i have on|tell me) (?:the |my |our )?(?P<list>[a-z' -]+?) list")
LIST_REMOVE = re.compile(r"(?:remove|take|delete|cross) (?P<items>.+?) (?:off|from) (?:of )?(?:the |my |our )?(?P<list>[a-z' -]+?) list")
NO_LIST_NAME = ("my", "the", "our", "a", "your")
RING = re.compile(r"(?:ring|find|where(?:'s| is)) my phone|make my phone ring|i can't find my phone")
BATTERY = re.compile(r"(?:what(?:'s| is) )?(?:my )?phone(?:'s)? battery(?: level| at)?|how much battery (?:does|is) my phone(?: have| at)?|is my phone charging")

def normalize(text):
    t = text.lower().replace("a.m.", "am").replace("p.m.", "pm").replace("&", " and ").replace("-", " ")
    t = re.sub(r"(\d)\s*(am|pm)\b", r"\1 \2", t)
    t = re.sub(r"(\d):\s+(\d\d)\b", r"\1:\2", t)
    t = re.sub(r"[^\w:' ]+", " ", t)
    t = " ".join(t.split())
    t = LEADING.sub("", t)
    t = TRAILING.sub("", t)
    return t


def parse_count(words):
    if not words:
        return None
    if len(words) == 1 and re.fullmatch(r"\d+(?:\.\d+)?", words[0]):
        return float(words[0])
    total, seen = 0, False
    for word in words:
        if word in TENS:
            total += TENS[word]
        elif word in ONES:
            total += ONES[word]
        elif re.fullmatch(r"\d+", word):
            total += int(word)
        else:
            return None
        seen = True
    return float(total) if seen else None


def parse_duration(text):
    words = text.replace("half an", "half a").split()
    total, number = 0.0, []
    i = 0
    while i < len(words):
        word = words[i]
        if word == "half" and i + 2 < len(words) and words[i + 1] in ("a", "an") and words[i + 2] in UNITS:
            total += UNITS[words[i + 2]] / 2
            i += 3
            continue
        if word in UNITS:
            count = parse_count([w for w in number if w not in ("and", "of")]) if number else 1.0
            if count is None:
                return None
            total += count * UNITS[word]
            number = []
            if words[i + 1:i + 4] == ["and", "a", "half"]:
                total += UNITS[word] / 2
                i += 3
            i += 1
            continue
        if word in DURATION_FILLER and not number:
            i += 1
            continue
        number.append(word)
        i += 1
    if number or total <= 0:
        return None
    return int(round(total))


def say_duration(seconds):
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    if secs and not hours:
        parts.append(f"{secs} second" + ("s" if secs != 1 else ""))
    return " and ".join(parts) or "0 seconds"


def say_clock(when):
    return when.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")


def ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def split_items(text):
    return [i.strip() for i in re.split(r",| and ", text) if i.strip() and i.strip() not in ("some", "a", "an")]


class Quick:
    def __init__(self, cfg, registry, permissions, storage):
        self.cfg = cfg
        self.registry = registry
        self.permissions = permissions
        self.storage = storage
        self.light_names = [n.strip() for n in cfg.lights.devices if n.strip()]
        self.forecasts = {}
        self.handlers = (self.time, self.date, self.list_add, self.list_remove, self.list_read, self.timer_left, self.cancel, self.alarm,
                         self.timer, self.spotify_control, self.tv_power, self.ps5, self.light, self.tv_app,
                         self.ring, self.battery, self.weather, self.play)

    def has(self, name):
        spec = self.registry.spec(name)
        return spec is not None and spec.fn is not None and spec.policy == "auto"

    def match(self, text):
        t = normalize(text)
        for candidate in dict.fromkeys((t, LEADING.sub("", t))):
            found = self.first_match(candidate)
            if found is not None:
                return found
        return None

    def first_match(self, t):
        for handler in self.handlers:
            found = handler(t)
            if found is not None:
                return found
        return None

    def recognizes(self, text):
        return self.match(text) is not None

    async def handle(self, text, ctx):
        found = self.match(text)
        if found is None:
            return None
        intent, action = found
        try:
            reply = await action(ctx)
        except Exception as e:
            self.registry.publish("service.warning", message=f"quick {intent} failed: {type(e).__name__}: {e}")
            return None
        return (intent, reply) if reply else None

    async def tool(self, name, args, ctx):
        spec = self.registry.spec(name)
        decision = self.permissions.decide(name, args, ctx)
        if not decision.allow:
            return None
        self.permissions.record(name, args, decision, ctx)
        server = name.split("__")[1]
        result = await self.registry.wrap(spec, server).handler(args)
        if result.get("is_error"):
            return None
        texts = result_texts(result)
        return texts[0].split("\n", 1)[-1] if spec.untrusted_output and texts else (texts[0] if texts else "")

    def time(self, t):
        if TIME.fullmatch(t):
            return "time", self.say_time
        return None

    async def say_time(self, ctx):
        return f"It's {datetime.now().strftime('%I:%M %p').lstrip('0')}."

    def date(self, t):
        if DATE.fullmatch(t):
            return "date", self.say_date
        return None

    async def say_date(self, ctx):
        now = datetime.now()
        return f"It's {now.strftime('%A, %B')} {ordinal(now.day)}."

    def timer(self, t):
        if not self.has("mcp__time__set_timer"):
            return None
        for pattern in TIMER_SET:
            m = pattern.fullmatch(t)
            if m:
                seconds = parse_duration(m.group("d"))
                if seconds:
                    label = (m.groupdict().get("label") or "").strip()
                    return "timer", lambda ctx: self.set_timer(seconds, label, ctx)
        return None

    async def set_timer(self, seconds, label, ctx):
        args = {"seconds": seconds, **({"label": label} if label else {})}
        if await self.tool("mcp__time__set_timer", args, ctx) is None:
            return None
        return f"{label.capitalize() + ' timer' if label else 'Timer'} set for {say_duration(seconds)}."

    def timer_left(self, t):
        if TIMER_LEFT.fullmatch(t) and self.has("mcp__time__list_timers"):
            return "timers", self.say_timers
        return None

    async def say_timers(self, ctx):
        if await self.tool("mcp__time__list_timers", {}, ctx) is None:
            return None
        items = self.storage.reminders()
        if not items:
            return "You don't have any timers or alarms set."
        now = datetime.now()
        parts = []
        for item in items[:3]:
            due = datetime.fromisoformat(item["due"])
            label = f"{item['label']} " if item["label"] else ""
            if item["kind"] == "timer":
                parts.append(f"your {label}timer has {say_duration(max(0, (due - now).total_seconds()))} left")
            else:
                when = say_clock(due) + ("" if due.date() == now.date() else " tomorrow" if due.date() == (now + timedelta(days=1)).date() else due.strftime(" on %A"))
                parts.append(f"your {label}{item['kind']} is set for {when}")
        more = f", plus {len(items) - 3} more" if len(items) > 3 else ""
        text = "; ".join(parts) + more + "."
        return text[:1].upper() + text[1:]

    def cancel(self, t):
        m = CANCEL.fullmatch(t)
        if not m or not self.has("mcp__time__cancel_timer"):
            return None
        which = "all" if m.group("all") else (m.group("label") or "").strip() or m.group("kind")
        return "cancel", lambda ctx: self.cancel_items(which, m.group("kind"), ctx)

    async def cancel_items(self, which, kind, ctx):
        before = len(self.storage.reminders())
        if await self.tool("mcp__time__cancel_timer", {"which": which}, ctx) is None:
            return f"You don't have any {kind}s set."
        removed = before - len(self.storage.reminders())
        return f"Cancelled {'your ' + kind if removed == 1 else str(removed) + ' ' + ('items' if which == 'all' else kind + 's')}."

    def alarm(self, t):
        m = ALARM.fullmatch(t)
        if not m or not self.has("mcp__time__set_alarm"):
            return None
        clock = m.group("t").replace(" ", "")
        if not re.search(r"[ap]m", clock) and int(clock.split(":")[0]) <= 12:
            if not re.search(r"\b(?:wake|morning)\b", t):
                return None
            clock += "am"
        try:
            due = parse_clock(clock)
        except ValueError:
            return None
        repeat = {"every day": "daily", "daily": "daily"}.get((m.group("rep") or "").strip(), "weekdays" if m.group("rep") else "")
        return "alarm", lambda ctx: self.set_alarm(clock, due, repeat, ctx)

    async def set_alarm(self, clock, due, repeat, ctx):
        args = {"at": clock, **({"repeat": repeat} if repeat else {})}
        if await self.tool("mcp__time__set_alarm", args, ctx) is None:
            return None
        day = "" if repeat else (" today" if due.date() == datetime.now().date() else " tomorrow")
        every = {"daily": " every day", "weekdays": " on weekdays"}.get(repeat, "")
        return f"Alarm set for {say_clock(due)}{day}{every}."

    def weather(self, t):
        if not WEATHER_WORDS.search(t) or WEATHER_OTHER.search(t) or len(t.split()) > 12 or not self.has("mcp__weather__get_weather"):
            return None
        if re.match(r"(?:play|add|put|remove|send|text|tell|call|search|look up|find|buy|order|shop)\b", t):
            return None
        day = 1 if "tomorrow" in t else 0
        kind = "rain" if re.search(r"\b(?:rain|raining|reign|umbrella|snow|snowing)\b", t) else "general"
        return "weather", lambda ctx: self.say_weather(day, kind, "tonight" in t, ctx)

    async def say_weather(self, day, kind, tonight, ctx):
        decision = self.permissions.decide("mcp__weather__get_weather", {"days": 2}, ctx)
        if not decision.allow:
            return None
        self.permissions.record("mcp__weather__get_weather", {"days": 2}, decision, ctx)
        person = self.storage.person(ctx.person_id) if ctx is not None and ctx.person_id else None
        data = await asyncio.to_thread(self.forecast, (person or {}).get("units") or "imperial")
        if data is None:
            return None
        daily, now = data["daily"], data["current"]
        cond = weather_api.describe(daily["weather_code"][day])
        high, low = round(daily["temperature_2m_max"][day]), round(daily["temperature_2m_min"][day])
        chance = daily["precipitation_probability_max"][day] or 0
        when = "tomorrow" if day else ("tonight" if tonight else "today")
        if kind == "rain":
            if chance >= 60:
                return f"Yes, there's a {chance} percent chance of rain {when}."
            if chance >= 25:
                return f"Maybe, there's a {chance} percent chance of rain {when}."
            return f"No, only a {chance} percent chance of rain {when}." if chance else f"No rain expected {when}."
        rain = f", and a {chance} percent chance of rain" if chance >= 20 else ", and no rain expected"
        if day:
            return f"Tomorrow looks {cond}, with a high of {high} and a low of {low}{rain}."
        if tonight:
            return f"Tonight it's {cond}, down to {low} degrees{rain}."
        return f"It's {round(now['temperature_2m'])} degrees and {weather_api.describe(now['weather_code'])} right now. Today's high is {high}, low {low}{rain}."

    def forecast(self, units="imperial"):
        town = household_facts(self.storage).get("town", "")
        if not town:
            return None
        cached = self.forecasts.get((town, units))
        if cached and time.monotonic() - cached[0] < FORECAST_SECONDS:
            return cached[1]
        place = weather_api.find_place(town)
        if place is None:
            return None
        data, _ = weather_api.forecast(place, units, 2)
        self.forecasts[(town, units)] = (time.monotonic(), data)
        return data

    def prefetch(self):
        try:
            for units in {(p.get("units") or "imperial") for p in self.storage.people()} or {"imperial"}:
                self.forecast(units)
        except Exception:
            pass

    def ring(self, t):
        if RING.fullmatch(t) and self.has("mcp__phone__ring_phone"):
            return "phone", lambda ctx: self.tool("mcp__phone__ring_phone", {}, ctx)
        return None

    def battery(self, t):
        if BATTERY.fullmatch(t) and self.has("mcp__phone__phone_battery"):
            return "phone", self.say_battery
        return None

    async def say_battery(self, ctx):
        reply = await self.tool("mcp__phone__phone_battery", {}, ctx)
        m = re.fullmatch(r"Battery (\d+)%(, charging)?\.", reply or "")
        return f"Your phone is at {m.group(1)} percent{' and charging' if m.group(2) else ''}." if m else reply

    def spotify_control(self, t):
        if not self.has("mcp__spotify__spotify_control"):
            return None
        for action, pattern in SPOTIFY:
            if pattern.fullmatch(t):
                if action == "now":
                    return "spotify", lambda ctx: self.tool("mcp__spotify__spotify_now", {}, ctx)
                return "spotify", lambda ctx, a=action: self.tool("mcp__spotify__spotify_control", {"action": a}, ctx)
        return None

    def play(self, t):
        m = PLAY.fullmatch(t)
        if not m or NOT_SPOTIFY.search(t) or not self.has("mcp__spotify__spotify_play"):
            return None
        query, kind = m.group("q").strip(), "track"
        k = re.fullmatch(r"(?:the |my )?(album|playlist|artist) (.+)", query) or re.fullmatch(r"(?:the |my )?(.+?) (album|playlist)", query)
        if k:
            kind, query = (k.group(1), k.group(2)) if k.group(1) in ("album", "playlist", "artist") else (k.group(2), k.group(1))
        elif query.startswith("some "):
            kind, query = "playlist", query[5:]
        if not query or query in ("something", "anything", "music", "a song", "some music"):
            return None
        return "spotify", lambda ctx: self.tool("mcp__spotify__spotify_play", {"query": query, "kind": kind}, ctx)

    def tv_power(self, t):
        m = TV_POWER.fullmatch(t)
        if not m or not self.has("mcp__tv__tv_power"):
            return None
        on = (m.group("a") or m.group("b") or m.group("c")) == "on"
        return "tv", lambda ctx: self.tool("mcp__tv__tv_power", {"on": on}, ctx)

    def tv_app(self, t):
        m = TV_APP.fullmatch(t)
        if not m or m.group("app").strip() not in TV_APPS or not self.has("mcp__tv__tv_app"):
            return None
        return "tv", lambda ctx: self.tool("mcp__tv__tv_app", {"name": m.group("app").strip()}, ctx)

    def ps5(self, t):
        m = PS5.fullmatch(t)
        if not m or not self.has("mcp__ps5__ps5_power"):
            return None
        on = (m.group("a") or m.group("b")) == "on"
        return "ps5", lambda ctx: self.tool("mcp__ps5__ps5_power", {"on": on}, ctx)

    def light(self, t):
        m = LIGHT.fullmatch(t)
        if not m or not self.light_names or not self.has("mcp__lights__light_set"):
            return None
        on = (m.group("a") or m.group("b")) == "on"
        said = (m.group("name") or m.group("name2")).strip()
        forms = {said, re.sub(r"\s+(?:light|lights|switch)$", "", said)}
        found = [n for n in self.light_names if forms & {n.lower(), re.sub(r"\s+(?:light|lights|switch)$", "", n.lower())}]
        if len(found) != 1:
            return None
        return "lights", lambda ctx: self.say_light(found[0], on, ctx)

    async def say_light(self, name, on, ctx):
        if await self.tool("mcp__lights__light_set", {"name": name, "on": on}, ctx) is None:
            return None
        return f"Turned {'on' if on else 'off'} the {name}."

    def list_add(self, t):
        m = LIST_ADD.fullmatch(t)
        if not m or m.group("list").strip() in NO_LIST_NAME or not self.has("mcp__lists__add_to_list"):
            return None
        items = split_items(m.group("items"))
        return ("lists", lambda ctx: self.say_list_add(m.group("list"), items, ctx)) if items else None

    async def say_list_add(self, name, items, ctx):
        reply = await self.tool("mcp__lists__add_to_list", {"list": name, "items": items}, ctx)
        return re.sub(r"\s*The [\w' -]+ list has \d+ items\.$", "", reply or "") or None

    def list_remove(self, t):
        m = LIST_REMOVE.fullmatch(t)
        if not m or m.group("list").strip() in NO_LIST_NAME or not self.has("mcp__lists__remove_from_list"):
            return None
        items = split_items(m.group("items"))
        return ("lists", lambda ctx: self.tool("mcp__lists__remove_from_list", {"list": m.group("list"), "items": items}, ctx)) if items else None

    def list_read(self, t):
        m = LIST_READ.fullmatch(t)
        if not m or m.group("list").strip() in NO_LIST_NAME or not self.has("mcp__lists__read_list"):
            return None
        return "lists", lambda ctx: self.say_list(m.group("list"), ctx)

    async def say_list(self, name, ctx):
        reply = await self.tool("mcp__lists__read_list", {"list": name}, ctx)
        if reply is None:
            return None
        m = re.fullmatch(r"The ([\w' -]+) list \((\d+) items\): (.*)\.", reply)
        if not m:
            return reply
        items = m.group(3).split("; ")
        spoken = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
        return f"Your {m.group(1)} list has {spoken}."
