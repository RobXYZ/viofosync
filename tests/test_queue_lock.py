"""queue.set_locked: flips the user 'locked' flag on both tables + audit log."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _env(tmp_path: Path):
    from web.db import Database
    rec = tmp_path / "rec"
    rec.mkdir()
    return rec, Database(str(rec / "v.db"))


def _clip(db, basename):
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index (path, basename, group_name, timestamp, "
            " camera, sequence, event_type, size_bytes, has_gpx, scanned_at) "
            "VALUES (?,?, '2026-06-27', 1, 'F', 1, 'normal', 1, 0, 1)",
            (f"/x/{basename}", basename),
        )
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, enqueued_at) "
            "VALUES (?, '/DCIM', 'done', 1)", (basename,),
        )


def test_set_locked_sets_both_tables(tmp_path):
    from web.services.queue import set_locked
    rec, db = _env(tmp_path)
    _clip(db, "A.MP4")
    n = set_locked(db, ["A.MP4"], True)
    assert n == 1
    with db.conn() as c:
        assert c.execute("SELECT locked FROM clip_index WHERE basename='A.MP4'").fetchone()["locked"] == 1
        assert c.execute("SELECT locked FROM download_queue WHERE filename='A.MP4'").fetchone()["locked"] == 1


def test_set_locked_unlock(tmp_path):
    from web.services.queue import set_locked
    rec, db = _env(tmp_path)
    _clip(db, "A.MP4")
    set_locked(db, ["A.MP4"], True)
    set_locked(db, ["A.MP4"], False)
    with db.conn() as c:
        assert c.execute("SELECT locked FROM clip_index WHERE basename='A.MP4'").fetchone()["locked"] == 0


def test_set_locked_logs_audit(tmp_path, caplog):
    from web.services.queue import set_locked
    rec, db = _env(tmp_path)
    _clip(db, "A.MP4")
    with caplog.at_level("INFO", logger="viofosync.queue"):
        set_locked(db, ["A.MP4"], True)
    assert any("read-only" in r.getMessage().lower() and "A.MP4" in r.getMessage()
               for r in caplog.records if r.name == "viofosync.queue")


def test_set_locked_empty_is_noop(tmp_path):
    from web.services.queue import set_locked
    rec, db = _env(tmp_path)
    assert set_locked(db, [], True) == 0


# --- endpoint smoke test ---


def _insert_queue(c, filename, state="done"):
    now = int(time.time())
    c.execute(
        "INSERT INTO download_queue "
        "(filename, source_dir, state, enqueued_at) "
        "VALUES (?, ?, ?, ?)",
        (filename, "/DCIM", state, now),
    )


def _insert_index(c, basename):
    c.execute(
        "INSERT INTO clip_index (path, basename, group_name, timestamp, "
        " camera, sequence, event_type, size_bytes, has_gpx, scanned_at) "
        "VALUES (?,?, '2026-06-27', 1, 'F', 1, 'normal', 1, 0, 1)",
        (f"/x/{basename}", basename),
    )


@pytest.fixture
def authed_client(tmp_config_dir: Path, tmp_recordings_dir: Path, monkeypatch):
    from web import app as app_mod
    from web import settings as settings_mod
    monkeypatch.setenv("VIOFOSYNC_RESTART_DISABLED", "1")
    settings_mod.reset_for_tests()
    application = app_mod.create_app()
    with TestClient(application) as c:
        c.post("/setup", data={
            "address": "192.168.1.230",
            "password": "twelve-chars-min!",
            "confirm": "twelve-chars-min!",
        })
        csrf = c.get("/api/auth/csrf").json()["csrf"]
        c.headers.update({"x-csrf-token": csrf})
        yield c


def test_lock_endpoint(authed_client):
    db = authed_client.app.state.db
    with db.write() as c:
        _insert_queue(c, "B.MP4", "done")
        _insert_index(c, "B.MP4")

    r = authed_client.post("/api/queue/lock", json={"filenames": ["B.MP4"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["updated"] == 1

    with db.conn() as c:
        assert c.execute(
            "SELECT locked FROM clip_index WHERE basename='B.MP4'"
        ).fetchone()["locked"] == 1
        assert c.execute(
            "SELECT locked FROM download_queue WHERE filename='B.MP4'"
        ).fetchone()["locked"] == 1


def test_unlock_endpoint(authed_client):
    # /queue/unlock is the reverse of /queue/lock — before it existed a
    # mark-read-only was permanent short of sqlite3 surgery on the DB.
    db = authed_client.app.state.db
    with db.write() as c:
        _insert_queue(c, "U.MP4", "done")
        _insert_index(c, "U.MP4")

    authed_client.post("/api/queue/lock", json={"filenames": ["U.MP4"]})
    r = authed_client.post("/api/queue/unlock", json={"filenames": ["U.MP4"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["updated"] == 1

    with db.conn() as c:
        assert c.execute(
            "SELECT locked FROM clip_index WHERE basename='U.MP4'"
        ).fetchone()["locked"] == 0
        assert c.execute(
            "SELECT locked FROM download_queue WHERE filename='U.MP4'"
        ).fetchone()["locked"] == 0
