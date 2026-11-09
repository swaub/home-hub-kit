import asyncio
import json
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

API = "https://api.steampowered.com"
STORE_SEARCH = "https://store.steampowered.com/api/storesearch/"
STORE_DETAILS = "https://store.steampowered.com/api/appdetails"
TIMEOUT = 8
PROMPT = ("Steam: steam_price looks up a game's current price and sale on the Steam store; steam_status says whether they're online "
          "and what they're playing; steam_recent lists recently played games with hours; steam_owns checks their library for a game.")
NOT_SET_UP = "Steam isn't set up: add [steam] with api_key and steam_id (or your profile name) to secrets.toml in the data folder."
STATES = {0: "offline", 1: "online", 2: "busy", 3: "away", 4: "snoozing", 5: "looking to trade", 6: "looking to play"}


class SteamError(RuntimeError):
    pass


def load_settings(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        table = tomllib.load(f).get("steam") or {}
    if not table.get("api_key") or not table.get("steam_id"):
        return None
    return {"api_key": str(table["api_key"]), "steam_id": str(table["steam_id"])}


def get_json(url, params):
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": "homehub"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SteamError("Steam rejected the API key.") from e
        raise SteamError(f"Steam said {e.code}.") from e


def price_words(overview):
    if not overview:
        return "no price listed (not sold on its own right now)"
    final = overview.get("final_formatted") or f"${overview.get('final', 0) / 100:.2f}"
    if overview.get("discount_percent"):
        return f"{final}, {overview['discount_percent']}% off (normally {overview.get('initial_formatted', '')})"
    return f"{final}, not on sale"


class Steam:
    def __init__(self, cfg):
        self.cfg = cfg
        self.owned = None

    def settings(self):
        settings = load_settings(self.cfg)
        if settings is None:
            raise SteamError(NOT_SET_UP)
        return settings

    def steam_id(self):
        settings = self.settings()
        sid = settings["steam_id"]
        if sid.isdigit() and len(sid) == 17:
            return sid
        reply = get_json(f"{API}/ISteamUser/ResolveVanityURL/v1/", {"key": settings["api_key"], "vanityurl": sid}).get("response", {})
        if reply.get("success") != 1:
            raise SteamError(f"Steam doesn't know a profile called {sid}.")
        return reply["steamid"]

    def status(self):
        settings = self.settings()
        players = get_json(f"{API}/ISteamUser/GetPlayerSummaries/v2/", {"key": settings["api_key"], "steamids": self.steam_id()}).get("response", {}).get("players", [])
        if not players:
            raise SteamError("Steam returned no profile; is it set to public?")
        p = players[0]
        if p.get("gameextrainfo"):
            return f"{p.get('personaname', 'You')} is playing {p['gameextrainfo']}."
        return f"{p.get('personaname', 'You')} is {STATES.get(p.get('personastate', 0), 'offline')} on Steam."

    def recent(self):
        settings = self.settings()
        games = get_json(f"{API}/IPlayerService/GetRecentlyPlayedGames/v1/", {"key": settings["api_key"], "steamid": self.steam_id(), "count": 8}).get("response", {}).get("games", [])
        if not games:
            return "Nothing played on Steam in the last two weeks (or game details are private)."
        return "; ".join(f"{g['name']}: {g.get('playtime_2weeks', 0) / 60:.1f} h in the last two weeks, {g.get('playtime_forever', 0) / 60:.0f} h total" for g in games)

    def owns(self, name):
        settings = self.settings()
        if self.owned is None or time.monotonic() - self.owned[0] > 3600:
            games = get_json(f"{API}/IPlayerService/GetOwnedGames/v1/", {"key": settings["api_key"], "steamid": self.steam_id(), "include_appinfo": 1}).get("response", {}).get("games", [])
            self.owned = (time.monotonic(), games)
        wanted = name.strip().lower()
        hits = [g for g in self.owned[1] if wanted in g.get("name", "").lower()]
        if not hits:
            return f"No game like {name} in the Steam library ({len(self.owned[1])} games)."
        return "; ".join(f"{g['name']} ({g.get('playtime_forever', 0) / 60:.0f} h played)" for g in hits[:5])


def store_price(name):
    found = get_json(STORE_SEARCH, {"term": name, "l": "english", "cc": "US"}).get("items", [])
    if not found:
        raise SteamError(f"The Steam store has nothing called {name}.")
    app = found[0]
    details = (get_json(STORE_DETAILS, {"appids": app["id"], "cc": "US"}) or {}).get(str(app["id"]), {}).get("data")
    details = details if isinstance(details, dict) else {}
    if details.get("is_free"):
        return f"{app['name']} is free on Steam."
    return f"{app['name']} on Steam: {price_words(details.get('price_overview'))}."


def build_module(cfg, client=None):
    steam = client or Steam(cfg)

    def run(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except (SteamError, OSError, KeyError) as e:
                return text_result(str(e), error=True)
        return call

    steam_price = tool("steam_price", "Current Steam store price for a game and whether it's on sale.",
                       {"type": "object", "properties": {"game": {"type": "string"}}, "required": ["game"]})(run(lambda a: store_price(a["game"])))
    steam_status = tool("steam_status", "Whether they're online on Steam and what they're playing.", {"type": "object", "properties": {}})(run(lambda a: steam.status()))
    steam_recent = tool("steam_recent", "Games played on Steam in the last two weeks, with hours.", {"type": "object", "properties": {}})(run(lambda a: steam.recent()))
    steam_owns = tool("steam_owns", "Whether a game is in their Steam library, with hours played.",
                      {"type": "object", "properties": {"game": {"type": "string"}}, "required": ["game"]})(run(lambda a: steam.owns(a["game"])))
    return ToolModule(server="steam", prompt=PROMPT, tools=[
        ToolSpec(fn=fn, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="Steam")
        for fn in (steam_price, steam_status, steam_recent, steam_owns)])
