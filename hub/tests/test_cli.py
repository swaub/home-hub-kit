import logging
import os
import select
import subprocess
import sys
import time
from datetime import date, timedelta

from homehub import config
from homehub.instance import StatusFile
from homehub.logging_setup import log_file_name, setup_logging


def write_config(tmp_path):
    text = config.EXAMPLE_PATH.read_text(encoding="utf-8")
    text = text.replace('data_dir = "~/.local/share/homehub"', f'data_dir = "{tmp_path / "data"}"')
    text = text.replace('state_dir = "~/.local/state/homehub"', f'state_dir = "{tmp_path / "state"}"')
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def run_cli(cfg_path, *args, timeout=120):
    return subprocess.run([sys.executable, "-m", "homehub", "--config", str(cfg_path), *args], capture_output=True, text=True, timeout=timeout)


def test_status_when_never_started(tmp_path):
    out = run_cli(write_config(tmp_path), "status")
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert lines[0] == "running: no (never started on this machine)"
    assert lines[1].startswith("version: homehub ")
    assert any(l.startswith("claude: ") for l in lines)
    assert any(l.startswith("cost today: $0.0000 API-equivalent over 0 turn(s)") for l in lines)


def test_status_reports_a_live_and_a_dead_writer(tmp_path):
    cfg_path = write_config(tmp_path)
    StatusFile(tmp_path / "state").write(state={"app": "running"}, last_error="Claude was unreachable")
    out = run_cli(cfg_path, "status")
    assert out.stdout.startswith(f"running: yes (pid {os.getpid()}, started ")
    assert "state: app: running" in out.stdout
    assert "last error: Claude was unreachable" in out.stdout
    child = subprocess.Popen([sys.executable, "-c", f"from homehub.instance import StatusFile\nStatusFile({str(tmp_path / 'state')!r}).write(state='starting')"])
    assert child.wait(timeout=60) == 0
    out = run_cli(cfg_path, "status")
    assert out.stdout.startswith("running: no (last status ") and f"from pid {child.pid}, which has exited" in out.stdout


def test_logs_prints_the_day_and_follows_new_lines(tmp_path):
    cfg_path = write_config(tmp_path)
    directory = setup_logging(tmp_path / "state")
    log = logging.getLogger("homehub.test")
    for i in range(5):
        log.info("line %d", i)
    out = run_cli(cfg_path, "logs")
    assert out.returncode == 0 and out.stdout.count("homehub.test: line") == 5
    out = run_cli(cfg_path, "logs", "-n", "2")
    assert out.stdout.count("homehub.test: line") == 2 and "line 4" in out.stdout
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    out = run_cli(cfg_path, "logs", "--date", yesterday)
    assert out.returncode == 1 and out.stderr.startswith(f"no log for {yesterday}")
    (directory / log_file_name(date.today() - timedelta(days=1))).write_text("old line\n", encoding="utf-8")
    assert run_cli(cfg_path, "logs", "--date", yesterday).stdout == "old line\n"
    follower = subprocess.Popen([sys.executable, "-m", "homehub", "--config", str(cfg_path), "logs", "-f", "-n", "1"],
                                stdout=subprocess.PIPE, text=True)
    try:
        assert "line 4" in follower.stdout.readline()
        time.sleep(0.5)
        log.info("appended while following")
        ready, _, _ = select.select([follower.stdout], [], [], 10)
        assert ready and "appended while following" in follower.stdout.readline()
    finally:
        follower.terminate()
        follower.wait(timeout=10)


def test_doctor_runs_end_to_end(tmp_path):
    out = run_cli(write_config(tmp_path), "doctor")
    assert out.returncode in (0, 1), out.stderr
    lines = out.stdout.splitlines()
    assert lines[0].startswith("OK    config: ")
    assert any(l.startswith("OK    household: ") for l in lines) and not any(" models:" in l or " audio:" in l for l in lines)
    assert lines[-1].endswith(("all checks passed", "warning(s)"))
    assert (out.returncode == 1) == any(l.startswith("FAIL") for l in lines)


def test_bad_config_exits_with_code_2(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[brain]\nmax_turns = 0\n", encoding="utf-8")
    out = run_cli(bad, "status")
    assert out.returncode == 2 and "brain.max_turns must be at least 1" in out.stderr


def test_vpn_add_refuses_a_bad_name_before_touching_anything(tmp_path, capsys):
    from homehub.cli import main

    config = tmp_path / "config.toml"
    config.write_text(f'[paths]\ndata_dir = "{(tmp_path / "data").as_posix()}"\nstate_dir = "{(tmp_path / "state").as_posix()}"\n', encoding="utf-8")
    assert main(["--config", str(config), "vpn", "add", "../etc"]) == 1
    assert "letters, numbers and dashes" in capsys.readouterr().out


def test_notify_parses_a_title_and_a_message_and_says_when_it_is_not_set_up(tmp_path):
    from homehub.cli import build_parser, cmd_notify

    args = build_parser().parse_args(["notify", "--title", "Hub job failed", "duckdns-update.service", "failed"])
    assert args.func is cmd_notify and args.title == "Hub job failed" and args.message == ["duckdns-update.service", "failed"] and args.priority == 3
    assert build_parser().parse_args(["notify", "--priority", "4", "hello"]).priority == 4
    out = run_cli(write_config(tmp_path), "notify", "--title", "T", "hello")
    assert out.returncode == 1 and "push notifications aren't set up" in out.stderr


def test_notify_reports_an_unreachable_server(tmp_path):
    cfg_path = write_config(tmp_path)
    text = cfg_path.read_text(encoding="utf-8")
    start = text.index("[notify]")
    end = text.index("\n[", start + 1)
    cfg_path.write_text(text[:start] + '[notify]\nurl = "http://127.0.0.1:1"\ntopic = "hub-test"\n' + text[end:], encoding="utf-8")
    out = run_cli(cfg_path, "notify", "hello")
    assert out.returncode == 1 and "couldn't send the notification" in out.stderr
