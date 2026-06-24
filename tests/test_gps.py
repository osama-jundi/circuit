"""Tests for optional GPS coordinates and the map-view data."""

import io

from openpyxl import Workbook
from conftest import login


def _xlsx(header, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Energization"
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _project_with(c, h, xlsx_bytes):
    pid = c.post("/api/projects", json={"name": "GPS"}, headers=h).get_json()["id"]
    fid = c.post(f"/api/projects/{pid}/files",
                 data={"file": (io.BytesIO(xlsx_bytes), "f.xlsx")},
                 content_type="multipart/form-data", headers=h).get_json()["id"]
    return pid, fid


def test_file_without_gps_still_works(admin):
    c, h = admin
    data = _xlsx(["SN", "Fed From", "Feed To", "Paulos", "Site status"],
                 [[1, "TX-1", "MDB-1", "P1", "Energized"]])
    pid, fid = _project_with(c, h, data)
    g = c.get(f"/api/files/{fid}/graph").get_json()
    assert g["loaded"] and g["has_gps"] is False
    assert all("lat" not in n["data"] for n in g["elements"]["nodes"])


def test_file_with_gps_exposes_coords(admin):
    c, h = admin
    data = _xlsx(["SN", "Fed From", "Feed To", "Paulos", "Site status", "GPS"],
                 [[1, "TX-1", "MDB-1", "P1", "Energized", "25.2048, 55.2708"],
                  [2, "MDB-1", "DB-1", "P2", "Issued", "25.2055, 55.2710"]])
    pid, fid = _project_with(c, h, data)
    g = c.get(f"/api/files/{fid}/graph").get_json()
    assert g["has_gps"] is True and g["stats"]["located"] == 2
    nodes = {n["data"]["id"]: n["data"] for n in g["elements"]["nodes"]}
    assert nodes["MDB-1"]["lat"] == 25.2048 and nodes["MDB-1"]["lon"] == 55.2708
    # GPS column also kept as an extra detail on the feeder
    e = next(e["data"] for e in g["elements"]["edges"] if e["data"]["sn"] == 1)
    assert "GPS" in e["extra"]


def test_separate_lat_lon_columns(admin):
    c, h = admin
    data = _xlsx(["SN", "Fed From", "Feed To", "Paulos", "Site status", "Latitude", "Longitude"],
                 [[1, "TX-1", "MDB-1", "P1", "Energized", 25.2, 55.3]])
    pid, fid = _project_with(c, h, data)
    g = c.get(f"/api/files/{fid}/graph").get_json()
    assert g["has_gps"] is True
    nodes = {n["data"]["id"]: n["data"] for n in g["elements"]["nodes"]}
    assert nodes["MDB-1"]["lat"] == 25.2 and nodes["MDB-1"]["lon"] == 55.3


def test_invalid_gps_ignored(admin):
    c, h = admin
    data = _xlsx(["SN", "Fed From", "Feed To", "Paulos", "Site status", "Coordinates"],
                 [[1, "TX-1", "MDB-1", "P1", "Energized", "not coords"],
                  [2, "MDB-1", "DB-1", "P2", "Issued", "26.1, 50.5"]])
    pid, fid = _project_with(c, h, data)
    g = c.get(f"/api/files/{fid}/graph").get_json()
    assert g["has_gps"] is True and g["stats"]["located"] == 1
