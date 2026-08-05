"""Retention must not delete clips an export job will read.

Exports of old footage are the norm (you export *before* retention
takes it); the sweep deleting a source file mid-render fails the job
with ENOENT on the next segment. Pending/active export jobs publish a
protect-set the sweep and make_room_for must honour.
"""
from __future__ import annotations

import json
import time as _time
from collections import namedtuple
from pathlib import Path

import pytest

from web.db import Database
from web.services import retention as ret
from web.services.exporter import export_protect_ids

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


@pytest.fixture
def env(tmp_path: Path):
    rec = tmp_path / "rec"
    rec.mkdir()
    db = Database(str(rec / ".viofosync.db"))
    return rec, db


def _make_clip(rec: Path, db: Database, *, basename: str, ts: int) -> int:
    day = _time.strftime("%Y-%m-%d", _time.gmtime(ts))
    folder = rec / day
    folder.mkdir(exist_ok=True)
    path = folder / basename
    path.write_bytes(b"x" * 1024)
    with db.write() as c:
        cur = c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " event_type, size_bytes, has_gpx, scanned_at) "
            "VALUES (?, ?, ?, ?, 'F', 1, 'normal', 1024, 0, ?)",
            (str(path), basename, day, ts, int(_time.time())),
        )
        return cur.lastrowid


def _add_job(db: Database, *, state: str, payload, clip_start=None,
             clip_end=None) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO export_jobs (type, clip_ids, state, created_at, "
            "clip_start, clip_end) VALUES ('join_front', ?, ?, 0, ?, ?)",
            (json.dumps(payload), state, clip_start, clip_end),
        )


# ---- export_protect_ids payload forms ----

def test_protect_ids_dict_payload(env):
    rec, db = env
    _add_job(db, state="queued", payload={"clip_ids": [3, 7], "encoder": "software"})
    assert export_protect_ids(db) == frozenset({3, 7})


def test_protect_ids_legacy_list_payload(env):
    rec, db = env
    _add_job(db, state="running", payload=[4, 5])
    assert export_protect_ids(db) == frozenset({4, 5})


def test_protect_ids_timeline_job_uses_time_range(env):
    rec, db = env
    inside = _make_clip(rec, db, basename="IN.MP4", ts=5000)
    outside = _make_clip(rec, db, basename="OUT.MP4", ts=50_000)
    _add_job(db, state="paused", payload={"segments": [], "encoder": "software"},
             clip_start=4900, clip_end=6000)
    ids = export_protect_ids(db)
    assert inside in ids
    assert outside not in ids


def test_protect_ids_ignores_finished_jobs(env):
    rec, db = env
    _add_job(db, state="done", payload={"clip_ids": [1]})
    _add_job(db, state="failed", payload={"clip_ids": [2]})
    assert export_protect_ids(db) == frozenset()


# ---- sweep honours the protect set ----

def test_disk_pressure_skips_protected_clips(env, monkeypatch):
    rec, db = env
    protected = _make_clip(rec, db, basename="A.MP4", ts=100)   # oldest
    _make_clip(rec, db, basename="B.MP4", ts=200)
    _make_clip(rec, db, basename="C.MP4", ts=300)

    state = {"used_pct": 95}
    monkeypatch.setattr(
        "web.services.retention.shutil.disk_usage",
        lambda p: DiskUsage(100, state["used_pct"], 100 - state["used_pct"]),
    )
    orig = ret._delete_clip_files

    def wrapped(*a, **kw):
        out = orig(*a, **kw)
        state["used_pct"] -= 20
        return out

    monkeypatch.setattr(ret, "_delete_clip_files", wrapped)

    ret.sweep(
        db, str(rec), max_days=0, disk_pct=80, protect_ro=True,
        protect_ids=frozenset({protected}), _now=86400 * 365,
    )

    with db.conn() as c:
        remaining = {r["basename"] for r in
                     c.execute("SELECT basename FROM clip_index")}
    assert "A.MP4" in remaining, "sweep deleted a clip an export is reading"
    assert "B.MP4" not in remaining  # pressure satisfied by next-oldest


def test_time_rule_skips_protected_clips(env):
    rec, db = env
    now = 86400 * 365
    protected = _make_clip(rec, db, basename="OLD.MP4", ts=0)
    _make_clip(rec, db, basename="OLD2.MP4", ts=1)

    summary = ret.sweep(
        db, str(rec), max_days=30, disk_pct=0, protect_ro=True,
        protect_ids=frozenset({protected}), _now=now,
    )
    assert summary["deleted_time"] == 1
    with db.conn() as c:
        remaining = {r["basename"] for r in
                     c.execute("SELECT basename FROM clip_index")}
    assert remaining == {"OLD.MP4"}


# ---- make_room_for honours the protect set ----

def test_make_room_for_skips_protected_clips(env, monkeypatch):
    rec, db = env
    protected = _make_clip(rec, db, basename="A.MP4", ts=100)
    _make_clip(rec, db, basename="B.MP4", ts=200)

    # Quota mode: pretend each clip is 0.6 GiB so one eviction fixes
    # it; deletes must report the same fake size for the bookkeeping.
    gib = 1 << 30
    monkeypatch.setattr(
        ret, "_scan_dir_bytes",
        lambda p, exclude=frozenset():
            len(list(Path(p).rglob("*.MP4"))) * int(0.6 * gib),
    )
    orig_del = ret._delete_clip_files

    def _del(*a, **kw):
        orig_del(*a, **kw)
        return (int(0.6 * gib), True)

    monkeypatch.setattr(ret, "_delete_clip_files", _del)
    ok = ret.make_room_for(
        db, str(rec), size=1, before_ts=10_000, disk_pct=0, quota_gb=1,
        protect_ro=True, protect_ids=frozenset({protected}),
    )
    assert ok is True
    with db.conn() as c:
        remaining = {r["basename"] for r in
                     c.execute("SELECT basename FROM clip_index")}
    assert "A.MP4" in remaining
    assert "B.MP4" not in remaining
