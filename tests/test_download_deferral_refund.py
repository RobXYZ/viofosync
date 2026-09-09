"""The active-recording overage deferral must NOT burn a download attempt.

When download_file detects the file is still growing (streamed bytes exceed the
expected size) it raises DownloadDeferred; the sync worker must treat that like
a cancellation — return the item to pending with its attempt refunded — so a
clip the completeness guard missed can never exhaust max_attempts and get stuck
permanently 'failed'."""
from __future__ import annotations

import time

import viofosync_lib as vfs
from web.db import Database
from web.services import queue as q
from web.services.hub import Hub
from web.services.sync_worker import SyncWorker


class _Snap:
    def __init__(self, recordings):
        self.recordings = recordings
        self.grouping = "daily"
        self.gps_extract = False
        self.gps_triage = False
        self.primary_scope = "everything"
        self.primary_gps_triage = False
        self.alternative_scope = "everything"
        self.alternative_gps_triage = False
        self.delete_after_download = False
        self.download_attempts = 3
        self.max_attempts = 3
        self.timeout = 5


class _Provider:
    def __init__(self, snap):
        self._snap = snap

    def get(self):
        return self._snap


def _seed(db, filename):
    now = int(time.time())
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, camera, event_type, state, enqueued_at, "
            " remote_complete) VALUES (?,?,?,?,?,?,?)",
            (filename, "/DCIM", filename[-5], "normal", "pending", now, 1),
        )


def _row(db, filename):
    with db.conn() as c:
        return dict(c.execute(
            "SELECT * FROM download_queue WHERE filename=?", (filename,)
        ).fetchone())


async def test_deferral_refunds_attempt(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    rec.mkdir()
    snap = _Snap(str(rec))
    sw = SyncWorker(db, _Provider(snap), Hub())
    sw._active_address = "1.2.3.4"

    _seed(db, "2026_0628_133416_0002F.MP4")
    item = q.next_pending(db)
    assert item is not None

    # Simulate download_file detecting the still-growing active file.
    def _deferred(*a, **k):
        raise vfs.DownloadDeferred("file still growing: streamed 3145728 > 60")

    monkeypatch.setattr(vfs, "download_file_with", _deferred)

    ok = await sw._download_one(item)
    assert ok is False

    after = _row(db, "2026_0628_133416_0002F.MP4")
    # Refunded: back to pending, attempt NOT burned (mirrors cancellation).
    assert after["state"] == "pending"
    assert after["attempts"] == 0
