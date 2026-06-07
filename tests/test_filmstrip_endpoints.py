"""Tests for GET /api/archive/clip/{id}/filmstrip[.jpg]."""
from __future__ import annotations

from pathlib import Path

import pytest


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
    yield c, Path(str(tmp_recordings_dir))
    c.__exit__(None, None, None)
    settings_mod.reset_for_tests()


def _insert_clip(app, clip_id: int, path: str, duration_s: float) -> None:
    db = app.state.db
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(id, path, basename, group_name, timestamp, camera, "
            " sequence, event_type, has_gpx, gps_examined, scanned_at, duration_s) "
            "VALUES (?,?,?,?,?,?,?,?,0,0,?,?)",
            (clip_id, path, f"{clip_id}.MP4", "2026-06-02",
             1_717_312_440, "F", clip_id, "normal", 1_717_312_440, duration_s),
        )


def test_filmstrip_meta_returns_slicing_info(logged_in_client, monkeypatch):
    client, rec = logged_in_client
    clip_file = rec / "clip.mp4"
    clip_file.write_bytes(b"\0")
    _insert_clip(client.app, 1, str(clip_file), 60.0)

    from web.services import filmstrip

    async def fake_ensure(recordings, clip_id, video_path, duration_s):
        Path(filmstrip.sprite_path(recordings, clip_id)).write_bytes(b"\xff\xd8\xff\xd9")
        return filmstrip.FilmstripMeta(8, 8, 160, 90, 60.0)

    monkeypatch.setattr("web.routers.archive.filmstrip.ensure_filmstrip", fake_ensure)

    r = client.get("/api/archive/clip/1/filmstrip")
    assert r.status_code == 200
    body = r.json()
    assert body["frames"] == 8
    assert body["interval_s"] == 8
    assert body["tile_w"] == 160
    assert body["sprite_url"] == "/api/archive/clip/1/filmstrip.jpg"


def test_filmstrip_meta_204_when_ffmpeg_unavailable(logged_in_client, monkeypatch):
    client, rec = logged_in_client
    clip_file = rec / "clip.mp4"
    clip_file.write_bytes(b"\0")
    _insert_clip(client.app, 2, str(clip_file), 60.0)

    async def fake_ensure(*a, **k):
        return None

    monkeypatch.setattr("web.routers.archive.filmstrip.ensure_filmstrip", fake_ensure)

    r = client.get("/api/archive/clip/2/filmstrip")
    assert r.status_code == 204


def test_filmstrip_jpg_served(logged_in_client, monkeypatch):
    client, rec = logged_in_client
    clip_file = rec / "clip.mp4"
    clip_file.write_bytes(b"\0")
    _insert_clip(client.app, 3, str(clip_file), 60.0)

    from web.services import filmstrip

    async def fake_ensure(recordings, clip_id, video_path, duration_s):
        Path(filmstrip.sprite_path(recordings, clip_id)).write_bytes(b"\xff\xd8\xff\xd9")
        return filmstrip.FilmstripMeta(8, 8, 160, 90, 60.0)

    monkeypatch.setattr("web.routers.archive.filmstrip.ensure_filmstrip", fake_ensure)

    r = client.get("/api/archive/clip/3/filmstrip.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")


def test_filmstrip_jpg_404_when_ffmpeg_unavailable(logged_in_client, monkeypatch):
    client, rec = logged_in_client
    clip_file = rec / "clip.mp4"
    clip_file.write_bytes(b"\0")
    _insert_clip(client.app, 4, str(clip_file), 60.0)

    async def fake_ensure(*a, **k):
        return None

    monkeypatch.setattr("web.routers.archive.filmstrip.ensure_filmstrip", fake_ensure)

    r = client.get("/api/archive/clip/4/filmstrip.jpg")
    assert r.status_code == 404


def test_filmstrip_jpg_404_when_sprite_missing(logged_in_client, monkeypatch):
    # Defensive guard: meta is returned but the sprite file is absent
    # (e.g. retention deleted it concurrently) -> 404, not a 500.
    client, rec = logged_in_client
    clip_file = rec / "clip.mp4"
    clip_file.write_bytes(b"\0")
    _insert_clip(client.app, 5, str(clip_file), 60.0)

    from web.services import filmstrip

    async def fake_ensure(recordings, clip_id, video_path, duration_s):
        # Return valid meta but deliberately DO NOT write the sprite file.
        return filmstrip.FilmstripMeta(8, 8, 160, 90, 60.0)

    monkeypatch.setattr("web.routers.archive.filmstrip.ensure_filmstrip", fake_ensure)

    r = client.get("/api/archive/clip/5/filmstrip.jpg")
    assert r.status_code == 404
