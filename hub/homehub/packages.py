import base64
import html
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime

from homehub.storage import now_iso
from homehub.tools.google_auth import GoogleAuth

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TOKEN_FILE = "gmail-token.json"
VERIFIER_FILE = "gmail-login.json"
TIMEOUT = 15
NOT_SET_UP = ("Gmail isn't connected for package tracking: the [google_tasks] app in secrets.toml needs the Gmail API turned on, then "
              "connect it from the Packages page or with 'homehub packages login'.")
FIRST_DAYS = 30
LATER_DAYS = 4
DELIVERED_SHOW_DAYS = 3
QUERY = ('newer_than:{days}d -category:promotions -category:social (subject:(shipped OR delivered OR "out for delivery" OR "on the way" '
         'OR "on its way" OR ordered OR "order confirmed" OR arriving OR "delivery" OR "shipment" OR "package") OR "tracking number")')
SKIP_SENDERS = ("doordash.com", "ubereats.com", "uber.com", "grubhub.com", "postmates.com", "instacart.com", "gopuff.com", "seamless.com",
                "members.ebay.com")
SKIP_SUBJECTS = re.compile(r"you made the sale|sent a message about|your item sold|offer|refund|return (label|started)|review", re.I)
RANK = {"ordered": 0, "label": 1, "shipped": 2, "out_for_delivery": 3, "delivered": 4, "problem": 3, "tracking": 1}
LABELS = {"ordered": "Ordered", "label": "Label created", "shipped": "On the way", "out_for_delivery": "Out for delivery",
          "delivered": "Delivered", "problem": "Problem", "tracking": "Tracking"}
SUBJECT_STATUS = [
    ("delivered", re.compile(r"\bdelivered\b", re.I)),
    ("out_for_delivery", re.compile(r"out for delivery", re.I)),
    ("problem", re.compile(r"delivery (attempt|exception|problem)|delayed|\bdelay\b|unable to deliver|returned to sender|action required", re.I)),
    ("shipped", re.compile(r"\bshipped\b|on (the|its) way|in transit|shipment|has shipped|arriving", re.I)),
    ("label", re.compile(r"label (created|printed)|shipping label", re.I)),
    ("ordered", re.compile(r"\bordered\b|order (#\S+ )?(is )?(confirmed|received|placed)|thanks for your order|order confirmation", re.I)),
]
CARRIERS = [
    ("UPS", re.compile(r"\b1Z[0-9A-Z]{16}\b")),
    ("Amazon", re.compile(r"\bTBA\d{12}\b")),
    ("USPS", re.compile(r"\b(?:420\d{5,9})?(9[2345]\d{18,24})\b|\b[A-Z]{2}\d{9}US\b")),
    ("FedEx", re.compile(r"(?i:fedex)[^\n]{0,60}?\b(\d{12}|\d{15}|\d{20})\b")),
    ("DHL", re.compile(r"(?i:dhl)[^\n]{0,60}?\b(\d{10})\b")),
    ("AliExpress", re.compile(r"\b(?:YT|LP|UB|SF|CN|AE)\d{10,18}\b")),
]
TRACK_LINKS = {"UPS": "https://www.ups.com/track?tracknum={n}", "USPS": "https://tools.usps.com/go/TrackConfirmAction?tLabels={n}",
               "FedEx": "https://www.fedex.com/fedextrack/?trknbr={n}", "DHL": "https://www.dhl.com/us-en/home/tracking.html?tracking-id={n}",
               "Amazon": "https://www.amazon.com/gp/your-account/order-history", "AliExpress": "https://t.17track.net/en#nums={n}"}
ETA = re.compile(r"(?:arriving|estimated delivery(?: date)?|expected (?:delivery|by|to arrive)|delivery (?:date|by)|get it)\s*:?\s*"
                 r"([A-Za-z][^\n.!]{1,48})", re.I)
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ORDER = re.compile(r"order\s*(?:#|number|no\.?)\s*:?\s*#?\s*([0-9][0-9A-Z-]{3,30})", re.I)
AMAZON_SUBJECT = re.compile(r"^(?:Ordered|Shipped|Out for delivery|Delivered|Arriving[^:]*|Delivery update)\s*:?\s*(?:\d+\s+items?\s*:)?\s*(.+)$", re.I)


class PackageError(RuntimeError):
    pass


def detect_carrier(text):
    for carrier, pattern in CARRIERS:
        found = pattern.search(text or "")
        if found:
            number = next((g for g in found.groups() if g), None) or found.group(0)
            return carrier, number.upper()
    return "", ""


def track_link(carrier, number, fallback=""):
    template = TRACK_LINKS.get(carrier)
    if carrier == "Amazon" and fallback:
        return fallback
    return template.format(n=urllib.parse.quote(number)) if template and number else fallback


def subject_status(subject):
    for status, pattern in SUBJECT_STATUS:
        if pattern.search(subject or ""):
            return status
    return ""


def resolve_eta(text, sent):
    found = ETA.search(text or "")
    if not found:
        return ""
    raw = re.sub(r"\s+", " ", found.group(1)).strip(" ,;:-")
    lower = raw.lower()
    day = None
    if lower.startswith("today"):
        day, raw = sent, raw[5:]
    elif lower.startswith("tomorrow"):
        day, raw = sent + timedelta(days=1), raw[8:]
    elif lower.split(" ")[0].rstrip(",") in WEEKDAYS:
        target = WEEKDAYS.index(lower.split(" ")[0].rstrip(","))
        day, raw = sent + timedelta(days=(target - sent.weekday()) % 7), raw.split(" ", 1)[1] if " " in raw else ""
    if day is None:
        return raw[:40]
    return (f"{day:%a, %b} {day.day}" + (f" {raw.strip(' ,')}" if raw.strip(" ,") else "")).strip()[:48]


def first_link(body, host):
    found = re.search(rf"https?://[^\s)\"'>]*{re.escape(host)}[^\s)\"'>]*", body or "")
    return found.group(0) if found else ""


def shop_item(body):
    found = re.search(r"Items in this shipment\s*-*\s*\n+\s*([^\n]{2,80})", body or "")
    return found.group(1).strip() if found else ""


def parse_email(sender, subject, body, sent):
    name, address = parseaddr(sender or "")
    domain = address.rsplit("@", 1)[-1].lower()
    if any(domain == d or domain.endswith("." + d) for d in SKIP_SENDERS) or SKIP_SUBJECTS.search(subject or ""):
        return None
    status = subject_status(subject)
    if not status:
        return None
    text = f"{subject}\n{body or ''}"
    carrier, number = detect_carrier(text)
    eta = resolve_eta(body, sent) if status in ("shipped", "out_for_delivery", "label", "ordered") else ""
    is_amazon = domain == "amazon.com" or domain.endswith(".amazon.com")
    order = ORDER.search(text)
    order_ref = order.group(1).rstrip("-") if order else ""
    if is_amazon:
        matched = AMAZON_SUBJECT.match((subject or "").strip())
        item = matched.group(1).strip() if matched else subject
        merchant = "Amazon"
        if not order_ref:
            found = re.search(r"orderId=([0-9-]{10,})", body or "")
            order_ref = found.group(1) if found else ""
        key = f"amazon:{order_ref}" if order_ref else (f"tracking:{number}" if number else "")
        url = first_link(body, "amazon.com/progress-tracker") or (f"https://www.amazon.com/gp/your-account/order-details?orderID={order_ref}" if order_ref else "")
        carrier = carrier or "Amazon"
    else:
        item = shop_item(body) or re.sub(r"^(re|fwd?):\s*", "", subject or "", flags=re.I).strip()
        merchant = (name or (domain.split(".")[-2].title() if "." in domain else domain)).strip().strip('"')
        if number and number in item.upper().replace(" ", ""):
            item = f"{merchant} package"
        if number:
            key = f"tracking:{number}"
        elif order_ref:
            key = f"order:{domain}:{order_ref}"
        else:
            return None
        url = track_link(carrier, number)
    if not key:
        return None
    return {"key": key, "carrier": carrier, "tracking": number, "order_ref": order_ref, "merchant": merchant[:60], "item": item[:120],
            "status": status, "eta": eta, "url": url, "updated": sent.isoformat(timespec="seconds")}


def merge(old, update, owner_id):
    if old is None:
        row = {**update, "owner_id": owner_id, "source": "email", "first_seen": now_iso(), "hidden": 0, "delivered_at": None}
    else:
        row = dict(old)
        newer = update["updated"] >= old["updated"]
        for field in ("carrier", "tracking", "order_ref", "merchant", "item", "url", "eta"):
            if update.get(field) and (newer or not row.get(field)):
                row[field] = update[field]
        if newer and (RANK.get(update["status"], 0) >= RANK.get(old["status"], 0) or update["status"] == "problem" or old["status"] == "problem"):
            row["status"] = update["status"]
        row["updated"] = max(old["updated"], update["updated"])
    if row["status"] == "delivered" and not row.get("delivered_at"):
        row["delivered_at"] = update["updated"]
        row["eta"] = ""
    return row


def html_text(raw):
    raw = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", raw)
    raw = re.sub(r"(?i)<a [^>]*href=\"([^\"]+)\"[^>]*>", r" \1 ", raw)
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))


def message_body(payload):
    plain, rich = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime in ("text/plain", "text/html"):
            text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            (plain if mime == "text/plain" else rich).append(text)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    return "\n".join(plain) if plain else html_text("\n".join(rich))


class Gmail:
    def __init__(self, cfg):
        self.auth = GoogleAuth(cfg, SCOPE, TOKEN_FILE, VERIFIER_FILE, PackageError, NOT_SET_UP,
                               "Start the Gmail connection again so the link and the code match.")

    def ready(self):
        return self.auth.ready()

    def get(self, path, params=None):
        url = f"{GMAIL}{path}" + (f"?{urllib.parse.urlencode(params, doseq=True)}" if params else "")
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.auth.access_token()}"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code == 403 and ("accessNotConfigured" in detail or "has not been used" in detail or "is disabled" in detail):
                raise PackageError("The Gmail API is turned off for the hub's Google app; turn it on in Google Cloud, then try again.") from e
            raise PackageError(f"Gmail said {e.code}: {detail}") from e

    def search(self, query, limit=100):
        return [m["id"] for m in self.get("/messages", {"q": query, "maxResults": limit}).get("messages", [])]

    def message(self, message_id):
        data = self.get(f"/messages/{message_id}", {"format": "full"})
        headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
        try:
            sent = parsedate_to_datetime(headers.get("date", "")).astimezone().replace(tzinfo=None)
        except (TypeError, ValueError):
            sent = datetime.fromtimestamp(int(data.get("internalDate", "0")) / 1000)
        return headers.get("from", ""), headers.get("subject", ""), message_body(data.get("payload", {})), sent


class PackageTracker:
    def __init__(self, cfg, storage, owner_id, emit=None, notify=None, gmail=None):
        self.cfg = cfg
        self.storage = storage
        self.owner_id = owner_id
        self.emit = emit or (lambda name, **payload: None)
        self.notify = notify
        self.gmail = gmail or Gmail(cfg)
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.last_scan = None
        self.last_error = ""
        self.thread = None

    def apply(self, update):
        old = self.storage.package(update["key"])
        if old is not None and old.get("owner_id") != self.owner_id:
            return None
        row = merge(old, update, self.owner_id)
        self.storage.save_package(row)
        changed = old is None or old["status"] != row["status"]
        if changed:
            self.emit("package.changed", key=row["key"], status=row["status"], carrier=row["carrier"], merchant=row["merchant"],
                      item=row["item"], eta=row["eta"], person_id=self.owner_id)
            recent = datetime.fromisoformat(row["updated"]) > datetime.now() - timedelta(days=1)
            if self.notify is not None and recent and row["status"] in ("out_for_delivery", "delivered", "problem"):
                self.notify(f"{LABELS[row['status']]}: {row['item'] or row['merchant']}",
                            " · ".join(p for p in (row["merchant"], row["carrier"] if row["carrier"] != row["merchant"] else "", row["eta"]) if p),
                            row["url"])
        return row

    def scan(self, days=None):
        with self.lock:
            try:
                if not self.gmail.ready():
                    raise PackageError(NOT_SET_UP)
                days = days or (LATER_DAYS if self.last_scan else FIRST_DAYS)
                found = 0
                for message_id in reversed(self.gmail.search(QUERY.format(days=days))):
                    if self.storage.package_message_seen(message_id):
                        continue
                    update = parse_email(*self.gmail.message(message_id))
                    if update is not None:
                        self.apply(update)
                        found += 1
                    self.storage.mark_package_message(message_id)
                self.last_scan, self.last_error = datetime.now(), ""
                return found
            except (PackageError, OSError, ValueError, KeyError) as e:
                self.last_error = str(e)
                raise

    def add(self, number, item=""):
        number = re.sub(r"[\s-]", "", number or "").upper()
        carrier, found = detect_carrier(number)
        number = found or number
        if not re.fullmatch(r"[A-Z0-9]{8,40}", number):
            raise PackageError(f"{number!r} doesn't look like a tracking number")
        return self.apply({"key": f"tracking:{number}", "carrier": carrier, "tracking": number, "order_ref": "", "merchant": "",
                           "item": " ".join(str(item).split())[:120], "status": "tracking", "eta": "",
                           "url": track_link(carrier, number) or TRACK_LINKS["AliExpress"].format(n=number),
                           "updated": now_iso()})

    def hide(self, key):
        return self.storage.hide_package(key, self.owner_id) > 0

    def current(self):
        cutoff = (datetime.now() - timedelta(days=DELIVERED_SHOW_DAYS)).isoformat(timespec="seconds")
        rows = [r for r in self.storage.packages(self.owner_id) if r["status"] != "delivered" or (r["delivered_at"] or r["updated"]) >= cutoff]
        rows.sort(key=lambda r: r["updated"], reverse=True)
        rows.sort(key=lambda r: (r["status"] == "delivered", -RANK.get(r["status"], 0)))
        return [{**r, "label": LABELS.get(r["status"], r["status"])} for r in rows]

    def state(self):
        return {"ready": self.gmail.ready(), "packages": self.current(), "error": self.last_error,
                "last_scan": self.last_scan.isoformat(timespec="seconds") if self.last_scan else None}

    def connect_url(self):
        return self.gmail.auth.auth_url()

    def connect(self, address):
        self.gmail.auth.login(address)
        return self.scan(days=FIRST_DAYS)

    def describe(self):
        rows = self.current()
        if not rows:
            return "No packages on the way." + ("" if self.gmail.ready() else f" {NOT_SET_UP}")
        lines = []
        for r in rows:
            what = r["item"] or "a package"
            source = f" from {r['merchant']}" if r["merchant"] and r["merchant"] not in what else ""
            when = f", expected {r['eta']}" if r["eta"] and r["status"] != "delivered" else ""
            via = f" by {r['carrier']}" if r["carrier"] and r["carrier"] != r["merchant"] else ""
            lines.append(f"{what}{source}: {r['label'].lower()}{via}{when}" + (f" [tracking {r['tracking']}]" if r["tracking"] else ""))
        return "; ".join(lines) + "."

    def start(self, minutes):
        def loop():
            while not self.stopping.is_set():
                try:
                    if self.gmail.ready():
                        self.scan()
                except Exception as e:
                    self.emit("service.warning", message=f"package scan: {type(e).__name__}: {e}")
                self.stopping.wait(minutes * 60)

        self.thread = threading.Thread(target=loop, name="packages", daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stopping.set()
