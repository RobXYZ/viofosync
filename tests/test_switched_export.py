"""Tests for the switched export job (enqueue + render, ffmpeg mocked)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from web.db import Database
from web.services.exporter import ExportWorker


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "t.db"))


async def _noop(_e):  # broadcast stub
    pass


def _worker(db):
    return ExportWorker(db=db, provider=MagicMock(), broadcast=_noop)


def test_enqueue_switched_stores_plan_and_range(db, monkeypatch):
    monkeypatch.setattr("web.services.exporter.ffmpeg_available", lambda: True)
    segs = [
        {"channel": "rear", "start_ts": 1000.0, "end_ts": 1020.0},
        {"channel": "front", "start_ts": 1020.0, "end_ts": 1050.0},
    ]
    job_id = _worker(db).enqueue_switched(segs, encoder="software")
    with db.conn() as c:
        row = c.execute(
            "SELECT type, clip_ids, clip_start, clip_end FROM export_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    import json
    assert row["type"] == "switched"
    payload = json.loads(row["clip_ids"])
    assert payload["encoder"] == "software"
    assert len(payload["segments"]) == 2
    assert row["clip_start"] == 1000
    assert row["clip_end"] == 1050


def test_enqueue_switched_rejects_empty(db, monkeypatch):
    monkeypatch.setattr("web.services.exporter.ffmpeg_available", lambda: True)
    with pytest.raises(ValueError):
        _worker(db).enqueue_switched([], encoder="software")


def test_enqueue_switched_rejects_bad_window(db, monkeypatch):
    monkeypatch.setattr("web.services.exporter.ffmpeg_available", lambda: True)
    with pytest.raises(ValueError):
        _worker(db).enqueue_switched(
            [{"channel": "front", "start_ts": 50.0, "end_ts": 50.0}],
            encoder="software",
        )


def _insert_clip(db, clip_id, ts, camera, dur, path):
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(id, path, basename, group_name, timestamp, camera, "
            " sequence, event_type, has_gpx, gps_examined, scanned_at, duration_s) "
            "VALUES (?,?,?,?,?,?,?,?,0,0,?,?)",
            (clip_id, path, f"{clip_id}.MP4", "2026-06-02",
             ts, camera, clip_id, "normal", ts, dur),
        )


async def test_run_switched_trims_and_concats(db, tmp_path, monkeypatch):
    monkeypatch.setattr("web.services.exporter.ffmpeg_available", lambda: True)
    _insert_clip(db, 1, 1000, "F", 60.0, "/rec/f0.mp4")
    _insert_clip(db, 2, 1000, "R", 60.0, "/rec/r0.mp4")
    snap = MagicMock()
    snap.recordings = str(tmp_path)
    provider = MagicMock()
    provider.get.return_value = snap
    worker = ExportWorker(db=db, provider=provider, broadcast=_noop)

    calls = []

    async def fake_run_ffmpeg(job_id, args, total, **kw):
        calls.append(args)
        Path(args[-1]).write_bytes(b"\0")
        return 0, ""

    async def fake_probe_res(path):
        return (1920, 1080)

    monkeypatch.setattr(worker, "_run_ffmpeg", fake_run_ffmpeg)
    monkeypatch.setattr(worker, "_probe_resolution", fake_probe_res)
    finishes = []
    monkeypatch.setattr(worker, "_finish",
                        lambda jid, ok, err, out: finishes.append((ok, err, out)))

    segs = [
        {"channel": "rear", "start_ts": 1000, "end_ts": 1020},
        {"channel": "front", "start_ts": 1020, "end_ts": 1050},
    ]
    import json as _json
    job = {"id": 5, "type": "switched",
           "clip_ids": _json.dumps({"segments": segs, "encoder": "software"})}
    await worker._run_job(job)

    assert finishes and finishes[-1][0] is True
    assert len(calls) == 3                      # 2 trims + 1 concat
    trim0 = calls[0]
    assert "/rec/r0.mp4" in trim0
    assert trim0[trim0.index("-ss") + 1] == "0.0"
    assert "scale=1920:1080,setsar=1" in trim0[trim0.index("-vf") + 1]
    trim1 = calls[1]
    assert "/rec/f0.mp4" in trim1
    assert trim1[trim1.index("-ss") + 1] == "20.0"
    concat = calls[2]
    assert "concat" in concat and "copy" in concat


async def test_run_switched_no_footage_fails(db, tmp_path, monkeypatch):
    monkeypatch.setattr("web.services.exporter.ffmpeg_available", lambda: True)
    snap = MagicMock()
    snap.recordings = str(tmp_path)
    provider = MagicMock()
    provider.get.return_value = snap
    worker = ExportWorker(db=db, provider=provider, broadcast=_noop)
    finishes = []
    monkeypatch.setattr(worker, "_finish",
                        lambda jid, ok, err, out: finishes.append((ok, err)))
    import json as _json
    job = {"id": 6, "type": "switched",
           "clip_ids": _json.dumps(
               {"segments": [{"channel": "front", "start_ts": 1, "end_ts": 9}],
                "encoder": "software"})}
    await worker._run_job(job)
    assert finishes[-1][0] is False


class _FakeMqttService:
    def __init__(self, **k): pass
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


def test_post_switched_export_creates_job(logged_in_client, monkeypatch):
    logged_in_client.app.state.export_encoders = {"software": True}
    csrf = logged_in_client.get("/api/auth/csrf").json()["csrf"]
    r = logged_in_client.post("/api/exports", json={
        "type": "switched",
        "segments": [
            {"channel": "rear", "start_ts": 1000.0, "end_ts": 1020.0},
            {"channel": "front", "start_ts": 1020.0, "end_ts": 1050.0},
        ],
        "encoder": "software",
    }, headers={"x-csrf-token": csrf})
    assert r.status_code == 200, r.text
    assert "job_id" in r.json()


def test_post_switched_requires_segments(logged_in_client):
    logged_in_client.app.state.export_encoders = {"software": True}
    csrf = logged_in_client.get("/api/auth/csrf").json()["csrf"]
    r = logged_in_client.post("/api/exports", json={
        "type": "switched", "clip_ids": [], "encoder": "software"},
        headers={"x-csrf-token": csrf})
    assert r.status_code in (400, 422)
