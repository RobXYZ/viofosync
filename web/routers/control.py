"""Camera control routes — read and (safely) adjust dashcam settings.

Backed by :mod:`viofosync_lib._control`, which decodes the camera's settings
against the vendor command map and enforces the destructive-command denylist
plus per-value validation. The camera address comes from the ``ADDRESS``
setting (same one the sync worker uses).

All blocking camera I/O runs in a threadpool so the event loop is never held.
Reads require a session; the single mutating route additionally requires CSRF.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ..auth import require_csrf, require_session
from viofosync_lib import _control as control

router = APIRouter(prefix="/api/camera", tags=["camera"],
                   dependencies=[Depends(require_session)])


def _address(request: Request) -> str:
    """Resolve the configured camera address or 409 if unset."""
    snap = request.app.state.settings_provider.get()
    if not snap.address:
        raise HTTPException(
            status_code=409,
            detail="No camera ADDRESS configured (set it in Settings).")
    return snap.address


def _run(fn, *args):
    """Invoke a blocking control call, mapping control errors to HTTP."""
    try:
        return fn(*args)
    except control.GeofenceNoFix as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except control.DestructiveCommandError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except control.ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except control.CameraUnreachable as e:
        raise HTTPException(
            status_code=502,
            detail=f"Camera unreachable: {e}") from e
    except control.ControlError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/info")
async def get_info(request: Request) -> dict:
    """Firmware, free space, card status and lens/sensor count."""
    addr = _address(request)
    return await run_in_threadpool(_run, control.read_info, addr)


@router.get("/settings")
async def get_settings(request: Request) -> dict:
    """Live decoded settings (cmd=3014), labelled against the command map."""
    addr = _address(request)
    return await run_in_threadpool(_run, control.read_settings, addr)


@router.get("/settings/catalog")
async def get_catalog(request: Request) -> dict:
    """Safely-writable settings with their option lists and current values —
    the data the Camera UI renders its controls from."""
    addr = _address(request)
    return await run_in_threadpool(_run, control.writable_catalog, addr)


class _SetRequest(BaseModel):
    value: str | int


@router.post(
    "/settings/{key}",
    dependencies=[Depends(require_csrf)],
)
async def set_setting(key: str, body: _SetRequest, request: Request) -> dict:
    """Validate and apply one setting (``key`` = CMD_KEY or numeric cmd),
    then read back to verify. Destructive ids are refused with 403."""
    addr = _address(request)
    return await run_in_threadpool(
        _run, control.set_setting, addr, key, body.value)


# --------------------------------------------------------------------------- #
# Live GPS and the camera's own geofence zones
# --------------------------------------------------------------------------- #

def _refs(request: Request) -> list[tuple[float, float]]:
    """Saved locations, used to restore the hemisphere signs the camera
    drops from its coordinates."""
    snap = request.app.state.settings_provider.get()
    return [(p.lat, p.lon) for p in snap.locations]


def _with_model(fn, addr, *args):
    """Run a geofence call after detecting the model from the firmware."""
    model = control.detect_model(
        control._send(addr, control.CMD_SYSTEM_INFO).get("String"))
    return fn(addr, model, *args)


@router.get("/gps")
async def get_gps(request: Request) -> dict:
    """The camera's live GPS fix and satellite signal levels."""
    addr = _address(request)
    return await run_in_threadpool(_run, control.read_gps, addr, _refs(request))


@router.get("/geofence")
async def get_geofence(request: Request) -> dict:
    """The camera's geofence zones (enabled, centre, radius)."""
    addr = _address(request)
    return await run_in_threadpool(
        _run, _with_model, control.read_geofence, addr, _refs(request))


class _ZoneEnable(BaseModel):
    enabled: bool


class _ZoneCapture(BaseModel):
    radius_m: int = Field(ge=control.GEOFENCE_RADIUS_RANGE[0],
                          le=control.GEOFENCE_RADIUS_RANGE[1])


@router.post("/geofence/{zone}", dependencies=[Depends(require_csrf)])
async def set_geofence_zone(zone: int, body: _ZoneEnable,
                            request: Request) -> dict:
    """Enable or disable one zone (1-3). Returns every zone, read back."""
    addr = _address(request)
    return await run_in_threadpool(
        _run, _with_model, control.set_geofence_zone, addr, zone,
        body.enabled, _refs(request))


@router.post("/geofence/{zone}/capture", dependencies=[Depends(require_csrf)])
async def capture_geofence_zone(zone: int, body: _ZoneCapture,
                                request: Request) -> dict:
    """Centre a zone (1-3) on where the camera is now, with this radius.
    422 if the camera has no GPS fix."""
    addr = _address(request)
    return await run_in_threadpool(
        _run, _with_model, control.capture_geofence_zone, addr, zone,
        body.radius_m, _refs(request))
