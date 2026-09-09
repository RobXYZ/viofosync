"""Tests for SyncWorker._refresh_listing_and_reconcile.

Pinning the regression: during a long sync drain the queue used
to only refresh once per cycle, so clips the dashcam recorded
mid-drain didn't appear in the UI's queue until the cycle
ended. The helper is now called between successful downloads to
keep the queue current — throttled by the adaptive interval in
test_relist_throttle.py so cameras that are slow to list don't
pay a listing tax per download.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from web.db import Database
from web.services.hub import Hub
from web.services.sync_worker import SyncWorker


class _Rec:
    """Minimal Recording stand-in — only the fields reconcile reads."""

    def __init__(
        self, filename: str, *,
        filepath: str = "/DCIM/Movie",
        size: int = 1000,
    ) -> None:
        self.filename = filename
        self.filepath = filepath
        self.size = size
        self.datetime = _dt.datetime(2026, 5, 8, 12, 0, 0)


def _make_snap():
    snap = MagicMock()
    snap.address = "192.168.1.230"
    snap.use_html_listing = True
    snap.grouping = "daily"
    snap.recordings = "/tmp"
    return snap


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / ".viofosync.db"))


def _queue_rows(db: Database) -> list[tuple[str, str]]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT filename, state FROM download_queue "
            "ORDER BY filename"
        ).fetchall()
    return [(r["filename"], r["state"]) for r in rows]


# ---- happy path ----

async def test_refresh_enqueues_new_clips(db: Database) -> None:
    provider = MagicMock()
    provider.get.return_value = _make_snap()
    hub = Hub()  # no clients connected; broadcast is a no-op
    worker = SyncWorker(db, provider, hub)

    listing = [_Rec("A.MP4"), _Rec("B.MP4")]
    with patch.object(worker, "_fetch_listing", return_value=listing), \
         patch.object(worker, "_present_filenames", return_value=[]):
        ok = await worker._refresh_listing_and_reconcile()

    assert ok is True
    assert _queue_rows(db) == [
        ("A.MP4", "pending"),
        ("B.MP4", "pending"),
    ]


async def test_refresh_picks_up_clips_added_between_calls(
    db: Database,
) -> None:
    """The actual scenario: a sync is running, the user/dashcam
    is recording, and a new clip lands on the SD card during the
    sync. The second call sees it."""
    provider = MagicMock()
    provider.get.return_value = _make_snap()
    hub = Hub()
    worker = SyncWorker(db, provider, hub)

    # First listing: only A
    with patch.object(worker, "_fetch_listing",
                      return_value=[_Rec("A.MP4")]), \
         patch.object(worker, "_present_filenames", return_value=[]):
        await worker._refresh_listing_and_reconcile()
    assert _queue_rows(db) == [("A.MP4", "pending")]

    # ...time passes, dashcam records B...

    # Second listing: A and B. B is new, A is unchanged.
    with patch.object(worker, "_fetch_listing",
                      return_value=[_Rec("A.MP4"), _Rec("B.MP4")]), \
         patch.object(worker, "_present_filenames", return_value=[]):
        await worker._refresh_listing_and_reconcile()
    assert _queue_rows(db) == [
        ("A.MP4", "pending"),
        ("B.MP4", "pending"),
    ]


async def test_refresh_updates_source_dir_when_clip_moves_to_ro(
    db: Database,
) -> None:
    """If the user locks a clip mid-cycle the dashcam moves it
    from /DCIM/Movie to /DCIM/Movie/RO. The next reconcile must
    refresh source_dir on the existing row, otherwise the
    download worker keeps hitting the stale path and 404s out
    its retry budget."""
    provider = MagicMock()
    provider.get.return_value = _make_snap()
    hub = Hub()
    worker = SyncWorker(db, provider, hub)

    with patch.object(worker, "_fetch_listing",
                      return_value=[_Rec("A.MP4", filepath="/DCIM/Movie")]), \
         patch.object(worker, "_present_filenames", return_value=[]):
        await worker._refresh_listing_and_reconcile()

    with db.conn() as c:
        row = c.execute(
            "SELECT source_dir FROM download_queue WHERE filename='A.MP4'"
        ).fetchone()
    assert row["source_dir"] == "/DCIM/Movie"

    # User locks the clip; dashcam re-reports it under /RO.
    with patch.object(worker, "_fetch_listing",
                      return_value=[_Rec("A.MP4", filepath="/DCIM/Movie/RO")]), \
         patch.object(worker, "_present_filenames", return_value=[]):
        await worker._refresh_listing_and_reconcile()

    with db.conn() as c:
        row = c.execute(
            "SELECT source_dir FROM download_queue WHERE filename='A.MP4'"
        ).fetchone()
    assert row["source_dir"] == "/DCIM/Movie/RO"


# ---- failure handling ----

async def test_refresh_returns_false_on_listing_exception(
    db: Database,
) -> None:
    """Listing failures must be soft — caller decides what to do
    with the False result. Critically, no exception leaks out so
    the surrounding drain loop can keep running."""
    provider = MagicMock()
    provider.get.return_value = _make_snap()
    hub = Hub()
    worker = SyncWorker(db, provider, hub)

    def _boom():
        raise OSError("dashcam unreachable")

    with patch.object(worker, "_fetch_listing", side_effect=_boom):
        ok = await worker._refresh_listing_and_reconcile()
    assert ok is False
    # Queue is untouched.
    assert _queue_rows(db) == []


# ---- event-loop responsiveness ----

async def test_refresh_does_not_block_event_loop(db: Database) -> None:
    """The NAS directory walk + queue reconcile are blocking I/O and
    must run off the loop — a slow recordings volume used to freeze
    every HTTP request and WebSocket for its duration."""
    import asyncio
    import time

    provider = MagicMock()
    provider.get.return_value = _make_snap()
    worker = SyncWorker(db, provider, Hub())

    def _slow_present():
        time.sleep(0.3)  # simulate a slow NAS walk
        return []

    ticks = 0

    async def _ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.create_task(_ticker())
    try:
        with patch.object(worker, "_fetch_listing", return_value=[_Rec("A.MP4")]), \
             patch.object(worker, "_present_filenames", side_effect=_slow_present):
            await worker._refresh_listing_and_reconcile()
    finally:
        t.cancel()

    # With the walk on the loop the ticker barely runs (~0-2 ticks);
    # off the loop it accumulates ~15. Threshold splits them cleanly.
    assert ticks >= 5, f"event loop starved during reconcile ({ticks} ticks)"
    assert _queue_rows(db) == [("A.MP4", "pending")]
