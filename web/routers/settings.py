"""Settings CRUD routes (auth + CSRF required)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_csrf, require_session
from ..settings_schema import (
    DEFAULT_VALUES,
    EDITABLE_KEYS,
    MASKED_SECRET,
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
    """Project the snapshot into the env-style key map (UI's contract).

    HEADS-UP when adding a key here: this projection also feeds the
    user-downloadable debug bundle (web/services/debug_bundle.py
    collect_settings), which is designed to be safe to attach to a
    PUBLIC GitHub issue. Secrets must use the MASKED_SECRET sentinel
    (like MQTT_PASSWORD below); anything location- or identity-bearing
    (coordinates, emails, hostnames/addresses) must be redacted in
    collect_settings — add new sensitive keys to its redaction list,
    and extend tests/test_debug_bundle_collectors.py to pin it.
    """
    return {
        "ADDRESS": snap.address or "",
        "ADDRESS_FALLBACK": snap.address_fallback or "",
        "IMPORT_PATH": snap.import_path,
        "GROUPING": snap.grouping,
        "HTML": snap.use_html_listing,
        "GPS_EXTRACT": snap.gps_extract,
        "GPS_TRIAGE": snap.gps_triage,
        "DERIVE_THUMBS_EAGER": snap.derive_thumbs_eager,
        "DERIVE_FILMSTRIPS_EAGER": snap.derive_filmstrips_eager,
        "DELETE_AFTER_DOWNLOAD": snap.delete_after_download,
        "SYNC_RO_ONLY": snap.sync_ro_only,
        "LOCATIONS": [
            {"name": p.name, "lat": p.lat, "lon": p.lon,
             "radius_m": p.radius_m, "exclude_recordings": p.exclude_recordings,
             "is_home": p.is_home}
            for p in snap.locations
        ],
        "RETENTION_MAX_DAYS": snap.retention_max_days,
        "RETENTION_DISK_PCT": snap.retention_disk_pct,
        "RETENTION_PROTECT_RO": snap.retention_protect_ro,
        "RECORDINGS_QUOTA_GB": snap.recordings_quota_gb,
        "DISK_CRITICAL_PCT": snap.disk_critical_pct,
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
        "INSTANCE_NAME": snap.instance_name,
        "MQTT_ENABLED": snap.mqtt_enabled,
        "MQTT_HOST": snap.mqtt_host,
        "MQTT_PORT": snap.mqtt_port,
        "MQTT_USERNAME": snap.mqtt_username,
        # Write-only: never echo the stored secret. The sentinel
        # round-trips as "unchanged" (see validate_partial).
        "MQTT_PASSWORD": MASKED_SECRET if snap.mqtt_password else "",
        "MQTT_TLS": snap.mqtt_tls,
        "MQTT_CLIENT_ID": snap.mqtt_client_id,
        "MQTT_DISCOVERY_PREFIX": snap.mqtt_discovery_prefix,
        "MQTT_NODE_ID": snap.mqtt_node_id,
        "MQTT_DISCOVERY_ENABLED": snap.mqtt_discovery_enabled,
        "MQTT_QOS": snap.mqtt_qos,
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

    from ..services import tasks as _tasks
    _tasks.spawn(_bye(), name="graceful-restart")
    return {"ok": True}
