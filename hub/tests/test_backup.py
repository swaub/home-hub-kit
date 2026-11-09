import io
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from homehub import household
from homehub.backup import (DB_NAME, BackupError, backup_name, list_backups, make_backup, newest_backup_age, read_manifest, restore_backup,
                               verify_archive)
from homehub.config import REPO_ROOT, parse_config
from homehub.household import sync_household
from homehub.storage import Storage


@pytest.fixture(autouse=True)
def example_household(monkeypatch):
    monkeypatch.setenv("HOMEHUB_PEOPLE", str(household.EXAMPLE_PATH))


def make_world(tmp_path, name="a"):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / name / "data"), "state_dir": str(tmp_path / name / "state")},
                        "backup": {"dir": str(tmp_path / name / "backups"), "keep": 3}})
    cfg.data_dir().mkdir(parents=True)
    store = Storage(cfg.data_dir() / DB_NAME)
    sync_household(cfg, store, household.EXAMPLE_PATH)
    store.remember("The garage code is 4471.", owner_id="alex", visibility="private")
    store.add_reminder("alarm", "2026-10-24T06:30:00", "work", "weekdays", "alex", None)
    return cfg, store


def test_backup_archives_the_database_and_prunes(tmp_path):
    cfg, store = make_world(tmp_path)
    when = datetime(2026, 10, 23, 3, 0, 0)
    first = make_backup(cfg, now=when)
    assert first["path"].name == backup_name(when) == "homehub-20261023-030000.tar.gz" and first["bytes"] > 0
    assert first["files"][0] == DB_NAME and first["files"][-1] == "manifest.json"
    with tarfile.open(first["path"]) as tar:
        assert sorted(tar.getnames()) == sorted(first["files"])
    manifest = read_manifest(first["path"])
    assert manifest["database"] == DB_NAME and len(manifest["database_sha256"]) == 64 and manifest["created"] == "2026-10-23T03:00:00"
    assert verify_archive(first["path"]) == manifest
    for day in range(1, 5):
        make_backup(cfg, now=when + timedelta(days=day))
    names = [p.name for p in list_backups(cfg.backup.dir)]
    assert names == ["homehub-20261027-030000.tar.gz", "homehub-20261026-030000.tar.gz", "homehub-20261025-030000.tar.gz"]
    newest, age = newest_backup_age(cfg.backup.dir)
    assert newest.name == names[0] and 0 <= age < 60
    assert newest_backup_age(tmp_path / "nowhere") == (None, None)
    store.close()


def test_backup_copies_to_a_second_location_and_restores_elsewhere(tmp_path):
    cfg, store = make_world(tmp_path)
    second = tmp_path / "usb-drive" / "homehub"
    (cfg.data_dir() / "secrets.toml").write_text("[weather]\n", encoding="utf-8")
    result = make_backup(cfg, copy_to=str(second))
    assert Path(result["copied_to"]) == second / result["path"].name and (second / result["path"].name).stat().st_size == result["bytes"]
    store.close()
    other, other_store = make_world(tmp_path, "b")
    other_store.remember("Something only the other machine knew.")
    other_store.close()
    restored = restore_backup(other, result["path"], now=datetime(2026, 10, 24, 9, 0, 0))
    assert restored["previous"].name == "homehub.db.before-restore-20261024-090000" and restored["previous"].exists()
    assert restored["files_for_you"] == ["config/secrets.toml"] and (restored["files_dir"] / "config" / "secrets.toml").is_file()
    back = Storage(other.data_dir() / DB_NAME)
    try:
        assert [f["text"] for f in back.memory_facts("alex")] == ["The garage code is 4471."]
        assert [r["label"] for r in back.reminders()] == ["work"]
        assert {p["id"] for p in back.people()} == {"alex", "sam", "riley"}
    finally:
        back.close()
    before = Storage(restored["previous"])
    try:
        assert any("other machine" in f["text"] for f in before.memory_facts())
    finally:
        before.close()


def test_damaged_archives_are_refused(tmp_path):
    cfg, store = make_world(tmp_path)
    result = make_backup(cfg)
    store.close()
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"not an archive")
    with pytest.raises(BackupError, match="not a readable backup"):
        verify_archive(bad)
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(result["path"]) as src, tarfile.open(tampered, "w:gz") as dst:
        for member in src.getmembers():
            data = src.extractfile(member)
            if member.name == DB_NAME:
                original = data.read()
                payload = original[:-16] + bytes(b ^ 0xFF for b in original[-16:])
                member.size = len(payload)
                dst.addfile(member, io.BytesIO(payload))
            else:
                dst.addfile(member, data)
    with pytest.raises(BackupError, match="does not match its checksum"):
        verify_archive(tampered)
    with pytest.raises(BackupError, match="no database at"):
        make_backup(parse_config({"paths": {"data_dir": str(tmp_path / "empty"), "state_dir": str(tmp_path / "s")}}))


def test_backup_command_lists_backs_up_and_asks_before_restoring(tmp_path):
    cfg, store = make_world(tmp_path)
    store.close()
    text = ((REPO_ROOT / "config" / "config.example.toml").read_text(encoding="utf-8")
            .replace('data_dir = "~/.local/share/homehub"', f'data_dir = "{cfg.data_dir()}"')
            .replace('state_dir = "~/.local/state/homehub"', f'state_dir = "{cfg.state_dir()}"')
            .replace('dir = "~/.local/share/homehub/backups"', f'dir = "{cfg.backup.dir}"'))
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(text, encoding="utf-8")

    def run(*args, **kw):
        return subprocess.run([sys.executable, "-m", "homehub", "--config", str(cfg_path), "backup", *args], capture_output=True, text=True, timeout=120, **kw)

    out = run()
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("backed up to ") and "homehub.db, " in out.stdout and "manifest.json" in out.stdout
    listed = run("--list")
    assert listed.returncode == 0 and listed.stdout.count("homehub-") == 1
    archive = list_backups(cfg.backup.dir)[0]
    declined = run("--restore", str(archive), input="n\n")
    assert declined.returncode == 1 and declined.stdout.strip().endswith("not restored")
    restored = run("--restore", str(archive), "--yes")
    assert restored.returncode == 0, restored.stderr
    assert "restored " in restored.stdout and "before-restore" in restored.stdout
    missing = run("--restore", str(tmp_path / "nope.tar.gz"))
    assert missing.returncode == 1 and "not a readable backup" in missing.stderr
