"""
Shared pytest fixtures.

Each test gets a fresh app bound to a throwaway SQLite database (the modules
are reloaded so app.init_db runs against the temp DB). A small in-memory
workbook with a known structure is used everywhere so assertions are stable.
"""

import importlib
import io

import pytest
from openpyxl import Workbook

# Known feeder set: 6 feeders -> Energized 2, Issued 1, Not Issued 3.
ROWS = [
    (1, "GRID",  "TX-1",  "P1", "Energized"),
    (2, "TX-1",  "MDB-1", "P2", "Energized"),
    (3, "MDB-1", "DB-1",  "P3", "Not Issued"),
    (4, "MDB-1", "DB-2",  "P4", "Issued"),
    (5, "MDB-1", "SML-1", "P5", "Not Issued"),
    (6, "DB-1",  "SML-2", "P6", "Not Issued"),
]
TOTAL_FEEDERS = len(ROWS)


def _make_xlsx():
    wb = Workbook()
    ws = wb.active
    ws.title = "Energization"
    ws.append(["SN", "Fed From", "Feed To", "Paulos", "Site status"])
    for r in ROWS:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


XLSX = _make_xlsx()


@pytest.fixture
def appmod(tmp_path, monkeypatch):
    """Reload the app against a fresh temp SQLite DB."""
    monkeypatch.setenv("CIRCUIT_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    import db as dbmod
    importlib.reload(dbmod)
    import app as app_module
    importlib.reload(app_module)
    return app_module


@pytest.fixture
def client(appmod):
    return appmod.app.test_client()


def login(c, username="admin", password="admin123"):
    """Log in and return headers carrying the session's CSRF token."""
    c.post("/login", data={"username": username, "password": password})
    with c.session_transaction() as s:
        return {"X-CSRFToken": s.get("csrf", "")}


@pytest.fixture
def admin(client):
    """A logged-in admin client plus its CSRF headers."""
    headers = login(client)
    return client, headers


def upload_file(c, headers, pid, name="Diagram"):
    """Upload the fixture workbook into a project; return the file id."""
    return c.post(
        f"/api/projects/{pid}/files",
        data={"file": (io.BytesIO(XLSX), "t.xlsx"), "name": name},
        content_type="multipart/form-data", headers=headers,
    ).get_json()["id"]


def make_project_with_file(c, headers, name="Proj"):
    pid = c.post("/api/projects", json={"name": name}, headers=headers).get_json()["id"]
    fid = upload_file(c, headers, pid)
    return pid, fid
