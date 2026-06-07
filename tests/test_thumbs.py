"""Tests for the thumbnail service (ffmpeg mocked)."""
from __future__ import annotations

from pathlib import Path

from web.services import thumbs


class _HangProc:
    """Fake ffmpeg child: kill() records, wait() counts body runs."""
    returncode = None

    def __init__(self):
        self.killed = False
        self.reaped = 0

    def kill(self):
        self.killed = True

    async def wait(self):
        self.reaped += 1
        return 0


async def _raise_timeout(coro, timeout):
    # Close the inner proc.wait() coroutine so it isn't left un-awaited
    # (the suite runs under filterwarnings=error), then simulate a timeout.
    coro.close()
    raise TimeoutError


async def test_ensure_thumb_reaps_child_on_timeout(tmp_path: Path, monkeypatch):
    rec = str(tmp_path)
    monkeypatch.setattr(thumbs.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    fake = _HangProc()

    async def fake_exec(*a, **k):
        return fake

    monkeypatch.setattr(thumbs.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(thumbs.asyncio, "wait_for", _raise_timeout)

    result = await thumbs.ensure_thumb(rec, 7, "/x.mp4")
    assert result is None
    assert fake.killed is True
    assert fake.reaped == 1   # proc.wait() awaited after kill -> child reaped
