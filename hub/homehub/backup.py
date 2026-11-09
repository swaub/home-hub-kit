import hashlib
import json
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path

from homehub import household
from homehub.version import describe

PREFIX = "homehub-"
SUFFIX = ".tar.gz"
DB_NAME = "homehub.db"


class BackupError(Exception):
    pass


def sha256(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def backup_name(now=None):
    return PREFIX + (now or datetime.now()).strftime("%Y%m%d-%H%M%S") + SUFFIX


def extra_files(cfg):
    files = {}
    if cfg.source is not None and Path(cfg.source).name != "config.example.toml" and Path(cfg.source).is_file():
        files["config/" + Path(cfg.source).name] = Path(cfg.source)
    people = household.people_path()
    if people is not None and Path(people).is_file() and Path(people).name != "people.example.toml":
        files["config/" + Path(people).name] = Path(people)
    secrets = cfg.data_dir() / "secrets.toml"
    if secrets.is_file():
        files["config/secrets.toml"] = secrets
    return files


def snapshot_database(db_path, target):
    source = sqlite3.connect(str(db_path))
    copy = sqlite3.connect(str(target))
    try:
        source.backup(copy)
        verdict = copy.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        copy.close()
        source.close()
    if verdict != "ok":
        raise BackupError(f"the database failed its integrity check: {verdict}")
    return target


def list_backups(directory):
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        return []
    return sorted((p for p in directory.iterdir() if p.name.startswith(PREFIX) and p.name.endswith(SUFFIX)), key=lambda p: p.name, reverse=True)


def prune(directory, keep):
    removed = []
    for old in list_backups(directory)[max(int(keep), 1):]:
        old.unlink()
        removed.append(old)
    return removed


def copy_elsewhere(archive, target):
    target = str(target).strip()
    if not target:
        return None
    if ":" in target and not target.startswith("/"):
        result = subprocess.run(["rsync", "-a", str(archive), target], capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise BackupError(f"rsync to {target} failed: {(result.stderr or result.stdout).strip()}")
        return target
    folder = Path(target).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    return str(shutil.copy2(archive, folder / archive.name))


def make_backup(cfg, dest=None, keep=None, copy_to=None, now=None):
    started = time.perf_counter()
    db_path = cfg.data_dir() / DB_NAME
    if not db_path.is_file():
        raise BackupError(f"no database at {db_path}; run the assistant once first")
    directory = Path(dest or cfg.backup.dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / backup_name(now)
    extras = extra_files(cfg)
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = snapshot_database(db_path, Path(tmp) / DB_NAME)
        manifest = {"created": (now or datetime.now()).isoformat(timespec="seconds"), "version": describe(), "database": DB_NAME,
                    "database_sha256": sha256(snapshot), "database_bytes": snapshot.stat().st_size, "files": sorted(extras)}
        manifest_path = Path(tmp) / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(snapshot, arcname=DB_NAME)
            for name, path in extras.items():
                tar.add(path, arcname=name)
            tar.add(manifest_path, arcname="manifest.json")
    removed = prune(directory, cfg.backup.keep if keep is None else keep)
    copied = copy_elsewhere(archive, cfg.backup.copy_to if copy_to is None else copy_to)
    return {"path": archive, "bytes": archive.stat().st_size, "files": [DB_NAME, *sorted(extras), "manifest.json"], "removed": removed,
            "copied_to": copied, "seconds": round(time.perf_counter() - started, 2), "manifest": manifest}


def read_manifest(archive):
    try:
        with tarfile.open(archive, "r:gz") as tar:
            member = tar.extractfile("manifest.json")
            if member is None:
                raise BackupError(f"{archive} has no manifest")
            return json.loads(member.read().decode("utf-8"))
    except (tarfile.TarError, OSError, ValueError, KeyError) as e:
        raise BackupError(f"{archive} is not a readable backup: {e}")


def verify_archive(archive):
    archive = Path(archive)
    manifest = read_manifest(archive)
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(archive, "r:gz") as tar:
            try:
                tar.extract(DB_NAME, tmp, filter="data")
            except KeyError:
                raise BackupError(f"{archive} has no database")
        extracted = Path(tmp) / DB_NAME
        if sha256(extracted) != manifest.get("database_sha256"):
            raise BackupError(f"{archive}: the database does not match its checksum")
        conn = sqlite3.connect(str(extracted))
        try:
            verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    if verdict != "ok" or "people" not in tables:
        raise BackupError(f"{archive}: the database inside is damaged ({verdict})")
    return manifest


def restore_backup(cfg, archive, now=None):
    archive = Path(archive).expanduser()
    manifest = verify_archive(archive)
    data_dir = cfg.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    db_path = data_dir / DB_NAME
    aside = None
    if db_path.exists():
        aside = data_dir / f"{DB_NAME}.before-restore-{stamp}"
        shutil.copy2(db_path, aside)
        for extra in (db_path.with_name(DB_NAME + "-wal"), db_path.with_name(DB_NAME + "-shm")):
            if extra.exists():
                extra.unlink()
    extracted_dir = data_dir / f"restored-{stamp}"
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(extracted_dir, filter="data")
    shutil.copy2(extracted_dir / DB_NAME, db_path)
    (extracted_dir / DB_NAME).unlink()
    (extracted_dir / "manifest.json").unlink()
    leftovers = sorted(str(p.relative_to(extracted_dir)) for p in extracted_dir.rglob("*") if p.is_file())
    if not leftovers:
        shutil.rmtree(extracted_dir)
    return {"database": db_path, "previous": aside, "manifest": manifest, "files_for_you": leftovers,
            "files_dir": extracted_dir if leftovers else None}


def newest_backup_age(directory):
    backups = list_backups(directory)
    if not backups:
        return None, None
    newest = backups[0]
    return newest, time.time() - newest.stat().st_mtime
