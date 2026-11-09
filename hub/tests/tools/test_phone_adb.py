import asyncio
import dataclasses
import re
import time

import pytest

from homehub.tools import Registry, result_texts
from homehub.tools.phone_adb import (KEYCODES, SETTINGS_PAGES, Adb, adb_text, ago, app_name, build_module, connected_serials, find_phone_app,
                                        format_status, match_node, notifications_text, obstructed, pairing_addresses, parse_battery,
                                        parse_notifications, parse_ui_dump, parse_ui_tree, screen_labels)

SAMPLE_DUMP = """Current Notification Manager state:
  Notification List:
    NotificationRecord(0x1: pkg=com.discord user=UserHandle{0} id=1 tag=null importance=4 key=0|com.discord|1|null|1: Notification(channel=x flags=AUTO_CANCEL))
      flags=AUTO_CANCEL
            when=1792700000000/1792700000000
                android.title=String (Endermoon21)
                android.text=String (you up?)
    NotificationRecord(0x2: pkg=com.spotify.music user=UserHandle{0} id=2 tag=null importance=2 key=0|com.spotify.music|2|null|1: Notification(channel=x category=transport flags=NO_CLEAR|FOREGROUND_SERVICE))
      flags=NO_CLEAR|FOREGROUND_SERVICE
            when=1792700000000/1792700000000
                android.title=String (Song)
                android.text=String (Artist)
    NotificationRecord(0x3: pkg=android user=UserHandle{-1} id=3 tag=null importance=4 key=x: Notification(channel=x flags=ONGOING_EVENT))
      flags=ONGOING_EVENT
                android.title=String (Wireless debugging connected)
    NotificationRecord(0x4: pkg=com.google.android.apps.messaging user=UserHandle{0} id=4 tag=null importance=4 key=z: Notification(channel=x flags=AUTO_CANCEL))
      flags=AUTO_CANCEL
            when=1792699000000/1792699000000
                android.title=String (Riley)
                android.text=String (Dinner at 6?)
  TimeoutPendingIntent:
  History Notification List:
    NotificationRecord(0x9: pkg=com.whatsapp user=UserHandle{0} id=9 tag=null importance=4 key=y: Notification(channel=x flags=0))
                android.title=String (Old message)
"""
SAMPLE_UI = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" resource-id="com.android.settings:id/search_bar" class="android.widget.FrameLayout" clickable="true" bounds="[40,120][1040,260]">
  <node index="0" text="Search settings" resource-id="com.android.settings:id/search_action_bar_title" clickable="false" bounds="[120,160][900,220]"/>
</node>
<node index="1" text="" class="androidx.recyclerview.widget.RecyclerView" clickable="false" bounds="[0,300][1080,2200]">
  <node index="0" text="" class="android.widget.LinearLayout" clickable="true" bounds="[0,300][1080,480]">
    <node index="0" text="Battery" clickable="false" bounds="[60,340][600,400]"/>
    <node index="1" text="Battery saver, Power saving" clickable="false" bounds="[60,400][900,460]"/>
  </node>
  <node index="1" text="Display" class="android.widget.TextView" clickable="true" bounds="[60,520][600,580]"/>
  <node index="2" content-desc="Back" class="android.widget.ImageButton" clickable="true" bounds="[0,0][120,120]"/>
  <node index="3" text="Hidden" clickable="true" bounds="[0,0][0,0]"/>
</node>
</hierarchy>"""
BATTERY = "Current Battery Service state:\n  AC powered: false\n  USB powered: true\n  status: 2\n  health: 2\n  level: 83\n  scale: 100\n  temperature: 291\n"


def text(result):
    return result_texts(result)[0]


def test_notifications_are_parsed_filtered_and_worded():
    items = parse_notifications(SAMPLE_DUMP, now_ms=1792700000000 + 5 * 60000)
    assert len(items) == 4
    assert items[0]["app"] == "Discord" and items[0]["title"] == "Endermoon21" and items[0]["text"] == "you up?" and items[0]["minutes_ago"] == 5
    assert items[1]["media"] and items[1]["ongoing"] and items[1]["app"] == "Spotify"
    assert items[2]["system"] and items[2]["ongoing"]
    assert items[3]["app"] == "Messages" and items[3]["minutes_ago"] == 22
    everything, shown = notifications_text(items)
    assert shown == 2 and everything.splitlines() == ["Discord: Endermoon21 - you up? (5 min ago)", "Messages: Riley - Dinner at 6? (22 min ago)",
                                                      "Now playing: Song - Artist (Spotify)"]
    assert notifications_text(items, "riley") == ("Messages: Riley - Dinner at 6? (22 min ago)", 1)
    assert notifications_text(items, "discord")[1] == 1 and notifications_text(items, "slack") == ("No notifications matching 'slack'.", 0)
    assert notifications_text([]) == ("No notifications.", 0)
    assert parse_notifications(SAMPLE_DUMP, now_ms=1792700000000 - 90000)[0]["minutes_ago"] == 0


def test_app_names_and_ages():
    assert app_name("com.discord") == "Discord" and app_name("app.morphe.android.apps.youtube.music") == "YouTube Music"
    assert app_name("com.squareup.cash") == "Cash" and app_name("com.example.my_notes") == "My Notes"
    assert [ago(m) for m in (None, 0, 5, 90, 1500)] == ["", "just now", "5 min ago", "1 h ago", "1 days ago"]


def test_fuzzy_app_names_never_open_a_random_app():
    packages = ["com.discord", "com.spotify.music", "com.sec.android.app.camera", "com.android.settings", "app.morphe.android.youtube",
                "app.morphe.android.apps.youtube.music", "com.squareup.cash", "com.roblox.client", "com.samsung.android.messaging",
                "com.sec.android.app.popupcalculator", "com.android.chrome", "com.google.android.apps.maps", "com.whatsapp", "com.anthropic.claude",
                "com.reddit.frontpage"]
    for q, want in [("Discord", "com.discord"), ("camera", "com.sec.android.app.camera"), ("youtube music", "app.morphe.android.apps.youtube.music"),
                    ("YouTube", "app.morphe.android.youtube"), ("cash app", "com.squareup.cash"), ("roblox", "com.roblox.client"),
                    ("texts", "com.samsung.android.messaging"), ("the calculator", None), ("calculator", "com.sec.android.app.popupcalculator"),
                    ("chrome", "com.android.chrome"), ("maps", "com.google.android.apps.maps"), ("whatsapp", "com.whatsapp"), ("claude", "com.anthropic.claude"),
                    ("reddit", "com.reddit.frontpage"), ("Spotify!", "com.spotify.music")]:
        assert find_phone_app(q, packages) == want, q
    assert find_phone_app("nonexistent thing", packages) is None


def test_typing_is_escaped_for_adb():
    assert adb_text("hi, it's 6pm & ok") == "hi,%sit\\'s%s6pm%s\\&%sok"
    assert adb_text("user@example.com:8080/path") == "user@example.com:8080/path"


def test_screen_hierarchy_and_label_matching():
    nodes = parse_ui_dump("junk before\n" + SAMPLE_UI + "\ntrailing")
    labels = [n["label"] for n in nodes]
    assert labels == ["Search settings", "Battery", "Battery saver, Power saving", "Display", "Back"]
    battery = [n for n in nodes if n["label"] == "Battery"][0]
    assert battery == {"label": "Battery", "x": 330, "y": 370, "clickable": True, "search": False}
    assert [n for n in nodes if n["label"] == "Search settings"][0]["search"] is True
    assert match_node(nodes, "battery")["label"] == "Battery"
    assert match_node(nodes, "batt")["label"] == "Battery" and match_node(nodes, "saver")["label"] == "Battery saver, Power saving"
    assert match_node(nodes, "search")["label"] == "Search settings" and match_node(nodes, "wifi") is None
    assert screen_labels(nodes, limit=3) == "Back, Search settings, Battery ..."
    assert parse_ui_dump("no xml here") == []
    nodes, bars = parse_ui_tree(SAMPLE_UI)
    assert bars == [(40, 120, 1040, 260)]
    assert obstructed({"label": "x", "x": 500, "y": 200, "search": False}, bars, 2340) is True
    assert obstructed({"label": "x", "x": 500, "y": 2250, "search": False}, [], 2340) is True
    assert obstructed(battery, bars, 2340) is False


def test_battery_and_status_wording():
    battery = parse_battery(BATTERY)
    assert battery == {"level": "83", "status": "charging", "temperature_c": 29.1}
    assert format_status("SM-S928U", "15", battery, "/dev/block/dm-50 229G 150G 79G 66% /data", "up 3 days") == (
        "Model: SM-S928U\nAndroid: 15\nBattery: 83% (charging), 29.1 C\nStorage (/data): /dev/block/dm-50 229G 150G 79G 66% /data\nUptime: up 3 days")
    assert parse_battery("level: 50\nstatus: 9\n") == {"level": "50", "status": "unknown", "temperature_c": None}
    assert connected_serials("List of devices attached\nadb-R5CX-abc._adb-tls-connect._tcp\tdevice\n192.168.1.5:5555\toffline\n") == ["adb-R5CX-abc._adb-tls-connect._tcp"]
    assert pairing_addresses("List of discovered mdns services\nadb-R5CX\t_adb-tls-pairing._tcp\t192.168.1.5:37123\n") == ["192.168.1.5:37123"]


def test_settings_pages_keys_and_policies(example_config):
    for page in ("wifi", "bluetooth", "battery", "display", "sound", "notifications", "apps", "storage", "location", "about", "date", "security",
                 "accessibility", "developer options", "hotspot", "airplane mode", "data usage"):
        assert page in SETTINGS_PAGES, page
    assert all(v.startswith("android.") for v in SETTINGS_PAGES.values())
    assert KEYCODES["home"] == "KEYCODE_HOME" and KEYCODES["recent apps"] == "KEYCODE_APP_SWITCH" and KEYCODES["screen off"] == "KEYCODE_SLEEP"
    module = build_module(Adb(path="/nonexistent/adb"))
    specs = {spec.name: spec for spec in module.tools}
    assert len(specs) == 16 and module.server == "adb"
    assert specs["android_pair"].policy == "confirm" and specs["android_pair"].describe({"code": "123456"}) == "pair with a new phone using code 123456"
    assert specs["phone_notifications"].untrusted_output and specs["phone_notifications"].scope.detail == "notifications"
    assert specs["phone_screen_text"].untrusted_output and specs["phone_tap"].scope.detail == "phone"
    assert module.is_available(example_config) is False
    assert module.is_available(dataclasses.replace(example_config, features=dataclasses.replace(example_config.features, adb=True))) is True
    assert "mcp__adb__phone_tap" not in Registry(example_config, [module]).specs


def test_tools_explain_when_adb_or_the_phone_is_missing(tmp_path):
    missing = {spec.name: spec.fn.handler for spec in build_module(Adb(path=str(tmp_path / "adb"))).tools}
    out = asyncio.run(missing["android_status"]({}))
    assert out["is_error"] is True and text(out) == "adb (Android platform-tools) is not installed."
    assert text(asyncio.run(missing["phone_key"]({"key": "home"}))) == "adb (Android platform-tools) is not installed."


def phone_or_none():
    adb = Adb()
    if not adb.available():
        return None, None
    try:
        return adb, adb.serial()
    except RuntimeError:
        return None, None


@pytest.fixture
def phone():
    adb, serial = phone_or_none()
    if not serial:
        pytest.skip("no Android phone connected over ADB (turn on Wireless debugging and pair once)")
    return adb, serial


@pytest.mark.live
def test_live_notification_round_trip(phone):
    adb, serial = phone
    stamp = time.strftime("%H%M%S")
    adb.run(["shell", f"cmd notification post -t 'Test Sender {stamp}' va_check_{stamp} 'Voice assistant check {stamp}'"], serial)
    time.sleep(1)
    items = parse_notifications(adb.run(["shell", "dumpsys", "notification", "--noredact"], serial, timeout=20))
    found = [i for i in items if stamp in i["title"]]
    assert found and stamp in found[0]["text"] and found[0]["minutes_ago"] in (0, 1)
    tools = {spec.name: spec.fn.handler for spec in build_module(adb).tools}
    status = text(asyncio.run(tools["android_status"]({})))
    assert status.startswith("Model: ") and "Battery: " in status


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.slow
def test_live_claude_reads_a_notification_and_controls_the_phone(phone, harness, example_config):
    adb, serial = phone
    if adb.locked(serial):
        pytest.skip("the phone is locked; unlock it to run the control checks")
    stamp = time.strftime("%H%M%S")
    adb.run(["shell", f"cmd notification post -t 'Test Sender {stamp}' va_check_{stamp} 'Voice assistant check {stamp}'"], serial)
    zen_before = adb.zen(serial)
    h = harness([build_module(adb)], adb=True)

    def top():
        m = re.search(r"topResumedActivity=ActivityRecord\{\S+ \S+ (\S+)/", adb.run(["shell", "dumpsys", "activity", "activities"], serial))
        return m.group(1) if m else ""

    async def scenario():
        async with h:
            out = []
            for q in (f"Any notifications on my phone from Test Sender {stamp}? What does it say?", "Open the calculator on my phone.",
                      "Type 12+30= into it, then look at the phone screen and tell me the result.", "Go to the home screen on my phone.",
                      "Open Settings on my phone, go to the main Settings page, and tap on Battery.",
                      "On my phone, open Wi-Fi settings and tell me which network it's on.", "Turn on Do Not Disturb on my phone.",
                      "Now turn Do Not Disturb back off.", "Go home on my phone."):
                reply = await h.ask(q)
                out.append((q, reply, h.tools_called(), top(), adb.zen(serial)))
            return out

    try:
        r = asyncio.run(scenario())
    finally:
        adb.run(["shell", "cmd", "notification", "set_dnd", "off" if zen_before == "0" else "priority"], serial)
        adb.run(["shell", "input", "keyevent", "KEYCODE_HOME"], serial)
    for q, reply, called, foreground, zen in r:
        print(f"\n  {q} -> {called} (foreground {foreground}, dnd {zen})\n      reply: {reply[:200]}")
    assert "phone_notifications" in r[0][2] and "voice assistant check" in r[0][1].lower()
    assert "calculator" in r[1][3]
    assert "42" in r[2][1] and "phone_type" in r[2][2] and any(t in r[2][2] for t in ("android_screenshot", "phone_screen_text"))
    assert "launcher" in r[3][3]
    assert r[4][3] in ("com.samsung.android.lool", "com.android.settings") and "battery" in r[4][1].lower() and len(r[4][2]) <= 4
    assert "phone_open_settings" in r[5][2] and len(r[5][2]) <= 3
    assert r[6][4] != "0" and r[7][4] == "0"
    assert adb.zen(serial) == zen_before
