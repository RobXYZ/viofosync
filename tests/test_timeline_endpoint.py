"""Tests for build_route_payload + GET /api/archive/timeline."""
from __future__ import annotations

from pathlib import Path

import pytest

from web.db import Database
from web.routers import archive


def test_build_route_payload_empty_day(tmp_path: Path):
    """No gpx clips for the date -> empty journeys/stops, point_count 0."""
    db = Database(str(tmp_path / "t.db"))
    payload = archive.build_route_payload(db, "2026-06-02", None)
    assert payload["date"] == "2026-06-02"
    assert payload["point_count"] == 0
    assert payload["journeys"] == []
    assert payload["stops"] == []


class _FakeMqttService:
    def __init__(self, **kwargs): pass
    def start(self): pass
    async def stop(self): pass
    async def on_settings_changed(self, keys, snap): pass
    def get_status(self):
        return {"state": "idle", "detail": None, "last_published_at": None}


@pytest.fixture
def logged_in_client(tmp_config_dir, tmp_recordings_dir, monkeypatch):
    import bcrypt
    from fastapi.testclient import TestClient

    from web import settings as settings_mod
    from web.app import create_app
    from web.services.sync_worker import SyncWorker

    digest = bcrypt.hashpw(b"pw" * 8, bcrypt.gensalt()).decode()
    settings_mod.reset_for_tests()
    p = settings_mod.get_provider()
    data = p._store.load()
    data["WEB_PASSWORD_HASH"] = digest
    p._store.write(data)
    settings_mod.reset_for_tests()

    monkeypatch.setattr(SyncWorker, "start", lambda self: None)
    monkeypatch.setattr("web.app.MqttService", _FakeMqttService)

    app = create_app()
    c = TestClient(app)
    c.__enter__()
    c.post("/api/auth/login", json={"password": "pwpwpwpwpwpwpwpw"})
    yield c
    c.__exit__(None, None, None)
    settings_mod.reset_for_tests()


def _insert_clip(app, clip_id, ts, camera, duration_s, date="2026-06-02"):
    with app.state.db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(id, path, basename, group_name, timestamp, camera, "
            " sequence, event_type, has_gpx, gps_examined, scanned_at, duration_s) "
            "VALUES (?,?,?,?,?,?,?,?,0,0,?,?)",
            (clip_id, f"/rec/{clip_id}.MP4", f"{clip_id}.MP4", date,
             ts, camera, clip_id, "normal", ts, duration_s),
        )


def test_timeline_bad_date_400(logged_in_client):
    r = logged_in_client.get("/api/archive/timeline?date=nonsense")
    assert r.status_code == 400


def test_timeline_day_mode_channels_clips_bounds(logged_in_client):
    app = logged_in_client.app
    _insert_clip(app, 1, 1_717_312_440, "F", 60.0)
    _insert_clip(app, 2, 1_717_312_440, "R", 60.0)
    _insert_clip(app, 3, 1_717_312_500, "F", 60.0)

    r = logged_in_client.get("/api/archive/timeline?date=2026-06-02")
    assert r.status_code == 200
    body = r.json()
    assert [ch["key"] for ch in body["channels"]] == ["front", "rear"]
    assert body["channels"][0]["label"] == "Front"
    assert len(body["clips"]) == 3
    assert body["bounds"]["start_ts"] == 1_717_312_440
    assert body["bounds"]["end_ts"] == 1_717_312_560
    assert body["gps"] is None


def test_timeline_journey_mode_windows_clips(logged_in_client, monkeypatch):
    app = logged_in_client.app
    _insert_clip(app, 1, 1_717_312_440, "F", 60.0)
    _insert_clip(app, 2, 1_717_312_500, "F", 60.0)
    _insert_clip(app, 3, 1_717_313_040, "F", 60.0)

    fake_route = {
        "date": "2026-06-02",
        "point_count": 5,
        "journeys": [{"start_ts": 1_717_312_440, "end_ts": 1_717_312_560}],
        "stops": [],
    }
    monkeypatch.setattr(
        "web.routers.archive.build_route_payload",
        lambda db, date, geocoder: fake_route,
    )

    r = logged_in_client.get("/api/archive/timeline?date=2026-06-02&journey=0")
    assert r.status_code == 200
    body = r.json()
    ids = sorted(c["id"] for c in body["clips"])
    assert ids == [1, 2]
    assert [ch["key"] for ch in body["channels"]] == ["front"]
    assert body["bounds"]["start_ts"] == 1_717_312_440
    assert body["bounds"]["end_ts"] == 1_717_312_560
    assert body["gps"] is not None


def test_timeline_journey_out_of_range_404(logged_in_client, monkeypatch):
    app = logged_in_client.app
    _insert_clip(app, 1, 1_717_312_440, "F", 60.0)
    monkeypatch.setattr(
        "web.routers.archive.build_route_payload",
        lambda db, date, geocoder: {
            "date": "2026-06-02", "point_count": 0, "journeys": [], "stops": [],
        },
    )
    r = logged_in_client.get("/api/archive/timeline?date=2026-06-02&journey=0")
    assert r.status_code == 404
