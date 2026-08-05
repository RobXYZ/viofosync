"""Tests for import staging exclusion + make_room_for."""
from __future__ import annotations

from pathlib import Path

import pytest

from web.db import Database
from web.services import retention as ret


@pytest.fixture
def env(tmp_path: Path):
    rec = tmp_path / "rec"
    rec.mkdir()
    db = Database(str(tmp_path / ".viofosync.db"))
    return rec, db


def test_import_exclude_set(tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    rec_abs = str(rec.resolve())
    tmp = f"{rec_abs}/.import_tmp"

    # Default: .import_tmp + the in-tree default import folder.
    s = ret.import_exclude_set(rec_abs)
    assert s == frozenset({tmp, f"{rec_abs}/import"})

    # In-tree custom import dir is excluded.
    s = ret.import_exclude_set(rec_abs, f"{rec_abs}/incoming")
    assert f"{rec_abs}/incoming" in s and tmp in s

    # Out-of-tree (external mount) import path: only .import_tmp excluded.
    s = ret.import_exclude_set(rec_abs, "/mnt/usb")
    assert s == frozenset({tmp})

    # import_path equal to the recordings root is NOT added.
    s = ret.import_exclude_set(rec_abs, rec_abs)
    assert s == frozenset({tmp})


def test_scan_dir_bytes_excludes_given_dirs(env):
    rec, _db = env
    (rec / "2026-01-01").mkdir()
    (rec / "2026-01-01" / "A.MP4").write_bytes(b"x" * 1000)
    staging = rec / ".import_tmp"
    staging.mkdir()
    (staging / "B.MP4").write_bytes(b"y" * 5000)

    everything = ret._scan_dir_bytes(str(rec))
    excluded = ret._scan_dir_bytes(
        str(rec), exclude=frozenset({str(staging.resolve())})
    )
    assert everything == 6000
    assert excluded == 1000


def _clip(rec: Path, db: Database, *, basename: str, ts: int,
          size: int, event_type: str = "normal") -> int:
    import time as _t
    day = _t.strftime("%Y-%m-%d", _t.gmtime(ts))
    folder = rec / day
    folder.mkdir(exist_ok=True)
    (folder / basename).write_bytes(b"x" * size)
    with db.write() as c:
        cur = c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " event_type, size_bytes, has_gpx, scanned_at) "
            "VALUES (?,?,?,?, 'F', 1, ?, ?, 0, ?)",
            (str(folder / basename), basename, day, ts, event_type,
             size, int(_t.time())),
        )
        return cur.lastrowid


def _ids(db: Database) -> set[str]:
    with db.conn() as c:
        return {r["basename"] for r in c.execute(
            "SELECT basename FROM clip_index").fetchall()}


def _patch_quota(monkeypatch, used_bytes: int):
    # Force _scan_dir_bytes to report a fixed total regardless of disk.
    monkeypatch.setattr(
        "web.services.retention._scan_dir_bytes",
        lambda path, exclude=frozenset(): used_bytes,
    )


def test_make_room_no_rules_always_true(env):
    rec, db = env
    assert ret.make_room_for(
        db, str(rec), size=10, before_ts=100,
        disk_pct=0, quota_gb=0, protect_ro=True,
    ) is True


def test_make_room_under_quota_no_eviction(env, monkeypatch):
    rec, db = env
    _clip(rec, db, basename="OLD.MP4", ts=100, size=1)
    _clip(rec, db, basename="MID.MP4", ts=200, size=1)
    _patch_quota(monkeypatch, used_bytes=2)  # 2 bytes, well under 1 GiB
    ok = ret.make_room_for(
        db, str(rec), size=0, before_ts=300,
        disk_pct=0, quota_gb=1, protect_ro=True,
    )
    # Under threshold -> nothing evicted, returns True.
    assert ok is True
    assert _ids(db) == {"OLD.MP4", "MID.MP4"}


def test_make_room_skips_when_only_newer_remain(env, monkeypatch):
    rec, db = env
    _clip(rec, db, basename="NEW.MP4", ts=500, size=1)
    # Over quota forever (patched huge); importing an OLDER clip (ts=100)
    # can't evict NEW (newer) -> must skip.
    _patch_quota(monkeypatch, used_bytes=2 * (1 << 30))
    ok = ret.make_room_for(
        db, str(rec), size=1, before_ts=100,
        disk_pct=0, quota_gb=1, protect_ro=True,
    )
    assert ok is False
    assert _ids(db) == {"NEW.MP4"}  # untouched


def test_make_room_evicts_until_under_then_true(env, monkeypatch):
    rec, db = env
    _clip(rec, db, basename="A.MP4", ts=100, size=1)
    _clip(rec, db, basename="B.MP4", ts=200, size=1)
    # Start over quota; each eviction frees ~1 GiB so the cached
    # running total crosses below the 1 GiB cap. (Real files are
    # tiny; we patch the freed-bytes return to GiB scale.)
    _patch_quota(monkeypatch, used_bytes=2 * (1 << 30))
    monkeypatch.setattr(
        "web.services.retention._delete_clip_files",
        lambda row, recordings: (1 << 30, True),
    )
    ok = ret.make_room_for(
        db, str(rec), size=0, before_ts=300,
        disk_pct=0, quota_gb=1, protect_ro=True,
    )
    assert ok is True
    # 2 GiB -> evict A -> 1 GiB (still >= cap) -> evict B -> 0 (< cap) -> stop
    assert _ids(db) == set()


def test_make_room_honors_disk_pct_after_quota_satisfied(env, monkeypatch):
    # Both rules set. Quota is satisfied from the start (used patched to 0),
    # but disk_pct stays breached until BOTH clips are gone. Proves OR
    # semantics: the loop keeps evicting for the disk rule even though the
    # quota branch never fires. (Would fail if the loop exited as soon as
    # quota was satisfied.)
    import collections
    rec, db = env
    _clip(rec, db, basename="A.MP4", ts=100, size=1)
    _clip(rec, db, basename="B.MP4", ts=200, size=1)
    _patch_quota(monkeypatch, used_bytes=0)  # quota never over

    DU = collections.namedtuple("DU", "total used free")

    def fake_du(path):
        n = sum(1 for _ in rec.rglob("*.MP4"))
        used = 100 if n > 0 else 0   # any file present -> 100% (>= 90)
        return DU(total=100, used=used, free=100 - used)

    monkeypatch.setattr("web.services.retention.shutil.disk_usage", fake_du)

    ok = ret.make_room_for(
        db, str(rec), size=0, before_ts=300,
        disk_pct=90, quota_gb=1, protect_ro=True,
    )
    assert ok is True
    assert _ids(db) == set()  # evicted until disk usage dropped under 90%


# ---------------------------------------------------------------------------
# locked=1 protection — make_room_for must never evict a locked clip
# ---------------------------------------------------------------------------

def test_make_room_for_skips_locked_clip_protect_ro_false(env, monkeypatch):
    """make_room_for must not evict a locked=1 clip even with protect_ro=False."""
    rec, db = env
    locked_id = _clip(rec, db, basename="LOCKED.MP4", ts=100, size=1)
    with db.write() as c:
        c.execute("UPDATE clip_index SET locked=1 WHERE id=?", (locked_id,))

    # Always over quota so the loop would evict if it could.
    _patch_quota(monkeypatch, used_bytes=2 * (1 << 30))
    monkeypatch.setattr(
        "web.services.retention._delete_clip_files",
        lambda row, recordings: (1 << 30, True),
    )

    ok = ret.make_room_for(
        db, str(rec), size=0, before_ts=300,
        disk_pct=0, quota_gb=1, protect_ro=False,
    )
    # Locked clip is the only candidate — nothing can be evicted → False.
    assert ok is False
    assert _ids(db) == {"LOCKED.MP4"}
