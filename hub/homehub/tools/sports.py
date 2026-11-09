import asyncio
import json
import time
import urllib.request
from datetime import datetime

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

BASE = "https://site.api.espn.com/apis/site/v2/sports"
LEAGUES = {
    "nfl": "football/nfl", "college football": "football/college-football", "nba": "basketball/nba", "wnba": "basketball/wnba",
    "college basketball": "basketball/mens-college-basketball", "mlb": "baseball/mlb", "nhl": "hockey/nhl", "mls": "soccer/usa.1",
    "premier league": "soccer/eng.1",
}
ALIASES = {"football": "nfl", "basketball": "nba", "baseball": "mlb", "hockey": "nhl", "soccer": "mls", "epl": "premier league",
           "ncaaf": "college football", "ncaab": "college basketball"}
TIMEOUT = 8
CACHE_SECONDS = 60
PROMPT = ("For sports use sports_scores (today's or this week's games and live scores in a league, optionally for one team) and "
          "team_games (a team's last result and next game); leagues: NFL, college football, NBA, WNBA, college basketball, MLB, NHL, "
          "MLS, Premier League. Say scores and start times plainly, in local time.")


def league_path(name):
    key = (name or "").strip().lower()
    key = ALIASES.get(key, key)
    if key not in LEAGUES:
        raise ValueError(f"I don't follow a league called {name}. Leagues: NFL, college football, NBA, WNBA, college basketball, MLB, NHL, MLS, Premier League.")
    return LEAGUES[key]


def local_time(iso):
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except (AttributeError, ValueError):
        return ""
    return when.strftime("%a %b %-d, %-I:%M %p")


def describe_event(event):
    comp = event["competitions"][0]
    status = event["status"]["type"]
    sides = sorted(comp["competitors"], key=lambda c: c.get("homeAway") != "away")
    names = [c["team"].get("displayName") or c["team"].get("name", "") for c in sides]
    scores = [str(c.get("score", {}).get("displayValue") if isinstance(c.get("score"), dict) else c.get("score", "")) for c in sides]
    if status.get("state") == "pre":
        return f"{names[0]} at {names[1]}: {local_time(event.get('date', ''))}"
    detail = "final" if status.get("completed") else status.get("shortDetail", "in progress")
    return f"{names[0]} {scores[0]}, {names[1]} {scores[1]} ({detail})"


def matches(event, team):
    if not team:
        return True
    wanted = team.strip().lower()
    for c in event["competitions"][0]["competitors"]:
        t = c["team"]
        if any(wanted == str(t.get(k, "")).lower() or (k != "abbreviation" and wanted in str(t.get(k, "")).lower())
               for k in ("abbreviation", "displayName", "name", "shortDisplayName", "location")):
            return True
    return False


class Espn:
    def __init__(self):
        self.cache = {}

    def get(self, url):
        cached = self.cache.get(url)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        request = urllib.request.Request(url, headers={"User-Agent": "homehub"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.cache[url] = (time.monotonic(), data)
        return data

    def scores(self, league, team=""):
        events = self.get(f"{BASE}/{league_path(league)}/scoreboard").get("events", [])
        return [describe_event(e) for e in events if matches(e, team)]

    def team(self, league, team):
        path = league_path(league)
        teams = [entry["team"] for entry in self.get(f"{BASE}/{path}/teams")["sports"][0]["leagues"][0]["teams"]]
        wanted = team.strip().lower()
        for t in teams:
            if wanted in (t.get("abbreviation", "").lower(), t.get("displayName", "").lower(), t.get("name", "").lower(),
                          t.get("location", "").lower(), t.get("shortDisplayName", "").lower()) or wanted in t.get("displayName", "").lower():
                return path, t
        raise ValueError(f"No {league} team matches {team}.")

    def games(self, league, team):
        path, t = self.team(league, team)
        events = self.get(f"{BASE}/{path}/teams/{t['id']}/schedule").get("events", [])
        done = [e for e in events if e["competitions"][0]["status"]["type"].get("completed")]
        upcoming = [e for e in events if not e["competitions"][0]["status"]["type"].get("completed")]
        lines = [t.get("displayName", team)]
        if done:
            lines.append("Last: " + describe_schedule(done[-1]))
        if upcoming:
            lines.append("Next: " + describe_schedule(upcoming[0]))
        return lines


def describe_schedule(event):
    comp = event["competitions"][0]
    status = comp["status"]["type"]
    sides = sorted(comp["competitors"], key=lambda c: c.get("homeAway") != "away")
    names = [c["team"].get("displayName", "") for c in sides]
    if not status.get("completed") and status.get("state") == "pre":
        return f"{names[0]} at {names[1]}, {local_time(event.get('date', ''))}"
    scores = [c.get("score", {}).get("displayValue", "") if isinstance(c.get("score"), dict) else str(c.get("score", "")) for c in sides]
    return f"{names[0]} {scores[0]}, {names[1]} {scores[1]}" + ("" if status.get("completed") else f" ({status.get('shortDetail', 'live')})")


def build_module():
    espn = Espn()

    @tool("sports_scores", "Current scoreboard for a league (this week's NFL games, today's NBA games...), optionally only one team's game.",
          {"type": "object", "properties": {"league": {"type": "string"}, "team": {"type": "string"}}, "required": ["league"]})
    async def sports_scores(args):
        try:
            lines = await asyncio.to_thread(espn.scores, args["league"], args.get("team", ""))
        except (ValueError, OSError, KeyError) as e:
            return text_result(f"Couldn't get scores: {e}", error=True)
        return text_result("\n".join(lines) if lines else "No games on that scoreboard right now.")

    @tool("team_games", "A team's most recent result and its next game with the start time.",
          {"type": "object", "properties": {"league": {"type": "string"}, "team": {"type": "string"}}, "required": ["league", "team"]})
    async def team_games(args):
        try:
            lines = await asyncio.to_thread(espn.games, args["league"], args["team"])
        except (ValueError, OSError, KeyError) as e:
            return text_result(f"Couldn't get that team's games: {e}", error=True)
        return text_result("\n".join(lines))

    return ToolModule(server="sports", prompt=PROMPT, tools=[
        ToolSpec(fn=fn, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="ESPN") for fn in (sports_scores, team_games)])
