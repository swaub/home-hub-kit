import dataclasses
import json

from homehub import config
from homehub.brain.session import build_options, hidden_user_servers, user_mcp_servers
from homehub.config import load_config
from homehub.tools import Registry, ToolModule, ToolSpec, builtins


def write_claude_json(path):
    path.write_text(json.dumps({"mcpServers": {"ghidra": {"command": "x"}, "xbox": {"command": "y"}},
                                "projects": {"/home/someone": {"mcpServers": {"blender": {"command": "z"}}}}}))
    return path


def test_user_mcp_servers_are_read_from_the_claude_config(tmp_path):
    assert user_mcp_servers(tmp_path / "missing.json") == []
    (tmp_path / "broken.json").write_text("{")
    assert user_mcp_servers(tmp_path / "broken.json") == []
    assert user_mcp_servers(write_claude_json(tmp_path / ".claude.json")) == ["blender", "ghidra", "xbox"]


def test_servers_that_are_not_ours_are_hidden_only_when_google_loads_user_settings(tmp_path, monkeypatch):
    cfg = load_config(config.EXAMPLE_PATH)
    module = ToolModule(server="xbox", tools=[ToolSpec(name="power", policy="auto")])
    registry = Registry(cfg, [module, builtins.WEB, builtins.HIDDEN_BUILTINS])
    path = write_claude_json(tmp_path / ".claude.json")
    assert hidden_user_servers(registry, path) == ["mcp__blender", "mcp__ghidra"]
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert cfg.features.google
    disallowed = build_options(cfg, registry, "prompt").disallowed_tools
    assert "mcp__ghidra" in disallowed and "mcp__blender" in disallowed and "mcp__xbox" not in disallowed
    assert "Task" in disallowed
    cfg = dataclasses.replace(cfg, features=dataclasses.replace(cfg.features, google=False))
    assert "mcp__ghidra" not in build_options(cfg, registry, "prompt").disallowed_tools
