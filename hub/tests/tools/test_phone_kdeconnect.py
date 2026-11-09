import asyncio
import dataclasses
import importlib.util
import os
import subprocess
import sys

import pytest

from homehub import household
from homehub.bus import Bus
from homehub.household import sync_household
from homehub.tools import CallContext, Registry, current_context, result_texts
from homehub.tools import phone_adb
from homehub.tools.phone_kdeconnect import (NOT_RUNNING, KdeConnect, ago, announce_text, build_module, format_battery, format_messages,
                                               format_notifications, pair_target, parse_messages, resolve_phone, unwrap)


def text(result):
    return result_texts(result)[0]


def test_pure_helpers_parse_and_word_things():
    Variant = pytest.importorskip("dbus_next").Variant
    assert unwrap(Variant("a{sv}", {"a": Variant("s", "x"), "b": Variant("ai", [1, 2])})) == {"a": "x", "b": [1, 2]}
    now = 1_792_700_000.0
    raw = [Variant("a{sv}", {"addresses": Variant("av", [Variant("a{sv}", {"address": Variant("s", "+1 206 555 0142")})]), "body": Variant("s", "Dinner at 6?"),
                            "date": Variant("x", int((now - 300) * 1000)), "type": Variant("i", 1), "read": Variant("b", False), "thread_id": Variant("x", 7), "_id": Variant("x", 70)}),
           Variant("a{sv}", {"addresses": Variant("av", [Variant("a{sv}", {"address": Variant("s", "+1 206 555 0142")})]), "body": Variant("s", "On my way"),
                            "date": Variant("x", int((now - 60) * 1000)), "type": Variant("i", 2), "read": Variant("b", True), "thread_id": Variant("x", 7), "_id": Variant("x", 71)}),
           Variant("a{sv}", {"addresses": Variant("av", [Variant("a{sv}", {"address": Variant("s", "Sam")})]), "body": Variant("s", "Call me"),
                            "date": Variant("x", int((now - 7200) * 1000)), "type": Variant("i", 1), "read": Variant("b", True), "thread_id": Variant("x", 9), "_id": Variant("x", 90)})]
    messages = parse_messages(raw, now=now)
    assert [m["body"] for m in messages] == ["On my way", "Dinner at 6?", "Call me"]
    assert messages[1] == {"thread_id": 7, "id": 70, "from": "+1 206 555 0142", "body": "Dinner at 6?", "sent": False, "read": False, "date": int((now - 300) * 1000), "ago": "5 min ago"}
    assert format_messages(messages) == ("You: On my way (1 min ago)\n+1 206 555 0142: Dinner at 6? (5 min ago)\nSam: Call me (2 h ago)", 3)
    assert format_messages(messages, unread_only=True) == ("+1 206 555 0142: Dinner at 6? (5 min ago)", 1)
    assert format_messages(messages, contact="sam") == ("Sam: Call me (2 h ago)", 1)
    assert format_messages(messages, unread_only=True, contact="sam") == ("No texts unread with sam.", 0)
    items = [{"app": "Discord", "title": "Endermoon21", "text": "you up?", "reply_id": "r1"}, {"app": "Gmail", "title": "Newsletter", "text": "", "reply_id": ""},
             {"app": "Spotify", "title": "", "text": "", "reply_id": ""}]
    assert format_notifications(items) == ("Discord: Endermoon21 - you up? (can reply)\nGmail: Newsletter", 2)
    assert format_notifications(items, "discord")[1] == 1 and format_notifications(items, "riley") == ("No notifications matching 'riley'.", 0)
    assert format_battery(83, True) == "Battery 83%, charging." and format_battery(-1, False) == "The phone hasn't reported its battery yet."
    assert announce_text({"app": "Messages", "title": "Riley", "text": "Dinner at 6?"}) == "Riley on Messages: Dinner at 6?"
    assert announce_text({"app": "Gmail", "title": "Newsletter", "text": ""}) == "Gmail: Newsletter"
    assert [ago(s) for s in (None, 5, 300, 7200, 200000)] == ["", "just now", "5 min ago", "2 h ago", "2 days ago"]
    devices = [{"id": "a", "name": "Android phone", "reachable": True, "paired": False}, {"id": "b", "name": "Pixel 8", "reachable": True, "paired": False},
               {"id": "c", "name": "Old tablet", "reachable": False, "paired": False}, {"id": "d", "name": "Laptop", "reachable": True, "paired": True}]
    assert pair_target(devices, "Android phone")[0]["id"] == "a" and pair_target(devices, "pixel")[0]["id"] == "b"
    target, candidates = pair_target(devices, "")
    assert target is None and [d["id"] for d in candidates] == ["a", "b"]
    assert pair_target(devices[:1], "")[0]["id"] == "a"


def test_phones_resolve_through_the_household(example_config, temp_storage):
    sync_household(example_config, temp_storage, household.EXAMPLE_PATH)
    me = CallContext(room="bedroom", person_id="alex")
    person, device, problem = resolve_phone(temp_storage, me, "")
    assert person["id"] == "alex" and device["id"] == "alex_phone" and problem is None
    assert resolve_phone(temp_storage, me, "my phone")[1]["id"] == "alex_phone"
    assert resolve_phone(temp_storage, CallContext(room="bedroom"), "")[2] == "I need to know who's asking before I can get to their phone."
    assert resolve_phone(temp_storage, me, "Zed")[2] == "I don't know anyone called Zed in this household."
    assert resolve_phone(temp_storage, me, "Ry")[2] == "Riley doesn't have a phone linked with KDE Connect in the household file."
    temp_storage.upsert_device("dad_phone", "phone", "Sam's Pixel", owner_id="sam", capabilities=["adb"])
    assert "doesn't have a phone linked with KDE Connect" in resolve_phone(temp_storage, me, "Sam")[2]


def test_module_policies_and_the_adb_overlap(example_config):
    module = build_module(None)
    specs = {spec.name: spec for spec in module.tools}
    assert set(specs) == {"phone_notifications", "read_texts", "text_conversation", "send_text", "reply_to_notification", "phone_battery", "ring_phone",
                          "phone_media", "share_to_phone", "call_on_phone", "pair_phone"}
    assert specs["send_text"].policy == "confirm" and specs["send_text"].outward and specs["send_text"].scope.detail == "texts"
    assert specs["send_text"].describe({"to": "Riley", "message": "On my way"}) == "text Riley saying: On my way"
    assert specs["reply_to_notification"].policy == "confirm" and specs["reply_to_notification"].outward
    assert specs["pair_phone"].policy == "confirm" and specs["pair_phone"].describe({}) == "pair your phone with KDE Connect"
    assert all(specs[n].untrusted_output for n in ("phone_notifications", "read_texts", "text_conversation"))
    assert specs["phone_notifications"].scope.detail == "notifications" and specs["phone_battery"].scope.detail == "phone"
    assert module.is_available(example_config) is False
    on = dataclasses.replace(example_config, features=dataclasses.replace(example_config.features, kdeconnect=True, adb=True))
    assert module.is_available(on) is True
    names = {n for n in Registry(on, [module, phone_adb.build_module(screen_only=True)]).specs}
    assert "mcp__phone__ring_phone" in names and "mcp__adb__ring_phone" not in names and "mcp__adb__phone_notifications" not in names
    assert "mcp__adb__android_status" not in names and "mcp__adb__phone_tap_text" in names
    assert "mcp__adb__ring_phone" in {n for n in Registry(on, [phone_adb.build_module()]).specs}


def session_bus_without_kdeconnect():
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return "no D-Bus session bus here"
    if importlib.util.find_spec("dbus_next") is None:
        return "dbus-next is not installed"
    client = KdeConnect()
    try:
        if asyncio.run(client.available()):
            return "KDE Connect is running here; this test is for machines without it"
    finally:
        client.close()
    return None


@pytest.mark.skipif(session_bus_without_kdeconnect() is not None, reason=str(session_bus_without_kdeconnect()))
def test_tools_say_so_when_the_daemon_is_absent_on_the_real_bus(example_config, temp_storage):
    sync_household(example_config, temp_storage, household.EXAMPLE_PATH)
    client = KdeConnect(storage=temp_storage)
    tools = {spec.name: spec.fn.handler for spec in build_module(temp_storage, client).tools}
    token = current_context.set(CallContext(room="bedroom", person_id="alex"))
    try:
        battery = asyncio.run(tools["phone_battery"]({}))
        pair = asyncio.run(tools["pair_phone"]({}))
        nobody = asyncio.run(tools["ring_phone"]({"person": "Zed"}))
    finally:
        current_context.reset(token)
        client.close()
    assert battery["is_error"] is True and text(battery) == NOT_RUNNING
    assert pair["is_error"] is True and text(pair) == NOT_RUNNING
    assert nobody["is_error"] is True and "Zed" in text(nobody)
    assert client.person_for_device("nope") == (None, None)
    temp_storage.set_device_runtime("alex_phone", kdeconnect_id="abc123")
    assert client.person_for_device("abc123") == ("alex", "alex_phone")


def test_phone_command_reports_the_missing_daemon(tmp_path):
    from tests.test_cli import write_config

    cfg_path = write_config(tmp_path)
    out = subprocess.run([sys.executable, "-m", "homehub", "--config", str(cfg_path), "phone", "list"], capture_output=True, text=True, timeout=120)
    if out.returncode == 3:
        assert "no D-Bus session bus" in out.stderr
    elif out.returncode == 1:
        assert "KDE Connect isn't running" in out.stderr
    else:
        assert out.returncode == 0 and "device(s) known to KDE Connect" in out.stdout


@pytest.mark.live
def test_live_paired_phone_reports_battery_and_notifications(example_config, temp_storage):
    pytest.importorskip("dbus_next")
    if session_bus_without_kdeconnect() is None or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        pytest.skip("KDE Connect is not running here")
    sync_household(example_config, temp_storage)
    client = KdeConnect(storage=temp_storage)
    try:
        devices = asyncio.run(client.devices(only_reachable=True, only_paired=True))
        if not devices:
            pytest.skip("no paired, reachable phone")
        phone = devices[0]
        charge, charging = asyncio.run(client.battery(phone["id"]))
        assert (charge == -1 or 0 <= charge <= 100) and isinstance(charging, bool)
        assert format_battery(charge, charging) == ("The phone hasn't reported its battery yet." if charge < 0 else f"Battery {charge}%" + (", charging." if charging else "."))
        items = asyncio.run(client.notifications(phone["id"]))
        assert isinstance(items, list) and all("app" in i for i in items)
        print(f"\n{phone['name']}: battery {charge}% charging {charging}; {len(items)} notifications")
    finally:
        client.close()


def test_contacts_come_from_the_synced_cards_and_pick_the_mobile_number(tmp_path, monkeypatch):
    from homehub.tools.phone_kdeconnect import best_number, contacts_dir, find_contact, load_contacts, parse_vcard, resolve_number

    card = "BEGIN:VCARD\r\nVERSION:2.1\r\nN:Rivera;Alex\r\nFN:Alex Rivera\r\nTEL;HOME:+1 425-555-0100\r\nTEL;CELL:(425) 555-0199\r\nEND:VCARD\r\n"
    assert parse_vcard(card) == {"name": "Alex Rivera", "numbers": [("home", "+1 425-555-0100"), ("mobile", "(425) 555-0199")]}
    monkeypatch.setenv("HOME", str(tmp_path))
    folder = contacts_dir("dev1")
    folder.mkdir(parents=True)
    (folder / "1.vcf").write_text(card, encoding="utf-8")
    (folder / "2.vcf").write_text("BEGIN:VCARD\nFN:Alex Morgan\nTEL;CELL:425-555-0123\nEND:VCARD\n", encoding="utf-8")
    (folder / "3.vcf").write_text("BEGIN:VCARD\nFN:Mom\nTEL;CELL:425-555-0111\nEND:VCARD\n", encoding="utf-8")
    (folder / "4.vcf").write_text("BEGIN:VCARD\nFN:No Number\nEND:VCARD\n", encoding="utf-8")
    contacts = load_contacts("dev1")
    assert sorted(c["name"] for c in contacts) == ["Alex Morgan", "Alex Rivera", "Mom"]
    assert best_number(find_contact(contacts, "alex rivera")[0]) == "(425) 555-0199"
    assert resolve_number("dev1", "my mom") == ("Mom", "4255550111")
    assert resolve_number("dev1", "425 555 0000") == ("425 555 0000", "4255550000")
    import pytest as _pytest
    with _pytest.raises(ValueError, match="Several contacts match alex: Alex Morgan, Alex Rivera"):
        resolve_number("dev1", "alex")
    with _pytest.raises(ValueError, match="can't find Zed"):
        resolve_number("dev1", "Zed")


def test_a_ringing_call_reaches_the_bus_with_the_callers_name(example_config, temp_storage):
    sync_household(example_config, temp_storage, household.EXAMPLE_PATH)
    temp_storage.set_device_runtime("alex_phone", kdeconnect_id="abc123")

    async def scenario():
        bus = Bus().start()
        calls = []
        bus.subscribe("phone.call", calls.append)
        KdeConnect(emit=bus.publish, storage=temp_storage).call_received("abc123", ["ringing", "+14255550111", "Mom"])
        await bus.drain()
        return calls

    calls = asyncio.run(scenario())
    assert len(calls) == 1
    assert {key: calls[0][key] for key in ("person_id", "device_id", "event", "number", "caller", "untrusted")} == {
        "person_id": "alex", "device_id": "alex_phone", "event": "ringing", "number": "+14255550111", "caller": "Mom", "untrusted": True}
