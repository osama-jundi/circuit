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
        revision   INTEGER NOT NULL DEFAULT 0,
        summary    TEXT
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


def _ddl_feeder_notes():
    return """CREATE TABLE IF NOT EXISTS feeder_notes (
        file_id    INTEGER NOT NULL,
        sn         INTEGER NOT NULL,
        note       TEXT NOT NULL,
        updated_by TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (file_id, sn)
    )"""


def _ddl_feeder_milestones():
    return """CREATE TABLE IF NOT EXISTS feeder_milestones (
        file_id   INTEGER NOT NULL,
        sn        INTEGER NOT NULL,
        milestone TEXT NOT NULL,
        done_by   TEXT,
        done_at   TEXT NOT NULL,
        PRIMARY KEY (file_id, sn, milestone)
    )"""


def _ddl_feeder_tests():
    return f"""CREATE TABLE IF NOT EXISTS feeder_tests (
        id        {_pk()},
        file_id   INTEGER NOT NULL,
        sn        INTEGER NOT NULL,
        test_type TEXT NOT NULL,
        value     TEXT,
        result    TEXT NOT NULL,
        notes     TEXT,
        tested_by TEXT,
        tested_at TEXT NOT NULL
    )"""


def _ddl_snags():
    return f"""CREATE TABLE IF NOT EXISTS snags (
        id          {_pk()},
        file_id     INTEGER NOT NULL,
        sn          INTEGER NOT NULL,
        description TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'open',
        created_by  TEXT,
        created_at  TEXT NOT NULL,
        closed_by   TEXT,
        closed_at   TEXT
    )"""


def _ddl_project_milestones():
    return f"""CREATE TABLE IF NOT EXISTS project_milestones (
        id         {_pk()},
        project_id INTEGER NOT NULL,
        name       TEXT NOT NULL,
        created_at TEXT NOT NULL
    )"""


def _ddl_feeder_photos():
    return f"""CREATE TABLE IF NOT EXISTS feeder_photos (
        id          {_pk()},
        file_id     INTEGER NOT NULL,
        sn          INTEGER NOT NULL,
        mimetype    TEXT NOT NULL,
        data        {_blob()} NOT NULL,
        caption     TEXT,
        uploaded_by TEXT,
        uploaded_at TEXT NOT NULL
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

    # Additive column for the progress dashboard (safe on existing DBs).
    if not _column_exists("files", "summary"):
        _exec("ALTER TABLE files ADD COLUMN summary TEXT")
    # Per-feeder notes, photos and commissioning records
    # (all safe to create on existing DBs).
    _exec(_ddl_feeder_notes())
    _exec(_ddl_feeder_photos())
    _exec(_ddl_feeder_milestones())
    _exec(_ddl_feeder_tests())
    _exec(_ddl_snags())
    _exec(_ddl_project_milestones())

    if _exec("SELECT COUNT(*) AS n FROM users", fetch="one")["n"] == 0:
        _exec("INSERT INTO users (username, password_hash, role, must_change, created_at)"
              " VALUES (?, ?, 'admin', 0, ?)",
              (DEFAULT_ADMIN, generate_password_hash(DEFAULT_ADMIN_PW), _utcnow()))

    _maybe_reset_admin()


def _maybe_reset_admin():
    """Account recovery for hosts without shell access (e.g. Render free tier).

    When ADMIN_RESET is truthy, force the admin account on boot to match
    ADMIN_USERNAME / ADMIN_PASSWORD: create it if missing, otherwise reset its
    password and ensure it's an admin. Set ADMIN_RESET in the dashboard, log in,
    then remove it so passwords aren't reset on every restart.
    """
    if os.environ.get("ADMIN_RESET", "").strip().lower() not in ("1", "true", "yes", "on"):
        return
    pw_hash = generate_password_hash(DEFAULT_ADMIN_PW)
    if get_user(DEFAULT_ADMIN):
        _exec("UPDATE users SET password_hash = ?, role = 'admin', must_change = 0"
              " WHERE username = ?", (pw_hash, DEFAULT_ADMIN))
        print(f"[ADMIN_RESET] Reset password for admin '{DEFAULT_ADMIN}'. "
              f"Remove ADMIN_RESET now.")
    else:
        _exec("INSERT INTO users (username, password_hash, role, must_change, created_at)"
              " VALUES (?, ?, 'admin', 0, ?)", (DEFAULT_ADMIN, pw_hash, _utcnow()))
        print(f"[ADMIN_RESET] Created admin '{DEFAULT_ADMIN}'. Remove ADMIN_RESET now.")


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
        _purge_file_records(fid)
    _exec("DELETE FROM files WHERE project_id = ?", (pid,))
    _exec("DELETE FROM audit_log WHERE project_id = ?", (pid,))
    _exec("DELETE FROM project_milestones WHERE project_id = ?", (pid,))
    _exec("DELETE FROM projects WHERE id = ?", (pid,))


# ---------------- Custom workflow milestones (per project) ----------------
def get_project_milestones(pid):
    """Admin-added milestone names for a project, in insertion order."""
    return _exec("SELECT id, name FROM project_milestones WHERE project_id = ? ORDER BY id",
                 (pid,), fetch="all")


def add_project_milestone(pid, name):
    name = (name or "").strip()
    if not name:
        raise ValueError("milestone name is required")
    existing = [r["name"].lower() for r in get_project_milestones(pid)]
    if name.lower() in existing:
        raise ValueError("that milestone already exists")
    return _insert_returning_id(
        "INSERT INTO project_milestones (project_id, name, created_at) VALUES (?, ?, ?)",
        (pid, name, _utcnow()))


def delete_project_milestone(mid):
    row = _exec("SELECT project_id, name FROM project_milestones WHERE id = ?", (mid,), fetch="one")
    if not row:
        return
    # Remove any feeder ticks recorded against this custom milestone.
    fids = [r["id"] for r in _exec("SELECT id FROM files WHERE project_id = ?",
                                   (row["project_id"],), fetch="all")]
    for fid in fids:
        _exec("DELETE FROM feeder_milestones WHERE file_id = ? AND milestone = ?",
              (fid, row["name"]))
    _exec("DELETE FROM project_milestones WHERE id = ?", (mid,))


def get_milestone_meta(mid):
    return _exec("SELECT id, project_id, name FROM project_milestones WHERE id = ?",
                 (mid,), fetch="one")


# ---------------- Files (diagrams within a project) ----------------
def list_files(pid):
    """File metadata for a project (no workbook bytes)."""
    return _exec("SELECT id, project_id, name, filename, created_by, created_at, revision, summary"
                 " FROM files WHERE project_id = ? ORDER BY created_at, id", (pid,), fetch="all")


def get_file(fid):
    """Full file row including the workbook bytes."""
    return _exec("SELECT id, project_id, name, filename, data, created_by, created_at, revision, summary"
                 " FROM files WHERE id = ?", (fid,), fetch="one")


def get_file_meta(fid):
    return _exec("SELECT id, project_id, name, filename, created_by, created_at, revision, summary"
                 " FROM files WHERE id = ?", (fid,), fetch="one")


def set_file_summary(fid, summary_json):
    """Store the per-status feeder counts (JSON) used by the dashboard."""
    _exec("UPDATE files SET summary = ? WHERE id = ?", (summary_json, fid))


def all_file_summaries():
    """[{project_id, summary}] across every file — for per-project rollups."""
    return _exec("SELECT project_id, summary FROM files", fetch="all")


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


def _purge_file_records(fid):
    """Remove every per-feeder record attached to a file."""
    for table in ("feeder_status", "feeder_notes", "feeder_photos",
                  "feeder_milestones", "feeder_tests", "snags"):
        _exec(f"DELETE FROM {table} WHERE file_id = ?", (fid,))


def delete_file(fid):
    _purge_file_records(fid)
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


# ---------------- Feeder notes (per file) ----------------
def get_notes(fid):
    rows = _exec("SELECT sn, note, updated_by, updated_at FROM feeder_notes WHERE file_id = ?",
                 (fid,), fetch="all")
    return {r["sn"]: {"note": r["note"], "by": r["updated_by"], "at": r["updated_at"]} for r in rows}


def set_note(fid, sn, note, updated_by):
    """Set a feeder note, or delete it when the note is blank."""
    note = (note or "").strip()
    if not note:
        _exec("DELETE FROM feeder_notes WHERE file_id = ? AND sn = ?", (fid, sn))
        return
    _exec("INSERT INTO feeder_notes (file_id, sn, note, updated_by, updated_at)"
          " VALUES (?, ?, ?, ?, ?)"
          " ON CONFLICT(file_id, sn) DO UPDATE SET"
          " note = excluded.note, updated_by = excluded.updated_by,"
          " updated_at = excluded.updated_at",
          (fid, sn, note, updated_by, _utcnow()))


# ---------------- Feeder photos (per file) ----------------
def add_photo(file_id, sn, mimetype, data, caption, uploaded_by):
    return _insert_returning_id(
        "INSERT INTO feeder_photos (file_id, sn, mimetype, data, caption, uploaded_by, uploaded_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (file_id, sn, mimetype, _binary(data), (caption or "").strip() or None,
         uploaded_by, _utcnow()))


def count_photos(file_id, sn):
    return _exec("SELECT COUNT(*) AS n FROM feeder_photos WHERE file_id = ? AND sn = ?",
                 (file_id, sn), fetch="one")["n"]


def list_photos(file_id):
    """Photo metadata for a whole file, grouped by feeder SN (no image bytes)."""
    rows = _exec("SELECT id, sn, caption, uploaded_by, uploaded_at FROM feeder_photos"
                 " WHERE file_id = ? ORDER BY id", (file_id,), fetch="all")
    out = {}
    for r in rows:
        out.setdefault(r["sn"], []).append(
            {"id": r["id"], "caption": r["caption"], "by": r["uploaded_by"], "at": r["uploaded_at"]})
    return out


def get_photo(photo_id):
    """Full photo row including image bytes (for serving)."""
    return _exec("SELECT id, file_id, sn, mimetype, data, uploaded_by FROM feeder_photos"
                 " WHERE id = ?", (photo_id,), fetch="one")


def get_photo_meta(photo_id):
    return _exec("SELECT id, file_id, sn, caption, uploaded_by FROM feeder_photos"
                 " WHERE id = ?", (photo_id,), fetch="one")


def delete_photo(photo_id):
    _exec("DELETE FROM feeder_photos WHERE id = ?", (photo_id,))


# ---------------- Commissioning milestones (per file) ----------------
def get_milestones(file_id):
    """{sn: {milestone: {by, at}}} for a whole file."""
    rows = _exec("SELECT sn, milestone, done_by, done_at FROM feeder_milestones"
                 " WHERE file_id = ?", (file_id,), fetch="all")
    out = {}
    for r in rows:
        out.setdefault(r["sn"], {})[r["milestone"]] = {"by": r["done_by"], "at": r["done_at"]}
    return out


def set_milestone(file_id, sn, milestone, done, by):
    """Mark a milestone done (records who/when) or clear it."""
    if not done:
        _exec("DELETE FROM feeder_milestones WHERE file_id = ? AND sn = ? AND milestone = ?",
              (file_id, sn, milestone))
        return
    _exec("INSERT INTO feeder_milestones (file_id, sn, milestone, done_by, done_at)"
          " VALUES (?, ?, ?, ?, ?)"
          " ON CONFLICT(file_id, sn, milestone) DO UPDATE SET"
          " done_by = excluded.done_by, done_at = excluded.done_at",
          (file_id, sn, milestone, by, _utcnow()))


def milestone_counts(file_id):
    """{milestone: n_done} across the file, for dashboard/report rollups."""
    rows = _exec("SELECT milestone, COUNT(*) AS n FROM feeder_milestones"
                 " WHERE file_id = ? GROUP BY milestone", (file_id,), fetch="all")
    return {r["milestone"]: r["n"] for r in rows}


# ---------------- Test records (per file) ----------------
def get_tests(file_id):
    """{sn: [test rows]} newest first."""
    rows = _exec("SELECT id, sn, test_type, value, result, notes, tested_by, tested_at"
                 " FROM feeder_tests WHERE file_id = ? ORDER BY id DESC", (file_id,), fetch="all")
    out = {}
    for r in rows:
        out.setdefault(r["sn"], []).append(dict(r))
    return out


def add_test(file_id, sn, test_type, value, result, notes, by):
    return _insert_returning_id(
        "INSERT INTO feeder_tests (file_id, sn, test_type, value, result, notes, tested_by, tested_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (file_id, sn, test_type, (value or "").strip() or None, result,
         (notes or "").strip() or None, by, _utcnow()))


def get_test_meta(test_id):
    return _exec("SELECT id, file_id, sn, tested_by FROM feeder_tests WHERE id = ?",
                 (test_id,), fetch="one")


def delete_test(test_id):
    _exec("DELETE FROM feeder_tests WHERE id = ?", (test_id,))


def test_counts(file_id):
    row = _exec("SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN result = 'Pass' THEN 1 ELSE 0 END) AS passed"
                " FROM feeder_tests WHERE file_id = ?", (file_id,), fetch="one")
    return {"total": row["total"] or 0, "passed": row["passed"] or 0}


# ---------------- Punch list / snags (per file) ----------------
def get_snags(file_id):
    """All snags for a file, open first then newest."""
    return _exec("SELECT id, sn, description, status, created_by, created_at, closed_by, closed_at"
                 " FROM snags WHERE file_id = ?"
                 " ORDER BY CASE WHEN status = 'open' THEN 0 ELSE 1 END, id DESC",
                 (file_id,), fetch="all")


def add_snag(file_id, sn, description, by):
    description = (description or "").strip()
    if not description:
        raise ValueError("snag description is required")
    return _insert_returning_id(
        "INSERT INTO snags (file_id, sn, description, status, created_by, created_at)"
        " VALUES (?, ?, ?, 'open', ?, ?)",
        (file_id, sn, description, by, _utcnow()))


def get_snag(snag_id):
    return _exec("SELECT id, file_id, sn, description, status, created_by FROM snags"
                 " WHERE id = ?", (snag_id,), fetch="one")


def set_snag_status(snag_id, status, by):
    if status == "closed":
        _exec("UPDATE snags SET status = 'closed', closed_by = ?, closed_at = ? WHERE id = ?",
              (by, _utcnow(), snag_id))
    else:
        _exec("UPDATE snags SET status = 'open', closed_by = NULL, closed_at = NULL WHERE id = ?",
              (snag_id,))


def snag_counts(file_id):
    row = _exec("SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_n"
                " FROM snags WHERE file_id = ?", (file_id,), fetch="one")
    return {"total": row["total"] or 0, "open": row["open_n"] or 0}


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
