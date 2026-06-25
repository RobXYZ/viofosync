"""SyncWorker — the dashcam download background task.

The car can drive away at any moment, so this worker is built
around the assumption that the dashcam is flaky:

* every cycle starts with a short reachability probe; failure
  is a no-op (not an error) and we back off exponentially;
* a single in-flight download holds the worker; the download's
  HTTP read loop polls ``cancel_check`` so we can abort within
  one chunk when the probe fails or the user hits Stop;
* transient socket errors return the item to ``pending`` with
  an incremented attempt counter, not ``failed``;
* every event (reachability change, queue mutation, per-item
  progress) is pushed to the WebSocket hub so the UI sees live
  state without polling.

The actual downloading is delegated to
``viofosync_lib.download_file`` so we share the retry/verify
logic with the CLI.
"""

from __future__ import annotations

import asyncio
import errno as _errno
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

import viofosync_lib as vfs

from ..db import Database
from ..settings import SettingsProvider
from . import queue as q
from . import retention as _retention
from . import scanner
from .hub import Hub

log = logging.getLogger("viofosync.sync_worker")

BACKOFF_STEPS = [10, 30, 120, 600]  # seconds

# How often the standalone retention loop enforces the archive caps,
# independent of whether anything was downloaded. The download cycle
# still sweeps immediately after a drain; this loop is what keeps the
# archive bounded when the camera is offline or has nothing new.
RETENTION_INTERVAL_SECONDS = 300  # 5 minutes

# Upper bound on how long stop() waits for the cycle/retention tasks
# before abandoning them — keeps SIGTERM shutdown bounded even when a
# cycle is wedged in an uncancellable executor call.
STOP_TIMEOUT = 10.0

# kv key for the persisted pause toggle ("1" = paused). Survives a
# restart so the worker resumes in the state the user last left it.
_PAUSED_KV_KEY = "sync_paused"


class DiskFullError(Exception):
    """Raised by _download_one on ENOSPC: the recordings volume is
    full, so the drain loop must stop instead of marching the rest of
    the queue through their retry budgets."""


def _path_is_writable(path: str) -> bool:
    """Return True if *path* is a directory we can actually write to.

    We probe with a real create-and-delete rather than ``os.access(W_OK)``:
    on NFS (and other networked filesystems) ``os.access`` checks the cached
    owner/mode against the local UID and can report a perfectly writable
    export as non-writable, while the real write is accepted by the server's
    own permission mapping. A live probe is the only reliable test."""
    if not (path and os.path.isdir(path)):
        return False
    probe = os.path.join(path, f".viofosync-writetest-{os.getpid()}")
    try:
        with open(probe, "w"):
            pass
    except OSError:
        return False
    try:
        os.unlink(probe)
    except OSError:
        pass
    return True


def _filter_ro_only(listing):
    """Yield only Recordings whose dashcam source path lies under
    /RO/. Used when the user has 'Sync read-only files only' on."""
    for r in listing:
        fp = (getattr(r, "filepath", None) or "").upper()
        if "/RO/" in fp or fp.endswith("/RO"):
            yield r


def _should_delete_after_download(
    item,
    *,
    dest_path: str,
    delete_enabled: bool,
    local_size: int,
    local_exists: bool,
) -> tuple[bool, str]:
    """The three-guard decision. Pure function — no side effects.
    Returns ``(ok_to_delete, reason)`` where reason is one of
    ``setting_off`` / ``locked`` / ``local_missing`` /
    ``size_mismatch`` / ``ok``.
    """
    if not delete_enabled:
        return False, "setting_off"
    src = item.source_dir or ""
    if "/RO/" in src or src.endswith("/RO"):
        return False, "locked"
    if not local_exists:
        return False, "local_missing"
    if not item.remote_size or local_size != item.remote_size:
        return False, "size_mismatch"
    return True, "ok"


def _maybe_delete_from_dashcam(
    *,
    item,
    dest_path: str,
    delete_enabled: bool,
    base_url: str,
    sink: "WebSink | None" = None,
) -> None:
    """Apply the three-guard check and, if all pass, ask the
    dashcam to delete the clip. Failure-to-delete is logged at
    WARNING and never raised — the caller has already marked the
    queue row done.

    When ``sink`` is provided, broadcasts a ``dashcam_delete``
    event with the outcome. Only fires when delete is enabled
    (``setting_off`` stays silent so we don't spam the log for
    users who never opted in)."""
    try:
        local_exists = os.path.exists(dest_path)
        local_size = os.path.getsize(dest_path) if local_exists else 0
    except OSError:
        local_exists, local_size = False, 0
    ok, reason = _should_delete_after_download(
        item,
        dest_path=dest_path,
        delete_enabled=delete_enabled,
        local_size=local_size,
        local_exists=local_exists,
    )
    if not ok:
        if reason == "locked":
            log.info("not deleting %s from dashcam: locked", item.filename)
        elif reason == "local_missing":
            log.warning(
                "not deleting %s from dashcam: local file missing",
                item.filename,
            )
        elif reason == "size_mismatch":
            log.warning(
                "not deleting %s from dashcam: size mismatch "
                "(local=%s, dashcam=%s)",
                item.filename, local_size, item.remote_size,
            )
        if sink is not None and reason != "setting_off":
            sink.dashcam_delete(
                item.filename, ok=False, reason=reason,
                local_size=local_size,
                remote_size=item.remote_size,
            )
        return

    success = vfs.delete_dashcam_file(
        base_url, item.source_dir, item.filename
    )
    if success:
        log.info("deleted %s from dashcam", item.filename)
    else:
        log.warning("dashcam delete failed for %s", item.filename)
    if sink is not None:
        sink.dashcam_delete(
            item.filename,
            ok=success,
            reason="ok" if success else "request_failed",
            local_size=local_size,
            remote_size=item.remote_size,
        )


def _refresh_queue_size(db, item, dest_path: str) -> None:
    """After a successful download, replace the queue row's
    ``remote_size`` with the actual on-disk size.

    The HTML directory listing reports sizes rounded to MB
    precision (`"102.00 MB"` → 102 << 20 bytes), which makes
    strict equality against the byte-precise local file useless.
    The download path itself uses HEAD to fetch the exact size
    and verifies it during streaming, so the on-disk file is the
    authoritative size — adopt it as truth for downstream checks
    (the auto-delete guard, the queue's MB stats) and to fix the
    persisted row for the next cycle.
    """
    try:
        actual = os.path.getsize(dest_path)
    except OSError:
        return
    with db.write() as c:
        c.execute(
            "UPDATE download_queue SET remote_size=? WHERE id=?",
            (actual, item.id),
        )
    item.remote_size = actual


class WebSink(vfs.ProgressSink):
    """Bridges the downloader (running on a worker thread) to
    the asyncio hub on the main loop.

    All calls are synchronous and thread-safe because they use
    :meth:`Hub.schedule_broadcast` which goes through
    ``asyncio.run_coroutine_threadsafe``.
    """

    def __init__(
        self,
        hub: Hub,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.hub = hub
        self.loop = loop

    def _send(self, event: dict) -> None:
        self.hub.schedule_broadcast(self.loop, event)

    def queue_set(self, filenames):
        self._send({
            "type": "queue_set",
            "filenames": list(filenames),
        })

    def item_started(self, filename, total_bytes):
        self._send({
            "type": "item_started",
            "filename": filename,
            "total": total_bytes,
        })

    def item_progress(self, filename, bytes_done, total, speed):
        self._send({
            "type": "item_progress",
            "filename": filename,
            "bytes": bytes_done,
            "total": total,
            "speed": speed,
        })

    def item_finished(self, filename, ok, err, bytes_written):
        self._send({
            "type": "item_finished",
            "filename": filename,
            "ok": ok,
            "error": err,
            "bytes": bytes_written,
        })

    def sync_done(self, ok, err):
        self._send({"type": "sync_done", "ok": ok, "error": err})

    def dashcam_delete(
        self, filename, *, ok, reason,
        local_size=None, remote_size=None,
    ):
        ev = {
            "type": "dashcam_delete",
            "filename": filename,
            "ok": bool(ok),
            "reason": reason,
        }
        if local_size is not None:
            ev["local_size"] = local_size
        if remote_size is not None:
            ev["remote_size"] = remote_size
        self._send(ev)

    def retention_deleted(self, filename, *, reason):
        self._send({
            "type": "retention_deleted",
            "filename": filename,
            "reason": reason,
        })


class _ArgsShim:
    """Tiny duck-typed stand-in for argparse.Namespace that
    ``viofosync.sync()`` expects. Saves us from importing
    argparse just to synthesise an object."""

    def __init__(self, use_html: bool, gps_extract: bool) -> None:
        self.html = use_html
        self.gps_extract = gps_extract


class SyncWorker:
    def __init__(
        self,
        db: Database,
        provider: SettingsProvider,
        hub: Hub,
    ) -> None:
        self.db = db
        self._provider = provider
        self.hub = hub
        self._task: Optional[asyncio.Task] = None
        self._retention_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._cancel_current = threading.Event()
        self._kick = asyncio.Event()
        self._paused = threading.Event()    # set = paused
        self._cancel_events: dict[int, threading.Event] = {}
        # Restore the last-used pause toggle (persisted in kv) so a
        # restart resumes in the state the user left it. ENABLE_SCHEDULED_SYNC
        # still gates whether the schedule runs at all. Best-effort: with
        # no store there's simply nothing to restore (prod always has one).
        if self.db is not None and self.db.kv_get(_PAUSED_KV_KEY) == "1":
            self._paused.set()
        self._backoff_idx = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running_cycle = False
        self._current_filename: Optional[str] = None
        self._current_filenames: set[str] = set()
        # The address chosen for the in-flight cycle (primary or the
        # alternative). Selected once per cycle and held for the whole
        # drain — no mid-download switching. None when offline.
        self._active_address: Optional[str] = None
        # Tracks the kind of sync_error currently sticky on the hub, so
        # we can emit clear signals only when a previously-set error
        # actually changes.
        self._last_error_kind: Optional[str] = None
        # Last-known dashcam reachability, so online/offline is logged
        # on transition rather than every cycle. None = unknown.
        self._online: Optional[bool] = None

    # ---- lifecycle ----

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once from the lifespan hook to capture the
        main event loop. ``start()`` is safe to call from a
        threadpool handler after this."""
        self._loop = loop

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                raise RuntimeError(
                    "SyncWorker.start called without an event loop; "
                    "call bind_loop() during app startup"
                )
        self._stop.clear()
        log.info("sync worker started")
        # Schedule the coroutine onto the captured loop — works
        # both from the loop thread and from threadpool handlers.
        self._task = asyncio.run_coroutine_threadsafe(
            self._run(), self._loop
        )
        # Retention runs on its own cadence, decoupled from the
        # download cycle: a parked/offline camera or an empty queue
        # must not stop the archive caps from being enforced.
        self._retention_task = asyncio.run_coroutine_threadsafe(
            self._retention_loop(), self._loop
        )

    def _is_running(self) -> bool:
        if self._task is None:
            return False
        # asyncio.Task has .done(); concurrent.futures.Future also
        # exposes .done(), so the check works for both.
        return not self._task.done()

    async def stop(self) -> None:
        self._stop.set()
        self._cancel_current.set()
        self._kick.set()
        import concurrent.futures
        for task in (self._task, self._retention_task):
            if task is None:
                continue
            # ``task`` may be an asyncio.Task or a
            # concurrent.futures.Future — wrap uniformly, and bound
            # the wait either way: a cycle wedged in an uncancellable
            # executor call (hung NFS stat, slow urllib read) must
            # not hang SIGTERM shutdown forever.
            if isinstance(task, concurrent.futures.Future):
                waitable = asyncio.wrap_future(task)
            else:
                waitable = task
            try:
                await asyncio.wait_for(waitable, timeout=STOP_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning(
                    "sync worker did not stop within %.0fs; abandoning",
                    STOP_TIMEOUT,
                )
                try:
                    task.cancel()
                except Exception:
                    pass
            except Exception:
                log.exception("sync worker task raised during stop")
        log.info("sync worker stopped")

    def kick(self) -> None:
        """Trigger an immediate cycle (e.g. user clicked Start
        sync or changed priorities). Thread-safe."""
        self._backoff_idx = 0
        if self._loop is not None and self._loop.is_running():
            # asyncio.Event.set() is not thread-safe; hop onto
            # the loop thread first.
            self._loop.call_soon_threadsafe(self._kick.set)
        else:
            self._kick.set()

    def cancel_current(self) -> None:
        """Abort the in-flight download ASAP. Used when the
        reachability probe fails mid-download or the user
        presses Stop."""
        log.info("aborting current download")
        self._cancel_current.set()
        for ev in list(getattr(self, "_cancel_events", {}).values()):
            ev.set()

    def skip_current(self) -> None:
        """Cancel the in-flight download and move on to the
        next queue item. Unlike pause, the worker keeps running."""
        log.info("skipping current download")
        self._cancel_current.set()
        for ev in list(getattr(self, "_cancel_events", {}).values()):
            ev.set()

    def pause(self) -> None:
        """Pause the worker: finish the current chunk then stop
        picking new items. The current download is cancelled."""
        log.info("sync paused")
        self._paused.set()
        self.db.kv_set(_PAUSED_KV_KEY, "1")
        self._cancel_current.set()
        for ev in list(getattr(self, "_cancel_events", {}).values()):
            ev.set()
        self._broadcast_sync_state()

    def resume(self) -> None:
        """Unpause and kick the worker to pick up immediately."""
        log.info("sync resumed")
        self._paused.clear()
        self.db.kv_set(_PAUSED_KV_KEY, "0")
        self._broadcast_sync_state()
        self.kick()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def current_filename(self) -> Optional[str]:
        return self._current_filename

    def get_status(self) -> dict:
        return {
            "running": self._is_running(),
            "paused": self.paused,
            "current_filename": self._current_filename,
            "current_filenames": sorted(
                getattr(self, "_current_filenames", set())
            ),
        }

    def _broadcast_sync_state(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(
                    self.hub.broadcast({
                        "type": "sync_state",
                        "running": self._is_running(),
                        "paused": self.paused,
                        "current_filename": self._current_filename,
                        "current_filenames": sorted(
                            getattr(self, "_current_filenames", set())
                        ),
                    })
                )
            )

    async def _emit_disk_pct(self) -> None:
        """Broadcast the current filesystem disk usage % so the Hub
        can evaluate the critical-disk error condition. Called from
        ``_cycle`` on the event loop.

        Uses :func:`retention.filesystem_used_pct` — NOT the
        quota-aware ``disk_used_pct``. When ``RECORDINGS_QUOTA_GB``
        is set, quota retention deliberately keeps the recordings dir
        at ~100% of quota, so the quota-aware metric would trip
        ``DISK_CRITICAL_PCT`` perpetually. The critical-disk error
        is reserved for the filesystem-level "OS will deny writes
        soon" condition, which is independent of our self-imposed
        quota.
        """
        snap = self._provider.get()
        pct = _retention.filesystem_used_pct(snap.recordings)
        if pct is None:
            return
        await self.hub.broadcast({"type": "disk_pct", "pct": float(pct)})

    async def _emit_sync_error(
        self, kind: Optional[str], message: Optional[str]
    ) -> None:
        await self.hub.broadcast({
            "type": "sync_error", "kind": kind, "message": message,
        })

    async def _set_sync_error(self, kind: str, message: str) -> None:
        if self._last_error_kind == kind:
            return  # already sticky on the hub; no event
        self._last_error_kind = kind
        await self._emit_sync_error(kind, message)

    async def _clear_sync_error(self) -> None:
        if self._last_error_kind is None:
            return
        self._last_error_kind = None
        await self._emit_sync_error(None, None)

    async def _check_recordings_writable(self) -> bool:
        """Return True if the recordings root exists and is writable.
        Emits a sticky sync_error on failure, clears one on recovery."""
        snap = self._provider.get()
        path = getattr(snap, "recordings", None) or ""
        ok = _path_is_writable(path)
        if not ok:
            await self._set_sync_error(
                "recordings_unwritable",
                "recordings path not writable",
            )
            return False
        if self._last_error_kind == "recordings_unwritable":
            await self._clear_sync_error()
        return True

    async def _classify_listing_failure(self, exc: BaseException) -> None:
        """Inspect a listing exception. If it's HTTP 401/403, emit a
        sticky auth_failure error. Other exceptions are not promoted to
        sticky errors — they're transient by nature."""
        if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
            await self._set_sync_error(
                "auth_failure",
                "camera authentication failed",
            )

    # ---- main loop ----

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                did_work = await self._cycle()
            except Exception:  # pragma: no cover
                log.exception("sync cycle crashed")
                did_work = False

            if did_work:
                wait = 1.0   # immediately look for more
                self._backoff_idx = 0
            else:
                wait = float(
                    BACKOFF_STEPS[
                        min(self._backoff_idx, len(BACKOFF_STEPS) - 1)
                    ]
                )
                self._backoff_idx = min(
                    self._backoff_idx + 1, len(BACKOFF_STEPS) - 1
                )

            # Sleep, but wake up early on kick or stop.
            try:
                await asyncio.wait_for(
                    self._kick.wait(), timeout=wait
                )
            except asyncio.TimeoutError:
                pass
            self._kick.clear()

    async def _retention_loop(self) -> None:
        """Enforce the archive caps on a fixed cadence, independent of
        download activity and camera reachability.

        The download cycle's own post-drain sweep handles clips that
        just arrived; this loop is what keeps the archive under quota
        when the camera is offline or has nothing new to download —
        the cases where ``_cycle`` returns before reaching its sweep.
        Runs only while the worker is running, so retention follows
        the same lifecycle as scheduled sync.
        """
        while not self._stop.is_set():
            try:
                await self._run_retention_sweep()
            except Exception:  # pragma: no cover — never kill the loop
                log.exception("periodic retention sweep failed")
            # Sleep until the next pass, waking immediately on stop.
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=RETENTION_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    async def _run_retention_sweep(self) -> None:
        """Run one retention pass against the live settings, then
        refresh the broadcast disk %. Offloaded to a thread because
        the sweep walks the archive and touches the DB."""
        snap = self._provider.get()
        sink = WebSink(self.hub, asyncio.get_running_loop())
        from .exporter import export_protect_ids
        await asyncio.to_thread(
            _retention.sweep,
            self.db, snap.recordings,
            max_days=snap.retention_max_days,
            disk_pct=snap.retention_disk_pct,
            protect_ro=snap.retention_protect_ro,
            quota_gb=snap.recordings_quota_gb,
            sink=sink,
            exclude=_retention.import_exclude_set(
                snap.recordings, snap.import_path
            ),
            protect_ids=export_protect_ids(self.db),
        )
        await self._emit_disk_pct()

    # ---- probe ----

    async def _probe_one(self, address: str) -> bool:
        """3-second TCP probe to one address on port 80. True = reachable."""
        loop = asyncio.get_running_loop()

        def _sync():
            try:
                with socket.create_connection((address, 80), timeout=3.0):
                    return True
            except OSError:
                return False

        return await loop.run_in_executor(None, _sync)

    async def _select_active_address(self) -> tuple[Optional[str], str]:
        """Pick the address for this cycle: primary first, then the
        alternative. Returns ``(address, source)`` with source one of
        ``"primary"``, ``"alternative"``, or ``"offline"`` (address None).
        """
        snap = self._provider.get()
        for source, address in (
            ("primary", snap.address),
            ("alternative", snap.address_fallback),
        ):
            if address and await self._probe_one(address):
                return address, source
        return None, "offline"

    # ---- one cycle ----

    async def _refresh_listing_and_reconcile(self) -> bool:
        """Pull a fresh listing from the dashcam and reconcile it
        with the queue. Returns True on success, False on
        listing failure (logged at warning, no broadcast).

        Used at the top of every cycle and again after each
        successful download mid-drain — the latter is what gets
        clips recorded *during* a long sync into the queue
        without waiting for the cycle to end.
        """
        try:
            listing = await asyncio.get_running_loop().run_in_executor(
                None, self._fetch_listing
            )
        except Exception as e:
            log.warning("listing fetch failed: %s", e)
            await self._classify_listing_failure(e)
            return False
        if self._provider.get().sync_ro_only:
            listing = list(_filter_ro_only(listing))
        # Both the archive walk and the reconcile transaction are
        # blocking I/O (NAS stat calls, sqlite write lock) — keep
        # them off the event loop or every request/WebSocket stalls
        # behind a slow recordings volume.
        present = await asyncio.to_thread(self._present_filenames)
        summary = await asyncio.to_thread(
            q.reconcile, self.db, listing, present
        )
        await self.hub.broadcast({
            "type": "queue_reconciled",
            "summary": summary,
            "queue": q.list_all(self.db, limit=200),
        })
        q.emit_queue_changed(self.db, self.hub)
        if self._last_error_kind == "auth_failure":
            await self._clear_sync_error()
        return True

    def _note_reachability(self, online: bool, source: str = "") -> None:
        """Log dashcam online/offline only when it actually changes, so
        the Logs tab records the transition without one line per cycle."""
        if self._online == online:
            return
        self._online = online
        if online:
            log.info("dashcam online (%s)", source or "primary")
        else:
            log.info("dashcam offline")

    async def _cycle(self) -> bool:
        # A paused worker does no dashcam work — no probe, no listing —
        # so "sync paused" stays the last word in the log until resume.
        if self._paused.is_set():
            return False
        await self._emit_disk_pct()
        if not await self._check_recordings_writable():
            return False
        active, source = await self._select_active_address()
        self._active_address = active
        if active is not None:
            self._note_reachability(True, source)
            await self.hub.broadcast({
                "type": "dashcam_online",
                "source": source,
                "address": active,
            })
        else:
            self._note_reachability(False)
            await self.hub.broadcast({"type": "dashcam_offline"})
            return False

        # Initial listing — failure here aborts the cycle so
        # back-off can kick in. Mid-cycle re-listings (below)
        # are best-effort instead.
        if not await self._refresh_listing_and_reconcile():
            await self.hub.broadcast({
                "type": "sync_error",
                "error": "listing failed",
            })
            return False

        # Drain the queue with a rolling task pool. Each task atomically
        # claims one pending row before it starts, so concurrent downloads
        # cannot pick the same file. When one finishes, immediately refill
        # that slot instead of waiting for the slowest sibling.
        did_any = False
        refresh_after_success = False
        active: dict[
            asyncio.Task[bool],
            tuple[q.QueueItem, threading.Event],
        ] = {}

        def _set_current_from_active() -> None:
            self._current_filename = (
                sorted(self._current_filenames)[0]
                if self._current_filenames else None
            )
            self._broadcast_sync_state()

        def _forget_active_task(task: asyncio.Task[bool]) -> q.QueueItem:
            item, _cancel_event = active.pop(task)
            self._cancel_events.pop(item.id, None)
            self._current_filenames.discard(item.filename)
            return item

        def _forget_all_active() -> None:
            for item, _cancel_event in list(active.values()):
                self._cancel_events.pop(item.id, None)
                self._current_filenames.discard(item.filename)
            active.clear()
            _set_current_from_active()

        while not self._stop.is_set():
            snap = self._provider.get()
            concurrency = max(1, int(getattr(snap, "download_concurrency", 1)))

            while (
                len(active) < concurrency
                and not self._stop.is_set()
                and not self._paused.is_set()
            ):
                # Re-probe before filling an open slot so we don't burn a
                # retry budget on a dashcam that's already gone.
                if did_any and not await self._probe_one(self._active_address):
                    self.cancel_current()
                    await self.hub.broadcast({
                        "type": "dashcam_offline",
                    })
                    if active:
                        await asyncio.gather(
                            *active.keys(), return_exceptions=True,
                        )
                        _forget_all_active()
                    return True

                # Refresh listing after successful completions and before
                # claiming the next item, so new clips can enter the queue
                # without draining the whole current pool first.
                if refresh_after_success:
                    await self._refresh_listing_and_reconcile()
                    refresh_after_success = False

                item = q.claim_next_pending(
                    self.db,
                    ro_only=snap.sync_ro_only,
                )
                if item is None:
                    break
                cancel_event = threading.Event()
                self._cancel_events[item.id] = cancel_event
                self._current_filenames.add(item.filename)
                _set_current_from_active()
                task = asyncio.create_task(
                    self._download_one(
                        item,
                        cancel_event=cancel_event,
                        already_claimed=True,
                    )
                )
                active[task] = (item, cancel_event)

            if not active:
                break

            done, _pending = await asyncio.wait(
                active.keys(), return_when=asyncio.FIRST_COMPLETED,
            )
            disk_full = False
            for task in done:
                item = _forget_active_task(task)
                try:
                    result = task.result()
                except DiskFullError:
                    disk_full = True
                    result = False
                except Exception:
                    log.exception("download task crashed for %s", item.filename)
                    result = False

                did_any = True
                if result is True:
                    refresh_after_success = True
            _set_current_from_active()

            if disk_full:
                self.cancel_current()
                log.warning(
                    "recordings volume full — stopping this cycle's "
                    "downloads; will retry next cycle"
                )
                if active:
                    await asyncio.gather(
                        *active.keys(), return_exceptions=True,
                    )
                    _forget_all_active()
                return did_any

        # Re-index + sweep thumbs so new clips appear in the UI.
        # Both calls are idempotent; the did_any gate is just to
        # skip the directory walk when nothing changed.
        if did_any:
            snap = self._provider.get()
            try:
                await asyncio.to_thread(
                    scanner.scan,
                    self.db, snap.recordings, snap.grouping,
                    self.hub, asyncio.get_running_loop(),
                )
                sink = WebSink(self.hub, asyncio.get_running_loop())
                await asyncio.to_thread(
                    _retention.sweep,
                    self.db, snap.recordings,
                    max_days=snap.retention_max_days,
                    disk_pct=snap.retention_disk_pct,
                    protect_ro=snap.retention_protect_ro,
                    quota_gb=snap.recordings_quota_gb,
                    sink=sink,
                    exclude=_retention.import_exclude_set(
                        snap.recordings, snap.import_path
                    ),
                )
            except Exception:  # pragma: no cover — non-fatal
                log.exception("post-cycle scan/thumb sweep failed")

        await self._emit_disk_pct()
        await self.hub.broadcast({
            "type": "sync_done",
            "ok": True,
            "queue": q.list_all(self.db, limit=200),
        })
        return did_any

    def _fetch_listing(self):
        snap = self._provider.get()
        base = f"http://{self._active_address}"
        if snap.use_html_listing:
            return vfs.get_dashcam_filenames_html(base)
        return vfs.get_dashcam_filenames(base)

    def _present_filenames(self):
        snap = self._provider.get()
        out = []
        for filename, _ in vfs.get_downloaded_recordings(
            snap.recordings, snap.grouping
        ):
            out.append(filename)
        return out

    # ---- single item download ----

    async def _download_one(
        self,
        item: q.QueueItem,
        *,
        cancel_event: threading.Event | None = None,
        already_claimed: bool = False,
    ) -> bool:
        snap = self._provider.get()
        if not already_claimed:
            q.mark_downloading(self.db, item.id)
        if cancel_event is None:
            cancel_event = self._cancel_current
            cancel_event.clear()
        loop = asyncio.get_running_loop()
        sink = WebSink(self.hub, loop)

        def _blocking():
            """Runs on an executor thread. Synthesises the
            Recording tuple ``download_file`` expects."""
            import datetime as _dt
            # get_group_name wants a datetime; if the queue row
            # didn't capture one, now() is a safe fallback.
            recorded = (
                _dt.datetime.fromtimestamp(item.recorded_at)
                if item.recorded_at
                else _dt.datetime.now()
            )
            group_name = vfs.get_group_name(
                recorded, snap.grouping
            )
            rec = vfs.Recording(
                filename=item.filename,
                filepath=item.source_dir,
                size=item.remote_size,
                timecode=None,
                datetime=recorded,
                attr=None,
            )
            base = f"http://{self._active_address}"
            try:
                ok, _ = vfs.download_file_with(
                    base, rec, snap.recordings,
                    group_name,
                    progress_sink=sink,
                    cancel_check=cancel_event.is_set,
                    max_attempts=snap.download_attempts,
                    socket_timeout=snap.timeout,
                )
                # download_file_with() doesn't pull GPX, so the worker
                # has to do it here when the setting is on.
                if ok:
                    dest_path = vfs.get_filepath(
                        snap.recordings, group_name, item.filename,
                    )
                    # Adopt the actual byte count as the queue's
                    # remote_size (the HTML listing rounds to MB).
                    _refresh_queue_size(self.db, item, dest_path)
                    if snap.gps_extract:
                        try:
                            vfs.extract_gps_data(dest_path)
                        except Exception as e:
                            # Clips recorded without GPS lock have no
                            # track to extract; not a download failure.
                            log.info(
                                "gpx extract failed for %s: %s",
                                item.filename, e,
                            )
                    _maybe_delete_from_dashcam(
                        item=item,
                        dest_path=dest_path,
                        delete_enabled=snap.delete_after_download,
                        base_url=base,
                        sink=sink,
                    )
                return ok, None, False, False
            except vfs.DownloadCancelled:
                # Deliberate abort (pause/stop/unreachable): not a
                # failure, so the caller must not burn an attempt.
                return False, None, True, False
            except OSError as e:
                if e.errno == _errno.ENOSPC:
                    return False, str(e), False, True
                return False, str(e), False, False
            except Exception as e:
                return False, str(e), False, False

        ok, err, cancelled, disk_full = await loop.run_in_executor(
            None, _blocking
        )

        if disk_full:
            # Refund the attempt (retrying a full disk can only fail),
            # surface a sticky error, and abort this cycle's drain.
            # The next cycle retries one item; success clears the error.
            q.mark_cancelled(self.db, item.id)
            q.emit_queue_changed(self.db, self.hub)
            await self._set_sync_error(
                "disk_full", "recordings volume is full",
            )
            raise DiskFullError(err)

        if cancelled:
            # Return the item to pending with its attempt refunded so it
            # picks up cleanly on resume. download_file already logged the
            # cancellation at INFO — no need to repeat it here.
            q.mark_cancelled(self.db, item.id)
            q.emit_queue_changed(self.db, self.hub)
            return False

        if ok:
            if self._last_error_kind == "disk_full":
                await self._clear_sync_error()
            q.mark_done(self.db, item.id)
            q.emit_queue_changed(self.db, self.hub)
            # Index this clip now so it appears immediately, then enqueue
            # its (heavier) derive at live priority.
            from . import derive_queue as _dq
            _snap = self._provider.get()
            _cid = scanner.index_one_clip(
                self.db, _snap.recordings, _snap.grouping, item.filename,
                source_dir=item.source_dir,
            )
            if _cid is not None:
                _dq.enqueue(self.db, _cid, priority=0, now=int(time.time()))
            return True

        new_state = q.mark_transient_failure(
            self.db,
            item.id,
            err or "unknown",
            snap.max_attempts,
        )
        q.emit_queue_changed(self.db, self.hub)
        await self.hub.broadcast({
            "type": "item_state_change",
            "filename": item.filename,
            "state": new_state,
            "error": err,
        })
        # If we gave up permanently, keep going; otherwise,
        # yield to let the reachability re-probe decide
        # whether to continue this cycle.
        return False
