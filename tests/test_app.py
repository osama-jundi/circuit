"""End-to-end tests for the SLD app (auth, projects, files, statuses,
notes, bulk edit, dashboard, and security)."""

import io
import json

from conftest import XLSX, ROWS, TOTAL_FEEDERS, login, upload_file, make_project_with_file


# ---------------- Auth ----------------
def test_health_is_public(client):
    assert client.get("/healthz").status_code == 200


def test_pages_require_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (301, 302) and "/login" in r.headers["Location"]
    assert client.get("/api/projects").status_code == 401


def test_login_good_and_bad(client):
    assert client.post("/login", data={"username": "admin", "password": "nope"}).status_code == 401
    assert client.post("/login", data={"username": "admin", "password": "admin123"}).status_code in (301, 302)


def test_login_rate_limit(client):
    for _ in range(5):
        client.post("/login", data={"username": "admin", "password": "x"})
    # locked out even with the correct password
    assert client.post("/login", data={"username": "admin", "password": "admin123"}).status_code == 429


def test_logout_clears_session(admin):
    c, _ = admin
    c.get("/logout")
    assert c.get("/api/projects").status_code == 401


# ---------------- CSRF ----------------
def test_csrf_required_for_writes(admin):
    c, headers = admin
    assert c.post("/api/projects", json={"name": "NoTok"}).status_code == 400
    assert c.post("/api/projects", json={"name": "Tok"}, headers=headers).status_code == 200


# ---------------- Projects & roles ----------------
def test_project_crud(admin):
    c, h = admin
    pid = c.post("/api/projects", json={"name": "Site A"}, headers=h).get_json()["id"]
    assert any(p["name"] == "Site A" for p in c.get("/api/projects").get_json()["projects"])
    assert c.patch(f"/api/projects/{pid}", json={"name": "Site B"}, headers=h).get_json()["ok"]
    assert c.get(f"/project/{pid}").status_code == 200
    assert c.delete(f"/api/projects/{pid}", headers=h).get_json()["ok"]
    assert c.get("/api/projects").get_json()["projects"] == []


def test_user_role_enforcement(admin, client, appmod):
    c, h = admin
    pid = c.post("/api/projects", json={"name": "P"}, headers=h).get_json()["id"]
    c.post("/api/users", json={"username": "bob", "password": "pw", "role": "user"}, headers=h)

    bob = appmod.app.test_client()
    bh = login(bob, "bob", "pw")
    # bob can view but not create/upload/delete
    assert bob.get("/api/projects").get_json()["can_manage"] is False
    assert bob.post("/api/projects", json={"name": "x"}, headers=bh).status_code == 403
    assert bob.post(f"/api/projects/{pid}/files", data={"file": (io.BytesIO(XLSX), "x.xlsx")},
                    content_type="multipart/form-data", headers=bh).status_code == 403


def test_last_admin_protected(admin):
    c, h = admin
    assert c.patch("/api/users/admin", json={"role": "user"}, headers=h).status_code == 400
    assert c.delete("/api/users/admin", headers=h).status_code == 400


# ---------------- Files & statuses ----------------
def test_upload_and_graph(admin):
    c, h = admin
    pid, fid = make_project_with_file(c, h)
    g = c.get(f"/api/files/{fid}/graph").get_json()
    assert g["loaded"] and len(g["elements"]["edges"]) == TOTAL_FEEDERS


def test_status_change_persists_and_isolates(admin, appmod):
    c, h = admin
    pid, fid = make_project_with_file(c, h)
    pid2, fid2 = make_project_with_file(c, h, name="Other")
    r = c.post(f"/api/files/{fid}/edge/3/status", json={"status": "Energized"}, headers=h).get_json()
    assert r["ok"]
    assert c.get(f"/api/files/{fid}/state").get_json()["statuses"]["3"] == "Energized"
    # other file unaffected
    assert c.get(f"/api/files/{fid2}/state").get_json()["statuses"] == {}
    # survives cache eviction (simulated restart)
    appmod.FILES.clear()
    r2 = c.get(f"/api/files/{fid}/graph").get_json()
    assert any(e["data"]["sn"] == 3 and e["data"]["status"] == "Energized" for e in r2["elements"]["edges"])


def test_bulk_status(admin):
    c, h = admin
    pid, fid = make_project_with_file(c, h)
    res = c.post(f"/api/files/{fid}/bulk-status", json={"sns": [3, 4, 5], "status": "Energized"},
                 headers=h).get_json()
    assert res["ok"]
    st = c.get(f"/api/files/{fid}/state").get_json()["statuses"]
    assert st["3"] == st["4"] == st["5"] == "Energized"


def test_dashboard_summary(admin):
    c, h = admin
    pid, fid = make_project_with_file(c, h)
    files = c.get(f"/api/projects/{pid}/files").get_json()["files"]
    summ = json.loads(files[0]["summary"])
    assert summ["total"] == TOTAL_FEEDERS
    assert summ["Energized"] == 2 and summ["Issued"] == 1 and summ["Not Issued"] == 3
    # project rollup present
    prog = c.get("/api/projects").get_json()["projects"][0]["progress"]
    assert prog["total"] == TOTAL_FEEDERS


# ---------------- Notes ----------------
def test_notes_set_get_clear(admin):
    c, h = admin
    pid, fid = make_project_with_file(c, h)
    assert c.get(f"/api/files/{fid}/notes").get_json()["notes"] == {}
    c.post(f"/api/files/{fid}/edge/3/note", json={"note": "waiting on cable"}, headers=h)
    notes = c.get(f"/api/files/{fid}/notes").get_json()["notes"]
    assert notes["3"]["note"] == "waiting on cable" and notes["3"]["by"] == "admin"
    c.post(f"/api/files/{fid}/edge/3/note", json={"note": "  "}, headers=h)
    assert "3" not in c.get(f"/api/files/{fid}/notes").get_json()["notes"]


# ---------------- Photos ----------------
def _png_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def test_photo_upload_list_serve_delete(admin):
    c, h = admin
    pid, fid = make_project_with_file(c, h)
    # upload
    r = c.post(f"/api/files/{fid}/edge/3/photos",
               data={"photo": (io.BytesIO(_png_bytes()), "site.png"), "caption": "north wall"},
               content_type="multipart/form-data", headers=h).get_json()
    assert r["ok"]
    photo_id = r["id"]
    # listed under the feeder
    photos = c.get(f"/api/files/{fid}/photos").get_json()["photos"]
    assert photos["3"][0]["id"] == photo_id and photos["3"][0]["caption"] == "north wall"
    # served as a (re-encoded JPEG) image
    img = c.get(f"/api/photos/{photo_id}")
    assert img.status_code == 200 and img.mimetype == "image/jpeg" and len(img.data) > 0
    # delete
    assert c.delete(f"/api/photos/{photo_id}", headers=h).get_json()["ok"]
    assert c.get(f"/api/files/{fid}/photos").get_json()["photos"] == {}


def test_photo_rejects_non_image(admin):
    c, h = admin
    pid, fid = make_project_with_file(c, h)
    r = c.post(f"/api/files/{fid}/edge/3/photos",
               data={"photo": (io.BytesIO(b"not an image"), "x.png")},
               content_type="multipart/form-data", headers=h)
    assert r.status_code == 400


def test_photo_limit_per_feeder(admin):
    c, h = admin
    pid, fid = make_project_with_file(c, h)
    for _ in range(6):
        assert c.post(f"/api/files/{fid}/edge/3/photos",
                      data={"photo": (io.BytesIO(_png_bytes()), "p.png")},
                      content_type="multipart/form-data", headers=h).status_code == 200
    # 7th is rejected
    assert c.post(f"/api/files/{fid}/edge/3/photos",
                  data={"photo": (io.BytesIO(_png_bytes()), "p.png")},
                  content_type="multipart/form-data", headers=h).status_code == 400


# ---------------- Presence ----------------
def test_presence_lists_viewers(admin, appmod):
    c, h = admin
    pid, fid = make_project_with_file(c, h)
    # admin views the file
    r = c.get(f"/api/files/{fid}/presence").get_json()
    assert r["you"] == "admin" and "admin" in r["viewers"]
    # a second user views it too
    c.post("/api/users", json={"username": "bob", "password": "pw", "role": "user"}, headers=h)
    bob = appmod.app.test_client()
    login(bob, "bob", "pw")
    bob.get(f"/api/files/{fid}/presence")
    viewers = c.get(f"/api/files/{fid}/presence").get_json()["viewers"]
    assert "admin" in viewers and "bob" in viewers


# ---------------- Account ----------------
def test_self_password_change(admin, appmod):
    c, h = admin
    assert c.post("/api/account/password",
                  json={"current_password": "wrong", "new_password": "abcdef"}, headers=h).status_code == 400
    assert c.post("/api/account/password",
                  json={"current_password": "admin123", "new_password": "x"}, headers=h).status_code == 400
    assert c.post("/api/account/password",
                  json={"current_password": "admin123", "new_password": "newpass1"}, headers=h).get_json()["ok"]
    fresh = appmod.app.test_client()
    assert fresh.post("/login", data={"username": "admin", "password": "admin123"}).status_code == 401
    assert fresh.post("/login", data={"username": "admin", "password": "newpass1"}).status_code in (301, 302)
