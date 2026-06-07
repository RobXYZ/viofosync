"""On-demand filmstrip sprite-sheet generation via ffmpeg.

One JPEG per clip: a horizontal montage of frames, one every
``INTERVAL_S`` seconds, packed with ffmpeg's ``tile`` filter. Cached
to ``$RECORDINGS/.filmstrips/<clip_id>.jpg`` with a sidecar
``<clip_id>.json`` holding the slicing metadata the frontend needs.

Mirrors ``thumbs.py``: the first request shells out to ffmpeg; later
requests read the cache. Returns ``None`` if ffmpeg is missing or
extraction failed, so the API layer can serve a placeholder.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import shutil
from dataclasses import asdict, dataclass

INTERVAL_S = 8          # one frame every 8 seconds
TILE_W = 160            # tile width  (16:9 dashcam frame)
TILE_H = 90             # tile height
_MAX_CONCURRENCY = 3    # cap simultaneous ffmpeg children


@dataclass
class FilmstripMeta:
    frames: int
    interval_s: int
    tile_w: int
    tile_h: int
    duration_s: float


def _cache_dir(recordings: str) -> str:
    d = os.path.join(recordings, ".filmstrips")
    os.makedirs(d, exist_ok=True)
    return d


def sprite_path(recordings: str, clip_id: int) -> str:
    return os.path.join(_cache_dir(recordings), f"{clip_id}.jpg")


def meta_path(recordings: str, clip_id: int) -> str:
    return os.path.join(_cache_dir(recordings), f"{clip_id}.json")


def frame_count(duration_s: float | None, interval_s: int = INTERVAL_S) -> int:
    """Number of tiles for a clip: one frame every ``interval_s``
    seconds, always at least one."""
    if not duration_s or duration_s <= 0:
        return 1
    return max(1, math.ceil(duration_s / interval_s))


# Per-event-loop semaphores. A module-level Semaphore created at
# import binds to whichever loop first acquires it, which breaks
# pytest's function-scoped loops; keying by the running loop keeps
# it correct in both tests and the single-loop production server.
_sems: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _sems.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        _sems[loop] = sem
    return sem


def _build_cmd(ffmpeg: str, video_path: str, out: str, frames: int) -> list[str]:
    return [
        ffmpeg,
        "-loglevel", "error",
        "-y",
        "-i", video_path,
        "-vf", f"fps=1/{INTERVAL_S},scale={TILE_W}:{TILE_H},tile={frames}x1",
        "-frames:v", "1",
        out,
    ]


def _read_cached_meta(mp: str) -> FilmstripMeta | None:
    try:
        with open(mp) as f:
            return FilmstripMeta(**json.load(f))
    except (OSError, ValueError, TypeError, KeyError):
        return None  # corrupt/old/partial sidecar -> regenerate


async def ensure_filmstrip(
    recordings: str, clip_id: int, video_path: str, duration_s: float | None
) -> FilmstripMeta | None:
    """Return slicing metadata for ``clip_id``'s filmstrip sprite,
    generating the sprite + sidecar if missing. ``None`` when ffmpeg
    is unavailable or extraction failed."""
    sp = sprite_path(recordings, clip_id)
    mp = meta_path(recordings, clip_id)

    if os.path.exists(sp) and os.path.getsize(sp) > 0 and os.path.exists(mp):
        cached = _read_cached_meta(mp)
        if cached is not None:
            return cached

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None

    frames = frame_count(duration_s)
    async with _semaphore():
        proc = await asyncio.create_subprocess_exec(
            *_build_cmd(ffmpeg, video_path, sp, frames),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=60.0)
        except TimeoutError:   # asyncio.TimeoutError is the builtin since 3.11
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()   # reap the killed child (no zombie)
            return None

    if proc.returncode != 0 or not os.path.exists(sp) or os.path.getsize(sp) == 0:
        return None

    meta = FilmstripMeta(
        frames=frames, interval_s=INTERVAL_S,
        tile_w=TILE_W, tile_h=TILE_H,
        duration_s=float(duration_s) if duration_s else 0.0,
    )
    try:
        with open(mp, "w") as f:
            json.dump(asdict(meta), f)
    except OSError:
        pass  # sprite is usable even if the sidecar write fails
    return meta
