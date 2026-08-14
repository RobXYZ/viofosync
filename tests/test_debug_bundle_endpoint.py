"""The /api/debug-bundle endpoint: auth-gated, attachment headers,
generator runs off the event loop, only one generation in flight."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from fastapi import HTTPException

from web.auth import require_session
from web.routers import debug as debug_router


def test_router_requires_session():
    deps = [getattr(d, "dependency", None)
            for d in debug_router.router.dependencies]
    assert require_session in deps


async def test_endpoint_returns_attachment(tmp_path, monkeypatch):
    from web.db import Database
    monkeypatch.setattr(
        debug_router.debug_bundle, "build_bundle",
        lambda db, snap, *, encoders: "# viofosync debug bundle\nok")
    state = SimpleNamespace(
        db=Database(str(tmp_path / "v.db")),
        settings_provider=SimpleNamespace(
            get=lambda: SimpleNamespace(instance_name="garage")),
        export_encoders={"selected": "software"},
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    resp = await debug_router.download_bundle(request)
    assert resp.media_type == "text/markdown"
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment;")
    assert "garage" in cd and cd.endswith('.md"')
    assert resp.body.decode().startswith("# viofosync debug bundle")


async def test_concurrent_requests_one_rejected_with_429(tmp_path, monkeypatch):
    """Two overlapping generations must not both probe the camera: the
    second call is rejected with 429 while the first is still in flight,
    deterministically via a threading.Event the slow stub blocks on."""
    from web.db import Database

    release_event = threading.Event()

    def slow_build(db, snap, *, encoders):
        release_event.wait(timeout=5)
        return "# viofosync debug bundle\nslow"

    monkeypatch.setattr(debug_router.debug_bundle, "build_bundle", slow_build)

    state = SimpleNamespace(
        db=Database(str(tmp_path / "v.db")),
        settings_provider=SimpleNamespace(
            get=lambda: SimpleNamespace(instance_name="garage")),
        export_encoders={"selected": "software"},
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    first_task = asyncio.create_task(debug_router.download_bundle(request))
    try:
        # Yield once so the first call acquires the lock and suspends on
        # the blocking to_thread call before we fire the second request.
        await asyncio.sleep(0)

        second_error = None
        try:
            await debug_router.download_bundle(request)
        except HTTPException as exc:
            second_error = exc
    finally:
        release_event.set()

    first_resp = await first_task

    assert second_error is not None
    assert second_error.status_code == 429
    assert first_resp.media_type == "text/markdown"


def test_debug_bundle_requires_login_401(tmp_config_dir, tmp_recordings_dir,
                                          monkeypatch):
    """Real ASGI round-trip (not the router unit tests above): an
    unauthenticated client must be rejected before any bundle work runs."""
    import bcrypt
    from fastapi.testclient import TestClient

    from web import settings as settings_mod
    from web.app import create_app
    from web.services.sync_worker import SyncWorker

    digest = bcrypt.hashpw(b"pw" * 8, bcrypt.gensalt()).decode()
    settings_mod.reset_for_tests()
    p = settings_mod.get_provider()
    data = p._store.load()
    data["WEB_PASSWORD_HASH"] = digest
    p._store.write(data)
    settings_mod.reset_for_tests()
    monkeypatch.setattr(SyncWorker, "start", lambda self: None)
    app = create_app()
    with TestClient(app) as c:
        r = c.get("/api/debug-bundle")
        assert r.status_code == 401
