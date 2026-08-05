"""Export jobs router."""

from __future__ import annotations

import contextlib
import os
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from ..auth import require_csrf, require_session
from ..services import export_preview
from ..services.naming import (
    CHANNEL_ORDER,
    EXPORT_JOB_TYPES,
    export_download_name,
    parse_clip_ids,
)

# 1x1 transparent PNG — served when a preview can't be produced (job not done,
# unknown, or generation failed), so the <img> degrades cleanly.
_PLACEHOLDER_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

router = APIRouter(
    prefix="/api/exports",
    tags=["exports"],
    dependencies=[Depends(require_session)],
)


def _resolve_default_encoder(app_state) -> str:
    """Resolve the current EXPORT_ENCODER preference against the
    cached encoder probe results into a concrete encoder name.

    Reads the settings snapshot per call so changes made through
    the config GUI take effect for newly created jobs without a
    restart. The encoder probe itself (expensive) stays cached on
    ``app_state.export_encoders``.
    """
    snap = app_state.settings_provider.get()
    pref = snap.export_encoder_pref
    encoders = getattr(app_state, "export_encoders", {}) or {}
    if pref == "auto":
        for name in (
            "videotoolbox", "nvenc", "qsv", "vaapi", "software",
        ):
            if encoders.get(name):
                return name
        return "software"
    if not encoders.get(pref):
        return "software"
    return pref


class Segment(BaseModel):
    channel: str = Field(pattern=f"^({'|'.join(CHANNEL_ORDER)})$")
    start_ts: float
    end_ts: float
    pip: str | None = Field(
        default=None,
        pattern=f"^({'|'.join(CHANNEL_ORDER)})$",
    )


class CreateExport(BaseModel):
    type: str = Field(
        pattern=f"^({'|'.join((*EXPORT_JOB_TYPES, 'timeline'))})$"
    )
    clip_ids: List[int] = []
    segments: list[Segment] | None = None
    encoder: str | None = Field(
        default=None,
        pattern="^(software|videotoolbox|nvenc|qsv|vaapi)$",
    )


@router.get("/encoders")
def list_encoders(request: Request) -> dict:
    """What video encoders this ffmpeg build supports + the
    server's default choice. The UI uses this to populate a
    dropdown limited to encoders that will actually work."""
    encoders = getattr(request.app.state, "export_encoders", {})
    default = _resolve_default_encoder(request.app.state)
    return {
        "available": [k for k, v in encoders.items() if v],
        "default": default,
    }


@router.post("", dependencies=[Depends(require_csrf)])
def create(body: CreateExport, request: Request) -> dict:
    worker = getattr(request.app.state, "export_worker", None)
    if worker is None:
        raise HTTPException(503, "export worker not running")
    encoders = getattr(request.app.state, "export_encoders", {})
    encoder = body.encoder or _resolve_default_encoder(
        request.app.state,
    )
    if not encoders.get(encoder):
        raise HTTPException(
            400,
            f"encoder '{encoder}' not available on this server",
        )
    try:
        if body.type == "timeline":
            segs = [s.model_dump() for s in (body.segments or [])]
            job_id = worker.enqueue_timeline(segs, encoder=encoder)
        else:
            job_id = worker.enqueue(
                body.type, body.clip_ids, encoder=encoder,
            )
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"job_id": job_id, "encoder": encoder}


@router.get("")
def list_jobs(request: Request) -> JSONResponse:
    with request.app.state.db.conn() as c:
        rows = c.execute(
            "SELECT id, type, state, progress, error, "
            "created_at, started_at, finished_at, "
            "clip_start, clip_end, clip_ids, "
            "output_size, output_duration_s "
            "FROM export_jobs ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    recordings = request.app.state.settings_provider.get().recordings
    jobs = []
    for r in rows:
        job = dict(r)
        # clip_count is derived from the always-present clip_ids; the
        # raw id list isn't useful to the UI, so swap it out.
        job["clip_count"] = len(parse_clip_ids(job.pop("clip_ids")))
        # Whether the filmstrip sprite has been generated yet. The worker
        # builds it after the job finishes, so a freshly-done job has none —
        # the UI shows a "generating" placeholder until this flips true.
        sp = export_preview.preview_path(recordings, job["id"])
        job["has_preview"] = (
            job["state"] == "done"
            and os.path.exists(sp)
            and os.path.getsize(sp) > 0
        )
        jobs.append(job)
    return JSONResponse({"jobs": jobs})


@router.get("/{job_id}/download")
def download(job_id: int, request: Request):
    with request.app.state.db.conn() as c:
        row = c.execute(
            "SELECT output_path, state, type, clip_ids "
            "FROM export_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "job not found")
        if row["state"] != "done":
            raise HTTPException(
                409, f"job not ready (state={row['state']})"
            )
        path = row["output_path"]
        if not path or not os.path.isfile(path):
            raise HTTPException(410, "output missing")
        # Best-effort friendly filename from the source clips'
        # timestamps. If retention pruned them we fall back to the
        # legacy name inside export_download_name.
        clip_ids = parse_clip_ids(row["clip_ids"])
        clips = []
        if clip_ids:
            ph = ",".join("?" * len(clip_ids))
            clips = [
                dict(r)
                for r in c.execute(
                    f"SELECT timestamp FROM clip_index "
                    f"WHERE id IN ({ph})",
                    clip_ids,
                ).fetchall()
            ]
    snap = request.app.state.settings_provider.get()
    filename = export_download_name(
        row["type"], clips, job_id, instance_name=snap.instance_name
    )
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=filename,
    )


@router.get("/{job_id}/video")
def video(job_id: int, request: Request):
    """Stream the export output for in-page playback. Unlike ``download``
    this sets no ``filename=`` (no attachment disposition), so a <video>
    element plays it inline; Starlette's FileResponse honours Range
    requests for seeking."""
    with request.app.state.db.conn() as c:
        row = c.execute(
            "SELECT output_path, state FROM export_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "job not found")
    if row["state"] != "done":
        raise HTTPException(409, f"job not ready (state={row['state']})")
    path = row["output_path"]
    if not path or not os.path.isfile(path):
        raise HTTPException(410, "output missing")
    return FileResponse(path, media_type="video/mp4")


@router.get("/{job_id}/filmstrip.jpg")
async def filmstrip_jpg(job_id: int, request: Request):
    # Serve-only: the preview is generated once by the export worker when the
    # job finishes (see ExportWorker._make_export_preview). We never generate
    # at request time — doing so caused a CPU storm when the jobs table
    # re-rendered on every progress tick. Missing preview -> placeholder.
    recordings = request.app.state.settings_provider.get().recordings
    sp = export_preview.preview_path(recordings, job_id)
    if os.path.exists(sp) and os.path.getsize(sp) > 0:
        return FileResponse(
            sp,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )
    return Response(content=_PLACEHOLDER_PNG, media_type="image/png")


@router.delete("/{job_id}", dependencies=[Depends(require_csrf)])
async def delete(job_id: int, request: Request) -> dict:
    # If this is the job currently rendering, kill its ffmpeg first so we
    # don't leave an orphaned encoder running (and writing to a deleted row).
    worker = getattr(request.app.state, "export_worker", None)
    if worker is not None:
        await worker.cancel(job_id)
    with request.app.state.db.write() as c:
        row = c.execute(
            "SELECT output_path FROM export_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "job not found")
        if row["output_path"] and os.path.exists(row["output_path"]):
            try:
                os.remove(row["output_path"])
            except OSError:
                pass
        c.execute("DELETE FROM export_jobs WHERE id=?", (job_id,))
    recordings = request.app.state.settings_provider.get().recordings
    pv = export_preview.preview_path(recordings, job_id)
    with contextlib.suppress(OSError):
        if os.path.exists(pv):
            os.remove(pv)
    return {"ok": True}


@router.post("/{job_id}/pause", dependencies=[Depends(require_csrf)])
async def pause(job_id: int, request: Request) -> dict:
    worker = getattr(request.app.state, "export_worker", None)
    if worker is None:
        raise HTTPException(503, "export worker not running")
    if not await worker.pause(job_id):
        raise HTTPException(409, "job is not currently rendering")
    return {"ok": True, "state": "paused"}


@router.post("/{job_id}/resume", dependencies=[Depends(require_csrf)])
async def resume(job_id: int, request: Request) -> dict:
    worker = getattr(request.app.state, "export_worker", None)
    if worker is None:
        raise HTTPException(503, "export worker not running")
    if not await worker.resume(job_id):
        raise HTTPException(409, "job is not currently paused")
    return {"ok": True, "state": "running"}
