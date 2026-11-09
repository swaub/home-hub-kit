import io
import subprocess
import sys

from homehub import config
from homehub.cli import Renderer
from homehub.config import load_config


def test_renderer_formats_transcript_and_service_lines():
    cfg = load_config(config.EXAMPLE_PATH)
    out = io.StringIO()
    renderer = Renderer(cfg, out=out, color=False)
    for event in [
        {"name": "service.starting", "version": "x"},
        {"name": "transcript.line", "room": "remote", "kind": "you", "text": "what time is it"},
        {"name": "transcript.line", "room": "remote", "kind": "claude", "text": "It's 9:31 PM."},
        {"name": "transcript.line", "room": "app", "kind": "action", "text": "timer due: Your timer is done."},
        {"name": "brain.error", "room": "remote", "kind": "offline", "message": "ENOTFOUND"},
        {"name": "reminder.due", "kind": "timer", "label": "pizza"},
        {"name": "person.identified", "room": "remote", "person_id": "alex", "how": "vpn_key"},
        {"name": "service.ready", "startup_seconds": 0.4, "checks": {}},
    ]:
        renderer.handle(event)
    assert out.getvalue().splitlines() == [
        "Starting up...",
        "[YOU] what time is it",
        "[CLAUDE] It's 9:31 PM.",
        "[ACTION] timer due: Your timer is done.",
        "[ERROR] Claude (offline): ENOTFOUND",
        "[REMINDER] timer: pizza",
        "[WHO] alex (vpn key)",
        "Ready in 0.4 s",
    ]
    colored = io.StringIO()
    Renderer(cfg, out=colored, color=True).handle({"name": "transcript.line", "room": "remote", "kind": "you", "text": "hi"})
    assert colored.getvalue() == "\033[92m[YOU] hi\033[0m\n"


def test_run_starts_without_any_devices_and_stops_on_its_own(tmp_path):
    text = config.EXAMPLE_PATH.read_text(encoding="utf-8")
    text = text.replace('data_dir = "~/.local/share/homehub"', f'data_dir = "{tmp_path / "data"}"')
    text = text.replace('state_dir = "~/.local/state/homehub"', f'state_dir = "{tmp_path / "state"}"')
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(text, encoding="utf-8")
    out = subprocess.run([sys.executable, "-m", "homehub", "--config", str(cfg_path), "run", "--seconds", "6"], capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr[-800:]
    assert "Starting up..." in out.stdout and "Ready in" in out.stdout
