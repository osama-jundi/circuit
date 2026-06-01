"""Tests for the database upgrade paths in db.init_db()."""

import importlib
import sqlite3

from werkzeug.security import generate_password_hash

from conftest import XLSX


def _reload_db(path, monkeypatch):
    monkeypatch.setenv("CIRCUIT_DB", str(path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import db as dbmod
    importlib.reload(dbmod)
    return dbmod


def test_migrate_legacy_dataset(tmp_path, monkeypatch):
    """Oldest schema: a single `dataset` table becomes one project + file."""
    p = tmp_path / "legacy.db"
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE,
            password_hash TEXT, role TEXT, must_change INTEGER DEFAULT 0, created_at TEXT);
        CREATE TABLE dataset (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, data BLOB,
            uploaded_by TEXT, uploaded_at TEXT);
        CREATE TABLE feeder_status (sn INTEGER PRIMARY KEY, status TEXT, updated_by TEXT, updated_at TEXT);
        CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, username TEXT,
            action TEXT, sn INTEGER, paulos TEXT, source TEXT, target TEXT, old_value TEXT, new_value TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    c.execute("INSERT INTO users (username,password_hash,role,created_at) VALUES ('admin',?,'admin','x')",
              (generate_password_hash("admin123"),))
    c.execute("INSERT INTO dataset (filename,data,uploaded_by,uploaded_at) VALUES ('NHM.xlsx',?, 'admin','x')",
              (XLSX,))
    c.execute("INSERT INTO feeder_status (sn,status,updated_by,updated_at) VALUES (3,'Energized','admin','x')")
    c.commit(); c.close()

    db = _reload_db(p, monkeypatch)
    db.init_db()
    projects = db.list_projects()
    assert len(projects) == 1
    files = db.list_files(projects[0]["id"])
    assert len(files) == 1
    assert db.get_status_overrides(files[0]["id"])[3] == "Energized"
    assert db._table_exists("feeder_notes")


def test_migrate_projects_with_data(tmp_path, monkeypatch):
    """Previous schema: projects carried the workbook; becomes project + file."""
    p = tmp_path / "v1.db"
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE,
            password_hash TEXT, role TEXT, must_change INTEGER DEFAULT 0, created_at TEXT);
        CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, filename TEXT,
            data BLOB, created_by TEXT, created_at TEXT, revision INTEGER DEFAULT 0);
        CREATE TABLE feeder_status (project_id INTEGER, sn INTEGER, status TEXT, updated_by TEXT,
            updated_at TEXT, PRIMARY KEY(project_id, sn));
        CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, ts TEXT,
            username TEXT, action TEXT, sn INTEGER, paulos TEXT, source TEXT, target TEXT,
            old_value TEXT, new_value TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    c.execute("INSERT INTO users (username,password_hash,role,created_at) VALUES ('admin',?,'admin','x')",
              (generate_password_hash("admin123"),))
    c.execute("INSERT INTO projects (name,filename,data,created_by,created_at,revision)"
              " VALUES ('NHM Site','NHM.xlsx',?, 'admin','x',2)", (XLSX,))
    c.execute("INSERT INTO feeder_status (project_id,sn,status,updated_by,updated_at)"
              " VALUES (1,4,'Energized','admin','x')")
    c.commit(); c.close()

    db = _reload_db(p, monkeypatch)
    db.init_db()
    projects = db.list_projects()
    assert len(projects) == 1 and projects[0]["name"] == "NHM Site"
    files = db.list_files(projects[0]["id"])
    assert len(files) == 1
    assert db.get_status_overrides(files[0]["id"])[4] == "Energized"
    # idempotent
    db.init_db()
    assert len(db.list_projects()) == 1
