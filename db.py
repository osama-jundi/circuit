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
dataset       : the uploaded workbook bytes (latest row = current dataset)
feeder_status : current status per feeder SN (survives restart)
audit_log     : every change (who / when / what), newest first
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


def _binary(b: bytes):
    """Wrap raw bytes for a binary column (BYTEA needs this on psycopg2)."""
    return psycopg2.Binary(b) if USE_PG else b


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Per-dialect DDL. Differences: autoincrement key and the binary column type.
def _schema():
    if USE_PG:
        pk = "BIGSERIAL PRIMARY KEY"
        blob = "BYTEA"
    else:
        pk = "INTEGER PRIMARY KEY AUTOINCREMENT"
        blob = "BLOB"
    return [
        f"""CREATE TABLE IF NOT EXISTS users (
            id            {pk},
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'user',
            must_change   INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS dataset (
            id          {pk},
            filename    TEXT NOT NULL,
            data        {blob} NOT NULL,
            uploaded_by TEXT,
            uploaded_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS feeder_status (
            sn         INTEGER PRIMARY KEY,
            status     TEXT NOT NULL,
            updated_by TEXT,
            updated_at TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS audit_log (
            id        {pk},
            ts        TEXT NOT NULL,
            username  TEXT,
            action    TEXT NOT NULL,
            sn        INTEGER,
            paulos    TEXT,
            source    TEXT,
            target    TEXT,
            old_value TEXT,
            new_value TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""",
    ]


def init_db():
    """Create tables if needed and seed the default admin."""
    for stmt in _schema():
        _exec(stmt)
    row = _exec("SELECT COUNT(*) AS n FROM users", fetch="one")
    if row["n"] == 0:
        _exec(
            "INSERT INTO users (username, password_hash, role, must_change, created_at)"
            " VALUES (?, ?, 'admin', 0, ?)",
            (DEFAULT_ADMIN, generate_password_hash(DEFAULT_ADMIN_PW), _utcnow()),
        )


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


# ---------------- Dataset (uploaded workbook) ----------------
def save_dataset(filename: str, data: bytes, uploaded_by: str):
    """Store a freshly uploaded workbook and clear old status overrides
    (the new file is a new baseline)."""
    _exec(
        "INSERT INTO dataset (filename, data, uploaded_by, uploaded_at)"
        " VALUES (?, ?, ?, ?)",
        (filename, _binary(data), uploaded_by, _utcnow()),
    )
    _exec("DELETE FROM feeder_status")


def latest_dataset():
    """Return (filename, data_bytes) of the current dataset, or None."""
    row = _exec(
        "SELECT filename, data FROM dataset ORDER BY id DESC LIMIT 1", fetch="one"
    )
    return (row["filename"], bytes(row["data"])) if row else None


# ---------------- Feeder status overrides ----------------
def get_status_overrides() -> dict:
    """{sn: status} of edits made since the dataset was uploaded."""
    rows = _exec("SELECT sn, status FROM feeder_status", fetch="all")
    return {r["sn"]: r["status"] for r in rows}


def set_feeder_status(sn: int, status: str, updated_by: str):
    # ON CONFLICT ... DO UPDATE (upsert) works the same on SQLite and Postgres.
    _exec(
        "INSERT INTO feeder_status (sn, status, updated_by, updated_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(sn) DO UPDATE SET"
        " status = excluded.status,"
        " updated_by = excluded.updated_by,"
        " updated_at = excluded.updated_at",
        (sn, status, updated_by, _utcnow()),
    )


# ---------------- Audit log ----------------
def log(action: str, username: str, **fields):
    _exec(
        "INSERT INTO audit_log (ts, username, action, sn, paulos, source, target,"
        " old_value, new_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
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


def recent_audit(limit: int = 200):
    return _exec(
        "SELECT ts, username, action, sn, paulos, source, target, old_value, new_value"
        " FROM audit_log ORDER BY id DESC LIMIT ?",
        (limit,),
        fetch="all",
    )
