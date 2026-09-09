"""First-run setup wizard routes.

Mounted unconditionally; the routes themselves return 404 once the
app is configured. Setup-mode middleware (web/setup_mode.py) makes
every other route 307 to /setup while we're unconfigured.
"""
from __future__ import annotations

import asyncio
import socket

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from ..settings_schema import validate_new_password

router = APIRouter()


def _require_unconfigured(request: Request) -> None:
    if not request.app.state.settings_provider.get().is_unconfigured:
        raise HTTPException(status_code=404)


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request) -> HTMLResponse:
    _require_unconfigured(request)
    import os
    static = os.path.join(os.path.dirname(__file__), "..", "static", "setup.html")
    with open(static, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@router.post("/setup")
async def setup_submit(
    request: Request,
    response: Response,
    address: str = Form(""),
    password: str = Form(...),
    confirm: str = Form(...),
    scope: str = Form("everything"),
    retention_days: int = Form(0),
    retention_disk_pct: int = Form(0),
    quota_gb: int = Form(0),
    units: str = Form("km"),
    # Absence means unchecked: a bool default cannot tell the two apart.
    geocode: str | None = Form(None),
    home_lat: str = Form(""),
    home_lon: str = Form(""),
    home_radius: int = Form(30),
):
    _require_unconfigured(request)
    if password != confirm:
        raise HTTPException(status_code=400, detail="passwords do not match")
    try:
        validate_new_password(password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    provider = request.app.state.settings_provider

    # DISK_CRITICAL_PCT must stay at or above the retention threshold and
    # is not shown here, so report the limit rather than naming the setting.
    critical = provider.get().disk_critical_pct
    if retention_disk_pct > critical:
        raise HTTPException(
            status_code=400,
            detail=f"Disk limit must be {critical}% or lower.",
        )

    patch: dict = {
        "PRIMARY_SCOPE": scope,
        # Written explicitly, not defaulted in the schema: defaults are
        # merged at read time, so a schema change would also enable triage
        # on existing installs that never set it.
        "PRIMARY_GPS_TRIAGE": True,
        "RETENTION_MAX_DAYS": retention_days,
        "RETENTION_DISK_PCT": retention_disk_pct,
        "RECORDINGS_QUOTA_GB": quota_gb,
        "DISTANCE_UNITS": units,
        "GEOCODE_ENABLED": geocode is not None,
    }
    if address:
        patch["ADDRESS"] = address.strip()

    # Optional, and both coordinates or neither. validate_partial runs
    # normalize_locations, which enforces the single-Home invariant.
    if home_lat.strip() and home_lon.strip():
        try:
            patch["LOCATIONS"] = [{
                "name": "Home",
                "lat": float(home_lat),
                "lon": float(home_lon),
                "radius_m": home_radius,
                "exclude_recordings": False,
                "is_home": True,
            }]
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail="home location is not a valid coordinate"
            ) from e

    try:
        provider.update(patch, actor="setup-wizard")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    provider.set_password(password, actor="setup-wizard")
    redirect = RedirectResponse(url="/", status_code=303)
    request.app.state.auth.issue_session(redirect)
    return redirect


class TestDashcamRequest(BaseModel):
    address: str


@router.post("/api/setup/test-dashcam")
async def test_dashcam(request: Request, body: TestDashcamRequest):
    _require_unconfigured(request)
    # Deliberately NOT restricted to LAN targets: the dashcam is
    # legitimately reachable via a public-resolving name (Tailscale's
    # 100.64/10, dynamic DNS to a port-forwarded camera, etc.), and
    # the dominant risk during the unauthenticated setup window is the
    # setup form itself (account takeover), not this connect probe —
    # so a LAN-only filter here would block real setups for no
    # meaningful security gain. The README warns not to expose the
    # container to the internet during the setup window.
    return await _probe(body.address)


async def _probe(address: str) -> dict:
    """Best-effort TCP-connect probe; returns ok+latency or error."""
    host, _, port_s = address.partition(":")
    port = int(port_s) if port_s.isdigit() else 80
    loop = asyncio.get_running_loop()
    start = loop.time()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, _sync_connect, host, port),
            timeout=3.0,
        )
        return {"ok": True, "latency_ms": int((loop.time() - start) * 1000)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _sync_connect(host: str, port: int) -> None:
    with socket.create_connection((host, port), timeout=3.0):
        pass
