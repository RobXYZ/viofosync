"""Download queue persistence + helpers.

Pure-SQLite layer — no asyncio, no threading concerns. The
:class:`SyncWorker` is the only writer during normal operation;
HTTP routes (prioritize, refresh) write between cycles.

State machine (see the plan for rationale):

    pending ──▶ downloading ──▶ done
       ▲            │
       └────────────┘   (transient I/O error; attempts++)
                   │
                   └──▶ failed   (attempts exhausted across 2+ windows)

    pending ──▶ gone   (no longer on the dashcam)
    failed ──▶ pending (manual retry)
    pending ──▶ skipped (user) ──▶ pending (un-skip)
    failed  ──▶ skipped (user)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, List, Optional

from viofosync_lib import downloaded_filename_re
from viofosync_lib.cameras import CAMERA_LETTERS

from ..db import Database


@dataclass
class QueueItem:
    id: int
    filename: str
    source_dir: str
    remote_size: Optional[int]
    recorded_at: Optional[int]
    camera: Optional[str]
    event_type: Optional[str]
    state: str
    priority: int
    attempts: int
    last_error: Optional[str]
    last_attempt_at: Optional[int]


def reconcile(
    db: Database,
    remote_recordings: Iterable,  # iterable of viofosync Recording
    present_filenames: Iterable[str],
) -> dict:
    """Fold a fresh remote listing into the queue.

    - Remote files not in the queue are inserted as ``pending``.
    - Queue rows in state ``pending`` or ``failed`` whose
      filename has vanished from the remote are marked ``gone``.
    - Files already present on disk are marked ``done`` (covers
      the case where the user copied files manually, or a
      previous run finished between cycles).

    Returns a summary dict for logging / UI updates.
    """
    now = int(time.time())
    present = set(present_filenames)
    remote_by_name: dict = {}
    for r in remote_recordings:
        remote_by_name[r.filename] = r

    added = 0
    marked_gone = 0
    marked_done = 0
    refreshed_source_dir = 0
    with db.write() as c:
        existing = {
            row["filename"]: dict(row)
            for row in c.execute(
                "SELECT filename, state, source_dir FROM download_queue"
            ).fetchall()
        }

        for filename, rec in remote_by_name.items():
            if filename in present:
                # Already on disk — record it as done so the
                # queue view shows the full history.
                if filename not in existing:
                    c.execute(
                        """
                        INSERT INTO download_queue
                            (filename, source_dir, remote_size,
                             recorded_at, camera, event_type,
                             state, enqueued_at, finished_at)
                        VALUES (?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            filename,
                            getattr(rec, "filepath", "") or "",
                            getattr(rec, "size", None),
                            int(rec.datetime.timestamp())
                            if getattr(rec, "datetime", None)
                            else None,
                            _camera_from_filename(filename),
                            _event_from_filename(filename),
                            "done",
                            now,
                            now,
                        ),
                    )
                    marked_done += 1
                elif existing[filename]["state"] in ("pending", "failed"):
                    # The clip got onto disk by another path (bulk import,
                    # manual copy) after it was queued. Heal the stale row
                    # instead of re-downloading a file we already have.
                    c.execute(
                        "UPDATE download_queue SET state='done', "
                        "finished_at=? WHERE filename=?",
                        (now, filename),
                    )
                    marked_done += 1
                continue

            if filename in existing:
                # Locking a clip on the dashcam moves it from
                # /DCIM/Movie to /DCIM/Movie/RO. The fresh listing
                # carries the new path; refresh the queue row so
                # the worker doesn't keep retrying the stale URL.
                fresh_source = getattr(rec, "filepath", "") or ""
                if (
                    fresh_source
                    and existing[filename]["source_dir"] != fresh_source
                    and existing[filename]["state"] in ("pending", "failed")
                ):
                    c.execute(
                        "UPDATE download_queue SET source_dir=? "
                        "WHERE filename=?",
                        (fresh_source, filename),
                    )
                    refreshed_source_dir += 1
                continue

            c.execute(
                """
                INSERT INTO download_queue
                    (filename, source_dir, remote_size,
                     recorded_at, camera, event_type,
                     state, enqueued_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    filename,
                    getattr(rec, "filepath", "") or "",
                    getattr(rec, "size", None),
                    int(rec.datetime.timestamp())
                    if getattr(rec, "datetime", None)
                    else None,
                    _camera_from_filename(filename),
                    _event_from_filename(filename),
                    "pending",
                    now,
                ),
            )
            added += 1

        # Anything previously pending/failed but not in the
        # fresh listing has rotated off the card.
        for filename, row in existing.items():
            if row["state"] not in ("pending", "failed"):
                continue
            if filename in remote_by_name:
                continue
            c.execute(
                "UPDATE download_queue SET state='gone', "
                "finished_at=? WHERE filename=?",
                (now, filename),
            )
            marked_gone += 1

    return {
        "added": added,
        "marked_gone": marked_gone,
        "marked_done": marked_done,
        "refreshed_source_dir": refreshed_source_dir,
    }


def _camera_from_filename(filename: str) -> Optional[str]:
    # Handles both ``…_0001F.MP4`` and ``…_0001PF.MP4`` /
    # ``…_0001EF.MP4`` — the optional prefix letter encodes the
    # event type (P=parking, E=event). Older Viofo filenames may omit
    # the camera letter entirely; treat those as front-camera clips.
    m = downloaded_filename_re.match(filename)
    if not m:
        return None
    suffix = (m.group("camera") or "").upper()
    if not suffix:
        return "F"
    camera = suffix[-1]
    return camera if camera in CAMERA_LETTERS else None


def _event_from_filename(filename: str) -> Optional[str]:
    m = downloaded_filename_re.match(filename)
    if not m:
        return None
    suffix = (m.group("camera") or "").upper()
    prefix = suffix[-2] if len(suffix) >= 2 else ""
    return {"P": "parking", "E": "event"}.get(prefix, "normal")


# SQL expressions for deriving camera / event type straight
# from the filename. Used for filtering so we don't depend on
# historical rows having ``camera`` / ``event_type`` populated.
# Filenames end in ``…NNNNN[PE]?[FRTI].MP4`` — the camera letter
# is the character immediately before ``.MP4``, and the byte
# before that is either a digit (normal) or P/E.
_CAM_SQL = "COALESCE(NULLIF(camera, ''), 'F')"
_EVT_PREFIX_SQL = (
    "CASE event_type WHEN 'parking' THEN 'P' "
    "WHEN 'event' THEN 'E' ELSE '' END"
)


def next_pending(
    db: Database, *, ro_only: bool = False,
) -> Optional[QueueItem]:
    """Highest priority, oldest enqueue time. If ``ro_only`` is
    set, only consider rows whose source_dir is under /RO/."""
    sql = (
        "SELECT * FROM download_queue "
        "WHERE state='pending'"
    )
    if ro_only:
        sql += " AND (source_dir LIKE '%/RO/%' OR source_dir LIKE '%/RO')"
    sql += " ORDER BY priority DESC, enqueued_at ASC LIMIT 1"
    with db.conn() as c:
        row = c.execute(sql).fetchone()
    if row is None:
        return None
    return QueueItem(
        id=row["id"],
        filename=row["filename"],
        source_dir=row["source_dir"],
        remote_size=row["remote_size"],
        recorded_at=row["recorded_at"],
        camera=row["camera"],
        event_type=row["event_type"],
        state=row["state"],
        priority=row["priority"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        last_attempt_at=row["last_attempt_at"],
    )


def reconcile_orphan_downloads(db: Database) -> int:
    """Reset rows stuck at ``state='downloading'`` back to
    ``'pending'`` so the next sync cycle picks them up.

    The intended caller is the lifespan startup hook: if the
    worker crashed (or the container was replaced) mid-download,
    those rows have no live owner and would otherwise sit
    "downloading" forever in the UI's queue.

    We deliberately do NOT bump ``attempts`` — an interrupted
    download from a crash is not the same as a failed download
    attempt and shouldn't burn the user's retry budget.

    Returns the number of rows updated.
    """
    with db.write() as c:
        cur = c.execute(
            "UPDATE download_queue "
            "SET state='pending', started_at=NULL "
            "WHERE state='downloading'"
        )
        return cur.rowcount


def mark_downloading(db: Database, item_id: int) -> None:
    with db.write() as c:
        c.execute(
            "UPDATE download_queue SET state='downloading', "
            "started_at=?, attempts=attempts+1, "
            "last_attempt_at=? WHERE id=?",
            (int(time.time()), int(time.time()), item_id),
        )


def mark_done(db: Database, item_id: int) -> None:
    with db.write() as c:
        c.execute(
            "UPDATE download_queue SET state='done', "
            "finished_at=? WHERE id=?",
            (int(time.time()), item_id),
        )


def mark_transient_failure(
    db: Database,
    item_id: int,
    error: str,
    max_attempts: int,
) -> str:
    """Return the new state after a transient failure.

    Transitions back to ``pending`` unless the per-item attempt
    budget is exhausted, in which case it becomes ``failed``.
    """
    with db.write() as c:
        row = c.execute(
            "SELECT attempts FROM download_queue WHERE id=?",
            (item_id,),
        ).fetchone()
        new_state = (
            "failed" if row and row["attempts"] >= max_attempts
            else "pending"
        )
        c.execute(
            "UPDATE download_queue SET state=?, last_error=? "
            "WHERE id=?",
            (new_state, error, item_id),
        )
    return new_state


def mark_cancelled(db: Database, item_id: int) -> None:
    """Return a deliberately-interrupted download (user pause/stop, or
    lost reachability) to ``pending`` without counting it as a failed
    attempt.

    ``mark_downloading`` bumps ``attempts`` on pickup; hand that
    increment back so a pause can't silently exhaust the retry budget.
    Mirrors ``reconcile_orphan_downloads`` — an interrupted download is
    not a failed one.
    """
    with db.write() as c:
        c.execute(
            "UPDATE download_queue SET state='pending', "
            "started_at=NULL, attempts=MAX(attempts-1, 0), "
            "last_error=NULL WHERE id=?",
            (item_id,),
        )


def list_all(db: Database, limit: int = 500) -> List[dict]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM download_queue "
            "ORDER BY priority DESC, enqueued_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# Columns safe to sort by — maps the API name to the SQL column.
_SORT_COLUMNS = {
    "priority": "priority",
    "filename": "filename",
    "date": "recorded_at",
    "size": "remote_size",
    "state": "state",
    "attempts": "attempts",
    # "order" is handled specially — see list_page().
}


def list_page(
    db: Database,
    page: int = 1,
    per_page: int = 100,
    query: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "desc",
) -> dict:
    where = ""
    params: List[object] = []
    if query:
        where = "WHERE filename LIKE ?"
        params.append(f"%{query}%")

    # "order" sorts by actual download order (priority DESC,
    # enqueued_at ASC) — same as the default, but exposed as a
    # clickable column so the user can toggle direction.
    if sort_by == "order":
        # asc = position 1 first (highest priority, earliest enqueue)
        # desc = position last first
        if sort_dir == "asc":
            order = "dq.priority DESC, dq.enqueued_at ASC"
        else:
            order = "dq.priority ASC, dq.enqueued_at DESC"
    else:
        col = _SORT_COLUMNS.get(sort_by)
        direction = "ASC" if sort_dir == "asc" else "DESC"
        if col:
            order = f"dq.{col} {direction}, dq.priority DESC, dq.enqueued_at ASC"
        else:
            order = "dq.priority DESC, dq.enqueued_at ASC"

    with db.conn() as c:
        total = c.execute(
            f"SELECT COUNT(*) AS n FROM download_queue {where}",
            params,
        ).fetchone()["n"]
        if total:
            max_page = ((total - 1) // per_page) + 1
            page = min(page, max_page)

        # Compute queue_position for pending items using a CTE.
        # Position = rank in download order among all pending rows.
        # downloading items get position 0 (currently in-flight).
        # done/failed/gone get NULL.
        rows = c.execute(
            f"""
            WITH positions AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           ORDER BY priority DESC, enqueued_at ASC
                       ) AS queue_position
                FROM download_queue
                WHERE state = 'pending'
            )
            SELECT dq.*,
                   CASE
                       WHEN dq.state = 'downloading' THEN 0
                       ELSE p.queue_position
                   END AS queue_position
            FROM download_queue dq
            LEFT JOIN positions p ON dq.id = p.id
            {where.replace("filename", "dq.filename") if where else ""}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "sort_by": sort_by or "priority",
        "sort_dir": sort_dir,
        "items": [dict(r) for r in rows],
    }


def _day_expr() -> str:
    """SQL expression for the YYYY-MM-DD day key.

    Prefer the dashcam filename date so day grouping stays independent of
    server/browser timezone. Fall back to recorded_at for legacy rows whose
    filenames cannot be parsed by either Viofo naming style.
    """
    return (
        "CASE "
        "WHEN filename GLOB '[0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9]_*' "
        "THEN substr(filename,1,4) || '-' || substr(filename,6,2) || '-' || substr(filename,8,2) "
        "WHEN filename GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*_*' "
        "THEN substr(filename,1,4) || '-' || substr(filename,5,2) || '-' || substr(filename,7,2) "
        "WHEN recorded_at IS NOT NULL "
        "THEN strftime('%Y-%m-%d', recorded_at, 'unixepoch') "
        "ELSE substr(filename,1,4) || '-' || "
        "substr(filename,6,2) || '-' || substr(filename,8,2) END"
    )


_RO_SQL = "source_dir LIKE '%/RO/%'"


def _kind_filters(
    driving: bool,
    parking: bool,
    ro: bool,
    alias: str = "",
) -> tuple[list[str], list[object]]:
    """Build a WHERE clause for the three event-type filters.

    Each flag means "include this category"; clips are partitioned
    so that every clip belongs to exactly one. Read-only takes
    precedence (any clip in ``/RO/``), then Parking (``P`` event
    prefix, not in /RO/), then Driving (everything else).

    All three on → no filter (the partition covers every row).
    Any off → OR-of-included-categories.

    ``alias`` prefixes column refs so the expressions work in
    both aliased and unaliased queries.
    """
    prefix = f"{alias}." if alias else ""
    evt = (
        f"CASE {prefix}event_type "
        "WHEN 'parking' THEN 'P' "
        "WHEN 'event' THEN 'E' "
        "ELSE '' END"
    )
    ro_expr = _RO_SQL.replace("source_dir", f"{prefix}source_dir")

    if driving and parking and ro:
        return [], []
    if not driving and not parking and not ro:
        return ["1 = 0"], []

    parts: list[str] = []
    if ro:
        parts.append(f"({ro_expr})")
    if parking:
        parts.append(f"(NOT ({ro_expr}) AND {evt} = 'P')")
    if driving:
        parts.append(f"(NOT ({ro_expr}) AND {evt} <> 'P')")

    return [f"({' OR '.join(parts)})"], []


def list_days(
    db: Database,
    query: Optional[str] = None,
    driving: bool = True,
    parking: bool = True,
    ro: bool = True,
) -> List[dict]:
    """Return a per-day summary of queue contents.
    Ordered newest day first. Filters by filename if ``query``
    is given; days with no matching files are omitted."""
    clauses: list[str] = []
    params: list[object] = []
    if query:
        clauses.append("filename LIKE ?")
        params.append(f"%{query}%")
    kind_clauses, kind_params = _kind_filters(
        driving, parking, ro
    )
    clauses.extend(kind_clauses)
    params.extend(kind_params)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    day = _day_expr()
    with db.conn() as c:
        rows = c.execute(
            f"""
            SELECT
                {day} AS day,
                COUNT(*) AS clip_count,
                COALESCE(SUM(remote_size), 0) AS total_bytes,
                SUM(CASE WHEN state='pending'     THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN state='downloading' THEN 1 ELSE 0 END) AS downloading_count,
                SUM(CASE WHEN state='done'        THEN 1 ELSE 0 END) AS done_count,
                SUM(CASE WHEN state='failed'      THEN 1 ELSE 0 END) AS failed_count,
                SUM(CASE WHEN state='gone'        THEN 1 ELSE 0 END) AS gone_count,
                SUM(CASE WHEN state='skipped'     THEN 1 ELSE 0 END) AS skipped_count,
                SUM(CASE WHEN {_RO_SQL} THEN 1 ELSE 0 END) AS ro_count,
                SUM(CASE
                    WHEN NOT ({_RO_SQL}) AND {_EVT_PREFIX_SQL} = 'P' THEN 1
                    ELSE 0
                END) AS parking_count,
                SUM(CASE
                    WHEN NOT ({_RO_SQL}) AND {_EVT_PREFIX_SQL} <> 'P' THEN 1
                    ELSE 0
                END) AS driving_count,
                COALESCE(SUM(CASE WHEN state='pending' THEN remote_size ELSE 0 END), 0) AS pending_bytes
            FROM download_queue
            {where}
            GROUP BY {day}
            ORDER BY day DESC
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def list_day_items(
    db: Database,
    day: str,
    query: Optional[str] = None,
    driving: bool = True,
    parking: bool = True,
    ro: bool = True,
) -> List[dict]:
    """Return all queue items for a given day (``YYYY-MM-DD``),
    newest recording first. Filenames start with
    ``YYYY_MMDD_HHMMSS_NN<cam>`` so a plain text DESC sort gives
    reverse time-of-day order with front/rear pairs adjacent.
    ``queue_position`` is still computed against the real
    download order (priority + enqueued_at) so the client can
    show "next up" cues independent of display order.
    """
    day_expr = _day_expr()
    clauses = [f"{day_expr} = ?"]
    params: List[object] = [day]
    if query:
        clauses.append("dq.filename LIKE ?")
        params.append(f"%{query}%")
    kind_clauses, kind_params = _kind_filters(
        driving, parking, ro, alias="dq"
    )
    clauses.extend(kind_clauses)
    params.extend(kind_params)
    where = "WHERE " + " AND ".join(clauses)

    cam_dq = "COALESCE(NULLIF(dq.camera, ''), 'F')"
    evt_dq = (
        "CASE dq.event_type "
        "WHEN 'parking' THEN 'P' "
        "WHEN 'event' THEN 'E' "
        "ELSE '' END"
    )
    ro_dq = _RO_SQL.replace("source_dir", "dq.source_dir")

    with db.conn() as c:
        rows = c.execute(
            f"""
            WITH positions AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           ORDER BY priority DESC, enqueued_at ASC
                       ) AS queue_position
                FROM download_queue
                WHERE state = 'pending'
            )
            SELECT dq.*,
                   CASE
                       WHEN dq.state = 'downloading' THEN 0
                       ELSE p.queue_position
                   END AS queue_position,
                   {cam_dq} AS kind_camera,
                   CASE {evt_dq}
                       WHEN 'P' THEN 'parking'
                       WHEN 'E' THEN 'event'
                       ELSE 'normal'
                   END AS kind_event,
                   CASE WHEN {ro_dq} THEN 1 ELSE 0 END AS kind_ro
            FROM download_queue dq
            LEFT JOIN positions p ON dq.id = p.id
            {where}
            ORDER BY dq.filename DESC
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def pending_bytes(db: Database) -> int:
    """Total ``remote_size`` across all rows in state ``pending``.

    Feeds the session ETA. HTML-listing rows are MB-rounded, which is
    fine for an estimate; rows are corrected to byte-exact sizes after
    each download.
    """
    with db.conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(remote_size), 0) AS n "
            "FROM download_queue WHERE state='pending'"
        ).fetchone()
    return int(row["n"])


def prioritize_recent_hours(db: Database, hours: float) -> int:
    """Bump all pending items recorded in the last ``hours``
    hours to the top of the queue. Returns the count updated."""
    if hours <= 0:
        return 0
    cutoff = int(time.time() - hours * 3600)
    with db.write() as c:
        max_prio = c.execute(
            "SELECT COALESCE(MAX(priority),0) AS m "
            "FROM download_queue"
        ).fetchone()["m"]
        cur = c.execute(
            "UPDATE download_queue SET priority=? "
            "WHERE state='pending' AND recorded_at >= ?",
            (max_prio + 1, cutoff),
        )
        return cur.rowcount


def prioritize(
    db: Database, filenames: List[str], position: str
) -> int:
    """Bump priority so the given filenames run next (``top``)
    or last (``bottom``). Returns the number of rows updated."""
    if not filenames:
        return 0
    with db.write() as c:
        row = c.execute(
            "SELECT COALESCE(MAX(priority),0) AS m, "
            "COALESCE(MIN(priority),0) AS n FROM download_queue"
        ).fetchone()
        target = (row["m"] + 1) if position == "top" else (row["n"] - 1)
        ph = ",".join("?" * len(filenames))
        cur = c.execute(
            f"UPDATE download_queue SET priority=? "
            f"WHERE filename IN ({ph}) AND state='pending'",
            [target] + filenames,
        )
        return cur.rowcount


def retry(db: Database, filenames: List[str]) -> int:
    if not filenames:
        return 0
    with db.write() as c:
        ph = ",".join("?" * len(filenames))
        cur = c.execute(
            f"UPDATE download_queue SET state='pending', "
            f"attempts=0, last_error=NULL "
            f"WHERE filename IN ({ph}) AND state='failed'",
            filenames,
        )
        return cur.rowcount


def skip(db: Database, filenames: List[str]) -> int:
    """Mark the given queued files ``skipped`` so the worker never
    downloads them. Only ``pending``/``failed`` rows change; returns the
    number updated. ``next_pending`` selects only ``pending``, so a skipped
    row is simply never picked up."""
    if not filenames:
        return 0
    with db.write() as c:
        ph = ",".join("?" * len(filenames))
        cur = c.execute(
            f"UPDATE download_queue SET state='skipped' "
            f"WHERE filename IN ({ph}) "
            f"AND state IN ('pending', 'failed')",
            filenames,
        )
        return cur.rowcount


def unskip(db: Database, filenames: List[str]) -> int:
    """Return ``skipped`` files to ``pending`` for downloading again,
    resetting attempts/last_error for a fresh try (mirrors ``retry``).
    Only ``skipped`` rows change; returns the number updated."""
    if not filenames:
        return 0
    with db.write() as c:
        ph = ",".join("?" * len(filenames))
        cur = c.execute(
            f"UPDATE download_queue SET state='pending', "
            f"attempts=0, last_error=NULL "
            f"WHERE filename IN ({ph}) AND state='skipped'",
            filenames,
        )
        return cur.rowcount


def retry_failed(db: Database) -> int:
    """Move every queue item in state ``failed`` back to ``pending``,
    resetting attempts. Returns the count updated."""
    with db.write() as c:
        cur = c.execute(
            "UPDATE download_queue SET state='pending', "
            "attempts=0, last_error=NULL WHERE state='failed'"
        )
        return cur.rowcount


def emit_queue_changed(db: Database, hub, *, loop=None) -> None:
    """Broadcast queue_changed from any caller context. Off-loop
    callers may pass ``loop``; otherwise the hub's bound loop is
    used (threadpool route handlers used to drop the event here)."""
    if hub is None:
        return
    with db.conn() as c:
        rows = c.execute(
            "SELECT state, COUNT(*) AS n FROM download_queue "
            "WHERE state IN ('pending','downloading','failed') "
            "GROUP BY state"
        ).fetchall()
    counts = {"pending": 0, "downloading": 0, "failed": 0}
    for r in rows:
        counts[r["state"]] = r["n"]
    event = {"type": "queue_changed", **counts}
    import asyncio as _asyncio
    try:
        _asyncio.get_running_loop()
        from . import tasks as _tasks
        _tasks.spawn(hub.broadcast(event), name="queue-changed-broadcast")
        return
    except RuntimeError:
        pass
    hub.schedule_broadcast(loop, event)
