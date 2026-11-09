import subprocess
import sys

from homehub import config
from homehub.cli import TEST_STAGES


def test_every_stage_points_at_an_existing_test_path():
    for stage, path in TEST_STAGES.items():
        assert (config.REPO_ROOT / path).exists(), stage


def test_test_command_runs_a_stage_and_rejects_unknown_ones():
    out = subprocess.run([sys.executable, "-m", "homehub", "test", "bus"], capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.startswith(f"{sys.executable} -m pytest -q tests/test_bus.py -m not live and not claude")
    assert "passed" in out.stdout
    out = subprocess.run([sys.executable, "-m", "homehub", "test", "nothing"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 2 and "unknown stage 'nothing'" in out.stderr


def test_voice_commands_are_gone():
    for command in ("say", "devices", "bench", "record"):
        out = subprocess.run([sys.executable, "-m", "homehub", command], capture_output=True, text=True, timeout=60)
        assert out.returncode == 2 and "invalid choice" in out.stderr, command
