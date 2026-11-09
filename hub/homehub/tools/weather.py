import asyncio
import json
import urllib.parse
import urllib.request

from claude_agent_sdk import tool

from homehub.household import household_facts
from homehub.tools import Scope, ToolModule, ToolSpec, current_context, text_result

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 8
PROMPT = ("For weather questions (now, today, tonight, tomorrow, this week, rain, temperature, wind) use get_weather, never a web "
          "search; leave place empty for the household's town. Say temperatures as plain numbers with the unit word, like "
          "'fifty-four degrees'.")
US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california", "co": "colorado", "ct": "connecticut",
    "de": "delaware", "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland", "ma": "massachusetts", "mi": "michigan",
    "mn": "minnesota", "ms": "mississippi", "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada", "nh": "new hampshire",
    "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina", "nd": "north dakota", "oh": "ohio",
    "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina", "sd": "south dakota",
    "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}
CONDITIONS = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy", 45: "foggy", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 56: "freezing drizzle", 57: "heavy freezing drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
    96: "thunderstorms with hail", 99: "severe thunderstorms with hail",
}
UNITS = {
    "imperial": {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch", "temp": "F", "wind": "mph",
                 "rain": "in"},
    "metric": {"temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm", "temp": "C", "wind": "km/h", "rain": "mm"},
}
_places = {}


def get_json(url, params):
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": "homehub"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def describe(code):
    return CONDITIONS.get(int(code), "mixed conditions")


def place_label(result):
    parts = [result.get("name", "")]
    region = result.get("admin1") or result.get("country") or ""
    if region and region != parts[0]:
        parts.append(region)
    return ", ".join(p for p in parts if p)


def find_place(query):
    key = " ".join(query.lower().split())
    if key in _places:
        return _places[key]
    name, _, rest = query.partition(",")
    hints = [US_STATES.get(h.strip().lower(), h.strip().lower()) for h in rest.split(",") if h.strip()]
    results = get_json(GEOCODE_URL, {"name": name.strip(), "count": 10, "language": "en", "format": "json"}).get("results") or []
    if hints:
        matching = [r for r in results if all(any(h == str(r.get(f, "")).lower() for f in ("admin1", "admin2", "country", "country_code"))
                                              for h in hints)]
        results = matching or results
    if not results:
        return None
    best = results[0]
    place = {"label": place_label(best), "latitude": best["latitude"], "longitude": best["longitude"]}
    _places[key] = place
    return place


def forecast(place, units="imperial", days=3):
    u = UNITS.get(units, UNITS["imperial"])
    data = get_json(FORECAST_URL, {
        "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto", "forecast_days": max(1, min(int(days), 7)),
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
        "temperature_unit": u["temperature_unit"], "wind_speed_unit": u["wind_speed_unit"], "precipitation_unit": u["precipitation_unit"],
    })
    return data, u


def format_report(place, data, u):
    now = data["current"]
    lines = [f"{place['label']} right now: {round(now['temperature_2m'])}°{u['temp']} (feels like {round(now['apparent_temperature'])}), "
             f"{describe(now['weather_code'])}, wind {round(now['wind_speed_10m'])} {u['wind']}."]
    daily = data["daily"]
    names = ["Today", "Tomorrow"]
    for i, day in enumerate(daily["time"]):
        label = names[i] if i < len(names) else day
        chance = daily["precipitation_probability_max"][i]
        lines.append(f"{label} ({day}): {describe(daily['weather_code'][i])}, high {round(daily['temperature_2m_max'][i])}, "
                     f"low {round(daily['temperature_2m_min'][i])}, chance of rain {chance if chance is not None else 'unknown'}%, "
                     f"rain {daily['precipitation_sum'][i]} {u['rain']}, wind up to {round(daily['wind_speed_10m_max'][i])} {u['wind']}.")
    return "\n".join(lines)


def build_module(storage):
    @tool("get_weather", "Current weather and the daily forecast (high, low, conditions, chance of rain, wind) from Open-Meteo. place is a "
          "town like 'Springfield, Illinois' or 'Paris, France'; leave it empty for the household's town. days is how many days of "
          "forecast, 1 to 7 (default 2: today and tomorrow).",
          {"type": "object", "properties": {"place": {"type": "string"}, "days": {"type": "integer"}}})
    async def get_weather(args):
        query = (args.get("place") or "").strip() or household_facts(storage).get("town", "")
        if not query:
            return text_result("No place given and the household town isn't set in people.toml; ask which town.", error=True)
        ctx = current_context.get()
        person = storage.person(ctx.person_id) if getattr(ctx, "person_id", None) else None
        units = (person or {}).get("units") or "imperial"
        try:
            place = await asyncio.to_thread(find_place, query)
            if place is None:
                return text_result(f"I couldn't find a place called {query}.", error=True)
            data, u = await asyncio.to_thread(forecast, place, units, args.get("days") or 2)
        except Exception as e:
            return text_result(f"The weather service didn't answer: {type(e).__name__}: {e}", error=True)
        return text_result(format_report(place, data, u))

    return ToolModule(server="weather", prompt=PROMPT, tools=[
        ToolSpec(fn=get_weather, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="the weather service"),
    ])
