"""Support/debug bundle endpoint."""
from __future__ import annotations

import asyncio
import datetime
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from ..auth import require_session
from ..services import debug_bundle

router = APIRouter(
    prefix="/api",
    tags=["debug"],
    dependencies=[Depends(require_session)],
)

# Bundle generation probes the (single-threaded) camera, so two concurrent
# requests can stall a live sync. Only one generation runs at a time; a
# second request is rejected outright rather than queued.
_inflight = asyncio.Lock()


@router.get("/debug-bundle")
async def download_bundle(request: Request) -> Response:
    if _inflight.locked():
        raise HTTPException(
            status_code=429,
            detail="a debug bundle is already being generated",
        )
    state = request.app.state
    async with _inflight:
        snap = state.settings_provider.get()
        md = await asyncio.to_thread(
            debug_bundle.build_bundle,
            state.db, snap,
            encoders=getattr(state, "export_encoders", None),
        )
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S")
    instance = re.sub(r"[^A-Za-z0-9_-]", "",
                      getattr(snap, "instance_name", "") or "") or "viofosync"
    filename = f"viofosync-debug-{instance}-{stamp}.md"
    return Response(
        content=md,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
