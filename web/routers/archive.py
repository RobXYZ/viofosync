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
from ..services import durations, filmstrip, route_cache, scanner, thumbs
from ..services import tasks as _tasks
from ..services import gps as gps_service
from ..services import day_tracks
from ..services import geofence as geofence_service
from ..services import queue as q
from ..services.gps import MAX_JOURNEY_BUFFER_S, expand_journey_window, _haversine_ll
from ..services import locations as locations_service
from ..services.naming import (
    CAMERAS,
    CHANNEL_LABELS,
    CHANNEL_ORDER,
    GPS_CAMERA_LETTER,
    channel_of,
    day_key_sql,
    gps_lens_sql,
    pair_slot_of,
)

log = logging.getLogger("viofosync.archive")

# Until ffprobe has populated ``clip_index.duration_s`` (a background sweep
# that can take a while on a large archive), the timeline editor would see
# zero-length clips and render nothing. Fall back to the gap until the next
# clip on the same channel — dashcam clips are contiguous — capped so a
# parking gap can't produce an absurdly long block. The last clip on a
# channel has no successor to measure, so it gets a typical-clip default.
FALLBACK_MAX_S = 300.0
FALLBACK_DEFAULT_S = 60.0

# MAX_JOURNEY_BUFFER_S and expand_journey_window live in services/gps.py so the
# geofence service can share them (a service can't import from this router).

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

    days = [dict(r) for r in rows]
    # GPS Triage: only fold in queued (not-downloaded) clips when triage is on.
    # With triage off, the archive shows downloaded clips only — no placeholder
    # tiles and no remote-only day cards.
    if _settings(request).gps_triage:
        # Annotate every page's days with a remote (queued) count, and on page 1
        # fold in days that ONLY have queued clips (so brand-new footage gets a
        # card to open). Page-1-only keeps remote-only days from repeating on
        # every page; with the default newest-first sort they belong at the top.
        # NOTE: remote-only days are injected/counted only on page 1, so `total`
        # is page-1-inclusive only — the frontend must not derive an exact page
        # count from it across pages. A fully paginated clip_index + queue union
        # is deliberately out of scope (see the plan).
        # GPS-excluded (geofence auto-skipped) clips per day, so the day card
        # can report how much parked-at-home footage was held back.
        gskip = geofence_skipped_by_day(_db(request), date_from, date_to)
        for d in days:
            d["geofence_skipped_count"] = gskip.get(d["day"], 0)
        by_day = {d["day"]: d for d in days}
        for rd in remote_day_summaries(_db(request), date_from, date_to):
            d = by_day.get(rd["day"])
            if d is not None:
                d["remote_count"] = rd["remote_count"]
                d["remote_gps_count"] = rd["remote_gps_count"]
            elif page == 1 and rd["remote_gps_count"]:
                # Only inject a remote-ONLY day once it has GPS-bearing footage
                # to place on a journey. A day of track-less clips (no GPS lock)
                # would otherwise open to an empty/Ungrouped card; that footage
                # stays in the Queue tab until downloaded.
                days.append({
                    "day": rd["day"], "clip_count": 0,
                    "driving_count": 0, "parking_count": 0, "ro_count": 0,
                    "gpx_count": 0, "first_ts": rd["first_ts"],
                    "last_ts": rd["last_ts"], "total_bytes": 0,
                    "remote_count": rd["remote_count"],
                    "remote_gps_count": rd["remote_gps_count"],
                    "geofence_skipped_count": gskip.get(rd["day"], 0),
                })
                total += 1
        days.sort(key=lambda dd: dd["day"], reverse=(sort == "desc"))
    return {"total": total, "page": page, "per_page": per_page, "days": days}


@router.get("/day/{date}")
def get_day(
    request: Request,
    date: str,
    time_from: str | None = Query(None),
    time_to: str | None = Query(None),
    driving: bool = Query(True),
    parking: bool = Query(True),
    ro: bool = Query(True),
    show_geofenced: bool = Query(False),
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
                   sequence, event_type, size_bytes, has_gpx, locked
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

    # Group cameras by (timestamp, event_type). Viofo clips from
    # one capture share a timestamp but get consecutive sequence
    # numbers, so keying on sequence wouldn't pair them.
    # event_type keeps parking (PF/PR) separate from normal (F/R).
    # Slot is the registry channel of the last letter, so PF/EF
    # still = front.
    slots = [c.channel for c in CAMERAS]
    pairs: dict[tuple[int, str], dict] = defaultdict(
        lambda: dict.fromkeys([*slots, "sequence"])
    )
    for r in rows:
        if not _in_range(r["timestamp"]):
            continue
        kind = r["event_type"] or "normal"
        key = (r["timestamp"], kind)
        slot = pair_slot_of(r["camera"])
        pairs[key][slot] = dict(r)
        # Prefer the front sequence number for the pair; fall
        # back to any other camera's if there's no front clip.
        if slot == "front" or pairs[key]["sequence"] is None:
            pairs[key]["sequence"] = r["sequence"]

    clips = []
    # Newest first, matching the SQL ORDER BY above.
    for (ts, kind), pair in sorted(pairs.items(), reverse=True):
        clips.append({
            "timestamp": ts,
            "sequence": pair["sequence"],
            "event_type": kind,
            "locked": 1 if any(
                pair[s] and pair[s].get("locked") for s in slots
            ) else 0,
            "iso": _dt.datetime.fromtimestamp(ts).isoformat(),
            **{s: pair[s] for s in slots},
        })

    # GPS Triage: only when enabled, append queued (not-downloaded) clips as
    # placeholder pairs (respecting the same kind + time-range filters).
    # remote_day_clips only returns clips with a known GPS trace, so track-less
    # footage never snaps onto a journey — it stays in the Queue tab until
    # downloaded. When a (timestamp, event_type) is already covered by a
    # downloaded pair, the remote pair's slots are merged into that pair's
    # EMPTY slots (a half-downloaded capture: front done, rear still queued)
    # rather than dropped — dropping would make the queued rear invisible and
    # undownloadable from the archive. Occupied slots always win, so a
    # downloaded clip never regresses to a ghost placeholder. With triage off,
    # the day view shows downloaded clips only.
    if _settings(request).gps_triage:
        kind_ok = {"normal": driving, "parking": parking, "ro": ro}
        by_key = {(cl["timestamp"], cl["event_type"]): cl for cl in clips}
        for rc in remote_day_clips(_db(request), date, show_geofenced):
            if not kind_ok.get(rc["event_type"], True):
                continue
            if not _in_range(rc["timestamp"]):
                continue
            have = by_key.get((rc["timestamp"], rc["event_type"]))
            if have is not None:
                for s in slots:
                    if have[s] is None and rc[s] is not None:
                        have[s] = rc[s]
                have["locked"] = 1 if any(
                    have[s] and have[s].get("locked") for s in slots
                ) else 0
                continue
            clips.append(rc)
        clips.sort(key=lambda cl: cl["timestamp"], reverse=True)
    return {"date": date, "clips": clips}


def _queue_day_expr() -> str:
    # YYYY-MM-DD from a Viofo filename, format-aware (standard + compact).
    return day_key_sql()


def remote_day_summaries(db, date_from=None, date_to=None) -> list[dict]:
    """Per-day counts of queued, not-yet-downloaded clips, so a day with no
    downloaded clips still gets a card. Counts pairs by front clips only,
    matching the archive's pair-oriented view."""
    day = _queue_day_expr()
    where = [
        # Skipped clips are excluded from download AND dropped from the journey
        # map, so they must not be counted into the archive grid either — they'd
        # otherwise snap onto a journey. They stay visible in the Queue tab.
        "state IN ('pending','failed','downloading')",
        gps_lens_sql(),
    ]
    params: list = []
    if date_from:
        where.append(f"{day} >= ?")
        params.append(date_from)
    if date_to:
        where.append(f"{day} <= ?")
        params.append(date_to)
    with db.conn() as c:
        rows = c.execute(
            f"""
            SELECT {day} AS day,
                   COUNT(*) AS remote_count,
                   SUM(CASE WHEN triaged_at IS NOT NULL AND gps_points > 0
                            THEN 1 ELSE 0 END) AS remote_gps_count,
                   MIN(recorded_at) AS first_ts,
                   MAX(recorded_at) AS last_ts
            FROM download_queue
            WHERE {' AND '.join(where)}
            GROUP BY {day}
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def geofence_skipped_by_day(db, date_from=None, date_to=None) -> dict[str, int]:
    """Per-day count of clips auto-skipped by the home geofence
    (``skip_reason='geofence'``), keyed by ``YYYY-MM-DD``. Counts front clips
    only, matching the archive's pair-oriented day stats. Lets the day card show
    how much footage was excluded as parked-at-home."""
    day = _queue_day_expr()
    where = [
        "state = 'skipped'",
        "skip_reason = 'geofence'",
        gps_lens_sql(),
    ]
    params: list = []
    if date_from:
        where.append(f"{day} >= ?")
        params.append(date_from)
    if date_to:
        where.append(f"{day} <= ?")
        params.append(date_to)
    with db.conn() as c:
        rows = c.execute(
            f"SELECT {day} AS day, COUNT(*) AS n FROM download_queue "
            f"WHERE {' AND '.join(where)} GROUP BY {day}",
            params,
        ).fetchall()
    return {r["day"]: r["n"] for r in rows}


def remote_day_clips(
    db, date: str, include_geofenced: bool = False,
) -> list[dict]:
    """Queued, not-downloaded clips for a day, paired front+rear and flagged
    remote. Mirrors get_day's pair shape so the frontend renders them in the
    same grid/journey layout.

    Only captures with a known GPS trace are returned: the archive is
    journey-organised, so a clip with no track has nowhere to sit and would
    land in "Ungrouped" (or snap onto the wrong journey). Only the GPS-bearing
    lens is ever triaged, so every lens qualifies via its GPS *sibling*
    (``queue.GPS_SIBLING_SQL``: same timestamp prefix, GPS letter) — the
    sibling's ``triaged_at``/``gps_points`` speak for the whole capture. The
    sibling may already be downloaded (``done`` — its real sidecar carries the
    trace) but not ``gone`` (skeleton swept, no trace left). A sibling whose
    queue row was never triaged (downloaded before triage existed, or while it
    was off — ``purge_all`` clears the columns) still qualifies via its
    ``clip_index`` sidecar (``has_gpx=1``), so legacy captures surface their
    queued lenses too. Track-less captures (no GPS lock — e.g. garage
    parking — or not yet triaged) and orphan non-GPS clips stay in the Queue
    tab until they're downloaded. This mirrors ``day_tracks.day_gpx_paths``'
    skeleton filter so a grid tile always has a matching track on the journey
    map.

    With ``include_geofenced`` (the archive's opt-in "GPS-excluded" view),
    geofence-auto-skipped rows are returned too; user-skipped rows never are.
    """
    day = _queue_day_expr()
    state_clause = "dq.state IN ('pending','failed','downloading')"
    if include_geofenced:
        state_clause = (
            "(dq.state IN ('pending','failed','downloading') "
            "OR (dq.state = 'skipped' AND dq.skip_reason = 'geofence'))"
        )
    with db.conn() as c:
        rows = c.execute(
            f"""
            SELECT dq.filename, dq.source_dir, dq.camera, dq.event_type,
                   dq.recorded_at, dq.triaged_at, dq.gps_points, dq.state,
                   dq.skip_reason, dq.locked
            FROM download_queue dq
            WHERE {state_clause}
              AND {day.replace('filename', 'dq.filename')} = ?
              AND (
                EXISTS (
                  SELECT 1 FROM download_queue f
                  WHERE {q.GPS_SIBLING_SQL}
                    AND f.state <> 'gone'
                    AND f.triaged_at IS NOT NULL AND f.gps_points > 0
                )
                OR EXISTS (
                  SELECT 1 FROM clip_index ci
                  WHERE ci.basename >= substr(dq.filename, 1, 16)
                    AND ci.basename < substr(dq.filename, 1, 16) || '~'
                    AND {gps_lens_sql('ci.basename')}
                    AND ci.has_gpx = 1
                )
              )
            ORDER BY dq.recorded_at DESC, dq.filename DESC
            """,
            (date,),
        ).fetchall()

    slots = [cam.channel for cam in CAMERAS]
    pairs: dict = defaultdict(lambda: dict.fromkeys(slots))
    for r in rows:
        src = (r["source_dir"] or "").upper()
        raw = r["event_type"] or "normal"
        # Match scanner._event_type_for, which classifies downloaded clips as
        # only 'ro' / 'parking' / 'normal' (impact 'event' clips collapse to
        # 'normal'). Aligning here keeps get_day's (ts, event_type) de-dup key
        # matching so a downloaded clip never leaves a ghost placeholder.
        kind = "ro" if "/RO/" in src or src.endswith("/RO") else (
            "parking" if raw == "parking" else "normal")
        ts = r["recorded_at"] or 0
        key = (ts, kind)
        slot = pair_slot_of(r["camera"] or "")
        if slot not in slots:
            continue
        pairs[key][slot] = {
            "remote": True,
            "filename": r["filename"],
            "basename": r["filename"],
            "camera": r["camera"],
            "triaged": r["triaged_at"] is not None,
            "gps_points": r["gps_points"] or 0,
            "state": r["state"],
            "skip_reason": r["skip_reason"],
            "locked": r["locked"] or 0,
        }

    out = []
    for (ts, kind), pair in sorted(pairs.items(), reverse=True):
        out.append({
            "remote": True,
            "timestamp": ts,
            "sequence": 0,
            "event_type": kind,
            "locked": 1 if any(
                pair[s] and pair[s].get("locked") for s in slots
            ) else 0,
            "iso": _dt.datetime.fromtimestamp(ts).isoformat() if ts else "",
            **{s: pair[s] for s in slots},
        })
    return out


def build_route_payload(
    db, recordings, date: str, geocoder, places=(),
    include_geofenced: bool = False,
) -> dict:
    """Merged GPS track for a day plus detected journeys/stops, as a
    JSON-able dict. Shared by GET /day/{date}/route and GET /timeline.

    The GPX re-parse is the slow part (tens of seconds on a busy day) and
    only changes when the day's GPX files change, so cache it keyed by a
    signature of those files. Labels are applied after, on every request,
    so they stay current as the geocode cache fills.

    ``geocoder`` is the app's geocoder (or None); only its synchronous
    ``cache_lookup`` is used here — uncached labels are fetched lazily
    by the UI via /geocode after first paint.

    ``include_geofenced`` weaves the day's geofence-skipped skeleton tracks
    into journey detection (the archive's opt-in "GPS-excluded" view), and
    adds per-clip overlay geometry under ``geofenced_tracks`` so the UI can
    draw the recovered stretches distinctly from the rest of the journey.
    """
    gpx_paths = day_tracks.day_gpx_paths(db, recordings, date)
    geo_paths: list[str] = []
    if include_geofenced:
        # The archive's "GPS-excluded" view: geofence-skipped skeletons
        # join journey detection. The cache signature covers the combined
        # file set, so on/off variants are distinct cache entries.
        geo_paths = [
            p for p in day_tracks.geofence_skipped_gpx_paths(
                db, recordings, date)
            if p not in gpx_paths
        ]
        gpx_paths = gpx_paths + geo_paths

    sig = route_cache.signature(gpx_paths)
    payload = route_cache.load(recordings, date, sig)
    if payload is None or "points" not in payload:
        log.info(
            "route: aggregating %d GPX file(s) for %s", len(gpx_paths), date
        )
        points, stops, journeys = gps_service.aggregate_day(gpx_paths)
        log.info(
            "route: aggregated %s -> %d point(s), %d journey(s), %d stop(s)",
            date, len(points), len(journeys), len(stops),
        )
        payload = _assemble_route(date, points, stops, journeys)
        route_cache.store(recordings, date, sig, payload)

    if include_geofenced:
        # Per-clip overlay geometry, applied on read (never cached): lets
        # the UI draw recovered stretches dashed over the journey lines.
        payload["geofenced_tracks"] = _geofenced_tracks(geo_paths)

    _apply_group_windows(db, date, payload, places)
    _trim_stops_to_journeys(payload)
    _reframe_journeys(payload)
    _apply_completion(db, date, payload)
    payload.pop("points", None)  # server-only scratch; don't ship it to clients
    _apply_labels(payload, geocoder, places)
    return payload


def _assemble_route(date: str, points, stops, journeys) -> dict:
    """Build the route payload (no labels — those are applied on read so
    they stay current as the geocode cache fills). This is the expensive-
    to-produce part that gets cached."""
    return {
        "date": date,
        "point_count": len(points),
        "points": [[p.lon, p.lat, p.t.timestamp()] for p in points],
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
                "start_label": None,
                "end_label": None,
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
                "label": None,
            }
            for s in stops
        ],
    }


def _geofenced_tracks(paths: list[str]) -> list[dict]:
    """Per-clip skeleton traces for geofence-skipped clips. Unparseable or
    single-point skeletons are dropped — nothing to draw. Each track carries
    a parallel ``times`` array (one epoch-second per coordinate) so the client
    can scope it to the owning journey's window — without it, every journey
    map on a multi-journey day would draw every day's recovered stretch."""
    tracks = []
    for p in paths:
        try:
            pts = gps_service._parse_gpx(p)
        except Exception:
            continue
        if len(pts) < 2:
            continue
        name = os.path.basename(p)
        if name.endswith(".gpx"):
            name = name[:-4]
        tracks.append({
            "filename": name,
            "times": [pt.t.timestamp() for pt in pts],
            "geojson": {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[pt.lon, pt.lat] for pt in pts],
                },
            },
        })
    return tracks


def _apply_labels(payload: dict, geocoder, places=()) -> None:
    """Fill journey/stop labels. A point inside a named location's radius is
    labelled with the location name regardless of geocoding; otherwise the
    geocode cache is used (uncached labels stay None and are fetched lazily via
    /geocode). Mutates ``payload`` in place; runs on every read so it stays
    current.

    Flags set per point:
    - ``home``/``start_home``/``end_home`` — True only when the matched place
      has ``is_home=True`` (i.e. the designated Home location).
    - ``named``/``start_named``/``end_named`` — True whenever *any* named place
      matched, regardless of whether it is the Home; False for geocoded or
      unresolved labels."""
    def _lookup(lat, lon):
        p = locations_service.match_for_point(places, lat, lon)
        if p is not None:
            return p.name, p.is_home, True
        return (geocoder.cache_lookup(lat, lon) if geocoder else None), False, False
    for j in payload.get("journeys", []):
        j["start_label"], j["start_home"], j["start_named"] = _lookup(j["start_lat"], j["start_lon"])
        j["end_label"], j["end_home"], j["end_named"] = _lookup(j["end_lat"], j["end_lon"])
    for s in payload.get("stops", []):
        s["label"], s["home"], s["named"] = _lookup(s["lat"], s["lon"])


def _trim_stops_to_journeys(payload: dict) -> None:
    """Shrink each parking stop's window so it no longer overlaps an adjacent
    journey's PADDED window (``group_start_ts``/``group_end_ts`` from
    :func:`_apply_group_windows`). The padding claims the pull-away/pull-in
    clips at a drive's edges, so the stop must not also advertise that time —
    otherwise a card reads "stopped for 20 min" while its clips start a couple
    minutes in (the arrival clips render on the drive). Run AFTER
    ``_apply_group_windows``; mutates ``payload['stops']`` in place.

    A stop entirely absorbed by journey padding (trimmed to a non-positive
    span) is dropped: its clips, if any, belong to the flanking drive(s). The
    displayed duration and ISO times are recomputed to match the trimmed
    window, so the card and its clips agree."""
    windows = [
        (j.get("group_start_ts", j["start_ts"]),
         j.get("group_end_ts", j["end_ts"]))
        for j in payload.get("journeys", [])
    ]
    if not windows:
        return
    kept = []
    for s in payload.get("stops", []):
        ss0, se0 = s["start_ts"], s["end_ts"]
        ss, se = ss0, se0
        for gs, ge in windows:
            if gs <= ss0 < ge:        # padded drive covers the stop's start
                ss = max(ss, ge)
            if gs < se0 <= ge:        # padded drive covers the stop's end
                se = min(se, gs)
        if ss >= se:                  # fully absorbed by the drive(s) — drop it
            continue
        if ss != ss0 or se != se0:
            s["start_ts"], s["end_ts"] = ss, se
            s["start_time"], s["end_time"] = _iso_utc(ss), _iso_utc(se)
            s["duration_s"] = int(se - ss)
        kept.append(s)
    payload["stops"] = kept


def _apply_group_windows(
    db, date: str, payload: dict, places=(), now: float | None = None,
) -> None:
    """Pad each journey's window outward for the archive day-grid grouping, so
    pull-away / pull-in clips that sit just outside the raw GPS journey (the GPS
    stop boundary lands ~STOP_RADIUS_M inside the real drive) attach to the
    journey card instead of the neighbouring stop. Adds ``group_start_ts`` /
    ``group_end_ts`` to each journey (= the raw window when there's nothing to
    pad). Mirrors the timeline's ``expand_journey_window`` and is bounded the
    same way by the day's parking clips. Applied on read (not cached) so it
    tracks parking clips and queue state, which the cached payload's signature
    doesn't cover.

    A confirmed shutdown tail (``geofence_service.shutdown_tails``) extends the
    trailing edge past the 120 s cap to the camera's last file: the post-arrival
    footage a bypass-parking camera records before powering off belongs to the
    journey. The extension is clamped to the next journey's padded start — a
    camera that restarts within ``gps.SESSION_GAP_SECONDS`` (30 min) stays in
    the same GPS session, so the next journey can inherit a pre-gap dwell fix
    as its own raw start, and an unclamped extension could run past it. That
    clamp is what keeps adjacent padded windows from overlapping, not the
    recording gap itself. An undecided (possibly still live) dwell does not
    extend — it resolves on a later read.

    A clamped extension can make adjacent windows TOUCH (previously they were
    strictly separated), so ``_reframe_journeys``'s inclusive slice shares
    exactly one fix at the shared boundary. In a same-session restart this
    also means the tail's last minute or two fall outside the arriving
    journey's window and attach to the NEXT (outbound) journey's trace
    instead — surprising if you're only looking at the arriving side.

    ``payload['journeys']`` is chronologically ordered by construction
    (``gps.aggregate_day`` walks sessions and points in ascending time order
    and nothing downstream re-sorts them), so the next list entry is always
    the next journey by start time."""
    journeys = payload.get("journeys")
    if not journeys:
        return
    # Parking clips bound the pad; under triage they live in the queue, not
    # clip_index, so use the unioned source (see day_tracks.day_parking_spans).
    parking_spans = day_tracks.day_parking_spans(db, date)
    tails: list[tuple[float, float]] = []
    zones = locations_service.exclusion_zones(places)
    if zones:
        zone_stops = [
            (s["start_ts"], s["end_ts"])
            for s in payload.get("stops", [])
            if any(
                _haversine_ll(s["lat"], s["lon"], z.lat, z.lon) <= z.radius_m
                for z in zones
            )
        ]
        if zone_stops:
            # A tail's end is at most ss + TAIL_CAP_S + NOMINAL_CLIP_S (<=
            # ss + 960), while the parking disqualifier in shutdown_tails
            # rejects any stop with a parking file in [ss, ss + 1200] — so by
            # construction no parking file can land inside a tail extension,
            # and bypassing the parking-clip bound here is safe.
            tails, _undecided = geofence_service.shutdown_tails(
                db, date, zone_stops,
                [j["end_ts"] for j in journeys], now=now,
            )
    base = [
        expand_journey_window(j["start_ts"], j["end_ts"], parking_spans)
        for j in journeys
    ]
    for i, j in enumerate(journeys):
        gs, ge = base[i]
        # A tail extension must stop short of the next journey's padded
        # start: without this clamp the windows would overlap and
        # _reframe_journeys (which slices points per window, unclamped)
        # would draw the shared dwell fixes on both traces.
        limit = base[i + 1][0] if i + 1 < len(base) else None
        for ts, te in tails:
            if abs(ts - j["end_ts"]) <= geofence_service.ARRIVAL_MATCH_S:
                ge = max(ge, te if limit is None else min(te, limit))
        j["group_start_ts"] = gs
        j["group_end_ts"] = ge


def _iso_utc(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()


def _reframe_journeys(payload: dict) -> None:
    """Re-derive each journey's geometry, endpoints, time-range, duration and
    distance from the merged-point slice inside its padded group window, so the
    trace, the start/end markers, the geocoded labels and the displayed time
    all describe the SAME window the day grid uses to group clips.

    The window only *selects* points; it never fabricates them. When fixes exist
    before the raw GPS start (low-speed maneuvering the stop detector swallowed)
    the start moves back to them. When none do (a cold GPS start with no fix
    until a minute in) the start stays at the first real fix — the line can't be
    drawn where nothing was recorded.

    Runs on read, after ``_apply_group_windows`` (which sets the window from the
    *raw* start/end) and before ``_apply_labels`` (which reads the new
    endpoints). ``payload['points']`` must be present and sorted ascending by
    timestamp."""
    pts = payload.get("points")
    if not pts:
        return
    for j in payload.get("journeys", []):
        gs = j.get("group_start_ts", j["start_ts"])
        ge = j.get("group_end_ts", j["end_ts"])
        sl = [p for p in pts if gs <= p[2] <= ge]
        if len(sl) < 2:
            continue
        j["geojson"] = {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[p[0], p[1]] for p in sl],
            },
        }
        j["times"] = [p[2] for p in sl]
        j["start_ts"], j["end_ts"] = sl[0][2], sl[-1][2]
        j["start_time"], j["end_time"] = _iso_utc(sl[0][2]), _iso_utc(sl[-1][2])
        j["start_lon"], j["start_lat"] = sl[0][0], sl[0][1]
        j["end_lon"], j["end_lat"] = sl[-1][0], sl[-1][1]
        j["duration_s"] = int(sl[-1][2] - sl[0][2])
        dist = 0.0
        for i in range(1, len(sl)):
            dist += _haversine_ll(sl[i - 1][1], sl[i - 1][0], sl[i][1], sl[i][0])
        j["distance_m"] = round(dist, 1)


def _apply_completion(db, date: str, payload: dict) -> None:
    """Attach per-journey data-completeness so the archive can show a pie:
    ``completion`` (downloaded duration / total, 0-1) and ``completion_detail``
    for the tooltip. Front clips only (the GPS-bearing lens; counting the rear
    too would double the duration). Skipped clips are already absent from both
    sources, so they're excluded from the denominator — a fully-geofenced
    journey reads complete. Empty window -> None (the UI omits the pie).

    Runs on read, in the chain ``_apply_group_windows`` -> ``_reframe_journeys``
    -> this. It needs the padded window; it reads ``group_start_ts`` /
    ``group_end_ts``, which ``_reframe_journeys`` doesn't touch, so order with
    that step is safe.
    Duration is estimated uniformly via gap-to-next-front-clip (the
    ``_effective_durations`` fallback) so it works before ffprobe and for
    not-yet-downloaded clips: real ``duration_s`` is preferred when present."""
    journeys = payload.get("journeys")
    if not journeys:
        return
    qday = _queue_day_expr()
    with db.conn() as c:
        dl = c.execute(
            "SELECT timestamp AS ts, duration_s FROM clip_index "
            "WHERE group_name = ? AND upper(substr(camera, -1, 1)) = ? "
            "ORDER BY timestamp ASC",
            (date, GPS_CAMERA_LETTER),
        ).fetchall()
        pend = c.execute(
            f"SELECT recorded_at AS ts FROM download_queue "
            f"WHERE {qday} = ? AND {gps_lens_sql()} "
            f"AND state IN ('pending','failed','downloading') "
            f"AND triaged_at IS NOT NULL AND gps_points > 0 "
            f"ORDER BY recorded_at ASC",
            (date,),
        ).fetchall()

    clips = [(r["ts"], r["duration_s"], True) for r in dl if r["ts"] is not None]
    clips += [(r["ts"], None, False) for r in pend if r["ts"] is not None]
    clips.sort(key=lambda x: x[0])

    eff = []
    for i, (ts, real, dled) in enumerate(clips):
        if real and real > 0:
            d = float(real)
        elif i + 1 < len(clips):
            gap = clips[i + 1][0] - ts
            d = float(min(gap, FALLBACK_MAX_S)) if gap > 0 else FALLBACK_DEFAULT_S
        else:
            d = FALLBACK_DEFAULT_S
        eff.append((ts, d, dled))

    for j in journeys:
        gs = j.get("group_start_ts", j["start_ts"])
        ge = j.get("group_end_ts", j["end_ts"])
        total_s = dl_s = 0.0
        n_total = n_dl = 0
        for ts, d, dled in eff:
            if gs <= ts <= ge:
                total_s += d
                n_total += 1
                if dled:
                    dl_s += d
                    n_dl += 1
        j["completion"] = (
            None if total_s <= 0 else round(dl_s / total_s, 4)
        )
        j["completion_detail"] = {
            "downloaded_s": round(dl_s),
            "total_s": round(total_s),
            "downloaded_clips": n_dl,
            "total_clips": n_total,
        }


@router.get("/day/{date}/route")
def get_route(
    request: Request, date: str, show_geofenced: bool = Query(False),
) -> dict:
    """Merged GPS track for the day plus detected journeys."""
    try:
        _dt.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "bad date format")
    geocoder = getattr(request.app.state, "geocode", None)
    return build_route_payload(
        _db(request), _settings(request).recordings, date, geocoder,
        _settings(request).locations, include_geofenced=show_geofenced,
    )


def _effective_durations(rows) -> dict[int, float]:
    """Map clip id -> a usable duration. Uses the real probed ``duration_s``
    when present; otherwise estimates from the gap to the next clip on the
    same channel (capped), so the editor renders before ffprobe catches up.
    ``rows`` must be ordered by timestamp ascending."""
    by_channel: dict[str, list] = {}
    for r in rows:
        by_channel.setdefault(channel_of(r["camera"]), []).append(r)

    eff: dict[int, float] = {}
    for chrows in by_channel.values():
        for i, r in enumerate(chrows):
            real = r["duration_s"] or 0.0
            if real > 0:
                eff[r["id"]] = float(real)
                continue
            if i + 1 < len(chrows):
                gap = chrows[i + 1]["timestamp"] - r["timestamp"]
                eff[r["id"]] = (
                    float(min(gap, FALLBACK_MAX_S))
                    if gap > 0 else FALLBACK_DEFAULT_S
                )
            else:
                eff[r["id"]] = FALLBACK_DEFAULT_S
    return eff


@router.get("/timeline")
def get_timeline(
    request: Request,
    date: str,
    journey: int | None = Query(None, ge=0),
    driving: bool = Query(True),
    parking: bool = Query(True),
    ro: bool = Query(True),
) -> dict:
    """Everything the timeline editor needs for one journey (or a whole
    day when ``journey`` is omitted): channels present, clips with
    channel + start_ts + duration, time bounds, and the GPS route."""
    try:
        _dt.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "bad date format, use YYYY-MM-DD") from None

    log.info("timeline: open date=%s journey=%s — building route", date, journey)
    geocoder = getattr(request.app.state, "geocode", None)
    db = _db(request)
    route = build_route_payload(
        db, _settings(request).recordings, date, geocoder,
        _settings(request).locations,
    )
    log.info(
        "timeline: route built (%d GPS point(s)) — querying clips",
        route["point_count"],
    )

    start_ts: float | None = None
    end_ts: float | None = None
    if journey is not None:
        journeys = route["journeys"]
        if journey >= len(journeys):
            raise HTTPException(404, "journey index out of range")
        j = journeys[journey]
        # The journey already carries its padded window (group_start_ts/
        # group_end_ts from _apply_group_windows), bounded by the unioned
        # clip_index+download_queue parking source. Use it directly so the
        # editor's clip window matches the day grid and the reframed trace —
        # one definition of the journey's edges, no second (divergent) re-pad.
        start_ts = j["group_start_ts"]
        end_ts = j["group_end_ts"]

    where = ["group_name = ?"]
    params: list = [date]
    kind_clause = _kind_filter_clause(driving, parking, ro)
    if kind_clause is not None:
        where.append(kind_clause)

    with db.conn() as c:
        rows = c.execute(
            f"""
            SELECT id, camera, timestamp, duration_s
            FROM clip_index
            WHERE {' AND '.join(where)}
            ORDER BY timestamp ASC
            """,
            params,
        ).fetchall()

    eff_dur = _effective_durations(rows)

    clips = []
    present: set[str] = set()
    for r in rows:
        ts = r["timestamp"]
        dur = eff_dur[r["id"]]
        if start_ts is not None and (ts > end_ts or (ts + dur) < start_ts):
            continue
        ch = channel_of(r["camera"])
        present.add(ch)
        clips.append({
            "id": r["id"],
            "channel": ch,
            "start_ts": ts,
            "duration_s": dur,
        })

    channels = [
        {"key": k, "label": CHANNEL_LABELS[k]}
        for k in CHANNEL_ORDER
        if k in present
    ]

    if start_ts is None and clips:
        start_ts = min(c["start_ts"] for c in clips)
        end_ts = max(c["start_ts"] + c["duration_s"] for c in clips)
    elif journey is not None and clips:
        # Clamp the padded window to the footage that's actually there: the
        # buffer must reach an adjacent clip, not lead into empty timeline, and
        # a long clip overlapping an edge must not drag bounds past the cap.
        cov_start = min(c["start_ts"] for c in clips)
        cov_end = max(c["start_ts"] + c["duration_s"] for c in clips)
        start_ts = max(start_ts, cov_start)
        end_ts = min(end_ts, cov_end)

    # Each clip block lazy-loads a filmstrip sprite, so this count is how
    # many ffmpeg jobs the editor may kick off — the usual cause of a NAS
    # CPU spike on open.
    log.info(
        "timeline: date=%s journey=%s -> %d clip(s) across %d channel(s)",
        date, journey, len(clips), len(channels),
    )

    return {
        "date": date,
        "journey": journey,
        "bounds": {"start_ts": start_ts, "end_ts": end_ts},
        "channels": channels,
        "clips": clips,
        "gps": route if route["point_count"] > 0 else None,
    }


@router.get("/geocode")
async def geocode(
    request: Request,
    lat: float = Query(...),
    lon: float = Query(...),
) -> dict:
    # A named location wins over reverse-geocoding (local, always available).
    p = locations_service.match_for_point(_settings(request).locations, lat, lon)
    if p is not None:
        return {"lat": lat, "lon": lon, "label": p.name,
                "home": p.is_home, "named": True}
    geocoder = getattr(request.app.state, "geocode", None)
    if geocoder is None:
        return {"lat": lat, "lon": lon, "label": None, "home": False, "named": False}
    label = await geocoder.reverse(lat, lon)
    return {"lat": lat, "lon": lon, "label": label, "home": False, "named": False}


# --- Clip bytes ---


def _fetch_clip(request: Request, clip_id: int) -> dict:
    # Blocking: sqlite connect + an isfile() that can stall for hundreds
    # of ms on a spun-down array. Async handlers must call this via
    # asyncio.to_thread or they stall the event loop (and with it every
    # other request and the progress WebSocket) — a day view issues ~240
    # of these back-to-back.
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
    clip = await asyncio.to_thread(_fetch_clip, request, clip_id)
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
    clip = await asyncio.to_thread(_fetch_clip, request, clip_id)
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
    clip = await asyncio.to_thread(_fetch_clip, request, clip_id)
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
    return JSONResponse({"ok": True, "indexed": n})


@router.post(
    "/rebuild-grouping",
    dependencies=[Depends(require_csrf)],
)
async def rebuild_grouping(request: Request) -> dict:
    """Maintenance: flush stale geofence decisions and the journey/route cache,
    then re-evaluate the geofence from the GPS data already on disk. The
    ``.triage`` skeletons are NOT touched, so this needs no camera and triggers
    no re-triage. Clips the current logic no longer re-skips return to
    ``pending`` and may be downloaded on the next drain — that's intended."""
    s = _settings(request)
    db = _db(request)
    unskipped = await asyncio.to_thread(q.unskip_geofence, db)
    route_cache.clear_all(s.recordings)
    # Reset the worker's per-day signature cache so its incremental sweeps
    # re-evaluate too (not just this one-shot full sweep).
    worker = getattr(request.app.state, "sync_worker", None)
    if worker is not None:
        worker._geofence_seen.clear()
    reskipped = 0
    zones = locations_service.exclusion_zones(s.locations)
    if s.gps_triage and zones:
        reskipped = await asyncio.to_thread(
            geofence_service.sweep_all, db, s.recordings, zones, seen=None
        )
    return {"ok": True, "unskipped": unskipped, "reskipped": reskipped}


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
