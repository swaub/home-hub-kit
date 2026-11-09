import io
import json
import shutil
import socket
import subprocess
import time
from datetime import datetime

import pytest

from homehub.minecraft import Catalog, McError, Minecraft, java_tag, neoforge_game_version, size_bytes, slug_for

HAS_DOCKER = shutil.which("docker") is not None and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def catalog():
    return Catalog()


def make_server(root, slug="test-world", loader="fabric"):
    folder = root / slug
    (folder / "data" / "mods").mkdir(parents=True)
    meta = {"name": slug, "slug": slug, "loader": loader, "version": "1.21.1", "memory_gb": 2, "disk_gb": 5, "max_players": 5, "motd": slug,
            "difficulty": "normal", "mode": "survival", "mods": [], "port": 25565, "public": False, "players": [], "created": "2026-11-02T08:00:00",
            "owner": "alex", "note": ""}
    (folder / "server.json").write_text(json.dumps(meta), encoding="utf-8")
    return Minecraft(root, catalog=Catalog())


def test_names_versions_and_java_choices():
    assert slug_for("Alex's Survival World!") == "alex-s-survival-world"
    assert slug_for("!!!") is None
    assert neoforge_game_version("21.1.77") == "1.21.1"
    assert neoforge_game_version("21.0.167") == "1.21"
    assert neoforge_game_version("20.4.237") == "1.20.4"
    assert neoforge_game_version("26.3.0.101") == "26.3"
    assert neoforge_game_version("26.1.2.5") == "26.1.2"
    assert neoforge_game_version("21.2.0-beta") is None
    assert [java_tag(n) for n in (8, 9, 17, 18, 21, 25, 30)] == [8, 11, 17, 21, 21, 25, 25]
    assert size_bytes("2.516GiB") == int(2.516 * 1024 ** 3) and size_bytes(" 512MiB ") == 512 * 1024 ** 2 and size_bytes("lots") is None


@pytest.mark.live
def test_every_loader_lists_real_minecraft_releases_newest_first(catalog):
    releases = catalog.releases()
    assert releases and "1.21.1" in releases
    for loader in ("vanilla", "paper", "fabric", "forge", "neoforge"):
        versions = catalog.loader_versions(loader)
        assert versions and set(versions) <= set(releases), loader
        assert versions == [v for v in releases if v in versions], loader
    assert "1.21.1" in catalog.loader_versions("neoforge") and "1.20.1" in catalog.loader_versions("forge")
    assert catalog.java_for("1.21.1") == 21 and catalog.java_for("1.16.5") == 8
    with pytest.raises(McError):
        catalog.loader_versions("bukkit")


@pytest.mark.live
def test_mod_search_only_offers_mods_that_run_on_a_server_for_that_loader_and_version(catalog):
    found = catalog.mods("fabric", "1.21.1", "lithium")
    assert found["total"] > 0 and any(h["slug"] == "lithium" for h in found["hits"])
    assert all(h["server"] in ("required", "optional") for h in found["hits"])
    popular = catalog.mods("paper", "1.21.1")
    assert popular["hits"] and popular["hits"][0]["downloads"] >= popular["hits"][-1]["downloads"]
    assert catalog.mods("vanilla", "1.21.1") == {"total": 0, "hits": []}


@pytest.mark.live
def test_mods_with_only_test_builds_for_a_version_are_pinned_and_released_ones_are_not(catalog):
    assert catalog.pin("lithium", "fabric", "1.21.1") == "lithium"
    pinned = catalog.pin("c2me-fabric", "fabric", "1.21.1")
    assert pinned.startswith("c2me-fabric:") and len(pinned.split(":")[1]) == 8
    with pytest.raises(McError):
        catalog.pin("lithium", "fabric", "1.7.10")


@pytest.mark.live
def test_creating_checks_the_name_type_version_and_eula_before_anything_runs(tmp_path, catalog):
    mc = Minecraft(tmp_path, catalog=catalog)
    good = {"name": "Survival", "loader": "fabric", "version": "1.21.1", "eula": True}
    for bad in ({**good, "name": "!!"}, {**good, "loader": "bukkit"}, {**good, "version": "1.99"}, {**good, "eula": False},
                {**good, "memory_gb": 64}, {**good, "max_players": 0}, {**good, "mods": ["ok", "../evil"]}, {**good, "players": ["a b"]},
                {**good, "difficulty": "nightmare"}):
        with pytest.raises(McError):
            mc.validate(bad, creating=True)
    with pytest.raises(McError):
        mc.validate({**good, "whitelist": "off"}, creating=True)
    with pytest.raises(McError):
        mc.validate({**good, "seed": "has spaces"}, creating=True)
    assert mc.validate({**good, "seed": "-4172144997902289642"}, creating=True)["seed"] == "-4172144997902289642"
    assert mc.validate({"whitelist": False}, creating=False) == {"whitelist": False}
    clean = mc.validate({**good, "memory_gb": 4, "mods": ["lithium", "lithium"], "players": ["Steve_1"]}, creating=True)
    assert clean["slug"] == "survival" and clean["mods"] == ["lithium"] and clean["players"] == ["Steve_1"]
    assert list(tmp_path.iterdir()) == []


def test_the_file_browser_stays_inside_the_servers_folder(tmp_path):
    mc = make_server(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("keep out", encoding="utf-8")
    (tmp_path / "test-world" / "data" / "link").symlink_to(outside)
    for path in ("../server.json", "../../secret.txt", "/etc/passwd", "link", "mods/../../server.json"):
        with pytest.raises(McError):
            mc.files("test-world", path)
    with pytest.raises(McError):
        mc.files("../test-world", "")
    mc.write_text("test-world", "server.properties", "max-players=5\n")
    listing = mc.files("test-world", "")
    assert [e["name"] for e in listing["entries"]][:1] == ["mods"] and any(e["name"] == "server.properties" for e in listing["entries"])
    assert mc.files("test-world", "server.properties")["text"] == "max-players=5\n"
    saved = mc.upload("test-world", "mods", "../../sneaky.jar", io.BytesIO(b"PK\x03\x04\x00\x00jar"), 9)
    assert saved == "sneaky.jar" and (tmp_path / "test-world" / "data" / "mods" / "sneaky.jar").read_bytes() == b"PK\x03\x04\x00\x00jar"
    assert mc.mod_files("test-world") == {"folder": "mods", "files": ["sneaky.jar"]}
    with pytest.raises(McError):
        mc.upload("test-world", "mods", "cut.jar", io.BytesIO(b"abc"), 10)
    assert not any(p.name.endswith(".uploading") for p in (tmp_path / "test-world" / "data" / "mods").iterdir())
    mc.make_dir("test-world", "config/sub")
    mc.delete_path("test-world", "config")
    with pytest.raises(McError):
        mc.delete_path("test-world", "")
    assert outside.read_text(encoding="utf-8") == "keep out"
    assert mc.files("test-world", "mods/sneaky.jar")["text"] is None


def test_disk_use_is_measured_fresh_for_a_small_server_and_kept_a_minute_only_for_a_big_one(tmp_path):
    mc = make_server(tmp_path)
    data = tmp_path / "test-world" / "data"
    empty = mc.size("test-world")
    (data / "level.dat").write_bytes(b"x" * 5000)
    assert mc.size("test-world") == empty + 5000
    (data / "region").mkdir()
    for i in range(2001):
        (data / "region" / f"r.{i}.mca").write_bytes(b"y")
    big = mc.size("test-world")
    assert big == empty + 5000 + 2001
    (data / "late.dat").write_bytes(b"z" * 300)
    assert mc.size("test-world") == big and mc.size("test-world", fresh=True) == big + 300


@pytest.mark.slow
@pytest.mark.live
@pytest.mark.skipif(not HAS_DOCKER, reason="needs Docker")
def test_a_real_paper_server_installs_starts_answers_commands_and_moves_to_the_trash_on_delete(tmp_path, catalog):
    mc = Minecraft(tmp_path, catalog=catalog, vpn_address="127.0.0.1", cpus=2)
    version = catalog.loader_versions("paper")[0]
    created = mc.create({"name": "Pytest World", "loader": "paper", "version": version, "eula": True, "memory_gb": 1, "max_players": 3,
                         "players": ["Notch"]}, "alex")
    slug = created["slug"]
    try:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline and mc.detail(slug)["state"] != "online":
            time.sleep(5)
        info = mc.detail(slug)
        assert info["state"] == "online", mc.log(slug)[-2000:]
        assert info["players_online"] == 0 and info["max_players"] == 3 and info["disk_used"] > 0
        assert "whitelist" in mc.command(slug, "whitelist list").lower()
        assert mc.command(slug, "hello from the test", chat_as="Alex") == "Sent, but nobody is online to see it."
        log = mc.log(slug)
        assert "For help" in log and "RCON Client" not in log
        assert info["memory_used"] > 100_000_000
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not 0 < (info["mspt"] or 0) < 50:
            time.sleep(1)
            info = mc.detail(slug)
        assert 0 < info["mspt"] < 50 and info["tps"] == 20.0
        properties = mc.files(slug, "server.properties")["text"]
        assert "white-list=true" in properties and "max-players=3" in properties and "online-mode=true" in properties
        added = mc.change_whitelist(slug, "jeb_", True, "alex")
        assert "jeb_" in added["whitelist"] and "Notch" in added["whitelist"]
        assert mc.change_whitelist(slug, "jeb_", False, "alex")["whitelist"] == ["Notch"]
        assert mc.detail(slug)["operators"] == ["Notch"]
        mc.update(slug, {"max_players": 4}, "alex")
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline and mc.detail(slug)["state"] != "online":
            time.sleep(5)
        assert "max-players=4" in mc.files(slug, "server.properties")["text"]
        with pytest.raises(McError):
            mc.action(slug, "delete", "alex", confirm="wrong name")
    finally:
        mc.action(slug, "delete", "alex", confirm="Pytest World")
    assert not (tmp_path / slug).exists() and any(p.name.startswith(slug) for p in (tmp_path / ".trash").iterdir())
    assert slug not in mc.containers()


def test_a_new_server_never_takes_a_port_another_server_or_program_is_using(tmp_path):
    mc = make_server(tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        try:
            holder.bind(("0.0.0.0", 25566))
        except OSError:
            pass
        holder.listen()
        port = mc.free_port()
        assert port not in (25565, 25566)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("0.0.0.0", port))
        assert mc.free_port(keep=mc.meta("test-world")) != 25566


@pytest.mark.live
@pytest.mark.skipif(not HAS_DOCKER, reason="needs Docker to see the server is stopped")
def test_the_whitelist_can_be_changed_while_the_server_is_off_using_real_minecraft_accounts(tmp_path):
    mc = make_server(tmp_path)
    assert mc.change_whitelist("test-world", "notch", True, "alex") == {"reply": "Added Notch to the whitelist", "whitelist": ["Notch"]}
    mc.change_whitelist("test-world", "Android14", True, "alex")
    mc.change_whitelist("test-world", "Notch", True, "alex")
    saved = json.loads((tmp_path / "test-world" / "data" / "whitelist.json").read_text())
    assert [p["name"] for p in saved] == ["Notch", "Android14"] and saved[0]["uuid"] == "069a79f4-44e9-4726-a5be-fca90e38aaf5"
    with pytest.raises(McError, match="no Minecraft account"):
        mc.change_whitelist("test-world", "zq9xnotarealname", True, "alex")
    with pytest.raises(McError):
        mc.change_whitelist("test-world", "bad name!", True, "alex")
    assert mc.change_whitelist("test-world", "NOTCH", False, "alex")["whitelist"] == ["Android14"]
    assert mc.whitelist("test-world") == ["Android14"]


def test_a_custom_address_must_be_a_domain(tmp_path):
    mc = make_server(tmp_path)
    for bad in ("not a domain", "http://x.com", "localhost", "-bad-.com"):
        with pytest.raises(McError):
            mc.validate({"host": bad}, creating=False)
    assert mc.validate({"host": "Play.Example.COM."}, creating=False) == {"host": "play.example.com"}
    assert mc.validate({"host": ""}, creating=False) == {"host": ""}
    with pytest.raises(McError, match="DuckDNS isn't set up"):
        mc.claim_duckdns("somebody.duckdns.org")


@pytest.mark.live
def test_a_custom_addresss_dns_is_checked_against_home(tmp_path):
    from homehub.minecraft import resolve

    mc = make_server(tmp_path)
    assert resolve("dns.google") and resolve("no-such-name.invalid") == []
    assert mc.check_host("no-such-name.invalid")["ok"] is False


def test_account_checks_can_only_be_off_while_the_server_is_closed_to_friends(tmp_path):
    mc = make_server(tmp_path)
    meta = mc.meta("test-world")
    meta["public"] = True
    mc.save(meta)
    with pytest.raises(McError, match="Close it to friends"):
        mc.update("test-world", {"online_mode": False}, "alex")
    meta["public"], meta["online_mode"] = False, False
    mc.save(meta)
    mc.forwards = object()
    with pytest.raises(McError, match="online mode"):
        mc.set_public("test-world", True, "alex")
    with pytest.raises(McError):
        mc.validate({"online_mode": "no"}, creating=False)


def test_editing_server_properties_by_hand_carries_into_the_settings_so_a_restart_keeps_it(tmp_path):
    mc = make_server(tmp_path)
    original = "#Minecraft server properties\nmax-players=5\nmotd=test-world\ndifficulty=normal\ngamemode=survival\nview-distance=10\n"
    (tmp_path / "test-world" / "data" / "server.properties").write_text(original, encoding="utf-8")
    edited = original.replace("max-players=5", "max-players=12").replace("difficulty=normal", "difficulty=hard").replace("view-distance=10", "view-distance=8")
    mc.write_text("test-world", "server.properties", edited)
    meta = mc.meta("test-world")
    assert meta["max_players"] == 12 and meta["difficulty"] == "hard" and meta["mode"] == "survival"
    env = mc.env(meta, first=False)
    assert env["MAX_PLAYERS"] == "12" and env["DIFFICULTY"] == "hard"
    assert mc.files("test-world", "server.properties")["text"] == edited
    for bad in ("max-players=lots", "difficulty=nightmare", "white-list=maybe", "max-players=0", "server-port=25570"):
        with pytest.raises(McError):
            mc.write_text("test-world", "server.properties", edited + bad + "\n")
    assert mc.files("test-world", "server.properties")["text"] == edited and mc.meta("test-world")["max_players"] == 12
    meta["public"] = True
    mc.save(meta)
    with pytest.raises(McError, match="Close it to friends"):
        mc.write_text("test-world", "server.properties", edited + "online-mode=false\n")
    assert mc.meta("test-world").get("online_mode", True)
    mc.write_text("test-world", "notes.txt", "max-players=99\n")
    assert mc.meta("test-world")["max_players"] == 12


def test_settings_changed_from_the_menu_show_in_server_properties_right_away(tmp_path):
    mc = make_server(tmp_path)
    meta = mc.meta("test-world")
    mc.write_properties(meta)
    assert not (tmp_path / "test-world" / "data" / "server.properties").exists()
    (tmp_path / "test-world" / "data" / "server.properties").write_text("#kept\nmax-players=5\nmotd=old\nview-distance=10\n", encoding="utf-8")
    mc.write_properties({**meta, "max_players": 9, "motd": "Friends SMP", "whitelist": False, "mode": "creative"})
    text = mc.files("test-world", "server.properties")["text"]
    found = text.splitlines()
    assert found[:4] == ["#kept", "max-players=9", "motd=Friends SMP", "view-distance=10"]
    assert {"difficulty=normal", "gamemode=creative", "white-list=false", "enforce-whitelist=false", "online-mode=true"} <= set(found)


@pytest.mark.skipif(not HAS_DOCKER, reason="needs Docker")
def test_a_stopped_servers_settings_reach_its_files_without_starting_it(tmp_path):
    mc = make_server(tmp_path, slug="pytest-stopped")
    (tmp_path / "pytest-stopped" / "data" / "server.properties").write_text("max-players=5\nmotd=pytest-stopped\n", encoding="utf-8")
    mc.update("pytest-stopped", {"max_players": 7, "motd": "Changed while off"}, "alex")
    text = mc.files("pytest-stopped", "server.properties")["text"]
    assert "max-players=7" in text and "motd=Changed while off" in text
    assert "pytest-stopped" not in mc.containers()


def test_a_server_starts_small_grows_to_its_limit_and_hands_memory_back_when_quiet(tmp_path):
    mc = make_server(tmp_path)
    meta = {**mc.meta("test-world"), "memory_gb": 4}
    env = mc.env(meta, first=False)
    assert env["INIT_MEMORY"] == "512M" and env["MAX_MEMORY"] == "4G"
    assert "MEMORY" not in env and "USE_AIKAR_FLAGS" not in env
    flags = env["JVM_XX_OPTS"].split()
    assert "-XX:G1PeriodicGCInterval=60000" in flags and "-XX:MaxHeapFreeRatio=30" in flags
    assert not any("AlwaysPreTouch" in flag for flag in flags)


def test_deleted_worlds_stay_in_the_trash_for_a_week_then_go_for_good(tmp_path):
    mc = make_server(tmp_path)
    trash = tmp_path / ".trash"
    for name in ("old-smp-20261101-090000", "recent-smp-20261108-090000", "notes"):
        (trash / name / "data").mkdir(parents=True)
        (trash / name / "data" / "level.dat").write_bytes(b"world")
    (trash / "stray-20261101-090000.txt").write_text("not a world", encoding="utf-8")
    removed = mc.empty_trash(now=datetime(2026, 11, 9, 12, 0))
    assert removed == ["old-smp-20261101-090000"]
    assert sorted(p.name for p in trash.iterdir()) == ["notes", "recent-smp-20261108-090000", "stray-20261101-090000.txt"]
    assert (tmp_path / "test-world" / "server.json").is_file()
    assert mc.empty_trash(now=datetime(2026, 11, 15, 9, 0)) == ["recent-smp-20261108-090000"]
