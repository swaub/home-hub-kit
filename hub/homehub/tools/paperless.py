import base64
import json
import re
import secrets
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TIMEOUT = 20
PAGE = 20
MAX_THUMB = 400_000
MAX_UPLOAD = 100 * 1024 * 1024
ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff", ".txt", ".docx", ".odt"}
NOT_SET_UP = "Paperless isn't set up: add [paperless] with url and token to secrets.toml in the data folder."


class PaperlessError(RuntimeError):
    pass


def load_settings(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        table = tomllib.load(f).get("paperless") or {}
    if not table.get("url") or not table.get("token"):
        return None
    url = str(table["url"]).rstrip("/")
    return {"url": url, "token": str(table["token"]), "web": str(table.get("web") or url).rstrip("/")}


def safe_name(name):
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", Path(str(name or "")).name).strip(" .")
    if not name or Path(name).suffix.lower() not in ALLOWED:
        raise PaperlessError("Paperless takes PDFs, photos, Word or text files.")
    return name[:150]


class PaperlessPanel:
    def __init__(self, cfg):
        self.cfg = cfg
        self.thumbs = {}
        self.names = {}
        self.lock = threading.Lock()

    def settings(self):
        settings = load_settings(self.cfg)
        if settings is None:
            raise PaperlessError(NOT_SET_UP)
        return settings

    def require(self, who):
        if not who or who.get("role") != "owner":
            raise PaperlessError("Only the owner's phone can see the documents.")

    def request(self, path, data=None, headers=None, image=False):
        settings = self.settings()
        request = urllib.request.Request(settings["url"] + path, data=data,
                                         headers={"Authorization": f"Token {settings['token']}", "Accept": "*/*" if image else "application/json",
                                                  **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT if data is None else 120) as response:
                kind = response.headers.get_content_type()
                raw = response.read(MAX_THUMB + 1) if image else response.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise PaperlessError("Paperless refused the hub's key.") from e
            raise PaperlessError(f"Paperless said {e.code}: {e.read()[:200].decode('utf-8', 'replace')}") from e
        except OSError as e:
            raise PaperlessError(f"Paperless didn't answer: {e}") from e
        if image:
            if len(raw) > MAX_THUMB or not kind.startswith("image/"):
                raise PaperlessError("Paperless sent something other than a preview.")
            return f"data:{kind};base64,{base64.b64encode(raw).decode()}"
        text = raw.decode("utf-8", "replace").strip()
        return json.loads(text) if text[:1] in ("{", "[", '"') else text

    def thumb(self, doc_id, modified):
        key = (doc_id, modified)
        with self.lock:
            if key in self.thumbs:
                return self.thumbs[key]
        try:
            image = self.request(f"/api/documents/{int(doc_id)}/thumb/", image=True)
        except PaperlessError:
            return None
        with self.lock:
            if len(self.thumbs) > 500:
                self.thumbs.clear()
            self.thumbs[key] = image
        return image

    def correspondents(self):
        found = self.request("/api/correspondents/?page_size=1000").get("results", [])
        return {c["id"]: c["name"] for c in found}

    def cards(self, results):
        names = self.correspondents() if any(d.get("correspondent") for d in results) else {}
        web = self.settings()["web"]
        return [{"id": d["id"], "title": d.get("title") or "Untitled", "created": d.get("created"), "added": d.get("added"),
                 "from": names.get(d.get("correspondent"), ""), "pages": d.get("page_count"), "thumb": self.thumb(d["id"], d.get("modified")),
                 "open": f"{web}/documents/{d['id']}/details", "hit": ((d.get("__search_hit__") or {}).get("highlights") or "")[:240]} for d in results]

    def overview(self, who=None):
        self.require(who)
        stats = self.request("/api/statistics/")
        recent = self.request(f"/api/documents/?ordering=-added&page_size={PAGE}").get("results", [])
        tasks = self.request("/api/tasks/?acknowledged=false")
        rows = tasks if isinstance(tasks, list) else tasks.get("results", []) if isinstance(tasks, dict) else []
        working = sum(1 for t in rows if str(t.get("status") or "").upper() in ("PENDING", "STARTED"))
        return {"total": stats.get("documents_total", 0), "inbox": stats.get("documents_inbox") or 0, "recent": self.cards(recent),
                "working": working, "web": self.settings()["web"]}

    def search(self, query):
        query = query.strip()[:200]
        if not query:
            return {"results": []}
        found = self.request("/api/documents/?" + urllib.parse.urlencode({"query": query, "page_size": PAGE})).get("results", [])
        return {"results": self.cards(found)}

    def upload(self, name, stream, length, who):
        self.require(who)
        name = safe_name(name)
        if length <= 0 or length > MAX_UPLOAD:
            raise PaperlessError("Documents up to 100 MB.")
        data = bytearray()
        while len(data) < length:
            piece = stream.read(min(1 << 20, length - len(data)))
            if not piece:
                raise PaperlessError("The upload was cut off; try again.")
            data.extend(piece)
        boundary = secrets.token_hex(16)
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{name}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + bytes(data) + f"\r\n--{boundary}--\r\n".encode()
        task = self.request("/api/documents/post_document/", data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return {"text": f"Sent {name} to Paperless; it shows up once it's read.", "task": task if isinstance(task, str) else ""}

    def task(self, task_id):
        if not re.fullmatch(r"[0-9a-f-]{36}", task_id or ""):
            raise PaperlessError("No such upload.")
        found = self.request(f"/api/tasks/?task_id={task_id}")
        rows = found if isinstance(found, list) else found.get("results", [])
        if not rows:
            return {"status": "PENDING"}
        row = rows[0]
        result = row.get("result_data") if isinstance(row.get("result_data"), dict) else {}
        linked = row.get("related_document_ids") or [row.get("related_document")]
        message = row.get("result") or result.get("error") or result.get("message") or ""
        return {"status": str(row.get("status") or "").upper(), "document": result.get("document_id") or linked[0], "message": str(message)[:300]}

    def act(self, body, who=None):
        self.require(who)
        action = str(body.get("action", ""))
        if action == "search":
            return self.search(str(body.get("query", "")))
        if action == "task":
            return self.task(str(body.get("id", "")))
        raise PaperlessError("unknown documents action")
