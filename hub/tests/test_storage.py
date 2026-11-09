import sqlite3
import threading
from datetime import date, datetime, timedelta

import pytest

from homehub.storage import SCHEMA_VERSION, Storage

TABLES = ["accounts", "conversations", "device_aliases", "devices", "grants", "list_items", "memory", "package_messages", "packages", "people", "person_aliases",
          "reminders", "rooms", "schema_version", "settings", "tool_calls", "turns"]


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / "data" / "homehub.db")
    yield s
    s.close()


def household(store):
    store.upsert_room("bedroom", "Bedroom")
    store.upsert_room("office", "Sam's office")
    store.upsert_person("alex", "Alex", "owner", default_room="bedroom", aliases=["me", "the owner"])
    store.upsert_person("sam", "Sam", "adult", default_room="office", aliases=["Samuel"])
    return store


def test_schema_is_created_once_and_uses_wal(store, tmp_path):
    assert store.version() == SCHEMA_VERSION
    assert store.tables() == TABLES
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store.migrate() == SCHEMA_VERSION
    again = Storage(tmp_path / "data" / "homehub.db")
    assert again.version() == SCHEMA_VERSION and again.tables() == TABLES
    again.close()


def test_people_round_trip_with_aliases(store):
    household(store)
    person = store.person("alex")
    assert person["name"] == "Alex" and person["role"] == "owner" and person["default_room"] == "bedroom"
    assert person["units"] == "imperial" and person["announce_phone"] is False
    assert person["aliases"] == ["me", "the owner"]
    assert [p["id"] for p in store.people()] == ["alex", "sam"]
    assert store.find_person("ME")["id"] == "alex"
    assert store.find_person("Samuel")["id"] == "sam"
    assert store.find_person("alex")["id"] == "alex"
    assert store.find_person("SAM")["id"] == "sam"
    assert store.find_person("nobody") is None and store.find_person("") is None


def test_person_upsert_replaces_aliases_and_keeps_identity(store):
    household(store)
    store.upsert_person("sam", "Sam", "adult", default_room="office", aliases=["pops"], announce_phone=True)
    sam = store.person("sam")
    assert sam["aliases"] == ["pops"] and sam["announce_phone"] is True
    assert store.find_person("Samuel") is None
    with pytest.raises(ValueError, match="alias 'me' already belongs to alex"):
        store.upsert_person("sam", "Sam", "adult", aliases=["me"])
    with pytest.raises(ValueError, match="role must be one of owner, adult, child, guest"):
        store.upsert_person("kid", "Kid", "toddler")


def test_rooms_round_trip(store):
    store.upsert_room("bedroom", "Bedroom", ha_area="Bedroom", input_device="USB Speakerphone", output_device="USB Speakerphone")
    assert store.room("bedroom") == {"id": "bedroom", "name": "Bedroom", "ha_area": "Bedroom", "input_device": "USB Speakerphone",
                                     "output_device": "USB Speakerphone"}
    store.upsert_room("bedroom", "Alex's room")
    assert store.room("bedroom")["name"] == "Alex's room" and store.room("bedroom")["input_device"] is None
    assert [r["id"] for r in store.rooms()] == ["bedroom"]


def test_devices_keep_runtime_fields_across_file_reimports(store):
    household(store)
    store.upsert_device("sam_office_pc", "pc", "Sam's office computer", owner_id="sam", room_id="office",
                        capabilities=["pc_agent", "wake_on_lan"], aliases=["OFFICE-PC", "the office computer"])
    store.set_device_runtime("sam_office_pc", mac="aa:bb:cc:dd:ee:ff", last_ip="192.168.1.20", tailscale_name="office-pc")
    store.upsert_device("sam_office_pc", "pc", "Sam's office PC", owner_id="sam", room_id="office",
                        capabilities=["wake_on_lan", "pc_agent", "pc_agent"], aliases=["OFFICE-PC"])
    device = store.device("sam_office_pc")
    assert device["name"] == "Sam's office PC"
    assert device["capabilities"] == ["pc_agent", "wake_on_lan"]
    assert device["aliases"] == ["OFFICE-PC"]
    assert device["mac"] == "aa:bb:cc:dd:ee:ff" and device["last_ip"] == "192.168.1.20" and device["tailscale_name"] == "office-pc"
    assert device["last_seen"] is not None and device["source"] == "file"
    store.upsert_device("sam_office_pc", "pc", "Sam's office PC", owner_id="sam", aliases=["OFFICE-PC"], mac="11:22:33:44:55:66")
    assert store.device("sam_office_pc")["mac"] == "11:22:33:44:55:66"
    assert store.find_device("office-pc")["id"] == "sam_office_pc"
    assert store.find_device("Sam's office PC")["id"] == "sam_office_pc"
    assert store.find_device("the office computer") is None
    assert store.find_device("office-pc", owner_id="alex") is None
    assert store.find_device("office-pc", owner_id="sam")["id"] == "sam_office_pc"


def test_device_validation_and_lookups(store):
    household(store)
    with pytest.raises(ValueError, match="device kind must be one of"):
        store.upsert_device("x", "toaster", "Toaster")
    with pytest.raises(ValueError, match="unknown device field colour"):
        store.upsert_device("x", "other", "Thing", colour="red")
    with pytest.raises(KeyError):
        store.set_device_runtime("missing", last_ip="1.2.3.4")
    store.upsert_device("alex_phone", "phone", "Android phone", owner_id="alex", aliases=["my phone"], capabilities=["kdeconnect", "adb"])
    store.upsert_device("tv", "tv", "Living room TV", capabilities=["home_assistant"], source="voice")
    assert [d["id"] for d in store.devices()] == ["alex_phone", "sam_office_pc"] if False else [d["id"] for d in store.devices()] == ["alex_phone", "tv"]
    assert [d["id"] for d in store.devices(owner_id="alex")] == ["alex_phone"]
    assert [d["id"] for d in store.devices(kind="tv")] == ["tv"]
    assert store.find_device("tv", owner_id="alex")["id"] == "tv"
    with pytest.raises(ValueError, match="alias 'my phone' already belongs to device alex_phone"):
        store.upsert_device("dad_phone", "phone", "Sam's phone", owner_id="sam", aliases=["my phone"])
    assert store.delete_device("tv") == 1 and store.device("tv") is None


def test_accounts_and_grants(store):
    household(store)
    store.upsert_account("alex_google", "alex", "google", "claude_ai_connector")
    assert store.accounts("alex") == [{"id": "alex_google", "person_id": "alex", "kind": "google", "via": "claude_ai_connector"}]
    assert store.accounts("sam") == [] and store.accounts("alex", "google")[0]["id"] == "alex_google"
    store.grant("sam", "device:alex_phone", "alex")
    store.grant("sam", "device:alex_phone", "alex")
    assert store.has_grant("sam", "device:alex_phone") and not store.has_grant("sam", "email")
    assert [g["scope"] for g in store.grants("sam")] == ["device:alex_phone"]
    assert store.revoke("sam", "device:alex_phone") == 1 and store.grants() == []
    with pytest.raises(sqlite3.IntegrityError):
        store.grant("ghost", "email", "alex")


def test_memory_dedupes_and_scopes_by_owner(store):
    household(store)
    fact, new = store.remember("Sam's office computer is named OFFICE-PC.", owner_id="alex")
    assert new and fact["visibility"] == "household" and len(fact["id"]) == 6
    assert store.remember("sam's office computer is named office-pc.", owner_id="sam") == (fact, False)
    store.remember("My Wi-Fi password hint is the dog's name.", owner_id="alex", visibility="private")
    store.remember("Sam likes his coffee black.", owner_id="sam", visibility="private")
    assert [f["text"] for f in store.memory_facts("alex")] == ["Sam's office computer is named OFFICE-PC.", "My Wi-Fi password hint is the dog's name."]
    assert [f["text"] for f in store.memory_facts("sam")] == ["Sam's office computer is named OFFICE-PC.", "Sam likes his coffee black."]
    assert [f["text"] for f in store.memory_facts(None)] == ["Sam's office computer is named OFFICE-PC."]
    with pytest.raises(ValueError, match="must not be empty"):
        store.remember("   ")
    with pytest.raises(sqlite3.IntegrityError):
        store.remember("Nobody owns this.", owner_id="ghost", visibility="private")


def test_recall_and_forget_follow_the_rules(store):
    household(store)
    store.remember("Sam's office computer is named OFFICE-PC.")
    store.remember("Riley's laptop is the silver HP.")
    store.remember("The pizza place closes at 10 pm.")
    assert [f["text"] for f in store.recall("sam computer")][0] == "Sam's office computer is named OFFICE-PC."
    assert store.recall("volcano") == []
    removed, ambiguous = store.forget("pizza")
    assert [f["text"] for f in removed] == ["The pizza place closes at 10 pm."] and ambiguous == []
    store.remember("Sam's favorite pizza is pepperoni.")
    store.remember("Riley's favorite pizza is margherita.")
    removed, ambiguous = store.forget("favorite pizza")
    assert removed == [] and sorted(f["text"] for f in ambiguous) == ["Riley's favorite pizza is margherita.", "Sam's favorite pizza is pepperoni."]
    fact = store.recall("pepperoni")[0]
    assert store.forget(fact["id"])[0][0]["id"] == fact["id"]
    assert store.forget("unicorns") == ([], [])


def test_forget_cannot_reach_another_persons_private_fact(store):
    household(store)
    store.remember("Sam likes his coffee black.", owner_id="sam", visibility="private")
    assert store.forget("coffee", viewer_id="alex") == ([], [])
    assert store.recall("coffee", viewer_id="alex") == []
    assert len(store.forget("coffee", viewer_id="sam")[0]) == 1


def test_memory_prompt_block_format(store):
    assert store.memory_prompt_block() == ""
    store.remember("First fact.")
    store.remember("Second fact.")
    assert store.memory_prompt_block() == "Things you've been asked to remember (long-term memory):\n- First fact.\n- Second fact."
    store.remember("x" * 30)
    block = store.memory_prompt_block(limit_chars=40)
    assert block.startswith("Things you've been asked to remember (long-term memory) (2 older facts not shown; use recall to search them.):\n")
    assert block.endswith("- " + "x" * 30)


def test_reminders_round_trip_cancel_and_due(store):
    household(store)
    soon = datetime.now().replace(microsecond=0) + timedelta(minutes=20)
    later = soon + timedelta(hours=2)
    timer = store.add_reminder("timer", soon, "pizza", owner_id="alex", room_id="bedroom")
    alarm = store.add_reminder("alarm", later, "wake up", repeat="weekdays", owner_id="alex", room_id="bedroom", notify_phone=True)
    reminder = store.add_reminder("reminder", later.isoformat(timespec="seconds"), "call riley", owner_id="sam", room_id="office")
    assert timer["due"] == soon.isoformat(timespec="seconds") and timer["label"] == "pizza" and timer["repeat"] is None
    assert alarm["notify_phone"] is True and alarm["repeat"] == "weekdays"
    assert [r["id"] for r in store.reminders()] == [timer["id"], alarm["id"], reminder["id"]]
    assert [r["id"] for r in store.reminders("sam")] == [reminder["id"]]
    assert store.reminders_due(now=soon) == [store.reminder(timer["id"])]
    assert store.reminders_due(now=soon - timedelta(seconds=1)) == []
    store.reschedule_reminder(timer["id"], later + timedelta(minutes=1))
    assert [r["id"] for r in store.reminders()][-1] == timer["id"]
    assert [r["label"] for r in store.cancel_reminders("riley")] == ["call riley"]
    assert [r["kind"] for r in store.cancel_reminders("alarm")] == ["alarm"]
    assert store.cancel_reminders("nothing") == []
    assert [r["label"] for r in store.cancel_reminders("all")] == ["pizza"]
    assert store.reminders() == []
    with pytest.raises(ValueError, match="reminder kind"):
        store.add_reminder("nap", soon)
    with pytest.raises(ValueError, match="repeat must be one of daily, weekdays"):
        store.add_reminder("alarm", soon, repeat="hourly")
    with pytest.raises(KeyError):
        store.reschedule_reminder("nope", soon)


def test_conversations_turns_tool_calls_and_costs(store):
    household(store)
    store.start_conversation("c1", "bedroom", "alex", "sess-1")
    store.add_turn("c1", "what's the weather", "Sunny and 72.", "claude-haiku-5-5", 2, 0.0012, {"first_sentence": 900, "total": 2100})
    store.add_turn("c1", "thanks", "You're welcome.", "claude-haiku-5-5", 1, 0.0005)
    store.add_tool_call("c1", "WebSearch", {"query": "weather seattle"}, "auto", "alex", ok=True, ms=1200)
    store.add_tool_call("c1", "mcp__texts__send_text", {"to": "Riley", "message": "hi"}, "declined", "alex", ok=False)
    store.end_conversation("c1", "goodbye")
    conversation = store.conversation("c1")
    assert conversation["turns"] == 2 and conversation["cost_usd"] == pytest.approx(0.0017)
    assert conversation["ended"] is not None and conversation["reason"] == "goodbye"
    turns = store.turns("c1")
    assert turns[0]["latency"] == {"first_sentence": 900, "total": 2100} and turns[1]["latency"] is None
    calls = store.tool_calls("c1")
    assert calls[0]["input"] == {"query": "weather seattle"} and calls[0]["ok"] is True
    assert calls[1]["decision"] == "declined" and calls[1]["ok"] is False
    assert store.cost_total(day=date.today()) == pytest.approx(0.0017)
    assert store.cost_total(person_id="sam") == 0
    assert store.costs_daily()[0]["turns"] == 2
    assert store.conversations()[0]["id"] == "c1"


def test_prune_removes_old_conversations_with_their_rows(store):
    household(store)
    store.start_conversation("old", "bedroom", "alex")
    store.add_turn("old", "hi", "hello")
    store.add_tool_call("old", "Read", {"file_path": "/x"}, "auto")
    store.start_conversation("new", "bedroom", "alex")
    store.add_turn("new", "hi again", "hello again")
    with store.conn:
        store.conn.execute("UPDATE conversations SET started=? WHERE id='old'", ((datetime.now() - timedelta(days=40)).isoformat(timespec="seconds"),))
    assert store.prune_conversations(30) == 1
    assert store.conversation("old") is None and store.conversation("new") is not None
    assert store.turns("old") == [] and store.tool_calls("old") == []
    assert len(store.turns("new")) == 1
    assert store.prune_conversations(30) == 0


def test_settings_store_json_values(store):
    assert store.get_setting("volume") is None and store.get_setting("volume", 0.3) == 0.3
    store.set_setting("volume", 0.25)
    store.set_setting("voice", "af_heart")
    store.set_setting("last_room", {"alex": "bedroom"})
    assert store.get_setting("volume") == 0.25 and store.get_setting("last_room") == {"alex": "bedroom"}
    store.set_setting("volume", 0.2)
    assert store.settings() == {"last_room": {"alex": "bedroom"}, "voice": "af_heart", "volume": 0.2}


def test_forget_everything_about_me(store):
    household(store)
    store.remember("Household fact.", owner_id="alex")
    store.remember("Sam's private fact.", owner_id="sam", visibility="private")
    store.start_conversation("d1", "office", "sam")
    store.add_turn("d1", "hi", "hello")
    store.add_tool_call("d1", "Read", {}, "auto", "sam")
    store.start_conversation("b1", "bedroom", "alex")
    store.add_turn("b1", "hi", "hello")
    assert store.forget_person("sam") == {"memory": 1, "tool_calls": 1, "turns": 1, "conversations": 1}
    assert store.person("sam")["name"] == "Sam"
    assert [f["text"] for f in store.memory_facts("sam")] == ["Household fact."]
    assert store.conversation("d1") is None and store.conversation("b1") is not None


def test_threads_can_write_concurrently(store):
    household(store)
    due = datetime.now() + timedelta(hours=1)

    def work(label):
        for i in range(100):
            store.add_reminder("timer", due, f"{label} {i}", owner_id="alex")

    threads = [threading.Thread(target=work, args=(name,)) for name in ("A", "B", "C")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.reminders()) == 300


def test_a_new_id_is_never_one_that_is_already_taken(store):
    taken = 2_000_000
    numbers = f"WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM n WHERE i < {taken - 1}) "
    with store.conn:
        store.conn.execute(numbers + "INSERT INTO memory (id, text, visibility, saved) SELECT printf('%06x', i), 'old ' || i, 'household', '2020-01-01T00:00:00' FROM n")
        store.conn.execute(numbers + "INSERT INTO reminders (id, kind, due, created) SELECT printf('%06x', i), 'timer', '2020-01-01T00:00:00', '2020-01-01T00:00:00' FROM n")
        store.conn.execute(numbers + "INSERT INTO list_items SELECT printf('%06x', i), 'old', 'item ' || i, '2020-01-01T00:00:00', NULL FROM n")
    facts = [store.remember(f"New fact {i}.")[0]["id"] for i in range(40)]
    reminders = [store.add_reminder("timer", datetime(2030, 1, 1), label=str(i))["id"] for i in range(150)]
    _, added, _ = store.add_list_items("shopping", [f"thing {i}" for i in range(150)])
    assert len(set(facts)) == 40 and len(set(reminders)) == 150 and len(added) == 150
    assert all(int(found, 16) >= taken for found in facts + reminders)
    assert store.conn.execute("SELECT COUNT(*), COUNT(DISTINCT id) FROM list_items WHERE list_name='shopping'").fetchone()[:] == (150, 150)
    kept = store.remember("Asked for a taken id.", fact_id="00000a")[0]["id"]
    assert kept != "00000a" and store.add_reminder("timer", datetime(2030, 1, 1), reminder_id="00000b")["id"] != "00000b"
    assert store.remember("Asked for a free id.", fact_id="zzzzzz")[0]["id"] == "zzzzzz"
