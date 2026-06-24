"""Tests for the commissioning layer: extra columns, workflow milestones,
test records, the punch list, and the progress report."""

import io

from conftest import XLSX_RICH, login, make_project_with_file


def _rich_file(c, h):
    pid = c.post("/api/projects", json={"name": "Comm"}, headers=h).get_json()["id"]
    fid = c.post(f"/api/projects/{pid}/files",
                 data={"file": (io.BytesIO(XLSX_RICH), "rich.xlsx")},
                 content_type="multipart/form-data", headers=h).get_json()["id"]
    return pid, fid


def test_extra_columns_surface_in_graph(admin):
    c, h = admin
    pid, fid = _rich_file(c, h)
    g = c.get(f"/api/files/{fid}/graph").get_json()
    e = g["elements"]["edges"][0]["data"]
    assert e["extra"]["Cable Size"] == "4Cx95mm2"
    assert e["extra"]["Breaker"] == "MCCB 250A"


def test_workflow_milestones(admin):
    c, h = admin
    pid, fid = make_project_with_file(c, h)
    wf = c.get(f"/api/files/{fid}/workflow").get_json()
    assert "Energized" in wf["milestones"] and wf["done"] == {}
    assert c.post(f"/api/files/{fid}/edge/3/milestone",
                  json={"milestone": "Tested", "done": True}, headers=h).get_json()["ok"]
    wf = c.get(f"/api/files/{fid}/workflow").get_json()
    assert wf["done"]["3"]["Tested"]["by"] == "admin"
    # un-tick
    c.post(f"/api/files/{fid}/edge/3/milestone",
           json={"milestone": "Tested", "done": False}, headers=h)
    assert c.get(f"/api/files/{fid}/workflow").get_json()["done"] == {}
    # invalid milestone rejected
    assert c.post(f"/api/files/{fid}/edge/3/milestone",
                  json={"milestone": "Nope", "done": True}, headers=h).status_code == 400


def test_test_records(admin):
    c, h = admin
    pid, fid = make_project_with_file(c, h)
    r = c.post(f"/api/files/{fid}/edge/3/tests",
               json={"test_type": "Insulation Resistance (Megger)", "value": "250 MΩ",
                     "result": "Pass"}, headers=h).get_json()
    assert r["ok"]
    tid = r["id"]
    tests = c.get(f"/api/files/{fid}/tests").get_json()["tests"]
    assert tests["3"][0]["result"] == "Pass" and tests["3"][0]["value"] == "250 MΩ"
    # bad result rejected
    assert c.post(f"/api/files/{fid}/edge/3/tests",
                  json={"test_type": "x", "result": "Maybe"}, headers=h).status_code == 400
    # delete
    assert c.delete(f"/api/tests/{tid}", headers=h).get_json()["ok"]
    assert c.get(f"/api/files/{fid}/tests").get_json()["tests"] == {}


def test_snags_open_and_close(admin):
    c, h = admin
    pid, fid = make_project_with_file(c, h)
    sid = c.post(f"/api/files/{fid}/edge/3/snags",
                 json={"description": "Gland plate missing"}, headers=h).get_json()["id"]
    snags = c.get(f"/api/files/{fid}/snags").get_json()["snags"]
    assert snags[0]["status"] == "open" and snags[0]["description"] == "Gland plate missing"
    assert c.patch(f"/api/snags/{sid}", json={"status": "closed"}, headers=h).get_json()["ok"]
    assert c.get(f"/api/files/{fid}/snags").get_json()["snags"][0]["status"] == "closed"
    # empty description rejected
    assert c.post(f"/api/files/{fid}/edge/3/snags",
                  json={"description": "  "}, headers=h).status_code == 400


def test_custom_milestones(admin, appmod):
    c, h = admin
    pid, fid = make_project_with_file(c, h)
    # defaults present, no custom yet
    wf = c.get(f"/api/files/{fid}/workflow").get_json()
    assert wf["milestones"][:6] == ["Cable Pulled", "Glanded", "Terminated",
                                    "Tested", "Energized", "Commissioned"]
    assert wf["custom"] == [] and wf["can_manage"] is True
    # admin adds a custom stage
    mid = c.post(f"/api/projects/{pid}/milestones", json={"name": "SAT Witnessed"},
                 headers=h).get_json()["id"]
    wf = c.get(f"/api/files/{fid}/workflow").get_json()
    assert wf["milestones"][-1] == "SAT Witnessed"
    # it can be ticked on a feeder
    assert c.post(f"/api/files/{fid}/edge/3/milestone",
                  json={"milestone": "SAT Witnessed", "done": True}, headers=h).get_json()["ok"]
    assert c.get(f"/api/files/{fid}/workflow").get_json()["done"]["3"]["SAT Witnessed"]["by"] == "admin"
    # duplicate rejected
    assert c.post(f"/api/projects/{pid}/milestones", json={"name": "sat witnessed"},
                  headers=h).status_code == 400
    # non-admin can't add
    c.post("/api/users", json={"username": "bob", "password": "pw", "role": "user"}, headers=h)
    bob = appmod.app.test_client()
    bh = login(bob, "bob", "pw")
    assert bob.post(f"/api/projects/{pid}/milestones", json={"name": "x"}, headers=bh).status_code == 403
    # admin removes it -> gone from list and ticks cleared
    assert c.delete(f"/api/milestones/{mid}", headers=h).get_json()["ok"]
    wf = c.get(f"/api/files/{fid}/workflow").get_json()
    assert "SAT Witnessed" not in wf["milestones"]
    assert "3" not in wf["done"] or "SAT Witnessed" not in wf["done"].get("3", {})


def test_custom_test_type(admin):
    c, h = admin
    pid, fid = make_project_with_file(c, h)
    # a free-text custom test type is accepted and stored
    r = c.post(f"/api/files/{fid}/edge/3/tests",
               json={"test_type": "Polarity", "value": "OK", "result": "Pass"}, headers=h)
    assert r.status_code == 200
    tests = c.get(f"/api/files/{fid}/tests").get_json()["tests"]
    assert tests["3"][0]["test_type"] == "Polarity"


def test_report_renders(admin):
    c, h = admin
    pid, fid = _rich_file(c, h)
    c.post(f"/api/files/{fid}/edge/3/snags", json={"description": "Open item"}, headers=h)
    r = c.get(f"/project/{pid}/report")
    assert r.status_code == 200
    body = r.data
    assert b"Progress Report" in body and b"Open item" in body and b"Switchboard" in body


def test_commissioning_records_csrf_protected(admin):
    c, h = admin
    pid, fid = make_project_with_file(c, h)
    # no CSRF header -> blocked
    assert c.post(f"/api/files/{fid}/edge/3/milestone",
                  json={"milestone": "Tested", "done": True}).status_code == 400
    assert c.post(f"/api/files/{fid}/edge/3/snags",
                  json={"description": "x"}).status_code == 400
