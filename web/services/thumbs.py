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


def _cache_dir(recordings: str) -> str:
    d = os.path.join(recordings, ".thumbs")
    os.makedirs(d, exist_ok=True)
    return d


def thumb_path(recordings: str, clip_id: int) -> str:
    return os.path.join(_cache_dir(recordings), f"{clip_id}.jpg")


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
        out,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=15.0)
    except TimeoutError:   # asyncio.TimeoutError is the builtin since 3.11
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()   # reap the killed child (no zombie)
        return None

    if proc.returncode != 0 or not os.path.exists(out):
        return None
    return out
