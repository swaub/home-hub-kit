import base64
import hashlib
import json
import os
import secrets
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT_URI = "http://127.0.0.1:8899/"
TIMEOUT = 10


def load_app(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        table = tomllib.load(f).get("google_tasks") or {}
    if not table.get("client_id") or not table.get("client_secret"):
        return None
    return {"client_id": str(table["client_id"]), "client_secret": str(table["client_secret"])}


def write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


class GoogleAuth:
    def __init__(self, cfg, scope, token_file, verifier_file, error, not_set_up, start_again):
        self.cfg = cfg
        self.scope = scope
        self.token_file = token_file
        self.verifier_file = verifier_file
        self.error = error
        self.not_set_up = not_set_up
        self.start_again = start_again
        self.lock = threading.Lock()

    def app(self):
        app = load_app(self.cfg)
        if app is None:
            raise self.error(self.not_set_up)
        return app

    def ready(self):
        return load_app(self.cfg) is not None and (self.cfg.data_dir() / self.token_file).is_file()

    def auth_url(self):
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        write_private(self.cfg.data_dir() / self.verifier_file, {"verifier": verifier})
        return f"{AUTH_URL}?" + urllib.parse.urlencode({"client_id": self.app()["client_id"], "redirect_uri": REDIRECT_URI, "response_type": "code",
                                                         "scope": self.scope, "access_type": "offline", "prompt": "consent",
                                                         "code_challenge": challenge, "code_challenge_method": "S256"})

    def token_request(self, fields):
        app = self.app()
        body = urllib.parse.urlencode({"client_id": app["client_id"], "client_secret": app["client_secret"], **fields}).encode()
        request = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise self.error(f"Google refused the sign-in: {e.read().decode('utf-8', 'replace')[:200]}") from e

    def login(self, pasted):
        text = pasted.strip()
        code = urllib.parse.parse_qs(urllib.parse.urlparse(text).query).get("code", [None])[0] if "://" in text else text
        if not code:
            raise self.error("That address has no sign-in code; paste the whole address the browser ended on.")
        verifier_path = self.cfg.data_dir() / self.verifier_file
        if not verifier_path.is_file():
            raise self.error(self.start_again)
        verifier = json.loads(verifier_path.read_text(encoding="utf-8"))["verifier"]
        token = self.token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
        write_private(self.cfg.data_dir() / self.token_file, token)
        verifier_path.unlink()
        return token

    def access_token(self):
        with self.lock:
            path = self.cfg.data_dir() / self.token_file
            if not path.is_file():
                raise self.error(self.not_set_up)
            token = json.loads(path.read_text(encoding="utf-8"))
            if time.time() < token.get("expires_at", 0):
                return token["access_token"]
            fresh = self.token_request({"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
            token.update(fresh)
            token["expires_at"] = time.time() + int(fresh.get("expires_in", 3600)) - 60
            write_private(path, token)
            return token["access_token"]
