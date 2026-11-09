import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 5
PROMPT_LIMIT_CHARS = 4000
ROLES = ("owner", "adult", "child", "guest")
DEVICE_KINDS = ("phone", "pc", "tv", "console", "camera", "speaker", "light", "sensor", "other")
DEVICE_SOURCES = ("file", "voice", "discovered")
REMINDER_KINDS = ("timer", "reminder", "alarm")
REPEATS = ("daily", "weekdays")
VISIBILITIES = ("household", "private")
RUNTIME_DEVICE_FIELDS = ("mac", "hostname", "last_ip", "tailscale_name", "kdeconnect_id", "adb_serial")
STOPWORDS = set("a an the my me i is are was of to in on for and or what whats which who that this it its do does "
                "you your about please remember know".split())

SCHEMA_V1 = """
CREATE TABLE people (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'adult', 'child', 'guest')),
    default_room TEXT,
    units TEXT NOT NULL DEFAULT 'imperial',
    announce_phone INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL
);
CREATE TABLE person_aliases (
    person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    alias TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (alias)
);
CREATE TABLE rooms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ha_area TEXT,
    input_device TEXT,
    output_device TEXT
);
CREATE TABLE devices (
    id TEXT PRIMARY KEY,
    owner_id TEXT REFERENCES people(id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK (kind IN ('phone', 'pc', 'tv', 'console', 'camera', 'speaker', 'light', 'sensor', 'other')),
    name TEXT NOT NULL,
    room_id TEXT REFERENCES rooms(id),
    mac TEXT,
    hostname TEXT,
    last_ip TEXT,
    tailscale_name TEXT,
    kdeconnect_id TEXT,
    adb_serial TEXT,
    capabilities TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL CHECK (source IN ('file', 'voice', 'discovered')),
    created TEXT NOT NULL,
    last_seen TEXT
);
CREATE TABLE device_aliases (
    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    alias TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (alias)
);
CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    via TEXT NOT NULL
);
CREATE TABLE grants (
    id INTEGER PRIMARY KEY,
    grantee_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    scope TEXT NOT NULL,
    granted_by TEXT NOT NULL REFERENCES people(id),
    created TEXT NOT NULL,
    UNIQUE (grantee_id, scope)
);
CREATE TABLE memory (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    owner_id TEXT REFERENCES people(id) ON DELETE CASCADE,
    visibility TEXT NOT NULL CHECK (visibility IN ('household', 'private')),
    saved TEXT NOT NULL,
    conversation_id TEXT
);
CREATE TABLE reminders (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('timer', 'reminder', 'alarm')),
    due TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    repeat TEXT CHECK (repeat IN ('daily', 'weekdays')),
    owner_id TEXT REFERENCES people(id) ON DELETE CASCADE,
    room_id TEXT REFERENCES rooms(id),
    notify_phone INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL
);
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    person_id TEXT,
    session_id TEXT,
    started TEXT NOT NULL,
    ended TEXT,
    reason TEXT,
    turns INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE TABLE turns (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    heard TEXT NOT NULL,
    reply TEXT,
    model TEXT,
    steps INTEGER,
    cost_usd REAL,
    latency_json TEXT
);
CREATE TABLE tool_calls (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    tool TEXT NOT NULL,
    input_json TEXT NOT NULL,
    decision TEXT NOT NULL,
    person_id TEXT,
    ok INTEGER,
    ms INTEGER
);
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE VIEW costs_daily AS
    SELECT substr(turns.ts, 1, 10) AS day, conversations.person_id AS person_id, SUM(turns.cost_usd) AS usd, COUNT(*) AS turns
    FROM turns JOIN conversations ON turns.conversation_id = conversations.id
    GROUP BY day, person_id;
"""
SCHEMA_V2 = "DROP TABLE IF EXISTS voiceprints;"
SCHEMA_V3 = """
CREATE TABLE list_items (
    id TEXT PRIMARY KEY,
    list_name TEXT NOT NULL,
    text TEXT NOT NULL,
    added TEXT NOT NULL,
    added_by TEXT REFERENCES people(id) ON DELETE SET NULL
);
CREATE INDEX list_items_by_list ON list_items (list_name, added);
"""
SCHEMA_V4 = """
CREATE TABLE packages (
    key TEXT PRIMARY KEY,
    owner_id TEXT REFERENCES people(id) ON DELETE CASCADE,
    carrier TEXT NOT NULL DEFAULT '',
    tracking TEXT NOT NULL DEFAULT '',
    order_ref TEXT NOT NULL DEFAULT '',
    merchant TEXT NOT NULL DEFAULT '',
    item TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    eta TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'email',
    updated TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    delivered_at TEXT,
    hidden INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX packages_by_owner ON packages (owner_id, updated);
CREATE TABLE package_messages (
    message_id TEXT PRIMARY KEY,
    seen TEXT NOT NULL
);
"""
SCHEMA_V5 = "ALTER TABLE reminders ADD COLUMN at_home INTEGER NOT NULL DEFAULT 1;"
MIGRATIONS = [(1, SCHEMA_V1), (2, SCHEMA_V2), (3, SCHEMA_V3), (4, SCHEMA_V4), (5, SCHEMA_V5)]


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def new_id():
    return uuid.uuid4().hex[:6]


def words(text):
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS}


def _check(value, allowed, what):
    if value not in allowed:
        raise ValueError(f"{what} must be one of {', '.join(a for a in allowed if a)}, not {value!r}")


class Storage:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.migrate()

    def close(self):
        with self.lock:
            self.conn.close()

    def version(self):
        with self.lock:
            table = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
            if not table:
                return 0
            row = self.conn.execute("SELECT version FROM schema_version").fetchone()
            return row["version"] if row else 0

    def migrate(self):
        with self.lock:
            current = self.version()
            for version, sql in MIGRATIONS:
                if version > current:
                    with self.conn:
                        self.conn.executescript(sql)
                        self.conn.execute("DELETE FROM schema_version")
                        self.conn.execute("INSERT INTO schema_version VALUES (?)", (version,))
                    current = version
            return current

    def tables(self):
        with self.lock:
            return sorted(r["name"] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))

    def _rows(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _row(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def upsert_person(self, person_id, name, role, default_room=None, units="imperial", announce_phone=False, aliases=()):
        _check(role, ROLES, "role")
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO people (id, name, role, default_room, units, announce_phone, created) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, role=excluded.role, default_room=excluded.default_room, "
                "units=excluded.units, announce_phone=excluded.announce_phone",
                (person_id, name, role, default_room, units, int(bool(announce_phone)), now_iso()))
            self.conn.execute("DELETE FROM person_aliases WHERE person_id=?", (person_id,))
            for alias in aliases:
                owner = self._row("SELECT person_id FROM person_aliases WHERE alias=?", (alias,))
                if owner and owner["person_id"] != person_id:
                    raise ValueError(f"alias '{alias}' already belongs to {owner['person_id']}")
                self.conn.execute("INSERT INTO person_aliases (person_id, alias) VALUES (?, ?)", (person_id, alias))
        return self.person(person_id)

    def _with_aliases(self, row, table, key):
        if row is None:
            return None
        row["aliases"] = [r["alias"] for r in self.conn.execute(f"SELECT alias FROM {table} WHERE {key}=? ORDER BY alias", (row["id"],))]
        return row

    def person(self, person_id):
        with self.lock:
            row = self._row("SELECT * FROM people WHERE id=?", (person_id,))
            if row:
                row["announce_phone"] = bool(row["announce_phone"])
            return self._with_aliases(row, "person_aliases", "person_id")

    def people(self):
        with self.lock:
            return [self.person(r["id"]) for r in self.conn.execute("SELECT id FROM people ORDER BY id")]

    def find_person(self, text):
        text = (text or "").strip()
        if not text:
            return None
        with self.lock:
            row = self._row("SELECT id FROM people WHERE id=? OR name=? COLLATE NOCASE", (text, text))
            if not row:
                row = self._row("SELECT person_id AS id FROM person_aliases WHERE alias=?", (text,))
            return self.person(row["id"]) if row else None

    def upsert_room(self, room_id, name, ha_area=None, input_device=None, output_device=None):
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO rooms (id, name, ha_area, input_device, output_device) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, ha_area=excluded.ha_area, "
                "input_device=excluded.input_device, output_device=excluded.output_device",
                (room_id, name, ha_area, input_device, output_device))
        return self.room(room_id)

    def room(self, room_id):
        with self.lock:
            return self._row("SELECT * FROM rooms WHERE id=?", (room_id,))

    def rooms(self):
        with self.lock:
            return self._rows("SELECT * FROM rooms ORDER BY id")

    def upsert_device(self, device_id, kind, name, owner_id=None, room_id=None, capabilities=(), aliases=(), source="file",
                      **runtime):
        _check(kind, DEVICE_KINDS, "device kind")
        _check(source, DEVICE_SOURCES, "device source")
        unknown = set(runtime) - set(RUNTIME_DEVICE_FIELDS)
        if unknown:
            raise ValueError(f"unknown device field {sorted(unknown)[0]}")
        values = {field: runtime.get(field) for field in RUNTIME_DEVICE_FIELDS}
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO devices (id, owner_id, kind, name, room_id, mac, hostname, last_ip, tailscale_name, kdeconnect_id, "
                "adb_serial, capabilities, source, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET owner_id=excluded.owner_id, kind=excluded.kind, name=excluded.name, "
                "room_id=excluded.room_id, capabilities=excluded.capabilities, "
                "mac=COALESCE(excluded.mac, devices.mac), hostname=COALESCE(excluded.hostname, devices.hostname), "
                "last_ip=COALESCE(excluded.last_ip, devices.last_ip), tailscale_name=COALESCE(excluded.tailscale_name, devices.tailscale_name), "
                "kdeconnect_id=COALESCE(excluded.kdeconnect_id, devices.kdeconnect_id), adb_serial=COALESCE(excluded.adb_serial, devices.adb_serial)",
                (device_id, owner_id, kind, name, room_id, values["mac"], values["hostname"], values["last_ip"], values["tailscale_name"],
                 values["kdeconnect_id"], values["adb_serial"], json.dumps(sorted(set(capabilities))), source, now_iso()))
            self.conn.execute("DELETE FROM device_aliases WHERE device_id=?", (device_id,))
            for alias in aliases:
                holder = self._row("SELECT device_id FROM device_aliases WHERE alias=?", (alias,))
                if holder and holder["device_id"] != device_id:
                    raise ValueError(f"alias '{alias}' already belongs to device {holder['device_id']}")
                self.conn.execute("INSERT INTO device_aliases (device_id, alias) VALUES (?, ?)", (device_id, alias))
        return self.device(device_id)

    def set_device_runtime(self, device_id, **fields):
        unknown = set(fields) - set(RUNTIME_DEVICE_FIELDS)
        if unknown:
            raise ValueError(f"unknown device field {sorted(unknown)[0]}")
        if not fields:
            return self.device(device_id)
        assignments = ", ".join(f"{field}=?" for field in fields) + ", last_seen=?"
        with self.lock, self.conn:
            changed = self.conn.execute(f"UPDATE devices SET {assignments} WHERE id=?", (*fields.values(), now_iso(), device_id)).rowcount
        if not changed:
            raise KeyError(f"no device {device_id}")
        return self.device(device_id)

    def _device_row(self, row):
        if row:
            row["capabilities"] = json.loads(row["capabilities"])
        return self._with_aliases(row, "device_aliases", "device_id")

    def device(self, device_id):
        with self.lock:
            return self._device_row(self._row("SELECT * FROM devices WHERE id=?", (device_id,)))

    def devices(self, owner_id=None, kind=None):
        sql, params = "SELECT id FROM devices", []
        clauses = []
        if owner_id is not None:
            clauses.append("owner_id=?")
            params.append(owner_id)
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self.lock:
            return [self.device(r["id"]) for r in self.conn.execute(sql + " ORDER BY id", params)]

    def find_device(self, text, owner_id=None):
        text = (text or "").strip()
        if not text:
            return None
        with self.lock:
            row = self._row("SELECT id FROM devices WHERE id=? OR name=? COLLATE NOCASE OR hostname=? COLLATE NOCASE", (text, text, text))
            if not row:
                row = self._row("SELECT device_id AS id FROM device_aliases WHERE alias=?", (text,))
            device = self.device(row["id"]) if row else None
            if device and owner_id is not None and device["owner_id"] not in (owner_id, None):
                return None
            return device

    def delete_device(self, device_id):
        with self.lock, self.conn:
            return self.conn.execute("DELETE FROM devices WHERE id=?", (device_id,)).rowcount

    def upsert_account(self, account_id, person_id, kind, via):
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO accounts (id, person_id, kind, via) VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                              "person_id=excluded.person_id, kind=excluded.kind, via=excluded.via", (account_id, person_id, kind, via))
        return self._row("SELECT * FROM accounts WHERE id=?", (account_id,))

    def accounts(self, person_id=None, kind=None):
        with self.lock:
            if person_id is None:
                return self._rows("SELECT * FROM accounts ORDER BY id")
            if kind is None:
                return self._rows("SELECT * FROM accounts WHERE person_id=? ORDER BY id", (person_id,))
            return self._rows("SELECT * FROM accounts WHERE person_id=? AND kind=? ORDER BY id", (person_id, kind))

    def grant(self, grantee_id, scope, granted_by):
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO grants (grantee_id, scope, granted_by, created) VALUES (?, ?, ?, ?) "
                              "ON CONFLICT(grantee_id, scope) DO UPDATE SET granted_by=excluded.granted_by", (grantee_id, scope, granted_by, now_iso()))

    def revoke(self, grantee_id, scope):
        with self.lock, self.conn:
            return self.conn.execute("DELETE FROM grants WHERE grantee_id=? AND scope=?", (grantee_id, scope)).rowcount

    def has_grant(self, grantee_id, scope):
        with self.lock:
            return self._row("SELECT 1 FROM grants WHERE grantee_id=? AND scope=?", (grantee_id, scope)) is not None

    def grants(self, grantee_id=None):
        with self.lock:
            if grantee_id is None:
                return self._rows("SELECT * FROM grants ORDER BY grantee_id, scope")
            return self._rows("SELECT * FROM grants WHERE grantee_id=? ORDER BY scope", (grantee_id,))

    def remember(self, text, owner_id=None, visibility="household", conversation_id=None, saved=None, fact_id=None):
        _check(visibility, VISIBILITIES, "visibility")
        text = text.strip()
        if not text:
            raise ValueError("a fact must not be empty")
        with self.lock, self.conn:
            scope = "visibility='household'" if visibility == "household" else "visibility='private' AND owner_id=?"
            params = (text,) if visibility == "household" else (text, owner_id)
            existing = self._row(f"SELECT * FROM memory WHERE text=? COLLATE NOCASE AND {scope}", params)
            if existing:
                return existing, False
            if not fact_id or self._row("SELECT 1 FROM memory WHERE id=?", (fact_id,)):
                fact_id = new_id()
            self.conn.execute("INSERT INTO memory (id, text, owner_id, visibility, saved, conversation_id) VALUES (?, ?, ?, ?, ?, ?)",
                              (fact_id, text, owner_id, visibility, saved or now_iso(), conversation_id))
        return self._row("SELECT * FROM memory WHERE id=?", (fact_id,)), True

    def memory_facts(self, viewer_id=None):
        with self.lock:
            return self._rows("SELECT * FROM memory WHERE visibility='household' OR (visibility='private' AND owner_id=?) "
                              "ORDER BY saved, rowid", (viewer_id,))

    def _scored(self, query, viewer_id):
        q = words(query)
        scored = []
        for fact in self.memory_facts(viewer_id):
            f = words(fact["text"])
            score = len(q & f) + sum(0.5 for w in q for v in f if w != v and min(len(w), len(v)) >= 3 and (w in v or v in w))
            if query.strip().lower() in fact["text"].lower():
                score += 2
            if score:
                scored.append((score, fact))
        return sorted(scored, key=lambda x: -x[0])

    def recall(self, query, viewer_id=None, limit=10):
        with self.lock:
            return [f for _, f in self._scored(query, viewer_id)[:limit]]

    def forget(self, which, viewer_id=None):
        which = which.strip()
        with self.lock, self.conn:
            visible = self.memory_facts(viewer_id)
            exact = [f for f in visible if f["id"] == which.lower()]
            if exact:
                removed = exact
            else:
                scored = self._scored(which, viewer_id)
                if not scored:
                    return [], []
                if len(scored) > 1 and scored[1][0] == scored[0][0]:
                    return [], [f for score, f in scored if score == scored[0][0]]
                removed = [scored[0][1]]
            for fact in removed:
                self.conn.execute("DELETE FROM memory WHERE id=?", (fact["id"],))
            return removed, []

    def memory_prompt_block(self, viewer_id=None, limit_chars=PROMPT_LIMIT_CHARS):
        facts = self.memory_facts(viewer_id)
        lines, size = [], 0
        for fact in reversed(facts):
            line = f"- {fact['text']}"
            if size + len(line) > limit_chars:
                break
            lines.append(line)
            size += len(line)
        if not lines:
            return ""
        more = len(facts) - len(lines)
        extra = f" ({more} older facts not shown; use recall to search them.)" if more else ""
        return "Things you've been asked to remember (long-term memory)" + extra + ":\n" + "\n".join(reversed(lines))

    def add_reminder(self, kind, due, label="", repeat=None, owner_id=None, room_id=None, notify_phone=False, reminder_id=None, created=None,
                     at_home=True):
        _check(kind, REMINDER_KINDS, "reminder kind")
        if repeat is not None:
            _check(repeat, REPEATS, "repeat")
        if isinstance(due, datetime):
            due = due.isoformat(timespec="seconds")
        with self.lock, self.conn:
            if not reminder_id or self._row("SELECT 1 FROM reminders WHERE id=?", (reminder_id,)):
                reminder_id = new_id()
            self.conn.execute("INSERT INTO reminders (id, kind, due, label, repeat, owner_id, room_id, notify_phone, created, at_home) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (reminder_id, kind, due, label or "", repeat, owner_id, room_id, int(bool(notify_phone)), created or now_iso(),
                               int(bool(at_home))))
        return self.reminder(reminder_id)

    def _reminder_row(self, row):
        if row:
            row["notify_phone"] = bool(row["notify_phone"])
            row["at_home"] = bool(row.get("at_home", 1))
        return row

    def reminder(self, reminder_id):
        with self.lock:
            return self._reminder_row(self._row("SELECT * FROM reminders WHERE id=?", (reminder_id,)))

    def reminders(self, owner_id=None):
        with self.lock:
            if owner_id is None:
                rows = self._rows("SELECT * FROM reminders ORDER BY due, rowid")
            else:
                rows = self._rows("SELECT * FROM reminders WHERE owner_id=? ORDER BY due, rowid", (owner_id,))
            return [self._reminder_row(r) for r in rows]

    def reminders_due(self, now=None):
        now = (now or datetime.now()).isoformat(timespec="seconds")
        with self.lock:
            return [self._reminder_row(r) for r in self._rows("SELECT * FROM reminders WHERE due<=? ORDER BY due, rowid", (now,))]

    def reschedule_reminder(self, reminder_id, due):
        if isinstance(due, datetime):
            due = due.isoformat(timespec="seconds")
        with self.lock, self.conn:
            if not self.conn.execute("UPDATE reminders SET due=? WHERE id=?", (due, reminder_id)).rowcount:
                raise KeyError(f"no reminder {reminder_id}")
        return self.reminder(reminder_id)

    def delete_reminder(self, reminder_id):
        with self.lock, self.conn:
            return self.conn.execute("DELETE FROM reminders WHERE id=?", (reminder_id,)).rowcount

    def cancel_reminders(self, which, owner_id=None):
        which = (which or "").strip().lower()
        with self.lock, self.conn:
            items = self.reminders(owner_id)
            if which in ("all", "everything"):
                removed = items
            else:
                removed = [i for i in items if i["id"] == which or (which and which in i["label"].lower()) or which == i["kind"]]
            for item in removed:
                self.conn.execute("DELETE FROM reminders WHERE id=?", (item["id"],))
            return removed

    def start_conversation(self, conversation_id, room_id, person_id=None, session_id=None):
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO conversations (id, room_id, person_id, session_id, started) VALUES (?, ?, ?, ?, ?)",
                              (conversation_id, room_id, person_id, session_id, now_iso()))
        return self.conversation(conversation_id)

    def end_conversation(self, conversation_id, reason):
        with self.lock, self.conn:
            self.conn.execute("UPDATE conversations SET ended=?, reason=? WHERE id=?", (now_iso(), reason, conversation_id))
        return self.conversation(conversation_id)

    def conversation(self, conversation_id):
        with self.lock:
            return self._row("SELECT * FROM conversations WHERE id=?", (conversation_id,))

    def conversations(self, limit=50):
        with self.lock:
            return self._rows("SELECT * FROM conversations ORDER BY started DESC, rowid DESC LIMIT ?", (limit,))

    def add_turn(self, conversation_id, heard, reply=None, model=None, steps=None, cost_usd=None, latency=None):
        with self.lock, self.conn:
            cur = self.conn.execute("INSERT INTO turns (conversation_id, ts, heard, reply, model, steps, cost_usd, latency_json) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                    (conversation_id, now_iso(), heard, reply, model, steps, cost_usd, json.dumps(latency) if latency is not None else None))
            self.conn.execute("UPDATE conversations SET turns=turns+1, cost_usd=cost_usd+? WHERE id=?", (cost_usd or 0.0, conversation_id))
            return cur.lastrowid

    def turns(self, conversation_id):
        with self.lock:
            rows = self._rows("SELECT * FROM turns WHERE conversation_id=? ORDER BY id", (conversation_id,))
            for row in rows:
                row["latency"] = json.loads(row.pop("latency_json")) if row["latency_json"] else None
            return rows

    def add_tool_call(self, conversation_id, tool, tool_input, decision, person_id=None, ok=None, ms=None):
        with self.lock, self.conn:
            cur = self.conn.execute("INSERT INTO tool_calls (conversation_id, ts, tool, input_json, decision, person_id, ok, ms) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                    (conversation_id, now_iso(), tool, json.dumps(tool_input, default=str), decision, person_id,
                                     None if ok is None else int(bool(ok)), ms))
            return cur.lastrowid

    def tool_calls(self, conversation_id=None, limit=100):
        with self.lock:
            if conversation_id is None:
                rows = self._rows("SELECT * FROM tool_calls ORDER BY id DESC LIMIT ?", (limit,))
            else:
                rows = self._rows("SELECT * FROM tool_calls WHERE conversation_id=? ORDER BY id LIMIT ?", (conversation_id, limit))
            for row in rows:
                row["input"] = json.loads(row.pop("input_json"))
                row["ok"] = None if row["ok"] is None else bool(row["ok"])
            return rows

    def costs_daily(self, day=None, person_id=None):
        clauses, params = [], []
        if day is not None:
            clauses.append("day=?")
            params.append(day if isinstance(day, str) else day.isoformat())
        if person_id is not None:
            clauses.append("person_id=?")
            params.append(person_id)
        sql = "SELECT * FROM costs_daily" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY day, person_id"
        with self.lock:
            return self._rows(sql, params)

    def cost_total(self, day=None, person_id=None):
        return sum(r["usd"] for r in self.costs_daily(day, person_id))

    def get_setting(self, key, default=None):
        with self.lock:
            row = self._row("SELECT value FROM settings WHERE key=?", (key,))
            return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO settings (key, value, updated) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                              "value=excluded.value, updated=excluded.updated", (key, json.dumps(value), now_iso()))
        return value

    def settings(self):
        with self.lock:
            return {r["key"]: json.loads(r["value"]) for r in self.conn.execute("SELECT key, value FROM settings ORDER BY key")}

    def prune_conversations(self, retention_days, now=None):
        cutoff = ((now or datetime.now()) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self.lock, self.conn:
            return self.conn.execute("DELETE FROM conversations WHERE started<?", (cutoff,)).rowcount

    def forget_person(self, person_id):
        with self.lock, self.conn:
            counts = {
                "memory": self.conn.execute("DELETE FROM memory WHERE owner_id=? AND visibility='private'", (person_id,)).rowcount,
                "tool_calls": self.conn.execute("DELETE FROM tool_calls WHERE person_id=?", (person_id,)).rowcount,
                "turns": self.conn.execute("DELETE FROM turns WHERE conversation_id IN (SELECT id FROM conversations WHERE person_id=?)", (person_id,)).rowcount,
                "conversations": self.conn.execute("DELETE FROM conversations WHERE person_id=?", (person_id,)).rowcount,
            }
        return counts

    @staticmethod
    def list_key(name):
        key = " ".join(re.findall(r"[a-z0-9]+", name.lower()))
        key = re.sub(r"^(?:my |our |the )+", "", key)
        key = re.sub(r" list$", "", key).strip()
        return {"todo": "to do", "grocery": "shopping", "groceries": "shopping"}.get(key, key)

    def add_list_items(self, list_name, items, added_by=None):
        key = self.list_key(list_name)
        if not key:
            raise ValueError("a list needs a name")
        added, already = [], []
        with self.lock, self.conn:
            for item in items:
                text = " ".join(item.split())
                if not text:
                    continue
                if self._row("SELECT 1 FROM list_items WHERE list_name=? AND text=? COLLATE NOCASE", (key, text)):
                    already.append(text)
                    continue
                self.conn.execute("INSERT INTO list_items VALUES (?, ?, ?, ?, ?)", (new_id(), key, text, now_iso(), added_by))
                added.append(text)
        return key, added, already

    def list_items(self, list_name):
        with self.lock:
            return self._rows("SELECT * FROM list_items WHERE list_name=? ORDER BY added, rowid", (self.list_key(list_name),))

    def package(self, key):
        with self.lock:
            return self._row("SELECT * FROM packages WHERE key=?", (key,))

    def packages(self, owner_id, include_hidden=False):
        with self.lock:
            sql = "SELECT * FROM packages WHERE owner_id IS ?" + ("" if include_hidden else " AND hidden=0") + " ORDER BY updated DESC"
            return self._rows(sql, (owner_id,))

    def save_package(self, row):
        fields = ("key", "owner_id", "carrier", "tracking", "order_ref", "merchant", "item", "status", "eta", "url", "source", "updated",
                  "first_seen", "delivered_at", "hidden")
        with self.lock, self.conn:
            self.conn.execute(f"INSERT OR REPLACE INTO packages ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                              tuple(row.get(f) for f in fields))

    def hide_package(self, key, owner_id):
        with self.lock, self.conn:
            return self.conn.execute("UPDATE packages SET hidden=1 WHERE key=? AND owner_id IS ?", (key, owner_id)).rowcount

    def package_message_seen(self, message_id):
        with self.lock:
            return self._row("SELECT 1 FROM package_messages WHERE message_id=?", (message_id,)) is not None

    def mark_package_message(self, message_id):
        with self.lock, self.conn:
            self.conn.execute("INSERT OR IGNORE INTO package_messages VALUES (?, ?)", (message_id, now_iso()))

    def list_names(self):
        with self.lock:
            return [(r["list_name"], r["n"]) for r in self._rows("SELECT list_name, COUNT(*) AS n FROM list_items GROUP BY list_name ORDER BY list_name")]

    def remove_list_items(self, list_name, items):
        key = self.list_key(list_name)
        removed, missing = [], []
        with self.lock, self.conn:
            for item in items:
                wanted = words(item)
                rows = self._rows("SELECT * FROM list_items WHERE list_name=? ORDER BY added", (key,))
                exact = [r for r in rows if r["text"].lower() == item.strip().lower()]
                match = exact or [r for r in rows if wanted and wanted <= words(r["text"])]
                if not match:
                    missing.append(item)
                    continue
                self.conn.execute("DELETE FROM list_items WHERE id=?", (match[0]["id"],))
                removed.append(match[0]["text"])
        return key, removed, missing

    def clear_list(self, list_name):
        key = self.list_key(list_name)
        with self.lock, self.conn:
            return key, self.conn.execute("DELETE FROM list_items WHERE list_name=?", (key,)).rowcount

