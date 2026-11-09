import hashlib
import hmac
import secrets
import time

SIGNATURE_HEADER = "X-Home-Hub-Signature"
TIMESTAMP_HEADER = "X-Home-Hub-Timestamp"
NONCE_HEADER = "X-Home-Hub-Nonce"
REPLAY_WINDOW = 60.0


def new_key():
    return secrets.token_hex(32)


def body_digest(body):
    return hashlib.sha256(body or b"").hexdigest()


def signing_string(method, path, timestamp, nonce, body):
    return "\n".join((method.upper(), path, str(timestamp), nonce, body_digest(body)))


def sign(key, method, path, timestamp, nonce, body):
    return hmac.new(key.encode(), signing_string(method, path, timestamp, nonce, body).encode(), hashlib.sha256).hexdigest()


def signed_headers(key, method, path, body, now=None):
    timestamp = str(int(now if now is not None else time.time()))
    nonce = secrets.token_hex(8)
    return {SIGNATURE_HEADER: sign(key, method, path, timestamp, nonce, body), TIMESTAMP_HEADER: timestamp, NONCE_HEADER: nonce}


class ReplayGuard:
    def __init__(self, window=REPLAY_WINDOW):
        self.window = window
        self.seen = {}

    def check(self, nonce, now):
        for old, stamp in list(self.seen.items()):
            if now - stamp > self.window * 2:
                del self.seen[old]
        if nonce in self.seen:
            return False
        self.seen[nonce] = now
        return True


def verify(key, headers, method, path, body, guard, now=None):
    now = time.time() if now is None else now
    signature = headers.get(SIGNATURE_HEADER, "")
    timestamp = headers.get(TIMESTAMP_HEADER, "")
    nonce = headers.get(NONCE_HEADER, "")
    if not signature or not timestamp or not nonce:
        return False, "missing signature headers"
    try:
        sent = int(timestamp)
    except ValueError:
        return False, "bad timestamp"
    if abs(now - sent) > REPLAY_WINDOW:
        return False, "request too old or clock skew"
    expected = sign(key, method, path, timestamp, nonce, body)
    if not hmac.compare_digest(expected, signature):
        return False, "bad signature"
    if not guard.check(nonce, now):
        return False, "replayed request"
    return True, ""
