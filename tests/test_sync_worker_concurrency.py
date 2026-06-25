"""SyncWorker parallel download drain."""
from __future__ import annotations

import asyncio
import time
import types

from web.db import Database
from web.services import queue as q
from web.services import sync_worker as sw_mod
from web.services.sync_worker import SyncWorker


class _Hub:
    def __init__(self):
        self.events = []

    async def broadcast(self, event):
        self.events.append(event)

    def schedule_broadcast(self, loop, event):
        self.events.append(event)


def _add_pending(db: Database, filename: str, enq: int) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, remote_size, "
            "recorded_at) VALUES (?, ?, 'pending', ?, ?, ?)",
            (filename, "/DCIM/Movie", enq, 1024, int(time.time())),
        )


async def test_cycle_runs_downloads_up_to_configured_concurrency(
    tmp_path,
    monkeypatch,
):
    db = Database(str(tmp_path / "v.db"))
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    for idx in range(3):
        _add_pending(db, f"{idx}.MP4", idx)

    snap = types.SimpleNamespace(
        download_concurrency=2,
        sync_ro_only=False,
        recordings=str(rec_dir),
        grouping="none",
        retention_max_days=0,
        retention_disk_pct=0,
        retention_protect_ro=True,
        recordings_quota_gb=0,
        import_path="",
    )
    worker = SyncWorker(db, types.SimpleNamespace(get=lambda: snap), _Hub())
    worker._active_address = "192.0.2.10"

    async def yes(*args, **kwargs):
        return True

    async def active_address():
        return "192.0.2.10", "primary"

    worker._emit_disk_pct = yes
    worker._check_recordings_writable = yes
    worker._select_active_address = active_address
    worker._refresh_listing_and_reconcile = yes
    worker._probe_one = yes

    monkeypatch.setattr(sw_mod.scanner, "scan", lambda *a, **k: 0)
    monkeypatch.setattr(sw_mod._retention, "sweep", lambda *a, **k: None)

    active = 0
    max_active = 0
    seen = []

    async def fake_download(item, **kwargs):
        nonlocal active, max_active
        seen.append(item.filename)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        q.mark_done(db, item.id)
        active -= 1
        return True

    worker._download_one = fake_download

    did = await worker._cycle()

    assert did is True
    assert max_active == 2
    assert sorted(seen) == ["0.MP4", "1.MP4", "2.MP4"]


async def test_cycle_refills_slot_as_soon_as_download_finishes(
    tmp_path,
    monkeypatch,
):
    db = Database(str(tmp_path / "v.db"))
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    for idx in range(5):
        _add_pending(db, f"{idx}.MP4", idx)

    snap = types.SimpleNamespace(
        download_concurrency=4,
        sync_ro_only=False,
        recordings=str(rec_dir),
        grouping="none",
        retention_max_days=0,
        retention_disk_pct=0,
        retention_protect_ro=True,
        recordings_quota_gb=0,
        import_path="",
    )
    worker = SyncWorker(db, types.SimpleNamespace(get=lambda: snap), _Hub())
    worker._active_address = "192.0.2.10"

    async def yes(*args, **kwargs):
        return True

    async def active_address():
        return "192.0.2.10", "primary"

    worker._emit_disk_pct = yes
    worker._check_recordings_writable = yes
    worker._select_active_address = active_address
    worker._refresh_listing_and_reconcile = yes
    worker._probe_one = yes

    monkeypatch.setattr(sw_mod.scanner, "scan", lambda *a, **k: 0)
    monkeypatch.setattr(sw_mod._retention, "sweep", lambda *a, **k: None)

    fifth_started = asyncio.Event()
    seen = []

    async def fake_download(item, **kwargs):
        seen.append(item.filename)
        if item.filename == "0.MP4":
            await asyncio.sleep(0.01)
        elif item.filename in {"1.MP4", "2.MP4", "3.MP4"}:
            await asyncio.wait_for(fifth_started.wait(), timeout=1)
        elif item.filename == "4.MP4":
            fifth_started.set()
        q.mark_done(db, item.id)
        return True

    worker._download_one = fake_download

    did = await worker._cycle()

    assert did is True
    assert seen[:4] == ["0.MP4", "1.MP4", "2.MP4", "3.MP4"]
    assert "4.MP4" in seen
