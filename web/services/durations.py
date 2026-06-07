"""Populate ``clip_index.duration_s`` via ffprobe.

The scanner indexes clips from filenames but never measures their
length. ``duration_s`` drives filmstrip frame counts and the
timeline layout, so probe any clip missing it and store the value.
Mirrors ``scanner.sweep_missing_thumbs``: bounded concurrency,
idempotent (only NULL/zero rows are probed), non-fatal on failure.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil

from ..db import Database

log = logging.getLogger("viofosync.durations")


async def probe_duration(path: str) -> float | None:
    """Clip length in seconds via ffprobe, or None if ffprobe is
    missing / the probe fails / the value is non-positive."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
    except (TimeoutError, OSError):
        return None
    try:
        d = float(out.decode().strip())
    except ValueError:
        return None
    return d if d > 0 else None


async def sweep_missing_durations(db: Database, *, concurrency: int = 4) -> int:
    """ffprobe every indexed clip with a NULL/zero ``duration_s`` and
    store the result. Returns the number of rows updated. Idempotent."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, path FROM clip_index "
            "WHERE duration_s IS NULL OR duration_s <= 0"
        ).fetchall()
    todo = [(r["id"], r["path"]) for r in rows if os.path.isfile(r["path"])]
    if not todo:
        return 0

    log.info("duration sweep: probing %d clip(s) (concurrency=%d)",
             len(todo), concurrency)
    sem = asyncio.Semaphore(concurrency)

    async def _one(clip_id: int, path: str) -> tuple[int, float | None]:
        async with sem:
            try:
                return clip_id, await probe_duration(path)
            except Exception as e:  # pragma: no cover — non-fatal
                log.warning("duration probe failed for %s: %s", path, e)
                return clip_id, None

    results = await asyncio.gather(*(_one(cid, p) for cid, p in todo))

    updated = 0
    with db.write() as c:
        for clip_id, dur in results:
            if dur is not None:
                c.execute(
                    "UPDATE clip_index SET duration_s = ? WHERE id = ?",
                    (dur, clip_id),
                )
                updated += 1
    log.info("duration sweep: %d updated", updated)
    return updated
