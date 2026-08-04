# web/services/derive_worker.py
"""Unified per-clip derive step + the DeriveWorker consumer (later task).

derive_one() runs four idempotent sub-steps against one clip, gated by
the settings snapshot, reusing the existing generators unchanged. It
NEVER raises for a per-clip data problem (corrupt clip, missing file);
those are classified into the returned DeriveResult so the queue layer
decides requeue vs done.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from ..db import Database
from . import derive_queue as dq
from . import durations, filmstrip, thumbs

log = logging.getLogger("viofosync.derive")


@dataclass
class DeriveResult:
    gone: bool = False              # clip file vanished (retention race)
    transient_failed: bool = False  # something worth retrying later


async def derive_one(db: Database, snap, clip: dict) -> DeriveResult:
    """Derive duration + GPS + thumb + filmstrip for one clip dict
    ({id, path, duration_s}). Idempotent; honours the eager toggles."""
    clip_id = clip["id"]
    path = clip["path"]
    if not os.path.isfile(path):
        return DeriveResult(gone=True)

    res = DeriveResult()
    duration = clip.get("duration_s")

    # 1. duration (always) — probe + store if missing.
    if not duration or duration <= 0:
        try:
            duration = await durations.probe_and_store(db, clip_id, path)
        except Exception as e:  # transient probe error
            log.warning("derive: duration probe failed clip=%s: %s", clip_id, e)
            res.transient_failed = True

    # 2. GPS (gated) — inline at download already; worker is the backfill
    #    path. Best-effort, never fatal.
    if getattr(snap, "gps_extract", False):
        try:
            await durations.ensure_gps(db, clip_id, path)
        except Exception as e:  # pragma: no cover - non-fatal
            log.warning("derive: gps failed clip=%s: %s", clip_id, e)

    # 3. thumbnail (gated) — ensure_thumb is idempotent + has its own
    #    failed-marker, so a permanent failure won't re-spawn ffmpeg.
    if snap.derive_thumbs_eager:
        try:
            await thumbs.ensure_thumb(snap.recordings, clip_id, path)
        except Exception as e:
            log.warning("derive: thumb failed clip=%s: %s", clip_id, e)
            res.transient_failed = True

    # 4. filmstrip (gated, needs a known duration) — skip this pass if
    #    duration is still unknown; the clip will be retried.
    if snap.derive_filmstrips_eager:
        if duration and duration > 0:
            try:
                await filmstrip.ensure_filmstrip(
                    snap.recordings, clip_id, path, duration
                )
            except Exception as e:
                log.warning("derive: filmstrip failed clip=%s: %s", clip_id, e)
                res.transient_failed = True
        else:
            res.transient_failed = True  # retry once duration lands

    return res


def enqueue_backfill(db: Database, *, now: int) -> int:
    """Enqueue clips missing a done derive pass, at backfill priority.
    Delegates to derive_queue.enqueue_missing so a re-boot doesn't
    re-derive already-done clips. Returns count enqueued."""
    return dq.enqueue_missing(db, priority=1, now=now)


class DeriveWorker:
    """Long-lived async consumer of derive_queue. One clip at a time;
    reuses derive_one + the generators' own semaphores, so it yields to
    on-demand requests automatically."""

    def __init__(self, db: Database, provider, hub) -> None:
        self.db = db
        self._provider = provider
        self.hub = hub
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task = None
        self._stop = asyncio.Event()
        self._kick = asyncio.Event()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        dq.reconcile_orphans(self.db, now=int(time.time()))
        self._stop.clear()
        self._task = asyncio.run_coroutine_threadsafe(self._run(), self._loop)
        log.info("derive worker started")

    def kick(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._kick.set)
        else:
            self._kick.set()

    async def stop(self) -> None:
        self._stop.set()
        self.kick()
        if self._task is not None:
            import concurrent.futures
            waitable = (asyncio.wrap_future(self._task)
                        if isinstance(self._task, concurrent.futures.Future)
                        else self._task)
            try:
                await asyncio.wait_for(waitable, timeout=10.0)
            except (TimeoutError, asyncio.CancelledError):
                pass

    def _fetch_clip(self, clip_id: int) -> dict | None:
        with self.db.conn() as c:
            row = c.execute(
                "SELECT id, path, duration_s FROM clip_index WHERE id=?",
                (clip_id,),
            ).fetchone()
        return dict(row) if row else None

    async def _run(self) -> None:
        while not self._stop.is_set():
            clip_id = dq.claim_next(self.db, now=int(time.time()))
            if clip_id is None:
                self._kick.clear()
                try:
                    await asyncio.wait_for(self._kick.wait(), timeout=5.0)
                except TimeoutError:
                    pass
                continue
            await self._process(clip_id)

    async def _process(self, clip_id: int) -> None:
        snap = self._provider.get()
        clip = self._fetch_clip(clip_id)
        if clip is None:
            dq.remove_clip(self.db, clip_id)
            return
        started = time.monotonic()
        try:
            res = await derive_one(self.db, snap, clip)
        except Exception as e:  # pragma: no cover - defensive
            log.exception("derive worker: clip %s crashed", clip_id)
            dq.mark_transient_failure(
                self.db, clip_id, str(e),
                max_attempts=getattr(snap, "max_attempts", 3),
                now=int(time.time()),
            )
            return
        now = int(time.time())
        if res.gone:
            dq.remove_clip(self.db, clip_id)
        elif res.transient_failed:
            dq.mark_transient_failure(
                self.db, clip_id, "transient",
                max_attempts=getattr(snap, "max_attempts", 3), now=now,
            )
        else:
            dq.mark_done(self.db, clip_id, now=now)
            log.info(
                "derive: clip %s done in %.1fs",
                clip_id, time.monotonic() - started,
            )
            await self.hub.broadcast({"type": "clip_derived", "clip_id": clip_id})
