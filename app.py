"""
app.py - Flask web server for the SLD viewer.

The xlsx is now provided by the user through an **Upload** button in the
website (POST /api/upload). Nothing is read from disk automatically anymore,
though if a default file happens to sit next to this script we'll load it
once at startup as a convenience.

The in-memory DATA dict is the single source of truth while the server runs.
RAW_BYTES holds the bytes of the last uploaded workbook so /api/export can
re-open the original file (keeping every other sheet / formatting intact)
and only overwrite the changed status cells.
"""

from flask import Flask, render_template, jsonify, request, abort, send_file
import io
from openpyxl import load_workbook
from pathlib import Path

import graph as graph_module

# ---- Settings ----
# Optional convenience: if this file is present next to app.py we load it at
# startup. Otherwise the user just uploads one from the website.
XLSX_FILE = "NHM - Feeders Energization OS.xlsx"
SHEET_NAME = "Energization"

app = Flask(__name__)

# These start empty; an upload (or the optional default file) fills them in.
DATA = None        # the built graph dict (see graph.load_and_build)
RAW_BYTES = None   # raw bytes of the workbook, used for export


def _build_from_bytes(raw: bytes):
    """Parse workbook bytes into the DATA dict. Raises ValueError on bad data."""
    return graph_module.load_and_build(io.BytesIO(raw), SHEET_NAME)


def _try_load_default():
    """Load the bundled xlsx if it exists. Never fatal."""
    global DATA, RAW_BYTES
    p = Path(XLSX_FILE)
    if not p.exists():
        print(f"No default file at {p.resolve()} — waiting for an upload.")
        return
    try:
        RAW_BYTES = p.read_bytes()
        DATA = _build_from_bytes(RAW_BYTES)
        print(f"Loaded default: {DATA['stats']['panels']} panels, "
              f"{DATA['stats']['feeders']} feeders.")
    except Exception as e:  # noqa: BLE001 - default load is best-effort
        print(f"Could not load default file ({e}); waiting for an upload.")
        DATA, RAW_BYTES = None, None


_try_load_default()


def _edge_by_sn(sn: int):
    """Find the edge dict for a given SN. Returns None if not found."""
    if DATA is None:
        return None
    for e in DATA["cytoscape"]["edges"]:
        if e["data"]["sn"] == sn:
            return e
    return None


@app.route("/")
def index():
    stats = DATA["stats"] if DATA else None
    return render_template("index.html", stats=stats)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Accept an .xlsx upload and (re)build the in-memory graph."""
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
    except Exception as e:  # noqa: BLE001 - report any parse failure to the user
        return jsonify({"error": f"Could not read that file: {e}"}), 400

    DATA, RAW_BYTES = data, raw
    return jsonify({"ok": True, "stats": DATA["stats"]})


@app.route("/api/graph")
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
def api_node(node_id):
    """Return the panel's incoming and outgoing feeders."""
    if DATA is None:
        abort(404)
    incoming = []
    outgoing = []
    for e in DATA["cytoscape"]["edges"]:
        d = e["data"]
        if d["target"] == node_id:
            incoming.append(d)
        if d["source"] == node_id:
            outgoing.append(d)

    if not incoming and not outgoing:
        # Make sure the node actually exists before returning empty
        if not any(n["data"]["id"] == node_id
                   for n in DATA["cytoscape"]["nodes"]):
            abort(404)

    return jsonify({
        "id":       node_id,
        "incoming": incoming,
        "outgoing": outgoing,
    })


@app.route("/api/edge/<int:sn>/status", methods=["POST"])
def api_set_status(sn):
    """Change the status of one feeder.
    Body: {"status": "Energized" | "Issued" | "Not Issued"}
    """
    if DATA is None:
        return jsonify({"error": "No data loaded. Upload a file first."}), 409
    body = request.get_json(silent=True) or {}
    new_status = body.get("status")
    if new_status not in graph_module.VALID_STATUSES:
        return jsonify({"error":
            f"Invalid status. Must be one of: {graph_module.VALID_STATUSES}"
        }), 400

    edge = _edge_by_sn(sn)
    if edge is None:
        return jsonify({"error": f"No feeder with SN {sn}"}), 404

    edge["data"]["status"] = new_status
    edge["data"]["color"]  = graph_module.STATUS_COLORS[new_status]

    # Update the in-memory DataFrame too so export reflects this change
    df = DATA["_df"]
    df.loc[df["SN"] == sn, "Site status"] = new_status

    return jsonify({
        "ok":     True,
        "sn":     sn,
        "status": new_status,
        "color":  graph_module.STATUS_COLORS[new_status],
    })


@app.route("/api/export")
def api_export():
    if DATA is None or RAW_BYTES is None:
        return jsonify({"error": "No data loaded. Upload a file first."}), 409

    # Live statuses (with the user's edits), keyed by SN
    df = DATA["_df"]
    status_by_sn = {int(sn): st for sn, st in zip(df["SN"], df["Site status"])}

    # Re-open the ORIGINAL uploaded bytes so all other sheets, the dropdown,
    # the legend and all formatting stay untouched. We only overwrite
    # the Site status cells that changed.
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
    return send_file(
        buf,
        as_attachment=True,
        download_name="NHM_Feeders_Energization_UPDATED.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
