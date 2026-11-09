import asyncio
import dataclasses
import re
import time
from datetime import datetime

import pytest

from homehub import household
from homehub.app import tool_modules
from homehub.brain.permissions import Permissions
from homehub.quick import Quick, normalize, parse_duration, say_duration
from homehub.tools import CallContext, Registry
from homehub.tools.timers import Scheduler


@pytest.fixture
def quick(example_config, temp_storage, tmp_path):
    people = tmp_path / "people.toml"
    people.write_text(household.EXAMPLE_PATH.read_text(encoding="utf-8").replace("[household]\n", "[household]\ntown = \"Springfield\"\n", 1),
                      encoding="utf-8")
    household.sync_household(example_config, temp_storage, people)
    cfg = dataclasses.replace(example_config, features=dataclasses.replace(example_config.features, ps5=True, kdeconnect=True, google=False),
                              lights=dataclasses.replace(example_config.lights, devices=["Desk Lamp", "Bedroom Fan"]))
    registry = Registry(cfg, tool_modules(cfg, Scheduler(temp_storage), temp_storage, None))
    permissions = Permissions(registry, temp_storage)
    temp_storage.start_conversation("q1", "bedroom", "alex")
    ctx = CallContext(room="bedroom", conversation_id="q1", person_id="alex", person_name="Alex", role="owner", confidence="vpn_key")
    registry.context = ctx
    return Quick(cfg, registry, permissions, temp_storage), ctx, temp_storage


def intent(q, text):
    found = q.match(text)
    return found[0] if found else None


def test_messages_are_normalized_and_durations_are_read_in_words_or_digits():
    assert normalize("Hey, can you set a timer for 5 minutes, please?") == "set a timer for 5 minutes"
    assert normalize("Set an alarm for 6:30 a.m.") == "set an alarm for 6:30 am"
    cases = {"five minutes": 300, "an hour and a half": 5400, "half an hour": 1800, "1 hour 30 minutes": 5400, "twenty five minutes": 1500,
             "a minute and a half": 90, "90 seconds": 90, "10 mins": 600, "three hours": 10800, "pizza": None, "five": None}
    assert {text: parse_duration(text) for text in cases} == cases
    assert say_duration(5400) == "1 hour and 30 minutes" and say_duration(90) == "1 minute and 30 seconds"


def test_everyday_commands_are_recognized_and_everything_else_goes_to_claude(quick):
    q, _, _ = quick
    expected = {
        "What time is it?": "time", "what's the date today": "date", "Set a timer for 10 minutes.": "timer",
        "Five minute timer": "timer", "set a pizza timer for 20 minutes": "timer", "how much time is left on the timer": "timers",
        "Cancel the timer.": "cancel", "cancel all my alarms": "cancel", "Wake me up at 6:30 a.m.": "alarm",
        "set an alarm for 7 am every weekday": "alarm", "What's the weather like?": "weather", "Will it rain tomorrow?": "weather",
        "how cold is it outside": "weather", "Add eggs and milk to my shopping list.": "lists", "What's on my shopping list?": "lists",
        "Take eggs off the shopping list": "lists",
        "Turn on the PS5": "ps5", "put the playstation into rest mode": "ps5", "Turn off the desk lamp": "lights",
        "turn the bedroom fan on": "lights", "Cancel the pizza timer.": "cancel", "Ring my phone.": "phone", "where's my phone": "phone",
        "What's my phone's battery?": "phone",
    }
    assert {text: intent(q, text) for text in expected} == expected
    for text in ("What's the capital of Australia?", "What's the weather in Paris?", "what's the weather this weekend",
                 "Set a timer for pizza", "Set an alarm for 7", "Call me later", "Ring me at noon", "Turn on the kitchen light", "Stop the alarm",
                 "Add a rain jacket to my wish list and tell me the weather", "Play rain sounds on the TV",
                 "Tell me a joke about the weather"):
        assert intent(q, text) is None, text
    assert intent(q, "add a rain jacket to my shopping list") == "lists"
    leftovers = {"remind me to call mom": None, "tell him to call mom": None, "i don't want a timer for 10 minutes": None,
                 "after dinner set a timer for 10 minutes": None, "call mom": None, "hang up": None}
    assert {text: intent(q, text) for text in leftovers} == leftovers
    assert intent(q, "play cloud nine on the tv") is None


def test_timers_lists_and_clock_answers_run_on_the_hub_without_claude(quick):
    q, ctx, storage = quick

    async def run(text):
        return await q.handle(text, ctx)

    assert asyncio.run(run("set a timer for twenty five minutes")) == ("timer", "Timer set for 25 minutes.")
    items = storage.reminders()
    assert len(items) == 1 and items[0]["kind"] == "timer"
    assert abs((datetime.fromisoformat(items[0]["due"]) - datetime.now()).total_seconds() - 1500) < 5
    intent_, left = asyncio.run(run("how much time is left on my timer"))
    assert intent_ == "timers" and re.fullmatch(r"Your timer has 2[45] minutes( and \d+ seconds?)? left\.", left)
    assert asyncio.run(run("set a pizza timer for 20 minutes")) == ("timer", "Pizza timer set for 20 minutes.")
    assert asyncio.run(run("cancel all my timers")) == ("cancel", "Cancelled 2 items.") and storage.reminders() == []
    asyncio.run(run("set a timer for twenty five minutes"))
    assert asyncio.run(run("cancel the timer")) == ("cancel", "Cancelled your timer.") and storage.reminders() == []
    assert asyncio.run(run("cancel the timer")) == ("cancel", "You don't have any timers set.")
    intent_, alarm = asyncio.run(run("wake me up at 6:30 am"))
    assert intent_ == "alarm" and alarm.startswith("Alarm set for 6:30 AM") and storage.reminders()[0]["kind"] == "alarm"
    assert asyncio.run(run("add eggs and milk to my shopping list")) == ("lists", "Added to the shopping list: eggs and milk.")
    assert asyncio.run(run("what's on my shopping list")) == ("lists", "Your shopping list has eggs and milk.")
    assert asyncio.run(run("take eggs off the shopping list")) == ("lists", "Removed from the shopping list: eggs.")
    intent_, clock = asyncio.run(run("what time is it"))
    assert intent_ == "time" and re.fullmatch(r"It's \d{1,2}:\d{2} [AP]M\.", clock)
    assert asyncio.run(run("what is the meaning of life")) is None
    assert asyncio.run(run("turn off the desk lamp")) is None
    calls = [row["tool"] for row in storage.tool_calls("q1")]
    assert "mcp__time__set_timer" in calls and "mcp__lists__add_to_list" in calls


@pytest.mark.live
@pytest.mark.live
def test_weather_is_answered_in_one_short_sentence_from_the_forecast(quick):
    q, ctx, _ = quick
    try:
        fetched = q.forecast("imperial")
    except OSError as e:
        pytest.skip(f"Open-Meteo did not answer: {e}")
    assert len(fetched["daily"]["time"]) == 2
    started = time.perf_counter()
    intent_, reply = asyncio.run(q.handle("what's the weather like", ctx))
    assert intent_ == "weather" and re.fullmatch(r"It's -?\d+ degrees and [a-z ]+ right now\. Today's high is -?\d+, low -?\d+, and .+\.", reply)
    intent_, again = asyncio.run(q.handle("how's the weather", ctx))
    assert intent_ == "weather" and again == reply
    intent_, reply = asyncio.run(q.handle("will it rain tomorrow", ctx))
    assert intent_ == "weather" and re.match(r"(Yes|Maybe|No)\b", reply) and "tomorrow" in reply
    assert time.perf_counter() - started < 0.5


def generated_commands():
    durations = ["five minutes", "10 minutes", "an hour", "half an hour", "90 seconds", "twenty five minutes", "an hour and a half", "3 hours", "a minute", "forty five seconds"]
    for d in durations:
        for template in ("set a timer for {d}", "start a timer for {d}", "can you set a timer for {d} please", "timer for {d}",
                         "put on a timer for {d}", "hey set a timer for {d} for me"):
            yield template.format(d=d), "timer"
    for item in ("eggs", "milk and bread", "paper towels", "batteries", "dog food", "coffee filters", "laundry soap", "apples, bananas and grapes"):
        for name in ("shopping", "grocery", "to do", "costco"):
            yield f"add {item} to my {name} list", "lists"
            yield f"put {item} on the {name} list", "lists"
            yield f"take {item} off the {name} list", "lists"
            yield f"remove {item} from my {name} list", "lists"
    for name in ("shopping", "grocery", "to do", "costco"):
        for template in ("what's on my {n} list", "read me the {n} list", "what is on the {n} list", "what do i have on my {n} list"):
            yield template.format(n=name), "lists"
    for when in ("", " today", " tomorrow", " right now", " tonight"):
        for template in ("what's the weather{w}", "how's the weather{w}", "what's the weather like{w}", "will it rain{w}",
                         "is it going to rain{w}", "what's the forecast{w}", "what's the temperature{w}", "do i need an umbrella{w}"):
            yield template.format(w=when), "weather"
    for clock in ("6 am", "6:30 am", "7:15 am", "5:45 am", "9 pm", "10:30 pm"):
        yield f"set an alarm for {clock}", "alarm"
        yield f"wake me up at {clock}", "alarm"
    for kind in ("timer", "alarm", "reminder"):
        for template in ("cancel the {k}", "cancel my {k}", "cancel all my {k}s", "delete the {k}", "cancel the pizza {k}"):
            yield template.format(k=kind), "cancel"
    for text in ("what time is it", "what's the time", "tell me the time", "what time is it right now", "time"):
        yield text, "time"
    for text in ("what's the date", "what's today's date", "what day is it", "what day is today", "what's the date today"):
        yield text, "date"
    for text in ("turn on the ps5", "turn off the playstation", "turn the ps5 on", "put the ps5 in rest mode"):
        yield text, "ps5"
    for text in ("ring my phone", "find my phone", "where's my phone", "what's my phone's battery", "is my phone charging"):
        yield text, "phone"


CLAUDE_ONLY = (
    "remind me at 7 pm to call mom", "remember that dad's office computer is named office pc", "what's the name of dad's computer",
    "forget my favorite pizza", "turn off the bedroom light", "what's the temperature in my bedroom", "dim the lights to 20 percent",
    "any new texts", "read my notifications", "text mom i'm on my way", "is the internet down", "how fast is the internet",
    "turn on my desktop", "ping the router", "what devices are on the network", "open chrome", "what's on my screen",
    "set the thermostat to 68 degrees", "what's the weather in seattle", "what's a good movie to watch tonight",
    "give me two tips for falling asleep faster", "what's the capital of australia", "read me the latest news",
    "what's on my calendar tomorrow", "do i have any new email", "add dentist at 3 pm thursday to my calendar", "my computer blue screened",
    "can you fix it for me", "what's the weather going to be like next week", "will it rain on saturday", "what's the weather in paris tomorrow",
    "set a timer", "set an alarm", "set an alarm for 7", "call me later", "call it a day", "time to go", "what time does target close",
    "what time is the seahawks game", "what's the date of thanksgiving", "how long is the movie dune", "add it to my calendar",
    "play rain sounds on the tv", "what's the weather like in tokyo right now", "why is it so cold in here", "is it hot in phoenix",
    "add a reminder to call mom", "what's on my calendar", "turn on the kitchen light", "what's my wife's phone number",
    "can you ring the doorbell", "stop the alarm", "turn off my alarm", "take the hub test package off my list",
    "remove the amazon package from my list", "add this to my list", "what's on my list",
)


def test_hundreds_of_phrasings_go_to_the_right_place(quick):
    q, _, _ = quick
    cases = list(generated_commands())
    assert len(cases) >= 280
    wrong = [(text, want, intent(q, text)) for text, want in cases if intent(q, text) != want]
    assert wrong == []
    claimed = [(text, intent(q, text)) for text in CLAUDE_ONLY if intent(q, text) is not None]
    assert claimed == []
