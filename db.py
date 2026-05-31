"""
db.py - persistence + auth helpers for the SLD app.

Backend
-------
If the DATABASE_URL environment variable is set we use **PostgreSQL**
(e.g. on Render). Otherwise we fall back to a local **SQLite** file, so
development stays zero-config. The same `?`-style SQL runs against both;
the only per-dialect bits are the CREATE TABLE statements and binary
handling, isolated below.

Data model
----------
projects      : a named container (a job / site). Holds many files.
files         : one uploaded .xlsx = one diagram, belonging to a project.
                Holds the workbook bytes + a `revision` counter bumped on
                every status change so clients can poll for "did it change?".
feeder_status : current status per (file, feeder SN); survives restart.
audit_log     : every change (who / when / what / which project+file).
users         : login accounts.
meta          : misc key/value (e.g. the Flask secret key).

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
    """Run one statement and return dict rows. `?` placeholders are translated
    to `%s` for Postgres. `fetch` is None | 'one' | 'all'."""
    if USE_PG:
        sql = sql.replace("?", "%s")
    conn = _connect()
    try:
        cur = (conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
               if USE_PG else conn.cursor())
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
    return psycopg2.Binary(b) if USE_PG else b


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- Schema introspection (used by migrations) ----
def _table_exists(name: str) -> bool:
    if USE_PG:
        row = _exec("SELECT 1 AS x FROM information_schema.tables"
                    " WHERE table_schema = 'public' AND table_name = ?", (name,), fetch="one")
    else:
        row = _exec("SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (name,), fetch="one")
    return row is not None


def _column_exists(table: str, column: str) -> bool:
    if USE_PG:
        return _exec("SELECT 1 AS x FROM information_schema.columns"
                     " WHERE table_name = ? AND column_name = ?",
                     (table, column), fetch="one") is not None
    rows = _exec(f"PRAGMA table_info({table})", fetch="all")
    return any(r["name"] == column for r in rows)


# ---- Per-dialect DDL ----
def _pk():
    return "BIGSERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"


def _blob():
    return "BYTEA" if USE_PG else "BLOB"


def _ddl_projects():
    return f"""CREATE TABLE IF NOT EXISTS projects (
        id         {_pk()},
        name       TEXT NOT NULL,
        created_by TEXT,
        created_at TEXT NOT NULL
    )"""


def _ddl_files():
    return f"""CREATE TABLE IF NOT EXISTS files (
        id         {_pk()},
        project_id INTEGER NOT NULL,
        name       TEXT NOT NULL,
        filename   TEXT,
        data       {_blob()} NOT NULL,
        created_by TEXT,
        created_at TEXT NOT NULL,
        revision   INTEGER NOT NULL DEFAULT 0
    )"""


def _ddl_feeder_status():
    return """CREATE TABLE IF NOT EXISTS feeder_status (
        file_id    INTEGER NOT NULL,
        sn         INTEGER NOT NULL,
        status     TEXT NOT NULL,
        updated_by TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (file_id, sn)
    )"""


def _ddl_audit_log():
    return f"""CREATE TABLE IF NOT EXISTS audit_log (
        id         {_pk()},
        project_id INTEGER,
        file_id    INTEGER,
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
    """Create tables if needed, migrate older schemas, and seed the admin."""
    _exec(f"""CREATE TABLE IF NOT EXISTS users (
        id            {_pk()},
        username      TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'user',
        must_change   INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    )""")
    _exec("""CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")

    if not _table_exists("files"):
        if _table_exists("projects") and _column_exists("projects", "data"):
            _migrate_projectsv1_to_files()       # projects-as-single-file -> project+file
        elif _table_exists("dataset"):
            _migrate_dataset_to_files()           # very old single-dataset DB
        else:
            _exec(_ddl_projects())
            _exec(_ddl_files())
            _exec(_ddl_feeder_status())
            _exec(_ddl_audit_log())
    else:
        # Already on the current schema — make sure everything exists.
        _exec(_ddl_projects()); _exec(_ddl_files())
        _exec(_ddl_feeder_status()); _exec(_ddl_audit_log())

    if _exec("SELECT COUNT(*) AS n FROM users", fetch="one")["n"] == 0:
        _exec("INSERT INTO users (username, password_hash, role, must_change, created_at)"
              " VALUES (?, ?, 'admin', 0, ?)",
              (DEFAULT_ADMIN, generate_password_hash(DEFAULT_ADMIN_PW), _utcnow()))


def _migrate_projectsv1_to_files():
    """Each old project (which carried one workbook) becomes a project
    container holding exactly one file. Status edits and history are kept."""
    print("Migrating projects into the project+files model…")
    _exec("ALTER TABLE projects RENAME TO projects_old")
    _exec("ALTER TABLE feeder_status RENAME TO feeder_status_old")
    audit_old = _table_exists("audit_log")
    if audit_old:
        _exec("ALTER TABLE audit_log RENAME TO audit_log_old")

    _exec(_ddl_projects()); _exec(_ddl_files())
    _exec(_ddl_feeder_status()); _exec(_ddl_audit_log())

    for r in _exec("SELECT * FROM projects_old ORDER BY id", fetch="all"):
        pid = _insert_returning_id(
            "INSERT INTO projects (name, created_by, created_at) VALUES (?, ?, ?)",
            (r["name"], r.get("created_by"), r.get("created_at") or _utcnow()))
        fid = _insert_returning_id(
            "INSERT INTO files (project_id, name, filename, data, created_by, created_at, revision)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, r["name"], r.get("filename"), _binary(bytes(r["data"])),
             r.get("created_by"), r.get("created_at") or _utcnow(), r.get("revision", 0)))
        _exec("INSERT INTO feeder_status (file_id, sn, status, updated_by, updated_at)"
              " SELECT ?, sn, status, updated_by, updated_at FROM feeder_status_old"
              " WHERE project_id = ?", (fid, r["id"]))
        if audit_old:
            _exec("INSERT INTO audit_log (project_id, file_id, ts, username, action, sn,"
                  " paulos, source, target, old_value, new_value)"
                  " SELECT ?, ?, ts, username, action, sn, paulos, source, target,"
                  " old_value, new_value FROM audit_log_old WHERE project_id = ?",
                  (pid, fid, r["id"]))

    if audit_old:
        # Keep account-level audit entries (no project) too.
        _exec("INSERT INTO audit_log (project_id, file_id, ts, username, action, sn,"
              " paulos, source, target, old_value, new_value)"
              " SELECT NULL, NULL, ts, username, action, sn, paulos, source, target,"
              " old_value, new_value FROM audit_log_old WHERE project_id IS NULL")
        _exec("DROP TABLE audit_log_old")
    _exec("DROP TABLE feeder_status_old")
    _exec("DROP TABLE projects_old")
    print("Migration complete.")


def _migrate_dataset_to_files():
    """Pre-projects DB: the latest dataset becomes one project + one file."""
    print("Migrating legacy single-dataset database…")
    _exec(_ddl_projects()); _exec(_ddl_files())
    fid = pid = None
    last = _exec("SELECT * FROM dataset ORDER BY id DESC LIMIT 1", fetch="one")
    if last:
        name = (last.get("filename") or "Imported project").rsplit(".", 1)[0]
        pid = _insert_returning_id(
            "INSERT INTO projects (name, created_by, created_at) VALUES (?, ?, ?)",
            (name, last.get("uploaded_by"), last.get("uploaded_at") or _utcnow()))
        fid = _insert_returning_id(
            "INSERT INTO files (project_id, name, filename, data, created_by, created_at, revision)"
            " VALUES (?, ?, ?, ?, ?, ?, 0)",
            (pid, name, last.get("filename"), _binary(bytes(last["data"])),
             last.get("uploaded_by"), last.get("uploaded_at") or _utcnow()))

    _exec("ALTER TABLE feeder_status RENAME TO feeder_status_old")
    audit_old = _table_exists("audit_log")
    if audit_old:
        _exec("ALTER TABLE audit_log RENAME TO audit_log_old")
    _exec(_ddl_feeder_status()); _exec(_ddl_audit_log())

    if fid is not None:
        _exec("INSERT INTO feeder_status (file_id, sn, status, updated_by, updated_at)"
              " SELECT ?, sn, status, updated_by, updated_at FROM feeder_status_old", (fid,))
        if audit_old:
            _exec("INSERT INTO audit_log (project_id, file_id, ts, username, action, sn,"
                  " paulos, source, target, old_value, new_value)"
                  " SELECT ?, ?, ts, username, action, sn, paulos, source, target,"
                  " old_value, new_value FROM audit_log_old", (pid, fid))
    _exec("DROP TABLE feeder_status_old")
    if audit_old:
        _exec("DROP TABLE audit_log_old")
    _exec("DROP TABLE dataset")
    print("Migration complete.")


def get_secret_key() -> str:
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
def get_user(username):
    return _exec("SELECT * FROM users WHERE username = ?", (username,), fetch="one")


def verify_login(username, password):
    u = get_user(username)
    if u and check_password_hash(u["password_hash"], password):
        return u
    return None


def list_users():
    return _exec("SELECT id, username, role, must_change, created_at FROM users ORDER BY username",
                 fetch="all")


def create_user(username, password, role):
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES}")
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("username and password are required")
    if _exec("SELECT 1 AS x FROM users WHERE username = ?", (username,), fetch="one"):
        raise ValueError(f"user '{username}' already exists")
    _exec("INSERT INTO users (username, password_hash, role, must_change, created_at)"
          " VALUES (?, ?, ?, 0, ?)",
          (username, generate_password_hash(password), role, _utcnow()))


def set_role(username, role):
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES}")
    _exec("UPDATE users SET role = ? WHERE username = ?", (role, username))


def set_password(username, password):
    if not password:
        raise ValueError("password is required")
    _exec("UPDATE users SET password_hash = ?, must_change = 0 WHERE username = ?",
          (generate_password_hash(password), username))


def delete_user(username):
    _exec("DELETE FROM users WHERE username = ?", (username,))


def count_admins():
    return _exec("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'", fetch="one")["n"]


# ---------------- Projects (containers) ----------------
def list_projects():
    return _exec(
        "SELECT p.id, p.name, p.created_by, p.created_at,"
        " (SELECT COUNT(*) FROM files f WHERE f.project_id = p.id) AS file_count"
        " FROM projects p ORDER BY p.created_at, p.id", fetch="all")


def get_project(pid):
    return _exec("SELECT id, name, created_by, created_at FROM projects WHERE id = ?",
                 (pid,), fetch="one")


def project_exists(pid):
    return _exec("SELECT 1 AS x FROM projects WHERE id = ?", (pid,), fetch="one") is not None


def create_project(name, created_by):
    name = (name or "").strip()
    if not name:
        raise ValueError("project name is required")
    return _insert_returning_id(
        "INSERT INTO projects (name, created_by, created_at) VALUES (?, ?, ?)",
        (name, created_by, _utcnow()))


def rename_project(pid, name):
    name = (name or "").strip()
    if not name:
        raise ValueError("project name is required")
    _exec("UPDATE projects SET name = ? WHERE id = ?", (name, pid))


def delete_project(pid):
    fids = [r["id"] for r in _exec("SELECT id FROM files WHERE project_id = ?", (pid,), fetch="all")]
    for fid in fids:
        _exec("DELETE FROM feeder_status WHERE file_id = ?", (fid,))
    _exec("DELETE FROM files WHERE project_id = ?", (pid,))
    _exec("DELETE FROM audit_log WHERE project_id = ?", (pid,))
    _exec("DELETE FROM projects WHERE id = ?", (pid,))


# ---------------- Files (diagrams within a project) ----------------
def list_files(pid):
    """File metadata for a project (no workbook bytes)."""
    return _exec("SELECT id, project_id, name, filename, created_by, created_at, revision"
                 " FROM files WHERE project_id = ? ORDER BY created_at, id", (pid,), fetch="all")


def get_file(fid):
    """Full file row including the workbook bytes."""
    return _exec("SELECT id, project_id, name, filename, data, created_by, created_at, revision"
                 " FROM files WHERE id = ?", (fid,), fetch="one")


def get_file_meta(fid):
    return _exec("SELECT id, project_id, name, filename, created_by, created_at, revision"
                 " FROM files WHERE id = ?", (fid,), fetch="one")


def create_file(pid, name, filename, data, created_by):
    name = (name or filename or "Untitled").strip()
    return _insert_returning_id(
        "INSERT INTO files (project_id, name, filename, data, created_by, created_at, revision)"
        " VALUES (?, ?, ?, ?, ?, ?, 0)",
        (pid, name, filename, _binary(data), created_by, _utcnow()))


def rename_file(fid, name):
    name = (name or "").strip()
    if not name:
        raise ValueError("file name is required")
    _exec("UPDATE files SET name = ? WHERE id = ?", (name, fid))


def delete_file(fid):
    _exec("DELETE FROM feeder_status WHERE file_id = ?", (fid,))
    _exec("DELETE FROM files WHERE id = ?", (fid,))


def get_revision(fid):
    row = _exec("SELECT revision FROM files WHERE id = ?", (fid,), fetch="one")
    return row["revision"] if row else None


# ---------------- Feeder status (per file) ----------------
def get_status_overrides(fid):
    rows = _exec("SELECT sn, status FROM feeder_status WHERE file_id = ?", (fid,), fetch="all")
    return {r["sn"]: r["status"] for r in rows}


def set_feeder_status(fid, sn, status, updated_by):
    _exec("INSERT INTO feeder_status (file_id, sn, status, updated_by, updated_at)"
          " VALUES (?, ?, ?, ?, ?)"
          " ON CONFLICT(file_id, sn) DO UPDATE SET"
          " status = excluded.status, updated_by = excluded.updated_by,"
          " updated_at = excluded.updated_at",
          (fid, sn, status, updated_by, _utcnow()))
    _exec("UPDATE files SET revision = revision + 1 WHERE id = ?", (fid,))


# ---------------- Audit log ----------------
def log(action, username, project_id=None, file_id=None, **fields):
    _exec("INSERT INTO audit_log (project_id, file_id, ts, username, action, sn, paulos,"
          " source, target, old_value, new_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          (project_id, file_id, _utcnow(), username, action, fields.get("sn"),
           fields.get("paulos"), fields.get("source"), fields.get("target"),
           fields.get("old_value"), fields.get("new_value")))


def recent_audit(project_id=None, file_id=None, limit=300):
    cols = "ts, username, action, sn, paulos, source, target, old_value, new_value"
    if file_id is not None:
        return _exec(f"SELECT {cols} FROM audit_log WHERE file_id = ? ORDER BY id DESC LIMIT ?",
                     (file_id, limit), fetch="all")
    if project_id is not None:
        return _exec(f"SELECT {cols} FROM audit_log WHERE project_id = ? ORDER BY id DESC LIMIT ?",
                     (project_id, limit), fetch="all")
    return _exec(f"SELECT {cols} FROM audit_log ORDER BY id DESC LIMIT ?", (limit,), fetch="all")
