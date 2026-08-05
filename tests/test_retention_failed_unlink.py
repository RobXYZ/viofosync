"""Failed unlinks must not drop index rows, credit freed bytes, or wedge
the retention loops.

Regression tests for the Unraid permissions failure mode: the container
user can't unlink in the recordings share, so os.remove raises
PermissionError. Before the fix, delete_clip removed the clip_index row
anyway (the clip vanished from the UI, no space freed, and it returned on
the next rescan), and the disk-pressure pass credited the file's size as
freed while looping over the same un-unlinkable candidates.
"""
from __future__ import annotations

import collections
import os
from pathlib import Path

import web.services.retention as ret


def _env(tmp_path: Path):
    from web.db import Database
    rec = tmp_path / "rec"
    rec.mkdir()
    db = Database(str(rec / "v.db"))
    return rec, db


def _make_clip(rec: Path, db, *, basename: str, ts: int):
    folder = rec / "2026-06-26"
    folder.mkdir(exist_ok=True)
    path = folder / basename
    path.write_bytes(b"x" * 1024)
    with db.write() as c:
        cur = c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " event_type, size_bytes, has_gpx, scanned_at) "
            "VALUES (?, ?, '2026-06-26', ?, 'F', 1, 'normal', 1024, 0, 1)",
            (str(path), basename, ts),
        )
        return cur.lastrowid, path


def _deny_mp4(monkeypatch):
    """Patch retention's os.remove so every .MP4 unlink raises the classic
    share-permissions error; sidecars/caches still remove normally."""
    real_remove = os.remove

    def deny(p, *a, **kw):
        if str(p).endswith(".MP4"):
            raise PermissionError(1, "Operation not permitted", str(p))
        return real_remove(p, *a, **kw)

    monkeypatch.setattr("web.services.retention.os.remove", deny)


def _index_count(db) -> int:
    with db.conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM clip_index").fetchone()["n"]


def test_delete_clip_keeps_index_row_on_failed_unlink(tmp_path, monkeypatch):
    rec, db = _env(tmp_path)
    cid, path = _make_clip(rec, db, basename="A.MP4", ts=100)
    _deny_mp4(monkeypatch)
    freed, ok = ret.delete_clip(db, {"id": cid, "path": str(path)}, str(rec))
    assert ok is False
    assert freed == 0            # nothing was actually reclaimed
    assert path.exists()
    assert _index_count(db) == 1  # row survives → UI stays truthful


def test_sweep_time_rule_failed_unlink_keeps_row(tmp_path, monkeypatch):
    rec, db = _env(tmp_path)
    _, path = _make_clip(rec, db, basename="OLD.MP4", ts=0)
    _deny_mp4(monkeypatch)
    summary = ret.sweep(
        db, str(rec), max_days=1, disk_pct=0, protect_ro=True,
        _now=86400 * 30,
    )
    assert summary["deleted_time"] == 0
    assert summary["failed"] == 1
    assert summary["bytes_freed"] == 0
    assert path.exists()
    assert _index_count(db) == 1


def test_disk_pressure_pass_skips_failed_and_terminates(tmp_path, monkeypatch):
    # Disk usage is pinned over threshold, and every unlink fails — the
    # pass must still terminate (failed ids leave the candidate pool)
    # with nothing deleted and nothing credited as freed.
    rec, db = _env(tmp_path)
    _make_clip(rec, db, basename="A.MP4", ts=100)
    _make_clip(rec, db, basename="B.MP4", ts=200)

    DU = collections.namedtuple("DU", "total used free")
    monkeypatch.setattr(
        "web.services.retention.shutil.disk_usage",
        lambda p: DU(total=100, used=95, free=5),
    )
    _deny_mp4(monkeypatch)
    deleted, bytes_freed, protected, failed = ret._disk_pressure_pass(
        db, str(rec), disk_pct=80, quota_gb=0, protect_ro=True, sink=None,
    )
    assert deleted == 0
    assert bytes_freed == 0
    assert failed == 2
    assert _index_count(db) == 2


def test_make_room_for_failed_unlink_terminates_false(tmp_path, monkeypatch):
    # Same wedge-guard for the import path: with every candidate
    # un-unlinkable and the quota permanently breached, make_room_for
    # must exhaust the pool and report False rather than spin.
    rec, db = _env(tmp_path)
    _make_clip(rec, db, basename="A.MP4", ts=100)
    monkeypatch.setattr(
        ret, "_scan_dir_bytes",
        lambda p, exclude=frozenset(): 2 * (1 << 30),
    )
    _deny_mp4(monkeypatch)
    ok = ret.make_room_for(
        db, str(rec), size=0, before_ts=300,
        disk_pct=0, quota_gb=1, protect_ro=True,
    )
    assert ok is False
    assert _index_count(db) == 1
