import asyncio
import threading
import time
from datetime import date

import pytest

from homehub.bus import Bus
from homehub.logging_setup import log_file_name, parse_event_line, setup_logging


def run(coro):
    return asyncio.run(coro)


def test_exact_prefix_and_wildcard_subscriptions():
    async def scenario():
        bus = Bus().start()
        exact, prefix, everything = [], [], []
        bus.subscribe("speech.final", exact.append)
        bus.subscribe("speech.*", prefix.append)
        bus.subscribe("*", everything.append)
        bus.publish("speech.final", room="bedroom", text="hello")
        bus.publish("speech.partial", room="bedroom", text="hel")
        bus.publish("speechless.thing", room="bedroom")
        bus.publish("wake.detected", room="bedroom", score=2.3)
        await bus.drain()
        return exact, prefix, everything

    exact, prefix, everything = run(scenario())
    assert [e["name"] for e in exact] == ["speech.final"]
    assert [e["name"] for e in prefix] == ["speech.final", "speech.partial"]
    assert [e["name"] for e in everything] == ["speech.final", "speech.partial", "speechless.thing", "wake.detected"]
    assert exact[0]["text"] == "hello" and exact[0]["room"] == "bedroom"
    assert exact[0]["ts"].startswith(date.today().isoformat())


def test_bad_names_patterns_and_reserved_keys_are_rejected():
    async def scenario():
        bus = Bus().start()
        with pytest.raises(ValueError, match="bad event name"):
            bus.publish("Speech.Final")
        with pytest.raises(ValueError, match="bad event name"):
            bus.publish("speech")
        with pytest.raises(ValueError, match="reserved"):
            bus.publish("speech.final", name="x")
        with pytest.raises(ValueError, match="reserved"):
            bus.publish("speech.final", ts="x")
        with pytest.raises(ValueError, match="bad subscription pattern"):
            bus.subscribe("speech*", print)

    run(scenario())


def test_publish_and_subscribe_require_a_started_bus():
    bus = Bus()
    with pytest.raises(RuntimeError, match="bus.start"):
        bus.publish("speech.final")
    with pytest.raises(RuntimeError, match="bus.start"):
        bus.subscribe("*", print)


def test_a_raising_subscriber_does_not_stop_others_or_itself():
    async def scenario():
        bus = Bus().start()
        good = []

        def bad(event):
            raise RuntimeError("boom " + event["name"])

        bad_sub = bus.subscribe("*", bad)
        bus.subscribe("*", good.append)
        for i in range(3):
            bus.publish("tool.called", tool=f"t{i}")
        await bus.drain()
        return good, bad_sub

    good, bad_sub = run(scenario())
    assert [e["tool"] for e in good] == ["t0", "t1", "t2"]
    assert bad_sub.errors == 3 and bad_sub.delivered == 0


def test_async_handlers_are_awaited_in_order():
    async def scenario():
        bus = Bus().start()
        seen = []

        async def handler(event):
            await asyncio.sleep(0.001)
            seen.append(event["n"])

        bus.subscribe("count.tick", handler)
        for n in range(20):
            bus.publish("count.tick", n=n)
        await bus.drain()
        return seen

    assert run(scenario()) == list(range(20))


def test_audio_events_are_dropped_oldest_first_under_pressure_and_nothing_else_is():
    async def scenario():
        bus = Bus().start()
        gate = asyncio.Event()
        received = []

        async def slow(event):
            received.append(event)
            await gate.wait()

        sub = bus.subscribe("*", slow, max_queue=5)
        bus.publish("audio.level", room="bedroom", peak=0)
        await asyncio.sleep(0.01)
        for i in range(1, 11):
            bus.publish("audio.level", room="bedroom", peak=i)
        for i in range(10):
            bus.publish("speech.partial", room="bedroom", text=str(i))
        snapshot = [e["name"] for e in sub.queue]
        gate.set()
        await bus.drain()
        return received, sub, snapshot

    received, sub, snapshot = run(scenario())
    assert received[0]["name"] == "audio.level" and received[0]["peak"] == 0
    assert [e["name"] for e in received[1:]] == ["speech.partial"] * 10
    assert [e["text"] for e in received[1:]] == [str(i) for i in range(10)]
    assert sub.dropped == 10
    assert snapshot == ["speech.partial"] * 10
    assert sub.delivered == 11


def test_audio_events_are_delivered_when_there_is_no_pressure():
    async def scenario():
        bus = Bus().start()
        got = []
        sub = bus.subscribe("audio.*", got.append, max_queue=5)
        for i in range(5):
            bus.publish("audio.level", peak=i)
        await bus.drain()
        return got, sub

    got, sub = run(scenario())
    assert [e["peak"] for e in got] == [0, 1, 2, 3, 4] and sub.dropped == 0


def test_publish_never_blocks_on_a_stuck_subscriber():
    async def scenario():
        bus = Bus().start()
        gate = asyncio.Event()
        received = []

        async def stuck(event):
            received.append(event)
            await gate.wait()

        bus.subscribe("*", stuck)
        start = time.perf_counter()
        for i in range(10000):
            bus.publish("speech.partial", text=str(i))
        elapsed = time.perf_counter() - start
        assert received == []
        gate.set()
        await bus.drain(timeout=30)
        return elapsed, len(received)

    elapsed, count = run(scenario())
    assert elapsed < 1.0
    assert count == 10000


def test_publish_from_another_thread_is_delivered_on_the_loop():
    async def scenario():
        bus = Bus().start()
        got = []
        loop_thread = threading.get_ident()
        handler_threads = []

        def handler(event):
            handler_threads.append(threading.get_ident())
            got.append(event)

        bus.subscribe("audio.level", handler)
        worker = threading.Thread(target=lambda: bus.publish("audio.level", room="bedroom", peak=0.3, gain=2.0))
        worker.start()
        worker.join()
        await bus.drain()
        return got, handler_threads, loop_thread

    got, handler_threads, loop_thread = run(scenario())
    assert len(got) == 1 and got[0]["gain"] == 2.0
    assert handler_threads == [loop_thread]


def test_unsubscribe_and_close_stop_delivery():
    async def scenario():
        bus = Bus().start()
        got = []
        sub = bus.subscribe("*", got.append)
        bus.publish("conversation.started", room="bedroom")
        await bus.drain()
        bus.unsubscribe(sub)
        bus.publish("conversation.ended", room="bedroom")
        await asyncio.sleep(0.01)
        other = bus.subscribe("*", got.append)
        await bus.close()
        return got, sub, other, bus

    got, sub, other, bus = run(scenario())
    assert [e["name"] for e in got] == ["conversation.started"]
    assert sub.task.cancelled() and other.task.cancelled()
    assert bus.subscriptions == []


def test_drain_times_out_when_a_handler_never_finishes():
    async def scenario():
        bus = Bus().start()

        async def forever(event):
            await asyncio.Event().wait()

        bus.subscribe("*", forever)
        bus.publish("speech.final", text="x")
        with pytest.raises(TimeoutError):
            await bus.drain(timeout=0.05)
        await bus.close()

    run(scenario())


def test_attach_logging_writes_one_json_line_per_event(tmp_path):
    directory = setup_logging(tmp_path)

    async def scenario():
        bus = Bus().start()
        bus.attach_logging()
        bus.publish("reminder.due", id="ab12cd", kind="timer", label="pizza", owner_id="alex", room="bedroom", missed=False)
        await bus.drain()

    run(scenario())
    line = (directory / log_file_name(date.today())).read_text(encoding="utf-8").splitlines()[-1]
    name, payload = parse_event_line(line)
    assert name == "reminder.due"
    assert payload == {"id": "ab12cd", "kind": "timer", "label": "pizza", "owner_id": "alex", "room": "bedroom", "missed": False}
