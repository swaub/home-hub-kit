import re
import subprocess
import sys

from homehub.version import describe, package_version


def test_describe_starts_with_name_and_version():
    assert describe().startswith(f"homehub {package_version()}")


def test_describe_includes_the_git_commit():
    assert re.search(r"\([0-9a-f]{7,}", describe())


def test_cli_version_prints_describe():
    out = subprocess.run([sys.executable, "-m", "homehub", "--version"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0
    assert out.stdout.strip() == describe()


def test_cli_without_arguments_prints_help():
    out = subprocess.run([sys.executable, "-m", "homehub"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0
    assert out.stdout.startswith("usage: homehub")
