"""WebSocket fan-out hub.

A tiny pub-sub used by background workers (downloader,
exporter, reachability probe) to push events to every
connected UI client without each worker knowing how many
there are.

Thread-safety: all ``broadcast`` calls happen from the event
loop (background tasks created via ``asyncio.create_task``).
The downloader progress sink is called from a blocking worker
thread; it schedules ``broadcast`` onto the loop via
``asyncio.run_coroutine_threadsafe`` — see
:class:`WebSink` in ``sync_worker.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Set

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from .sync_status import compute_sync_status

log = logging.getLogger("viofosync.hub")

# Per-client send budget. A client with a stalled TCP window (zombie
# tab, closed laptop lid) doesn't error — send just backpressures
# forever, which used to serialize every other client's events behind
# it while pending broadcasts piled up on the loop. A client that
# can't take an event within this window is treated as dead.
SEND_TIMEOUT_S = 2.0


async def _send_with_timeout(ws: WebSocket, event: Dict[str, Any]) -> bool:
    """Send one event; False means the client is dead/stalled and
    should be evicted."""
    try:
        await asyncio.wait_for(ws.send_json(event), timeout=SEND_TIMEOUT_S)
        return True
    except Exception:
        return False


class Hub:
    def __init__(self, settings_provider: Any = None, session: Any = None) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._settings_provider = settings_provider
        self._session = session
        # Bound in lifespan; schedule_broadcast falls back to this so
        # threadpool callers (sync route handlers) don't need to
        # thread a loop reference through every call site.
        self._loop: asyncio.AbstractEventLoop | None = None
        # Last broadcast session_stats key, for deduping the follow-up.
        self._last_session_key: Any = None
        # Retain the last snapshot of major state so a newly-
        # connected client sees the current situation without
        # waiting for the next event.
        self.last_state: Dict[str, Any] = {
            "dashcam_online": None,
            "dashcam_source": None,
            "dashcam_address": None,
            "current_item": None,
            "current_items": {},
            # Session-wide download stats (see download_session.py). Always
            # present so the WS snapshot and MQTT state_fn never KeyError.
            "session": {
                "active": False, "avg_speed_bps": None, "eta_seconds": None,
                "session_bytes": 0, "elapsed_s": 0.0,
            },
            # Stateful diagnostics consumed by compute_sync_status():
            "sync_error": None,
            "disk_pct": None,
            # Latest computed status + reason. Stored on the hub so
            # the WebSocket snapshot (and any consumer reading
            # last_state) sees a coherent pair without waiting for the
            # next change event. ``sync_status_reason`` is the
            # human-readable error reason — non-null only when status
            # is "error".
            "sync_status": None,
            "sync_status_reason": None,
        }

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        try:
            await ws.send_json(
                {"type": "snapshot", "state": self.last_state}
            )
        except (WebSocketDisconnect, RuntimeError, OSError):
            # Client closed during the handshake (e.g. tab hot-
            # reloaded between accept and the first send). The
            # route's finally-clause will remove us from
            # _clients via disconnect(); no need to raise out of
            # the route handler as a 500.
            log.debug(
                "client disconnected before initial snapshot",
            )
            async with self._lock:
                self._clients.discard(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, event: Dict[str, Any]) -> None:
        # Update snapshot for state-ish events so reconnects
        # land on a coherent view.
        t = event.get("type")
        if t == "dashcam_online":
            self.last_state["dashcam_online"] = True
            self.last_state["dashcam_source"] = event.get("source")
            self.last_state["dashcam_address"] = event.get("address")
        elif t == "dashcam_offline":
            self.last_state["dashcam_online"] = False
        elif t == "item_started":
            item = {
                "filename": event.get("filename"),
                "total": event.get("total"),
                "bytes": 0,
            }
            self.last_state["current_item"] = item
            items = dict(self.last_state.get("current_items") or {})
            if item["filename"] is not None:
                items[item["filename"]] = item
            self.last_state["current_items"] = items
        elif t == "item_progress":
            filename = event.get("filename")
            ci = {
                "filename": event.get("filename"),
                "bytes": event.get("bytes"),
                "total": event.get("total"),
                "speed": event.get("speed"),
            }
            self.last_state["current_item"] = ci
            items = dict(self.last_state.get("current_items") or {})
            if filename is not None:
                existing = dict(items.get(filename) or {})
                existing.update(ci)
                items[filename] = existing
            self.last_state["current_items"] = items
        elif t == "item_finished":
            filename = event.get("filename")
            items = dict(self.last_state.get("current_items") or {})
            if filename is not None:
                items.pop(filename, None)
            self.last_state["current_items"] = items
            self.last_state["current_item"] = next(
                iter(items.values()), None
            )
        elif t == "sync_state":
            self.last_state["sync_state"] = {
                "running": event.get("running"),
                "paused": event.get("paused"),
            }
        elif t == "sync_error":
            # kind=None is the clear signal. Anything else replaces the
            # current error verbatim — last writer wins.
            kind = event.get("kind")
            if kind is None:
                self.last_state["sync_error"] = None
            else:
                self.last_state["sync_error"] = {
                    "kind": kind,
                    "message": event.get("message"),
                }
        elif t == "disk_pct":
            pct = event.get("pct")
            if isinstance(pct, (int, float)):
                self.last_state["disk_pct"] = float(pct)

        # Feed the session tracker (reads the event; mutates the tracker).
        self._feed_session(t, event)

        dead: list = []
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            if not await _send_with_timeout(ws, event):
                dead.append(ws)
        # Recompute the unified status after the state mutation above.
        await self._maybe_emit_sync_status(dead)
        await self._maybe_emit_session_stats(dead)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    async def _maybe_emit_sync_status(self, dead: list) -> None:
        """After last_state mutations, recompute the unified status. If
        it differs from the cached value, store it and broadcast a
        follow-up event. Called from broadcast(); ``dead`` is the same
        list it accumulates so we drop disconnected clients in one pass.
        """
        if self._settings_provider is None:
            return
        try:
            snap = self._settings_provider.get()
            status, reason = compute_sync_status(self, None, snap)
        except Exception:
            log.exception("sync_status compute failed; skipping follow-up")
            return
        prev_status = self.last_state.get("sync_status")
        prev_reason = self.last_state.get("sync_status_reason")
        # Dedupe on the (status, reason) pair so a changing reason
        # (e.g. disk % climbing while status stays "error") still
        # reaches clients. Non-error states have reason=None so this
        # collapses back to status-only deduping there.
        if status == prev_status and reason == prev_reason:
            return
        self.last_state["sync_status"] = status
        self.last_state["sync_status_reason"] = reason
        event = {"type": "sync_status", "status": status, "reason": reason}
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            if not await _send_with_timeout(ws, event):
                dead.append(ws)

    def _feed_session(self, t: str, event: Dict[str, Any]) -> None:
        """Translate the relevant Hub events into DownloadSession calls.
        Runs on the event loop, so the tracker needs no locking."""
        s = self._session
        if s is None:
            return
        try:
            if t == "item_started":
                s.note_started(event.get("filename"), event.get("total"))
            elif t == "item_progress":
                s.note_progress(
                    event.get("filename"), event.get("bytes"),
                    event.get("total"),
                )
            elif t == "item_finished":
                s.note_finished(event.get("filename"), event.get("bytes"))
            elif t == "sync_done":
                s.note_idle()
            elif t == "dashcam_offline":
                s.note_idle()
            elif t == "sync_state":
                # running=True fires on every item pick — only idle the
                # session on the stopped/paused variant.
                if not event.get("running") or event.get("paused"):
                    s.note_idle()
            elif t in ("queue_reconciled", "queue_changed"):
                s.refresh_remaining()
        except Exception:
            log.exception("session tracker feed failed for %s", t)

    async def _maybe_emit_session_stats(self, dead: list) -> None:
        """Store the latest session snapshot in last_state and broadcast a
        session_stats follow-up when the rounded view changes. Mirrors
        _maybe_emit_sync_status; ``dead`` accumulates disconnected clients."""
        s = self._session
        if s is None:
            return
        try:
            snap = s.snapshot()
        except Exception:
            log.exception("session snapshot failed; skipping follow-up")
            return
        self.last_state["session"] = snap
        # Dedupe on a rounded view so sub-noise jitter doesn't broadcast.
        speed = snap.get("avg_speed_bps")
        eta = snap.get("eta_seconds")
        # Include whole-second elapsed so an active session emits a ~1/s
        # heartbeat even when speed/eta are stable — this keeps the MQTT
        # publisher triggered and the UI byte counter fresh. Idle sessions
        # hold elapsed at 0.0, so they stay silent.
        key = (
            snap.get("active"),
            None if speed is None else round(speed / (1024 * 1024), 1),
            None if eta is None else round(eta),
            round(snap.get("elapsed_s") or 0.0),
        )
        if key == self._last_session_key:
            return
        self._last_session_key = key
        event = {"type": "session_stats", **snap}
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            if not await _send_with_timeout(ws, event):
                dead.append(ws)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the app's event loop (called once in lifespan)."""
        self._loop = loop

    def schedule_broadcast(
        self,
        loop: asyncio.AbstractEventLoop | None,
        event: Dict[str, Any],
    ) -> None:
        """Thread-safe entry point: used from worker threads and
        threadpool route handlers, which don't own the event loop.
        ``loop`` may be None — the loop bound in lifespan is used."""
        loop = loop or self._loop
        if loop is None:
            log.warning("no event loop bound, dropping event %s", event)
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast(event), loop
            )
        except RuntimeError:
            log.debug("event loop closed, dropping event %s", event)
