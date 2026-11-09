import asyncio
import json
import threading
import time
import tomllib
import urllib.parse
import urllib.request

from claude_agent_sdk import tool

from homehub.household import household_facts
from homehub.tools import Scope, ToolModule, ToolSpec, text_result

GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{a};{b}?overview=false"
TOMTOM_URL = "https://api.tomtom.com/routing/1/calculateRoute/{a}:{b}/json"
USER_AGENT = "homehub/0.1 (home voice assistant)"
TIMEOUT = 10
PROMPT = ("For 'how long to get to X', distances, drive time or traffic use directions (from home unless they name a start). "
          "Say the drive time and distance in miles, and the traffic delay when there is one.")


def load_tomtom(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return ""
    with open(path, "rb") as f:
        return str((tomllib.load(f).get("tomtom") or {}).get("api_key", "")).strip()


def minutes_words(seconds):
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def miles(meters):
    value = meters / 1609.344
    return f"{value:.1f} miles" if value < 10 else f"{round(value)} miles"


class Router:
    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.storage = storage
        self.places = {}
        self.lock = threading.Lock()
        self.last_geocode = 0.0

    def get(self, url, params=None):
        full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        with urllib.request.urlopen(urllib.request.Request(full, headers={"User-Agent": USER_AGENT}), timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))

    def home(self):
        return self.cfg.directions.home.strip() or household_facts(self.storage).get("town", "")

    def geocode(self, query):
        key = " ".join(query.lower().split())
        if key in self.places:
            return self.places[key]
        with self.lock:
            wait = 1.1 - (time.monotonic() - self.last_geocode)
            if wait > 0:
                time.sleep(wait)
            found = self.get(GEOCODE_URL, {"q": query, "format": "json", "limit": 5, "countrycodes": "us", "addressdetails": 0})
            self.last_geocode = time.monotonic()
        if not found:
            raise RuntimeError(f"I couldn't find {query} on the map.")
        best = next((f for f in found if f.get("addresstype") in ("house", "building", "road", "city", "town", "village", "amenity", "shop")), found[0])
        place = {"lat": float(best["lat"]), "lon": float(best["lon"]), "label": best.get("name") or best.get("display_name", query).split(",")[0]}
        self.places[key] = place
        return place

    def route(self, destination, origin=""):
        start_text = origin.strip() or self.home()
        if not start_text:
            raise RuntimeError("I don't know where home is: set [directions] home in the config or say where to start.")
        start, end = self.geocode(start_text), self.geocode(destination)
        key = load_tomtom(self.cfg)
        if key:
            data = self.get(TOMTOM_URL.format(a=f"{start['lat']},{start['lon']}", b=f"{end['lat']},{end['lon']}"),
                            {"key": key, "traffic": "true", "travelMode": "car"})
            summary = data["routes"][0]["summary"]
            delay = summary.get("trafficDelayInSeconds", 0)
            traffic = f", including {minutes_words(delay)} of traffic delay right now" if delay >= 60 else ", traffic is light right now"
            return f"{end['label']}: {minutes_words(summary['travelTimeInSeconds'])} by car, {miles(summary['lengthInMeters'])}{traffic}."
        data = self.get(OSRM_URL.format(a=f"{start['lon']},{start['lat']}", b=f"{end['lon']},{end['lat']}"))
        if data.get("code") != "Ok" or not data.get("routes"):
            raise RuntimeError(f"No driving route to {destination}.")
        route = data["routes"][0]
        return f"{end['label']}: about {minutes_words(route['duration'])} by car without traffic, {miles(route['distance'])}."


def build_module(cfg, storage):
    router = Router(cfg, storage)

    @tool("directions", "Drive time and distance to a place (address, business or town), from home unless origin is given; includes live "
          "traffic delay when traffic data is set up.", {"type": "object", "properties": {"destination": {"type": "string"}, "origin": {"type": "string"}},
                                                          "required": ["destination"]})
    async def directions(args):
        try:
            return text_result(await asyncio.to_thread(router.route, args["destination"], args.get("origin", "")))
        except (RuntimeError, OSError, KeyError) as e:
            return text_result(f"Couldn't get directions: {e}", error=True)

    return ToolModule(server="directions", prompt=PROMPT, tools=[
        ToolSpec(fn=directions, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="the map service")])
