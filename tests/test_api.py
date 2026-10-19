import os
import tempfile
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_test_dir = tempfile.mkdtemp(prefix="cargosync-test-")
os.environ["CARGOSYNC_DATA_DIR"] = _test_dir
os.environ["CARGOSYNC_SECURE_COOKIES"] = "0"

from fastapi.testclient import TestClient
from app import app, create_admin_user, connect

admin_password = "Admin-Test-Password-2026!"
try:
    create_admin_user("Test Admin", "admin", admin_password)
except ValueError:
    pass


def test_server_auth_shared_schedule_roles_and_documents():
    admin = TestClient(app)
    assert admin.get("/health").status_code == 200
    assert admin.get("/api/workspace/state").status_code == 401
    r = admin.post("/api/auth/login", json={"username": "admin", "password": admin_password})
    assert r.status_code == 200, r.text

    r = admin.post("/api/users", json={"name": "Dispatcher One", "username": "dispatch1", "password": "Dispatcher-Sample-Pass-2026!", "role": "dispatcher"})
    assert r.status_code == 200, r.text
    dispatcher = r.json()["user"]
    assert "password_hash" not in dispatcher
    r = admin.post("/api/drivers", json={"name": "Alex Morgan", "truck": "101", "status": "Available", "dispatcherId": dispatcher["id"]})
    assert r.status_code == 200, r.text
    driver = r.json()["driver"]

    base = admin.get("/api/workspace/state").json()
    load = {"id": "load-1", "number": "CS-1001", "rate": 2450, "driver": driver["name"], "truck": driver["truck"], "dispatcherId": dispatcher["id"], "pu": "Chicago, IL", "del": "Atlanta, GA", "puDate": "2026-10-20", "delDate": "2026-10-22", "status": "booked", "rateFileName": "", "podFileName": ""}
    key = "cargosync_schedule_v2"
    r = admin.put(f"/api/workspace/state/{key}", json={"value": [load], "base_version": base["versions"][key]})
    assert r.status_code == 200, r.text

    disp = TestClient(app)
    r = disp.post("/api/auth/login", json={"username": "dispatch1", "password": "Dispatcher-Sample-Pass-2026!"})
    assert r.status_code == 200, r.text
    # Newly created accounts must change their temporary password before using workspace APIs.
    blocked = disp.get("/api/workspace/state")
    assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "PASSWORD_CHANGE_REQUIRED"
    changed_password = "Dispatcher-One-Changed-2026!"
    password_update = disp.post("/api/auth/password", json={"current_password": "Dispatcher-Sample-Pass-2026!", "new_password": changed_password})
    assert password_update.status_code == 200, password_update.text
    state = disp.get("/api/workspace/state").json()
    assert state["states"][key][0]["number"] == "CS-1001"
    changed = dict(load, rate=2650)
    r = disp.put(f"/api/workspace/state/{key}", json={"value": [changed], "base_version": state["versions"][key]})
    assert r.status_code == 200, r.text

    # Concurrent stale save must be rejected.
    stale = disp.put(f"/api/workspace/state/{key}", json={"value": [dict(changed, rate=2700)], "base_version": state["versions"][key]})
    assert stale.status_code == 409

    # A dispatcher cannot take over a load assigned to someone else.
    viewer_load = dict(changed, dispatcherId="different-user")
    state2 = disp.get("/api/workspace/state").json()
    denied = disp.put(f"/api/workspace/state/{key}", json={"value": [viewer_load], "base_version": state2["versions"][key]})
    assert denied.status_code == 403

    # Upload and retrieve a minimal valid PDF rate confirmation.
    state3 = admin.get("/api/workspace/state").json()
    # Admin changed version in own test client after driver's schedule writes; data is shared.
    pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF"
    upload = admin.put("/api/loads/load-1/documents/rate", files={"file": ("rate-confirmation.pdf", pdf, "application/pdf")})
    assert upload.status_code == 200, upload.text
    download = disp.get("/api/loads/load-1/documents/rate")
    assert download.status_code == 200
    assert download.content == pdf

    # Viewer is read-only for the team Schedule.
    r = admin.post("/api/users", json={"name": "Read Only", "username": "viewer1", "password": "Viewer-Sample-Pass-2026!", "role": "viewer"})
    assert r.status_code == 200
    viewer = TestClient(app)
    assert viewer.post("/api/auth/login", json={"username": "viewer1", "password": "Viewer-Sample-Pass-2026!"}).status_code == 200
    assert viewer.post("/api/auth/password", json={"current_password": "Viewer-Sample-Pass-2026!", "new_password": "Viewer-Changed-Password-2026!"}).status_code == 200
    snap = viewer.get("/api/workspace/state").json()
    denied = viewer.put(f"/api/workspace/state/{key}", json={"value": snap["states"][key], "base_version": snap["versions"][key]})
    assert denied.status_code == 403
    assert viewer.delete("/api/users/" + dispatcher["id"]).status_code == 403


def test_html_bootstrap_is_server_rendered():
    client = TestClient(app)
    unauth = client.get("/")
    assert unauth.status_code == 200
    assert "cargoServerBootstrap" in unauth.text
    assert '"authenticated":false' in unauth.text
    assert "ChangeMe-2026!" not in unauth.text


def test_login_attempts_are_rate_limited():
    client = TestClient(app)
    for _ in range(6):
        response = client.post("/api/auth/login", json={"username": "rate_limit_only", "password": "not-the-correct-password"})
        assert response.status_code == 401
    limited = client.post("/api/auth/login", json={"username": "rate_limit_only", "password": "not-the-correct-password"})
    assert limited.status_code == 429
    assert "Too many" in limited.json()["detail"]


def test_reassigning_driver_transfers_load_ownership():
    admin = TestClient(app)
    assert admin.post("/api/auth/login", json={"username": "admin", "password": admin_password}).status_code == 200
    created = admin.post("/api/users", json={"name": "Dispatcher Two", "username": "dispatch2", "password": "Dispatcher-Two-Pass-2026!", "role": "dispatcher"})
    assert created.status_code == 200, created.text
    dispatcher2 = created.json()["user"]
    drivers = admin.get("/api/drivers").json()["drivers"]
    alex = next(d for d in drivers if d["name"] == "Alex Morgan")
    assigned = admin.patch(f"/api/drivers/{alex['id']}", json={"dispatcherId": dispatcher2["id"]})
    assert assigned.status_code == 200, assigned.text
    state = admin.get("/api/workspace/state").json()
    load = next(l for l in state["states"]["cargosync_schedule_v2"] if l["id"] == "load-1")
    assert load["dispatcherId"] == dispatcher2["id"]

    old_dispatcher = TestClient(app)
    assert old_dispatcher.post("/api/auth/login", json={"username": "dispatch1", "password": "Dispatcher-One-Changed-2026!"}).status_code == 200
    old_loads = old_dispatcher.get("/api/workspace/state").json()
    old_load = dict(old_loads["states"]["cargosync_schedule_v2"][0], rate=2750)
    denied = old_dispatcher.put("/api/workspace/state/cargosync_schedule_v2", json={"value": [old_load], "base_version": old_loads["versions"]["cargosync_schedule_v2"]})
    assert denied.status_code == 403

    new_dispatcher = TestClient(app)
    assert new_dispatcher.post("/api/auth/login", json={"username": "dispatch2", "password": "Dispatcher-Two-Pass-2026!"}).status_code == 200
    assert new_dispatcher.post("/api/auth/password", json={"current_password": "Dispatcher-Two-Pass-2026!", "new_password": "Dispatcher-Two-Changed-2026!"}).status_code == 200
    new_state = new_dispatcher.get("/api/workspace/state").json()
    new_load = dict(new_state["states"]["cargosync_schedule_v2"][0], rate=2800)
    allowed = new_dispatcher.put("/api/workspace/state/cargosync_schedule_v2", json={"value": [new_load], "base_version": new_state["versions"]["cargosync_schedule_v2"]})
    assert allowed.status_code == 200, allowed.text
    latest = new_dispatcher.get("/api/workspace/state").json()
    forged = dict(latest["states"]["cargosync_schedule_v2"][0], id="forged-load", number="FORGED", driver="Someone Else", truck="999", dispatcherId=dispatcher2["id"])
    denied_forged = new_dispatcher.put("/api/workspace/state/cargosync_schedule_v2", json={"value": latest["states"]["cargosync_schedule_v2"] + [forged], "base_version": latest["versions"]["cargosync_schedule_v2"]})
    assert denied_forged.status_code == 403