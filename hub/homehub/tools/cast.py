import asyncio
import re
import threading
import urllib.parse
import urllib.request

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

PROMPT = ("cast_youtube plays a YouTube video on the TV by search words or a YouTube link; cast_media plays a direct video or audio "
          "link; cast_control pauses, resumes or stops what's casting; cast_status says what's playing.")
SEARCH_URL = "https://www.youtube.com/results?search_query={query}"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")
TYPES = {".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm", ".m3u8": "application/x-mpegURL", ".mp3": "audio/mpeg",
         ".m4a": "audio/mp4", ".flac": "audio/flac", ".ogg": "audio/ogg"}
DISCOVER_SECONDS = 8


class CastError(RuntimeError):
    pass


def video_id(text):
    text = text.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    m = VIDEO_ID.search(text)
    return m.group(1) if m else None


def search_youtube(query):
    request = urllib.request.Request(SEARCH_URL.format(query=urllib.parse.quote(query)), headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US"})
    with urllib.request.urlopen(request, timeout=8) as response:
        page = response.read().decode("utf-8", "replace")
    for m in re.finditer(r'"videoRenderer":\{"videoId":"([A-Za-z0-9_-]{11})".{0,2000}?"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"', page):
        return m.group(1), m.group(2).encode().decode("unicode_escape", "ignore")
    raise CastError(f"YouTube found nothing for {query}.")


def content_type(url):
    path = urllib.parse.urlparse(url).path.lower()
    return next((kind for ext, kind in TYPES.items() if path.endswith(ext)), "video/mp4")


class Caster:
    def __init__(self, cfg):
        self.name = cfg.cast.device.strip()
        self.cast = None
        self.browser = None
        self.lock = threading.Lock()

    def device(self):
        if not self.name:
            raise CastError("Casting isn't set up: put the TV's Cast name under [cast] device in the config.")
        with self.lock:
            if self.cast is not None and self.cast.socket_client.is_connected:
                return self.cast
            import pychromecast

            if self.browser is not None:
                self.browser.stop_discovery()
            casts, self.browser = pychromecast.get_listed_chromecasts(friendly_names=[self.name], discovery_timeout=DISCOVER_SECONDS)
            if not casts:
                self.browser.stop_discovery()
                self.browser = None
                raise CastError(f"I can't find {self.name} on the network; is the TV on?")
            self.cast = casts[0]
            self.cast.wait(timeout=15)
            return self.cast

    def youtube(self, what):
        vid = video_id(what)
        title = None
        if vid is None:
            vid, title = search_youtube(what)
        from pychromecast.controllers.youtube import YouTubeController

        cast = self.device()
        yt = YouTubeController()
        cast.register_handler(yt)
        yt.play_video(vid)
        return f"Playing {title or 'that video'} on {self.name}."

    def media(self, url):
        if not urllib.parse.urlparse(url).scheme.startswith("http"):
            raise CastError("That isn't a web link I can cast.")
        cast = self.device()
        cast.media_controller.play_media(url, content_type(url))
        cast.media_controller.block_until_active(timeout=15)
        return f"Casting to {self.name}."

    def control(self, action):
        cast = self.device()
        mc = cast.media_controller
        actions = {"pause": mc.pause, "play": mc.play, "resume": mc.play, "stop": cast.quit_app}
        if action not in actions:
            raise CastError("Action must be pause, resume or stop.")
        actions[action]()
        return {"pause": "Paused.", "play": "Playing.", "resume": "Playing.", "stop": f"Stopped casting to {self.name}."}[action]

    def status(self):
        cast = self.device()
        app = cast.app_display_name or "nothing"
        media = cast.media_controller.status
        title = media.title if media and media.title else ""
        state = (media.player_state or "").lower() if media else ""
        if title:
            return f"{self.name} is running {app}: {title} ({state or 'idle'})."
        return f"{self.name} is running {app}."


def build_module(cfg, caster=None):
    caster = caster or Caster(cfg)

    def run(fn, key):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args.get(key, "")) if key else await asyncio.to_thread(fn))
            except (CastError, OSError) as e:
                return text_result(str(e), error=True)
        return call

    cast_youtube = tool("cast_youtube", "Play a YouTube video on the TV, by search words or a YouTube link.",
                        {"type": "object", "properties": {"what": {"type": "string"}}, "required": ["what"]})(run(caster.youtube, "what"))
    cast_media = tool("cast_media", "Play a direct video or audio link (mp4, mp3, m3u8 stream...) on the TV.",
                      {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]})(run(caster.media, "url"))
    cast_control = tool("cast_control", "Pause, resume or stop what's casting on the TV. action is pause, resume or stop.",
                        {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]})(run(caster.control, "action"))
    cast_status = tool("cast_status", "What app and media are playing on the TV's Cast receiver.", {"type": "object", "properties": {}})(run(caster.status, None))
    return ToolModule(server="cast", prompt=PROMPT, available=lambda c: bool(c.cast.device.strip()), tools=[
        ToolSpec(fn=cast_youtube, policy="auto", scope=Scope.household()),
        ToolSpec(fn=cast_media, policy="auto", scope=Scope.household()),
        ToolSpec(fn=cast_control, policy="auto", scope=Scope.household()),
        ToolSpec(fn=cast_status, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="the TV's Cast status"),
    ])
