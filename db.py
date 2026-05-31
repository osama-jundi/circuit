"""
db.py - SQLite persistence + auth helpers for the SLD app.

Tables
------
users         : login accounts (username, bcrypt-ish hash, role)
dataset       : the uploaded workbook bytes (latest row = current dataset)
feeder_status : current status per feeder SN (survives restart)
audit_log     : every change (who / when / what), newest first
meta          : misc key/value (e.g. the Flask secret key)

Everything uses short-lived connections so the threaded dev server is safe.
"""

import sqlite3
import os
import secrets
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.environ.get("CIRCUIT_DB", "circuit.db")

# Seeded on first run; the UI shows a banner telling the admin to change it.
DEFAULT_ADMIN = "admin"
DEFAULT_ADMIN_PW = "admin123"

VALID_ROLES = ("admin", "user")


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db():
    """Create tables if needed and seed the default admin. Returns nothing."""
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'user',
                must_change   INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dataset (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                filename    TEXT NOT NULL,
                data        BLOB NOT NULL,
                uploaded_by TEXT,
                uploaded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feeder_status (
                sn         INTEGER PRIMARY KEY,
                status     TEXT NOT NULL,
                updated_by TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT NOT NULL,
                username  TEXT,
                action    TEXT NOT NULL,
                sn        INTEGER,
                paulos    TEXT,
                source    TEXT,
                target    TEXT,
                old_value TEXT,
                new_value TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # Seed the default admin once.
        row = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        if row["n"] == 0:
            c.execute(
                "INSERT INTO users (username, password_hash, role, must_change, created_at)"
                " VALUES (?, ?, 'admin', 0, ?)",
                (DEFAULT_ADMIN, generate_password_hash(DEFAULT_ADMIN_PW), _utcnow()),
            )


def get_secret_key() -> str:
    """A stable Flask secret key, persisted in the meta table."""
    with _conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key = 'secret_key'").fetchone()
        if row:
            return row["value"]
        key = secrets.token_hex(32)
        c.execute("INSERT INTO meta (key, value) VALUES ('secret_key', ?)", (key,))
        return key


# ---------------- Users ----------------
def get_user(username: str):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()


def verify_login(username: str, password: str):
    """Return the user row on success, else None."""
    u = get_user(username)
    if u and check_password_hash(u["password_hash"], password):
        return u
    return None


def list_users():
    with _conn() as c:
        return c.execute(
            "SELECT id, username, role, must_change, created_at FROM users ORDER BY username"
        ).fetchall()


def create_user(username: str, password: str, role: str):
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES}")
    username = username.strip()
    if not username or not password:
        raise ValueError("username and password are required")
    with _conn() as c:
        exists = c.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        if exists:
            raise ValueError(f"user '{username}' already exists")
        c.execute(
            "INSERT INTO users (username, password_hash, role, must_change, created_at)"
            " VALUES (?, ?, ?, 0, ?)",
            (username, generate_password_hash(password), role, _utcnow()),
        )


def set_role(username: str, role: str):
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES}")
    with _conn() as c:
        c.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))


def set_password(username: str, password: str):
    if not password:
        raise ValueError("password is required")
    with _conn() as c:
        c.execute(
            "UPDATE users SET password_hash = ?, must_change = 0 WHERE username = ?",
            (generate_password_hash(password), username),
        )


def delete_user(username: str):
    with _conn() as c:
        c.execute("DELETE FROM users WHERE username = ?", (username,))


def count_admins() -> int:
    with _conn() as c:
        return c.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin'"
        ).fetchone()["n"]


# ---------------- Dataset (uploaded workbook) ----------------
def save_dataset(filename: str, data: bytes, uploaded_by: str):
    """Store a freshly uploaded workbook and clear old status overrides
    (the new file is a new baseline)."""
    with _conn() as c:
        c.execute(
            "INSERT INTO dataset (filename, data, uploaded_by, uploaded_at)"
            " VALUES (?, ?, ?, ?)",
            (filename, data, uploaded_by, _utcnow()),
        )
        c.execute("DELETE FROM feeder_status")


def latest_dataset():
    """Return (filename, data_bytes) of the current dataset, or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT filename, data FROM dataset ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return (row["filename"], bytes(row["data"])) if row else None


# ---------------- Feeder status overrides ----------------
def get_status_overrides() -> dict:
    """{sn: status} of edits made since the dataset was uploaded."""
    with _conn() as c:
        rows = c.execute("SELECT sn, status FROM feeder_status").fetchall()
        return {r["sn"]: r["status"] for r in rows}


def set_feeder_status(sn: int, status: str, updated_by: str):
    with _conn() as c:
        c.execute(
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
    with _conn() as c:
        c.execute(
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
    with _conn() as c:
        return c.execute(
            "SELECT ts, username, action, sn, paulos, source, target, old_value, new_value"
            " FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
