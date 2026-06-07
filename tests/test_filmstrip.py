"""Tests for the filmstrip sprite service (ffmpeg mocked)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from web.services import filmstrip, retention


def test_frame_count_basics():
    # 60s clip, one frame / 8s -> ceil(60/8) = 8
    assert filmstrip.frame_count(60.0) == 8
    # exact multiple
    assert filmstrip.frame_count(16.0) == 2
    # short / zero / None always yields at least one tile
    assert filmstrip.frame_count(3.0) == 1
    assert filmstrip.frame_count(0.0) == 1
    assert filmstrip.frame_count(None) == 1


def test_paths_are_under_filmstrips_dir(tmp_path: Path):
    rec = str(tmp_path)
    sp = filmstrip.sprite_path(rec, 42)
    mp = filmstrip.meta_path(rec, 42)
    assert sp.endswith(os.path.join(".filmstrips", "42.jpg"))
    assert mp.endswith(os.path.join(".filmstrips", "42.json"))
    # accessing a path helper creates the cache dir
    assert os.path.isdir(os.path.join(rec, ".filmstrips"))


async def test_ensure_returns_none_when_ffmpeg_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(filmstrip.shutil, "which", lambda _name: None)
    meta = await filmstrip.ensure_filmstrip(
        str(tmp_path), 7, str(tmp_path / "clip.mp4"), 60.0
    )
    assert meta is None


async def test_ensure_cache_hit_reads_sidecar_without_ffmpeg(tmp_path: Path, monkeypatch):
    rec = str(tmp_path)
    # Pre-seed a cached sprite + sidecar.
    Path(filmstrip.sprite_path(rec, 9)).write_bytes(b"\xff\xd8\xff\xd9")  # tiny JPEG-ish
    Path(filmstrip.meta_path(rec, 9)).write_text(json.dumps({
        "frames": 8, "interval_s": 8, "tile_w": 160, "tile_h": 90, "duration_s": 60.0,
    }))

    # If ffmpeg were invoked this would explode — proves the cache short-circuits.
    def _boom(*a, **k):
        raise AssertionError("ffmpeg must not run on a cache hit")
    monkeypatch.setattr(filmstrip.asyncio, "create_subprocess_exec", _boom)

    meta = await filmstrip.ensure_filmstrip(rec, 9, str(tmp_path / "clip.mp4"), 60.0)
    assert meta == filmstrip.FilmstripMeta(8, 8, 160, 90, 60.0)


async def test_ensure_generates_sprite_and_sidecar(tmp_path: Path, monkeypatch):
    rec = str(tmp_path)
    monkeypatch.setattr(filmstrip.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    captured = {}

    class _FakeProc:
        returncode = 0
        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        captured["args"] = list(args)
        # ffmpeg would write the sprite as its last positional arg.
        Path(args[-1]).write_bytes(b"\xff\xd8\xff\xd9")
        return _FakeProc()

    monkeypatch.setattr(filmstrip.asyncio, "create_subprocess_exec", fake_exec)

    meta = await filmstrip.ensure_filmstrip(rec, 5, "/rec/clip.mp4", 60.0)

    # Returned + persisted metadata
    assert meta == filmstrip.FilmstripMeta(8, 8, 160, 90, 60.0)
    assert os.path.exists(filmstrip.sprite_path(rec, 5))
    with open(filmstrip.meta_path(rec, 5)) as f:
        assert json.load(f)["frames"] == 8

    # Correct ffmpeg invocation
    args = captured["args"]
    assert "/usr/bin/ffmpeg" in args[0]
    vf = args[args.index("-vf") + 1]
    assert vf == "fps=1/8,scale=160:90,tile=8x1"
    assert args[args.index("-i") + 1] == "/rec/clip.mp4"


async def test_ensure_returns_none_on_ffmpeg_nonzero(tmp_path: Path, monkeypatch):
    rec = str(tmp_path)
    monkeypatch.setattr(filmstrip.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    class _FailProc:
        returncode = 1
        async def wait(self):
            return 1

    async def fake_exec(*args, **kwargs):
        return _FailProc()  # writes nothing

    monkeypatch.setattr(filmstrip.asyncio, "create_subprocess_exec", fake_exec)
    meta = await filmstrip.ensure_filmstrip(rec, 6, "/rec/clip.mp4", 60.0)
    assert meta is None


def test_retention_removes_filmstrip_sprite_and_sidecar(tmp_path: Path):
    rec = str(tmp_path)
    clip_file = tmp_path / "clip.mp4"
    clip_file.write_bytes(b"\0")

    sp = filmstrip.sprite_path(rec, 11)
    mp = filmstrip.meta_path(rec, 11)
    Path(sp).write_bytes(b"\xff\xd8\xff\xd9")
    Path(mp).write_text("{}")

    rec_row = {"id": 11, "path": str(clip_file)}
    retention._delete_clip_files(rec_row, rec)

    assert not clip_file.exists()
    assert not os.path.exists(sp)
    assert not os.path.exists(mp)


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


async def test_ensure_reaps_child_on_timeout(tmp_path: Path, monkeypatch):
    rec = str(tmp_path)
    monkeypatch.setattr(filmstrip.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    fake = _HangProc()

    async def fake_exec(*a, **k):
        return fake

    monkeypatch.setattr(filmstrip.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(filmstrip.asyncio, "wait_for", _raise_timeout)

    result = await filmstrip.ensure_filmstrip(rec, 99, "/x.mp4", 60.0)
    assert result is None
    assert fake.killed is True
    assert fake.reaped == 1   # proc.wait() awaited after kill -> child reaped
