import asyncio
import base64
import difflib
import io
import re
import shutil
import subprocess
import time
from pathlib import Path

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

ADB_CANDIDATES = (Path.home() / "Android/Sdk/platform-tools/adb", Path("/usr/lib/android-sdk/platform-tools/adb"), Path("/opt/android-sdk/platform-tools/adb"))
APP_NAMES = {
    "com.discord": "Discord", "com.google.android.apps.messaging": "Messages", "com.samsung.android.messaging": "Samsung Messages",
    "com.whatsapp": "WhatsApp", "com.google.android.gm": "Gmail", "com.microsoft.office.outlook": "Outlook", "com.microsoft.teams": "Teams",
    "com.instagram.android": "Instagram", "com.snapchat.android": "Snapchat", "com.facebook.orca": "Messenger", "com.facebook.katana": "Facebook",
    "com.twitter.android": "X", "org.telegram.messenger": "Telegram", "com.slack": "Slack", "com.google.android.youtube": "YouTube",
    "com.spotify.music": "Spotify", "com.google.android.calendar": "Calendar", "com.samsung.android.calendar": "Calendar",
    "com.android.shell": "Test", "com.google.android.dialer": "Phone", "com.samsung.android.dialer": "Phone", "com.reddit.frontpage": "Reddit",
    "com.amazon.mShop.android.shopping": "Amazon", "com.ebay.mobile": "eBay",
}
SYSTEM_PACKAGES = ("android", "com.android.systemui", "com.samsung.android.app.routines", "com.google.android.gms", "com.samsung.android.lool",
                   "com.android.vending")
APP_ALIASES = {
    "camera": ["com.sec.android.app.camera", "com.google.android.GoogleCamera"],
    "messages": ["com.samsung.android.messaging", "com.google.android.apps.messaging"],
    "texts": ["com.samsung.android.messaging", "com.google.android.apps.messaging"],
    "phone": ["com.samsung.android.dialer", "com.google.android.dialer"], "dialer": ["com.samsung.android.dialer", "com.google.android.dialer"],
    "contacts": ["com.samsung.android.app.contacts", "com.google.android.contacts"],
    "settings": ["com.android.settings"], "gallery": ["com.sec.android.gallery3d"], "photos": ["com.google.android.apps.photos"],
    "files": ["com.sec.android.app.myfiles", "com.google.android.apps.nbu.files"], "my files": ["com.sec.android.app.myfiles"],
    "youtube": ["app.morphe.android.youtube", "app.revanced.android.youtube", "com.google.android.youtube"],
    "youtube music": ["app.morphe.android.apps.youtube.music", "app.revanced.android.apps.youtube.music", "com.google.android.apps.youtube.music"],
    "maps": ["com.google.android.apps.maps"], "calendar": ["com.samsung.android.calendar", "com.google.android.calendar"],
    "clock": ["com.sec.android.app.clockpackage", "com.google.android.deskclock"],
    "calculator": ["com.sec.android.app.popupcalculator", "com.google.android.calculator"],
    "play store": ["com.android.vending"], "chrome": ["com.android.chrome"], "internet": ["com.sec.android.app.sbrowser"],
    "health": ["com.sec.android.app.shealth"], "gemini": ["com.google.android.apps.bard"], "claude": ["com.anthropic.claude"],
}
GENERIC_TOKENS = ("com", "android", "app", "apps", "google", "samsung", "sec", "client", "mobile")
KEYCODES = {"home": "KEYCODE_HOME", "back": "KEYCODE_BACK", "recents": "KEYCODE_APP_SWITCH", "recent apps": "KEYCODE_APP_SWITCH",
            "enter": "KEYCODE_ENTER", "wake": "KEYCODE_WAKEUP", "sleep": "KEYCODE_SLEEP", "screen off": "KEYCODE_SLEEP",
            "volume up": "KEYCODE_VOLUME_UP", "volume down": "KEYCODE_VOLUME_DOWN", "play pause": "KEYCODE_MEDIA_PLAY_PAUSE",
            "next": "KEYCODE_MEDIA_NEXT", "previous": "KEYCODE_MEDIA_PREVIOUS", "delete": "KEYCODE_DEL", "search": "KEYCODE_SEARCH"}
SETTINGS_PAGES = {
    "wifi": "android.settings.WIFI_SETTINGS", "wi-fi": "android.settings.WIFI_SETTINGS", "bluetooth": "android.settings.BLUETOOTH_SETTINGS",
    "battery": "android.intent.action.POWER_USAGE_SUMMARY", "display": "android.settings.DISPLAY_SETTINGS", "sound": "android.settings.SOUND_SETTINGS",
    "notifications": "android.settings.NOTIFICATION_SETTINGS", "apps": "android.settings.APPLICATION_SETTINGS",
    "storage": "android.settings.INTERNAL_STORAGE_SETTINGS", "location": "android.settings.LOCATION_SOURCE_SETTINGS",
    "about": "android.settings.DEVICE_INFO_SETTINGS", "about phone": "android.settings.DEVICE_INFO_SETTINGS", "date": "android.settings.DATE_SETTINGS",
    "security": "android.settings.SECURITY_SETTINGS", "accessibility": "android.settings.ACCESSIBILITY_SETTINGS",
    "developer options": "android.settings.APPLICATION_DEVELOPMENT_SETTINGS", "hotspot": "android.settings.TETHER_SETTINGS",
    "airplane mode": "android.settings.AIRPLANE_MODE_SETTINGS", "data usage": "android.settings.DATA_USAGE_SETTINGS", "settings": "android.settings.SETTINGS",
}
BATTERY_STATUS = {"2": "charging", "3": "discharging", "4": "not charging", "5": "full"}
NO_PHONE = ("No phone connected. Tell the user: on the phone open Settings > Developer options > Wireless debugging and turn it on (phone and "
            "this computer must be on the same network, and the phone must have been paired with this computer once).")
LOCKED = "The phone is locked. Ask the user to unlock it first (it can't be unlocked remotely)."
PROMPT = ("The adb tools read and operate the user's Android phone over wireless debugging when it's connected: status and battery, "
          "notifications, screenshots, opening apps and Settings pages, reading and tapping what's on screen, typing, keys, Do Not Disturb, "
          "ringing it to find it. Use them only for things that need the phone itself.")


def app_name(pkg):
    if pkg in APP_NAMES:
        return APP_NAMES[pkg]
    if "youtube.music" in pkg:
        return "YouTube Music"
    return pkg.split(".")[-1].replace("_", " ").title()


def parse_notifications(dump, now_ms=None):
    now_ms = now_ms or time.time() * 1000
    section = dump.split("Notification List:", 1)[-1]
    section = re.split(r"\n  \S", section, maxsplit=1)[0]
    items = []
    for block in section.split("NotificationRecord(")[1:]:
        pkg = (re.search(r"pkg=(\S+)", block) or [None, ""])[1]
        flags = (re.search(r"\n\s+flags=(\S+)", block) or [None, ""])[1]
        fields = {k: v for k, v in re.findall(r"android\.(title|text|bigText|conversationTitle|subText)=\w+ \((.*)\)\s*$", block, re.M)}
        when = re.search(r"when=(\d+)", block)
        posted = int(when.group(1)) if when else None
        items.append({"app": app_name(pkg), "pkg": pkg, "title": fields.get("conversationTitle") or fields.get("title", ""),
                      "text": fields.get("bigText") or fields.get("text", ""),
                      "ongoing": any(f in flags for f in ("ONGOING_EVENT", "FOREGROUND_SERVICE", "NO_CLEAR")),
                      "media": "category=transport" in block, "summary": "GROUP_SUMMARY" in flags, "system": pkg in SYSTEM_PACKAGES,
                      "minutes_ago": max(0, round((now_ms - posted) / 60000)) if posted else None})
    return items


def ago(minutes):
    if minutes is None:
        return ""
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 1440:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


def notifications_text(items, want=""):
    want = (want or "").lower()
    media = [i for i in items if i["media"] and (i["title"] or i["text"])]
    shown = [i for i in items if not (i["ongoing"] or i["summary"] or i["system"] or i["media"]) and (i["title"] or i["text"])
             and (not want or any(want in i[k].lower() for k in ("app", "pkg", "title", "text")))]
    shown.sort(key=lambda i: i["minutes_ago"] if i["minutes_ago"] is not None else 1e9)
    lines = [f"{i['app']}: {i['title']}" + (f" - {i['text'][:300]}" if i["text"] else "") + f" ({ago(i['minutes_ago'])})" for i in shown[:25]]
    if media and not want:
        lines.append("Now playing: " + "; ".join(f"{m['title']} - {m['text']} ({m['app']})" for m in media))
    return "\n".join(lines) if lines else "No notifications" + (f" matching '{want}'." if want else "."), len(shown)


def find_phone_app(query, packages):
    q = re.sub(r"[^a-z0-9 ]", "", query.lower()).strip()
    for pkg in APP_ALIASES.get(q, []):
        if pkg in packages:
            return pkg
    compact = q.replace(" ", "")
    scored = []
    for pkg in packages:
        tokens = [t for t in re.split(r"[._]", pkg.lower()) if t not in GENERIC_TOKENS]
        if compact in tokens:
            scored.append((3, pkg))
        elif any(compact in t or (len(t) > 3 and t in compact) for t in tokens):
            scored.append((2, pkg))
    if scored:
        return sorted(scored, key=lambda x: (-x[0], len(x[1])))[0][1]
    names = {pkg: re.split(r"[._]", pkg)[-1].lower() for pkg in packages}
    close = difflib.get_close_matches(compact, list(names.values()), n=1, cutoff=0.75)
    return next((pkg for pkg, n in names.items() if close and n == close[0]), None)


def adb_text(text):
    out = []
    for ch in text:
        if ch == " ":
            out.append("%s")
        elif ch.isalnum() or ch in ".,@-_:/":
            out.append(ch)
        else:
            out.append("\\" + ch)
    return "".join(out)


def parse_ui_tree(raw):
    import xml.etree.ElementTree as ET

    raw = raw[raw.find("<?xml"):raw.rfind(">") + 1] if "<?xml" in raw else ""
    nodes, bars = [], []
    if not raw:
        return nodes, bars

    def walk(el, clickable_parent):
        clickable = el.get("clickable") == "true" or clickable_parent
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds", ""))
        label = (el.get("text") or el.get("content-desc") or "").strip()
        resource = (el.get("resource-id") or "").lower()
        if m:
            x1, y1, x2, y2 = map(int, m.groups())
            if x2 > x1 and y2 > y1:
                if label:
                    nodes.append({"label": label, "x": (x1 + x2) // 2, "y": (y1 + y2) // 2, "clickable": clickable, "search": "search" in resource})
                if "search" in resource and "layout" in (el.get("class") or "").lower():
                    bars.append((x1, y1, x2, y2))
        for child in el:
            walk(child, clickable)

    walk(ET.fromstring(raw), False)
    return nodes, bars


def parse_ui_dump(raw):
    return parse_ui_tree(raw)[0]


def obstructed(node, bars, height):
    if node["y"] > height * 0.92:
        return True
    return any(x1 <= node["x"] <= x2 and y1 <= node["y"] <= y2 for x1, y1, x2, y2 in bars)


def match_node(nodes, query):
    q = query.strip().lower()
    exact = [n for n in nodes if n["label"].lower() == q]
    starts = [n for n in nodes if n["label"].lower().startswith(q)]
    contains = [n for n in nodes if q in n["label"].lower()]
    for group in (exact, starts, contains):
        if group:
            return sorted(group, key=lambda n: (n["search"], not n["clickable"], n["y"]))[0]
    return None


def screen_labels(nodes, limit=18):
    labels = [n["label"] for n in sorted(nodes, key=lambda n: (n["y"], n["x"]))]
    return ", ".join(labels[:limit]) + (" ..." if len(labels) > limit else "")


def parse_battery(raw):
    fields = dict(re.findall(r"^\s*([A-Za-z ]+):\s*(.+)$", raw, re.M))
    temp = fields.get("temperature", "").strip()
    return {"level": fields.get("level", "?").strip(), "status": BATTERY_STATUS.get(fields.get("status", "").strip(), "unknown"),
            "temperature_c": int(temp) / 10 if temp.isdigit() else None}


def format_status(model, version, battery, storage, uptime):
    return (f"Model: {model}\nAndroid: {version}\nBattery: {battery['level']}% ({battery['status']})"
            + (f", {battery['temperature_c']:.1f} C" if battery["temperature_c"] is not None else "")
            + f"\nStorage (/data): {storage}\nUptime: {uptime}")


def connected_serials(devices_output):
    return re.findall(r"^(\S+)\s+device\b", devices_output, re.M)


def pairing_addresses(mdns_output):
    return re.findall(r"_adb-tls-pairing\._tcp\.?\s+(\d+\.\d+\.\d+\.\d+:\d+)", mdns_output)


def connect_addresses(mdns_output):
    return re.findall(r"_adb-tls-connect\._tcp\.?\s+(\d+\.\d+\.\d+\.\d+:\d+)", mdns_output)


class Adb:
    def __init__(self, path=None):
        self.path = path
        self.screen_scale = 1.0

    def exe(self):
        if self.path:
            return self.path if Path(self.path).exists() else None
        found = shutil.which("adb")
        if found:
            return found
        return next((str(p) for p in ADB_CANDIDATES if p.exists()), None)

    def available(self):
        return self.exe() is not None

    def run(self, args, serial=None, timeout=15):
        exe = self.exe()
        if not exe:
            raise RuntimeError("adb (Android platform-tools) is not installed.")
        try:
            out = subprocess.run([exe, *(["-s", serial] if serial else []), *args], capture_output=True, text=True, timeout=timeout,
                                 encoding="utf-8", errors="replace")
            return (out.stdout + out.stderr).strip()
        except subprocess.TimeoutExpired as e:
            return str(e)
        except FileNotFoundError:
            raise RuntimeError("adb (Android platform-tools) is not installed.")

    def raw(self, args, serial=None, timeout=20):
        return subprocess.run([self.exe(), "-s", serial, *args], capture_output=True, timeout=timeout).stdout

    def discover(self):
        for address in connect_addresses(self.run(["mdns", "services"])):
            self.run(["connect", address], timeout=8)
        return self.run(["devices", "-l"])

    def serial(self, wanted=None):
        connected = connected_serials(self.run(["devices"]))
        if not connected:
            self.discover()
            connected = connected_serials(self.run(["devices"]))
        if not connected:
            return None
        return wanted if wanted in connected else connected[0]

    def locked(self, serial):
        return "isKeyguardShowing=true" in self.run(["shell", "dumpsys", "window"], serial).replace(" ", "")

    def packages(self, serial):
        raw = self.run(["shell", "cmd", "package", "query-activities", "--brief", "-a", "android.intent.action.MAIN", "-c",
                        "android.intent.category.LAUNCHER"], serial)
        return sorted({line.strip().split("/")[0] for line in raw.splitlines() if "/" in line})

    def nodes(self, serial):
        return self.layout(serial)[0]

    def layout(self, serial):
        return parse_ui_tree(self.run(["exec-out", "uiautomator", "dump", "/dev/tty"], serial, timeout=20))

    def screen_size(self, serial):
        m = re.search(r"(\d+)x(\d+)", self.run(["shell", "wm", "size"], serial))
        return (int(m.group(1)), int(m.group(2))) if m else (1080, 2340)

    def tap(self, serial, x, y, long=False):
        if long:
            self.run(["shell", "input", "swipe", str(x), str(y), str(x), str(y), "700"], serial)
        else:
            self.run(["shell", "input", "tap", str(x), str(y)], serial)

    def screenshot(self, serial):
        from PIL import Image

        img = Image.open(io.BytesIO(self.raw(["exec-out", "screencap", "-p"], serial))).convert("RGB")
        full_width = img.width
        img.thumbnail((1568, 1568))
        self.screen_scale = img.width / full_width
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return base64.b64encode(buf.getvalue()).decode()

    def zen(self, serial):
        return self.run(["shell", "settings", "get", "global", "zen_mode"], serial).strip()


def build_module(adb=None, screen_only=False):
    adb = adb or Adb()

    async def on_phone(work, wanted=None, unlock=True):
        def run_it():
            serial = adb.serial(wanted)
            if not serial:
                return NO_PHONE
            if unlock and adb.locked(serial):
                return LOCKED
            return work(serial)

        try:
            return text_result(await asyncio.to_thread(run_it))
        except RuntimeError as e:
            return text_result(str(e), error=True)

    @tool("android_devices", "List Android phones/tablets connected over ADB (USB or wireless debugging).", {"type": "object", "properties": {}})
    async def android_devices(args):
        try:
            out = await asyncio.to_thread(adb.discover)
        except RuntimeError as e:
            return text_result(str(e), error=True)
        if not connected_serials(out):
            out += "\n" + NO_PHONE + " Or use android_connect with the IP:port shown on that screen."
        return text_result(out)

    @tool("android_connect", "Connect to an Android device's wireless debugging address (ip:port shown in its Wireless debugging settings).",
          {"type": "object", "properties": {"address": {"type": "string"}}, "required": ["address"]})
    async def android_connect(args):
        try:
            return text_result(await asyncio.to_thread(adb.run, ["connect", args["address"]]))
        except RuntimeError as e:
            return text_result(str(e), error=True)

    @tool("android_pair", "Pair with an Android device for wireless debugging (first time only). Ask the user to open Developer options > "
          "Wireless debugging > Pair device with pairing code and keep that screen open, then give the 6-digit code. The pairing address is "
          "found automatically while that screen is open; only pass address if the user gives it. Pass only the 6 digits of the code.",
          {"type": "object", "properties": {"code": {"type": "string"}, "address": {"type": "string"}}, "required": ["code"]})
    async def android_pair(args):
        code = "".join(re.findall(r"\d", args["code"]))
        if len(code) != 6:
            return text_result(f"The code '{args['code']}' isn't 6 digits. Ask the user for the 6-digit code again.", error=True)

        def pair():
            address = (args.get("address") or "").replace(" ", "")
            if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+:\d+", address):
                found = pairing_addresses(adb.run(["mdns", "services"]))
                if not found:
                    return ("Couldn't find a phone waiting to pair. Ask the user to open Wireless debugging > "
                            "Pair device with pairing code on the phone and keep that screen open, on the same network as this computer.")
                address = found[0]
            out = adb.run(["pair", address, code], timeout=20)
            if "Successfully paired" in out:
                out += "\n" + adb.discover()
            return f"Used code {' '.join(code)} at {address}.\n{out}"

        try:
            return text_result(await asyncio.to_thread(pair))
        except RuntimeError as e:
            return text_result(str(e), error=True)

    @tool("android_screenshot", "Take a screenshot of the connected Android phone's screen to see what's on it.",
          {"type": "object", "properties": {"serial": {"type": "string"}}})
    async def android_screenshot(args):
        def capture():
            serial = adb.serial(args.get("serial"))
            return adb.screenshot(serial) if serial else None

        try:
            data = await asyncio.to_thread(capture)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            return text_result(f"Couldn't capture the phone screen: {e}", error=True)
        if data is None:
            return text_result(NO_PHONE, error=True)
        return {"content": [{"type": "image", "data": data, "mimeType": "image/jpeg"}]}

    @tool("phone_notifications", "Read the notifications currently on the user's Android phone (messages, Discord, email, etc.), newest first, "
          "plus what's playing. Optional filter: an app ('Discord') or a person/word ('Mom'). Summarize them aloud briefly: who and what, "
          "not every word.", {"type": "object", "properties": {"filter": {"type": "string"}}})
    async def phone_notifications(args):
        def work(serial):
            items = parse_notifications(adb.run(["shell", "dumpsys", "notification", "--noredact"], serial, timeout=20))
            return notifications_text(items, args.get("filter") or args.get("app"))[0]

        return await on_phone(work, unlock=False)

    @tool("phone_open_app", "Open an app on the user's Android phone by name (e.g. 'Discord', 'camera', 'Spotify', 'settings').",
          {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
    async def phone_open_app(args):
        def work(serial):
            pkg = find_phone_app(args["name"], adb.packages(serial))
            if not pkg:
                return f"No app on the phone matches '{args['name']}'."
            if pkg == "com.android.settings":
                adb.run(["shell", "am", "start", "--activity-clear-task", "-a", SETTINGS_PAGES["settings"]], serial)
                time.sleep(1.2)
                return f"Opened the main Settings page on the phone. On screen: {screen_labels(adb.nodes(serial))}"
            adb.run(["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"], serial)
            return f"Opened {pkg} on the phone."

        def wake_first(serial):
            adb.run(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], serial)
            if adb.locked(serial):
                return LOCKED
            return work(serial)

        return await on_phone(wake_first, unlock=False)

    @tool("phone_open_settings", "Open a phone Settings page directly (fastest, most reliable): " + ", ".join(sorted(SETTINGS_PAGES)) +
          ". Returns what's on that page.", {"type": "object", "properties": {"page": {"type": "string"}}, "required": ["page"]})
    async def phone_open_settings(args):
        def work(serial):
            action = SETTINGS_PAGES.get(args["page"].lower().strip())
            if not action:
                return f"Unknown settings page. Choose from: {', '.join(sorted(SETTINGS_PAGES))}."
            adb.run(["shell", "am", "start", "--activity-clear-task", "-a", action], serial)
            time.sleep(1.2)
            return f"Opened {args['page']} settings. On screen: {screen_labels(adb.nodes(serial))}"

        return await on_phone(work)

    @tool("phone_screen_text", "Read all the text and labeled buttons currently on the phone screen, top to bottom. Faster and more exact "
          "than a screenshot when you only need words; use android_screenshot for pictures/layout.", {"type": "object", "properties": {}})
    async def phone_screen_text(args):
        def work(serial):
            nodes = adb.nodes(serial)
            return "\n".join(n["label"] for n in sorted(nodes, key=lambda n: (n["y"], n["x"]))) or "No text on screen."

        return await on_phone(work)

    @tool("phone_tap_text", "Tap a button, menu item or link on the phone by its visible text or label (e.g. 'Battery', 'Send', 'Wi-Fi'). "
          "Scrolls down to find it if needed. Prefer this over screenshot + phone_tap whenever the thing has a name.",
          {"type": "object", "properties": {"text": {"type": "string"}, "long": {"type": "boolean"}}, "required": ["text"]})
    async def phone_tap_text(args):
        def work(serial):
            w, h = adb.screen_size(serial)
            seen = None
            for attempt in range(6):
                nodes, bars = adb.layout(serial)
                node = match_node(nodes, args["text"])
                if node and not node["search"] and obstructed(node, bars, h):
                    adb.run(["shell", "input", "swipe", str(w // 2), str(int(h * 0.7)), str(w // 2), str(int(h * 0.4)), "300"], serial)
                    time.sleep(0.5)
                    nodes, bars = adb.layout(serial)
                    node = match_node(nodes, args["text"]) or node
                if node:
                    adb.tap(serial, node["x"], node["y"], bool(args.get("long")))
                    time.sleep(0.8)
                    return (f"Tapped '{node['label']}'" + (f" after scrolling {attempt} time(s)" if attempt else "") +
                            f". Now on screen: {screen_labels(adb.nodes(serial))}")
                labels = [n["label"] for n in nodes]
                if labels == seen:
                    break
                seen = labels
                adb.run(["shell", "input", "swipe", str(w // 2), str(int(h * 0.75)), str(w // 2), str(int(h * 0.3)), "300"], serial)
                time.sleep(0.4)
            return f"Couldn't find '{args['text']}' on the screen. Visible: " + ", ".join(seen or [])[:800]

        return await on_phone(work)

    @tool("phone_tap", "Tap the phone screen at x,y, pixel coordinates in the most recent android_screenshot image (take one first). long=true "
          "for a long press.", {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "long": {"type": "boolean"}},
                                "required": ["x", "y"]})
    async def phone_tap(args):
        def work(serial):
            x, y = int(args["x"] / adb.screen_scale), int(args["y"] / adb.screen_scale)
            adb.tap(serial, x, y, bool(args.get("long")))
            return f"Tapped at ({args['x']}, {args['y']})."

        return await on_phone(work)

    @tool("phone_swipe", "Swipe/scroll on the phone screen: direction up, down, left or right (up scrolls content up to see more below).",
          {"type": "object", "properties": {"direction": {"type": "string"}}, "required": ["direction"]})
    async def phone_swipe(args):
        def work(serial):
            w, h = adb.screen_size(serial)
            cx, cy = w // 2, h // 2
            d = args["direction"].lower()
            moves = {"up": (cx, int(h * 0.75), cx, int(h * 0.25)), "down": (cx, int(h * 0.25), cx, int(h * 0.75)),
                     "left": (int(w * 0.85), cy, int(w * 0.15), cy), "right": (int(w * 0.15), cy, int(w * 0.85), cy)}
            if d not in moves:
                return "Direction must be up, down, left or right."
            adb.run(["shell", "input", "swipe", *map(str, moves[d]), "300"], serial)
            return f"Swiped {d}."

        return await on_phone(work)

    @tool("phone_type", "Type text into the focused field on the phone (basic letters, numbers and punctuation; no emoji). submit=true presses "
          "enter afterwards. Type each piece of text only once.",
          {"type": "object", "properties": {"text": {"type": "string"}, "submit": {"type": "boolean"}}, "required": ["text"]})
    async def phone_type(args):
        def work(serial):
            adb.run(["shell", "input", "text", adb_text(args["text"])], serial)
            if args.get("submit"):
                adb.run(["shell", "input", "keyevent", "KEYCODE_ENTER"], serial)
            return "Typed it." + (" Pressed enter." if args.get("submit") else "")

        return await on_phone(work)

    @tool("phone_key", "Press a phone button: " + ", ".join(KEYCODES) + ".",
          {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]})
    async def phone_key(args):
        def work(serial):
            code = KEYCODES.get(args["key"].lower().strip())
            if not code:
                return f"Unknown key. Use one of: {', '.join(KEYCODES)}."
            adb.run(["shell", "input", "keyevent", code], serial)
            return f"Pressed {args['key']}."

        return await on_phone(work, unlock=False)

    @tool("phone_do_not_disturb", "Turn the phone's Do Not Disturb on or off, or check it (state: on, off, status).",
          {"type": "object", "properties": {"state": {"type": "string"}}, "required": ["state"]})
    async def phone_do_not_disturb(args):
        def work(serial):
            state = args["state"].lower().strip()
            if state in ("on", "off"):
                adb.run(["shell", "cmd", "notification", "set_dnd", "priority" if state == "on" else "off"], serial)
            return "Do Not Disturb is " + ("off." if adb.zen(serial) == "0" else "on.")

        return await on_phone(work, unlock=False)

    @tool("ring_phone", "Make the phone ring loudly so the user can find it (starts a 1-second timer in the phone's Clock app, which rings "
          "until dismissed on the phone).", {"type": "object", "properties": {}})
    async def ring_phone(args):
        def work(serial):
            adb.run(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], serial)
            out = adb.run(["shell", "am", "start", "-a", "android.intent.action.SET_TIMER", "--ei", "android.intent.extra.alarm.LENGTH", "1",
                           "--ez", "android.intent.extra.alarm.SKIP_UI", "true"], serial)
            return "Ringing the phone now; it keeps ringing until dismissed on the phone." if "Error" not in out else out

        return await on_phone(work, unlock=False)

    @tool("android_status", "Read an Android device's battery level, charging state, temperature, model, Android version, storage and uptime "
          "over ADB. serial is optional when only one device is connected.", {"type": "object", "properties": {"serial": {"type": "string"}}})
    async def android_status(args):
        def work(serial):
            battery_raw = adb.run(["shell", "dumpsys", "battery"], serial)
            if "error" in battery_raw.lower() and "level" not in battery_raw:
                return battery_raw
            return format_status(adb.run(["shell", "getprop", "ro.product.model"], serial), adb.run(["shell", "getprop", "ro.build.version.release"], serial),
                                 parse_battery(battery_raw), " ".join(adb.run(["shell", "df", "-h", "/data"], serial).splitlines()[-1:]),
                                 adb.run(["shell", "uptime"], serial))

        return await on_phone(work, args.get("serial"), unlock=False)

    phone = Scope.personal("phone")
    specs = [ToolSpec(fn=android_devices, policy="auto"), ToolSpec(fn=android_connect, policy="auto"),
             ToolSpec(fn=android_pair, policy="confirm", describe=lambda a: f"pair with a new phone using code {a.get('code', '')}"),
             ToolSpec(fn=android_screenshot, policy="auto", scope=phone, untrusted_output=True, untrusted_source="the phone screen"),
             ToolSpec(fn=phone_notifications, policy="auto", scope=Scope.personal("notifications"), untrusted_output=True, untrusted_source="phone notifications"),
             ToolSpec(fn=phone_open_app, policy="auto", scope=phone),
             ToolSpec(fn=phone_open_settings, policy="auto", scope=phone, untrusted_output=True, untrusted_source="the phone screen"),
             ToolSpec(fn=phone_screen_text, policy="auto", scope=phone, untrusted_output=True, untrusted_source="the phone screen"),
             ToolSpec(fn=phone_tap_text, policy="auto", scope=phone, untrusted_output=True, untrusted_source="the phone screen"),
             ToolSpec(fn=phone_tap, policy="auto", scope=phone), ToolSpec(fn=phone_swipe, policy="auto", scope=phone),
             ToolSpec(fn=phone_type, policy="auto", scope=phone), ToolSpec(fn=phone_key, policy="auto", scope=phone),
             ToolSpec(fn=phone_do_not_disturb, policy="auto", scope=phone), ToolSpec(fn=ring_phone, policy="auto", scope=phone),
             ToolSpec(fn=android_status, policy="auto", scope=phone)]
    if screen_only:
        specs = [spec for spec in specs if spec.name not in ("phone_notifications", "ring_phone", "android_status")]
    return ToolModule(server="adb", tools=specs, available=lambda cfg: cfg.features.adb, prompt=PROMPT)
