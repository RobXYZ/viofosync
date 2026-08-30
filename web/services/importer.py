"""Local import / ingest core.

Feeds locally-available Viofo clips (browser upload, drop folder, or
an external/USB mount) into the archive, reusing the same filename
patterns, path layout, GPX, indexing, and retention as Wi-Fi sync.

Pure-ish: ``scan_source`` and ``ingest_clip`` do filesystem + DB work
but no asyncio; ``run_folder_ingest`` is the batch driver invoked on a
worker thread, broadcasting progress via the hub.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field

import viofosync_lib as vfs
from viofosync_lib.cameras import GPS_CAMERA_LETTER

from ..db import Database
from . import retention as _retention
from . import scanner

log = logging.getLogger("viofosync.importer")

STAGING_DIRNAME = ".import_tmp"


@dataclass
class ClipResult:
    filename: str
    status: str          # imported|already_present|not_recognised
                         #          |over_quota_older|error
    detail: str = ""
    size_bytes: int = 0
    event_type: str = "normal"


@dataclass
class ScanItem:
    src_path: str
    source_rel_path: str
    basename: str
    timestamp: int
    camera: str
    sequence: int
    event_type: str
    size_bytes: int


@dataclass
class Manifest:
    items: list[ScanItem] = field(default_factory=list)      # newest-first
    skipped: list[dict] = field(default_factory=list)        # [{"name","reason"}]
    total_bytes: int = 0


def _is_ro(source_rel_path: str) -> bool:
    norm = "/" + source_rel_path.replace("\\", "/").strip("/").upper()
    return "/RO/" in norm


def classify_event_type(camera_field: str, source_rel_path: str) -> str:
    if _is_ro(source_rel_path):
        return "ro"
    if camera_field.upper().startswith("P"):
        return "parking"
    return "normal"


def scan_item_from_match(
    m: re.Match[str], name: str, *, source_rel_path: str, size: int, src_path: str,
) -> ScanItem:
    ts = int(_dt.datetime(
        int(m.group("year")), int(m.group("month")), int(m.group("day")),
        int(m.group("hour")), int(m.group("minute")), int(m.group("second")),
    ).timestamp())
    # Compact suffix-less names have an empty camera group; the sole lens
    # is the GPS-bearing one (mirrors scanner._clip_meta_for).
    cam = m.group("camera") or GPS_CAMERA_LETTER
    return ScanItem(
        src_path=src_path, source_rel_path=source_rel_path, basename=name,
        timestamp=ts, camera=cam.upper(),
        sequence=int(m.group("sequence") or 0),
        event_type=classify_event_type(cam, source_rel_path), size_bytes=size,
    )


def scan_source(root: str) -> Manifest:
    """Recurse ``root``, returning recognised Viofo clips (newest-first)
    and a list of skipped files with reasons."""
    man = Manifest()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            m = vfs.downloaded_filename_re.match(name)
            if not m:
                man.skipped.append({"name": name, "reason": "not_recognised"})
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            rel = os.path.relpath(full, root)
            try:
                item = scan_item_from_match(
                    m, name, source_rel_path=rel, size=size, src_path=full,
                )
            except ValueError:
                man.skipped.append({"name": name, "reason": "bad_timestamp"})
                continue
            man.items.append(item)
            man.total_bytes += size
    man.items.sort(key=lambda it: it.timestamp, reverse=True)
    return man


def scan_item_dict(it: ScanItem) -> dict:
    return asdict(it)


def clip_result_dict(res: ClipResult) -> dict:
    return asdict(res)


def dest_for(snap, item: ScanItem) -> str:
    group = vfs.get_group_name(
        _dt.datetime.fromtimestamp(item.timestamp), snap.grouping,
    )
    return vfs.get_filepath(snap.recordings, group or "", item.basename)


def has_complete_copy(dest: str, expected_size: int) -> bool:
    """True if ``dest`` already holds a non-partial copy: it exists and is
    not smaller than ``expected_size``. A smaller file is treated as a
    truncated/partial import and redone; an unknown size (<= 0) trusts mere
    existence. Larger-than-expected files are kept, never clobbered."""
    if not os.path.exists(dest):
        return False
    if expected_size and expected_size > 0:
        try:
            return os.path.getsize(dest) >= expected_size
        except OSError:
            return False
    return True


def present_in_archive(snap, sizes) -> set[str]:
    """Return the subset of names that already have a COMPLETE copy in the
    archive. ``sizes`` maps basename -> expected size in bytes (0/unknown
    trusts existence). Unrecognised / unparseable names are ignored. Lets
    the import flow skip clips already there instead of re-uploading or
    re-scanning them, while still redoing truncated partials."""
    out: set[str] = set()
    for name, size in sizes.items():
        m = vfs.downloaded_filename_re.match(name)
        if not m:
            continue
        try:
            item = scan_item_from_match(
                m, name, source_rel_path=name, size=size or 0, src_path="",
            )
        except ValueError:
            continue
        if has_complete_copy(dest_for(snap, item), size or 0):
            out.add(name)
    return out


def _origin_source_dir(item: ScanItem) -> str:
    # Invariant: contains "/RO/" iff the clip is locked, so scanner.scan
    # re-derives event_type='ro' on every future rescan.
    return "/import/RO/" if item.event_type == "ro" else "/import/"


def _record_origin(db: Database, item: ScanItem) -> None:
    now = int(time.time())
    with db.write() as c:
        # On conflict the clip was already queued (e.g. the dashcam listed
        # it before this bulk import). Flip that row to done so the next
        # Wi-Fi cycle doesn't re-attempt a download that 404s.
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, remote_size, recorded_at, camera, "
            " event_type, state, priority, enqueued_at, finished_at, manual) "
            "VALUES (?, ?, ?, ?, ?, ?, 'done', 0, ?, ?, 1) "
            "ON CONFLICT(filename) DO UPDATE SET "
            "  state='done', finished_at=excluded.finished_at, manual=1",
            (item.basename, _origin_source_dir(item), item.size_bytes,
             item.timestamp, item.camera, item.event_type, now, now),
        )


def is_cross_volume(root: str, recordings: str) -> bool:
    try:
        return os.stat(root).st_dev != os.stat(recordings).st_dev
    except OSError:
        return True  # safe default: copy, never a destructive move


def ingest_clip(
    db: Database, snap, item: ScanItem, *,
    cross_volume: bool, staged: bool = False,
) -> ClipResult:
    """Place one clip into the archive. ``staged`` means ``item.src_path``
    is already in ``.import_tmp`` and make-room was done by the caller
    (the upload path); we then go straight to the final rename."""
    recordings = snap.recordings
    dest = dest_for(snap, item)
    if has_complete_copy(dest, item.size_bytes):
        return ClipResult(item.basename, "already_present",
                          size_bytes=item.size_bytes, event_type=item.event_type)

    if not staged:
        from .exporter import export_protect_ids
        ok = _retention.make_room_for(
            db, recordings, size=item.size_bytes, before_ts=item.timestamp,
            disk_pct=snap.retention_disk_pct, quota_gb=snap.recordings_quota_gb,
            protect_ro=snap.retention_protect_ro,
            exclude=_retention.import_exclude_set(recordings, snap.import_path),
            protect_ids=export_protect_ids(db),
        )
        if not ok:
            return ClipResult(item.basename, "over_quota_older",
                              size_bytes=item.size_bytes, event_type=item.event_type)

    staging = os.path.join(recordings, STAGING_DIRNAME)
    tmp = item.src_path if staged else os.path.join(staging, item.basename)
    if not staged:
        os.makedirs(staging, exist_ok=True)
        try:
            if cross_volume:
                shutil.copy2(item.src_path, tmp)
                if os.path.getsize(tmp) != item.size_bytes:
                    os.remove(tmp)
                    log.warning(
                        "import size mismatch for %s after copy", item.basename
                    )
                    return ClipResult(item.basename, "error",
                                      detail="size mismatch after copy",
                                      size_bytes=item.size_bytes,
                                      event_type=item.event_type)
            else:
                os.replace(item.src_path, tmp)  # same-volume move into staging
        except OSError as e:
            log.warning("import staging failed for %s: %s", item.basename, e)
            return ClipResult(item.basename, "error", detail=str(e),
                              size_bytes=item.size_bytes,
                              event_type=item.event_type)

    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.replace(tmp, dest)
    except OSError as e:
        # Don't strand the staged file. A same-volume move has already
        # consumed the source, so restore it (no data loss); cross-volume
        # keeps an intact original and upload (staged) will be retried, so
        # the temp is safe to drop there.
        if not staged and not cross_volume:
            try:
                os.replace(tmp, item.src_path)
            except OSError as restore_err:
                log.error(
                    "import: failed to restore source for %s after a failed "
                    "placement; staged file %s may be lost: %s",
                    item.basename, tmp, restore_err,
                )
        else:
            try:
                os.remove(tmp)
            except OSError:
                pass
        log.warning("import placement failed for %s: %s", item.basename, e)
        return ClipResult(item.basename, "error", detail=str(e),
                          size_bytes=item.size_bytes, event_type=item.event_type)

    if snap.gps_extract:
        try:
            vfs.extract_gps_data(dest)
        except Exception as e:  # clips without a GPS lock — non-fatal
            log.info("gpx extract failed for %s: %s", item.basename, e)

    _record_origin(db, item)
    return ClipResult(item.basename, "imported",
                      size_bytes=item.size_bytes, event_type=item.event_type)


# Browser uploads stream to ``<name>.part`` and are renamed to the
# plain Viofo name only after size verification — so a plain-named
# staged file is complete by construction (same-volume ingest stages
# via atomic rename) and safe to recover; a ``.part`` file never is.
UPLOAD_PART_SUFFIX = ".part"
_STALE_PART_S = 3600.0


def _tidy_staging(recordings: str) -> None:
    """Remove staging debris that is provably not worth keeping:
    unrecognised files and stale ``.part`` uploads. Fresh ``.part``
    files (an upload streaming right now) and recognised complete
    clips (recoverable — see :func:`recover_staging`) are left alone.
    The old behaviour deleted everything, which destroyed the only
    copy of a clip after a crash mid-ingest and tore down concurrent
    browser uploads."""
    staging = os.path.join(recordings, STAGING_DIRNAME)
    if not os.path.isdir(staging):
        return
    for name in os.listdir(staging):
        path = os.path.join(staging, name)
        if name.endswith(UPLOAD_PART_SUFFIX):
            try:
                stale = time.time() - os.path.getmtime(path) > _STALE_PART_S
            except OSError:
                continue
            if not stale:
                continue
        elif vfs.downloaded_filename_re.match(name):
            continue  # complete staged clip — recover_staging's job
        try:
            os.remove(path)
        except OSError:  # pragma: no cover — best-effort
            pass


def recover_staging(db: Database, snap) -> dict:
    """Salvage complete staged clips left behind by a crash between
    the staging move and the final rename, then tidy remaining debris.

    Recovery re-runs the normal ingest (make-room included); the
    staging "move" is a same-path no-op. A clip that can't be placed
    (over quota, IO error) stays staged for the next attempt rather
    than being deleted. Original ``RO/`` context is lost in staging,
    so recovered clips classify from their camera code alone.
    """
    recordings = snap.recordings
    staging = os.path.join(recordings, STAGING_DIRNAME)
    summary = {"recovered": 0, "failed": 0}
    if os.path.isdir(staging):
        for name in sorted(os.listdir(staging)):
            m = vfs.downloaded_filename_re.match(name)
            if not m:
                continue
            path = os.path.join(staging, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            try:
                item = scan_item_from_match(
                    m, name, source_rel_path=name, size=size, src_path=path,
                )
            except ValueError:
                continue
            res = ingest_clip(db, snap, item, cross_volume=False,
                              staged=False)
            if res.status == "imported":
                log.info("recovered staged clip %s", name)
                summary["recovered"] += 1
            elif res.status == "already_present":
                try:
                    os.remove(path)  # archive already has it
                except OSError:
                    pass
            else:
                log.warning("staged clip %s not recovered: %s",
                            name, res.status)
                summary["failed"] += 1
    _tidy_staging(recordings)
    return summary


def _broadcast(hub, loop, event: dict) -> None:
    if hub is None:
        return
    hub.schedule_broadcast(loop, event)


_SUMMARY_KEYS = (
    "imported", "already_present",
    "over_quota_older", "errors",
)


def run_folder_ingest(db: Database, snap, hub, loop, *, root: str) -> dict:
    """Ingest every recognised clip under ``root`` (newest-first),
    then run the post-sync pipeline. Runs on a worker thread."""
    recordings = snap.recordings
    recover_staging(db, snap)  # salvage any prior aborted run first
    man = scan_source(root)
    cross = is_cross_volume(root, recordings)

    summary = {k: 0 for k in _SUMMARY_KEYS}
    summary["not_recognised"] = len(man.skipped)   # from the manifest, not per-clip
    summary["bytes_imported"] = 0
    total = len(man.items)
    _broadcast(hub, loop, {"type": "import_started", "total": total})

    for i, item in enumerate(man.items, 1):
        res = ingest_clip(db, snap, item, cross_volume=cross)
        if res.status in summary:
            summary[res.status] += 1
        else:  # 'error'
            summary["errors"] += 1
        if res.status == "imported":
            summary["bytes_imported"] += res.size_bytes
        _broadcast(hub, loop, {
            "type": "import_progress", "done": i, "total": total,
            "filename": res.filename, "result": res.status,
        })

    # Reuse the post-sync pipeline. scanner.scan handles its own
    # broadcast + threadsafe scheduling, and enqueues missing derives.
    scanner.scan(db, recordings, snap.grouping, hub, loop)
    from .exporter import export_protect_ids
    _retention.sweep(
        db, recordings,
        max_days=snap.retention_max_days,
        disk_pct=snap.retention_disk_pct,
        protect_ro=snap.retention_protect_ro,
        quota_gb=snap.recordings_quota_gb,
        exclude=_retention.import_exclude_set(recordings, snap.import_path),
        protect_ids=export_protect_ids(db),
    )
    log.info(
        "import complete: %d imported, %d already_present, %d not_recognised, "
        "%d over_quota_older, %d error(s)",
        summary["imported"], summary["already_present"],
        summary["not_recognised"], summary["over_quota_older"],
        summary["errors"],
    )
    _broadcast(hub, loop, {"type": "import_done", **summary})
    # Tidy (not wipe): keeps recoverable clips from this run's
    # failures and any concurrent browser upload's fresh .part file.
    _tidy_staging(recordings)
    return summary
