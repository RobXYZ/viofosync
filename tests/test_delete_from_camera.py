"""queue.delete_from_camera: dashcam SD-card delete, skipping locked/RO."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _env(tmp_path: Path):
    from web.db import Database
    rec = tmp_path / "rec"
    rec.mkdir()
    return rec, Database(str(rec / "v.db"))


def _q(db, filename, source_dir, *, locked=0, state="pending"):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, locked, "
            "enqueued_at) VALUES (?,?,?,?,1)", (filename, source_dir, state, locked),
        )


def test_delete_from_camera_deletes_and_marks_gone(tmp_path):
    from web.services.queue import delete_from_camera
    rec, db = _env(tmp_path)
    _q(db, "A.MP4", "/DCIM/Movie/A.MP4")
    with patch("web.services.queue.vfs.delete_dashcam_file", return_value=True) as m:
        res = delete_from_camera(db, ["A.MP4"], "http://cam")
    m.assert_called_once_with("http://cam", "/DCIM/Movie/A.MP4", "A.MP4", timeout=10.0)
    assert res == {"deleted": 1, "skipped": 0, "errors": 0}
    with db.conn() as c:
        assert c.execute("SELECT state FROM download_queue WHERE filename='A.MP4'").fetchone()["state"] == "gone"


def test_delete_from_camera_skips_locked_and_ro(tmp_path):
    from web.services.queue import delete_from_camera
    rec, db = _env(tmp_path)
    _q(db, "L.MP4", "/DCIM/Movie/L.MP4", locked=1)
    _q(db, "R.MP4", "/DCIM/Movie/RO/R.MP4")
    with patch("web.services.queue.vfs.delete_dashcam_file", return_value=True) as m:
        res = delete_from_camera(db, ["L.MP4", "R.MP4"], "http://cam")
    m.assert_not_called()
    assert res == {"deleted": 0, "skipped": 2, "errors": 0}


def test_delete_from_camera_skips_ro_in_xml_listing_mode(tmp_path):
    """XML-mode FPATH uses backslashes; RO must still be recognised."""
    from web.services.queue import delete_from_camera
    rec, db = _env(tmp_path)
    _q(db, "R.MP4", "A:\\DCIM\\Movie\\RO\\R.MP4")
    with patch("web.services.queue.vfs.delete_dashcam_file", return_value=True) as m:
        res = delete_from_camera(db, ["R.MP4"], "http://cam")
    m.assert_not_called()
    assert res == {"deleted": 0, "skipped": 1, "errors": 0}


class _Resp:
    """The camera's reply to cmd 4003: Status 0 when it deleted the file."""
    status = 200

    def __init__(self, camera_status=0):
        self.body = (f"<?xml version=\"1.0\" encoding=\"UTF-8\" ?>\n<Function>\n"
                     f"<Cmd>4003</Cmd>\n<Status>{camera_status}</Status>\n</Function>\n"
                     ).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self, n=-1):
        return self.body


@pytest.mark.parametrize("filepath", [
    "/DCIM/Movie/2026_1004_181400_041404F.MP4",        # HTML listing
    "A:\\DCIM\\Movie\\2026_1004_181400_041404F.MP4",   # XML listing
])
def test_delete_from_camera_sends_real_listing_path(tmp_path, filepath):
    """End to end from a listing: reconcile stores Recording.filepath in
    source_dir, and the delete request must carry that clip's path once
    — not ``<path>/<filename>``."""
    import datetime as dt

    from viofosync_lib import Recording
    from web.services import queue as q
    rec, db = _env(tmp_path)
    name = "2026_1004_181400_041404F.MP4"
    q.reconcile(
        db,
        [Recording(name, filepath, 1000, None,
                   dt.datetime(2026, 10, 4, 18, 14, 0), None)],
        present_filenames=[],
    )
    urls = []

    def fake_urlopen(url, timeout=None):
        urls.append(url)
        return _Resp()

    with patch("urllib.request.urlopen", fake_urlopen):
        res = q.delete_from_camera(db, [name], "http://cam")
    assert urls == [
        f"http://cam/?custom=1&cmd=4003&str=/DCIM/Movie/{name}"
    ]
    assert res == {"deleted": 1, "skipped": 0, "errors": 0}


def test_a_delete_the_camera_didnt_make_is_an_error_not_gone(tmp_path):
    """The camera answers 200 either way: only its Status says the file went."""
    from web.services.queue import delete_from_camera
    rec, db = _env(tmp_path)
    _q(db, "A.MP4", "/DCIM/Movie/A.MP4")
    with patch("urllib.request.urlopen", lambda url, timeout=None: _Resp(camera_status=-1)):
        res = delete_from_camera(db, ["A.MP4"], "http://cam")
    assert res == {"deleted": 0, "skipped": 0, "errors": 1}
    with db.conn() as c:
        assert c.execute("SELECT state FROM download_queue WHERE filename='A.MP4'"
                         ).fetchone()["state"] == "pending"


def test_delete_from_camera_import_row_is_an_error_not_gone(tmp_path):
    """A bulk-import row has no camera path; nothing is sent and the row
    is not marked gone."""
    from web.services.queue import delete_from_camera
    rec, db = _env(tmp_path)
    _q(db, "A.MP4", "/import/", state="done")
    with patch("urllib.request.urlopen") as m:
        res = delete_from_camera(db, ["A.MP4"], "http://cam")
    m.assert_not_called()
    assert res == {"deleted": 0, "skipped": 0, "errors": 1}
    with db.conn() as c:
        assert c.execute("SELECT state FROM download_queue WHERE filename='A.MP4'").fetchone()["state"] == "done"


def test_delete_from_camera_counts_errors(tmp_path):
    from web.services.queue import delete_from_camera
    rec, db = _env(tmp_path)
    _q(db, "A.MP4", "/DCIM/Movie/A.MP4")
    with patch("web.services.queue.vfs.delete_dashcam_file", return_value=False):
        res = delete_from_camera(db, ["A.MP4"], "http://cam")
    assert res == {"deleted": 0, "skipped": 0, "errors": 1}
    with db.conn() as c:
        assert c.execute("SELECT state FROM download_queue WHERE filename='A.MP4'").fetchone()["state"] == "pending"


def test_delete_from_camera_empty(tmp_path):
    from web.services.queue import delete_from_camera
    rec, db = _env(tmp_path)
    assert delete_from_camera(db, [], "http://cam") == {"deleted": 0, "skipped": 0, "errors": 0}


# ── endpoint tests ──────────────────────────────────────────────────────────

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


def test_delete_from_camera_endpoint_no_address(authed_client, monkeypatch):
    """Returns ok:False/error when no dashcam address is configured."""
    import dataclasses
    snap = authed_client.app.state.settings_provider.get()
    no_addr_snap = dataclasses.replace(snap, address=None)
    monkeypatch.setattr(
        authed_client.app.state.settings_provider, "get", lambda: no_addr_snap
    )
    r = authed_client.post(
        "/api/queue/delete-from-camera", json={"filenames": ["A.MP4"]}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert "error" in data


def test_delete_from_camera_endpoint_success(authed_client):
    """With address configured + mocked vfs, returns ok:True deleted:1."""
    db = authed_client.app.state.db
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, locked, enqueued_at) "
            "VALUES ('A.MP4', '/DCIM/Movie/A.MP4', 'pending', 0, 1)"
        )
    with patch("web.services.queue.vfs.delete_dashcam_file", return_value=True):
        r = authed_client.post(
            "/api/queue/delete-from-camera", json={"filenames": ["A.MP4"]}
        )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["deleted"] == 1
    assert data["skipped"] == 0
    assert data["errors"] == 0
