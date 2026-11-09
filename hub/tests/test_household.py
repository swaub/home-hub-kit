import subprocess
import sys

import pytest

from homehub import config, household
from homehub.config import load_config
from homehub.household import HouseholdError, describe_household, parse_household, sync_household
from homehub.storage import Storage

EXAMPLE = household.EXAMPLE_PATH.read_text(encoding="utf-8")


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / "homehub.db")
    yield s
    s.close()


@pytest.fixture
def cfg():
    return load_config(config.EXAMPLE_PATH)


def write(tmp_path, text, name="people.toml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_example_household_imports(cfg, store):
    summary = sync_household(cfg, store, household.EXAMPLE_PATH)
    assert summary["changed"] is True
    assert (summary["people"], summary["devices"], summary["accounts"], summary["grants"]) == (3, 2, 1, 0)
    assert summary["missing_people"] == [] and summary["missing_devices"] == []
    alex = store.person("alex")
    assert alex["role"] == "owner" and alex["units"] == "imperial" and alex["aliases"] == ["me", "the owner"]
    assert store.find_person("Samuel")["id"] == "sam" and store.find_person("Ry")["id"] == "riley"
    phone = store.device("alex_phone")
    assert phone["owner_id"] == "alex" and phone["kind"] == "phone" and phone["capabilities"] == ["adb", "kdeconnect"]
    assert store.find_device("my phone")["id"] == "alex_phone"
    assert store.find_device("the office computer")["owner_id"] == "sam"
    assert store.accounts("alex")[0]["kind"] == "google"


def test_unchanged_file_is_not_reimported_and_runtime_fields_survive_a_reimport(cfg, store, tmp_path):
    path = write(tmp_path, EXAMPLE)
    sync_household(cfg, store, path)
    store.set_device_runtime("alex_phone", kdeconnect_id="abc123def456", last_ip="192.168.1.42")
    store.upsert_device("tv", "tv", "Living room TV", source="voice")
    again = sync_household(cfg, store, path)
    assert again["changed"] is False and again["people"] == 0
    write(tmp_path, EXAMPLE.replace('name = "Android phone"', 'name = "Android phone (blue)"').replace('aliases = ["Samuel"]', 'aliases = ["Samuel", "pops"]'))
    third = sync_household(cfg, store, path)
    assert third["changed"] is True and third["devices"] == 2
    phone = store.device("alex_phone")
    assert phone["name"] == "Android phone (blue)"
    assert phone["kdeconnect_id"] == "abc123def456" and phone["last_ip"] == "192.168.1.42"
    assert store.person("sam")["aliases"] == ["pops", "Samuel"]
    assert store.device("tv")["source"] == "voice" and third["missing_devices"] == []


def test_force_reimports_an_unchanged_file(cfg, store, tmp_path):
    path = write(tmp_path, EXAMPLE)
    sync_household(cfg, store, path)
    assert sync_household(cfg, store, path, force=True)["people"] == 3


def test_people_removed_from_the_file_are_reported_but_kept(cfg, store, tmp_path):
    path = write(tmp_path, EXAMPLE)
    sync_household(cfg, store, path)
    store.remember("Riley likes tulips.", owner_id="riley", visibility="private")
    trimmed = EXAMPLE[:EXAMPLE.index("[people.riley]")] + EXAMPLE[EXAMPLE.index("[devices.alex_phone]"):]
    trimmed = trimmed[:trimmed.index("[devices.sam_office_pc]")] + trimmed[trimmed.index("[accounts.alex_google]"):]
    summary = sync_household(cfg, store, write(tmp_path, trimmed))
    assert summary["missing_people"] == ["riley"] and summary["missing_devices"] == ["sam_office_pc"]
    assert store.person("riley")["name"] == "Riley" and store.device("sam_office_pc") is not None
    assert len(store.memory_facts("riley")) == 1


def test_file_grants_and_file_set_mac_are_imported(cfg, store, tmp_path):
    text = EXAMPLE.replace('capabilities = ["wake_on_lan", "pc_agent"]', 'capabilities = ["wake_on_lan", "pc_agent"]\nmac = "3c:7c:3f:12:34:56"')
    text += '\n[[grants]]\ngrantee = "sam"\nscope = "device:alex_phone"\ngranted_by = "alex"\n'
    summary = sync_household(cfg, store, write(tmp_path, text))
    assert summary["grants"] == 1 and store.has_grant("sam", "device:alex_phone")
    pc = store.device("sam_office_pc")
    assert pc["mac"] == "3c:7c:3f:12:34:56"


@pytest.mark.parametrize("edit, message", [
    (('role = "owner"', 'role = "boss"'), r"people\.alex\.role must be one of owner, adult, child, guest"),
    (('role = "owner"', 'role = "adult"'), r'exactly one person must have role = "owner", found 0'),
    (('[people.sam]\nname = "Sam"\naliases = ["Samuel"]\nrole = "adult"', '[people.sam]\nname = "Sam"\naliases = ["Samuel"]\nrole = "owner"'), "found 2"),
    (('units = "imperial"', 'units = "imperial"\ndefault_room = "bedroom"'), r"unknown setting people\.alex\.default_room"),
    (('units = "imperial"', 'units = "furlongs"'), r"people\.alex\.units must be imperial or metric"),
    (('aliases = ["me", "the owner"]', 'aliases = "me"'), r"people\.alex\.aliases must be a list of text values"),
    (('name = "Alex"', 'name = "Alex"\nnickname = "B"'), r"unknown setting people\.alex\.nickname"),
    (('owner = "sam"', 'owner = "grandpa"'), r"devices\.sam_office_pc\.owner 'grandpa' is not a person"),
    (('kind = "pc"', 'kind = "toaster"'), r"devices\.sam_office_pc\.kind must be one of"),
    (('aliases = ["my phone", "Alex\'s phone"]', 'aliases = ["my phone", "the office computer"]'), r"device alias 'the office computer' is used by both alex_phone and sam_office_pc"),
    (('aliases = ["Samuel"]', 'aliases = ["me"]'), r"alias 'me' is used by both alex and sam"),
    (('aliases = ["Ry"]', 'aliases = ["Sam"]'), r"alias 'Sam' is used by both sam and riley"),
    (('person = "alex"', 'person = "nobody"'), r"accounts\.alex_google\.person 'nobody' is not a person"),
    (('[accounts.alex_google]', '[accounts.alex_google]\nnote = "x"'), r"unknown setting accounts\.alex_google\.note"),
])
def test_bad_household_files_are_rejected_with_the_location(cfg, store, tmp_path, edit, message):
    text = EXAMPLE.replace(*edit)
    assert text != EXAMPLE
    with pytest.raises(HouseholdError, match=message):
        sync_household(cfg, store, write(tmp_path, text))


def test_bad_grants_and_sections_are_rejected(cfg, store, tmp_path):
    with pytest.raises(HouseholdError, match=r"grants\[0\]\.grantee 'ghost' is not a person"):
        sync_household(cfg, store, write(tmp_path, EXAMPLE + '\n[[grants]]\ngrantee = "ghost"\nscope = "email"\ngranted_by = "alex"\n'))
    with pytest.raises(HouseholdError, match=r"grants\[0\]\.scope is required"):
        sync_household(cfg, store, write(tmp_path, EXAMPLE + '\n[[grants]]\ngrantee = "sam"\ngranted_by = "alex"\n'))
    with pytest.raises(HouseholdError, match="unknown section pets"):
        sync_household(cfg, store, write(tmp_path, EXAMPLE + '\n[pets.rex]\nname = "Rex"\n'))
    with pytest.raises(HouseholdError, match="at least one person"):
        parse_household({})
    with pytest.raises(HouseholdError, match="person id 'Big Sam'"):
        parse_household({"people": {"Big Sam": {"name": "Sam", "role": "owner"}}})
    with pytest.raises(HouseholdError, match="household file not found"):
        sync_household(cfg, store, tmp_path / "missing.toml")
    with pytest.raises(HouseholdError, match="broken.toml"):
        sync_household(cfg, store, write(tmp_path, "[people.alex\nname = 1", "broken.toml"))


def test_a_rejected_file_changes_nothing(cfg, store, tmp_path):
    sync_household(cfg, store, write(tmp_path, EXAMPLE))
    with pytest.raises(HouseholdError):
        sync_household(cfg, store, write(tmp_path, EXAMPLE.replace('role = "owner"', 'role = "boss"')))
    assert store.person("alex")["role"] == "owner" and len(store.people()) == 3


def test_describe_household_lists_everything(cfg, store):
    sync_household(cfg, store, household.EXAMPLE_PATH)
    text = describe_household(store)
    assert text.startswith("People:\n  alex             Alex                 owner | imperial | aka me, the owner")
    assert "  alex_phone       Android phone            phone | owner alex | adb, kdeconnect | aka Alex's phone, my phone" in text
    assert "  alex_google      google for alex via claude_ai_connector" in text
    assert text.endswith("Grants: none")
    store.grant("sam", "device:alex_phone", "alex")
    assert describe_household(store).endswith("  sam may use device:alex_phone (granted by alex)")


def test_people_path_prefers_explicit_then_env_then_default(tmp_path, monkeypatch):
    monkeypatch.setenv(household.ENV_VAR, str(tmp_path / "env.toml"))
    assert household.people_path(tmp_path / "x.toml") == tmp_path / "x.toml"
    assert household.people_path() == tmp_path / "env.toml"
    monkeypatch.delenv(household.ENV_VAR)
    assert household.people_path() in (household.DEFAULT_PATH, household.EXAMPLE_PATH)


def test_cli_people_import_and_list(tmp_path):
    cfg_text = config.EXAMPLE_PATH.read_text(encoding="utf-8").replace('data_dir = "~/.local/share/homehub"', f'data_dir = "{tmp_path / "data"}"')
    cfg_path = write(tmp_path, cfg_text, "config.toml")
    people = write(tmp_path, EXAMPLE)
    cmd = [sys.executable, "-m", "homehub", "--config", str(cfg_path), "people"]
    out = subprocess.run(cmd + ["import", "--file", str(people)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"Imported {people}: 3 people, 2 devices, 1 accounts, 0 grants."
    out = subprocess.run(cmd + ["list", "--file", str(people)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.startswith("People:\n  alex ")
    assert (tmp_path / "data" / "homehub.db").exists()
    bad = write(tmp_path, EXAMPLE.replace('role = "owner"', 'role = "boss"'), "bad.toml")
    out = subprocess.run(cmd + ["import", "--file", str(bad)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 2 and "people.alex.role must be one of" in out.stderr


def test_household_town_and_vocabulary_are_imported_and_optional(cfg, store, tmp_path):
    with_town = EXAMPLE.replace("[household]\n", '[household]\ntown = "Springfield, Illinois"\nvocabulary = ["Springfield", "Decatur", "Champaign"]\n')
    sync_household(cfg, store, write(tmp_path, with_town))
    facts = household.household_facts(store)
    assert facts["town"] == "Springfield, Illinois" and facts["vocabulary"][:2] == ["Springfield", "Decatur"]
    assert describe_household(store).startswith("Household: Springfield, Illinois; local names: Springfield, Decatur, Champaign\nPeople:")
    sync_household(cfg, store, household.EXAMPLE_PATH)
    assert household.household_facts(store) == {"town": "", "vocabulary": []}
    assert describe_household(store).startswith("People:")
    with pytest.raises(HouseholdError, match="unknown setting household.city"):
        parse_household({"household": {"city": "Springfield"}, "people": {}})
    with pytest.raises(HouseholdError, match="household.vocabulary must be a list of text values"):
        parse_household({"household": {"vocabulary": "Springfield"}, "people": {}})

