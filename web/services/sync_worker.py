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
from . import scanner
from .hub import Hub

log = logging.getLogger("viofosync.sync_worker")

BACKOFF_STEPS = [10, 30, 120, 600]  # seconds


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
        self._stop = asyncio.Event()
        self._cancel_current = threading.Event()
        self._kick = asyncio.Event()
        self._paused = threading.Event()    # set = paused
        self._backoff_idx = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running_cycle = False
        self._current_filename: Optional[str] = None

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
        # Schedule the coroutine onto the captured loop — works
        # both from the loop thread and from threadpool handlers.
        self._task = asyncio.run_coroutine_threadsafe(
            self._run(), self._loop
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
        if self._task is not None:
            # self._task may be an asyncio.Task or a
            # concurrent.futures.Future — wrap uniformly.
            import concurrent.futures
            try:
                if isinstance(self._task, concurrent.futures.Future):
                    await asyncio.wrap_future(self._task)
                else:
                    await asyncio.wait_for(self._task, timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                try:
                    self._task.cancel()
                except Exception:
                    pass

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
        self._cancel_current.set()

    def skip_current(self) -> None:
        """Cancel the in-flight download and move on to the
        next queue item. Unlike pause, the worker keeps running."""
        self._cancel_current.set()

    def pause(self) -> None:
        """Pause the worker: finish the current chunk then stop
        picking new items. The current download is cancelled."""
        self._paused.set()
        self._cancel_current.set()
        self._broadcast_sync_state()

    def resume(self) -> None:
        """Unpause and kick the worker to pick up immediately."""
        self._paused.clear()
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
                    })
                )
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

    # ---- probe ----

    async def _probe(self) -> bool:
        """3-second TCP probe to the dashcam. True = reachable."""
        snap = self._provider.get()
        if not snap.address:
            return False
        loop = asyncio.get_running_loop()
        address = snap.address

        def _sync():
            try:
                with socket.create_connection(
                    (address, 80), timeout=3.0
                ):
                    return True
            except OSError:
                return False

        return await loop.run_in_executor(None, _sync)

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
            return False
        if self._provider.get().sync_ro_only:
            listing = list(_filter_ro_only(listing))
        present = self._present_filenames()
        summary = q.reconcile(self.db, listing, present)
        await self.hub.broadcast({
            "type": "queue_reconciled",
            "summary": summary,
            "queue": q.list_all(self.db, limit=200),
        })
        return True

    async def _cycle(self) -> bool:
        reachable = await self._probe()
        await self.hub.broadcast({
            "type": "dashcam_online" if reachable else "dashcam_offline",
        })
        if not reachable:
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

        # Drain the queue. After each successful download the
        # loop re-checks ``next_pending`` so a priority update
        # mid-cycle takes effect immediately.
        did_any = False
        while not self._stop.is_set():
            if self._paused.is_set():
                break
            item = q.next_pending(
                self.db,
                ro_only=self._provider.get().sync_ro_only,
            )
            if item is None:
                break
            # Re-probe occasionally so we don't burn a whole
            # retry budget on a dashcam that's already gone.
            if did_any and not await self._probe():
                await self.hub.broadcast({
                    "type": "dashcam_offline",
                })
                return True
            self._current_filename = item.filename
            self._broadcast_sync_state()
            ok = await self._download_one(item)
            self._current_filename = None
            did_any = True
            if not ok:
                # Transient failure. Loop continues with next
                # pending item, which may well succeed.
                continue
            # Refresh listing between downloads so clips the
            # dashcam recorded during this transfer show up in
            # the queue before we pick the next pending one.
            # Best-effort: a transient listing failure here
            # leaves the existing queue intact.
            await self._refresh_listing_and_reconcile()

        # Re-index + sweep thumbs so new clips appear in the UI.
        # Both calls are idempotent; the did_any gate is just to
        # skip the directory walk when nothing changed.
        if did_any:
            snap = self._provider.get()
            try:
                await asyncio.to_thread(
                    scanner.scan,
                    self.db, snap.recordings, snap.grouping,
                )
                await scanner.sweep_missing_thumbs(
                    self.db, snap.recordings,
                )
                from . import retention as _retention
                sink = WebSink(self.hub, asyncio.get_running_loop())
                await asyncio.to_thread(
                    _retention.sweep,
                    self.db, snap.recordings,
                    max_days=snap.retention_max_days,
                    disk_pct=snap.retention_disk_pct,
                    protect_ro=snap.retention_protect_ro,
                    quota_gb=snap.recordings_quota_gb,
                    sink=sink,
                )
            except Exception:  # pragma: no cover — non-fatal
                log.exception("post-cycle scan/thumb sweep failed")

        await self.hub.broadcast({
            "type": "sync_done",
            "ok": True,
            "queue": q.list_all(self.db, limit=200),
        })
        return did_any

    def _fetch_listing(self):
        snap = self._provider.get()
        base = f"http://{snap.address}"
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

    async def _download_one(self, item: q.QueueItem) -> bool:
        snap = self._provider.get()
        q.mark_downloading(self.db, item.id)
        self._cancel_current.clear()
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
            base = f"http://{snap.address}"
            try:
                ok, _ = vfs.download_file_with(
                    base, rec, snap.recordings,
                    group_name,
                    progress_sink=sink,
                    cancel_check=self._cancel_current.is_set,
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
                return ok, None
            except Exception as e:
                return False, str(e)

        ok, err = await loop.run_in_executor(None, _blocking)

        if ok:
            q.mark_done(self.db, item.id)
            return True

        new_state = q.mark_transient_failure(
            self.db,
            item.id,
            err or "unknown",
            snap.max_attempts,
        )
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
