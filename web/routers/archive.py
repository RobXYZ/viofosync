"""Archive browser endpoints.

GET /api/archive/days         paginated day summaries + filters
GET /api/archive/day/{date}   paired clips for a single day
GET /api/archive/day/{date}/route   merged GPX as GeoJSON + journeys
GET /api/archive/clip/{id}/thumb    on-demand ffmpeg thumbnail
GET /api/archive/clip/{id}/video    streamed MP4 with Range support
POST /api/archive/rescan            force a filesystem rescan
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
from collections import defaultdict
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from ..auth import require_csrf, require_session
from ..services import filmstrip, scanner, thumbs
from ..services import gps as gps_service

log = logging.getLogger("viofosync.archive")

router = APIRouter(
    prefix="/api/archive",
    tags=["archive"],
    dependencies=[Depends(require_session)],
)


def _db(request: Request):
    return request.app.state.db


def _settings(request: Request):
    return request.app.state.settings_provider.get()


# --- Listing ---


_KIND_TO_EVENT_TYPE = {
    "driving": "normal",
    "parking": "parking",
    "ro": "ro",
}


def _kind_filter_clause(driving: bool, parking: bool, ro: bool) -> str | None:
    """Build a WHERE fragment for the three event-type filters.

    All on → no filter. All off → ``1 = 0`` (no rows). Otherwise
    an IN-list of the matching ``event_type`` literals.
    """
    if driving and parking and ro:
        return None
    if not (driving or parking or ro):
        return "1 = 0"
    flags = (("driving", driving), ("parking", parking), ("ro", ro))
    enabled = [_KIND_TO_EVENT_TYPE[k] for k, on in flags if on]
    quoted = ", ".join(f"'{e}'" for e in enabled)
    return f"event_type IN ({quoted})"


@router.get("/days")
def list_days(
    request: Request,
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    driving: bool = Query(True),
    parking: bool = Query(True),
    ro: bool = Query(True),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
) -> dict:
    """Paginated list of days with clips."""
    where = []
    params: list = []
    if date_from:
        where.append("group_name >= ?")
        params.append(date_from)
    if date_to:
        where.append("group_name <= ?")
        params.append(date_to)
    kind_clause = _kind_filter_clause(driving, parking, ro)
    if kind_clause is not None:
        where.append(kind_clause)
    w_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with _db(request).conn() as c:
        total = c.execute(
            f"SELECT COUNT(DISTINCT group_name) AS n "
            f"FROM clip_index {w_sql}", params
        ).fetchone()["n"]

        order = "DESC" if sort == "desc" else "ASC"
        offset = (page - 1) * per_page
        rows = c.execute(
            f"""
            SELECT group_name AS day,
                   COUNT(*) AS clip_count,
                   SUM(CASE WHEN event_type='normal'  THEN 1 ELSE 0 END)
                       AS driving_count,
                   SUM(CASE WHEN event_type='parking' THEN 1 ELSE 0 END)
                       AS parking_count,
                   SUM(CASE WHEN event_type='ro'      THEN 1 ELSE 0 END)
                       AS ro_count,
                   SUM(has_gpx) AS gpx_count,
                   MIN(timestamp) AS first_ts,
                   MAX(timestamp) AS last_ts,
                   SUM(size_bytes) AS total_bytes
            FROM clip_index
            {w_sql}
            GROUP BY group_name
            ORDER BY group_name {order}
            LIMIT ? OFFSET ?
            """,
            params + [per_page, offset],
        ).fetchall()

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "days": [dict(r) for r in rows],
    }


@router.get("/day/{date}")
def get_day(
    request: Request,
    date: str,
    time_from: str | None = Query(None),
    time_to: str | None = Query(None),
    driving: bool = Query(True),
    parking: bool = Query(True),
    ro: bool = Query(True),
) -> dict:
    """All clips for a date, paired front+rear by
    (timestamp, sequence)."""
    try:
        _dt.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "bad date format, use YYYY-MM-DD")

    where = ["group_name = ?"]
    params: list = [date]
    kind_clause = _kind_filter_clause(driving, parking, ro)
    if kind_clause is not None:
        where.append(kind_clause)

    with _db(request).conn() as c:
        rows = c.execute(
            f"""
            SELECT id, basename, path, timestamp, camera,
                   sequence, event_type, size_bytes, has_gpx
            FROM clip_index
            WHERE {' AND '.join(where)}
            ORDER BY timestamp DESC, sequence DESC
            """,
            params,
        ).fetchall()

    # In-memory time-range filter (cheap; at most a few hundred rows)
    def _in_range(ts: int) -> bool:
        if time_from is None and time_to is None:
            return True
        t = _dt.datetime.fromtimestamp(ts).time()
        if time_from:
            try:
                if t < _dt.time.fromisoformat(time_from):
                    return False
            except ValueError:
                pass
        if time_to:
            try:
                if t > _dt.time.fromisoformat(time_to):
                    return False
            except ValueError:
                pass
        return True

    # Pair front+rear by (timestamp, event_type). Viofo's F and R
    # from one capture share a timestamp but get consecutive
    # sequence numbers, so keying on sequence wouldn't pair them.
    # event_type keeps parking (PF/PR) separate from normal (F/R).
    # Slot is picked from the last letter so PF/EF still = front.
    pairs: dict[tuple[int, str], dict] = defaultdict(
        lambda: {"front": None, "rear": None, "sequence": None}
    )
    for r in rows:
        if not _in_range(r["timestamp"]):
            continue
        cam = (r["camera"] or "").upper()
        kind = r["event_type"] or "normal"
        key = (r["timestamp"], kind)
        slot = "front" if cam.endswith("F") else "rear"
        pairs[key][slot] = dict(r)
        # Prefer the front sequence number for the pair; fall
        # back to the rear's if there's no front clip.
        if slot == "front" or pairs[key]["sequence"] is None:
            pairs[key]["sequence"] = r["sequence"]

    clips = []
    # Newest first, matching the SQL ORDER BY above.
    for (ts, kind), pair in sorted(pairs.items(), reverse=True):
        clips.append({
            "timestamp": ts,
            "sequence": pair["sequence"],
            "event_type": kind,
            "iso": _dt.datetime.fromtimestamp(ts).isoformat(),
            "front": pair["front"],
            "rear": pair["rear"],
        })

    return {"date": date, "clips": clips}


@router.get("/day/{date}/route")
def get_route(request: Request, date: str) -> dict:
    """Merged GPS track for the day plus detected journeys."""
    try:
        _dt.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "bad date format")

    with _db(request).conn() as c:
        rows = c.execute(
            """
            SELECT path FROM clip_index
            WHERE group_name = ? AND has_gpx = 1
            ORDER BY timestamp ASC
            """,
            (date,),
        ).fetchall()

    gpx_paths = [r["path"] + ".gpx" for r in rows]
    points, stops, journeys = gps_service.aggregate_day(gpx_paths)

    # Synchronous cache lookup — no network. The UI fetches any
    # uncached labels lazily via /geocode after first paint.
    geocoder = getattr(request.app.state, "geocode", None)
    def _lbl(lat, lon):
        return geocoder.cache_lookup(lat, lon) if geocoder else None

    return {
        "date": date,
        "point_count": len(points),
        "journeys": [
            {
                "start_time": j.start_time.isoformat(),
                "end_time": j.end_time.isoformat(),
                "start_ts": j.start_time.timestamp(),
                "end_ts": j.end_time.timestamp(),
                "start_lat": j.start_lat,
                "start_lon": j.start_lon,
                "end_lat": j.end_lat,
                "end_lon": j.end_lon,
                "start_label": _lbl(j.start_lat, j.start_lon),
                "end_label": _lbl(j.end_lat, j.end_lon),
                "distance_m": round(j.distance_m, 1),
                "duration_s": int(
                    (j.end_time - j.start_time).total_seconds()
                ),
                "geojson": {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [p.lon, p.lat] for p in j.points
                        ],
                    },
                },
                "times": [p.t.timestamp() for p in j.points],
            }
            for j in journeys
        ],
        "stops": [
            {
                "start_time": s.start_time.isoformat(),
                "end_time": s.end_time.isoformat(),
                "start_ts": s.start_time.timestamp(),
                "end_ts": s.end_time.timestamp(),
                "duration_s": int(s.duration_s),
                "lat": s.center_lat,
                "lon": s.center_lon,
                "label": _lbl(s.center_lat, s.center_lon),
            }
            for s in stops
        ],
    }


@router.get("/geocode")
async def geocode(
    request: Request,
    lat: float = Query(...),
    lon: float = Query(...),
) -> dict:
    geocoder = getattr(request.app.state, "geocode", None)
    if geocoder is None:
        return {"lat": lat, "lon": lon, "label": None}
    label = await geocoder.reverse(lat, lon)
    return {"lat": lat, "lon": lon, "label": label}


# --- Clip bytes ---


def _fetch_clip(request: Request, clip_id: int) -> dict:
    with _db(request).conn() as c:
        row = c.execute(
            "SELECT id, path, basename, size_bytes, duration_s "
            "FROM clip_index WHERE id = ?",
            (clip_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "clip not found")
    if not os.path.isfile(row["path"]):
        raise HTTPException(410, "clip file missing on disk")
    return dict(row)


@router.get("/clip/{clip_id}/thumb")
async def clip_thumb(request: Request, clip_id: int):
    clip = _fetch_clip(request, clip_id)
    s = _settings(request)
    path = await thumbs.ensure_thumb(
        s.recordings, clip_id, clip["path"]
    )
    if path is None:
        # No ffmpeg or extraction failed — 1x1 transparent PNG
        tiny = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
            b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        return Response(content=tiny, media_type="image/png")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/clip/{clip_id}/filmstrip")
async def clip_filmstrip(request: Request, clip_id: int):
    """Slicing metadata for the clip's filmstrip sprite (generates it
    on demand). 204 when ffmpeg is unavailable so the UI shows
    placeholder tiles."""
    clip = _fetch_clip(request, clip_id)
    s = _settings(request)
    meta = await filmstrip.ensure_filmstrip(
        s.recordings, clip_id, clip["path"], clip.get("duration_s")
    )
    if meta is None:
        return Response(status_code=204)
    return {
        "sprite_url": f"/api/archive/clip/{clip_id}/filmstrip.jpg",
        "frames": meta.frames,
        "interval_s": meta.interval_s,
        "tile_w": meta.tile_w,
        "tile_h": meta.tile_h,
        "duration_s": meta.duration_s,
    }


@router.get("/clip/{clip_id}/filmstrip.jpg")
async def clip_filmstrip_jpg(request: Request, clip_id: int):
    clip = _fetch_clip(request, clip_id)
    s = _settings(request)
    meta = await filmstrip.ensure_filmstrip(
        s.recordings, clip_id, clip["path"], clip.get("duration_s")
    )
    sp = filmstrip.sprite_path(s.recordings, clip_id)
    if meta is None or not os.path.exists(sp):
        raise HTTPException(404, "no filmstrip")
    return FileResponse(sp, media_type="image/jpeg")


@router.get("/clip/{clip_id}/video")
def clip_video(request: Request, clip_id: int):
    """Stream the MP4. ``FileResponse`` handles HTTP Range
    out of the box so <video> seeking works."""
    clip = _fetch_clip(request, clip_id)
    return FileResponse(
        clip["path"],
        media_type="video/mp4",
        filename=clip["basename"],
    )


# --- Maintenance ---


@router.post(
    "/rescan",
    dependencies=[Depends(require_csrf)],
)
async def rescan(request: Request) -> JSONResponse:
    s = _settings(request)
    # Scan on a worker thread so the directory walk doesn't block
    # the event loop. Thumb sweep is fire-and-forget; the on-demand
    # handler covers anything the sweep hasn't reached yet.
    n = await asyncio.to_thread(
        scanner.scan,
        request.app.state.db, s.recordings, s.grouping,
        request.app.state.hub, asyncio.get_running_loop(),
    )
    asyncio.create_task(
        scanner.sweep_missing_thumbs(
            request.app.state.db, s.recordings,
        )
    )
    return JSONResponse({"ok": True, "indexed": n})


# --- GPS extraction for existing clips ---


@dataclass
class GpsExtractStatus:
    running: bool = False
    total: int = 0
    done: int = 0
    extracted: int = 0
    empty: int = 0
    errors: int = 0


def _extract_status(request: Request) -> GpsExtractStatus:
    st = getattr(request.app.state, "gps_extract_status", None)
    if st is None:
        st = GpsExtractStatus()
        request.app.state.gps_extract_status = st
    return st


def _status_dict(st: GpsExtractStatus) -> dict:
    return {
        "running": st.running,
        "total": st.total,
        "done": st.done,
        "extracted": st.extracted,
        "empty": st.empty,
        "errors": st.errors,
    }


def _select_extract_targets(db, *, force: bool) -> list[tuple[int, str]]:
    """Pick clip rows to run GPS extraction over.

    ``force`` returns every indexed clip; the default skips any
    clip that's already been examined (whether the moov atom
    yielded GPS data or not). Without this filter, clips that
    came back empty on a previous run would be re-parsed on
    every click — wasted minutes for a large library.
    """
    sql = "SELECT id, path FROM clip_index"
    if not force:
        sql += " WHERE gps_examined = 0"
    sql += " ORDER BY timestamp ASC"
    with db.conn() as c:
        return [(r["id"], r["path"]) for r in c.execute(sql).fetchall()]


def _mark_examined(db, clip_id: int, *, extracted: bool) -> None:
    """Set ``gps_examined=1`` on the clip row. Lifts ``has_gpx``
    when extraction produced a sidecar (``MAX`` so we never
    clobber a 1 with a 0)."""
    with db.write() as c:
        c.execute(
            "UPDATE clip_index SET "
            "  gps_examined = 1, "
            "  has_gpx = MAX(has_gpx, ?) "
            "WHERE id=?",
            (1 if extracted else 0, clip_id),
        )


def _process_extract_target(
    db, clip_id: int, path: str, *,
    parse_moov, generate_gpx,
) -> str:
    """Run a single GPS extraction. Returns one of ``'extracted'``
    / ``'sidecar_present'`` / ``'empty'`` / ``'error'`` and updates
    the clip row.

    Skips moov parsing when a sidecar already exists on disk —
    after an upgrade that introduced ``gps_examined``, this lets
    a single Extract GPS pass cheaply backfill the flag for
    everything that's already correct on disk, without re-parsing
    a multi-GB library.
    """
    if not os.path.isfile(path):
        _mark_examined(db, clip_id, extracted=False)
        return "error"

    sidecar = path + ".gpx"
    if os.path.isfile(sidecar):
        _mark_examined(db, clip_id, extracted=True)
        return "sidecar_present"

    try:
        with open(path, "rb") as fh:
            gps_data = parse_moov(fh)
    except Exception as e:  # pragma: no cover — corrupt MP4
        log.warning("gps extract failed for %s: %s", path, e)
        _mark_examined(db, clip_id, extracted=False)
        return "error"

    if not gps_data:
        _mark_examined(db, clip_id, extracted=False)
        return "empty"

    try:
        gpx_content = generate_gpx(gps_data, os.path.basename(sidecar))
        with open(sidecar, "w") as f:
            f.write(gpx_content)
    except Exception as e:  # pragma: no cover — write failure
        log.warning("gpx write failed for %s: %s", path, e)
        _mark_examined(db, clip_id, extracted=False)
        return "error"

    _mark_examined(db, clip_id, extracted=True)
    return "extracted"


@router.get("/extract-gps/status")
def extract_gps_status(request: Request) -> dict:
    return _status_dict(_extract_status(request))


@router.post(
    "/extract-gps",
    dependencies=[Depends(require_csrf)],
)
async def extract_gps(
    request: Request,
    force: bool = Query(False),
) -> dict:
    """Kick off a background extraction pass. By default only
    processes clips without a GPX sidecar; pass ``force=true``
    to re-extract every clip (used after filter tweaks)."""
    st = _extract_status(request)
    if st.running:
        raise HTTPException(409, "extraction already running")

    targets = _select_extract_targets(_db(request), force=force)

    if not targets:
        return {"ok": True, "started": False, "total": 0}

    st.running = True
    st.total = len(targets)
    st.done = 0
    st.extracted = 0
    st.empty = 0
    st.errors = 0

    hub = request.app.state.hub
    db = request.app.state.db
    loop = asyncio.get_running_loop()

    await hub.broadcast({
        "type": "gps_extract_started",
        "total": st.total,
    })

    def _work() -> None:
        import viofosync_lib as vfs_lib
        for cid, path in targets:
            basename = os.path.basename(path)
            result = _process_extract_target(
                db, cid, path,
                parse_moov=vfs_lib.parse_moov,
                generate_gpx=vfs_lib.generate_gpx,
            )
            if result == "extracted":
                st.extracted += 1
            elif result == "sidecar_present":
                # Folded into "extracted" for the user-visible
                # counters — they care about "this clip has GPS
                # we can use", not how it got that way.
                st.extracted += 1
            elif result == "empty":
                st.empty += 1
            elif result == "error":
                st.errors += 1
            st.done += 1
            hub.schedule_broadcast(loop, {
                "type": "gps_extract_progress",
                "done": st.done,
                "total": st.total,
                "filename": basename,
                "result": result,
            })
        st.running = False
        hub.schedule_broadcast(loop, {
            "type": "gps_extract_done",
            **_status_dict(st),
        })

    loop.run_in_executor(None, _work)
    return {"ok": True, "started": True, "total": st.total}
