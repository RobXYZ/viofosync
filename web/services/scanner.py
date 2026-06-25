"""Archive scanner — walks $RECORDINGS and indexes clips.

Uses :func:`viofosync_lib.get_downloaded_recordings` so the
listing shares the CLI's filename regex. Each clip is upserted
into ``clip_index`` with derived metadata (camera, sequence,
event type, GPX presence).

The walk is intentionally bounded to the grouping-folder depth
the CLI produces — a full ``os.walk`` would descend into the
``.thumbs`` and ``.exports`` caches.

Event type is a heuristic from the filename + queue source_dir.
The XML listing's ATTR byte is more authoritative but is only
available at download time.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import time
from dataclasses import dataclass
from typing import Iterable, List

import viofosync_lib as vfs

from ..db import Database

log = logging.getLogger("viofosync.scanner")


@dataclass
class ClipMeta:
    path: str
    basename: str
    group_name: str
    timestamp: _dt.datetime
    camera: str
    sequence: int
    event_type: str
    size_bytes: int
    has_gpx: bool


def _event_type_for(camera_field: str, source_dir: str) -> str:
    """Categorise a clip into 'normal' / 'parking' / 'ro'.

    Filenames look like ``YYYY_MMDD_HHMMSS_NNNN[event][cam].MP4``
    where ``event`` is ``P`` (parking), ``E`` (impact), or absent
    (normal driving), and ``cam`` is ``F``/``R``. The regex captures
    both characters together as ``camera``, so a parking front clip
    is ``"PF"`` and a normal rear is ``"R"``.

    RO can't be inferred from the filename — RO clips live under
    the dashcam's ``/Movie/RO/`` directory, so the caller passes
    ``source_dir`` (snapshotted from download_queue at scan time).
    Event-mode clips collapse into ``normal``.
    """
    if "/RO/" in source_dir.upper():
        return "ro"
    if camera_field.upper().startswith("P"):
        return "parking"
    return "normal"


def _clip_meta_for(
    destination: str,
    grouping: str,
    filename: str,
    source_dir: str,
) -> "ClipMeta | None":
    """Derive a :class:`ClipMeta` for a single filename under ``destination``.

    Returns ``None`` if the filename doesn't match the Viofo pattern or the
    file doesn't exist on disk. ``source_dir`` is the dashcam origin path
    (used to identify RO clips); pass ``""`` when not known.
    """
    m = vfs.downloaded_filename_re.match(filename)
    if not m:
        return None

    ts = _dt.datetime(
        int(m.group("year")), int(m.group("month")),
        int(m.group("day")), int(m.group("hour")),
        int(m.group("minute")), int(m.group("second")),
    )
    group_name = vfs.get_group_name(ts, grouping) or ""
    path = vfs.get_filepath(destination, group_name, filename)
    if not os.path.isfile(path):
        return None

    camera_field = (m.group("camera") or "F").upper()
    camera = camera_field[-1] if camera_field[-1:] in "FRTI" else "F"
    return ClipMeta(
        path=path,
        basename=filename,
        group_name=ts.strftime("%Y-%m-%d"),  # always daily key in UI
        timestamp=ts,
        camera=camera,
        sequence=int(m.group("sequence")),
        event_type=_event_type_for(camera_field, source_dir),
        size_bytes=os.path.getsize(path),
        has_gpx=os.path.exists(path + ".gpx"),
    )


def _iter_clips(
    destination: str,
    grouping: str,
    source_dirs: dict[str, str],
) -> Iterable[ClipMeta]:
    """Yield every clip under ``destination``.

    ``source_dirs`` maps filename → original dashcam source
    directory; needed to identify RO clips since the local
    path doesn't preserve that.

    ``get_downloaded_recordings()`` returns ``(filename, date)``
    only, so we reconstruct each path from the grouping scheme.
    Replace with a bounded ``os.walk`` if files ever land outside
    that layout.
    """
    for filename, _rec_date in vfs.get_downloaded_recordings(
        destination, grouping
    ):
        meta = _clip_meta_for(
            destination, grouping, filename,
            source_dirs.get(filename, ""),
        )
        if meta is not None:
            yield meta


def _upsert_clip(c, clip: ClipMeta, now: int) -> None:
    """Upsert one :class:`ClipMeta` row into ``clip_index``.

    ``gps_examined`` is monotonic: a sidecar discovered on disk lifts the
    flag, but a sidecar that vanished (or was never written) doesn't reset
    it to 0 — so ``MAX`` is used on conflict.
    """
    c.execute(
        """
        INSERT INTO clip_index (
            path, basename, group_name, timestamp,
            camera, sequence, event_type, size_bytes,
            has_gpx, gps_examined, scanned_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET
            size_bytes=excluded.size_bytes,
            has_gpx=excluded.has_gpx,
            gps_examined=MAX(
                clip_index.gps_examined,
                excluded.gps_examined
            ),
            event_type=excluded.event_type,
            scanned_at=excluded.scanned_at
        """,
        (
            clip.path,
            clip.basename,
            clip.group_name,
            int(clip.timestamp.timestamp()),
            clip.camera,
            clip.sequence,
            clip.event_type,
            clip.size_bytes,
            1 if clip.has_gpx else 0,
            # Sidecar present → necessarily examined.
            1 if clip.has_gpx else 0,
            now,
        ),
    )


def index_one_clip(
    db: Database, destination: str, grouping: str, filename: str, *,
    source_dir: str = "",
) -> "int | None":
    """Index one freshly-downloaded clip so it appears immediately.
    Returns its clip_index.id, or None if the file can't be derived/found."""
    meta = _clip_meta_for(destination, grouping, filename, source_dir)
    if meta is None:
        return None
    now = int(time.time())
    with db.write() as c:
        _upsert_clip(c, meta, now)
        row = c.execute(
            "SELECT id FROM clip_index WHERE path=?", (meta.path,)
        ).fetchone()
    return row["id"] if row else None


def scan(db: Database, destination: str, grouping: str, hub=None, loop=None) -> int:
    """Full rescan. Returns the number of rows written.

    The directory walk runs *without* the DB write lock so that a
    multi-minute scan on a spinning NAS doesn't starve sync_worker
    and export_jobs. The collected metadata is then flushed in a
    single short write transaction.

    Idempotent: re-running only bumps ``scanned_at``.

    When ``hub`` is provided a ``clip_indexed`` event is broadcast
    after the write transaction commits. Pass ``loop`` when calling
    from a non-async thread (e.g. via ``asyncio.to_thread``).
    """
    now = int(time.time())

    # Snapshot filename → source_dir from the queue so RO clips
    # can be identified — the local path doesn't preserve the
    # /Movie/RO/ origin.
    with db.conn() as c:
        rows = c.execute(
            "SELECT filename, source_dir FROM download_queue"
        ).fetchall()
    source_dirs = {r["filename"]: (r["source_dir"] or "") for r in rows}

    clips = list(_iter_clips(destination, grouping, source_dirs))
    seen_paths: List[str] = [clip.path for clip in clips]
    log.info("scan: %d clip(s) found under %s", len(clips), destination)

    with db.write() as c:
        c.execute("BEGIN")
        try:
            for clip in clips:
                _upsert_clip(c, clip, now)

            # Drop index rows whose files vanished (retention policy or
            # manual move). But a scan that found *nothing* almost always
            # means the recordings volume is unavailable — not yet mounted
            # at container start, or a transient NAS glitch — rather than
            # the user having deleted their entire archive. Wiping the index
            # there resets duration_s/gps_examined for every clip and kicks
            # off a full duration re-sweep, GPS re-exam and thumb regen. So
            # never prune on an empty scan when the index still holds rows.
            if seen_paths:
                placeholders = ",".join("?" * len(seen_paths))
                c.execute(
                    f"DELETE FROM clip_index "
                    f"WHERE path NOT IN ({placeholders})",
                    seen_paths,
                )
            else:
                existing = c.execute(
                    "SELECT COUNT(*) FROM clip_index"
                ).fetchone()[0]
                if existing:
                    log.warning(
                        "scan found 0 clips but index holds %d — skipping "
                        "prune (recordings dir %s likely unavailable)",
                        existing, destination,
                    )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    if hub is not None:
        event = {"type": "clip_indexed", "total": len(seen_paths)}
        try:
            asyncio.get_running_loop()
            from . import tasks as _tasks
            _tasks.spawn(hub.broadcast(event), name="clip-indexed-broadcast")
        except RuntimeError:
            if loop is not None:
                hub.schedule_broadcast(loop, event)

    from . import derive_queue as _dq
    _dq.enqueue_missing(db, priority=1, now=int(time.time()))

    return len(seen_paths)
