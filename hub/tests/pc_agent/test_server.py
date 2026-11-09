import json
import platform
import urllib.error
import urllib.request

import pytest

from homehub.pc_agent import ops
from homehub.pc_agent.auth import new_key
from homehub.pc_agent.server import AgentConfig, serve
from homehub.tools.pc_remote import PcAgentClient


@pytest.fixture
def agent(tmp_path):
    config = AgentConfig(new_key(), bind="127.0.0.1", port=0, name="testbox")
    lines = []
    server = serve(config, lines.append)
    client = PcAgentClient("127.0.0.1", config.key, port=server.server_address[1])
    yield server, client, config, lines
    server.shutdown()
    server.server_close()


def test_config_round_trips_and_rejects_short_keys(tmp_path):
    with pytest.raises(ValueError, match="at least 32"):
        AgentConfig("short")
    path = AgentConfig(new_key(), bind="192.168.1.5", port=7000, name="OFFICE-PC", allow_fixes=False).write(tmp_path / "ProgramData" / "agent.toml")
    loaded = AgentConfig.load(path)
    assert (loaded.bind, loaded.port, loaded.name, loaded.allow_fixes) == ("192.168.1.5", 7000, "OFFICE-PC", False) and len(loaded.key) == 64
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert AgentConfig.load(path).key == loaded.key


def test_health_is_open_and_everything_else_needs_a_valid_signature(agent):
    server, client, config, lines = agent
    health = client.health()
    assert health["ok"] is True and health["name"] == "testbox" and health["uptime_seconds"] >= 0
    bad = urllib.request.Request(client.url("/op/agent.ping"), data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(bad, timeout=5)
    assert e.value.code == 401 and json.loads(e.value.read())["error"] == "missing signature headers"
    wrong = PcAgentClient("127.0.0.1", new_key(), port=server.server_address[1])
    assert wrong.call("agent.ping") == {"error": "bad signature"}
    pong = client.call("agent.ping")
    assert pong["ok"] is True and pong["result"]["pong"] is True and pong["ms"] >= 0
    info = client.call("agent.info")["result"]
    assert info["system"] == platform.system() and "fix.reboot" in info["operations"] and "diagnostics.summary" in info["operations"]
    assert client.call("fix.format_everything") == {"error": "unknown operation fix.format_everything"}
    assert urllib.request.urlopen(client.url("/health"), timeout=5).status == 200
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(client.url("/nothing"), timeout=5)
    assert e.value.code == 404
    assert [name for _, name, _ in server.calls] == ["agent.ping", "agent.info"]
    assert any("agent.ping -> 200" in line for line in lines)


def test_fixes_can_be_turned_off_per_computer(tmp_path):
    config = AgentConfig(new_key(), bind="127.0.0.1", port=0, allow_fixes=False)
    server = serve(config)
    try:
        client = PcAgentClient("127.0.0.1", config.key, port=server.server_address[1])
        assert client.call("fix.clear_temp") == {"error": "fixes are turned off on this computer"}
        assert client.call("fix.restart_service", name="bad;name") in ({"error": "fixes are turned off on this computer"},)
    finally:
        server.shutdown()
        server.server_close()


def test_operations_validate_arguments():
    with pytest.raises(ops.OpError, match="service's name"):
        ops.fix_restart_service({"name": "Spooler; Remove-Item C:\\"})
    with pytest.raises(ops.OpError, match="startup item's name"):
        ops.fix_disable_startup({"name": ""})
    with pytest.raises(KeyError):
        ops.run_operation("agent.nothing", {})
    assert set(ops.HANDLERS) == set(ops.OPERATIONS)


@pytest.mark.skipif(platform.system() != "Windows", reason="the diagnostics run PowerShell on Windows")
def test_live_windows_diagnostics_through_the_agent(agent):
    server, client, config, lines = agent
    summary = client.call("diagnostics.summary", timeout=120)
    assert summary["ok"], summary
    result = summary["result"]
    assert result["os"].startswith("Microsoft Windows") and result["memory"]["total_mb"] > 1000 and result["disks"] and result["top_processes"]
    assert all(0 <= d["free_percent"] <= 100 for d in result["disks"])
    disks = client.call("disk.health", timeout=120)
    assert disks.get("ok"), disks
    disks = disks["result"]
    assert disks["disks"] and all("HealthStatus" in d for d in disks["disks"])
    crashes = client.call("crashes.recent", timeout=120, days=14)["result"]
    assert crashes["days"] == 14 and crashes["count"] == len(crashes["events"])
    startup = client.call("startup.list", timeout=120)["result"]
    assert startup["count"] == len(startup["items"])
    screen = client.call("screen.capture", timeout=60, notice=False)["result"]
    assert screen["width"] > 400 and len(screen["jpeg_base64"]) > 1000 and 0 < screen["scale"] <= 1
    print(f"\n  {result['os']}; memory {result['memory']['used_percent']}% used; disks {[(d['drive'], d['free_gb']) for d in result['disks']]}; "
          f"{crashes['count']} crash events in 14 days; {startup['count']} startup items; screen {screen['width']}x{screen['height']}")


@pytest.mark.skipif(platform.system() == "Windows", reason="the Linux hub cannot run Windows diagnostics itself")
def test_windows_only_operations_say_so_elsewhere(agent):
    server, client, config, lines = agent
    reply = client.call("diagnostics.summary")
    assert reply["ok"] is False and "needs Windows" in reply["error"]
