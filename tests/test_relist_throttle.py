"""Mid-drain listing-refresh throttle.

Cameras that are slow to report their file listing can take longer
to list than a short clip takes to download, so refreshing after
*every* download stalls the drain. The worker now relists mid-drain
only when the adaptive interval — scaled to the measured cost of
the last refresh — has elapsed. The cycle-start listing is
deliberately not throttled.
"""
from __future__ import annotations

import datetime as _dt
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from web.db import Database
from web.services.hub import Hub
from web.services.sync_worker import (
    RELIST_COST_FACTOR,
    RELIST_MAX_INTERVAL_S,
    RELIST_MIN_INTERVAL_S,
    SyncWorker,
    relist_interval,
)


class _Rec:
    """Minimal Recording stand-in — only the fields reconcile reads."""

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.filepath = "/DCIM/Movie"
        self.size = 1000
        self.datetime = _dt.datetime(2026, 8, 13, 12, 0, 0)


def _make_snap():
    snap = MagicMock()
    snap.address = "192.168.1.230"
    snap.use_html_listing = True
    snap.grouping = "daily"
    snap.recordings = "/tmp"
    snap.gps_triage = False
    snap.primary_scope = "everything"
    snap.primary_gps_triage = False
    snap.alternative_scope = "everything"
    snap.alternative_gps_triage = False
    return snap


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / ".viofosync.db"))


# ---- interval math ----

def test_interval_floor_for_fast_cameras() -> None:
    # A healthy camera lists in ~1-3s; the floor governs so the queue
    # still refreshes regularly mid-drain.
    assert relist_interval(0.0) == RELIST_MIN_INTERVAL_S
    assert relist_interval(1.3) == RELIST_MIN_INTERVAL_S


def test_interval_scales_with_listing_cost() -> None:
    # A slow camera: ~40s per listing -> ~400s between relists,
    # bounding listing overhead at ~1/RELIST_COST_FACTOR of drain time.
    assert relist_interval(40.0) == 40.0 * RELIST_COST_FACTOR


def test_interval_ceiling_for_pathological_cameras() -> None:
    assert relist_interval(120.0) == RELIST_MAX_INTERVAL_S


# ---- due predicate ----

def _worker(db: Database) -> SyncWorker:
    provider = MagicMock()
    provider.get.return_value = _make_snap()
    return SyncWorker(db, provider, Hub())


def test_fresh_worker_is_due(db: Database) -> None:
    assert _worker(db)._relist_due() is True


def test_not_due_inside_window(db: Database) -> None:
    sw = _worker(db)
    sw._last_relist_done = time.monotonic()
    sw._last_relist_cost_s = 40.0
    assert sw._relist_due() is False


def test_due_after_window_elapses(db: Database) -> None:
    sw = _worker(db)
    sw._last_relist_cost_s = 40.0
    sw._last_relist_done = (
        time.monotonic() - relist_interval(40.0) - 1.0
    )
    assert sw._relist_due() is True


# ---- the refresh stamps its own cost ----

async def test_refresh_stamps_time_and_cost_on_success(
    db: Database,
) -> None:
    sw = _worker(db)
    with patch.object(sw, "_fetch_listing", return_value=[_Rec("A.MP4")]), \
         patch.object(sw, "_present_filenames", return_value=[]):
        ok = await sw._refresh_listing_and_reconcile()
    assert ok is True
    assert sw._last_relist_done is not None
    assert time.monotonic() - sw._last_relist_done < 5.0
    assert sw._last_relist_cost_s >= 0.0


async def test_refresh_stamps_time_and_cost_on_failure(
    db: Database,
) -> None:
    """A camera that takes 40s to *fail* the listing must not be
    re-listed in a tight loop either — failures stamp too."""
    sw = _worker(db)

    def _boom():
        raise OSError("dashcam wedged")

    with patch.object(sw, "_fetch_listing", side_effect=_boom):
        ok = await sw._refresh_listing_and_reconcile()
    assert ok is False
    assert sw._last_relist_done is not None
    assert sw._last_relist_cost_s >= 0.0


# ---- drain integration ----

def _seed_pending(db: Database, *filenames: str) -> None:
    with db.write() as c:
        for i, fn in enumerate(filenames):
            c.execute(
                "INSERT INTO download_queue "
                "(filename, source_dir, state, enqueued_at, attempts, "
                " remote_size, recorded_at, remote_complete) "
                "VALUES (?, ?, 'pending', ?, 0, 1000, ?, 1)",
                (fn, "/DCIM/Movie", i, int(time.time()) - 10_000 + i),
            )


def _stub_cycle(sw: SyncWorker, monkeypatch, *, relist_calls: list) -> None:
    """Stub everything _cycle does around the drain, counting calls to
    _refresh_listing_and_reconcile and simulating a slow camera by
    stamping a 40s cost (as the real helper does)."""

    async def _noop(*a, **k):
        return None

    async def _true(*a, **k):
        return True

    async def _addr(*a, **k):
        return ("1.2.3.4", "primary")

    async def _relist(*a, **k):
        relist_calls.append(time.monotonic())
        sw._last_relist_done = time.monotonic()
        sw._last_relist_cost_s = 40.0
        return True

    async def _download_ok(item):
        from web.services import queue as q
        q.mark_downloading(sw.db, item.id)
        with sw.db.write() as c:
            c.execute(
                "UPDATE download_queue SET state='done' WHERE id=?",
                (item.id,),
            )
        return True

    monkeypatch.setattr(sw, "_emit_disk_pct", _noop)
    monkeypatch.setattr(sw, "_check_recordings_writable", _true)
    monkeypatch.setattr(sw, "_select_active_address", _addr)
    monkeypatch.setattr(sw, "_refresh_listing_and_reconcile", _relist)
    monkeypatch.setattr(sw, "_run_recording_status_pass", _noop)
    monkeypatch.setattr(sw, "_run_triage_pass", _noop)
    monkeypatch.setattr(sw, "_run_geofence_pass", _noop)
    monkeypatch.setattr(sw, "_probe_one", _true)
    monkeypatch.setattr(sw, "_download_one", _download_ok)
    # Skip the post-drain scan/retention (filesystem-heavy, irrelevant).
    from web.services import sync_worker as sw_mod
    monkeypatch.setattr(
        sw_mod.scanner, "scan", lambda *a, **k: None
    )
    monkeypatch.setattr(
        sw_mod._retention, "sweep", lambda *a, **k: None
    )
    monkeypatch.setattr(
        sw_mod._retention, "import_exclude_set", lambda *a, **k: set()
    )
    monkeypatch.setattr(
        sw_mod._retention, "filesystem_used_pct", lambda *a, **k: None
    )
    sw_mod_exporter = __import__(
        "web.services.exporter", fromlist=["export_protect_ids"]
    )
    monkeypatch.setattr(
        sw_mod_exporter, "export_protect_ids", lambda *a, **k: set()
    )


async def test_slow_camera_drain_relists_only_at_cycle_start(
    db: Database, monkeypatch,
) -> None:
    """Three short clips on a 40s-listing camera: the drain must not
    pay the listing tax between each — one listing at cycle start,
    none mid-drain (the interval hasn't elapsed)."""
    _seed_pending(db, "A.MP4", "B.MP4", "C.MP4")
    provider = MagicMock()
    provider.get.return_value = _make_snap()
    sw = SyncWorker(db, provider, Hub())

    relist_calls: list = []
    _stub_cycle(sw, monkeypatch, relist_calls=relist_calls)

    did = await sw._cycle()

    assert did is True
    with db.conn() as c:
        states = [
            r["state"] for r in
            c.execute("SELECT state FROM download_queue").fetchall()
        ]
    assert states == ["done", "done", "done"]
    assert len(relist_calls) == 1, (
        "mid-drain relists should be throttled on a slow camera"
    )


async def test_drain_still_relists_when_interval_elapsed(
    db: Database, monkeypatch,
) -> None:
    """The mid-drain-freshness behaviour survives the throttle: once
    the adaptive interval has passed, the relist runs between
    downloads again."""
    _seed_pending(db, "A.MP4", "B.MP4", "C.MP4")
    provider = MagicMock()
    provider.get.return_value = _make_snap()
    sw = SyncWorker(db, provider, Hub())

    relist_calls: list = []
    _stub_cycle(sw, monkeypatch, relist_calls=relist_calls)
    monkeypatch.setattr(sw, "_relist_due", lambda: True)

    did = await sw._cycle()

    assert did is True
    # Cycle-start + after each of the three successful downloads.
    assert len(relist_calls) == 4
