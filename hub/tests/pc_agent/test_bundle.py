import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import bundle_agent
from homehub.pc_agent import AGENT_VERSION


def test_bundle_holds_the_installer_and_a_stdlib_only_agent(tmp_path):
    out = tmp_path / "homehub-agent.zip"
    names = bundle_agent.build(out)
    assert {"homehub-agent/install_agent.ps1", "homehub-agent/uninstall_agent.ps1", "homehub-agent/HOW TO INSTALL.txt"} <= set(names)
    for name in ("__init__.py", "__main__.py", "auth.py", "ops.py", "server.py", "capture.ps1", "notice.ps1"):
        assert f"homehub-agent/pc_agent/{name}" in names
    assert not [n for n in names if n.endswith(".py") and "/pc_agent/" not in n]
    with zipfile.ZipFile(out) as archive:
        archive.extractall(tmp_path)
    package = tmp_path / "agent" / "homehub" / "pc_agent"
    package.parent.mkdir(parents=True)
    (package.parent / "__init__.py").touch()
    (tmp_path / "homehub-agent" / "pc_agent").rename(package)
    code = ("import homehub.pc_agent.server; from homehub.pc_agent.ops import run_operation; "
            "print(run_operation('agent.info', {})['version'], run_operation('agent.ping', {})['pong'])")
    run = subprocess.run([sys.executable, "-S", "-E", "-s", "-c", code], cwd=tmp_path / "agent", capture_output=True, text=True, timeout=60)
    assert run.returncode == 0 and run.stdout.strip() == f"homehub-agent {AGENT_VERSION} True", run.stderr


def test_installer_pins_python_and_finds_the_agent_in_both_layouts():
    text = (ROOT / "scripts" / "install_agent.ps1").read_text()
    version = re.search(r'\$PythonVersion = "(\d+\.\d+\.\d+)"', text).group(1)
    assert tuple(int(p) for p in version.split(".")) >= (3, 11)
    assert len(re.findall(r'(?:amd64|arm64) = "[0-9a-f]{64}"', text)) == 2
    assert "Get-FileHash" in text and "pc_agent" in text and "homehub\\pc_agent" in text
    assert "-RunLevel Highest" in text and "LocalSubnet" in text and "-Program $python" in text
    assert '-replace "[^a-z0-9]", "_"' in text
    for script in ("capture.ps1", "notice.ps1"):
        assert (ROOT / "homehub" / "pc_agent" / script).read_text().startswith("param(")
