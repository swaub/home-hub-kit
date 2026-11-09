import subprocess
import tomllib
from importlib import metadata
from pathlib import Path

PACKAGE = "homehub"
REPO_ROOT = Path(__file__).resolve().parents[1]


def package_version():
    try:
        return metadata.version(PACKAGE)
    except metadata.PackageNotFoundError:
        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            return tomllib.load(f)["project"]["version"]


def commit():
    try:
        out = subprocess.run(["git", "-C", str(REPO_ROOT), "describe", "--always", "--dirty"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def describe():
    sha = commit()
    return f"{PACKAGE} {package_version()}" + (f" ({sha})" if sha else "")
