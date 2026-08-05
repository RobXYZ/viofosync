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
import logging
import math
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass

log = logging.getLogger("viofosync.filmstrip")

INTERVAL_S = 8          # one frame every 8 seconds
TILE_W = 160            # tile width  (16:9 dashcam frame)
TILE_H = 90             # tile height
_MAX_CONCURRENCY = 3    # cap simultaneous ffmpeg children
_FFMPEG_TIMEOUT_S = 60.0  # kill a sprite job that outruns this

# ffmpeg-missing warns once, not once-per-clip — a whole day of clips
# would otherwise flood the log with the same line.
_warned_no_ffmpeg = False


@dataclass
class FilmstripMeta:
    frames: int
    interval_s: int
    tile_w: int
    tile_h: int
    duration_s: float


# Same makedirs memo as thumbs.py — sprite_path/meta_path are called per
# request and from retention; don't pay a NAS metadata round-trip each time.
_ensured_dirs: set[str] = set()


def _cache_dir(recordings: str) -> str:
    d = os.path.join(recordings, ".filmstrips")
    if d not in _ensured_dirs:
        os.makedirs(d, exist_ok=True)
        _ensured_dirs.add(d)
    return d


def sprite_path(recordings: str, clip_id: int) -> str:
    return os.path.join(_cache_dir(recordings), f"{clip_id}.jpg")


def meta_path(recordings: str, clip_id: int) -> str:
    return os.path.join(_cache_dir(recordings), f"{clip_id}.json")


def _fail_path(recordings: str, clip_id: int) -> str:
    return os.path.join(_cache_dir(recordings), f"{clip_id}.filmstrip.fail")


def mark_failed(recordings: str, clip_id: int) -> None:
    """Record that sprite generation failed for ``clip_id`` so the derive
    sweep doesn't re-spawn ffmpeg on the same un-renderable clip."""
    try:
        os.makedirs(_cache_dir(recordings), exist_ok=True)
        with open(_fail_path(recordings, clip_id), "w") as f:
            f.write("")
    except OSError:
        pass


def _clear_failed(recordings: str, clip_id: int) -> None:
    try:
        os.remove(_fail_path(recordings, clip_id))
    except OSError:
        pass


def failed_recently(recordings: str, clip_id: int, video_path: str) -> bool:
    """True if generation failed and the video hasn't changed since — so a
    corrupt/too-short clip is skipped until its file is replaced."""
    fp = _fail_path(recordings, clip_id)
    try:
        fail_mtime = os.path.getmtime(fp)
    except OSError:
        return False
    try:
        return os.path.getmtime(video_path) <= fail_mtime
    except OSError:
        return False


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


def _extract_cmd(ffmpeg: str, video_path: str, ts: float, out: str) -> list[str]:
    """ffmpeg argv to grab one scaled frame near ``ts`` seconds.

    Input seeking (``-ss`` *before* ``-i``) jumps to the nearest keyframe via
    the container index and reads only a small chunk around ``ts`` — so a
    whole sprite reads ~one chunk per tile instead of streaming the entire
    file. Benchmarked ~3x faster wall-clock and ~half the CPU of the old
    single-pass decode on a NAS, where reading the file was the bottleneck.
    Software only: hardware decode is *slower* here (it can't honour
    ``skip_frame`` and pays a per-frame GPU->RAM download)."""
    return [
        ffmpeg, "-loglevel", "error", "-y",
        "-ss", str(ts),
        "-i", video_path,
        "-an",
        "-frames:v", "1",
        "-vf", f"scale={TILE_W}:{TILE_H}",
        out,
    ]


def _tile_cmd(ffmpeg: str, pattern: str, frames: int, out: str) -> list[str]:
    """ffmpeg argv to stitch the extracted per-tile frames (an image2
    sequence) into the horizontal sprite. Tiny, fast, no large I/O."""
    return [
        ffmpeg, "-loglevel", "error", "-y",
        "-start_number", "0",
        "-i", pattern,
        "-vf", f"tile={frames}x1",
        "-frames:v", "1",
        out,
    ]


async def _run_ffmpeg(cmd: list[str], timeout: float) -> int | None:
    """Run one ffmpeg child with a timeout. Returns its return code, or
    ``None`` if it timed out (the child is killed and reaped)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:   # asyncio.TimeoutError is the builtin since 3.11
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()   # reap the killed child (no zombie)
        return None
    return proc.returncode


async def generate_sprite_at(
    ffmpeg: str, video_path: str, sprite: str, timestamps: list[float],
) -> bool:
    """Extract one scaled frame at each timestamp (seconds) and stitch them
    into ``sprite`` — a horizontal montage, one tile per timestamp. Returns
    True on success. Shared by the clip filmstrip (interval-spaced timestamps)
    and the export preview (N evenly-spaced timestamps). Extractions run
    sequentially so one sprite uses one ffmpeg at a time; the caller's
    semaphore bounds how many sprites run at once."""
    tiles_dir = tempfile.mkdtemp(prefix=".tiles_", dir=os.path.dirname(sprite))
    try:
        for i, ts in enumerate(timestamps):
            tile = os.path.join(tiles_dir, f"f{i:04d}.jpg")
            rc = await _run_ffmpeg(
                _extract_cmd(ffmpeg, video_path, ts, tile), _FFMPEG_TIMEOUT_S,
            )
            if rc != 0 or not (os.path.exists(tile) and os.path.getsize(tile) > 0):
                return False
        pattern = os.path.join(tiles_dir, "f%04d.jpg")
        # Montage to a temp name; rename only a verified result.
        # Writing the final path directly left a partial sprite that
        # the callers' exists()+size cache checks then served forever.
        tmp_sprite = f"{sprite}.part.jpg"
        rc = await _run_ffmpeg(
            _tile_cmd(ffmpeg, pattern, len(timestamps), tmp_sprite),
            _FFMPEG_TIMEOUT_S,
        )
        if (rc == 0 and os.path.exists(tmp_sprite)
                and os.path.getsize(tmp_sprite) > 0):
            os.replace(tmp_sprite, sprite)
            return True
        with contextlib.suppress(OSError):
            os.remove(tmp_sprite)
        return False
    finally:
        shutil.rmtree(tiles_dir, ignore_errors=True)


async def _generate_sprite(
    ffmpeg: str, video_path: str, sprite: str, frames: int
) -> bool:
    """Interval-spaced montage (one frame per ``INTERVAL_S`` of clip), used by
    the clip filmstrip. Thin wrapper over :func:`generate_sprite_at`."""
    timestamps = [i * INTERVAL_S for i in range(frames)]
    return await generate_sprite_at(ffmpeg, video_path, sprite, timestamps)


def _read_cached_meta(mp: str) -> FilmstripMeta | None:
    try:
        with open(mp) as f:
            return FilmstripMeta(**json.load(f))
    except (OSError, ValueError, TypeError, KeyError):
        return None  # corrupt/old/partial sidecar -> regenerate


async def ensure_filmstrip(
    recordings: str,
    clip_id: int,
    video_path: str,
    duration_s: float | None,
) -> FilmstripMeta | None:
    """Return slicing metadata for ``clip_id``'s filmstrip sprite,
    generating the sprite + sidecar if missing. ``None`` when ffmpeg
    is unavailable or extraction failed."""
    sp = sprite_path(recordings, clip_id)
    mp = meta_path(recordings, clip_id)

    if os.path.exists(sp) and os.path.getsize(sp) > 0 and os.path.exists(mp):
        cached = _read_cached_meta(mp)
        if cached is not None:
            log.debug("filmstrip cache hit clip=%s", clip_id)
            return cached

    if failed_recently(recordings, clip_id, video_path):
        return None

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        global _warned_no_ffmpeg
        if not _warned_no_ffmpeg:
            _warned_no_ffmpeg = True
            log.warning(
                "filmstrip: ffmpeg not found on PATH — sprites cannot be "
                "generated; the timeline will show placeholder tiles"
            )
        return None

    frames = frame_count(duration_s)
    # The CPU cost is roughly proportional to frame count (one decoded,
    # scaled frame per INTERVAL_S of clip). Logging it here makes a NAS
    # CPU spike traceable to the exact clips being rendered.
    log.info(
        "filmstrip: generating clip=%s frames=%d duration=%.0fs",
        clip_id, frames, duration_s or 0.0,
    )
    started = time.monotonic()
    async with _semaphore():
        ok = await _generate_sprite(ffmpeg, video_path, sp, frames)

    elapsed = time.monotonic() - started
    if not ok:
        log.warning(
            "filmstrip: clip=%s generation failed after %.1fs (frames=%d)",
            clip_id, elapsed, frames,
        )
        mark_failed(recordings, clip_id)
        return None
    log.info(
        "filmstrip: clip=%s done in %.1fs (frames=%d)",
        clip_id, elapsed, frames,
    )

    _clear_failed(recordings, clip_id)
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
