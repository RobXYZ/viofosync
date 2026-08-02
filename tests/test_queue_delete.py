"""queue.delete_clips: local file + index delete, mark download_queue skipped."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _insert_queue(c, filename, state):
    c.execute(
        "INSERT INTO download_queue (filename, source_dir, state, enqueued_at) "
        "VALUES (?, ?, ?, ?)",
        (filename, "/DCIM", state, int(time.time())),
    )


def _make_clip(rec: Path, db, *, basename, event_type="normal"):
    """A fake clip on disk + clip_index row + a 'done' download_queue row."""
    folder = rec / "2026-06-26"
    folder.mkdir(exist_ok=True)
    path = folder / basename
    path.write_bytes(b"x" * 1024)
    (folder / (basename + ".gpx")).write_text("<gpx/>")
    with db.write() as c:
        cur = c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " event_type, size_bytes, has_gpx, scanned_at) "
            "VALUES (?, ?, '2026-06-26', 1, 'F', 1, ?, 1024, 1, ?)",
            (str(path), basename, event_type, int(time.time())),
        )
        cid = cur.lastrowid
        _insert_queue(c, basename, "done")
    return cid, path


def _env(tmp_path: Path):
    from web.db import Database
    rec = tmp_path / "rec"
    rec.mkdir()
    db = Database(str(rec / "v.db"))
    return rec, db


def test_delete_removes_files_index_and_marks_skipped(tmp_path):
    from web.services.queue import delete_clips
    rec, db = _env(tmp_path)
    cid, path = _make_clip(rec, db, basename="A.MP4")
    res = delete_clips(db, ["A.MP4"], str(rec))
    assert res == {"deleted": 1, "skipped": 1, "protected": 0,
                   "protected_names": [], "failed": 0}
    assert not path.exists()
    assert not (path.parent / "A.MP4.gpx").exists()
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM clip_index").fetchone()["n"] == 0
        row = c.execute(
            "SELECT state, skip_reason FROM download_queue WHERE filename='A.MP4'"
        ).fetchone()
    assert row["state"] == "skipped" and row["skip_reason"] == "user"


def test_delete_skips_ro_clip(tmp_path):
    from web.services.queue import delete_clips
    rec, db = _env(tmp_path)
    _, path = _make_clip(rec, db, basename="LOCK.MP4", event_type="ro")
    res = delete_clips(db, ["LOCK.MP4"], str(rec))
    assert res["deleted"] == 0
    assert res.get("protected") == 1
    assert path.exists()  # RO clip retained


def test_delete_nonexistent_file_reports_zero(tmp_path):
    # A filename with no clip_index AND no download_queue row touches nothing:
    # skipped must reflect the actual queue UPDATE rowcount (0), not len(targets).
    from web.services.queue import delete_clips
    rec, db = _env(tmp_path)
    res = delete_clips(db, ["GHOST.MP4"], str(rec))
    assert res == {"deleted": 0, "skipped": 0, "protected": 0,
                   "protected_names": [], "failed": 0}


def test_delete_skips_locked_clip(tmp_path):
    from web.services.queue import delete_clips
    rec, db = _env(tmp_path)
    _, path = _make_clip(rec, db, basename="KEEP.MP4")
    with db.write() as c:
        c.execute("UPDATE clip_index SET locked=1 WHERE basename='KEEP.MP4'")
    res = delete_clips(db, ["KEEP.MP4"], str(rec))
    assert res["deleted"] == 0 and res.get("protected") == 1
    assert path.exists()


def test_delete_undownloaded_marks_skipped_only(tmp_path):
    # A queued-but-not-downloaded clip: no clip_index row, just a pending queue row.
    from web.services.queue import delete_clips
    rec, db = _env(tmp_path)
    with db.write() as c:
        _insert_queue(c, "PEND.MP4", "pending")
    res = delete_clips(db, ["PEND.MP4"], str(rec))
    assert res == {"deleted": 0, "skipped": 1, "protected": 0,
                   "protected_names": [], "failed": 0}
    with db.conn() as c:
        assert c.execute(
            "SELECT state FROM download_queue WHERE filename='PEND.MP4'"
        ).fetchone()["state"] == "skipped"


def test_delete_missing_file_does_not_raise(tmp_path):
    from web.services.queue import delete_clips
    rec, db = _env(tmp_path)
    _, path = _make_clip(rec, db, basename="GONE.MP4")
    path.unlink()  # file already gone; index row remains
    res = delete_clips(db, ["GONE.MP4"], str(rec))
    assert res["deleted"] == 1  # the index row was removed


def test_delete_empty_is_noop(tmp_path):
    from web.services.queue import delete_clips
    rec, db = _env(tmp_path)
    assert delete_clips(db, [], str(rec)) == {
        "deleted": 0, "skipped": 0, "protected": 0,
        "protected_names": [], "failed": 0}


def test_delete_logs_info_audit_line(tmp_path, caplog):
    # The viofosync.queue logger emits at INFO, which DBLogHandler persists to
    # app_log — so a delete leaves an audit trail.
    from web.services.queue import delete_clips
    rec, db = _env(tmp_path)
    _make_clip(rec, db, basename="A.MP4")
    with caplog.at_level("INFO", logger="viofosync.queue"):
        delete_clips(db, ["A.MP4"], str(rec))
    recs = [r for r in caplog.records if r.name == "viofosync.queue"]
    assert any("archive delete" in r.getMessage() and "A.MP4" in r.getMessage()
               for r in recs), recs


def test_delete_noop_does_not_log(tmp_path, caplog):
    # Nothing matched -> no audit line (avoids "removed 0" noise).
    from web.services.queue import delete_clips
    rec, db = _env(tmp_path)
    with caplog.at_level("INFO", logger="viofosync.queue"):
        delete_clips(db, ["NOPE.MP4"], str(rec))
    assert not [r for r in caplog.records if r.name == "viofosync.queue"]


def test_delete_failed_unlink_keeps_rows_and_reports_failed(tmp_path, monkeypatch):
    # A file the container can't unlink (classic Unraid PUID/permissions
    # mismatch) must NOT count as deleted: the index row stays, the queue row
    # stays untouched, and the response says 'failed'. Dropping the row while
    # the .mp4 survives would free nothing and resurrect the clip on rescan.
    from web.services.queue import delete_clips
    rec, db = _env(tmp_path)
    _, path = _make_clip(rec, db, basename="STUCK.MP4")
    real_remove = os.remove

    def deny_mp4(p, *a, **kw):
        if str(p) == str(path):
            raise PermissionError(1, "Operation not permitted", str(p))
        return real_remove(p, *a, **kw)

    monkeypatch.setattr("web.services.retention.os.remove", deny_mp4)
    res = delete_clips(db, ["STUCK.MP4"], str(rec))
    assert res["deleted"] == 0 and res["failed"] == 1
    assert path.exists()
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM clip_index").fetchone()["n"] == 1
        row = c.execute(
            "SELECT state FROM download_queue WHERE filename='STUCK.MP4'"
        ).fetchone()
    assert row["state"] == "done"  # not marked skipped — nothing went away


def test_delete_force_removes_protected(tmp_path):
    # force=True is the confirm-through path: the UI has already shown a
    # second "delete anyway?" dialog, so RO and user-locked clips go too.
    from web.services.queue import delete_clips
    rec, db = _env(tmp_path)
    _, ro_path = _make_clip(rec, db, basename="RO.MP4", event_type="ro")
    _, lk_path = _make_clip(rec, db, basename="LOCKED.MP4")
    with db.write() as c:
        c.execute("UPDATE clip_index SET locked=1 WHERE basename='LOCKED.MP4'")
    res = delete_clips(db, ["RO.MP4", "LOCKED.MP4"], str(rec), force=True)
    assert res["deleted"] == 2 and res["protected"] == 0 and res["failed"] == 0
    assert not ro_path.exists() and not lk_path.exists()
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM clip_index").fetchone()["n"] == 0


def test_delete_reports_protected_names(tmp_path):
    # The UI force-deletes exactly these names after its second confirm.
    from web.services.queue import delete_clips
    rec, db = _env(tmp_path)
    _make_clip(rec, db, basename="RO.MP4", event_type="ro")
    _, ok_path = _make_clip(rec, db, basename="OK.MP4")
    res = delete_clips(db, ["RO.MP4", "OK.MP4"], str(rec))
    assert res["deleted"] == 1 and not ok_path.exists()
    assert res["protected"] == 1 and res["protected_names"] == ["RO.MP4"]


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


def test_delete_endpoint(authed_client, tmp_recordings_dir: Path):
    db = authed_client.app.state.db
    folder = tmp_recordings_dir / "2026-06-26"
    folder.mkdir(exist_ok=True)
    path = folder / "A.MP4"
    path.write_bytes(b"x" * 1024)
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " event_type, size_bytes, has_gpx, scanned_at) "
            "VALUES (?, 'A.MP4', '2026-06-26', 1, 'F', 1, 'normal', 1024, 0, 1)",
            (str(path),),
        )
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, enqueued_at) "
            "VALUES ('A.MP4', '/DCIM', 'done', 1)",
        )
    r = authed_client.post("/api/queue/delete", json={"filenames": ["A.MP4"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["deleted"] == 1 and body["skipped"] == 1
    assert not path.exists()
    with db.conn() as c:
        assert c.execute(
            "SELECT state FROM download_queue WHERE filename='A.MP4'"
        ).fetchone()["state"] == "skipped"


def test_delete_endpoint_empty_body(authed_client):
    r = authed_client.post("/api/queue/delete", json={})
    assert r.status_code == 200
    assert r.json()["deleted"] == 0 and r.json()["skipped"] == 0


def test_delete_endpoint_force_confirm_through(authed_client, tmp_recordings_dir: Path):
    # First call refuses the RO clip and names it; the forced follow-up
    # (the UI's "delete anyway?" path) removes it.
    db = authed_client.app.state.db
    folder = tmp_recordings_dir / "2026-06-26"
    folder.mkdir(exist_ok=True)
    path = folder / "RO.MP4"
    path.write_bytes(b"x" * 128)
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " event_type, size_bytes, has_gpx, scanned_at) "
            "VALUES (?, 'RO.MP4', '2026-06-26', 1, 'F', 1, 'ro', 128, 0, 1)",
            (str(path),),
        )
    r = authed_client.post("/api/queue/delete", json={"filenames": ["RO.MP4"]})
    body = r.json()
    assert body["deleted"] == 0 and body["protected"] == 1
    assert body["protected_names"] == ["RO.MP4"]
    assert path.exists()
    r = authed_client.post(
        "/api/queue/delete", json={"filenames": ["RO.MP4"], "force": True})
    body = r.json()
    assert body["deleted"] == 1 and body["protected"] == 0
    assert not path.exists()
