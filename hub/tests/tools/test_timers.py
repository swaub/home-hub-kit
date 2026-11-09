import asyncio
import secrets
import time
from datetime import datetime, timedelta

import pytest

from homehub import household
from homehub.household import sync_household
from homehub.storage import Storage
from homehub.tools import CallContext, current_context, result_texts
from homehub.tools.notify import Ntfy
from homehub.tools.timers import Pusher, Scheduler, announcement, build_module, describe, next_repeat, parse_clock, spoken_time


def wait_for(predicate, timeout=4.0):
    start = time.perf_counter()
    while not predicate() and time.perf_counter() - start < timeout:
        time.sleep(0.05)
    return time.perf_counter() - start


def text(result):
    return result_texts(result)[0]


def test_parse_clock_reads_clock_times():
    now = datetime(2026, 10, 23, 15, 0)
    assert parse_clock("7 pm", now) == datetime(2026, 10, 23, 19, 0)
    assert parse_clock("7:30 am", now) == datetime(2026, 10, 24, 7, 30)
    assert parse_clock("12 am", now) == datetime(2026, 10, 24, 0, 0)
    assert parse_clock("12 pm", now) == datetime(2026, 10, 24, 12, 0)
    assert parse_clock("2026-10-25T09:15", now) == datetime(2026, 10, 25, 9, 15)
    assert parse_clock("19:00", now) == datetime(2026, 10, 23, 19, 0)
    with pytest.raises(ValueError, match="Couldn't understand"):
        parse_clock("half past", now)
    with pytest.raises(ValueError, match="isn't a valid time"):
        parse_clock("25:00", now)


def test_repeats_and_spoken_times():
    friday = datetime(2026, 10, 23, 6, 30)
    assert next_repeat(friday, "weekdays").weekday() == 0
    assert next_repeat(friday, "daily") == datetime(2026, 10, 24, 6, 30)
    now = datetime(2026, 10, 23, 15, 0)
    assert spoken_time(datetime(2026, 10, 23, 15, 20, 30), now) == "3:20 PM today (in 20 min 30 s)"
    assert spoken_time(datetime(2026, 10, 24, 7, 0), now) == "7:00 AM tomorrow"
    assert spoken_time(datetime(2026, 10, 26, 7, 0), now) == "7:00 AM Monday October 26"
    item = {"id": "ab12", "kind": "alarm", "label": "", "due": "2026-10-24T06:30:00", "repeat": "weekdays"}
    assert describe(item, now) == "alarm at 6:30 AM tomorrow, repeats weekdays [id ab12]"


def test_announcement_wording():
    assert announcement({"kind": "timer", "label": "pizza"}) == "Your pizza timer is done."
    assert announcement({"kind": "timer", "label": ""}) == "Your timer is done."
    assert announcement({"kind": "reminder", "label": "call riley", "missed": True}) == "You missed a reminder earlier: call riley."
    assert announcement({"kind": "reminder", "label": "call riley."}) == "Reminder: call riley."
    assert announcement({"kind": "alarm", "label": ""}, datetime(2026, 10, 23, 6, 30)) == "It's 6:30 AM. Time to wake up."
    assert announcement({"kind": "alarm", "label": "Gym"}, datetime(2026, 10, 23, 18, 5)) == "It's 6:05 PM. Gym."


def test_scheduler_fires_persists_and_cancels(tmp_path):
    path = tmp_path / "homehub.db"
    store = Storage(path)
    events, fired = [], []
    sched = Scheduler(store, emit=lambda name, **p: events.append((name, p)), on_due=fired.append, poll_seconds=0.1).start()
    item = sched.add("timer", datetime.now() + timedelta(seconds=1), "pizza")
    took = wait_for(lambda: fired)
    assert fired and fired[0]["label"] == "pizza" and fired[0]["missed"] is False and took < 4
    assert sched.upcoming() == [] and [r["label"] for r in sched.recent()] == ["pizza"]
    sched.add("reminder", datetime.now() + timedelta(hours=2), "call riley")
    sched.add("alarm", datetime.now() + timedelta(hours=9), "", "weekdays")
    sched.stop()
    store.close()
    reloaded = Scheduler(Storage(path))
    assert [i["kind"] for i in reloaded.upcoming()] == ["reminder", "alarm"]
    assert len(reloaded.cancel("riley")) == 1 and len(reloaded.upcoming()) == 1
    assert len(reloaded.cancel("alarm")) == 1 and reloaded.upcoming() == []
    reloaded.storage.close()
    assert [n for n, _ in events] == ["reminder.changed", "reminder.due", "reminder.changed", "reminder.changed"]
    due = [p for n, p in events if n == "reminder.due"][0]
    assert due["id"] == item["id"] and due["kind"] == "timer" and due["missed"] is False


def test_pusher_names_someone_else_and_shows_the_item_without_a_push_server(example_config, temp_storage):
    sync_household(example_config, temp_storage, household.EXAMPLE_PATH)
    events = []
    pusher = Pusher(Ntfy("", ""), temp_storage, emit=lambda name, **p: events.append((name, p)))
    assert pusher.message({"kind": "timer", "label": "pizza", "owner_id": "alex"}) == "Your pizza timer is done."
    assert pusher.message({"kind": "reminder", "label": "take your pills", "owner_id": "riley"}) == "For Riley: Reminder: take your pills."
    assert pusher.push({"kind": "timer", "label": "tea", "owner_id": "alex"}) is False
    assert events == [("transcript.line", {"room": "app", "kind": "action", "text": "timer due: Your tea timer is done."})]


@pytest.mark.live
def test_a_due_timer_is_pushed_through_a_real_ntfy_server(example_config, temp_storage):
    sync_household(example_config, temp_storage, household.EXAMPLE_PATH)
    ntfy = Ntfy("https://ntfy.sh", "homehub-test-" + secrets.token_hex(6))
    pusher = Pusher(ntfy, temp_storage)
    sched = Scheduler(temp_storage, on_due=pusher.push, poll_seconds=0.1).start()
    sched.add("timer", datetime.now() + timedelta(seconds=1), "pizza", owner_id="alex")
    recent = []
    for _ in range(12):
        time.sleep(1)
        recent = ntfy.recent()
        if recent:
            break
    sched.stop()
    assert any(m.get("message") == "Your pizza timer is done." and m.get("title") == "Timer done" for m in recent)


def test_missed_and_stale_items_at_startup(temp_storage):
    sched = Scheduler(temp_storage)
    t = datetime.now()
    sched.add("reminder", t - timedelta(hours=1), "take meds")
    sched.add("timer", t - timedelta(hours=1), "old timer")
    sched.add("reminder", t - timedelta(days=2), "ancient")
    sched.add("alarm", t - timedelta(minutes=5), "", "daily")
    sched.add("timer", t - timedelta(seconds=30), "fresh")
    due = sched.due_now(t)
    assert sorted(i["label"] for i in due) == ["", "fresh", "take meds"]
    assert {i["label"]: i["missed"] for i in due} == {"": True, "fresh": False, "take meds": True}
    left = sched.upcoming()
    assert [i["kind"] for i in left] == ["alarm"] and datetime.fromisoformat(left[0]["due"]) > t
    assert len(sched.cancel("all")) == 1 and sched.upcoming() == []


@pytest.fixture
def time_tools(example_config, temp_storage):
    sync_household(example_config, temp_storage, household.EXAMPLE_PATH)
    events = []
    sched = Scheduler(temp_storage, emit=lambda name, **p: events.append((name, p)))
    module = build_module(sched, temp_storage)
    return sched, {spec.name: spec.fn.handler for spec in module.tools}, events


def call(tools, name, args, ctx):
    token = current_context.set(ctx)
    try:
        return asyncio.run(tools[name](args))
    finally:
        current_context.reset(token)


def test_tools_validate_inputs_and_describe_results(time_tools):
    sched, tools, events = time_tools
    ctx = CallContext(person_id="alex", confidence="vpn_key")
    assert call(tools, "set_timer", {"seconds": 0}, ctx)["is_error"] is True
    assert text(call(tools, "set_timer", {"seconds": 180, "label": "pizza"}, ctx)).startswith("Timer set: timer 'pizza' at")
    assert call(tools, "set_reminder", {"message": "x"}, ctx)["is_error"] is True
    assert "already passed" in text(call(tools, "set_reminder", {"message": "x", "at": "2020-01-01T09:00"}, ctx))
    assert "Couldn't understand" in text(call(tools, "set_reminder", {"message": "x", "at": "soonish"}, ctx))
    assert text(call(tools, "set_reminder", {"message": "stretch", "in_seconds": 45}, ctx)).startswith("Reminder set: reminder 'stretch' at")
    assert "repeats weekdays" in text(call(tools, "set_alarm", {"at": "6:30", "repeat": "weekdays"}, ctx))
    assert "isn't a valid time" in text(call(tools, "set_alarm", {"at": "99:00"}, ctx))
    listed = text(call(tools, "list_timers", {}, ctx))
    assert "pizza" in listed and "stretch" in listed and listed.count("[id ") == 3
    assert all(i["owner_id"] == "alex" and i["notify_phone"] is True for i in sched.upcoming())
    assert call(tools, "cancel_timer", {"which": "nothing-here"}, ctx)["is_error"] is True
    assert text(call(tools, "cancel_timer", {"which": "pizza"}, ctx)).startswith("Cancelled: timer 'pizza'")
    assert "stop_alarm" not in tools
    assert text(call(tools, "cancel_timer", {"which": "all"}, ctx)).count("; ") == 1 and sched.upcoming() == []
    assert text(call(tools, "list_timers", {}, ctx)) == "Nothing is scheduled."
    assert [p["action"] for n, p in events if n == "reminder.changed"] == ["added", "added", "added", "cancelled", "cancelled", "cancelled"]


def test_reminder_for_someone_else_is_theirs(time_tools, temp_storage):
    sched, tools, events = time_tools
    ctx = CallContext(person_id="alex", confidence="vpn_key")
    reply = text(call(tools, "set_reminder", {"message": "take your pills", "at": "17:00", "for_person": "Riley"}, ctx))
    assert reply.startswith("Reminder set for Riley: reminder 'take your pills'") and sched.upcoming()[0]["owner_id"] == "riley"
    reply = text(call(tools, "set_reminder", {"message": "dinner", "in_seconds": 600, "for_person": "Ry"}, ctx))
    assert reply.startswith("Reminder set for Ry: ")
    mine = text(call(tools, "set_reminder", {"message": "stretch", "in_seconds": 30, "for_person": "me"}, ctx))
    assert mine.startswith("Reminder set: ") and sched.upcoming()[0]["owner_id"] == "alex"
    unknown = call(tools, "set_reminder", {"message": "x", "in_seconds": 60, "for_person": "Zed"}, ctx)
    assert unknown["is_error"] is True and "don't know anyone called Zed" in text(unknown)
    assert len(sched.cancel("all")) == 3


CLAUDE_CASES = [
    ("Set a timer for 3 minutes for the pizza.", "set_timer", lambda a: a.get("seconds") == 180),
    ("Remind me at 7 pm to call riley.", "set_reminder",
     lambda a: a.get("at", "").replace(" ", "").lower() in ("19:00", "7pm", "7:00pm") or "T19:00" in a.get("at", "")),
    ("Remind me in 45 seconds to stretch.", "set_reminder", lambda a: a.get("in_seconds") == 45),
    ("Wake me up at 6:30 on weekdays.", "set_alarm", lambda a: a.get("repeat") == "weekdays" and "6:30" in a.get("at", "").replace("06:30", "6:30")),
    ("What timers and reminders do I have?", "list_timers", lambda a: True),
    ("Cancel the pizza timer.", "cancel_timer", lambda a: "pizza" in a.get("which", "").lower() or len(a.get("which", "")) == 6),
]


@pytest.mark.claude
@pytest.mark.slow
def test_claude_uses_the_time_tools(harness, temp_storage):
    sched = Scheduler(temp_storage)
    h = harness([build_module(sched, temp_storage)])

    async def scenario():
        async with h:
            results = []
            for question, name, check in CLAUDE_CASES:
                reply = await h.ask(question)
                results.append((question, any(check(c) for c in h.calls(name)), h.tools_called(), reply))
            await h.ask("Cancel all my timers, reminders and alarms.")
            return results

    results = asyncio.run(scenario())
    for question, ok, called, reply in results:
        print(f"\n  {'ok ' if ok else 'BAD'} {question} -> {called}\n      reply: {reply}")
    for question, ok, called, reply in results:
        assert ok, (question, called, reply)
    assert sched.upcoming() == []
