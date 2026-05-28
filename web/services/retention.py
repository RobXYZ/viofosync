"""Local archive retention.

Two independent rules, both optional and zero-disabled:

* time cap (``max_days``) — delete clips older than N days
* disk-pressure cap (``disk_pct``) — when used disk % is at/above
  the threshold, delete oldest first until back below it

A separate flag (``protect_ro``) shields read-only / locked clips
from both rules. The actual ``sweep()`` function tying these to
the database lives below; the eligibility decision is factored out
as a pure function so it can be unit-tested without touching disk.
"""
from __future__ import annotations

import logging
import os
import shutil
import time as _time_mod
from typing import Optional

from ..db import Database
from . import thumbs as _thumbs

log = logging.getLogger("viofosync.retention")


def _eligible_by_time(
    clip: dict,
    *,
    now: int,
    max_days: int,
    protect_ro: bool,
) -> tuple[bool, str]:
    """Pure decision: should this clip be deleted by the time rule?

    Returns ``(delete, reason)`` where ``reason`` is one of
    ``'time'`` (yes, delete), ``'kept'`` (no, within retention
    window or rule disabled), or ``'ro_protected'`` (no, RO clip
    and protection is on).
    """
    if protect_ro and (clip.get("event_type") or "") == "ro":
        return False, "ro_protected"
    if max_days <= 0:
        return False, "kept"
    if clip["timestamp"] < now - max_days * 86400:
        return True, "time"
    return False, "kept"


def _delete_clip_files(rec: dict, recordings: str) -> int:
    """Delete the .mp4, .gpx sidecar, and cached thumb for one
    clip. Returns the number of bytes freed (best-effort; 0 on
    failure)."""
    freed = 0
    path = rec["path"]
    try:
        freed = os.path.getsize(path)
    except OSError:
        freed = 0
    for p in (path, path + ".gpx", _thumbs.thumb_path(recordings, rec["id"])):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except OSError as e:  # pragma: no cover — best-effort
            log.warning("retention: could not remove %s: %s", p, e)
    # Best-effort prune of an empty group folder.
    parent = os.path.dirname(path)
    try:
        os.rmdir(parent)
    except OSError:
        pass
    return freed


def _delete_index_row(db: Database, clip_id: int) -> None:
    with db.write() as c:
        c.execute("DELETE FROM clip_index WHERE id = ?", (clip_id,))


def _broadcast(sink, filename: str, reason: str) -> None:
    if sink is None:
        return
    try:
        sink.retention_deleted(filename, reason=reason)
    except Exception:  # pragma: no cover — never let UI plumbing break a sweep
        log.exception("retention: sink.retention_deleted raised")


def sweep(
    db: Database,
    recordings: str,
    *,
    max_days: int,
    disk_pct: int,
    protect_ro: bool,
    quota_gb: int = 0,
    sink=None,
    _now: Optional[int] = None,
) -> dict:
    """Run the retention pass. Returns a summary dict.

    ``_now`` is for tests only — production callers should leave
    it None so the function reads the current time.
    """
    import time as _time
    now = _now if _now is not None else int(_time.time())
    deleted_time = 0
    protected = 0
    bytes_freed = 0

    # Phase 1: time-based.
    if max_days > 0:
        with db.conn() as c:
            rows = [
                dict(r) for r in c.execute(
                    "SELECT id, path, basename, timestamp, event_type "
                    "FROM clip_index WHERE timestamp < ?",
                    (now - max_days * 86400,),
                ).fetchall()
            ]
        if rows:
            # "examining" rather than "deleting": with protect_ro on,
            # some rows here are RO-locked and will be skipped. The
            # end-of-sweep summary reports the real deleted count.
            log.info(
                "retention sweep: %d clip(s) older than %d days "
                "— examining",
                len(rows), max_days,
            )
        for row in rows:
            ok, reason = _eligible_by_time(
                row, now=now, max_days=max_days, protect_ro=protect_ro,
            )
            if not ok:
                if reason == "ro_protected":
                    protected += 1
                continue
            bytes_freed += _delete_clip_files(row, recordings)
            _delete_index_row(db, row["id"])
            deleted_time += 1
            _broadcast(sink, row["basename"], "time")
            if deleted_time % 10 == 0:
                log.info(
                    "retention sweep: %d/%d clip(s) deleted "
                    "(%.1f MB freed so far)",
                    deleted_time, len(rows), bytes_freed / (1 << 20),
                )

    # Phase 2: disk-pressure. Two independent triggers, either can
    # be set on its own; both is fine and uses OR semantics.
    deleted_disk = 0
    if disk_pct > 0 or quota_gb > 0:
        deleted_disk, freed_2, protected_2 = _disk_pressure_pass(
            db, recordings,
            disk_pct=disk_pct,
            quota_gb=quota_gb,
            protect_ro=protect_ro,
            sink=sink,
        )
        bytes_freed += freed_2
        protected += protected_2

    summary = {
        "deleted_time": deleted_time,
        "deleted_disk": deleted_disk,
        "protected": protected,
        "bytes_freed": bytes_freed,
    }
    if deleted_time or deleted_disk or protected:
        log.info(
            "retention sweep: %d by time, %d by disk, %d protected, "
            "%.1f MB freed",
            deleted_time, deleted_disk, protected,
            bytes_freed / (1 << 20),
        )
    return summary


_BATCH_SIZE = 16
_SIZE_CACHE_TTL = 60.0
# path -> (computed_at_monotonic, used_bytes). Bookkeeping cache for
# quota mode: deletes subtract from the cached total so the inner
# bail-out check in _disk_pressure_pass doesn't trigger a fresh tree
# walk after every file.
_size_cache: dict[str, tuple[float, int]] = {}


def _scan_dir_bytes(path: str) -> int:
    """Sum of file sizes in ``path``, recursing without crossing mount
    points. Used by quota mode in place of ``shutil.disk_usage`` when
    the OS-level free-space figure doesn't reflect the actual quota
    (e.g. Synology shared folder, ZFS dataset, NFS share)."""
    try:
        root_dev = os.stat(path).st_dev
    except OSError:
        return 0
    total = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_dev != root_dev:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += st.st_size
        except OSError:
            continue
    return total


def _cached_used_bytes(path: str, *, refresh: bool = False) -> int:
    now = _time_mod.monotonic()
    cached = _size_cache.get(path)
    if not refresh and cached and (now - cached[0]) < _SIZE_CACHE_TTL:
        return cached[1]
    used = _scan_dir_bytes(path)
    _size_cache[path] = (now, used)
    return used


def _cache_subtract(path: str, freed: int) -> None:
    cached = _size_cache.get(path)
    if cached is not None and freed > 0:
        _size_cache[path] = (cached[0], max(0, cached[1] - freed))


def _pct_exceeded(recordings: str, disk_pct: int) -> bool:
    """Filesystem-percent rule. ``disk_pct == 0`` disables it."""
    if disk_pct <= 0:
        return False
    du = shutil.disk_usage(recordings)
    if du.total <= 0:
        return False
    return (du.used / du.total * 100.0) >= disk_pct


def _quota_exceeded(recordings: str, quota_gb: int, *, refresh: bool = False) -> bool:
    """Absolute-quota rule. ``quota_gb == 0`` disables it. Reads from
    the cached size-walk (decremented in-place by each delete) so the
    inner sweep loop doesn't pay for a tree walk per file."""
    if quota_gb <= 0:
        return False
    return _cached_used_bytes(recordings, refresh=refresh) >= quota_gb * (1 << 30)


def _over_threshold(
    recordings: str, *, disk_pct: int, quota_gb: int, refresh: bool = False
) -> bool:
    """True if EITHER rule is currently breached. Independent triggers
    — set the percentage to bound the underlying filesystem, set the
    quota to bound bytes-under-recordings, or set both."""
    return (
        _pct_exceeded(recordings, disk_pct)
        or _quota_exceeded(recordings, quota_gb, refresh=refresh)
    )


def _disk_pressure_pass(
    db: Database,
    recordings: str,
    *,
    disk_pct: int,
    quota_gb: int,
    protect_ro: bool,
    sink,
) -> tuple[int, int, int]:
    """Delete oldest clips first until both pressure rules are
    satisfied or no more eligible candidates remain.

    The two rules are independent: ``disk_pct`` measures the
    underlying filesystem (cheap syscall via ``shutil.disk_usage``);
    ``quota_gb`` measures bytes under ``recordings`` against a
    declared cap (needed for Synology shares / ZFS datasets / NFS
    where the OS-level free figure doesn't reflect the real
    constraint). Either rule on its own works; if both are set we
    keep deleting while either is breached.

    Usage is re-checked at the top of each batch (forced fresh in
    quota mode so the loop sees ground truth) and again after every
    individual delete inside the batch. The inner check lets us bail
    the moment all rules are satisfied, avoiding overshoot when a
    single delete is already enough. The quota inner check reads the
    bookkeeping cache (decremented by each delete) so we don't walk
    the tree per file.

    If we exit still over-threshold AND ``protect_ro`` is on, counts
    the surviving RO clips and reports them as ``protected`` so an
    operator can see why usage didn't drop. Returns ``(deleted,
    bytes_freed, protected)``.
    """
    deleted = 0
    bytes_freed = 0
    while _over_threshold(
        recordings, disk_pct=disk_pct, quota_gb=quota_gb, refresh=True,
    ):
        where = ""
        if protect_ro:
            where = "WHERE COALESCE(event_type, '') != 'ro'"
        with db.conn() as c:
            rows = [
                dict(r) for r in c.execute(
                    f"SELECT id, path, basename, event_type "
                    f"FROM clip_index {where} "
                    f"ORDER BY timestamp ASC LIMIT ?",
                    (_BATCH_SIZE,),
                ).fetchall()
            ]
        if not rows:
            break
        for row in rows:
            freed = _delete_clip_files(row, recordings)
            _cache_subtract(recordings, freed)
            bytes_freed += freed
            _delete_index_row(db, row["id"])
            deleted += 1
            _broadcast(sink, row["basename"], "disk")
            if not _over_threshold(
                recordings, disk_pct=disk_pct, quota_gb=quota_gb,
            ):
                return deleted, bytes_freed, 0

    protected = 0
    if protect_ro and _over_threshold(
        recordings, disk_pct=disk_pct, quota_gb=quota_gb, refresh=True,
    ):
        with db.conn() as c:
            protected = c.execute(
                "SELECT COUNT(*) AS n FROM clip_index "
                "WHERE COALESCE(event_type, '') = 'ro'"
            ).fetchone()["n"]
    return deleted, bytes_freed, protected
