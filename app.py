"""
app.py - Flask web server for the SLD viewer.

Stage 3b adds:
  - POST /api/edge/<sn>/status   change a feeder's status
  - GET  /api/node/<id>          details about one panel (in/out feeders)

The in-memory DATA dict is the single source of truth while the server runs.
When the user exports (Stage 3c), we'll write it back to xlsx.
"""

from flask import Flask, render_template, jsonify, request, abort, send_file
import io
from openpyxl import load_workbook
from pathlib import Path
import sys

import graph as graph_module

# ---- Settings ----
XLSX_FILE = "NHM - Feeders Energization OS.xlsx"
SHEET_NAME = "Energization"

app = Flask(__name__)

# Load the data ONCE at startup; edits live in memory until export.
try:
    DATA = graph_module.load_and_build(XLSX_FILE, SHEET_NAME)
    print(f"Loaded {DATA['stats']['panels']} panels, "
          f"{DATA['stats']['feeders']} feeders.")
except FileNotFoundError:
    print(f"ERROR: can't find '{XLSX_FILE}' in {Path('.').resolve()}")
    sys.exit(1)
except ValueError as e:
    print(f"ERROR loading data: {e}")
    sys.exit(1)


def _edge_by_sn(sn: int):
    """Find the edge dict for a given SN. Returns None if not found."""
    for e in DATA["cytoscape"]["edges"]:
        if e["data"]["sn"] == sn:
            return e
    return None


@app.route("/")
def index():
    return render_template("index.html", stats=DATA["stats"])


@app.route("/api/graph")
def api_graph():
    return jsonify({
        "elements": DATA["cytoscape"],
        "findings": DATA["findings"],
        "stats":    DATA["stats"],
        "statuses": graph_module.VALID_STATUSES,
        "colors":   graph_module.STATUS_COLORS,
    })


@app.route("/api/node/<path:node_id>")
def api_node(node_id):
    """Return the panel's incoming and outgoing feeders."""
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
    # Live statuses (with the user's edits), keyed by SN
    df = DATA["_df"]
    status_by_sn = {int(sn): st for sn, st in zip(df["SN"], df["Site status"])}

    # Re-open the ORIGINAL file so all other sheets, the dropdown,
    # the legend and all formatting stay untouched. We only overwrite
    # the Site status cells that changed.
    wb = load_workbook(XLSX_FILE)
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
