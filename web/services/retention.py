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
from . import derive_queue as _dq
from . import filmstrip as _filmstrip
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
    if clip.get("locked"):
        return False, "locked"
    if protect_ro and (clip.get("event_type") or "") == "ro":
        return False, "ro_protected"
    if max_days <= 0:
        return False, "kept"
    if clip["timestamp"] < now - max_days * 86400:
        return True, "time"
    return False, "kept"


def _delete_clip_files(rec: dict, recordings: str) -> tuple[int, bool]:
    """Delete the .mp4, .gpx sidecar, and cached thumb/sprite/meta for
    one clip. Returns ``(bytes_freed, ok)``. ``ok`` is False when the
    primary recording itself could not be unlinked (typically share
    permissions) — sidecar/cache failures don't clear it. On failure
    ``bytes_freed`` reports 0 so quota bookkeeping never credits space
    that is still occupied. Callers must keep the index row when ``ok``
    is False, or the clip silently reappears on the next rescan while
    its file keeps holding disk.

    Day-folder pruning is NOT done here — callers collect
    ``os.path.dirname(rec['path'])`` and call :func:`prune_empty_dirs`
    once per batch, so a NAS doesn't pay a failing rmdir per clip."""
    freed = 0
    path = rec["path"]
    try:
        freed = os.path.getsize(path)
    except OSError:
        freed = 0
    ok = True
    for p in (
        path,
        path + ".gpx",
        _thumbs.thumb_path(recordings, rec["id"]),
        _filmstrip.sprite_path(recordings, rec["id"]),
        _filmstrip.meta_path(recordings, rec["id"]),
    ):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("retention: could not remove %s: %s", p, e)
            if p == path:
                ok = False
    return (freed if ok else 0, ok)


def prune_empty_dirs(dirs) -> None:
    """Best-effort rmdir of day/group folders that may now be empty.
    Almost always fails with ENOTEMPTY, which is fine — batching means
    that syscall is paid once per folder, not once per deleted clip."""
    for d in set(dirs):
        try:
            os.rmdir(d)
        except OSError:
            pass


def _delete_index_row(db: Database, clip_id: int) -> None:
    _dq.remove_clip(db, clip_id)
    with db.write() as c:
        c.execute("DELETE FROM clip_index WHERE id = ?", (clip_id,))


def delete_clip(
    db: Database, rec: dict, recordings: str, *, prune: bool = True,
) -> tuple[int, bool]:
    """Delete one downloaded clip's files (mp4 + .gpx + thumb/sprite/meta) and its
    clip_index row. Returns ``(bytes_freed, ok)``. When the primary file could
    not be unlinked the index row is KEPT and ``ok`` is False — dropping the row
    while the .mp4 survives would report success, free nothing, and resurrect
    the clip on the next rescan. Public wrapper over the same plumbing the
    retention sweep uses, for the user-initiated archive delete; batch callers
    pass ``prune=False`` and call :func:`prune_empty_dirs` once themselves."""
    freed, ok = _delete_clip_files(rec, recordings)
    if ok:
        _delete_index_row(db, rec["id"])
        if prune:
            prune_empty_dirs((os.path.dirname(rec["path"]),))
    return freed, ok


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
    exclude: frozenset[str] = frozenset(),
    protect_ids: frozenset[int] = frozenset(),
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
    failed = 0
    bytes_freed = 0

    # Phase 1: time-based. ``protect_ids`` (clips referenced by
    # pending/active export jobs) survive both phases — deleting a
    # source mid-render fails the export.
    if max_days > 0:
        guard_sql, guard_params = _protect_clause(protect_ids)
        with db.conn() as c:
            rows = [
                dict(r) for r in c.execute(
                    "SELECT id, path, basename, timestamp, event_type, locked "
                    f"FROM clip_index WHERE timestamp < ?{guard_sql}",
                    [now - max_days * 86400, *guard_params],
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
        pruned: set[str] = set()
        for row in rows:
            ok, reason = _eligible_by_time(
                row, now=now, max_days=max_days, protect_ro=protect_ro,
            )
            if not ok:
                if reason == "ro_protected":
                    protected += 1
                continue
            freed, removed = _delete_clip_files(row, recordings)
            if not removed:
                failed += 1
                continue
            bytes_freed += freed
            _delete_index_row(db, row["id"])
            pruned.add(os.path.dirname(row["path"]))
            deleted_time += 1
            _broadcast(sink, row["basename"], "time")
            if deleted_time % 10 == 0:
                log.info(
                    "retention sweep: %d/%d clip(s) deleted "
                    "(%.1f MB freed so far)",
                    deleted_time, len(rows), bytes_freed / (1 << 20),
                )
        prune_empty_dirs(pruned)

    # Phase 2: disk-pressure. Two independent triggers, either can
    # be set on its own; both is fine and uses OR semantics.
    deleted_disk = 0
    if disk_pct > 0 or quota_gb > 0:
        deleted_disk, freed_2, protected_2, failed_2 = _disk_pressure_pass(
            db, recordings,
            disk_pct=disk_pct,
            quota_gb=quota_gb,
            protect_ro=protect_ro,
            sink=sink,
            exclude=exclude,
            protect_ids=protect_ids,
        )
        bytes_freed += freed_2
        protected += protected_2
        failed += failed_2

    summary = {
        "deleted_time": deleted_time,
        "deleted_disk": deleted_disk,
        "protected": protected,
        "failed": failed,
        "bytes_freed": bytes_freed,
    }
    if deleted_time or deleted_disk or protected or failed:
        log.info(
            "retention sweep: %d by time, %d by disk, %d protected, "
            "%d failed, %.1f MB freed",
            deleted_time, deleted_disk, protected, failed,
            bytes_freed / (1 << 20),
        )
    return summary


_BATCH_SIZE = 16
_SIZE_CACHE_TTL = 60.0
# path -> (computed_at_monotonic, used_bytes). Bookkeeping cache for
# quota mode: deletes subtract from the cached total so the inner
# bail-out check in _disk_pressure_pass doesn't trigger a fresh tree
# walk after every file.
_size_cache: dict[tuple[str, frozenset[str]], tuple[float, int]] = {}


def _scan_dir_bytes(path: str, exclude: frozenset[str] = frozenset()) -> int:
    """Sum of file sizes in ``path``, recursing without crossing mount
    points. Directories whose absolute path is in ``exclude`` are
    skipped wholesale — used to keep import staging dirs off the quota
    books. Used by quota mode in place of ``shutil.disk_usage`` when
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
                        if os.path.abspath(entry.path) in exclude:
                            continue
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += st.st_size
        except OSError:
            continue
    return total


def _cached_used_bytes(
    path: str, *, refresh: bool = False, exclude: frozenset[str] = frozenset()
) -> int:
    now = _time_mod.monotonic()
    key = (path, exclude)
    cached = _size_cache.get(key)
    if not refresh and cached and (now - cached[0]) < _SIZE_CACHE_TTL:
        return cached[1]
    used = _scan_dir_bytes(path, exclude=exclude)
    _size_cache[key] = (now, used)
    return used


def _cache_subtract(
    path: str, freed: int, *, exclude: frozenset[str] = frozenset()
) -> None:
    key = (path, exclude)
    cached = _size_cache.get(key)
    if cached is not None and freed > 0:
        _size_cache[key] = (cached[0], max(0, cached[1] - freed))


def disk_used_pct(
    recordings: str, quota_gb: int = 0,
    exclude: frozenset[str] = frozenset(),
) -> Optional[float]:
    """Public helper for consumers (MQTT, status APIs) that want the
    same used-% the retention sweep evaluates against.

    Returns ``None`` when the recordings path is missing or the
    quota is meaningless — callers should treat that as Unknown
    rather than 0%. Reuses the 60-second tree-walk cache so two
    consumers a minute apart don't double-walk.

    For display only — the sweep itself uses ``_pct_exceeded`` and
    ``_quota_exceeded`` to decide whether each rule is currently
    breached, so a quota set without a percentage threshold (or
    vice versa) triggers independently.

    NOTE: For deciding whether the disk is critically full (the
    ``compute_sync_status`` error trigger), prefer
    :func:`filesystem_used_pct` — quota mode here reads ~100% when
    retention is doing its job correctly, which would otherwise trip
    a perpetual error.
    """
    if quota_gb > 0:
        used = _cached_used_bytes(recordings, exclude=exclude)
        limit = quota_gb * (1 << 30)
        if limit <= 0:
            return None
        return used / limit * 100.0
    return filesystem_used_pct(recordings)


def filesystem_used_pct(recordings: str) -> Optional[float]:
    """Filesystem-level disk usage % for the volume holding *recordings*.
    Ignores any configured quota — this is the "OS will start denying
    writes soon" signal, separate from the self-imposed quota that
    retention manages.

    Returns ``None`` when the path is missing.
    """
    try:
        du = shutil.disk_usage(recordings)
    except (OSError, FileNotFoundError):
        return None
    if du.total <= 0:
        return None
    return du.used / du.total * 100.0


def _pct_exceeded(recordings: str, disk_pct: int) -> bool:
    """Filesystem-percent rule. ``disk_pct == 0`` disables it."""
    if disk_pct <= 0:
        return False
    du = shutil.disk_usage(recordings)
    if du.total <= 0:
        return False
    return (du.used / du.total * 100.0) >= disk_pct


def _quota_exceeded(
    recordings: str, quota_gb: int, *, refresh: bool = False,
    exclude: frozenset[str] = frozenset(),
) -> bool:
    """Absolute-quota rule. ``quota_gb == 0`` disables it. Reads from
    the cached size-walk (decremented in-place by each delete) so the
    inner sweep loop doesn't pay for a tree walk per file."""
    if quota_gb <= 0:
        return False
    return _cached_used_bytes(
        recordings, refresh=refresh, exclude=exclude
    ) >= quota_gb * (1 << 30)


def _over_threshold(
    recordings: str, *, disk_pct: int, quota_gb: int, refresh: bool = False,
    exclude: frozenset[str] = frozenset(),
) -> bool:
    """True if EITHER rule is currently breached. Independent triggers
    — set the percentage to bound the underlying filesystem, set the
    quota to bound bytes-under-recordings, or set both."""
    return (
        _pct_exceeded(recordings, disk_pct)
        or _quota_exceeded(recordings, quota_gb, refresh=refresh, exclude=exclude)
    )


def _protect_clause(protect_ids: frozenset[int]) -> tuple[str, list]:
    """SQL fragment + params excluding export-protected clip ids from
    a deletion-candidate query. Empty set → no-op fragment."""
    if not protect_ids:
        return "", []
    ph = ",".join("?" * len(protect_ids))
    return f" AND id NOT IN ({ph})", list(protect_ids)


def _disk_pressure_pass(
    db: Database,
    recordings: str,
    *,
    disk_pct: int,
    quota_gb: int,
    protect_ro: bool,
    sink,
    exclude: frozenset[str] = frozenset(),
    protect_ids: frozenset[int] = frozenset(),
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
    bytes_freed, protected, failed)``.
    """
    deleted = 0
    bytes_freed = 0
    failed_ids: set[int] = set()
    pruned: set[str] = set()
    while _over_threshold(
        recordings, disk_pct=disk_pct, quota_gb=quota_gb, refresh=True,
        exclude=exclude,
    ):
        conds = ["COALESCE(locked, 0) = 0"]
        params: list = []
        if protect_ro:
            conds.append("COALESCE(event_type, '') != 'ro'")
        if failed_ids:
            # Clips whose unlink already failed this pass: keep them out
            # of the next batch or the loop would re-select them forever
            # while never freeing a byte.
            fph = ",".join("?" * len(failed_ids))
            conds.append(f"id NOT IN ({fph})")
            params.extend(failed_ids)
        guard_sql, guard_params = _protect_clause(protect_ids)
        if guard_sql:
            conds.append(guard_sql.removeprefix(" AND "))
            params.extend(guard_params)
        with db.conn() as c:
            rows = [
                dict(r) for r in c.execute(
                    f"SELECT id, path, basename, event_type "
                    f"FROM clip_index WHERE {' AND '.join(conds)} "
                    f"ORDER BY timestamp ASC LIMIT ?",
                    (*params, _BATCH_SIZE),
                ).fetchall()
            ]
        if not rows:
            break
        for row in rows:
            freed, removed = _delete_clip_files(row, recordings)
            if not removed:
                failed_ids.add(row["id"])
                continue
            _cache_subtract(recordings, freed, exclude=exclude)
            bytes_freed += freed
            _delete_index_row(db, row["id"])
            pruned.add(os.path.dirname(row["path"]))
            deleted += 1
            _broadcast(sink, row["basename"], "disk")
            if not _over_threshold(
                recordings, disk_pct=disk_pct, quota_gb=quota_gb,
                exclude=exclude,
            ):
                prune_empty_dirs(pruned)
                return deleted, bytes_freed, 0, len(failed_ids)

    prune_empty_dirs(pruned)
    protected = 0
    if protect_ro and _over_threshold(
        recordings, disk_pct=disk_pct, quota_gb=quota_gb, refresh=True,
        exclude=exclude,
    ):
        with db.conn() as c:
            protected = c.execute(
                "SELECT COUNT(*) AS n FROM clip_index "
                "WHERE COALESCE(event_type, '') = 'ro'"
            ).fetchone()["n"]
    return deleted, bytes_freed, protected, len(failed_ids)


def make_room_for(
    db: Database, recordings: str, *,
    size: int, before_ts: int,
    disk_pct: int, quota_gb: int, protect_ro: bool,
    exclude: frozenset[str] = frozenset(),
    protect_ids: frozenset[int] = frozenset(),
) -> bool:
    """Ensure ``size`` more bytes will fit under the active rules,
    by deleting the OLDEST clip whose timestamp < ``before_ts``
    (skipping ``ro`` when ``protect_ro``). Returns False when no
    evictable clip older than ``before_ts`` remains while still
    over threshold — the caller then skips that clip. Never deletes
    a clip newer than or equal to the one being imported.

    With neither rule set, returns True (rely on the filesystem).

    The quota total is walked ONCE up front and decremented by the
    bytes each delete frees, so a multi-eviction call does not
    re-walk the tree per file (matching the size-cache philosophy
    used elsewhere in this module).
    """
    if disk_pct <= 0 and quota_gb <= 0:
        return True

    quota_bytes = quota_gb * (1 << 30) if quota_gb > 0 else None
    used = _scan_dir_bytes(recordings, exclude=exclude) if quota_bytes else 0

    def _over() -> bool:
        if quota_bytes is not None and used + size >= quota_bytes:
            return True
        if disk_pct > 0:
            try:
                du = shutil.disk_usage(recordings)
            except OSError:
                return False
            if du.total > 0 and ((du.used + size) / du.total * 100.0) >= disk_pct:
                return True
        return False

    where = "WHERE timestamp < ? AND COALESCE(locked, 0) = 0"
    base_params: list = [before_ts]
    if protect_ro:
        where += " AND COALESCE(event_type, '') != 'ro'"
    guard_sql, guard_params = _protect_clause(protect_ids)
    where += guard_sql
    base_params.extend(guard_params)

    failed_ids: set[int] = set()
    pruned: set[str] = set()
    try:
        while _over():
            cond = where
            params = list(base_params)
            if failed_ids:
                # Un-unlinkable clips must leave the candidate pool, or
                # the oldest-first LIMIT 1 re-selects them forever.
                fph = ",".join("?" * len(failed_ids))
                cond += f" AND id NOT IN ({fph})"
                params.extend(failed_ids)
            with db.conn() as c:
                row = c.execute(
                    f"SELECT id, path, basename FROM clip_index {cond} "
                    f"ORDER BY timestamp ASC LIMIT 1",
                    params,
                ).fetchone()
            if row is None:
                return False
            freed, removed = _delete_clip_files(dict(row), recordings)
            if not removed:
                failed_ids.add(row["id"])
                continue
            _delete_index_row(db, row["id"])
            pruned.add(os.path.dirname(row["path"]))
            used = max(0, used - freed)
        return True
    finally:
        prune_empty_dirs(pruned)


def import_exclude_set(recordings: str, import_path: str = "") -> frozenset[str]:
    """Absolute dirs to keep off the quota walk during import: the
    ``.import_tmp`` staging dir, and the resolved import drop folder
    when it lives inside the recordings tree (a same-volume external
    mount is a different st_dev and is never counted anyway)."""
    rec_abs = os.path.abspath(recordings)
    out = {os.path.join(rec_abs, ".import_tmp")}
    resolved = os.path.abspath(import_path or os.path.join(recordings, "import"))
    try:
        if resolved != rec_abs and os.path.commonpath([resolved, rec_abs]) == rec_abs:
            out.add(resolved)
    except ValueError:  # different drives (Windows) — not under recordings
        pass
    return frozenset(out)
