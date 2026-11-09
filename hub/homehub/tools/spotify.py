import asyncio
import base64
import json
import re
import secrets
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result
from homehub.tools.google_auth import write_private

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private"
TOKEN_FILE = "spotify-token.json"
TIMEOUT = 10
MY_PLAYLISTS_LIMIT = 500
PROMPT = ("Spotify: spotify_now says what's playing; spotify_play plays a song, artist, album or playlist (optionally on a named device); "
          "spotify_control pauses, resumes, skips, goes back, sets volume (0-100) or shuffle; spotify_devices lists devices and "
          "spotify_transfer moves playback to one. Control needs Spotify Premium.")
NOT_SET_UP = ("Spotify isn't set up: put client_id and client_secret under [spotify] in secrets.toml, then run "
              "'homehub spotify login' on the hub.")


class SpotifyError(RuntimeError):
    pass


def load_app(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        table = tomllib.load(f).get("spotify") or {}
    if not table.get("client_id") or not table.get("client_secret"):
        return None
    return {"client_id": str(table["client_id"]), "client_secret": str(table["client_secret"]),
            "redirect_uri": str(table.get("redirect_uri") or REDIRECT_URI)}


def token_path(cfg):
    return cfg.data_dir() / TOKEN_FILE


def ready(cfg):
    return load_app(cfg) is not None and token_path(cfg).is_file()


class Spotify:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()

    def app(self):
        app = load_app(self.cfg)
        if app is None:
            raise SpotifyError(NOT_SET_UP)
        return app

    def auth_url(self, state=None):
        app = self.app()
        return f"{AUTH_URL}?" + urllib.parse.urlencode({"client_id": app["client_id"], "response_type": "code", "redirect_uri": app["redirect_uri"],
                                                         "scope": SCOPES, "state": state or secrets.token_urlsafe(8)})

    def token_request(self, fields):
        app = self.app()
        basic = base64.b64encode(f"{app['client_id']}:{app['client_secret']}".encode()).decode()
        request = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                         headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise SpotifyError(f"Spotify refused the sign-in: {e.read().decode('utf-8', 'replace')[:200]}") from e

    def save(self, token):
        write_private(token_path(self.cfg), token)

    def login(self, pasted):
        text = pasted.strip()
        code = urllib.parse.parse_qs(urllib.parse.urlparse(text).query).get("code", [None])[0] if "://" in text else text
        if not code:
            raise SpotifyError("That link has no sign-in code; paste the whole address the browser ended on.")
        token = self.token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": self.app()["redirect_uri"]})
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
        self.save(token)
        return token

    def access_token(self):
        with self.lock:
            path = token_path(self.cfg)
            if not path.is_file():
                raise SpotifyError(NOT_SET_UP)
            token = json.loads(path.read_text(encoding="utf-8"))
            if time.time() < token.get("expires_at", 0):
                return token["access_token"]
            fresh = self.token_request({"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
            token.update(fresh)
            token["expires_at"] = time.time() + int(fresh.get("expires_in", 3600)) - 60
            self.save(token)
            return token["access_token"]

    def api(self, method, path, params=None, body=None):
        url = f"{API}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        data = json.dumps(body).encode() if body is not None else (b"" if method in ("PUT", "POST") else None)
        request = urllib.request.Request(url, data=data, method=method, headers={"Authorization": f"Bearer {self.access_token()}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
            try:
                return json.loads(raw) if raw.strip() else {}
            except ValueError:
                return {"text": raw.decode("utf-8", "replace").strip()}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code == 403 and "PREMIUM" in detail.upper():
                raise SpotifyError("Controlling playback needs Spotify Premium.") from e
            if e.code == 403 and "RESTRICTION" in detail.upper():
                raise SpotifyError("Spotify won't do that right now; it may already be paused or playing, or the track doesn't allow it.") from e
            if e.code == 404:
                raise SpotifyError("No Spotify device is active; open Spotify on your phone or TV first, or name a device.") from e
            raise SpotifyError(f"Spotify said {e.code}: {detail[:200]}") from e

    def devices(self):
        return self.api("GET", "/me/player/devices").get("devices", [])

    def device_id(self, name):
        if not name:
            return None
        wanted = re.sub(r"^(?:my|the|our)\s+", "", name.strip().lower())
        devices = self.devices()
        for d in devices:
            if wanted in d.get("name", "").lower():
                return d["id"]
        for d in devices:
            if wanted in d.get("type", "").lower() or (wanted == "tv" and d.get("type", "").lower() == "tv"):
                return d["id"]
        raise SpotifyError(f"No Spotify device called {name}. Devices: {', '.join(d['name'] for d in self.devices()) or 'none open right now'}.")

    def now(self):
        state = self.api("GET", "/me/player")
        if not state or not state.get("item"):
            return "Nothing is playing on Spotify."
        item = state["item"]
        artists = ", ".join(a["name"] for a in item.get("artists", [])) or item.get("show", {}).get("name", "")
        playing = "Playing" if state.get("is_playing") else "Paused"
        device = state.get("device") or {}
        volume = f" at {device['volume_percent']}% volume" if device.get("volume_percent") is not None else ""
        return f"{playing}: {item['name']}" + (f" by {artists}" if artists else "") + f" on {device.get('name', 'Spotify')}{volume}."

    def my_playlists(self):
        found, offset = [], 0
        while offset < MY_PLAYLISTS_LIMIT:
            page = self.api("GET", "/me/playlists", {"limit": 50, "offset": offset}) or {}
            items = [p for p in page.get("items", []) if p]
            found.extend(items)
            if not page.get("next") or not items:
                break
            offset += 50
        return found

    def own_playlist(self, query):
        wanted = " ".join(query.lower().split())
        mine = self.my_playlists()
        exact = [p for p in mine if " ".join(p.get("name", "").lower().split()) == wanted]
        loose = [p for p in mine if wanted and wanted in p.get("name", "").lower()]
        return (exact or loose or [None])[0]

    def play(self, query, kind="track", device=""):
        kind = kind if kind in ("track", "artist", "album", "playlist") else "track"
        own = self.own_playlist(query) if kind == "playlist" else None
        found = [own] if own else self.api("GET", "/search", {"q": query, "type": kind, "limit": 1}).get(f"{kind}s", {}).get("items", [])
        found = [f for f in found if f]
        if not found:
            raise SpotifyError(f"Spotify found no {kind} for {query}.")
        item = found[0]
        params = {"device_id": self.device_id(device)} if device else None
        body = {"uris": [item["uri"]]} if kind == "track" else {"context_uri": item["uri"]}
        self.api("PUT", "/me/player/play", params, body)
        by = f" by {item['artists'][0]['name']}" if kind in ("track", "album") and item.get("artists") else ""
        return f"Playing {item['name']}{by}."

    def control(self, action, value=None):
        actions = {"pause": ("PUT", "/me/player/pause", None), "resume": ("PUT", "/me/player/play", None), "next": ("POST", "/me/player/next", None),
                   "previous": ("POST", "/me/player/previous", None)}
        if action in actions:
            method, path, params = actions[action]
            self.api(method, path, params)
            return {"pause": "Paused.", "resume": "Playing.", "next": "Skipped.", "previous": "Went back."}[action]
        if action == "volume":
            level = max(0, min(100, int(value)))
            self.api("PUT", "/me/player/volume", {"volume_percent": level})
            return f"Spotify volume {level}%."
        if action == "shuffle":
            on = str(value).lower() in ("1", "true", "on", "yes")
            self.api("PUT", "/me/player/shuffle", {"state": "true" if on else "false"})
            return f"Shuffle {'on' if on else 'off'}."
        raise SpotifyError("Action must be pause, resume, next, previous, volume or shuffle.")

    def transfer(self, device):
        self.api("PUT", "/me/player", body={"device_ids": [self.device_id(device)], "play": True})
        return f"Moved playback to {device}."


def build_module(cfg, client=None):
    spotify = client or Spotify(cfg)

    def run(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except (SpotifyError, OSError, KeyError, ValueError) as e:
                return text_result(str(e), error=True)
        return call

    def devices(args):
        found = spotify.devices()
        if not found:
            return "No Spotify devices are open right now."
        return "; ".join(f"{d['name']} ({d['type'].lower()}{', active' if d.get('is_active') else ''})" for d in found)

    spotify_now = tool("spotify_now", "What's playing on Spotify right now and on which device.", {"type": "object", "properties": {}})(run(lambda a: spotify.now()))
    spotify_play = tool("spotify_play", "Play on Spotify by search: kind is track, artist, album or playlist (default track); device is optional.",
                        {"type": "object", "properties": {"query": {"type": "string"}, "kind": {"type": "string"}, "device": {"type": "string"}}, "required": ["query"]})(
        run(lambda a: spotify.play(a["query"], a.get("kind") or "track", a.get("device") or "")))
    spotify_control = tool("spotify_control", "Spotify playback: action is pause, resume, next, previous, volume (value 0-100) or shuffle (value on/off).",
                           {"type": "object", "properties": {"action": {"type": "string"}, "value": {"type": "string"}}, "required": ["action"]})(
        run(lambda a: spotify.control(a["action"], a.get("value"))))
    spotify_devices = tool("spotify_devices", "List the Spotify devices that are open right now (phone, TV, computer, speakers).",
                           {"type": "object", "properties": {}})(run(devices))
    spotify_transfer = tool("spotify_transfer", "Move Spotify playback to a named device.",
                            {"type": "object", "properties": {"device": {"type": "string"}}, "required": ["device"]})(run(lambda a: spotify.transfer(a["device"])))
    specs = [ToolSpec(fn=fn, policy="auto", scope=Scope.household()) for fn in (spotify_play, spotify_control, spotify_transfer)]
    specs += [ToolSpec(fn=fn, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="Spotify") for fn in (spotify_now, spotify_devices)]
    return ToolModule(server="spotify", prompt=PROMPT, available=ready, tools=specs)
