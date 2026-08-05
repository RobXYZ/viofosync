"""FastAPI application factory for the viofosync web UI.

Usage::

    uvicorn web.app:create_app --factory --host 0.0.0.0 --port 8080

Reads :mod:`web.settings` on startup, wires up auth, opens the
SQLite state store, mounts the static SPA, and includes all
routers. Background workers (SyncWorker, ExportWorker) are
started in the ``startup`` event and stopped cleanly on shutdown.
"""

from __future__ import annotations

import html as html_lib
import logging
import os
import time
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import settings as settings_mod
from . import version as version_mod
from .auth import Auth
from .db import Database, default_db_path, migrate_legacy_db_path
from .routers import archive as archive_router
from .routers import auth as auth_router
from .routers import control as control_router
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
from .services import tasks as _tasks
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
from .services import derive_worker
from .services.derive_worker import DeriveWorker
from .services import locations as _locations
from .setup_mode import SetupModeMiddleware

log = logging.getLogger("viofosync.web")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def instance_label(name: str) -> str | None:
    """Display label for this instance, or None when the configured name
    is the default (case-insensitively) and no label should be shown.

    Mirrored client-side by ``applyInstanceName`` in web/static/app.js —
    keep the two rules in sync.
    """
    name = (name or "").strip()
    if not name or name.lower() == "viofosync":
        return None
    return name


def _sync_worker_action(keys: set, snap) -> str | None:
    """Decide how a settings change affects the sync worker.

    Returns ``"start"``, ``"stop"``, or None (no relevant change).
    Pure so the decision is unit-testable apart from the lifespan
    wiring.
    """
    if not ({"ADDRESS", "ENABLE_SCHEDULED_SYNC"} & keys):
        return None
    if snap.enable_scheduled_sync and snap.address:
        return "start"
    return "stop"


def _geofence_backfill_needed(keys: set, snap) -> bool:
    """True when a settings change should trigger an immediate geofence
    re-evaluation of the existing queue: a LOCATIONS change while GPS triage is
    on and at least one location is flagged for exclusion."""
    if "LOCATIONS" not in keys:
        return False
    if not getattr(snap, "gps_triage", False):
        return False
    return bool(_locations.exclusion_zones(getattr(snap, "locations", ()) or ()))


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

    # Collect unsubscribe handles so shutdown can detach every
    # callback — the provider is a module-level singleton, so leaking
    # callbacks across lifespans pins dead app objects.
    app.state.settings_unsubscribes = [provider.subscribe(_on_settings_changed)]

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
    # Drop partial/unreferenced files a crashed render left in
    # .exports (they'd otherwise count against the recordings quota).
    try:
        _exp_mod.sweep_orphan_exports(app.state.db, s.recordings)
    except Exception:  # pragma: no cover — non-fatal
        log.exception("export orphan sweep failed")

    # GPS Triage: drop skeleton sidecars orphaned by a crash (clip since
    # downloaded / rotated off / row gone).
    try:
        from .services import triage as _triage_mod
        _triage_mod.sweep_orphans(app.state.db, s.recordings)
    except Exception:  # pragma: no cover — non-fatal
        log.exception("triage startup sweep failed")

    log.info(
        "viofosync web UI ready on http://%s:%d", s.host, s.port
    )
    log.info(
        "archive root: %s | dashcam: %s",
        s.recordings, s.address or "<unset>",
    )

    async def _background_scan() -> None:
        # Salvage staged clips from a crash mid-import before the
        # scan, so the recovered files get indexed in the same pass.
        try:
            from .services import importer as _imp_mod
            rec = await asyncio.to_thread(
                _imp_mod.recover_staging, app.state.db, s,
            )
            if rec["recovered"]:
                log.info("recovered %d staged clip(s) from a previous "
                         "interrupted import", rec["recovered"])
        except Exception as e:  # pragma: no cover — non-fatal
            log.warning("staging recovery failed: %s", e)
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
                protect_ids=_exp_mod.export_protect_ids(app.state.db),
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
    # Threadpool route handlers emit events without a loop reference;
    # the hub falls back to this bound loop.
    app.state.hub.bind_loop(asyncio.get_running_loop())
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

    def _on_sync_settings_changed(keys, snap) -> None:
        # Apply ADDRESS / ENABLE_SCHEDULED_SYNC at runtime — these
        # are not restart-required keys, so the settings UI reports
        # them as applied; previously they did nothing until reboot.
        worker = app.state.sync_worker
        action = _sync_worker_action(keys, snap)
        if action == "start" and not worker._is_running():
            log.info("settings change: starting sync worker")
            worker.start()
        elif action == "stop" and worker._is_running():
            log.info("settings change: stopping sync worker")
            _tasks.spawn(worker.stop(), name="sync-worker-stop")

    app.state.settings_unsubscribes.append(
        provider.subscribe(_on_sync_settings_changed)
    )

    app.state.derive_worker = DeriveWorker(
        app.state.db, provider, app.state.hub
    )
    app.state.derive_worker.bind_loop(asyncio.get_running_loop())
    app.state.derive_worker.start()
    app.state.derive_worker.kick()  # pick up whatever the boot scan enqueues

    def _on_derive_settings_changed(keys, snap) -> None:
        # When eager-derive toggles change, backfill existing clips so
        # they get the newly-enabled artifact without waiting for the next
        # download cycle.
        if {"DERIVE_THUMBS_EAGER", "DERIVE_FILMSTRIPS_EAGER"} & keys:
            derive_worker.enqueue_backfill(
                app.state.db, now=int(time.time())
            )
            app.state.derive_worker.kick()

    app.state.settings_unsubscribes.append(
        provider.subscribe(_on_derive_settings_changed)
    )

    def _on_triage_settings_changed(keys, snap) -> None:
        # When GPS triage is turned off, drop the .triage skeleton cache and
        # clear the triage columns so stale skeletons don't linger.
        if "GPS_TRIAGE" in keys and not snap.gps_triage:
            from .services import triage as _triage_mod
            try:
                _triage_mod.purge_all(app.state.db, snap.recordings)
            except Exception:  # pragma: no cover — non-fatal
                log.exception("triage purge on disable failed")

    app.state.settings_unsubscribes.append(
        provider.subscribe(_on_triage_settings_changed)
    )

    def _on_geofence_settings_changed(keys, snap) -> None:
        # Re-evaluate the existing queue immediately when the home zone is
        # set/changed, so the backlog is skipped without waiting for a cycle.
        if _geofence_backfill_needed(keys, snap):
            _tasks.spawn(
                app.state.sync_worker._run_geofence_pass(),
                name="geofence-backfill",
            )

    app.state.settings_unsubscribes.append(
        provider.subscribe(_on_geofence_settings_changed)
    )

    app.state.mqtt = MqttService(
        db=app.state.db,
        provider=provider,
        hub=app.state.hub,
        app=app,
    )
    if s.mqtt_enabled and s.mqtt_host:
        app.state.mqtt.start()

    def _on_mqtt_settings_change(keys, snap):
        # Scheduled on the running loop so async work executes safely.
        _tasks.spawn(
            app.state.mqtt.on_settings_changed(keys, snap),
            name="mqtt-settings-changed",
        )

    app.state.settings_unsubscribes.append(
        provider.subscribe(_on_mqtt_settings_change)
    )

    try:
        yield
    finally:
        log.info("viofosync web UI shutting down")
        # Detach settings subscribers first so the singleton provider
        # doesn't keep firing into this (now tearing-down) app.
        for _unsub in getattr(app.state, "settings_unsubscribes", []):
            _unsub()
        mqtt_svc = getattr(app.state, "mqtt", None)
        if mqtt_svc is not None:
            await mqtt_svc.stop()
        for attr in ("initial_scan_task", "retention_task"):
            task = getattr(app.state, attr, None)
            if task is not None and not task.done():
                task.cancel()
        await app.state.sync_worker.stop()
        await app.state.derive_worker.stop()
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
        version=version_mod.raw_version() or "dev",
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
    # Compress the SPA (app.js alone is ~200 KB) and the JSON day payloads
    # (~80 KB for a 240-clip day, re-fetched on every re-render). ~5-8x
    # smaller on the wire; minimum_size skips tiny responses and streams.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    app.include_router(auth_router.router)
    app.include_router(archive_router.router)
    app.include_router(control_router.router)
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

            # Surface the instance label pre-auth so multi-instance
            # users can tell login pages apart (the whole point of
            # INSTANCE_NAME). Escaped: the name is free text.
            provider = getattr(app.state, "settings_provider", None)
            label = instance_label(
                provider.get().instance_name if provider else ""
            )
            if label:
                esc = html_lib.escape(label)
                html = html.replace(
                    "<title>Viofosync</title>",
                    f"<title>{esc} — Viofosync</title>",
                )
                html = html.replace(
                    '<p id="instance-login" class="instance-label" hidden></p>',
                    f'<p id="instance-login" class="instance-label">{esc}</p>',
                )
                html = html.replace(
                    '<span id="instance-badge" class="instance-badge" hidden></span>',
                    f'<span id="instance-badge" class="instance-badge">{esc}</span>',
                )

            return Response(
                content=html,
                media_type="text/html",
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                },
            )

    return app
