"""Settings CRUD routes (auth + CSRF required)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_csrf, require_session
from ..settings_schema import (
    DEFAULT_VALUES,
    EDITABLE_KEYS,
    RESTART_REQUIRED_KEYS,
)
from .setup import _probe

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _readonly_values(provider) -> dict[str, Any]:
    import os
    return {
        "PUID": os.environ.get("PUID", ""),
        "PGID": os.environ.get("PGID", ""),
        "TZ": os.environ.get("TZ", ""),
        "RECORDINGS": os.environ.get("RECORDINGS", "/recordings"),
        "CONFIG_FILE": str(provider.config_path),
    }


def _editable_values(snap) -> dict[str, Any]:
    """Project the snapshot into the env-style key map (UI's contract)."""
    return {
        "ADDRESS": snap.address or "",
        "GROUPING": snap.grouping,
        "HTML": snap.use_html_listing,
        "GPS_EXTRACT": snap.gps_extract,
        "DELETE_AFTER_DOWNLOAD": snap.delete_after_download,
        "SYNC_RO_ONLY": snap.sync_ro_only,
        "RETENTION_MAX_DAYS": snap.retention_max_days,
        "RETENTION_DISK_PCT": snap.retention_disk_pct,
        "RETENTION_PROTECT_RO": snap.retention_protect_ro,
        "RECORDINGS_QUOTA_GB": snap.recordings_quota_gb,
        "TIMEOUT": int(snap.timeout),
        "DOWNLOAD_ATTEMPTS": snap.download_attempts,
        "MAX_DOWNLOAD_ATTEMPTS": snap.max_attempts,
        "SYNC_INTERVAL": snap.sync_interval_seconds,
        "ENABLE_SCHEDULED_SYNC": snap.enable_scheduled_sync,
        "WEB_HOST": snap.host,
        "WEB_PORT": snap.port,
        "EXPORT_ENCODER": snap.export_encoder_pref,
        "PIP_POSITION": snap.pip_position,
        "NOMINATIM_EMAIL": snap.nominatim_email,
        "GEOCODE_ENABLED": snap.geocode_enabled,
        "DISTANCE_UNITS": snap.distance_units,
    }


@router.get("", dependencies=[Depends(require_session)])
def get_settings(request: Request) -> dict:
    provider = request.app.state.settings_provider
    snap = provider.get()
    return {
        "editable": _editable_values(snap),
        "readonly": _readonly_values(provider),
        "restart_required_keys": sorted(RESTART_REQUIRED_KEYS),
        "schema": {
            k: type(v).__name__
            for k, v in DEFAULT_VALUES.items()
            if k in EDITABLE_KEYS
        },
    }


@router.put("", dependencies=[Depends(require_session), Depends(require_csrf)])
async def put_settings(request: Request) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    provider = request.app.state.settings_provider
    try:
        snap = provider.update(body, actor="admin")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "editable": _editable_values(snap),
        "readonly": _readonly_values(provider),
        "restart_required_keys": sorted(set(body.keys()) & RESTART_REQUIRED_KEYS),
        "applied_keys": sorted(body.keys()),
    }


class _TestDashcamRequest(BaseModel):
    address: str


@router.post(
    "/test-dashcam",
    dependencies=[Depends(require_session), Depends(require_csrf)],
)
async def test_dashcam(body: _TestDashcamRequest) -> dict:
    return await _probe(body.address)


class _PasswordChange(BaseModel):
    current: str
    new_password: str
    logout_others: bool = False


@router.post(
    "/password",
    dependencies=[Depends(require_session), Depends(require_csrf)],
)
async def change_password(
    request: Request, body: _PasswordChange,
) -> dict:
    auth = request.app.state.auth
    if not auth.check_password(body.current):
        raise HTTPException(status_code=401, detail="current password incorrect")
    provider = request.app.state.settings_provider
    try:
        provider.set_password(body.new_password, actor="admin")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if body.logout_others:
        provider.rotate_session_secret(actor="admin")
        from fastapi.responses import JSONResponse
        resp = JSONResponse({"ok": True})
        auth.issue_session(resp)
        return resp
    return {"ok": True}


@router.post(
    "/restart",
    dependencies=[Depends(require_session), Depends(require_csrf)],
    status_code=202,
)
async def restart(request: Request) -> dict:
    """Schedule a graceful exit. Docker's restart policy brings us back."""
    import asyncio
    import os
    import signal

    if os.environ.get("VIOFOSYNC_RESTART_DISABLED") == "1":
        return {"ok": True}

    async def _bye():
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_bye())
    return {"ok": True}
