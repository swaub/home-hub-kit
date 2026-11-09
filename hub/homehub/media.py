import re
import secrets
import shutil
import threading
import time
import urllib.request
from pathlib import Path

VIDEO = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg"}
AUDIO = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".alac", ".wma"}
SUBTITLES = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"}
ART = {".jpg", ".jpeg", ".png", ".nfo"}
KINDS = {"movie": ("Movies", VIDEO | SUBTITLES | ART), "show": ("Shows", VIDEO | SUBTITLES | ART), "music": ("Music", AUDIO | ART)}
MAX_CHUNK = 32 * 1024 * 1024
STALE_SECONDS = 24 * 3600
EPISODE = re.compile(r"[Ss](\d{1,2})[ ._-]*[Ee](\d{1,3})|(\d{1,2})x(\d{2,3})")


class MediaError(RuntimeError):
    pass


def clean(text, limit=120):
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit].rstrip(" .")


def season_of(file_name, given=None):
    if given not in (None, ""):
        season = int(given)
    else:
        match = EPISODE.search(file_name)
        if not match:
            return None
        season = int(match.group(1) or match.group(3))
    if not 0 <= season <= 99:
        raise MediaError("The season has to be a number from 0 to 99.")
    return season


def target_for(kind, file_name, title="", year=None, season=None, album=""):
    if kind not in KINDS:
        raise MediaError("Choose movie, show or music.")
    folder, allowed = KINDS[kind]
    name = clean(Path(str(file_name)).name, 200)
    suffix = Path(name).suffix.lower()
    if not name or suffix not in allowed:
        raise MediaError(f"{Path(str(file_name)).name or 'That file'} isn't a {kind} file Jellyfin can use.")
    if kind == "movie":
        title = clean(title) or clean(Path(name).stem)
        if year not in (None, ""):
            year = int(year)
            if not 1880 <= year <= 2100:
                raise MediaError("The year doesn't look right.")
            base = f"{title} ({year})"
        else:
            base = title
        if suffix in VIDEO:
            file_part = base + suffix
        elif suffix in SUBTITLES:
            extra = re.search(r"\.([a-z]{2,3})\.(?:srt|ass|ssa|vtt|sub|idx)$", name.lower())
            file_part = base + (f".{extra.group(1)}" if extra else "") + suffix
        else:
            file_part = name
        return Path(folder) / base / file_part
    if kind == "show":
        title = clean(title)
        if not title:
            raise MediaError("Give the show's name.")
        number = season_of(name, season)
        if number is None:
            raise MediaError(f"I can't tell which season {name} is from; name it like S01E02 or choose the season.")
        return Path(folder) / title / f"Season {number:02d}" / name
    artist = clean(title)
    parts = [folder] + ([artist] if artist else []) + ([clean(album)] if clean(album) else [])
    return Path(*parts) / name


class MediaLibrary:
    def __init__(self, cfg, jellyfin=None):
        self.cfg = cfg
        self.jellyfin = jellyfin
        self.lock = threading.Lock()
        self.uploads = {}

    @property
    def root(self):
        return Path(self.cfg.media.folder)

    def require(self, who):
        if not self.cfg.media.folder:
            raise MediaError("Uploads aren't set up on the hub ([media] folder in config.toml).")
        if not who or who.get("role") not in ("owner", "adult"):
            raise MediaError("Only adults in the household can add to Jellyfin.")

    def overview(self, who=None):
        self.require(who)
        if not self.root.is_dir():
            raise MediaError("The Jellyfin media disk isn't reachable right now.")
        usage = shutil.disk_usage(self.root)
        libraries = []
        for kind, (folder, _) in KINDS.items():
            base = self.root / folder
            entries = sorted((p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.stat().st_mtime, reverse=True) if base.is_dir() else []
            libraries.append({"kind": kind, "folder": folder, "count": len(entries), "recent": [p.name for p in entries[:8]]})
        return {"free": usage.free, "total": usage.total, "libraries": libraries, "max_chunk": MAX_CHUNK,
                "uploads": [{"id": u, "name": v["target"].name, "size": v["size"], "received": v["received"]} for u, v in self.uploads.items()]}

    def start(self, body, who):
        self.require(who)
        size = int(body.get("size") or 0)
        if size <= 0:
            raise MediaError("That file is empty.")
        relative = target_for(str(body.get("kind", "")), body.get("name", ""), body.get("title", ""), body.get("year"), body.get("season"), body.get("album", ""))
        target = self.root / relative
        if not target.resolve().is_relative_to(self.root.resolve()):
            raise MediaError("That name isn't allowed.")
        if shutil.disk_usage(self.root).free < size + 2 * 2**30:
            raise MediaError("There isn't enough space on the Jellyfin disk for that file.")
        exists = target.exists() and target.stat().st_size == size
        if exists:
            return {"id": None, "done": True, "path": str(relative), "text": f"{relative.name} is already there."}
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(f".{target.name}.part")
        received = part.stat().st_size if part.exists() and part.stat().st_size <= size else 0
        if received == 0:
            part.write_bytes(b"")
        upload_id = secrets.token_urlsafe(12)
        with self.lock:
            self.prune()
            self.uploads[upload_id] = {"target": target, "part": part, "size": size, "received": received, "person": who.get("person"),
                                       "relative": relative, "touched": time.time()}
        return {"id": upload_id, "done": False, "received": received, "path": str(relative)}

    def prune(self):
        now = time.time()
        for upload_id in [u for u, v in self.uploads.items() if now - v["touched"] > STALE_SECONDS]:
            self.uploads.pop(upload_id)

    def upload(self, upload_id, who):
        self.require(who)
        upload = self.uploads.get(upload_id)
        if upload is None or upload["person"] != who.get("person"):
            raise MediaError("That upload isn't open any more; start it again and it picks up where it stopped.")
        return upload

    def write(self, upload_id, offset, stream, length, who):
        upload = self.upload(upload_id, who)
        if length <= 0 or length > MAX_CHUNK:
            raise MediaError("Send the file in pieces of up to 32 MB.")
        with self.lock:
            if offset != upload["received"]:
                return {"received": upload["received"]}
            if offset + length > upload["size"]:
                raise MediaError("That's more than the file's size.")
            with open(upload["part"], "r+b") as f:
                f.seek(offset)
                left = length
                while left:
                    chunk = stream.read(min(1 << 20, left))
                    if not chunk:
                        raise MediaError("The upload was cut off; it resumes from where it stopped.")
                    f.write(chunk)
                    left -= len(chunk)
            upload["received"] = offset + length
            upload["touched"] = time.time()
            return {"received": upload["received"]}

    def finish(self, upload_id, who):
        upload = self.upload(upload_id, who)
        if upload["received"] != upload["size"] or upload["part"].stat().st_size != upload["size"]:
            raise MediaError("Not all of the file has arrived yet.")
        upload["part"].replace(upload["target"])
        self.uploads.pop(upload_id, None)
        return {"text": f"Added {upload['relative'].name}.", "path": str(upload["relative"])}

    def cancel(self, upload_id, who):
        upload = self.upload(upload_id, who)
        upload["part"].unlink(missing_ok=True)
        self.uploads.pop(upload_id, None)
        return {"text": "Upload cancelled."}

    def refresh(self):
        if self.jellyfin is None:
            return "Jellyfin will find the new files on its own shortly."
        settings = self.jellyfin()
        if not settings:
            return "Jellyfin will find the new files on its own shortly."
        request = urllib.request.Request(settings["url"] + "/Library/Refresh", method="POST", data=b"",
                                         headers={"Authorization": f'MediaBrowser Token="{settings["api_key"]}"'})
        try:
            urllib.request.urlopen(request, timeout=15).read()
        except OSError:
            return "Jellyfin will find the new files on its own shortly."
        return "Jellyfin is adding them now; they show up in a minute or two."

    def act(self, body, who=None):
        action = str(body.get("action", ""))
        if action == "start":
            return self.start(body, who)
        if action == "finish":
            return self.finish(str(body.get("id", "")), who)
        if action == "cancel":
            return self.cancel(str(body.get("id", "")), who)
        if action == "refresh":
            self.require(who)
            return {"text": self.refresh()}
        raise MediaError("unknown media action")


def jellyfin_settings(cfg):
    from homehub.tools.jellyfin import load_settings

    return lambda: load_settings(cfg)
