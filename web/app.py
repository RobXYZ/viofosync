"""FastAPI application factory for the viofosync web UI.

Usage::

    uvicorn web.app:create_app --factory --host 0.0.0.0 --port 8080

Reads :mod:`web.settings` on startup, wires up auth, opens the
SQLite state store, mounts the static SPA, and includes all
routers. Background workers (SyncWorker, ExportWorker) are
started in the ``startup`` event and stopped cleanly on shutdown.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import settings as settings_mod
from .auth import Auth
from .db import Database, default_db_path, migrate_legacy_db_path
from .routers import archive as archive_router
from .routers import auth as auth_router
from .routers import exports as exports_router
from .routers import progress as progress_router
from .routers import queue as queue_router
from .routers import settings as settings_router
from .routers import mqtt as mqtt_router
from .routers import setup as setup_router
from .routers import storage as storage_router
from .routers import imports as imports_router
from .routers import logs as logs_router
from .services import durations as _dur_mod
from .services import retention as _ret_mod
from .services import scanner
from .services.exporter import (
    ExportWorker,
    ffmpeg_available,
    probe_encoders,
)
from .services.geocode import GeocodeService
from .services.download_session import DownloadSession
from .services.hub import Hub
from .services.log_store import DBLogHandler
from .services.mqtt import MqttService
from .services.sync_worker import SyncWorker
from .setup_mode import SetupModeMiddleware

log = logging.getLogger("viofosync.web")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hook.

    We initialise settings + auth + db here (not at import time)
    so that tests can construct a FastAPI app with a temporary
    settings object without touching real env vars.
    """
    import asyncio

    if not hasattr(app.state, "settings_provider"):
        app.state.settings_provider = settings_mod.get_provider()
    provider = app.state.settings_provider
    s = provider.get()

    app.state.auth = Auth(
        password_hash=s.password_hash, secret=s.session_secret
    )

    def _on_settings_changed(
        keys: set[str], snap: settings_mod.Snapshot,
    ) -> None:
        if "WEB_PASSWORD_HASH" in keys:
            app.state.auth.update_password_hash(snap.password_hash)
        if "SESSION_SECRET" in keys:
            app.state.auth.rotate_secret(snap.session_secret)

    provider.subscribe(_on_settings_changed)

    db_path = default_db_path()
    migrate_legacy_db_path(db_path)
    app.state.db = Database(db_path)

    # Reset any rows still marked downloading/running from the
    # previous process — those owners are gone, so the workers
    # need to pick the work back up rather than leave zombies.
    from .services import queue as _q_mod
    from .services import exporter as _exp_mod
    n_dl = _q_mod.reconcile_orphan_downloads(app.state.db)
    n_jobs = _exp_mod.reconcile_orphan_jobs(app.state.db)
    if n_dl:
        log.info("reset %d orphan download row(s) to pending", n_dl)
    if n_jobs:
        log.info("marked %d orphan export job(s) as failed", n_jobs)

    log.info(
        "viofosync web UI ready on http://%s:%d", s.host, s.port
    )
    log.info(
        "archive root: %s | dashcam: %s",
        s.recordings, s.address or "<unset>",
    )

    async def _background_scan() -> None:
        try:
            log.info("initial archive scan: starting (%s)", s.recordings)
            loop = asyncio.get_running_loop()
            n = await asyncio.to_thread(
                scanner.scan, app.state.db, s.recordings, s.grouping,
                app.state.hub, loop,
            )
            log.info("initial archive scan: %d clips indexed", n)
        except Exception as e:  # pragma: no cover — non-fatal
            log.warning("initial scan failed: %s", e)
            return
        # Sweep any clips that don't have a cached thumbnail yet —
        # backfills the cache on first boot of this image and on any
        # subsequent boot where new files landed (e.g. manual drop).
        try:
            await scanner.sweep_missing_thumbs(
                app.state.db, s.recordings,
            )
        except Exception as e:  # pragma: no cover — non-fatal
            log.warning("thumb sweep failed: %s", e)
        try:
            await _dur_mod.sweep_missing_durations(app.state.db)
        except Exception as e:  # pragma: no cover — non-fatal
            log.warning("duration sweep failed: %s", e)

    app.state.initial_scan_task = asyncio.create_task(_background_scan())

    async def _background_retention() -> None:
        try:
            await asyncio.to_thread(
                _ret_mod.sweep,
                app.state.db, s.recordings,
                max_days=s.retention_max_days,
                disk_pct=s.retention_disk_pct,
                protect_ro=s.retention_protect_ro,
                quota_gb=s.recordings_quota_gb,
            )
        except Exception:  # pragma: no cover — non-fatal
            log.exception("startup retention sweep failed")

    app.state.retention_task = asyncio.create_task(_background_retention())

    app.state.download_session = DownloadSession(
        remaining_bytes_provider=lambda: _q_mod.pending_bytes(app.state.db),
    )
    app.state.hub = Hub(
        settings_provider=provider,
        session=app.state.download_session,
    )
    # Now that db + hub + loop exist, let the log handler persist and
    # live-broadcast records. Records logged earlier in startup were
    # buffered by the handler and flush when the drain task starts.
    log_handler = getattr(app.state, "log_handler", None)
    if log_handler is not None:
        log_handler.bind(
            app.state.db,
            app.state.hub.broadcast,
            asyncio.get_running_loop(),
        )
        app.state.log_drain_task = asyncio.create_task(log_handler.run())
    # Compute initial sync_status so the very first WebSocket snapshot
    # and the first MQTT publish carry the right value without waiting
    # for the sync worker's first cycle.
    from web.services.sync_status import compute_sync_status as _csss
    try:
        _state, _ = _csss(app.state.hub, None, provider.get())
        app.state.hub.last_state["sync_status"] = _state
    except Exception:
        log.exception("initial sync_status compute failed")
    app.state.geocode = GeocodeService(app.state.db, provider)
    app.state.export_worker = ExportWorker(
        app.state.db, provider, app.state.hub.broadcast
    )
    if ffmpeg_available():
        app.state.export_worker.start()
        # Cache the encoder probe results — this is expensive
        # (spawns ffmpeg). The current default encoder for a new
        # job is resolved per-request in routers/exports.py so
        # EXPORT_ENCODER stays hot-reloadable through the GUI.
        encoders = await probe_encoders()
        app.state.export_encoders = encoders
        log.info(
            "export encoder available: %s",
            ", ".join(k for k, v in encoders.items() if v),
        )
    else:
        log.warning(
            "ffmpeg not found — export jobs will be rejected"
        )
        app.state.export_encoders = {}

    app.state.sync_worker = SyncWorker(
        app.state.db, provider, app.state.hub
    )
    app.state.sync_worker.bind_loop(asyncio.get_running_loop())
    if s.enable_scheduled_sync and s.address:
        app.state.sync_worker.start()
    elif not s.address:
        log.warning(
            "ADDRESS not set — sync worker idle until configured"
        )

    app.state.mqtt = MqttService(
        db=app.state.db,
        provider=provider,
        hub=app.state.hub,
        app=app,
    )
    if s.mqtt_enabled and s.mqtt_host:
        app.state.mqtt.start()

    # Track current discovery/node so on_settings_changed can publish
    # cleanup deletes against the *old* topology when those change.
    app.state.mqtt._last_node_id = s.mqtt_node_id
    app.state.mqtt._last_discovery_prefix = s.mqtt_discovery_prefix

    def _on_mqtt_settings_change(keys, snap):
        # Scheduled on the running loop so async work executes safely.
        asyncio.create_task(app.state.mqtt.on_settings_changed(keys, snap))

    provider.subscribe(_on_mqtt_settings_change)

    try:
        yield
    finally:
        log.info("viofosync web UI shutting down")
        mqtt_svc = getattr(app.state, "mqtt", None)
        if mqtt_svc is not None:
            await mqtt_svc.stop()
        for attr in ("initial_scan_task", "retention_task"):
            task = getattr(app.state, attr, None)
            if task is not None and not task.done():
                task.cancel()
        await app.state.sync_worker.stop()
        await app.state.export_worker.stop()
        drain = getattr(app.state, "log_drain_task", None)
        if drain is not None and not drain.done():
            drain.cancel()
            # Await the cancellation so the task unwinds before the loop
            # tears down — bare cancel() only stays warning-clean while
            # run() happens to be parked in queue.get(); awaiting it makes
            # that independent of where cancellation lands.
            with suppress(asyncio.CancelledError):
                await drain
        log_handler = getattr(app.state, "log_handler", None)
        if log_handler is not None:
            logging.getLogger().removeHandler(log_handler)


def create_app() -> FastAPI:
    # Force root logger to INFO — without `force=True` any earlier
    # basicConfig call (uvicorn, viofosync_lib) wins and our
    # log.info() calls vanish.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )

    app = FastAPI(
        title="Viofosync",
        version="2.2",
        lifespan=lifespan,
        docs_url=None,       # no swagger in prod build
        redoc_url=None,
    )

    # Persist INFO+ from our loggers (and WARNING+ from everything) into
    # the app_log table for the Logs tab. The handler only enqueues here;
    # lifespan binds the DB/hub/loop and starts the drain task. basicConfig
    # above ran with force=True, so any handler from a previous create_app
    # (in tests) was already removed — no accumulation.
    log_handler = DBLogHandler()
    logging.getLogger().addHandler(log_handler)
    app.state.log_handler = log_handler

    app.add_middleware(SetupModeMiddleware)

    app.include_router(auth_router.router)
    app.include_router(archive_router.router)
    app.include_router(exports_router.router)
    app.include_router(queue_router.router)
    app.include_router(progress_router.router)
    app.include_router(settings_router.router)
    app.include_router(setup_router.router)
    app.include_router(mqtt_router.router)
    app.include_router(storage_router.router)
    app.include_router(imports_router.router)
    app.include_router(logs_router.router)

    # Static SPA — served at / with an explicit index.html fall-through
    # so the SPA's hash-router owns everything that isn't /api/*.
    if os.path.isdir(STATIC_DIR):
        app.mount(
            "/static",
            StaticFiles(directory=STATIC_DIR),
            name="static",
        )

        # Cache-bust the SPA's static asset URLs by appending each
        # file's mtime as a query string. Without this the browser
        # happily holds onto an old app.js (or styles.css) after
        # a redeploy — we hit this every release. Index itself is
        # served no-cache so the rewritten URLs reach the user.
        @app.get("/", response_class=Response)
        def index() -> Response:
            html_path = os.path.join(STATIC_DIR, "index.html")
            with open(html_path, encoding="utf-8") as f:
                html = f.read()
            for asset in ("app.js", "styles.css"):
                asset_path = os.path.join(STATIC_DIR, asset)
                try:
                    stamp = int(os.path.getmtime(asset_path))
                except OSError:
                    continue
                html = html.replace(
                    f"/static/{asset}",
                    f"/static/{asset}?v={stamp}",
                )
            return Response(
                content=html,
                media_type="text/html",
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                },
            )

    return app
