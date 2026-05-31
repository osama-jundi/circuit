"""
app.py - Flask web server for the SLD viewer.

Now with accounts, roles and a full audit trail:
  - Login required for everything (Flask session).
  - role 'admin' : can upload a new workbook and manage users.
  - role 'user'  : can edit feeder statuses and export, but NOT upload.
  - Every status change and upload is written to an audit log (who / when /
    old -> new) and every change is persisted in SQLite, so edits survive a
    server restart.

The uploaded workbook and the per-feeder status overrides live in the
database (see db.py). On startup we rebuild the in-memory graph (DATA) from
the latest stored workbook and replay the saved status overrides on top.
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

# In-memory graph rebuilt from the DB. None until a workbook is stored.
DATA = None
RAW_BYTES = None


def _build_from_bytes(raw: bytes):
    """Parse workbook bytes into the DATA dict. Raises ValueError on bad data."""
    return graph_module.load_and_build(io.BytesIO(raw), SHEET_NAME)


def _apply_overrides(data):
    """Replay the saved per-feeder status edits on top of a freshly built graph."""
    overrides = db.get_status_overrides()
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


def _load_from_db():
    """Rebuild DATA / RAW_BYTES from the stored workbook (if any)."""
    global DATA, RAW_BYTES
    ds = db.latest_dataset()
    if not ds:
        DATA, RAW_BYTES = None, None
        print("No dataset stored yet — an admin needs to upload one.")
        return
    _filename, raw = ds
    try:
        data = _build_from_bytes(raw)
        _apply_overrides(data)
        DATA, RAW_BYTES = data, raw
        print(f"Loaded stored dataset: {DATA['stats']['panels']} panels, "
              f"{DATA['stats']['feeders']} feeders.")
    except Exception as e:  # noqa: BLE001 - stored file should be valid, but be safe
        print(f"Could not rebuild stored dataset ({e}).")
        DATA, RAW_BYTES = None, None


_load_from_db()


# ---------------- Auth helpers ----------------
def current_user():
    return session.get("user")


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            # API calls get JSON 401; page requests get redirected to login.
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


def _edge_by_sn(sn: int):
    if DATA is None:
        return None
    for e in DATA["cytoscape"]["edges"]:
        if e["data"]["sn"] == sn:
            return e
    return None


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
    stats = DATA["stats"] if DATA else None
    # Warn only when the seeded admin still uses the well-known insecure
    # password "admin123". A custom ADMIN_PASSWORD set via env is fine.
    default_admin = (current_user()["username"] == db.DEFAULT_ADMIN and
                     db.verify_login(db.DEFAULT_ADMIN, "admin123") is not None)
    return render_template("index.html", stats=stats, user=current_user(),
                           default_admin_warning=default_admin)


# ---------------- Graph / data API ----------------
@app.route("/api/me")
@login_required
def api_me():
    return jsonify(current_user())


@app.route("/api/upload", methods=["POST"])
@admin_required
def api_upload():
    """Accept an .xlsx upload (admin only) and rebuild the graph."""
    global DATA, RAW_BYTES
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

    who = current_user()["username"]
    db.save_dataset(f.filename, raw, who)   # also clears old status overrides
    db.log("upload", who, paulos=f.filename,
           new_value=f"{data['stats']['panels']} panels, {data['stats']['feeders']} feeders")
    DATA, RAW_BYTES = data, raw
    return jsonify({"ok": True, "stats": DATA["stats"]})


@app.route("/api/graph")
@login_required
def api_graph():
    if DATA is None:
        return jsonify({"loaded": False})
    return jsonify({
        "loaded":   True,
        "elements": DATA["cytoscape"],
        "findings": DATA["findings"],
        "stats":    DATA["stats"],
        "statuses": graph_module.VALID_STATUSES,
        "colors":   graph_module.STATUS_COLORS,
    })


@app.route("/api/node/<path:node_id>")
@login_required
def api_node(node_id):
    if DATA is None:
        abort(404)
    incoming, outgoing = [], []
    for e in DATA["cytoscape"]["edges"]:
        d = e["data"]
        if d["target"] == node_id:
            incoming.append(d)
        if d["source"] == node_id:
            outgoing.append(d)

    if not incoming and not outgoing:
        if not any(n["data"]["id"] == node_id for n in DATA["cytoscape"]["nodes"]):
            abort(404)

    return jsonify({"id": node_id, "incoming": incoming, "outgoing": outgoing})


@app.route("/api/edge/<int:sn>/status", methods=["POST"])
@login_required
def api_set_status(sn):
    """Change one feeder's status (any logged-in user). Persisted + audited."""
    if DATA is None:
        return jsonify({"error": "No data loaded. Ask an admin to upload a file."}), 409
    body = request.get_json(silent=True) or {}
    new_status = body.get("status")
    if new_status not in graph_module.VALID_STATUSES:
        return jsonify({"error":
            f"Invalid status. Must be one of: {graph_module.VALID_STATUSES}"}), 400

    edge = _edge_by_sn(sn)
    if edge is None:
        return jsonify({"error": f"No feeder with SN {sn}"}), 404

    old_status = edge["data"]["status"]
    edge["data"]["status"] = new_status
    edge["data"]["color"] = graph_module.STATUS_COLORS[new_status]
    DATA["_df"].loc[DATA["_df"]["SN"] == sn, "Site status"] = new_status

    who = current_user()["username"]
    db.set_feeder_status(sn, new_status, who)
    if old_status != new_status:
        db.log("status_change", who, sn=sn, paulos=edge["data"].get("paulos"),
               source=edge["data"].get("source"), target=edge["data"].get("target"),
               old_value=old_status, new_value=new_status)

    return jsonify({
        "ok": True, "sn": sn, "status": new_status,
        "color": graph_module.STATUS_COLORS[new_status],
    })


@app.route("/api/export")
@login_required
def api_export():
    if DATA is None or RAW_BYTES is None:
        return jsonify({"error": "No data loaded."}), 409

    df = DATA["_df"]
    status_by_sn = {int(sn): st for sn, st in zip(df["SN"], df["Site status"])}

    wb = load_workbook(io.BytesIO(RAW_BYTES))
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
    db.log("export", current_user()["username"])
    return send_file(
        buf, as_attachment=True,
        download_name="NHM_Feeders_Energization_UPDATED.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------- Audit history (any logged-in user) ----------------
@app.route("/api/history")
@login_required
def api_history():
    rows = db.recent_audit(300)
    return jsonify({"entries": [dict(r) for r in rows]})


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
        # Don't let the last admin demote themselves into a lockout.
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
