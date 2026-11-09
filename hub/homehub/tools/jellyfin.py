import asyncio
import base64
import json
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

TIMEOUT = 8
TYPES = "Movie,Series,Episode,MusicAlbum,Audio,MusicArtist"
PROMPT = ("Jellyfin is the home media server: jellyfin_search finds movies, shows and music in the library; jellyfin_play plays one "
          "on the TV (a show plays its next unwatched episode); jellyfin_continue lists what they were in the middle of; "
          "jellyfin_recent lists what was added lately.")
ROKU_CHANNEL = "592369"
POSTER_HEIGHT = 330
MAX_POSTER = 300_000
ROKU_KINDS = {"Movie": "movie", "Episode": "episode"}
NOT_SET_UP = "Jellyfin isn't set up: add [jellyfin] with url, api_key and user to secrets.toml in the data folder."


class JellyfinError(RuntimeError):
    pass


def load_settings(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        table = tomllib.load(f).get("jellyfin") or {}
    if not table.get("url") or not table.get("api_key"):
        return None
    return {"url": str(table["url"]).rstrip("/"), "api_key": str(table["api_key"]), "user": str(table.get("user", ""))}


def label(item):
    kind = item.get("Type", "")
    name = item.get("Name", "")
    if kind == "Episode":
        return f"{item.get('SeriesName', '')} season {item.get('ParentIndexNumber', '?')} episode {item.get('IndexNumber', '?')}, {name}"
    year = item.get("ProductionYear")
    if kind == "Audio":
        return f"{name} by {', '.join(item.get('Artists', [])[:2])}" if item.get("Artists") else name
    return f"{name} ({year})" if year and f"({year})" not in name else name


def stream_url(settings, item):
    base, key = settings["url"], settings["api_key"]
    if item["Type"] in ("Audio",):
        return f"{base}/Audio/{item['Id']}/universal?" + urllib.parse.urlencode({"ApiKey": key, "Container": "mp3,aac,m4a,flac", "AudioCodec": "mp3",
                                                                                    "TranscodingContainer": "mp3", "TranscodingProtocol": "http"})
    return f"{base}/Videos/{item['Id']}/master.m3u8?" + urllib.parse.urlencode({"ApiKey": key, "MediaSourceId": item["Id"], "VideoCodec": "h264",
                                                                                 "AudioCodec": "aac,mp3", "TranscodingMaxAudioChannels": 2})


class Jellyfin:
    def __init__(self, cfg, caster=None):
        self.cfg = cfg
        self.caster = caster
        self.user_id = None

    def settings(self):
        settings = load_settings(self.cfg)
        if settings is None:
            raise JellyfinError(NOT_SET_UP)
        return settings

    def get(self, path, **params):
        settings = self.settings()
        url = f"{settings['url']}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        request = urllib.request.Request(url, headers={"Authorization": f'MediaBrowser Token="{settings["api_key"]}"', "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise JellyfinError("Jellyfin rejected the API key.") from e
            raise JellyfinError(f"Jellyfin said {e.code}.") from e

    def user(self):
        if self.user_id is None:
            users = self.get("/Users") or []
            wanted = self.settings()["user"].lower()
            match = [u for u in users if u["Name"].lower() == wanted] or users
            if not match:
                raise JellyfinError("Jellyfin has no users yet; finish its setup first.")
            self.user_id = match[0]["Id"]
        return self.user_id

    def search(self, query, limit=8):
        return self.get("/Items", searchTerm=query, Recursive="true", IncludeItemTypes=TYPES, Limit=limit, userId=self.user()).get("Items", [])

    def recent(self):
        return self.get("/Items/Latest", userId=self.user(), Limit=10) or []

    def image(self, item_id, tag):
        settings = self.settings()
        url = f"{settings['url']}/Items/{item_id}/Images/Primary?" + urllib.parse.urlencode({"maxHeight": POSTER_HEIGHT, "quality": 80, "tag": tag})
        request = urllib.request.Request(url, headers={"Authorization": f'MediaBrowser Token="{settings["api_key"]}"'})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            kind = response.headers.get_content_type()
            data = response.read(MAX_POSTER + 1)
        if not kind.startswith("image/") or len(data) > MAX_POSTER:
            raise JellyfinError("Jellyfin sent something other than a poster.")
        return f"data:{kind};base64,{base64.b64encode(data).decode()}"

    def item(self, item_id):
        return self.get(f"/Items/{urllib.parse.quote(item_id, safe='')}", userId=self.user())

    def counts(self):
        counts = self.get("/Items/Counts", userId=self.user()) or {}
        return {"movies": counts.get("MovieCount", 0), "shows": counts.get("SeriesCount", 0), "episodes": counts.get("EpisodeCount", 0),
                "songs": counts.get("SongCount", 0)}

    def resume(self):
        return (self.get("/UserItems/Resume", userId=self.user(), Limit=10) or {}).get("Items", [])

    def playable(self, item):
        if item["Type"] == "Series":
            up = (self.get("/Shows/NextUp", userId=self.user(), seriesId=item["Id"], Limit=1) or {}).get("Items", [])
            if up:
                return up[0]
            episodes = (self.get(f"/Shows/{item['Id']}/Episodes", userId=self.user(), Limit=1) or {}).get("Items", [])
            if not episodes:
                raise JellyfinError(f"{item['Name']} has no episodes in the library.")
            return episodes[0]
        if item["Type"] in ("MusicAlbum", "MusicArtist"):
            scope = {"ParentId": item["Id"], "SortBy": "IndexNumber"} if item["Type"] == "MusicAlbum" else {"ArtistIds": item["Id"], "SortBy": "Random"}
            songs = self.get("/Items", IncludeItemTypes="Audio", Recursive="true", Limit=1, userId=self.user(), **scope).get("Items", [])
            if not songs:
                raise JellyfinError(f"No songs found for {item['Name']}.")
            return songs[0]
        return item

    def play(self, query):
        if self.caster is None:
            raise JellyfinError("Playing on the TV needs casting set up ([cast] device in the config).")
        found = self.search(query, 1)
        if not found:
            raise JellyfinError(f"Nothing called {query} in the Jellyfin library.")
        item = self.playable(found[0])
        self.caster.media(stream_url(self.settings(), item))
        return f"Playing {label(item)} on the TV."


def poster_source(item):
    if item.get("ImageTags", {}).get("Primary"):
        return item["Id"], item["ImageTags"]["Primary"]
    if item.get("SeriesId") and item.get("SeriesPrimaryImageTag"):
        return item["SeriesId"], item["SeriesPrimaryImageTag"]
    if item.get("AlbumId") and item.get("AlbumPrimaryImageTag"):
        return item["AlbumId"], item["AlbumPrimaryImageTag"]
    return None


def web_address(cfg, settings):
    for entry in cfg.remote.apps:
        name, _, url = entry.partition("=")
        if name.strip().lower().startswith("jellyfin") and url.strip():
            return url.strip().rstrip("/")
    return settings["url"]


class JellyfinPanel:
    def __init__(self, cfg, caster=None, roku=None, client=None):
        self.cfg = cfg
        self.caster = caster
        self.roku = roku
        self.jellyfin = client or Jellyfin(cfg, caster)
        self.posters = {}
        self.lock = threading.Lock()

    def poster(self, item):
        source = poster_source(item)
        if source is None:
            return None
        with self.lock:
            if source in self.posters:
                return self.posters[source]
        try:
            image = self.jellyfin.image(*source)
        except (JellyfinError, OSError):
            return None
        with self.lock:
            if len(self.posters) > 400:
                self.posters.clear()
            self.posters[source] = image
        return image

    def card(self, item):
        kind = item.get("Type", "")
        data = item.get("UserData") or {}
        if kind == "Episode":
            title, detail = item.get("SeriesName", ""), f"S{item.get('ParentIndexNumber', 0):02d}E{item.get('IndexNumber', 0):02d} · {item.get('Name', '')}"
        elif kind == "Audio":
            title, detail = item.get("Name", ""), ", ".join(item.get("Artists", [])[:2])
        else:
            title, detail = item.get("Name", ""), str(item.get("ProductionYear") or "")
        minutes = round(item["RunTimeTicks"] / 600_000_000) if item.get("RunTimeTicks") else None
        return {"id": item["Id"], "kind": kind.lower(), "title": title, "detail": detail, "minutes": minutes,
                "progress": round(data["PlayedPercentage"]) if data.get("PlayedPercentage") else 0, "watched": bool(data.get("Played")),
                "poster": self.poster(item), "rating": item.get("OfficialRating") or "", "score": item.get("CommunityRating")}

    def targets(self):
        found = []
        if self.roku is not None and self.cfg.roku.devices:
            found.append({"id": "roku", "name": self.roku.roku().name})
        if self.caster is not None:
            found.append({"id": "cast", "name": self.cfg.cast.device})
        return found

    def overview(self, who=None):
        settings = self.jellyfin.settings()
        return {"resume": [self.card(i) for i in self.jellyfin.resume()], "latest": [self.card(i) for i in self.jellyfin.recent()],
                "counts": self.jellyfin.counts(), "web": web_address(self.cfg, settings), "targets": self.targets()}

    def details(self, item_id):
        item = self.jellyfin.item(item_id)
        view = self.card(item)
        view.update({"overview": item.get("Overview", ""), "genres": item.get("Genres", [])[:4], "year": item.get("ProductionYear")})
        if item.get("Type") == "Series":
            nxt = self.jellyfin.playable(item)
            view["next"] = {"id": nxt["Id"], "label": f"S{nxt.get('ParentIndexNumber', 0):02d}E{nxt.get('IndexNumber', 0):02d} · {nxt.get('Name', '')}"}
        return view

    def play(self, item_id, target):
        item = self.jellyfin.playable(self.jellyfin.item(item_id))
        if target == "roku":
            if self.roku is None or not self.cfg.roku.devices:
                raise JellyfinError("No Roku is set up.")
            kind = ROKU_KINDS.get(item.get("Type"))
            if kind is None:
                raise JellyfinError("The Roku's Jellyfin app plays movies and episodes; cast music to the TV instead.")
            roku = self.roku.roku()
            roku.call(f"/launch/{ROKU_CHANNEL}?" + urllib.parse.urlencode({"contentId": item["Id"], "mediaType": kind}), "POST")
            return {"text": f"Playing {label(item)} on the {roku.name}."}
        if target == "cast":
            if self.caster is None:
                raise JellyfinError("Casting isn't set up ([cast] device in the config).")
            self.caster.media(stream_url(self.jellyfin.settings(), item))
            return {"text": f"Playing {label(item)} on {self.cfg.cast.device}."}
        raise JellyfinError("Choose where to play it.")

    def act(self, body, who=None):
        action = str(body.get("action", ""))
        if action == "search":
            query = str(body.get("query", "")).strip()[:100]
            if not query:
                return {"results": []}
            return {"results": [self.card(i) for i in self.jellyfin.search(query, 12)]}
        if action == "details":
            return self.details(str(body.get("id", "")))
        if action == "play":
            return self.play(str(body.get("id", "")), str(body.get("target", "")))
        if action == "stop":
            if body.get("target") == "cast" and self.caster is not None:
                return {"text": self.caster.control("stop")}
            if body.get("target") == "roku" and self.roku is not None:
                roku = self.roku.roku()
                roku.press("Home")
                return {"text": f"Stopped on the {roku.name}."}
            raise JellyfinError("Nothing to stop there.")
        raise JellyfinError("unknown Jellyfin action")


def build_module(cfg, caster=None, client=None):
    jellyfin = client or Jellyfin(cfg, caster)

    def run(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except (JellyfinError, OSError, KeyError) as e:
                return text_result(str(e), error=True)
        return call

    def listing(items, empty):
        return "; ".join(f"{label(i)} [{i.get('Type', '').lower()}]" for i in items) if items else empty

    jellyfin_search = tool("jellyfin_search", "Search the Jellyfin library for movies, shows, episodes, albums, songs or artists.",
                           {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})(
        run(lambda a: listing(jellyfin.search(a["query"]), f"Nothing in the library matches {a['query']}.")))
    jellyfin_play = tool("jellyfin_play", "Play a movie, show (next unwatched episode), album, artist or song from Jellyfin on the TV.",
                         {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})(run(lambda a: jellyfin.play(a["query"])))
    jellyfin_continue = tool("jellyfin_continue", "What they were in the middle of watching in Jellyfin.", {"type": "object", "properties": {}})(
        run(lambda a: listing(jellyfin.resume(), "Nothing is in progress.")))
    jellyfin_recent = tool("jellyfin_recent", "What was added to the Jellyfin library lately.", {"type": "object", "properties": {}})(
        run(lambda a: listing(jellyfin.recent(), "Nothing new in the library.")))
    return ToolModule(server="jellyfin", prompt=PROMPT, available=lambda c: load_settings(c) is not None, tools=[
        ToolSpec(fn=fn, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="the Jellyfin library")
        for fn in (jellyfin_search, jellyfin_play, jellyfin_continue, jellyfin_recent)])
