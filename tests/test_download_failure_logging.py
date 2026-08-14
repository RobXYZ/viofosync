"""Failed downloads must leave a trace in the app log.

Exceptions raised inside _download_one's _blocking helper before
viofosync_lib's own logging (an unwritable group dir, makedirs/mkstemp
OSErrors) used to surface only as last_error on the queue row — a user
whose every download failed saw zero log lines for days. The worker now
warns on the failure branch, deduplicated per drain: the first occurrence
of each distinct error logs the details, repeats are counted and
summarised when the drain ends.
"""
from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

import viofosync_lib as vfs
from web.db import Database
from web.services import queue as q
from web.services.hub import Hub
from web.services.sync_worker import SyncWorker

LOGGER = "viofosync.sync_worker"


class _Snap:
    def __init__(self, recordings):
        self.recordings = recordings
        self.grouping = "daily"
        self.gps_extract = False
        self.gps_triage = False
        self.delete_after_download = False
        self.download_attempts = 3
        self.max_attempts = 3
        self.timeout = 5
        self.sync_ro_only = False
        self.use_html_listing = True
        # Post-drain retention sweep args (the sweep itself is stubbed
        # in the _cycle test, but its arguments are still evaluated).
        self.retention_max_days = None
        self.retention_disk_pct = None
        self.retention_protect_ro = False
        self.recordings_quota_gb = None
        self.import_path = None


class _Provider:
    def __init__(self, snap):
        self._snap = snap

    def get(self):
        return self._snap


def _seed(db, *filenames):
    now = int(time.time())
    with db.write() as c:
        for i, fn in enumerate(filenames):
            c.execute(
                "INSERT INTO download_queue "
                "(filename, source_dir, camera, event_type, state, "
                " enqueued_at, remote_complete) VALUES (?,?,?,?,?,?,?)",
                (fn, "/DCIM/Movie", fn[-5], "normal", "pending", now + i, 1),
            )


def _worker(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    rec.mkdir()
    sw = SyncWorker(db, _Provider(_Snap(str(rec))), Hub())
    sw._active_address = "1.2.3.4"
    return db, sw


def _warnings(caplog):
    return [
        r.getMessage() for r in caplog.records
        if r.name == LOGGER and r.levelno >= logging.WARNING
    ]


def _fail_with(monkeypatch, message):
    def _boom(*a, **k):
        raise RuntimeError(message)

    monkeypatch.setattr(vfs, "download_file_with", _boom)


async def test_failure_logs_warning_with_details(
    tmp_path, monkeypatch, caplog,
):
    db, sw = _worker(tmp_path)
    _seed(db, "2026_0628_133416_0002F.MP4")
    _fail_with(monkeypatch, "Group dir not writable: /rec/2026-06-28")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        ok = await sw._download_one(q.next_pending(db))

    assert ok is False
    msgs = _warnings(caplog)
    assert len(msgs) == 1
    assert "2026_0628_133416_0002F.MP4" in msgs[0]
    assert "pending" in msgs[0]  # new_state: first of three attempts
    assert "attempt 1" in msgs[0]
    assert "Group dir not writable: /rec/2026-06-28" in msgs[0]


async def test_same_error_logged_once_per_drain(
    tmp_path, monkeypatch, caplog,
):
    db, sw = _worker(tmp_path)
    _seed(db, "2026_0628_133416_0002F.MP4", "2026_0628_133416_0002R.MP4")
    _fail_with(monkeypatch, "Group dir not writable: /rec/2026-06-28")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await sw._download_one(q.next_pending(db))
        await sw._download_one(q.next_pending(db))

    assert len(_warnings(caplog)) == 1


async def test_distinct_errors_each_logged(tmp_path, monkeypatch, caplog):
    db, sw = _worker(tmp_path)
    _seed(db, "2026_0628_133416_0002F.MP4", "2026_0628_133416_0002R.MP4")

    errors = iter(["first error", "second error"])

    def _boom(*a, **k):
        raise RuntimeError(next(errors))

    monkeypatch.setattr(vfs, "download_file_with", _boom)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await sw._download_one(q.next_pending(db))
        await sw._download_one(q.next_pending(db))

    msgs = _warnings(caplog)
    assert len(msgs) == 2
    assert "first error" in msgs[0]
    assert "second error" in msgs[1]


async def test_cycle_summarises_suppressed_repeats(
    tmp_path, monkeypatch, caplog,
):
    """One item, three attempts, same error: the drain logs the first
    occurrence with details, then one end-of-drain summary counting the
    two suppressed repeats."""
    db, sw = _worker(tmp_path)
    _seed(db, "2026_0628_133416_0002F.MP4")
    _fail_with(monkeypatch, "Group dir not writable: /rec/2026-06-28")

    async def _noop(*a, **k):
        return None

    async def _true(*a, **k):
        return True

    async def _addr(*a, **k):
        return ("1.2.3.4", "primary")

    monkeypatch.setattr(sw, "_emit_disk_pct", _noop)
    monkeypatch.setattr(sw, "_check_recordings_writable", _true)
    monkeypatch.setattr(sw, "_select_active_address", _addr)
    monkeypatch.setattr(sw, "_refresh_listing_and_reconcile", _true)
    monkeypatch.setattr(sw, "_run_recording_status_pass", _noop)
    monkeypatch.setattr(sw, "_run_triage_pass", _noop)
    monkeypatch.setattr(sw, "_run_geofence_pass", _noop)
    monkeypatch.setattr(sw, "_probe_one", _true)
    from web.services import sync_worker as sw_mod
    monkeypatch.setattr(sw_mod.scanner, "scan", MagicMock())
    monkeypatch.setattr(sw_mod._retention, "sweep", MagicMock())
    monkeypatch.setattr(
        sw_mod._retention, "import_exclude_set", MagicMock(return_value=set())
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await sw._cycle()

    with db.conn() as c:
        row = c.execute("SELECT state, attempts FROM download_queue").fetchone()
    assert row["state"] == "failed" and row["attempts"] == 3

    msgs = _warnings(caplog)
    assert len(msgs) == 2, msgs
    assert "2026_0628_133416_0002F.MP4" in msgs[0]
    assert "2 more" in msgs[1]
    assert "Group dir not writable: /rec/2026-06-28" in msgs[1]


async def test_dedup_resets_between_drains(tmp_path, monkeypatch, caplog):
    """The suppression is per drain: after the summary flush, the same
    error logs its details again."""
    db, sw = _worker(tmp_path)
    _seed(db, "2026_0628_133416_0002F.MP4", "2026_0628_133416_0002R.MP4")
    _fail_with(monkeypatch, "Group dir not writable: /rec/2026-06-28")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await sw._download_one(q.next_pending(db))
        sw._flush_drain_failure_summary()
        await sw._download_one(q.next_pending(db))

    # First occurrence, no-repeat flush (summary only fires for repeats),
    # then a fresh first occurrence in the next drain.
    msgs = _warnings(caplog)
    assert len(msgs) == 2
    assert all("2 more" not in m for m in msgs)
