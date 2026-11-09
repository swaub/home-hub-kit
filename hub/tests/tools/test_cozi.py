import asyncio
from datetime import date

import pytest

from homehub.config import load_config
from homehub.tools import result_texts
from homehub.tools.cozi import (NOT_SET_UP, Cozi, CoziError, build_module, clock, day_entries, describe_entry, load_login, parse_day,
                                   parse_time)

MONTH = {"days": {"2026-11-02": [{"id": "b"}, {"id": "a"}, {"id": "94983380355001-r"}], "2026-11-03": []},
         "items": {"a": {"description": "Brady in Mexicali ", "startTime": "00:00:00", "endTime": "00:00:00"},
                   "b": {"description": "Dentist", "startTime": "16:00:00", "endTime": "17:30:00"},
                   "r": {"description": "Trash day", "startTime": "07:00:00", "endTime": "07:15:00"}}}


def test_days_and_times_read_the_way_people_say_them():
    monday = date(2026, 11, 2)
    assert parse_day("", monday) == monday and parse_day("tomorrow", monday) == date(2026, 11, 3)
    assert parse_day("Friday", monday) == date(2026, 11, 6) and parse_day("monday", monday) == monday
    assert parse_day("2026-12-24", monday) == date(2026, 12, 24)
    with pytest.raises(CoziError):
        parse_day("someday", monday)
    assert [parse_time(t) for t in ("15:30", "3:30 pm", "9am", "12 am", "12:15 PM")] == ["15:30:00", "15:30:00", "09:00:00", "00:00:00", "12:15:00"]
    for bad in ("25:00", "noon-ish", "7:75"):
        with pytest.raises(CoziError):
            parse_time(bad)
    assert clock("16:00:00") == "4 PM" and clock("07:15:00") == "7:15 AM" and clock("00:30:00") == "12:30 AM"


def test_a_day_lists_all_day_entries_first_then_by_time_including_repeating_ones():
    entries = day_entries(MONTH, "2026-11-02")
    assert [describe_entry(e) for e in entries] == ["all day: Brady in Mexicali", "7 AM to 7:15 AM: Trash day", "4 PM to 5:30 PM: Dentist"]
    assert day_entries(MONTH, "2026-11-03") == [] and day_entries(MONTH, "2026-11-09") == []


def test_cozi_stays_off_until_a_login_is_saved(private_config):
    assert load_login(private_config) is None and not build_module(private_config).available(private_config)
    tools = {spec.name: spec for spec in build_module(private_config).tools}
    assert tools["cozi_add"].policy == "confirm" and tools["cozi_events"].policy == "auto"
    assert result_texts(asyncio.run(tools["cozi_events"].fn.handler({"day": "today"})))[0] == NOT_SET_UP
    assert tools["cozi_add"].describe({"title": "Dentist", "day": "Friday", "start": "3 pm", "who": ["Kyle"]}) == \
        "add 'Dentist' to the Cozi calendar on Friday at 3 pm for Kyle"
    with pytest.raises(CoziError, match="title"):
        Cozi(private_config).add("  ", date(2026, 11, 6))


@pytest.mark.live
def test_the_real_family_calendar_reads_a_week():
    cfg = load_config()
    if load_login(cfg) is None:
        pytest.skip("no [cozi] login on this machine")
    cozi = Cozi(cfg)
    text = cozi.events(date.today(), 7)
    assert text and "Cozi said" not in text
    assert len(cozi.people()) >= 1
    assert cozi.month(date.today().year, date.today().month)["items"]
