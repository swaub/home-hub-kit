import faulthandler
import logging
import re
import subprocess
import sys
from datetime import date, timedelta

from homehub import logging_setup
from homehub.logging_setup import (DailyFileHandler, log_event, log_file_name, parse_event_line, prune, redact,
                                      setup_logging)

LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2} (INFO|WARNING|ERROR) [\w.]+: .+$")


def today_file(directory):
    return directory / log_file_name(date.today())


def test_lines_go_to_todays_file_in_the_documented_format(tmp_path):
    directory = setup_logging(tmp_path)
    logging.getLogger("homehub.test").info("hello from the test")
    lines = today_file(directory).read_text(encoding="utf-8").splitlines()
    assert lines[-1].endswith(" INFO homehub.test: hello from the test")
    assert LINE.match(lines[-1])


def test_setup_twice_appends_and_never_truncates(tmp_path):
    directory = setup_logging(tmp_path)
    logging.getLogger("homehub.test").info("first run")
    setup_logging(tmp_path)
    logging.getLogger("homehub.test").info("second run")
    text = today_file(directory).read_text(encoding="utf-8")
    assert "first run" in text and "second run" in text
    assert len([h for h in logging.getLogger().handlers if getattr(h, "homehub", False)]) == 1


def test_two_processes_append_to_the_same_file_without_losing_lines(tmp_path):
    script = ("import logging, sys\nfrom homehub.logging_setup import setup_logging\n"
              "setup_logging(sys.argv[1])\nlog = logging.getLogger('homehub.proc')\n"
              "for i in range(400):\n    log.info('writer %s line %04d', sys.argv[2], i)\n")
    procs = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), name]) for name in ("A", "B")]
    assert [p.wait(timeout=60) for p in procs] == [0, 0]
    lines = (tmp_path / "logs" / log_file_name(date.today())).read_text(encoding="utf-8").splitlines()
    written = [l for l in lines if "homehub.proc: writer" in l]
    assert len(written) == 800
    assert all(re.search(r"writer [AB] line \d{4}$", l) for l in written)
    assert sorted(int(l[-4:]) for l in written if "writer A" in l) == list(range(400))


def test_secrets_are_redacted_before_they_reach_the_file(tmp_path):
    directory = setup_logging(tmp_path)
    log = logging.getLogger("homehub.test")
    log.info("key sk-ant-api03-AbCdEfGhIjKlMnOp-123456 was set")
    log.info("header Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    log.info("login password=hunter2 token: tok_abcdefgh12345")
    log.info("github ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456")
    text = today_file(directory).read_text(encoding="utf-8")
    for secret in ("AbCdEfGhIjKlMnOp", "eyJhbGciOiJIUzI1NiJ9", "hunter2", "tok_abcdefgh12345", "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"):
        assert secret not in text
    assert "sk-ant-[redacted] was set" in text
    assert "Bearer [redacted]" in text
    assert "password=[redacted] token=[redacted]" in text
    assert "ghp_[redacted]" in text


def test_redact_leaves_ordinary_text_alone():
    text = "Timer set: timer 'pizza' at 7:30 PM today (in 20 min 0 s) [id a1b2c3]"
    assert redact(text) == text


def test_old_logs_are_pruned_and_other_files_kept(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    today = date.today()
    ages = {31: "old", 30: "edge", 29: "recent", 1: "yesterday", 0: "today"}
    for days, label in ages.items():
        (logs / log_file_name(today - timedelta(days=days))).write_text(label, encoding="utf-8")
    (logs / "crash.log").write_text("keep", encoding="utf-8")
    (logs / "notes.txt").write_text("keep", encoding="utf-8")
    removed = prune(logs, 30, today)
    assert sorted(p.name for p in removed) == sorted(log_file_name(today - timedelta(days=d)) for d in (30, 31))
    remaining = sorted(p.name for p in logs.iterdir())
    assert log_file_name(today - timedelta(days=29)) in remaining
    assert "crash.log" in remaining and "notes.txt" in remaining
    assert log_file_name(today) in remaining


def test_setup_prunes_on_start(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    stale = logs / log_file_name(date.today() - timedelta(days=45))
    stale.write_text("stale", encoding="utf-8")
    setup_logging(tmp_path, retention_days=30)
    assert not stale.exists()


def test_rollover_switches_to_the_new_days_file(tmp_path):
    handler = DailyFileHandler(tmp_path, 30)
    first = handler.path
    handler.rollover(date.today() + timedelta(days=1))
    assert handler.path != first
    assert handler.path.name == log_file_name(date.today() + timedelta(days=1))
    assert handler.path.exists()
    handler.close()


def test_crash_log_is_enabled(tmp_path):
    directory = setup_logging(tmp_path)
    assert faulthandler.is_enabled()
    assert (directory / "crash.log").exists()


def test_warnings_are_captured_into_the_log(tmp_path):
    script = ("import sys, warnings\nfrom homehub.logging_setup import setup_logging\nsetup_logging(sys.argv[1])\n"
              "warnings.warn('can_use_tool will not be invoked for shadowed tools', UserWarning)\n")
    assert subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=60).returncode == 0
    assert "can_use_tool will not be invoked" in (tmp_path / "logs" / log_file_name(date.today())).read_text(encoding="utf-8")


def test_events_round_trip_as_json(tmp_path):
    directory = setup_logging(tmp_path)
    log_event("speech.final", {"room": "bedroom", "text": "what's the weather", "stt_ms": 310, "person_id": None})
    line = today_file(directory).read_text(encoding="utf-8").splitlines()[-1]
    assert parse_event_line(line) == ("speech.final", {"room": "bedroom", "text": "what's the weather", "stt_ms": 310, "person_id": None})
    assert parse_event_line("2026-10-23T09:00:00.000-07:00 INFO homehub.test: not an event") is None


def test_console_handler_is_optional_and_not_duplicated(tmp_path, capsys):
    setup_logging(tmp_path, console=True)
    setup_logging(tmp_path, console=True)
    logging.getLogger("homehub.test").info("to the console")
    assert capsys.readouterr().out.count("to the console") == 1
    setup_logging(tmp_path)
    assert all(isinstance(h, DailyFileHandler) for h in logging.getLogger().handlers if getattr(h, "homehub", False))


def test_module_constants_describe_the_file_layout():
    assert logging_setup.log_file_name(date(2026, 10, 23)) == "homehub-2026-10-23.log"
    assert logging_setup.log_file_date("homehub-2026-10-23.log") == date(2026, 10, 23)
    assert logging_setup.log_file_date("crash.log") is None
