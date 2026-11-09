import hashlib
import os
import re
import tomllib
from pathlib import Path

from homehub.config import CONFIG_DIR, ConfigError
from homehub.storage import DEVICE_KINDS, ROLES, RUNTIME_DEVICE_FIELDS

EXAMPLE_PATH = CONFIG_DIR / "people.example.toml"
DEFAULT_PATH = CONFIG_DIR / "people.toml"
ENV_VAR = "HOMEHUB_PEOPLE"
ID = re.compile(r"^[a-z][a-z0-9_]*$")
DIGEST_KEY = "household:digest"
UNITS = ("imperial", "metric")
PERSON_KEYS = {"name": str, "role": str, "aliases": list, "units": str}
DEVICE_KEYS = {"owner": str, "kind": str, "name": str, "aliases": list, "capabilities": list,
               "mac": str, "hostname": str, "tailscale_name": str}
ACCOUNT_KEYS = {"person": str, "kind": str, "via": str}
GRANT_KEYS = {"grantee": str, "scope": str, "granted_by": str}
SECTIONS = ("household", "people", "devices", "accounts", "grants")
HOUSEHOLD_KEY = "household"
HOUSEHOLD_KEYS = {"town": str, "vocabulary": list}


class HouseholdError(ConfigError):
    pass


def people_path(explicit=None):
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env).expanduser()
    return DEFAULT_PATH if DEFAULT_PATH.exists() else EXAMPLE_PATH


def _typed(table, keys, where, required=()):
    if not isinstance(table, dict):
        raise HouseholdError(f"{where} must be a table")
    unknown = sorted(set(table) - set(keys))
    if unknown:
        raise HouseholdError(f"unknown setting {where}.{unknown[0]}")
    for key in required:
        if key not in table:
            raise HouseholdError(f"{where}.{key} is required")
    for key, kind in keys.items():
        if key in table:
            value = table[key]
            if kind is bool and not isinstance(value, bool):
                raise HouseholdError(f"{where}.{key} must be true or false")
            if kind is str and (not isinstance(value, str) or not value.strip()):
                raise HouseholdError(f"{where}.{key} must be non-empty text in quotes")
            if kind is list and (not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value)):
                raise HouseholdError(f"{where}.{key} must be a list of text values")
    return table


def parse_household(data):
    if not isinstance(data, dict):
        raise HouseholdError("the household file must contain tables")
    unknown = sorted(set(data) - set(SECTIONS))
    if unknown:
        raise HouseholdError(f"unknown section {unknown[0]}")
    household = _typed(data.get("household") or {}, HOUSEHOLD_KEYS, "household")
    people = {}
    for person_id, table in (data.get("people") or {}).items():
        where = f"people.{person_id}"
        if not ID.match(person_id):
            raise HouseholdError(f"person id '{person_id}' must be lowercase letters, digits and underscores, starting with a letter")
        table = _typed(table, PERSON_KEYS, where, required=("name", "role"))
        if table["role"] not in ROLES:
            raise HouseholdError(f"{where}.role must be one of {', '.join(ROLES)}")
        if table.get("units", "imperial") not in UNITS:
            raise HouseholdError(f"{where}.units must be imperial or metric")
        people[person_id] = table
    owners = [p for p, t in people.items() if t["role"] == "owner"]
    if not people:
        raise HouseholdError("the household needs at least one person under [people.<id>]")
    if len(owners) != 1:
        raise HouseholdError(f"exactly one person must have role = \"owner\", found {len(owners)}")
    devices = {}
    for device_id, table in (data.get("devices") or {}).items():
        where = f"devices.{device_id}"
        if not ID.match(device_id):
            raise HouseholdError(f"device id '{device_id}' must be lowercase letters, digits and underscores, starting with a letter")
        table = _typed(table, DEVICE_KEYS, where, required=("kind", "name"))
        if table["kind"] not in DEVICE_KINDS:
            raise HouseholdError(f"{where}.kind must be one of {', '.join(DEVICE_KINDS)}")
        if "owner" in table and table["owner"] not in people:
            raise HouseholdError(f"{where}.owner '{table['owner']}' is not a person in the file")
        devices[device_id] = table
    accounts = {}
    for account_id, table in (data.get("accounts") or {}).items():
        where = f"accounts.{account_id}"
        table = _typed(table, ACCOUNT_KEYS, where, required=("person", "kind", "via"))
        if table["person"] not in people:
            raise HouseholdError(f"{where}.person '{table['person']}' is not a person in the file")
        accounts[account_id] = table
    grants = []
    raw_grants = data.get("grants") or []
    if not isinstance(raw_grants, list):
        raise HouseholdError("grants must be written as [[grants]] tables")
    for i, table in enumerate(raw_grants):
        where = f"grants[{i}]"
        table = _typed(table, GRANT_KEYS, where, required=("grantee", "scope", "granted_by"))
        for key in ("grantee", "granted_by"):
            if table[key] not in people:
                raise HouseholdError(f"{where}.{key} '{table[key]}' is not a person in the file")
        grants.append(table)
    seen = {}
    for person_id, table in people.items():
        for alias in [table["name"], *table.get("aliases", [])]:
            holder = seen.setdefault(alias.lower(), person_id)
            if holder != person_id:
                raise HouseholdError(f"alias '{alias}' is used by both {holder} and {person_id}")
    seen = {}
    for device_id, table in devices.items():
        for alias in table.get("aliases", []):
            holder = seen.setdefault(alias.lower(), device_id)
            if holder != device_id:
                raise HouseholdError(f"device alias '{alias}' is used by both {holder} and {device_id}")
    return {"household": {"town": household.get("town", ""), "vocabulary": household.get("vocabulary", [])},
            "people": people, "devices": devices, "accounts": accounts, "grants": grants}


def load_household(path):
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise HouseholdError(f"household file not found: {path}") from None
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise HouseholdError(f"{path}: {e}") from None
    return parse_household(parsed), hashlib.sha256(data).hexdigest()


def sync_household(cfg, storage, path=None, force=False):
    path = people_path(path)
    household, digest = load_household(path)
    summary = {"path": path, "changed": digest != storage.get_setting(DIGEST_KEY), "people": 0, "devices": 0, "accounts": 0,
               "grants": 0, "missing_people": [], "missing_devices": []}
    if not summary["changed"] and not force:
        return summary
    storage.set_setting(HOUSEHOLD_KEY, household["household"])
    for person_id, table in household["people"].items():
        storage.upsert_person(person_id, table["name"], table["role"], None, table.get("units", "imperial"),
                              False, table.get("aliases", []))
        summary["people"] += 1
    for device_id, table in household["devices"].items():
        runtime = {field: table[field] for field in RUNTIME_DEVICE_FIELDS if field in table}
        storage.upsert_device(device_id, table["kind"], table["name"], table.get("owner"), None,
                              table.get("capabilities", []), table.get("aliases", []), "file", **runtime)
        summary["devices"] += 1
    for account_id, table in household["accounts"].items():
        storage.upsert_account(account_id, table["person"], table["kind"], table["via"])
        summary["accounts"] += 1
    for table in household["grants"]:
        storage.grant(table["grantee"], table["scope"], table["granted_by"])
        summary["grants"] += 1
    summary["missing_people"] = sorted(p["id"] for p in storage.people() if p["id"] not in household["people"])
    summary["missing_devices"] = sorted(d["id"] for d in storage.devices() if d["source"] == "file" and d["id"] not in household["devices"])
    storage.set_setting(DIGEST_KEY, digest)
    return summary


def household_facts(storage):
    facts = storage.get_setting(HOUSEHOLD_KEY) or {}
    return {"town": facts.get("town", ""), "vocabulary": list(facts.get("vocabulary", []))}


def describe_household(storage):
    facts = household_facts(storage)
    lines = []
    if facts["town"] or facts["vocabulary"]:
        lines.append(f"Household: {facts['town'] or 'town not set'}" + (f"; local names: {', '.join(facts['vocabulary'])}" if facts["vocabulary"] else ""))
    lines.append("People:")
    for p in storage.people():
        extras = [p["role"], p["units"]]
        if p["aliases"]:
            extras.append("aka " + ", ".join(p["aliases"]))
        lines.append(f"  {p['id']:<16} {p['name']:<20} {' | '.join(extras)}")
    lines.append("Devices:")
    for d in storage.devices():
        extras = [d["kind"], f"owner {d['owner_id']}" if d["owner_id"] else "shared"]
        if d["capabilities"]:
            extras.append(", ".join(d["capabilities"]))
        if d["aliases"]:
            extras.append("aka " + ", ".join(d["aliases"]))
        lines.append(f"  {d['id']:<16} {d['name']:<24} {' | '.join(extras)}")
    lines.append("Accounts:")
    for a in storage.accounts():
        lines.append(f"  {a['id']:<16} {a['kind']} for {a['person_id']} via {a['via']}")
    lines.append("Grants:")
    for g in storage.grants():
        lines.append(f"  {g['grantee_id']} may use {g['scope']} (granted by {g['granted_by']})")
    if not storage.grants():
        lines[-1] = "Grants: none"
    return "\n".join(lines)
