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

import logging
import os
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

import viofosync_lib as vfs
from viofosync_lib.cameras import (
    CAMERA_LETTERS,
    GPS_CAMERA_LETTER,
    is_gps_camera,
)

from ..db import Database
from ..settings_schema import SCOPES
from .naming import camera_letter_sql, capture_key_sql, day_key_sql, gps_sibling_sql
from .triage import TRIAGE_MAX_ATTEMPTS

# INFO-level here is persisted to the app_log table by DBLogHandler (the
# "viofosync.*" namespace is captured at INFO) — so user-initiated archive
# mutations (delete/skip/unskip/retry/download-next/prioritize) leave an
# audit trail in the activity log, not just the console.
log = logging.getLogger("viofosync.queue")


def _names(filenames: List[str], limit: int = 20) -> str:
    """Compact, log-friendly rendering of a filename list (truncated)."""
    shown = ", ".join(filenames[:limit])
    extra = len(filenames) - limit
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def _gps_state(d: dict) -> Optional[str]:
    """Derive the per-file GPS indicator from triage columns.

    'ok'      = GPS fetched (gps_points > 0)
    'none'    = triaged with no fix, OR gave up after MAX unreadable attempts
    'pending' = still awaiting triage
    None      = non-GPS lens with no GPS sibling at its timestamp (orphan) —
                there is no capture-level GPS fact to imply

    GPS is a capture-level fact carried by one lens (the front): that row's
    own triage columns speak for it, and the other lenses (rear/tele/interior,
    which are never triaged themselves) *inherit* the state from their GPS
    sibling's columns — selected as ``sib_*`` by the listing queries via
    ``GPS_SIBLING_SQL``.

    Camera identity is taken from the filename via the registry, not the
    ``camera`` column, so historical rows with a NULL ``camera`` still resolve.
    """
    if is_gps_camera(_camera_from_filename(d.get("filename") or "")):
        triaged_at = d.get("triaged_at")
        gps_points = d.get("gps_points")
        attempts = d.get("triage_attempts")
    elif d.get("sib_id") is not None:
        triaged_at = d.get("sib_triaged_at")
        gps_points = d.get("sib_gps_points")
        attempts = d.get("sib_triage_attempts")
    else:
        return None
    if (gps_points or 0) > 0:
        return "ok"
    if triaged_at is not None:
        return "none"            # triaged, no GPS fix
    if (attempts or 0) >= TRIAGE_MAX_ATTEMPTS:
        return "none"            # gave up after MAX unreadable attempts
    return "pending"


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
    - A ``gone`` clip that reappears in the listing is revived to
      ``pending``: the listing can under-report (the camera's HTML
      directory page lags the card by minutes, and a truncated scrape
      still parses), and nothing else ever re-queues such a row —
      ``retry`` only touches ``failed``.

    An *empty* listing never marks anything gone. A camera that lists
    zero files while the queue still tracks rows is a failed or partial
    response far more often than a wiped card, and ``gone`` would
    otherwise wipe the queue in one pass. This mirrors the scanner's
    empty-scan prune guard. A card that really was formatted resolves
    itself: the rows stay pending, fail to download, and land in
    ``failed`` — noisy but recoverable, unlike a false ``gone``.

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
    revived = 0
    gone_skipped = 0
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
                elif existing[filename]["state"] in (
                    "pending", "failed", "gone",
                ):
                    # The clip got onto disk by another path (bulk import,
                    # manual copy) after it was queued. Heal the stale row
                    # instead of re-downloading a file we already have.
                    # 'gone' lands here too: we hold the file and the
                    # camera still lists it, so the gone was wrong and
                    # 'done' is the truthful state.
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
                if existing[filename]["state"] == "gone":
                    # Back on the card, so a previous reconcile goned it
                    # from an under-reporting listing. Requeue with a
                    # clean slate and whatever path the camera reports
                    # now.
                    c.execute(
                        "UPDATE download_queue SET state='pending', "
                        "attempts=0, last_error=NULL, finished_at=NULL, "
                        "requested_at=NULL, "
                        "source_dir=? WHERE filename=?",
                        (fresh_source or existing[filename]["source_dir"],
                         filename),
                    )
                    revived += 1
                    continue
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

        # Anything previously pending/failed but not in the fresh
        # listing has rotated off the card — unless the listing is
        # empty, which is the shape of a failed response, not of a
        # camera holding no footage (see the docstring).
        tracked = [
            filename for filename, row in existing.items()
            if row["state"] in ("pending", "failed")
        ]
        if not remote_by_name and tracked:
            gone_skipped = len(tracked)
            log.warning(
                "empty listing with %d queued clip(s) — not marking them "
                "gone; treating it as a failed listing, not an empty card",
                gone_skipped,
            )
        else:
            for filename in tracked:
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
        "revived": revived,
        "gone_skipped": gone_skipped,
    }


def _suffix_letters(filename: str) -> Optional[str]:
    """The trailing ``[PE]?<camera>`` letters of a recording filename, or
    ``None`` if the name isn't a recording or the letters aren't a shape
    the registry knows.

    Parsing goes through ``downloaded_filename_re`` so every on-disk
    layout it accepts is understood here too — restating the pattern is
    what left A129 Pro rows with a NULL camera, which collapsed their
    front/rear pairs into one slot and broke the parking/event filters.
    Compact single-channel names carry no letters at all; the sole lens
    is the GPS-bearing one, so they read as ``""``.
    """
    m = vfs.downloaded_filename_re.match(filename)
    if not m:
        return None
    letters = m.group("camera").upper()
    if not letters:
        return ""
    if letters[-1] not in CAMERA_LETTERS.upper():
        return None
    if len(letters) > 2 or (len(letters) == 2 and letters[0] not in "PE"):
        return None
    return letters


def _camera_from_filename(filename: str) -> Optional[str]:
    letters = _suffix_letters(filename)
    if letters is None:
        return None
    return letters[-1] if letters else GPS_CAMERA_LETTER


def _event_from_filename(filename: str) -> Optional[str]:
    letters = _suffix_letters(filename)
    if letters is None:
        return None
    return {"P": "parking", "E": "event"}.get(letters[:-1], "normal")


# SQL expressions for deriving camera / event type straight
# from the filename. Used for filtering so we don't depend on
# historical rows having ``camera`` / ``event_type`` populated.
# The camera letter comes from naming.camera_letter_sql (format-aware:
# suffix-less compact names default to the GPS lens); for standard names
# the byte before the letter is either a digit (normal) or P/E, and for
# compact names it is always a digit — which the CASE consumers already
# read as 'normal'.
_CAM_SQL = camera_letter_sql()
_EVT_PREFIX_SQL = "upper(substr(filename, -6, 1))"

# Single source of truth for "is this clip under RO", accepting a source_dir
# that ends in /RO as well as one containing /RO/. Every consumer (scope, kind
# filters, geofence) shares it so their answers can't diverge.
_RO_SQL = "(source_dir LIKE '%/RO/%' OR source_dir LIKE '%/RO')"


def scope_sql(scope: str, alias: str = "") -> str:
    """WHERE fragment (binds no params) selecting rows INSIDE ``scope``.

    RO clips are in scope for everything except ``nothing``: a locked parking
    clip still downloads under ``no_parking`` and ``ro_only``.
    ``alias`` prefixes column refs (e.g. ``"dq"``) for aliased queries.
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown scope: {scope!r}")
    if scope == "everything":
        return "1"
    if scope == "nothing":
        return "0"
    prefix = f"{alias}." if alias else ""
    ro = _RO_SQL.replace("source_dir", f"{prefix}source_dir")
    evt = _EVT_PREFIX_SQL.replace("filename", f"{prefix}filename")
    if scope == "ro_only":
        return ro
    return f"({ro} OR {evt} <> 'P')"   # no_parking


def _held_sql(scope: Optional[str], alias: str = "") -> str:
    """CASE expression → 1 for a pending row the active connection will not
    download (out of scope and not explicitly requested), else 0. With no
    scope (offline / unknown connection) nothing is held."""
    if scope is None:
        return "0"
    prefix = f"{alias}." if alias else ""
    return (
        f"CASE WHEN {prefix}state = 'pending' "
        f"AND {prefix}requested_at IS NULL "
        f"AND NOT ({scope_sql(scope, alias=alias)}) THEN 1 ELSE 0 END"
    )


def _stranded_sql(
    scope: Optional[str], other_scope: Optional[str], alias: str = "",
) -> str:
    """CASE expression → 1 for a held row (see :func:`_held_sql`) that the
    other connection's scope excludes too, so no connection will ever take
    it — the UI must not promise "waiting for primary" when the primary
    itself would skip the clip. ``other_scope`` None means there is no other
    connection (address blank), so every held row is stranded. Always a
    subset of ``held``; "0" when nothing is held."""
    if scope is None:
        return "0"
    held = _held_sql(scope, alias=alias)
    if other_scope is None:
        return held
    return (
        f"CASE WHEN ({held}) = 1 "
        f"AND NOT ({scope_sql(other_scope, alias=alias)}) THEN 1 ELSE 0 END"
    )


# Correlated match for a row's GPS-bearing sibling: ``f`` is the sibling
# candidate, ``dq`` the row being tested. See naming.gps_sibling_sql for the
# pairing rule (timestamp-prefix range, format-aware GPS-lens test; binds no
# parameters — the registry letter is interpolated).
GPS_SIBLING_SQL = gps_sibling_sql()

# GPS-sibling join + columns for the listing queries: exposes the sibling's
# triage columns as ``sib_*`` so :func:`_gps_state` can imply a non-GPS lens's
# badge. Binds no parameters. Assumes one GPS-lens file per capture
# timestamp — the same uniqueness every pairing consumer relies on (get_day,
# the triage gate, remote_day_clips).
_SIB_COLS_SQL = (
    "f.id AS sib_id, f.triaged_at AS sib_triaged_at, "
    "f.gps_points AS sib_gps_points, f.triage_attempts AS sib_triage_attempts"
)
_SIB_JOIN_SQL = f"LEFT JOIN download_queue f ON {GPS_SIBLING_SQL}"

# The capture key of the newest non-gone queue row — the only capture that
# can still be actively recording. Its lenses are held until remote_complete=1.
_CAPTURE_KEY_SQL = capture_key_sql("dq.filename")
_NEWEST_CAPTURE_SQL = (
    "SELECT MAX(" + capture_key_sql("filename") + ") "
    "FROM download_queue WHERE state <> 'gone'"
)


def next_pending(
    db: Database, *, scope: str = "everything", triage_gate: bool = False,
    active_guard: bool = False,
) -> Optional[QueueItem]:
    """Highest priority, oldest enqueue time, restricted to rows inside the
    active connection's ``scope`` (see :func:`scope_sql`). A row the user
    explicitly requested (``requested_at`` set by ``download_next``) bypasses
    scope — pressing "Download next" on the VPN means it. Rows excluded by the
    **scope** clause are exactly the rows the read APIs report as ``held``.

    If ``triage_gate`` is set (GPS_TRIAGE on), a row is held back while its
    GPS-bearing sibling is still awaiting triage (``triaged_at IS NULL AND
    triage_attempts < TRIAGE_MAX_ATTEMPTS``) — so we never download a clip, or
    its paired lenses, ahead of triage. The sibling is the GPS-bearing lens
    (letter from the registry) sharing the filename's timestamp prefix — NOT
    the full stem: same-capture lenses share the recording second but can carry
    different sequence numbers (see ``GPS_SIBLING_SQL``). The GPS lens is its
    own sibling, and an orphan non-GPS clip (no GPS sibling) is not gated.
    If ``active_guard`` is set, every lens of the newest capture group is held
    until its ``remote_complete`` flag is set, since that capture may still be
    recording."""
    sql = "SELECT dq.* FROM download_queue dq WHERE dq.state='pending'"
    params: List[object] = []
    sql += f" AND (dq.requested_at IS NOT NULL OR {scope_sql(scope, alias='dq')})"
    if triage_gate:
        sql += (
            " AND NOT EXISTS ("
            f"  SELECT 1 FROM download_queue f"
            f"  WHERE {GPS_SIBLING_SQL}"
            f"    AND f.state='pending'"
            f"    AND f.triaged_at IS NULL"
            f"    AND f.triage_attempts < ?"
            " )"
        )
        params.append(TRIAGE_MAX_ATTEMPTS)
    if active_guard:
        sql += (
            f" AND NOT ({_CAPTURE_KEY_SQL} = ({_NEWEST_CAPTURE_SQL})"
            f"          AND dq.remote_complete IS NULL)"
        )
    sql += " ORDER BY dq.priority DESC, dq.enqueued_at ASC LIMIT 1"
    with db.conn() as c:
        row = c.execute(sql, params).fetchone()
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
    scope: Optional[str] = None,
    other_scope: Optional[str] = None,
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
                   END AS queue_position,
                   {_held_sql(scope, alias="dq")} AS held,
                   {_stranded_sql(scope, other_scope, alias="dq")} AS stranded,
                   {_SIB_COLS_SQL}
            FROM download_queue dq
            LEFT JOIN positions p ON dq.id = p.id
            {_SIB_JOIN_SQL}
            {where.replace("filename", "dq.filename") if where else ""}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
    items = [dict(r) for r in rows]
    for d in items:
        d["gps_state"] = _gps_state(d)
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "sort_by": sort_by or "priority",
        "sort_dir": sort_dir,
        "items": items,
    }


def _day_expr() -> str:
    """SQL expression for the YYYY-MM-DD day key derived from the filename
    (format-aware — see naming.day_key_sql). Uses the filename rather than
    ``recorded_at`` so grouping is consistent even for rows missing a
    timestamp."""
    return day_key_sql()


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
    evt = _EVT_PREFIX_SQL.replace("filename", f"{prefix}filename")
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
    scope: Optional[str] = None,
    other_scope: Optional[str] = None,
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
    held = _held_sql(scope)
    stranded = _stranded_sql(scope, other_scope)
    with db.conn() as c:
        rows = c.execute(
            f"""
            SELECT
                {day} AS day,
                COUNT(*) AS clip_count,
                COALESCE(SUM(remote_size), 0) AS total_bytes,
                SUM(CASE WHEN state='pending' AND ({held}) = 0 THEN 1 ELSE 0 END) AS pending_count,
                SUM({held}) AS held_count,
                SUM({stranded}) AS stranded_count,
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
    scope: Optional[str] = None,
    other_scope: Optional[str] = None,
) -> List[dict]:
    """Return all queue items for a given day (``YYYY-MM-DD``),
    newest recording first. Filenames start with
    ``YYYY_MMDD_HHMMSS_NN<cam>`` so a plain text DESC sort gives
    reverse time-of-day order with front/rear pairs adjacent.
    ``queue_position`` is still computed against the real
    download order (priority + enqueued_at) so the client can
    show "next up" cues independent of display order.
    """
    day_expr = _day_expr().replace("filename", "dq.filename")
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

    cam_dq = _CAM_SQL.replace("filename", "dq.filename")
    evt_dq = _EVT_PREFIX_SQL.replace("filename", "dq.filename")
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
                   {_held_sql(scope, alias="dq")} AS held,
                   {_stranded_sql(scope, other_scope, alias="dq")} AS stranded,
                   {cam_dq} AS kind_camera,
                   CASE {evt_dq}
                       WHEN 'P' THEN 'parking'
                       WHEN 'E' THEN 'event'
                       ELSE 'normal'
                   END AS kind_event,
                   CASE WHEN {ro_dq} THEN 1 ELSE 0 END AS kind_ro,
                   {_SIB_COLS_SQL}
            FROM download_queue dq
            LEFT JOIN positions p ON dq.id = p.id
            {_SIB_JOIN_SQL}
            {where}
            ORDER BY dq.filename DESC
            """,
            params,
        ).fetchall()
    items = [dict(r) for r in rows]
    for d in items:
        d["gps_state"] = _gps_state(d)
    return items


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


def scope_preview(
    db: Database, scope: str, other_scope: Optional[str] = None,
) -> dict:
    """{"now": pending rows the given scope would download,
        "held": pending rows it would leave waiting,
        "stranded": the held rows ``other_scope`` would not download either}.
    Explicitly requested rows (``requested_at`` set) count as ``now``.
    ``other_scope`` None = no other connection, so ``stranded == held``.
    Reuses ``_held_sql`` / ``_stranded_sql`` so this preview and the queue's
    badges can never disagree."""
    held = _held_sql(scope)
    stranded = _stranded_sql(scope, other_scope)
    with db.conn() as c:
        row = c.execute(
            f"SELECT SUM({held}) AS held_n, SUM(1 - ({held})) AS now_n, "
            f"SUM({stranded}) AS stranded_n "
            f"FROM download_queue WHERE state='pending'"
        ).fetchone()
    return {
        "now": int(row["now_n"] or 0),
        "held": int(row["held_n"] or 0),
        "stranded": int(row["stranded_n"] or 0),
    }


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
        if cur.rowcount:
            log.info("archive prioritize (%s): %d clip(s) — %s",
                     position, cur.rowcount, _names(filenames))
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
        if cur.rowcount:
            log.info("archive retry: %d failed clip(s) requeued — %s",
                     cur.rowcount, _names(filenames))
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
            f"UPDATE download_queue SET state='skipped', skip_reason='user', "
            f"requested_at=NULL "
            f"WHERE filename IN ({ph}) "
            f"AND state IN ('pending', 'failed')",
            filenames,
        )
        if cur.rowcount:
            log.info("archive skip: %d clip(s) skipped — %s",
                     cur.rowcount, _names(filenames))
        return cur.rowcount


def delete_clips(
    db: Database, filenames: List[str], recordings: str, *, force: bool = False,
) -> dict:
    """User-initiated delete: remove downloaded files + clip_index rows and mark
    the queue rows skipped. Clips the user has pinned read-only (clip_index or
    download_queue locked=1) or dashcam-locked (event_type='ro') are refused and
    reported as 'protected' — unless ``force`` is True, the confirm-through
    path: the UI has already shown a second "delete anyway?" dialog covering
    exactly those clips. A clip whose file could not actually be unlinked
    (share permissions, immutable bit) keeps BOTH its index and queue rows and
    is reported as 'failed', never 'deleted' — otherwise the tile disappears,
    no space is freed, and the clip resurfaces on the next rescan.
    Returns {deleted, skipped, protected, failed}."""
    if not filenames:
        return {"deleted": 0, "skipped": 0, "protected": 0,
                "protected_names": [], "failed": 0}
    from . import retention as _retention
    protected: set = set()
    if not force:
        ph = ",".join("?" * len(filenames))
        with db.conn() as c:
            protected = {
                r["name"] for r in c.execute(
                    f"SELECT basename AS name FROM clip_index "
                    f"WHERE basename IN ({ph}) "
                    f"AND (COALESCE(locked,0)=1 OR COALESCE(event_type,'')='ro') "
                    f"UNION "
                    f"SELECT filename AS name FROM download_queue "
                    f"WHERE filename IN ({ph}) AND COALESCE(locked,0)=1",
                    [*filenames, *filenames],
                ).fetchall()
            }
    targets = [f for f in filenames if f not in protected]
    deleted = 0
    skipped = 0
    failed: set = set()
    if targets:
        tph = ",".join("?" * len(targets))
        with db.conn() as c:
            rows = c.execute(
                f"SELECT id, path, basename, event_type FROM clip_index "
                f"WHERE basename IN ({tph})", targets,
            ).fetchall()
        pruned = set()
        for r in rows:
            _, ok = _retention.delete_clip(db, dict(r), recordings, prune=False)
            if ok:
                deleted += 1
                pruned.add(os.path.dirname(r["path"]))
            else:
                failed.add(r["basename"])
        _retention.prune_empty_dirs(pruned)
        # Queue rows only for clips that actually went away (or were never
        # downloaded) — a failed unlink keeps its row so state stays truthful.
        mark = [f for f in targets if f not in failed]
        if mark:
            mph = ",".join("?" * len(mark))
            with db.write() as c:
                cur = c.execute(
                    f"UPDATE download_queue SET state='skipped', skip_reason='user', "
                    f"requested_at=NULL "
                    f"WHERE filename IN ({mph})", mark,
                )
                skipped = cur.rowcount  # rows actually marked, not len(mark)
    if deleted or skipped or protected or failed:
        log.info("archive delete: removed %d clip(s), %d protected, %d failed — %s",
                 deleted, len(protected), len(failed), _names(filenames))
    # protected_names lets the UI force-delete exactly the refused clips
    # after its second "delete anyway?" confirmation.
    return {"deleted": deleted, "skipped": skipped,
            "protected": len(protected), "protected_names": sorted(protected),
            "failed": len(failed)}


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
            f"attempts=0, last_error=NULL, skip_reason=NULL, "
            f"geofence_released_at=CASE WHEN skip_reason='geofence' "
            f"  THEN ? ELSE geofence_released_at END "
            f"WHERE filename IN ({ph}) AND state='skipped'",
            [int(time.time())] + filenames,
        )
        if cur.rowcount:
            log.info("archive unskip: %d clip(s) returned to pending — %s",
                     cur.rowcount, _names(filenames))
        return cur.rowcount


def geofence_skip(db: Database, filenames: List[str]) -> int:
    """Auto-skip the given clips as parked-at-home: ``pending`` →
    ``skipped``/``geofence``. Never touches non-pending rows or clips the user
    has permanently released (``geofence_released_at`` set). Returns the count."""
    if not filenames:
        return 0
    with db.write() as c:
        ph = ",".join("?" * len(filenames))
        cur = c.execute(
            f"UPDATE download_queue SET state='skipped', skip_reason='geofence', "
            f"requested_at=NULL "
            f"WHERE filename IN ({ph}) AND state='pending' "
            f"AND geofence_released_at IS NULL",
            filenames,
        )
        return cur.rowcount


def unskip_geofence(db: Database) -> int:
    """Reset every geofence auto-skipped clip back to ``pending`` so a fresh
    sweep can re-evaluate it. Unlike :func:`unskip`, this deliberately does NOT
    set ``geofence_released_at`` — we want the geofence to re-skip the genuine
    home clips. Leaves user skips, user-released clips, and downloaded/other
    rows untouched. Returns the count reset. Used by the maintenance flush."""
    with db.write() as c:
        cur = c.execute(
            "UPDATE download_queue SET state='pending', "
            "attempts=0, last_error=NULL, skip_reason=NULL "
            "WHERE state='skipped' AND skip_reason='geofence' "
            "AND geofence_released_at IS NULL"
        )
        return cur.rowcount


def geofence_candidates(db: Database, day: str) -> List[dict]:
    """Pending, time-stamped clips on ``day`` eligible for geofence auto-skip.

    Excludes RO/locked clips and event recordings (the sparse, important
    'something happened while parked' footage) and clips a user has
    permanently released. Returns ``[{filename, recorded_at}, ...]``."""
    day_expr = _day_expr()
    with db.conn() as c:
        rows = c.execute(
            f"SELECT filename, recorded_at FROM download_queue "
            f"WHERE {day_expr} = ? AND state='pending' "
            f"AND recorded_at IS NOT NULL "
            f"AND geofence_released_at IS NULL "
            # RO clips can be stored with or without a trailing slash;
            # _RO_SQL is the single source of truth for that test.
            f"AND NOT ({_RO_SQL}) "
            f"AND {_EVT_PREFIX_SQL} <> 'E'",
            (day,),
        ).fetchall()
    return [dict(r) for r in rows]


def geofence_day_signatures(db: Database, states: tuple) -> dict:
    """Per-day count of clips that carry a GPS triage skeleton, across the
    given queue ``states``. Monotonic as triage progresses (a skipped clip
    stays counted), so an unchanged count means a day has no new detection
    input — letting the geofence sweep skip re-parsing it.

    Returns ``{ 'YYYY-MM-DD': count, ... }``."""
    day_expr = _day_expr()
    ph = ",".join("?" * len(states))
    with db.conn() as c:
        rows = c.execute(
            f"SELECT {day_expr} AS day, COUNT(*) AS n FROM download_queue "
            f"WHERE state IN ({ph}) "
            f"  AND triaged_at IS NOT NULL AND gps_points > 0 "
            f"GROUP BY day",
            tuple(states),
        ).fetchall()
    return {r["day"]: r["n"] for r in rows}


def set_locked(db: Database, filenames: List[str], locked: bool = True) -> int:
    """Set the user 'retain indefinitely' flag on the given clips, in BOTH
    clip_index (by basename) and download_queue (by filename), so the state is
    stable whether the clip is downloaded or still queued. Returns the count of
    distinct filenames affected."""
    if not filenames:
        return 0
    val = 1 if locked else 0
    ph = ",".join("?" * len(filenames))
    with db.write() as c:
        c.execute(
            f"UPDATE clip_index SET locked=? WHERE basename IN ({ph})",
            [val, *filenames],
        )
        c.execute(
            f"UPDATE download_queue SET locked=? WHERE filename IN ({ph})",
            [val, *filenames],
        )
    verb = "read-only" if locked else "writable"
    log.info("archive mark %s: %d clip(s) — %s", verb, len(filenames),
             _names(filenames))
    return len(filenames)


def delete_from_camera(
    db: Database, filenames: List[str], base_url: str, *, timeout: float = 10.0,
) -> dict:
    """Delete the given clips from the dashcam SD card (cmd=4003). Skips clips
    the user has locked (retain indefinitely) and dashcam RO clips (source_dir
    under /RO/ — firmware write-protected). On a successful delete the queue row
    becomes 'gone' (no longer on the camera). Network failures are counted, not
    raised. Returns {deleted, skipped, errors}."""
    if not filenames:
        return {"deleted": 0, "skipped": 0, "errors": 0}
    ph = ",".join("?" * len(filenames))
    with db.conn() as c:
        rows = c.execute(
            f"SELECT filename, source_dir, locked FROM download_queue "
            f"WHERE filename IN ({ph})", filenames,
        ).fetchall()
    deleted = skipped = errors = 0
    gone: List[str] = []
    for r in rows:
        sd = r["source_dir"] or ""
        if r["locked"] or "/RO/" in sd or sd.endswith("/RO"):
            skipped += 1
            continue
        if vfs.delete_dashcam_file(base_url, sd, r["filename"], timeout=timeout):
            gone.append(r["filename"])
            deleted += 1
        else:
            errors += 1
    if gone:
        gph = ",".join("?" * len(gone))
        with db.write() as c:
            c.execute(
                f"UPDATE download_queue SET state='gone' WHERE filename IN ({gph})",
                gone,
            )
    log.info("delete from camera: %d deleted, %d skipped, %d error(s) — %s",
             deleted, skipped, errors, _names(filenames))
    return {"deleted": deleted, "skipped": skipped, "errors": errors}


def skip_listed_names(db: Database, names: Sequence[str]) -> set[str]:
    """Subset of ``names`` whose download_queue row is currently ``state='skipped'``
    (any ``skip_reason`` — geofence auto-skip or a user skip). Empty input → empty
    set with no query. ``filename`` is UNIQUE-indexed, so the lookup is cheap.

    Single source of truth for the manual-import skip gate: a clip the triage
    geofence (or the user) marked skipped is not re-imported via browser upload."""
    names = list(names)
    if not names:
        return set()
    ph = ",".join("?" * len(names))
    with db.conn() as c:
        rows = c.execute(
            f"SELECT filename FROM download_queue "
            f"WHERE state='skipped' AND filename IN ({ph})",
            names,
        ).fetchall()
    return {r["filename"] for r in rows}


def pending_days(db: Database) -> List[str]:
    """Distinct YYYY-MM-DD day keys that currently have ``pending`` clips."""
    day_expr = _day_expr()
    with db.conn() as c:
        rows = c.execute(
            f"SELECT DISTINCT {day_expr} AS day FROM download_queue "
            f"WHERE state='pending'"
        ).fetchall()
    return [r["day"] for r in rows]


def download_next(db: Database, filenames: List[str]) -> int:
    """Re-queue the given clips for immediate download: un-skip and retry any
    that were skipped/failed (both → ``pending``), set ``requested_at`` so
    they bypass the active connection's download scope (see
    :func:`next_pending`), then bump all of them to the top of the queue.
    Returns the number prioritized. ``done`` clips are untouched
    (``prioritize`` only affects ``pending``). Order matters: the state
    moves must run before ``prioritize`` so the rows are ``pending``."""
    if not filenames:
        return 0
    unskip(db, filenames)
    retry(db, filenames)
    with db.write() as c:
        ph = ",".join("?" * len(filenames))
        c.execute(
            f"UPDATE download_queue SET requested_at=? "
            f"WHERE filename IN ({ph}) AND state='pending'",
            [int(time.time())] + filenames,
        )
    return prioritize(db, filenames, "top")


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
