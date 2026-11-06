"""HTTP routes for live GPS and camera geofence zones (/api/camera/...)."""
from __future__ import annotations

import pytest

from tests.test_control_gps_geofence import GPS_REPLY, ZONES_REPLY
from viofosync_lib import _control as control


class _FakeMqttService:
    def __init__(self, **kwargs):
        self._last_node_id = ""
        self._last_discovery_prefix = ""

    def start(self): pass
    async def stop(self): pass
    async def on_settings_changed(self, keys, snap): pass
    def get_status(self):
        return {"state": "idle", "detail": None, "last_published_at": None}


@pytest.fixture
def client(tmp_config_dir, tmp_recordings_dir, monkeypatch):
    import bcrypt
    from fastapi.testclient import TestClient

    from web import settings as settings_mod
    from web.app import create_app
    from web.services.sync_worker import SyncWorker

    settings_mod.reset_for_tests()
    p = settings_mod.get_provider()
    data = p._store.load()
    data["WEB_PASSWORD_HASH"] = bcrypt.hashpw(
        b"pw" * 8, bcrypt.gensalt()).decode()
    data["ADDRESS"] = "cam"
    data["LOCATIONS"] = [{"name": "Home", "lat": 52.2501, "lon": -1.5001,
                          "radius_m": 100, "exclude_recordings": False,
                          "is_home": True}]
    p._store.write(data)
    settings_mod.reset_for_tests()

    monkeypatch.setattr(SyncWorker, "start", lambda self: None)
    monkeypatch.setattr("web.app.MqttService", _FakeMqttService)

    c = TestClient(create_app())
    c.__enter__()
    c.post("/api/auth/login", json={"password": "pwpwpwpwpwpwpwpw"})
    c.headers.update({"x-csrf-token": c.get("/api/auth/csrf").json()["csrf"]})
    yield c
    c.__exit__(None, None, None)
    settings_mod.reset_for_tests()


@pytest.fixture
def camera(monkeypatch):
    calls: list[str] = []
    replies = {
        "cmd=3012": "<Function><String>VIOFO_A229P_V1.5_260813</String></Function>",
        "cmd=8058": GPS_REPLY,
        "cmd=9350": ZONES_REPLY,
    }

    def fake_get(url):
        calls.append(url)
        for needle, body in replies.items():
            if needle in url:
                return body
        raise AssertionError(url)

    monkeypatch.setattr(control, "_http_get", fake_get)
    return calls, replies


def test_gps_uses_saved_locations_for_the_sign(client, camera):
    g = client.get("/api/camera/gps").json()
    assert g["fix"] is True and g["lon"] == -1.5


def test_geofence_read(client, camera):
    z = client.get("/api/camera/geofence").json()
    assert z["supported"] is True
    assert z["zones"][0]["lon"] == -1.5 and z["zones"][0]["enabled"] is True


def test_geofence_enable(client, camera):
    calls, replies = camera
    replies["cmd=9350"] = ZONES_REPLY.replace("<Zone2>0", "<Zone2>1")
    r = client.post("/api/camera/geofence/2", json={"enabled": True})
    assert r.status_code == 200
    assert calls[-1].endswith("cmd=9350&str=on&zone=1")


def test_geofence_capture_without_fix_is_422(client, camera):
    _, replies = camera
    replies["cmd=9350"] = "<Function><Error>GPS Not Available</Error></Function>"
    r = client.post("/api/camera/geofence/1/capture", json={"radius_m": 50})
    assert r.status_code == 422


def test_geofence_capture_radius_out_of_range(client, camera):
    calls, _ = camera
    r = client.post("/api/camera/geofence/1/capture", json={"radius_m": 5})
    assert r.status_code == 422
    assert not any("cmd=9350" in u for u in calls)
