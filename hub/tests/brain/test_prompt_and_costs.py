from datetime import date

from homehub import household
from homehub.brain import prompt
from homehub.brain.costs import CostTracker
from homehub.household import sync_household
from homehub.tools import Registry
from homehub.tools import builtins, google, memory


def test_system_prompt_contains_the_rules_tools_people_and_memory(example_config, temp_storage, tmp_path):
    with_town = household.EXAMPLE_PATH.read_text(encoding="utf-8").replace("[household]\n", '[household]\ntown = "Springfield, Illinois"\nvocabulary = ["Springfield", "Decatur"]\n')
    people_path = tmp_path / "people.toml"
    people_path.write_text(with_town, encoding="utf-8")
    sync_household(example_config, temp_storage, people_path)
    temp_storage.remember("Sam's office computer is named OFFICE-PC.", owner_id="alex")
    temp_storage.remember("Alex's private note.", owner_id="alex", visibility="private")
    temp_storage.remember("Sam's private note.", owner_id="sam", visibility="private")
    registry = Registry(example_config, [memory.build_module(temp_storage), google.MODULE, builtins.WEB, builtins.FILES, builtins.SHELL])
    text = prompt.build_system_prompt(example_config, registry, temp_storage, temp_storage.person("alex"))
    assert text.startswith(prompt.PERSONA)
    for part in (memory.PROMPT, google.PROMPT, builtins.WEB_PROMPT, prompt.APPROVAL_RULE, prompt.UNTRUSTED_RULE,
                 prompt.BRIEFING_RULE, prompt.LIMITS_RULE, prompt.TIME_RULE):
        assert part in text
    assert "You are talking with Alex (owner of the household)." in text
    assert "Their devices: Android phone (phone, also called Alex's phone, my phone)." in text
    assert "Other people in the household: Riley (adult), Sam (adult)." in text
    assert "They use imperial units." in text
    assert "The household lives in Springfield, Illinois" in text and "Local names, spelled correctly: Springfield, Decatur." in text
    assert text.index(prompt.APPROVAL_RULE) < text.index("The household lives") < text.index(prompt.UNTRUSTED_RULE)
    assert "- Sam's office computer is named OFFICE-PC.\n- Alex's private note." in text
    assert "Sam's private note." not in text
    assert prompt.SCREEN_RULES not in text and prompt.PHONE_RULES not in text
    assert "Bash commands" in text or "PowerShell commands" in text


def test_prompt_for_an_unknown_speaker_and_without_memory(example_config, temp_storage):
    registry = Registry(example_config, [builtins.WEB])
    text = prompt.build_system_prompt(example_config, registry, temp_storage, None)
    assert "You don't know who is asking: this device isn't linked to anyone in the household." in text
    assert "The household lives" not in text and "spelled correctly" not in text
    assert "Things you've been asked to remember" not in text
    assert google.PROMPT not in text and memory.PROMPT not in text


def test_cost_tracker_records_turns_and_totals(temp_storage):
    events = []
    temp_storage.start_conversation("c1", "bedroom", "alex")
    tracker = CostTracker(temp_storage, emit=lambda name, **p: events.append((name, p)))
    tracker.record("c1", "alex", "claude-haiku-5-5", "what's the weather", "Sunny.", 2, 0.0012, {"first_sentence": 900})
    tracker.record("c1", "alex", "claude-haiku-5-5", "thanks", "Welcome.", 1, 0.0005)
    assert tracker.turns == 2 and abs(tracker.session_usd - 0.0017) < 1e-9
    assert temp_storage.conversation("c1")["turns"] == 2
    assert events[-1][0] == "cost.turn" and events[-1][1]["session_usd"] == 0.0017 and events[-1][1]["day_usd"] == 0.0017
    assert temp_storage.cost_total(day=date.today()) == 0.0017
    assert tracker.summary().startswith("$0.0017 this session over 2 turn(s), $0.0017 today")
    tracker.reset_session()
    assert tracker.session_usd == 0 and tracker.turns == 0


def test_the_first_spoken_piece_can_end_at_a_clause_so_the_voice_starts_sooner():
    from homehub.brain.session import split_ready

    assert split_ready("It's about twenty-four past one, and", True) == (["It's about twenty-four past one,"], "and")
    assert split_ready("It's about twenty-four past one, and", False) == ([], "It's about twenty-four past one, and")
    assert split_ready("Sure, I can do", True) == ([], "Sure, I can do")
    assert split_ready("That costs about 1,250 dollars", True) == ([], "That costs about 1,250 dollars")
    assert split_ready("Done. The timer is set, and", True) == (["Done."], "The timer is set, and")
    assert split_ready("The weather today looks cloudy; highs", True) == (["The weather today looks cloudy;"], "highs")
