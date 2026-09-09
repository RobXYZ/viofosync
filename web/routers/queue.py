"""Download queue + sync control endpoints."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..auth import require_csrf, require_session
from ..services import queue as q
from ..services.profiles import profile_for
from ..settings_schema import SyncScope

router = APIRouter(
    prefix="/api",
    tags=["queue"],
    dependencies=[Depends(require_session)],
)


def _scopes(request: Request) -> tuple[str | None, str | None]:
    """``(active_scope, other_scope)`` for the queue's held/stranded flags.

    ``active_scope`` is the scope of the connection the sync worker is using,
    or None when offline / not yet cycled / known-offline (then nothing is
    reported as held). ``hub.last_state["dashcam_online"]`` is None until the
    first probe and True/False after — only an explicit False counts as
    offline, since a stale-but-not-yet-disproved ``source`` shouldn't
    suppress held while we simply haven't probed since startup.

    ``other_scope`` is the scope of the connection we are NOT on — the one a
    held clip is "waiting for" — or None when that connection has no address
    configured, so held clips have nothing to wait for and are stranded."""
    worker = getattr(request.app.state, "sync_worker", None)
    if worker is None:
        return None, None
    source = worker.get_status().get("source")
    if not source:
        return None, None
    hub = getattr(request.app.state, "hub", None)
    if hub is not None and hub.last_state.get("dashcam_online") is False:
        return None, None
    snap = request.app.state.settings_provider.get()
    other = "alternative" if source == "primary" else "primary"
    other_addr = snap.address_fallback if other == "alternative" else snap.address
    other_scope = profile_for(snap, other).scope if other_addr else None
    return profile_for(snap, source).scope, other_scope


def _scope_kwargs(request: Request) -> dict:
    scope, other_scope = _scopes(request)
    return {"scope": scope, "other_scope": other_scope}


@router.get("/queue")
def list_queue(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=500),
    search: str | None = Query(None),
    sort_by: str | None = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
) -> dict:
    return q.list_page(
        request.app.state.db,
        page=page,
        per_page=per_page,
        query=search.strip() if search else None,
        sort_by=sort_by,
        sort_dir=sort_dir,
        **_scope_kwargs(request),
    )


@router.get("/queue/days")
def list_queue_days(
    request: Request,
    search: str | None = Query(None),
    driving: bool = Query(True),
    parking: bool = Query(True),
    ro: bool = Query(True),
) -> dict:
    days = q.list_days(
        request.app.state.db,
        query=search.strip() if search else None,
        driving=driving,
        parking=parking,
        ro=ro,
        **_scope_kwargs(request),
    )
    return {"days": days}


@router.get("/queue/day/{day}")
def list_queue_day(
    request: Request,
    day: str,
    search: str | None = Query(None),
    driving: bool = Query(True),
    parking: bool = Query(True),
    ro: bool = Query(True),
) -> dict:
    items = q.list_day_items(
        request.app.state.db,
        day=day,
        query=search.strip() if search else None,
        driving=driving,
        parking=parking,
        ro=ro,
        **_scope_kwargs(request),
    )
    return {"day": day, "items": items}


@router.get("/queue/scope-preview")
def scope_preview(
    request: Request,
    scope: SyncScope = Query(...),
    other_scope: SyncScope | None = Query(None),
) -> dict:
    """What a download scope would do to the current pending backlog.
    Read-only; the UI calls it with the (possibly unsaved) select values —
    ``other_scope`` is the other card's scope (omitted when that card has no
    address), so the preview can say how many clips no connection would ever
    download."""
    counts = q.scope_preview(request.app.state.db, scope, other_scope)
    return {"scope": scope, **counts}


class PrioritizeRecent(BaseModel):
    hours: float = Field(gt=0, le=168)  # max 1 week


@router.post("/queue/prioritize-recent", dependencies=[Depends(require_csrf)])
def prioritize_recent(body: PrioritizeRecent, request: Request) -> dict:
    n = q.prioritize_recent_hours(
        request.app.state.db, body.hours
    )
    q.emit_queue_changed(request.app.state.db, request.app.state.hub)
    worker = getattr(request.app.state, "sync_worker", None)
    if worker is not None:
        worker.kick()
    return {"ok": True, "updated": n}


class Prioritize(BaseModel):
    filenames: List[str]
    position: str = Field(pattern="^(top|bottom)$")


@router.post("/queue/prioritize", dependencies=[Depends(require_csrf)])
def prioritize(body: Prioritize, request: Request) -> dict:
    n = q.prioritize(
        request.app.state.db, body.filenames, body.position
    )
    q.emit_queue_changed(request.app.state.db, request.app.state.hub)
    # Kick the worker so a reorder takes effect right away.
    worker = getattr(request.app.state, "sync_worker", None)
    if worker is not None:
        worker.kick()
    return {"ok": True, "updated": n}


class DownloadNext(BaseModel):
    filenames: List[str] = Field(default_factory=list)


@router.post("/queue/download-next", dependencies=[Depends(require_csrf)])
def download_next(body: DownloadNext, request: Request) -> dict:
    n = q.download_next(request.app.state.db, body.filenames)
    q.emit_queue_changed(request.app.state.db, request.app.state.hub)
    # Kick the worker so the prioritized clips download right away.
    worker = getattr(request.app.state, "sync_worker", None)
    if worker is not None:
        worker.kick()
    return {"ok": True, "updated": n}


class Retry(BaseModel):
    # Omit/empty to retry every failed file; otherwise retry just these.
    filenames: List[str] = Field(default_factory=list)


@router.post("/queue/retry", dependencies=[Depends(require_csrf)])
def retry(body: Retry, request: Request) -> dict:
    if body.filenames:
        n = q.retry(request.app.state.db, body.filenames)
    else:
        n = q.retry_failed(request.app.state.db)
    q.emit_queue_changed(request.app.state.db, request.app.state.hub)
    worker = getattr(request.app.state, "sync_worker", None)
    if worker is not None:
        worker.kick()
    return {"ok": True, "updated": n}


class Skip(BaseModel):
    filenames: List[str] = Field(default_factory=list)


@router.post("/queue/skip", dependencies=[Depends(require_csrf)])
def skip(body: Skip, request: Request) -> dict:
    n = q.skip(request.app.state.db, body.filenames)
    q.emit_queue_changed(request.app.state.db, request.app.state.hub)
    return {"ok": True, "updated": n}


class Unskip(BaseModel):
    filenames: List[str] = Field(default_factory=list)


class Lock(BaseModel):
    filenames: List[str] = Field(default_factory=list)


class DeleteClips(BaseModel):
    filenames: List[str] = Field(default_factory=list)
    # Confirm-through: the UI sends force=True only after a second dialog
    # that explicitly names the protected (read-only / locked) clips.
    force: bool = False


class DeleteFromCamera(BaseModel):
    filenames: List[str] = Field(default_factory=list)


@router.post("/queue/delete-from-camera", dependencies=[Depends(require_csrf)])
def delete_from_camera_route(body: DeleteFromCamera, request: Request) -> dict:
    snap = request.app.state.settings_provider.get()
    addr = snap.address
    if not addr:
        return {"ok": False, "error": "no dashcam address configured"}
    res = q.delete_from_camera(request.app.state.db, body.filenames, f"http://{addr}")
    q.emit_queue_changed(request.app.state.db, request.app.state.hub)
    return {"ok": True, **res}


@router.post("/queue/delete", dependencies=[Depends(require_csrf)])
def delete_clips(body: DeleteClips, request: Request) -> dict:
    recordings = request.app.state.settings_provider.get().recordings
    res = q.delete_clips(
        request.app.state.db, body.filenames, recordings, force=body.force,
    )
    q.emit_queue_changed(request.app.state.db, request.app.state.hub)
    return {"ok": True, **res}


@router.post("/queue/unskip", dependencies=[Depends(require_csrf)])
def unskip(body: Unskip, request: Request) -> dict:
    n = q.unskip(request.app.state.db, body.filenames)
    q.emit_queue_changed(request.app.state.db, request.app.state.hub)
    # Un-skipped files are pending again — wake the worker to pick them up.
    worker = getattr(request.app.state, "sync_worker", None)
    if worker is not None:
        worker.kick()
    return {"ok": True, "updated": n}


@router.post("/queue/lock", dependencies=[Depends(require_csrf)])
def lock(body: Lock, request: Request) -> dict:
    n = q.set_locked(request.app.state.db, body.filenames, True)
    q.emit_queue_changed(request.app.state.db, request.app.state.hub)
    return {"ok": True, "updated": n}


@router.post("/queue/unlock", dependencies=[Depends(require_csrf)])
def unlock(body: Lock, request: Request) -> dict:
    """Clear the user 'retain indefinitely' flag — the reverse of /queue/lock.
    Dashcam-locked clips (event_type='ro') keep that provenance; deleting those
    goes through the delete route's force flag instead."""
    n = q.set_locked(request.app.state.db, body.filenames, False)
    q.emit_queue_changed(request.app.state.db, request.app.state.hub)
    return {"ok": True, "updated": n}


@router.get("/sync/status")
def sync_status(request: Request) -> dict:
    worker = getattr(request.app.state, "sync_worker", None)
    if worker is None:
        return {"running": False, "paused": False, "current_filename": None}
    return worker.get_status()


@router.post("/sync/start", dependencies=[Depends(require_csrf)])
def sync_start(request: Request) -> dict:
    worker = getattr(request.app.state, "sync_worker", None)
    if worker is None:
        return {"ok": False, "error": "sync worker not configured"}
    worker.resume()  # clear paused flag if set
    worker.start()
    worker.kick()
    return {"ok": True}


@router.post("/sync/pause", dependencies=[Depends(require_csrf)])
def sync_pause(request: Request) -> dict:
    worker = getattr(request.app.state, "sync_worker", None)
    if worker is None:
        return {"ok": False}
    worker.pause()
    return {"ok": True}


@router.post("/sync/resume", dependencies=[Depends(require_csrf)])
def sync_resume(request: Request) -> dict:
    worker = getattr(request.app.state, "sync_worker", None)
    if worker is None:
        return {"ok": False}
    worker.resume()
    return {"ok": True}


@router.post("/sync/skip", dependencies=[Depends(require_csrf)])
def sync_skip(request: Request) -> dict:
    """Cancel the current in-flight download and continue
    with the next queue item."""
    worker = getattr(request.app.state, "sync_worker", None)
    if worker is None:
        return {"ok": False}
    worker.skip_current()
    return {"ok": True}


@router.post("/sync/stop", dependencies=[Depends(require_csrf)])
def sync_stop(request: Request) -> dict:
    worker = getattr(request.app.state, "sync_worker", None)
    if worker is None:
        return {"ok": False}
    worker.cancel_current()
    return {"ok": True}


@router.post("/queue/refresh", dependencies=[Depends(require_csrf)])
def refresh(request: Request) -> dict:
    worker = getattr(request.app.state, "sync_worker", None)
    if worker is not None:
        worker.kick()
    return {"ok": True}
