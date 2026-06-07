"""Tests for the clip duration ffprobe sweep."""
from __future__ import annotations

from pathlib import Path

import pytest

from web.db import Database
from web.services import durations


def _insert_clip(db, clip_id, path, duration_s=None):
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(id, path, basename, group_name, timestamp, camera, "
            " sequence, event_type, has_gpx, gps_examined, scanned_at, duration_s) "
            "VALUES (?,?,?,?,?,?,?,?,0,0,?,?)",
            (clip_id, path, f"{clip_id}.MP4", "2026-06-02",
             1_717_312_440, "F", clip_id, "normal", 1_717_312_440, duration_s),
        )


async def test_probe_duration_parses_ffprobe(monkeypatch):
    class _P:
        async def communicate(self):
            return (b"60.05\n", b"")
    async def fake_exec(*a, **k):
        return _P()
    monkeypatch.setattr(durations.shutil, "which", lambda _n: "/usr/bin/ffprobe")
    monkeypatch.setattr(durations.asyncio, "create_subprocess_exec", fake_exec)
    assert await durations.probe_duration("/x.mp4") == pytest.approx(60.05)


async def test_probe_duration_none_without_ffprobe(monkeypatch):
    monkeypatch.setattr(durations.shutil, "which", lambda _n: None)
    assert await durations.probe_duration("/x.mp4") is None


async def test_sweep_updates_null_durations(tmp_path: Path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    f1 = tmp_path / "clip1.mp4"
    f1.write_bytes(b"\0")
    f2 = tmp_path / "clip2.mp4"
    f2.write_bytes(b"\0")
    _insert_clip(db, 1, str(f1), duration_s=None)        # needs probe
    _insert_clip(db, 2, str(f2), duration_s=60.0)        # already has one -> skipped

    async def fake_probe(path):
        return 42.0
    monkeypatch.setattr(durations, "probe_duration", fake_probe)

    updated = await durations.sweep_missing_durations(db)
    assert updated == 1
    with db.conn() as c:
        d1 = c.execute("SELECT duration_s FROM clip_index WHERE id=1").fetchone()["duration_s"]
        d2 = c.execute("SELECT duration_s FROM clip_index WHERE id=2").fetchone()["duration_s"]
    assert d1 == pytest.approx(42.0)
    assert d2 == pytest.approx(60.0)   # untouched


async def test_sweep_skips_missing_files(tmp_path: Path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    _insert_clip(db, 1, str(tmp_path / "gone.mp4"), duration_s=None)  # file absent
    async def fake_probe(path):
        raise AssertionError("should not probe a missing file")
    monkeypatch.setattr(durations, "probe_duration", fake_probe)
    assert await durations.sweep_missing_durations(db) == 0
