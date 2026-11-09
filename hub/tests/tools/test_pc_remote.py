import asyncio
import platform

import pytest

from homehub import household
from homehub.household import sync_household
from homehub.pc_agent.auth import new_key
from homehub.pc_agent.server import AgentConfig, serve
from homehub.tools import CallContext, current_context, result_texts
from homehub.tools.pc_remote import FIX_WORDS, PcAgentClient, agent_key, build_module, resolve_pc, summarize
from homehub.tools.notify import Ntfy


def text(result):
    return result_texts(result)[0]


def call(tools, name, args):
    token = current_context.set(CallContext(room="bedroom", person_id="alex", role="owner"))
    try:
        return asyncio.run(tools[name](args))
    finally:
        current_context.reset(token)


@pytest.fixture
def world(example_config, temp_storage, tmp_path):
    import dataclasses

    cfg = dataclasses.replace(example_config, paths=dataclasses.replace(example_config.paths, data_dir=str(tmp_path / "data")))
    cfg.data_dir().mkdir(parents=True)
    sync_household(cfg, temp_storage, household.EXAMPLE_PATH)
    config = AgentConfig(new_key(), bind="127.0.0.1", port=0, name="OFFICE-PC")
    server = serve(config)
    port = server.server_address[1]
    temp_storage.set_device_runtime("sam_office_pc", last_ip="127.0.0.1")
    (cfg.data_dir() / "secrets.toml").write_text(f'[pc_agents]\nsam_office_pc = {{ key = "{config.key}", port = {port} }}\nold_pc = "{"a" * 64}"\n', encoding="utf-8")
    yield cfg, temp_storage, server, port
    server.shutdown()
    server.server_close()


def test_resolution_goes_through_the_registry_and_secrets(world):
    cfg, storage, server, port = world
    assert agent_key(cfg, "sam_office_pc") == server.config.key and agent_key(cfg, "nope") is None and agent_key(cfg, "old_pc") == "a" * 64
    device, client, problem = resolve_pc(storage, cfg, "the office computer")
    assert device["id"] == "sam_office_pc" and client.address == "127.0.0.1" and client.port == port and problem is None
    assert resolve_pc(storage, cfg, "toaster")[2].startswith("I don't know a computer")
    assert resolve_pc(storage, cfg, "Android phone")[2].startswith("I don't know a computer")
    storage.upsert_device("mom_laptop", "pc", "Riley's laptop", owner_id="riley", capabilities=["wake_on_lan"])
    assert "doesn't have the Home Hub agent" in resolve_pc(storage, cfg, "Riley's laptop")[2]
    storage.upsert_device("spare_pc", "pc", "Spare PC", capabilities=["pc_agent"])
    assert "don't have an address" in resolve_pc(storage, cfg, "Spare PC")[2]
    storage.set_device_runtime("spare_pc", hostname="SPARE")
    assert "isn't in secrets.toml" in resolve_pc(storage, cfg, "Spare PC")[2]
    storage.set_device_runtime("sam_office_pc", last_ip="127.0.0.2", hostname="localhost")
    device, client, problem = resolve_pc(storage, cfg, "the office computer")
    assert problem is None and client.address == "127.0.0.1" and client.port == port
    storage.set_device_runtime("sam_office_pc", hostname="nowhere.invalid")
    device, client, problem = resolve_pc(storage, cfg, "the office computer")
    assert problem is None and client.address == "127.0.0.2"


def test_tools_reach_the_agent_and_refuse_bad_fixes(world):
    cfg, storage, server, port = world
    module = build_module(storage, cfg, Ntfy("", ""))
    tools = {spec.name: spec.fn.handler for spec in module.tools}
    specs = {spec.name: spec for spec in module.tools}
    assert specs["pc_fix"].policy == "confirm" and specs["pc_fix"].describe({"device": "Sam's office computer", "fix": "sfc"}) == "run the Windows system file checker on Sam's office computer"
    assert specs["pc_check"].policy == "auto" and specs["pc_screen"].untrusted_output
    client = PcAgentClient("127.0.0.1", server.config.key, port=port)
    assert client.health()["name"] == "OFFICE-PC"
    reply = call(tools, "pc_check", {"device": "the office computer", "what": "nothing"})
    assert reply["is_error"] is True and "what must be" in text(reply)
    bad = call(tools, "pc_fix", {"device": "the office computer", "fix": "wipe"})
    assert bad["is_error"] is True and all(word in text(bad) for word in FIX_WORDS)
    missing = call(tools, "pc_check", {"device": "nothing here"})
    assert missing["is_error"] is True
    summary = call(tools, "pc_check", {"device": "the office computer"})
    if platform.system() == "Windows":
        assert "CPU" in text(summary) and "Drive C:" in text(summary)
    else:
        assert summary["is_error"] is True and "needs Windows" in text(summary)
    server.config.allow_fixes = False
    off = call(tools, "pc_fix", {"device": "the office computer", "fix": "clear_temp"})
    assert off["is_error"] is True and "turned off" in text(off)
    down = PcAgentClient("127.0.0.1", server.config.key, port=1)
    assert down.call("agent.ping")["ok"] is False


def test_summary_wording():
    result = {"os": "Microsoft Windows 11 Pro 10.0.26100", "last_boot": "2026-10-23T06:00:00", "cpu": {"name": "Intel i5", "load_percent": 12, "cores": 8},
              "memory": {"total_mb": 16384, "free_mb": 4096, "used_percent": 75},
              "disks": [{"drive": "C:", "size_gb": 512.0, "free_gb": 20.5, "free_percent": 4}],
              "top_processes": [{"name": "chrome", "cpu_seconds": 120.0, "memory_mb": 900}, {"name": "Teams", "cpu_seconds": 30.0, "memory_mb": 600}]}
    lines = summarize(result).splitlines()
    assert lines[0].startswith("Microsoft Windows 11 Pro") and "memory 75% used of 16 GB" in lines[1]
    assert lines[2] == "Drive C: 20.5 GB free of 512.0 GB (4% free)" and lines[3] == "Busiest programs: chrome (900 MB), Teams (600 MB)"
    assert "at 12% load" in lines[1]
    result["cpu"]["load_percent"] = None
    assert "load not reported" in summarize(result).splitlines()[1] and "None" not in summarize(result)


@pytest.mark.claude
@pytest.mark.slow
@pytest.mark.skipif(platform.system() != "Windows", reason="needs a Windows computer running the agent")
def test_claude_checks_a_family_pc_and_explains_in_plain_words(world, harness):
    cfg, storage, server, port = world
    h = harness([build_module(storage, cfg, Ntfy("", ""))])

    async def scenario():
        async with h:
            reply = await h.ask("Sam says his office computer has been slow. Can you check it and tell me what's going on, in simple words?")
            return reply, h.tools_called(), h.calls("pc_check")

    reply, called, calls = asyncio.run(scenario())
    print(f"\n  -> {called}\n      reply: {reply}")
    assert "pc_check" in called and any("office" in (c.get("device") or "").lower() for c in calls)
    assert any(word in reply.lower() for word in ("memory", "disk", "drive", "cpu", "processor", "space", "running"))
    assert "pc_fix" not in called
