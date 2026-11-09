import base64
import json
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 15
PAGE = 24
MAX_THUMB = 400_000
MAX_PREVIEW = 4_000_000
NOT_SET_UP = "Immich isn't set up: add [immich] with url and api_key to secrets.toml in the data folder."


class ImmichError(RuntimeError):
    pass


def load_settings(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        table = tomllib.load(f).get("immich") or {}
    if not table.get("url") or not table.get("api_key"):
        return None
    url = str(table["url"]).rstrip("/")
    return {"url": url, "api_key": str(table["api_key"]), "web": str(table.get("web") or url).rstrip("/")}


def length(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = int(value // 1000)
    else:
        parts = [float(x) for x in str(value).split(":")]
        seconds = int(sum(x * 60 ** i for i, x in enumerate(reversed(parts))))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def tile(asset):
    return {"id": asset["id"], "video": asset.get("type") == "VIDEO", "taken": asset.get("fileCreatedAt"), "uploaded": asset.get("createdAt"),
            "favorite": bool(asset.get("isFavorite")), "duration": length(asset.get("duration")) if asset.get("type") == "VIDEO" else None}


class ImmichPanel:
    def __init__(self, cfg):
        self.cfg = cfg
        self.thumbs = {}
        self.lock = threading.Lock()

    def settings(self):
        settings = load_settings(self.cfg)
        if settings is None:
            raise ImmichError(NOT_SET_UP)
        return settings

    def require(self, who):
        if not who or who.get("role") != "owner":
            raise ImmichError("Only the owner's phone can see the photo library.")

    def request(self, path, body=None, limit=None):
        settings = self.settings()
        request = urllib.request.Request(settings["url"] + path, data=None if body is None else json.dumps(body).encode(),
                                         headers={"x-api-key": settings["api_key"], "Content-Type": "application/json", "Accept": "*/*"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                kind = response.headers.get_content_type()
                data = response.read() if limit is None else response.read(limit + 1)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ImmichError("Immich refused the hub's key.") from e
            raise ImmichError(f"Immich said {e.code}.") from e
        except OSError as e:
            raise ImmichError(f"Immich didn't answer: {e}") from e
        if limit is not None:
            if len(data) > limit or not kind.startswith(("image/", "video/")):
                raise ImmichError("Immich sent something other than a picture.")
            return f"data:{kind};base64,{base64.b64encode(data).decode()}"
        return json.loads(data or b"null")

    def thumb(self, asset_id):
        with self.lock:
            if asset_id in self.thumbs:
                return self.thumbs[asset_id]
        try:
            image = self.request(f"/api/assets/{urllib.parse.quote(asset_id, safe='')}/thumbnail?size=thumbnail", limit=MAX_THUMB)
        except ImmichError:
            return None
        with self.lock:
            if len(self.thumbs) > 1000:
                self.thumbs.clear()
            self.thumbs[asset_id] = image
        return image

    def page(self, number):
        found = self.request("/api/search/metadata", {"page": number, "size": PAGE, "order": "desc", "visibility": "timeline"})["assets"]
        items = []
        for asset in found["items"]:
            entry = tile(asset)
            entry["thumb"] = self.thumb(asset["id"])
            items.append(entry)
        return {"items": items, "next": found.get("nextPage")}

    def overview(self, who=None):
        self.require(who)
        stats = self.request("/api/server/statistics")
        disk = self.request("/api/server/storage")
        first = self.page(1)
        newest = first["items"][0] if first["items"] else None
        last_upload = max((i["uploaded"] for i in first["items"] if i["uploaded"]), default=None)
        return {"photos": stats.get("photos", 0), "videos": stats.get("videos", 0), "usage": stats.get("usage", 0),
                "disk": {"used": disk.get("diskUseRaw"), "size": disk.get("diskSizeRaw"), "free": disk.get("diskAvailableRaw")},
                "newest": newest["taken"] if newest else None, "last_upload": last_upload, "recent": first["items"], "next": first["next"],
                "web": self.settings()["web"]}

    def act(self, body, who=None):
        self.require(who)
        action = str(body.get("action", ""))
        if action == "more":
            number = int(body.get("page") or 2)
            if not 1 <= number <= 500:
                raise ImmichError("No such page.")
            return self.page(number)
        if action == "photo":
            asset_id = str(body.get("id", ""))
            if not asset_id.replace("-", "").isalnum():
                raise ImmichError("No such photo.")
            return {"id": asset_id, "image": self.request(f"/api/assets/{asset_id}/thumbnail?size=preview", limit=MAX_PREVIEW),
                    "open": f"{self.settings()['web']}/photos/{asset_id}"}
        raise ImmichError("unknown photos action")
