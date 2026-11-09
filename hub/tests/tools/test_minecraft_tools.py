import asyncio
import json
import shutil
import subprocess

import pytest

from homehub.minecraft import Catalog, Minecraft
from homehub.tools import CallContext, current_context, result_texts
from homehub.tools.minecraft import build_module

HAS_DOCKER = shutil.which("docker") is not None and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def add_server(root, slug, port, name=None):
    folder = root / slug
    (folder / "data" / "mods").mkdir(parents=True)
    meta = {"name": name or slug, "slug": slug, "loader": "fabric", "version": "1.21.1", "memory_gb": 2, "disk_gb": 5, "max_players": 5,
            "motd": slug, "difficulty": "normal", "mode": "survival", "mods": [], "port": port, "public": False, "players": [],
            "created": "2026-11-04T08:00:00", "owner": "alex", "note": ""}
    (folder / "server.json").write_text(json.dumps(meta), encoding="utf-8")
    (folder / "data" / "server.properties").write_text(f"max-players=5\nmotd={slug}\nview-distance=10\n", encoding="utf-8")


def call(module, name, args):
    handler = next(spec.fn.handler for spec in module.tools if spec.name == name)
    token = current_context.set(CallContext(room="remote", person_id="alex", person_name="Alex"))
    try:
        return result_texts(asyncio.run(handler(args)))[0]
    finally:
        current_context.reset(token)


def test_reading_is_automatic_and_every_change_asks_first(tmp_path):
    module = build_module(Minecraft(tmp_path, catalog=Catalog()))
    policies = {spec.name: spec.policy for spec in module.tools}
    assert {n for n, p in policies.items() if p == "auto"} == {"minecraft_servers", "minecraft_server", "minecraft_find_mods", "minecraft_files"}
    assert {n for n, p in policies.items() if p == "confirm"} == {"minecraft_control", "minecraft_settings", "minecraft_whitelist", "minecraft_command",
                                                                  "minecraft_public", "minecraft_mods", "minecraft_write_file", "minecraft_create"}
    assert all(spec.untrusted_output for spec in module.tools if spec.policy == "auto")
    describe = {spec.name: spec.describe for spec in module.tools}
    assert describe["minecraft_settings"]({"max_players": 10, "port": 25570}) == "change the Minecraft server: max players to 10, port to 25570"
    assert describe["minecraft_control"]({"server": "SMP", "action": "restart"}) == "restart SMP"
    assert describe["minecraft_whitelist"]({"player": "Weighta", "add": False}) == "remove Weighta from the whitelist on the Minecraft server"
    assert describe["minecraft_mods"]({"add": ["lithium"], "remove": ["spark"]}) == "change mods on the Minecraft server: add lithium; remove spark"


@pytest.mark.skipif(not HAS_DOCKER, reason="needs Docker")
def test_the_chat_reads_and_changes_a_stopped_server_like_the_app_does(tmp_path):
    add_server(tmp_path, "pytest-smp", 25601, "Pytest SMP")
    mc = Minecraft(tmp_path, catalog=Catalog(), vpn_address="10.77.0.1")
    module = build_module(mc)
    assert call(module, "minecraft_servers", {}) == "Pytest SMP (fabric 1.21.1): stopped; VPN only at 10.77.0.1:25601"
    detail = call(module, "minecraft_server", {})
    assert 'up to 5 players, normal, survival, message "pytest-smp", 2 GB memory, 5 GB disk limit, port 25601, whitelist on' in detail
    assert call(module, "minecraft_settings", {"max_players": 8, "difficulty": "hard"}) == "Changed Pytest SMP: max players 8, difficulty hard."
    assert mc.meta("pytest-smp")["max_players"] == 8 and "max-players=8" in mc.files("pytest-smp", "server.properties")["text"]
    assert "pytest-smp" not in mc.containers()
    add_server(tmp_path, "pytest-other", 25602, "Pytest Other")
    assert call(module, "minecraft_server", {}).startswith("Which server? There are ")
    assert call(module, "minecraft_server", {"server": "nether"}).startswith("No server called nether")
    assert call(module, "minecraft_settings", {"server": "smp", "port": 25602}) == "Pytest Other already uses port 25602"
    assert call(module, "minecraft_settings", {"server": "smp", "port": 25611}) == "Changed Pytest SMP: port 25611."
    assert "VPN only at 10.77.0.1:25611" in call(module, "minecraft_servers", {})
    assert call(module, "minecraft_settings", {"server": "smp", "port": 80}) == "The port must be between 1024 and 65535"
    listing = call(module, "minecraft_files", {"server": "Pytest SMP"})
    assert listing.startswith("the server folder:\nmods/\n") and "server.properties (" in listing
    text = call(module, "minecraft_files", {"server": "Pytest SMP", "path": "server.properties"})
    assert text.startswith("server.properties:\n") and "view-distance=10" in text
    edited = text.removeprefix("server.properties:\n").replace("view-distance=10", "view-distance=6").replace("max-players=8", "max-players=12")
    assert call(module, "minecraft_write_file", {"server": "Pytest SMP", "path": "server.properties", "text": edited}) == "Saved server.properties on Pytest SMP."
    assert mc.meta("pytest-smp")["max_players"] == 12
    assert call(module, "minecraft_mods", {"server": "smp", "add": ["lithium"]}) == "Pytest SMP: added lithium. They install on its next start."
    assert mc.meta("pytest-smp")["mods"] == ["lithium"]
    assert call(module, "minecraft_mods", {"server": "smp", "remove": ["lithium"]}) == "Pytest SMP: removed 1. They install on its next start."
    assert mc.meta("pytest-smp")["mods"] == []
    assert "lithium" in call(module, "minecraft_find_mods", {"server": "smp", "query": "lithium"})


def test_with_no_servers_the_chat_says_where_to_make_one(tmp_path):
    if not HAS_DOCKER:
        pytest.skip("needs Docker")
    module = build_module(Minecraft(tmp_path, catalog=Catalog()))
    assert call(module, "minecraft_servers", {}) == "There are no Minecraft servers on the hub yet."
    assert call(module, "minecraft_control", {"action": "start"}) == "There are no Minecraft servers on the hub yet; make one on the Minecraft page"
