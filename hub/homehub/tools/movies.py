import asyncio
import json
import tomllib
import urllib.parse
import urllib.request

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

API = "https://api.themoviedb.org/3"
REGION = "US"
TIMEOUT = 8
PROMPT = ("For movies and TV shows use movie_lookup (what it is, year, rating, runtime, plot), where_to_watch (which streaming "
          "services have it) and movies_now (trending, in theaters, or popular). Keep plots to one sentence.")
NOT_SET_UP = "Movie lookup isn't set up: add [tmdb] with api_key (a free TMDB key) to secrets.toml in the data folder."


def load_key(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return ""
    with open(path, "rb") as f:
        return str((tomllib.load(f).get("tmdb") or {}).get("api_key", "")).strip()


class Tmdb:
    def __init__(self, cfg):
        self.cfg = cfg

    def get(self, path, **params):
        key = load_key(self.cfg)
        if not key:
            raise RuntimeError(NOT_SET_UP)
        headers = {"Accept": "application/json"}
        if key.startswith("ey"):
            headers["Authorization"] = f"Bearer {key}"
        else:
            params["api_key"] = key
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))

    def find(self, title):
        results = [r for r in self.get("/search/multi", query=title, include_adult="false").get("results", []) if r.get("media_type") in ("movie", "tv")]
        if not results:
            raise RuntimeError(f"Nothing called {title} on TMDB.")
        return results[0]

    def lookup(self, title):
        hit = self.find(title)
        kind = hit["media_type"]
        info = self.get(f"/{kind}/{hit['id']}")
        name = info.get("title") or info.get("name")
        year = (info.get("release_date") or info.get("first_air_date") or "")[:4]
        rating = info.get("vote_average")
        if kind == "movie":
            length = f"{info['runtime']} min" if info.get("runtime") else ""
        else:
            length = f"{info.get('number_of_seasons', '?')} seasons"
        genres = ", ".join(g["name"] for g in info.get("genres", [])[:3])
        parts = [f"{name} ({year}, {'movie' if kind == 'movie' else 'TV show'})", genres, length, f"rated {rating:.1f}/10" if rating else ""]
        return "; ".join(p for p in parts if p) + (f". {info.get('overview', '')}" if info.get("overview") else "")

    def providers(self, title):
        hit = self.find(title)
        region = self.get(f"/{hit['media_type']}/{hit['id']}/watch/providers").get("results", {}).get(REGION, {})
        name = hit.get("title") or hit.get("name")
        lines = []
        for key, label in (("flatrate", "Streaming on"), ("free", "Free on"), ("ads", "Free with ads on"), ("rent", "Rent on"), ("buy", "Buy on")):
            names = [p["provider_name"] for p in region.get(key, [])[:6]]
            if names:
                lines.append(f"{label}: {', '.join(names)}")
        return f"{name}: " + ("; ".join(lines) if lines else "not available to stream in the US right now") + "."

    def now(self, which):
        paths = {"trending": "/trending/all/week", "theaters": "/movie/now_playing", "popular": "/movie/popular", "tv": "/tv/popular"}
        if which not in paths:
            raise RuntimeError("Choose trending, theaters, popular or tv.")
        params = {"region": REGION} if which == "theaters" else {}
        items = self.get(paths[which], **params).get("results", [])[:8]
        return "; ".join(f"{i.get('title') or i.get('name')} ({(i.get('release_date') or i.get('first_air_date') or '')[:4]})" for i in items)


def build_module(cfg):
    tmdb = Tmdb(cfg)

    def run(fn, key):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args.get(key) or ""))
            except (RuntimeError, OSError, KeyError) as e:
                return text_result(str(e), error=True)
        return call

    movie_lookup = tool("movie_lookup", "Look up a movie or TV show: year, genre, length, rating and a short plot.",
                        {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]})(run(tmdb.lookup, "title"))
    where_to_watch = tool("where_to_watch", "Which US streaming services have a movie or show (subscription, free, rent or buy).",
                          {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]})(run(tmdb.providers, "title"))
    movies_now = tool("movies_now", "What's trending this week, in theaters now, popular movies, or popular TV. which is trending, theaters, popular or tv.",
                      {"type": "object", "properties": {"which": {"type": "string"}}, "required": ["which"]})(run(tmdb.now, "which"))
    return ToolModule(server="movies", prompt=PROMPT, available=lambda c: bool(load_key(c)), tools=[
        ToolSpec(fn=fn, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="TMDB") for fn in (movie_lookup, where_to_watch, movies_now)])
