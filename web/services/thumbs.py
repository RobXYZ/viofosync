"""On-demand thumbnail generation via ffmpeg.

Thumbs are cached to ``$RECORDINGS/.thumbs/<clip_id>.jpg``.
First request is slow (shells out to ffmpeg); subsequent
requests are a direct file read.

If ffmpeg isn't installed, :func:`ensure_thumb` returns
``None`` and the API layer serves a placeholder.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil

_TIMEOUT_S = 15.0
_MAX_CONCURRENCY = 3

# Per-event-loop semaphore (same pattern as filmstrip.py): a
# module-level Semaphore binds to whichever loop first touches it,
# which breaks pytest's function-scoped loops.
_sems: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _sems.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        _sems[loop] = sem
    return sem


def _discard(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


# Dirs already ensured this process. The path helpers below run on every
# thumb request and inside retention loops; without the memo each call pays
# a makedirs — a metadata round-trip on a NAS mount — for a dir that
# almost always exists.
_ensured_dirs: set[str] = set()


def _cache_dir(recordings: str) -> str:
    d = os.path.join(recordings, ".thumbs")
    if d not in _ensured_dirs:
        os.makedirs(d, exist_ok=True)
        _ensured_dirs.add(d)
    return d


def thumb_path(recordings: str, clip_id: int) -> str:
    return os.path.join(_cache_dir(recordings), f"{clip_id}.jpg")


def fail_marker_path(recordings: str, clip_id: int) -> str:
    return os.path.join(_cache_dir(recordings), f"{clip_id}.jpg.fail")


def mark_failed(recordings: str, clip_id: int) -> None:
    """Record that thumbnail extraction failed for this clip, so the
    sweep doesn't re-run ffmpeg on it every pass. Cleared automatically
    once the source file changes (see :func:`failed_recently`)."""
    try:
        with open(fail_marker_path(recordings, clip_id), "w"):
            pass
    except OSError:  # pragma: no cover — best-effort cache
        pass


def _clear_failed(recordings: str, clip_id: int) -> None:
    try:
        os.remove(fail_marker_path(recordings, clip_id))
    except OSError:
        pass


def failed_recently(recordings: str, clip_id: int, video_path: str) -> bool:
    """True if a prior thumbnail attempt failed and the source file
    hasn't changed since. A marker older than the video (the clip was
    rewritten — e.g. a partial import got redone) is treated as stale,
    so the thumb is worth another attempt."""
    marker = fail_marker_path(recordings, clip_id)
    try:
        marker_mtime = os.path.getmtime(marker)
    except OSError:
        return False
    try:
        return marker_mtime >= os.path.getmtime(video_path)
    except OSError:  # video gone — let the caller's isfile check handle it
        return False


async def ensure_thumb(
    recordings: str, clip_id: int, video_path: str
) -> str | None:
    """Return the path to a JPEG thumbnail for ``video_path``,
    generating it if missing. ``None`` if ffmpeg is unavailable
    or extraction failed."""
    out = thumb_path(recordings, clip_id)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None

    # ffmpeg writes to a temp name; only a verified result is renamed
    # onto the cache path. Writing the final path directly meant a
    # killed/timed-out job left a partial JPEG that the cache check
    # above then served forever. The semaphore caps concurrent ffmpeg
    # spawns — a 100-clip day view used to launch 100 at once.
    tmp = f"{out}.part.jpg"
    async with _semaphore():
        # Re-check after waiting: a concurrent request for the same
        # clip may have produced the thumb while we queued.
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        # Seek to 1s (skip the frequently-black first frame) and
        # grab one frame scaled to 320 wide.
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-loglevel", "error",
            "-y",
            "-ss", "1",
            "-i", video_path,
            "-frames:v", "1",
            "-vf", "scale=320:-1",
            tmp,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TIMEOUT_S)
        except TimeoutError:  # asyncio.TimeoutError is the builtin since 3.11
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()   # reap the killed child (no zombie)
            _discard(tmp)
            mark_failed(recordings, clip_id)
            return None

        if (proc.returncode != 0 or not os.path.exists(tmp)
                or os.path.getsize(tmp) == 0):
            _discard(tmp)
            mark_failed(recordings, clip_id)
            return None
        os.replace(tmp, out)
    _clear_failed(recordings, clip_id)
    return out
