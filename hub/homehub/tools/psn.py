import asyncio
import base64
import itertools
import json
import re
import threading
import time
import tomllib
import urllib.parse
import urllib.request

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

PROMPT = ("PlayStation Network: psn_status says whether the owner is online on PlayStation and what they're playing, and which "
          "friends are online and playing what; psn_games lists recently played games with play time; psn_trophies gives the trophy "
          "level and progress per game. These come from the owner's PSN account online, not from the console.")
NOT_SET_UP = ("PlayStation Network isn't connected: sign in at playstation.com, open ca.account.sony.com/api/v1/ssocookie, and put the "
              "npsso code under [psn] in secrets.toml.")
EXPIRED = ("The PlayStation Network sign-in has expired: sign in at playstation.com again, open ca.account.sony.com/api/v1/ssocookie and "
           "give the hub the new npsso code.")
LIVE_SECONDS = 60
SLOW_SECONDS = 900
MAX_IMAGE = 400_000
MAX_LIBRARY = 500
REMIND_DAYS = 7
TOKEN_SECONDS = 5_184_000
IMAGE_HOSTS = ("image.api.playstation.com", "psnobj.prod.dl.playstation.net", "static-resource.np.community.playstation.net")
RESIZABLE = ("image.api.playstation.com",)


class PsnError(RuntimeError):
    pass


def load_login(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        table = tomllib.load(f).get("psn") or {}
    if not table.get("npsso"):
        return None
    return {"npsso": str(table["npsso"]), "expires_at": int(table.get("expires_at") or 0)}


def presence_of(raw):
    basic = (raw or {}).get("basicPresence") or raw or {}
    platform = basic.get("primaryPlatformInfo") or {}
    games = basic.get("gameTitleInfoList") or []
    game = games[0] if games else {}
    return {"online": platform.get("onlineStatus") == "online" or basic.get("availability") == "availableToPlay",
            "platform": platform.get("platform", ""), "last_online": platform.get("lastOnlineDate") or basic.get("lastAvailableDate"),
            "game": game.get("titleName", ""), "game_image": game.get("conceptIconUrl") or game.get("npTitleIconUrl") or ""}


def with_token(secrets_text, npsso, expires_at):
    block = f'[psn]\nnpsso = "{npsso}"\nexpires_at = {int(expires_at)}\n'
    if re.search(r"(?m)^\[psn\]\s*$", secrets_text):
        return re.sub(r"(?ms)^\[psn\]\s*$.*?(?=^\[|\Z)", lambda m: block + "\n", secrets_text).rstrip("\n") + "\n"
    return (secrets_text.rstrip("\n") + "\n\n" if secrets_text.strip() else "") + block


def hours(duration):
    seconds = int(duration.total_seconds()) if duration else 0
    h, m = divmod(seconds // 60, 60)
    return f"{h} h {m} min" if h else f"{m} min"


def image_url(url):
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme not in ("https", "http") or parts.hostname not in IMAGE_HOSTS:
        return None
    if parts.scheme == "http":
        url = "https://" + url.split("://", 1)[1]
    if parts.hostname in RESIZABLE and "w=" not in parts.query:
        return url + ("&" if parts.query else "?") + "w=200"
    return url


class Psn:
    def __init__(self, cfg, login=None, notify=None):
        self.cfg = cfg
        self.login = login
        self.notify = notify
        self.reminded = None
        self.lock = threading.Lock()
        self.client = None
        self.slow = (None, 0.0)
        self.live = (None, 0.0)
        self.names = {}
        self.images = {}
        self.state = None
        self.error = None
        self.refreshing = False

    def credentials(self):
        found = self.login or load_login(self.cfg)
        if found is None:
            raise PsnError(NOT_SET_UP)
        return found

    def connect(self):
        if self.client is None:
            from psnawp_api import PSNAWP

            try:
                self.client = PSNAWP(self.credentials()["npsso"])
                self.client.me().account_id
            except Exception as e:
                self.client = None
                raise PsnError(EXPIRED if "npsso" in str(e).lower() or "401" in str(e) or "auth" in type(e).__name__.lower() else
                               f"PlayStation Network didn't answer: {type(e).__name__}") from e
        return self.client

    def guarded(self, work):
        try:
            return work()
        except PsnError:
            raise
        except Exception as e:
            text = f"{type(e).__name__}: {e}"
            self.client = None
            if "401" in text or "unauthor" in text.lower() or "expired" in text.lower():
                raise PsnError(EXPIRED) from e
            raise PsnError(f"PlayStation Network didn't answer: {text[:160]}") from e

    def name_of(self, account_id):
        if account_id not in self.names:
            self.names[account_id] = self.connect().user(account_id=account_id).online_id
        return self.names[account_id]

    def slow_part(self):
        data, at = self.slow
        if data is not None and time.monotonic() - at < SLOW_SECONDS:
            return data
        psn = self.connect()
        me = psn.me()
        profile = me.get_profile_legacy().get("profile", {})
        friends = [f.account_id for f in itertools.islice(me.friends_list(limit=200), 200)]
        recent = [{"name": t.name, "image": t.image_url or "", "plays": t.play_count, "time": hours(t.play_duration),
                   "minutes": int(t.play_duration.total_seconds() // 60) if t.play_duration else 0,
                   "last": t.last_played_date_time.isoformat() if t.last_played_date_time else None}
                  for t in itertools.islice(me.title_stats(limit=10), 10)]
        summary = me.trophy_summary()
        trophies = {"level": summary.trophy_level, "progress": summary.progress,
                    "earned": {k: getattr(summary.earned_trophies, k) for k in ("bronze", "silver", "gold", "platinum")},
                    "games": [{"name": t.title_name.removesuffix(" Trophies"), "image": t.title_icon_url or "", "progress": t.progress,
                               "earned": {k: getattr(t.earned_trophies, k) for k in ("bronze", "silver", "gold", "platinum")}}
                              for t in itertools.islice(me.trophy_titles(limit=10), 10)]}
        library, seen = [], set()
        for item in itertools.islice(me.game_entitlements(page_size=100), MAX_LIBRARY):
            meta = item.get("gameMeta") or {}
            name = meta.get("name") or (item.get("titleMeta") or {}).get("name")
            if item.get("isGame") and name and name not in seen:
                seen.add(name)
                library.append({"name": name, "image": meta.get("iconUrl") or ""})
        avatar = next((a.get("avatarUrl") for a in profile.get("avatarUrls", []) if a.get("avatarUrl")), "")
        data = {"online_id": profile.get("onlineId") or me.online_id, "account_id": me.account_id, "avatar": avatar,
                "plus": bool(profile.get("plus")), "friend_ids": friends, "recent": recent, "trophies": trophies,
                "library": sorted(library, key=lambda g: g["name"].lower())}
        self.slow = (data, time.monotonic())
        return data

    def live_part(self, slow):
        data, at = self.live
        if data is not None and time.monotonic() - at < LIVE_SECONDS:
            return data
        psn = self.connect()
        mine = presence_of(psn.user(account_id=slow["account_id"]).get_presence())
        friends = []
        ids = slow["friend_ids"]
        for start in range(0, len(ids), 20):
            for raw in psn.me().get_presences(ids[start:start + 20]).get("basicPresences", []):
                friends.append({"name": self.name_of(raw["accountId"]), **presence_of(raw)})
        friends.sort(key=lambda f: (not f["online"], f["name"].lower()))
        data = {"me": mine, "friends": friends}
        self.live = (data, time.monotonic())
        return data

    def load(self):
        with self.lock:
            login = self.credentials()
            slow = self.guarded(self.slow_part)
            live = self.guarded(lambda: self.live_part(slow))
        days = int((login["expires_at"] - time.time()) // 86400) if login["expires_at"] else None
        view = {"online_id": slow["online_id"], "avatar": slow["avatar"], "plus": slow["plus"], "me": live["me"], "friends": live["friends"],
                "recent": slow["recent"], "trophies": slow["trophies"], "library": slow["library"], "token_days_left": days,
                "updated": time.time()}
        self.state = view
        self.error = None
        self.remind(days)
        return view

    def remind(self, days):
        if self.notify is None or days is None or days > REMIND_DAYS:
            return
        stage = "soon" if days > 1 else "now"
        if self.reminded == stage:
            return
        self.reminded = stage
        when = "tomorrow" if days == 1 else "today" if days <= 0 else f"in {days} days"
        self.notify("PlayStation sign-in expires " + when, "Sign in at playstation.com, open ca.account.sony.com/api/v1/ssocookie, and paste the "
                    "npsso code on the PS5 page of the home app.")

    def set_token(self, text, who):
        if not who or who.get("role") != "owner":
            raise PsnError("Only the household's owner can change the PlayStation sign-in.")
        raw = str(text or "").strip()
        expires_in = None
        try:
            parsed = json.loads(raw)
            raw, expires_in = str(parsed.get("npsso", "")), parsed.get("expires_in")
        except (ValueError, AttributeError):
            found = re.search(r"[A-Za-z0-9]{64}", raw)
            raw = found.group(0) if found else raw
        if not re.fullmatch(r"[A-Za-z0-9]{64}", raw):
            raise PsnError("That isn't an npsso code: it's the 64 letters and numbers after npsso on the ssocookie page.")
        from psnawp_api import PSNAWP

        try:
            PSNAWP(raw).me().account_id
        except Exception as e:
            raise PsnError("PlayStation Network didn't accept that code; sign in at playstation.com again and copy a fresh one.") from e
        expires_at = int(time.time()) + int(expires_in or TOKEN_SECONDS)
        path = self.cfg.data_dir() / "secrets.toml"
        text_now = path.read_text(encoding="utf-8") if path.is_file() else ""
        updated = with_token(text_now, raw, expires_at)
        backup = path.with_name(path.name + ".before-psn-renewal")
        if path.is_file():
            backup.write_text(text_now, encoding="utf-8")
            backup.chmod(0o600)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(updated, encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
        with self.lock:
            self.client = None
            self.login = None
            self.slow = (None, 0.0)
            self.live = (None, 0.0)
        self.reminded = None
        self.refresh_in_background()
        return f"Saved the new PlayStation sign-in; it's good for {int(expires_in or TOKEN_SECONDS) // 86400} days."

    def refresh_in_background(self):
        if self.refreshing:
            return
        self.refreshing = True

        def work():
            try:
                self.load()
            except PsnError as e:
                self.error = str(e)
            finally:
                self.refreshing = False

        threading.Thread(target=work, name="psn-refresh", daemon=True).start()

    def warm(self):
        if load_login(self.cfg) is not None:
            self.refresh_in_background()
        return self

    def overview(self, who=None):
        self.credentials()
        view = self.state
        if view is None or time.time() - view["updated"] > LIVE_SECONDS:
            self.refresh_in_background()
        if view is None:
            return {"loading": True, "error": self.error}
        return {**view, "refreshing": self.refreshing, "error": self.error}

    def fetch_image(self, url):
        target = image_url(url)
        if target is None:
            return None
        request = urllib.request.Request(target, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                kind = response.headers.get_content_type()
                data = response.read(MAX_IMAGE + 1)
        except OSError:
            return None
        if not kind.startswith("image/") or len(data) > MAX_IMAGE:
            return None
        return f"data:{kind};base64,{base64.b64encode(data).decode()}"

    def act(self, body, who=None):
        if body.get("action") == "images":
            view = self.state or self.load()
            urls = {view["avatar"], view["me"]["game_image"], *(g["image"] for g in view["recent"]), *(g["image"] for g in view["trophies"]["games"]),
                    *(g["image"] for g in view["library"]), *(f["game_image"] for f in view["friends"])}
            for url in sorted(u for u in urls if u):
                if url not in self.images:
                    found = self.fetch_image(url)
                    if found:
                        self.images[url] = found
            return {"images": self.images}
        if body.get("action") == "set_token":
            return {"text": self.set_token(body.get("token"), who)}
        if body.get("action") == "refresh":
            with self.lock:
                self.slow = (None, 0.0)
                self.live = (None, 0.0)
            self.refresh_in_background()
            return {"text": "Refreshing from PlayStation Network; this takes about a minute."}
        raise PsnError("unknown PlayStation action")


def describe_status(view):
    me = view["me"]
    line = (f"{view['online_id']} is online" + (f", playing {me['game']}" if me["game"] else "") + "." if me["online"]
            else f"{view['online_id']} is offline on PlayStation.")
    online = [f for f in view["friends"] if f["online"]]
    if online:
        line += " Friends online: " + "; ".join(f["name"] + (f" playing {f['game']}" if f["game"] else "") for f in online) + "."
    else:
        line += f" None of {len(view['friends'])} friends are online."
    return line


def build_module(cfg, client=None):
    psn = client or Psn(cfg)

    def run(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except PsnError as e:
                return text_result(str(e), error=True)
        return call

    def games(args):
        view = psn.state or psn.load()
        return "Recently played: " + "; ".join(f"{g['name']} ({g['time']} over {g['plays']} session{'s' if g['plays'] != 1 else ''})"
                                               for g in view["recent"]) + "."

    def trophies(args):
        t = (psn.state or psn.load())["trophies"]
        earned = t["earned"]
        return (f"Trophy level {t['level']} ({t['progress']}% to the next), {earned['platinum']} platinum, {earned['gold']} gold, "
                f"{earned['silver']} silver, {earned['bronze']} bronze. By game: " + "; ".join(f"{g['name']} {g['progress']}%" for g in t["games"]) + ".")

    psn_status = tool("psn_status", "Whether the owner is online on PlayStation and what they're playing, and which friends are online.",
                      {"type": "object", "properties": {}})(run(lambda a: describe_status(psn.load())))
    psn_games = tool("psn_games", "Recently played PlayStation games with total play time.", {"type": "object", "properties": {}})(run(games))
    psn_trophies = tool("psn_trophies", "PlayStation trophy level and trophy progress per game.", {"type": "object", "properties": {}})(run(trophies))
    return ToolModule(server="psn", prompt=PROMPT, available=lambda c: load_login(c) is not None, tools=[
        ToolSpec(fn=fn, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="PlayStation Network")
        for fn in (psn_status, psn_games, psn_trophies)])
