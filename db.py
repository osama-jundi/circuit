"""
db.py - persistence + auth helpers for the SLD app.

Backend
-------
If the DATABASE_URL environment variable is set we use **PostgreSQL**
(e.g. on Render). Otherwise we fall back to a local **SQLite** file, so
development stays zero-config. The same `?`-style SQL runs against both;
the only per-dialect bits are the CREATE TABLE statements and binary
handling, isolated below.

Tables
------
users         : login accounts (username, password hash, role)
projects      : each uploaded workbook = one project (bytes + metadata +
                a `revision` counter bumped on every status change so clients
                can cheaply poll for "did anything change?")
feeder_status : current status per (project, feeder SN); survives restart
audit_log     : every change (who / when / what / which project), newest first
meta          : misc key/value (e.g. the Flask secret key)

Connections are short-lived (opened per call) so the threaded dev server
and gunicorn workers stay safe.
"""

import os
import secrets
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash, check_password_hash

DATABASE_URL = os.environ.get("DATABASE_URL")
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3

DB_PATH = os.environ.get("CIRCUIT_DB", "circuit.db")

# Seeded on first run. You can override these via the environment (handy on
# Render: set ADMIN_USERNAME / ADMIN_PASSWORD). If left at the defaults, the
# UI shows a banner telling the admin to change the password.
DEFAULT_ADMIN = os.environ.get("ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PW = os.environ.get("ADMIN_PASSWORD", "admin123")

VALID_ROLES = ("admin", "user")


def _connect():
    if USE_PG:
        return psycopg2.connect(DATABASE_URL)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _exec(sql, params=(), fetch=None):
    """Run one statement and return dict rows.

    `sql` is written with `?` placeholders; we translate them to `%s` for
    Postgres. `fetch` is None | 'one' | 'all'.
    """
    if USE_PG:
        sql = sql.replace("?", "%s")
    conn = _connect()
    try:
        if USE_PG:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = conn.cursor()
        cur.execute(sql, params)
        result = None
        if fetch == "one":
            row = cur.fetchone()
            result = dict(row) if row is not None else None
        elif fetch == "all":
            result = [dict(r) for r in cur.fetchall()]
        conn.commit()
        return result
    finally:
        conn.close()


def _insert_returning_id(sql, params):
    """Run an INSERT and return the new row's id (cross-dialect)."""
    conn = _connect()
    try:
        cur = conn.cursor()
        if USE_PG:
            cur.execute(sql.replace("?", "%s") + " RETURNING id", params)
            new_id = cur.fetchone()[0]
        else:
            cur.execute(sql, params)
            new_id = cur.lastrowid
        conn.commit()
        return new_id
    finally:
        conn.close()


def _binary(b: bytes):
    """Wrap raw bytes for a binary column (BYTEA needs this on psycopg2)."""
    return psycopg2.Binary(b) if USE_PG else b


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- Schema introspection (used by the legacy migration) ----
def _table_exists(name: str) -> bool:
    if USE_PG:
        row = _exec(
            "SELECT 1 AS x FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name = ?", (name,), fetch="one")
    else:
        row = _exec(
            "SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,), fetch="one")
    return row is not None


def _column_exists(table: str, column: str) -> bool:
    if USE_PG:
        row = _exec(
            "SELECT 1 AS x FROM information_schema.columns"
            " WHERE table_name = ? AND column_name = ?", (table, column), fetch="one")
        return row is not None
    rows = _exec(f"PRAGMA table_info({table})", fetch="all")
    return any(r["name"] == column for r in rows)


# ---- Per-dialect DDL fragments ----
def _pk():
    return "BIGSERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"


def _blob():
    return "BYTEA" if USE_PG else "BLOB"


def _ddl_feeder_status():
    return """CREATE TABLE IF NOT EXISTS feeder_status (
        project_id INTEGER NOT NULL,
        sn         INTEGER NOT NULL,
        status     TEXT NOT NULL,
        updated_by TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (project_id, sn)
    )"""


def _ddl_audit_log():
    return f"""CREATE TABLE IF NOT EXISTS audit_log (
        id         {_pk()},
        project_id INTEGER,
        ts         TEXT NOT NULL,
        username   TEXT,
        action     TEXT NOT NULL,
        sn         INTEGER,
        paulos     TEXT,
        source     TEXT,
        target     TEXT,
        old_value  TEXT,
        new_value  TEXT
    )"""


def init_db():
    """Create tables if needed, migrate any old single-dataset DB, and seed
    the default admin."""
    _exec(f"""CREATE TABLE IF NOT EXISTS users (
        id            {_pk()},
        username      TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'user',
        must_change   INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    )""")
    _exec("""CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    _exec(f"""CREATE TABLE IF NOT EXISTS projects (
        id         {_pk()},
        name       TEXT NOT NULL,
        filename   TEXT,
        data       {_blob()} NOT NULL,
        created_by TEXT,
        created_at TEXT NOT NULL,
        revision   INTEGER NOT NULL DEFAULT 0
    )""")

    # Upgrade path: an older DB has a single `dataset` table and a
    # `feeder_status` keyed only by sn. Migrate it into one project so no
    # data is lost.
    legacy = _table_exists("feeder_status") and not _column_exists("feeder_status", "project_id")
    if legacy:
        _migrate_legacy()
    else:
        _exec(_ddl_feeder_status())
        _exec(_ddl_audit_log())

    if _exec("SELECT COUNT(*) AS n FROM users", fetch="one")["n"] == 0:
        _exec(
            "INSERT INTO users (username, password_hash, role, must_change, created_at)"
            " VALUES (?, ?, 'admin', 0, ?)",
            (DEFAULT_ADMIN, generate_password_hash(DEFAULT_ADMIN_PW), _utcnow()),
        )


def _migrate_legacy():
    """Fold the old single-dataset schema into the new project model.

    The current dataset (the most recent upload) becomes one project, and the
    existing status edits + history are attached to it.
    """
    print("Migrating legacy single-dataset database into a project…")
    pid = None
    if _table_exists("dataset"):
        last = _exec("SELECT * FROM dataset ORDER BY id DESC LIMIT 1", fetch="one")
        if last:
            name = (last["filename"] or "Imported project")
            name = name.rsplit(".", 1)[0]
            pid = _insert_returning_id(
                "INSERT INTO projects (name, filename, data, created_by, created_at, revision)"
                " VALUES (?, ?, ?, ?, ?, 0)",
                (name, last["filename"], _binary(bytes(last["data"])),
                 last.get("uploaded_by"), last.get("uploaded_at") or _utcnow()),
            )

    _exec("ALTER TABLE feeder_status RENAME TO feeder_status_old")
    audit_old = _table_exists("audit_log")
    if audit_old:
        _exec("ALTER TABLE audit_log RENAME TO audit_log_old")

    _exec(_ddl_feeder_status())
    _exec(_ddl_audit_log())

    if pid is not None:
        _exec(
            "INSERT INTO feeder_status (project_id, sn, status, updated_by, updated_at)"
            " SELECT ?, sn, status, updated_by, updated_at FROM feeder_status_old",
            (pid,))
        if audit_old:
            _exec(
                "INSERT INTO audit_log (project_id, ts, username, action, sn, paulos,"
                " source, target, old_value, new_value)"
                " SELECT ?, ts, username, action, sn, paulos, source, target,"
                " old_value, new_value FROM audit_log_old",
                (pid,))

    _exec("DROP TABLE feeder_status_old")
    if audit_old:
        _exec("DROP TABLE audit_log_old")
    if _table_exists("dataset"):
        _exec("DROP TABLE dataset")
    print("Migration complete.")


def get_secret_key() -> str:
    """A stable Flask secret key.

    Prefer the SECRET_KEY environment variable (set this in production /
    Render). If it's absent we fall back to a random key persisted in the
    meta table so sessions survive a restart on a single instance.
    """
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    row = _exec("SELECT value FROM meta WHERE key = 'secret_key'", fetch="one")
    if row:
        return row["value"]
    key = secrets.token_hex(32)
    _exec("INSERT INTO meta (key, value) VALUES ('secret_key', ?)", (key,))
    return key


# ---------------- Users ----------------
def get_user(username: str):
    return _exec("SELECT * FROM users WHERE username = ?", (username,), fetch="one")


def verify_login(username: str, password: str):
    """Return the user row on success, else None."""
    u = get_user(username)
    if u and check_password_hash(u["password_hash"], password):
        return u
    return None


def list_users():
    return _exec(
        "SELECT id, username, role, must_change, created_at FROM users ORDER BY username",
        fetch="all",
    )


def create_user(username: str, password: str, role: str):
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES}")
    username = username.strip()
    if not username or not password:
        raise ValueError("username and password are required")
    if _exec("SELECT 1 AS x FROM users WHERE username = ?", (username,), fetch="one"):
        raise ValueError(f"user '{username}' already exists")
    _exec(
        "INSERT INTO users (username, password_hash, role, must_change, created_at)"
        " VALUES (?, ?, ?, 0, ?)",
        (username, generate_password_hash(password), role, _utcnow()),
    )


def set_role(username: str, role: str):
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES}")
    _exec("UPDATE users SET role = ? WHERE username = ?", (role, username))


def set_password(username: str, password: str):
    if not password:
        raise ValueError("password is required")
    _exec(
        "UPDATE users SET password_hash = ?, must_change = 0 WHERE username = ?",
        (generate_password_hash(password), username),
    )


def delete_user(username: str):
    _exec("DELETE FROM users WHERE username = ?", (username,))


def count_admins() -> int:
    return _exec(
        "SELECT COUNT(*) AS n FROM users WHERE role = 'admin'", fetch="one"
    )["n"]


# ---------------- Projects ----------------
def list_projects():
    """Lightweight metadata for the project switcher (no workbook bytes)."""
    return _exec(
        "SELECT id, name, filename, created_by, created_at, revision"
        " FROM projects ORDER BY created_at, id",
        fetch="all",
    )


def get_project(pid: int):
    """Full project row including the workbook bytes."""
    return _exec(
        "SELECT id, name, filename, data, created_by, created_at, revision"
        " FROM projects WHERE id = ?", (pid,), fetch="one")


def project_exists(pid: int) -> bool:
    return _exec("SELECT 1 AS x FROM projects WHERE id = ?", (pid,), fetch="one") is not None


def create_project(name: str, filename: str, data: bytes, created_by: str) -> int:
    name = (name or filename or "Untitled project").strip()
    return _insert_returning_id(
        "INSERT INTO projects (name, filename, data, created_by, created_at, revision)"
        " VALUES (?, ?, ?, ?, ?, 0)",
        (name, filename, _binary(data), created_by, _utcnow()),
    )


def rename_project(pid: int, name: str):
    name = (name or "").strip()
    if not name:
        raise ValueError("project name is required")
    _exec("UPDATE projects SET name = ? WHERE id = ?", (name, pid))


def delete_project(pid: int):
    _exec("DELETE FROM feeder_status WHERE project_id = ?", (pid,))
    _exec("DELETE FROM audit_log WHERE project_id = ?", (pid,))
    _exec("DELETE FROM projects WHERE id = ?", (pid,))


def get_revision(pid: int):
    row = _exec("SELECT revision FROM projects WHERE id = ?", (pid,), fetch="one")
    return row["revision"] if row else None


# ---------------- Feeder status (per project) ----------------
def get_status_overrides(pid: int) -> dict:
    """{sn: status} of edits made to this project since it was uploaded."""
    rows = _exec("SELECT sn, status FROM feeder_status WHERE project_id = ?",
                 (pid,), fetch="all")
    return {r["sn"]: r["status"] for r in rows}


def set_feeder_status(pid: int, sn: int, status: str, updated_by: str):
    # Upsert the status, then bump the project revision so other clients
    # polling for changes notice. ON CONFLICT works on SQLite and Postgres.
    _exec(
        "INSERT INTO feeder_status (project_id, sn, status, updated_by, updated_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(project_id, sn) DO UPDATE SET"
        " status = excluded.status,"
        " updated_by = excluded.updated_by,"
        " updated_at = excluded.updated_at",
        (pid, sn, status, updated_by, _utcnow()),
    )
    _exec("UPDATE projects SET revision = revision + 1 WHERE id = ?", (pid,))


# ---------------- Audit log ----------------
def log(action: str, username: str, project_id=None, **fields):
    _exec(
        "INSERT INTO audit_log (project_id, ts, username, action, sn, paulos, source,"
        " target, old_value, new_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            _utcnow(),
            username,
            action,
            fields.get("sn"),
            fields.get("paulos"),
            fields.get("source"),
            fields.get("target"),
            fields.get("old_value"),
            fields.get("new_value"),
        ),
    )


def recent_audit(project_id=None, limit: int = 300):
    cols = ("ts, username, action, sn, paulos, source, target, old_value, new_value")
    if project_id is None:
        return _exec(
            f"SELECT {cols} FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,), fetch="all")
    return _exec(
        f"SELECT {cols} FROM audit_log WHERE project_id = ? ORDER BY id DESC LIMIT ?",
        (project_id, limit), fetch="all")
