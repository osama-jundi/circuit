"""
app.py - Flask web server for the SLD viewer.

Multi-project + collaborative:
  - Each uploaded workbook is a **project** (stored in the DB). Everyone can
    see the list of projects and open any of them.
  - Admins can create (upload), rename and delete projects. Any logged-in
    user can edit feeder statuses and export.
  - Status edits are persisted per (project, feeder) and bump the project's
    `revision`, so other people's browsers poll /state and pick up changes
    within a few seconds — multiple users can work the same project live.
  - Every change is audited (who / when / old -> new / which project).

Each project's graph is built lazily from its stored bytes and cached in
PROJECTS; status edits update both the cache and the DB.
"""

import io
import os
from functools import wraps

# Load a local .env file if present (no-op when python-dotenv isn't installed).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import (Flask, render_template, jsonify, request, abort, send_file,
                   session, redirect, url_for)
from openpyxl import load_workbook

import graph as graph_module
import db

SHEET_NAME = "Energization"

app = Flask(__name__)

db.init_db()
app.secret_key = db.get_secret_key()

# --- Session cookie hardening ---
# In production (behind HTTPS, e.g. on Render) set SECURE_COOKIES=1 so the
# session cookie is only sent over HTTPS. HttpOnly is on by default, which
# keeps the cookie away from JavaScript/XSS. SameSite=Lax blocks it on most
# cross-site requests (CSRF mitigation).
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SECURE_COOKIES", "0") == "1",
)

# Lazily-built graph per project: pid -> {"data": graphdict, "raw": bytes,
# "name": str}. Source of truth for the live diagram within this process.
PROJECTS = {}


def _build_from_bytes(raw: bytes):
    """Parse workbook bytes into the graph dict. Raises ValueError on bad data."""
    return graph_module.load_and_build(io.BytesIO(raw), SHEET_NAME)


def _apply_overrides(pid, data):
    """Replay this project's saved status edits on top of a freshly built graph."""
    overrides = db.get_status_overrides(pid)
    if not overrides:
        return
    df = data["_df"]
    for e in data["cytoscape"]["edges"]:
        sn = e["data"]["sn"]
        if sn in overrides:
            st = overrides[sn]
            e["data"]["status"] = st
            e["data"]["color"] = graph_module.STATUS_COLORS.get(st, "#999999")
            df.loc[df["SN"] == sn, "Site status"] = st


def _project(pid):
    """Return the cached {data, raw, name} for a project, building it on first
    use. None if the project doesn't exist."""
    if pid in PROJECTS:
        return PROJECTS[pid]
    row = db.get_project(pid)
    if not row:
        return None
    raw = bytes(row["data"])
    try:
        data = _build_from_bytes(raw)
    except Exception as e:  # noqa: BLE001
        print(f"Could not build project {pid}: {e}")
        return None
    _apply_overrides(pid, data)
    PROJECTS[pid] = {"data": data, "raw": raw, "name": row["name"]}
    return PROJECTS[pid]


def _edge_by_sn(data, sn: int):
    for e in data["cytoscape"]["edges"]:
        if e["data"]["sn"] == sn:
            return e
    return None


# ---------------- Auth helpers ----------------
def current_user():
    return session.get("user")


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not logged in"}), 401
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u:
            return jsonify({"error": "Not logged in"}), 401
        if u.get("role") != "admin":
            return jsonify({"error": "Admin only"}), 403
        return fn(*args, **kwargs)
    return wrapper


# ---------------- Auth routes ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    body = request.form if request.form else (request.get_json(silent=True) or {})
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    u = db.verify_login(username, password)
    if not u:
        return render_template("login.html", error="Invalid username or password."), 401

    session["user"] = {"username": u["username"], "role": u["role"]}
    nxt = request.args.get("next") or url_for("index")
    return redirect(nxt)


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    default_admin = (current_user()["username"] == db.DEFAULT_ADMIN and
                     db.verify_login(db.DEFAULT_ADMIN, "admin123") is not None)
    return render_template("index.html", user=current_user(),
                           default_admin_warning=default_admin)


@app.route("/api/me")
@login_required
def api_me():
    return jsonify(current_user())


# ---------------- Projects ----------------
@app.route("/api/projects", methods=["GET"])
@login_required
def api_projects():
    """List all projects (everyone can see them)."""
    projects = [dict(r) for r in db.list_projects()]
    return jsonify({"projects": projects, "can_manage": current_user()["role"] == "admin"})


@app.route("/api/projects", methods=["POST"])
@admin_required
def api_create_project():
    """Upload a workbook as a new project (admin only)."""
    f = request.files.get("file")
    if f is None or f.filename == "":
        return jsonify({"error": "No file uploaded."}), 400
    if not f.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"error": "Please upload an .xlsx file."}), 400

    raw = f.read()
    try:
        data = _build_from_bytes(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Could not read that file: {e}"}), 400

    name = (request.form.get("name") or "").strip() or f.filename.rsplit(".", 1)[0]
    who = current_user()["username"]
    pid = db.create_project(name, f.filename, raw, who)
    PROJECTS[pid] = {"data": data, "raw": raw, "name": name}
    db.log("upload", who, project_id=pid, paulos=f.filename,
           new_value=f"{data['stats']['panels']} panels, {data['stats']['feeders']} feeders")
    return jsonify({"ok": True, "id": pid, "name": name, "stats": data["stats"]})


@app.route("/api/projects/<int:pid>", methods=["PATCH"])
@admin_required
def api_rename_project(pid):
    if not db.project_exists(pid):
        return jsonify({"error": "No such project"}), 404
    body = request.get_json(silent=True) or {}
    try:
        db.rename_project(pid, body.get("name", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if pid in PROJECTS:
        PROJECTS[pid]["name"] = body["name"].strip()
    db.log("project_rename", current_user()["username"], project_id=pid,
           new_value=body.get("name"))
    return jsonify({"ok": True})


@app.route("/api/projects/<int:pid>", methods=["DELETE"])
@admin_required
def api_delete_project(pid):
    if not db.project_exists(pid):
        return jsonify({"error": "No such project"}), 404
    name = PROJECTS.get(pid, {}).get("name")
    db.delete_project(pid)
    PROJECTS.pop(pid, None)
    db.log("project_delete", current_user()["username"], paulos=name)
    return jsonify({"ok": True})


# ---------------- Per-project graph / data ----------------
@app.route("/api/project/<int:pid>/graph")
@login_required
def api_graph(pid):
    proj = _project(pid)
    if proj is None:
        return jsonify({"loaded": False}), 404
    data = proj["data"]
    return jsonify({
        "loaded":   True,
        "name":     proj["name"],
        "revision": db.get_revision(pid),
        "elements": data["cytoscape"],
        "findings": data["findings"],
        "stats":    data["stats"],
        "statuses": graph_module.VALID_STATUSES,
        "colors":   graph_module.STATUS_COLORS,
    })


@app.route("/api/project/<int:pid>/node/<path:node_id>")
@login_required
def api_node(pid, node_id):
    proj = _project(pid)
    if proj is None:
        abort(404)
    data = proj["data"]
    incoming, outgoing = [], []
    for e in data["cytoscape"]["edges"]:
        d = e["data"]
        if d["target"] == node_id:
            incoming.append(d)
        if d["source"] == node_id:
            outgoing.append(d)
    if not incoming and not outgoing:
        if not any(n["data"]["id"] == node_id for n in data["cytoscape"]["nodes"]):
            abort(404)
    return jsonify({"id": node_id, "incoming": incoming, "outgoing": outgoing})


@app.route("/api/project/<int:pid>/edge/<int:sn>/status", methods=["POST"])
@login_required
def api_set_status(pid, sn):
    """Change one feeder's status (any logged-in user). Persisted + audited,
    and bumps the project revision so others see it on their next poll."""
    proj = _project(pid)
    if proj is None:
        return jsonify({"error": "No such project"}), 404
    body = request.get_json(silent=True) or {}
    new_status = body.get("status")
    if new_status not in graph_module.VALID_STATUSES:
        return jsonify({"error":
            f"Invalid status. Must be one of: {graph_module.VALID_STATUSES}"}), 400

    data = proj["data"]
    edge = _edge_by_sn(data, sn)
    if edge is None:
        return jsonify({"error": f"No feeder with SN {sn}"}), 404

    old_status = edge["data"]["status"]
    edge["data"]["status"] = new_status
    edge["data"]["color"] = graph_module.STATUS_COLORS[new_status]
    data["_df"].loc[data["_df"]["SN"] == sn, "Site status"] = new_status

    who = current_user()["username"]
    db.set_feeder_status(pid, sn, new_status, who)
    if old_status != new_status:
        db.log("status_change", who, project_id=pid, sn=sn,
               paulos=edge["data"].get("paulos"), source=edge["data"].get("source"),
               target=edge["data"].get("target"), old_value=old_status, new_value=new_status)

    return jsonify({
        "ok": True, "sn": sn, "status": new_status,
        "color": graph_module.STATUS_COLORS[new_status],
        "revision": db.get_revision(pid),
    })


@app.route("/api/project/<int:pid>/state")
@login_required
def api_state(pid):
    """Lightweight polling endpoint: the project revision plus the current
    status overrides. Clients apply these when the revision changes."""
    rev = db.get_revision(pid)
    if rev is None:
        return jsonify({"error": "No such project"}), 404
    return jsonify({"revision": rev, "statuses": db.get_status_overrides(pid)})


@app.route("/api/project/<int:pid>/history")
@login_required
def api_history(pid):
    return jsonify({"entries": [dict(r) for r in db.recent_audit(project_id=pid)]})


@app.route("/api/project/<int:pid>/export")
@login_required
def api_export(pid):
    proj = _project(pid)
    if proj is None:
        return jsonify({"error": "No such project"}), 404

    data, raw = proj["data"], proj["raw"]
    df = data["_df"]
    status_by_sn = {int(sn): st for sn, st in zip(df["SN"], df["Site status"])}

    wb = load_workbook(io.BytesIO(raw))
    ws = wb[SHEET_NAME]
    hdr = {ws.cell(row=1, column=c).value: c for c in range(1, ws.max_column + 1)}
    sn_col, st_col = hdr["SN"], hdr["Site status"]
    for r in range(2, ws.max_row + 1):
        sn = ws.cell(row=r, column=sn_col).value
        if sn is not None and int(sn) in status_by_sn:
            ws.cell(row=r, column=st_col).value = status_by_sn[int(sn)]

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    db.log("export", current_user()["username"], project_id=pid)
    safe = (proj["name"] or "project").replace(" ", "_")
    return send_file(
        buf, as_attachment=True,
        download_name=f"{safe}_UPDATED.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------- User management (admin only) ----------------
@app.route("/api/users", methods=["GET"])
@admin_required
def api_users():
    return jsonify({"users": [dict(r) for r in db.list_users()]})


@app.route("/api/users", methods=["POST"])
@admin_required
def api_create_user():
    body = request.get_json(silent=True) or {}
    try:
        db.create_user(body.get("username", ""), body.get("password", ""),
                       body.get("role", "user"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    db.log("user_create", current_user()["username"],
           paulos=body.get("username"), new_value=body.get("role", "user"))
    return jsonify({"ok": True})


@app.route("/api/users/<username>", methods=["PATCH"])
@admin_required
def api_update_user(username):
    body = request.get_json(silent=True) or {}
    me = current_user()["username"]

    if "role" in body:
        target = db.get_user(username)
        if (target and target["role"] == "admin" and body["role"] != "admin"
                and db.count_admins() <= 1):
            return jsonify({"error": "Can't remove the last admin."}), 400
        try:
            db.set_role(username, body["role"])
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        db.log("user_role", me, paulos=username, new_value=body["role"])

    if body.get("password"):
        db.set_password(username, body["password"])
        db.log("user_password", me, paulos=username)

    return jsonify({"ok": True})


@app.route("/api/users/<username>", methods=["DELETE"])
@admin_required
def api_delete_user(username):
    if username == current_user()["username"]:
        return jsonify({"error": "You can't delete your own account."}), 400
    target = db.get_user(username)
    if target and target["role"] == "admin" and db.count_admins() <= 1:
        return jsonify({"error": "Can't delete the last admin."}), 400
    db.delete_user(username)
    db.log("user_delete", current_user()["username"], paulos=username)
    return jsonify({"ok": True})


if __name__ == "__main__":
    # Local dev server. On Render we run via gunicorn (see Procfile), which
    # imports `app` directly and ignores this block.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host=host, port=port, debug=debug)
