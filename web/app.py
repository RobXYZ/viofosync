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
from contextlib import asynccontextmanager

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
from .routers import setup as setup_router
from .services import retention as _ret_mod
from .services import scanner
from .services.exporter import (
    ExportWorker,
    ffmpeg_available,
    probe_encoders,
)
from .services.geocode import GeocodeService
from .services.hub import Hub
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
            n = await asyncio.to_thread(
                scanner.scan, app.state.db, s.recordings, s.grouping,
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

    app.state.hub = Hub()
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

    try:
        yield
    finally:
        log.info("viofosync web UI shutting down")
        for attr in ("initial_scan_task", "retention_task"):
            task = getattr(app.state, attr, None)
            if task is not None and not task.done():
                task.cancel()
        await app.state.sync_worker.stop()
        await app.state.export_worker.stop()


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
        version="0.1",
        lifespan=lifespan,
        docs_url=None,       # no swagger in prod build
        redoc_url=None,
    )

    app.add_middleware(SetupModeMiddleware)

    app.include_router(auth_router.router)
    app.include_router(archive_router.router)
    app.include_router(exports_router.router)
    app.include_router(queue_router.router)
    app.include_router(progress_router.router)
    app.include_router(settings_router.router)
    app.include_router(setup_router.router)

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
