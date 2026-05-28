"""Retention sweep tests — eligibility logic + integration."""
from __future__ import annotations

import time as _time
from collections import namedtuple
from pathlib import Path

import pytest

from web.db import Database
from web.services.retention import _eligible_by_time, sweep


def _clip(*, ts: int, event_type: str = "normal") -> dict:
    return {
        "id": 1,
        "path": "/x.MP4",
        "timestamp": ts,
        "event_type": event_type,
    }


def test_eligible_by_time_max_days_zero_returns_false() -> None:
    clip = _clip(ts=0)  # ancient, but max_days disabled
    ok, reason = _eligible_by_time(
        clip, now=1_000_000, max_days=0, protect_ro=True,
    )
    assert ok is False
    assert reason == "kept"


def test_eligible_by_time_old_clip_returns_true() -> None:
    clip = _clip(ts=0)
    ok, reason = _eligible_by_time(
        clip, now=86400 * 31, max_days=30, protect_ro=False,
    )
    assert ok is True
    assert reason == "time"


def test_eligible_by_time_recent_clip_returns_false() -> None:
    now = 86400 * 31
    clip = _clip(ts=now - 86400 * 5)  # 5 days old, cap is 30
    ok, reason = _eligible_by_time(
        clip, now=now, max_days=30, protect_ro=False,
    )
    assert ok is False
    assert reason == "kept"


def test_eligible_by_time_ro_protected_blocks_deletion() -> None:
    clip = _clip(ts=0, event_type="ro")
    ok, reason = _eligible_by_time(
        clip, now=86400 * 365, max_days=30, protect_ro=True,
    )
    assert ok is False
    assert reason == "ro_protected"


def test_eligible_by_time_ro_unprotected_can_be_deleted() -> None:
    clip = _clip(ts=0, event_type="ro")
    ok, reason = _eligible_by_time(
        clip, now=86400 * 365, max_days=30, protect_ro=False,
    )
    assert ok is True
    assert reason == "time"


@pytest.fixture
def env(tmp_path: Path):
    """Recordings dir + db with a clean clip_index."""
    rec = tmp_path / "rec"
    rec.mkdir()
    db = Database(str(rec / ".viofosync.db"))
    return rec, db


def _make_clip(
    rec: Path, db: Database, *,
    basename: str, ts: int, event_type: str = "normal",
    with_gpx: bool = True,
) -> int:
    """Create a fake clip on disk + clip_index row. Returns id."""
    day = _time.strftime("%Y-%m-%d", _time.gmtime(ts))
    folder = rec / day
    folder.mkdir(exist_ok=True)
    path = folder / basename
    path.write_bytes(b"x" * 1024)
    if with_gpx:
        (folder / (basename + ".gpx")).write_text("<gpx/>")
    with db.write() as c:
        cur = c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " event_type, size_bytes, has_gpx, scanned_at) "
            "VALUES (?, ?, ?, ?, 'F', 1, ?, ?, ?, ?)",
            (
                str(path), basename, day, ts, event_type,
                1024, 1 if with_gpx else 0, int(_time.time()),
            ),
        )
        return cur.lastrowid


def _index_count(db: Database) -> int:
    with db.conn() as c:
        return c.execute(
            "SELECT COUNT(*) AS n FROM clip_index"
        ).fetchone()["n"]


def test_sweep_time_deletes_old_clip(env) -> None:
    rec, db = env
    now = 86400 * 365
    _make_clip(rec, db, basename="OLD.MP4", ts=0)
    _make_clip(rec, db, basename="NEW.MP4", ts=now - 3600)
    summary = sweep(
        db, str(rec), max_days=30, disk_pct=0,
        protect_ro=True, _now=now,
    )
    assert summary["deleted_time"] == 1
    assert summary["deleted_disk"] == 0
    assert _index_count(db) == 1
    assert not (rec / "1970-01-01" / "OLD.MP4").exists()
    assert not (rec / "1970-01-01" / "OLD.MP4.gpx").exists()


def test_sweep_time_protects_ro_when_enabled(env) -> None:
    rec, db = env
    now = 86400 * 365
    _make_clip(rec, db, basename="LOCK.MP4", ts=0, event_type="ro")
    summary = sweep(
        db, str(rec), max_days=30, disk_pct=0,
        protect_ro=True, _now=now,
    )
    assert summary["deleted_time"] == 0
    assert summary["protected"] == 1
    assert _index_count(db) == 1


def test_sweep_time_max_days_zero_is_noop(env) -> None:
    rec, db = env
    _make_clip(rec, db, basename="OLD.MP4", ts=0)
    summary = sweep(
        db, str(rec), max_days=0, disk_pct=0,
        protect_ro=True, _now=86400 * 365,
    )
    assert summary["deleted_time"] == 0
    assert _index_count(db) == 1


def test_sweep_time_removes_empty_group_dir(env) -> None:
    rec, db = env
    now = 86400 * 365
    _make_clip(rec, db, basename="OLD.MP4", ts=0)
    sweep(
        db, str(rec), max_days=30, disk_pct=0,
        protect_ro=False, _now=now,
    )
    assert not (rec / "1970-01-01").exists()


DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


def test_sweep_disk_deletes_oldest_first_until_under(env, monkeypatch) -> None:
    rec, db = env
    # Three clips, oldest first.
    _make_clip(rec, db, basename="A.MP4", ts=100)
    _make_clip(rec, db, basename="B.MP4", ts=200)
    _make_clip(rec, db, basename="C.MP4", ts=300)

    # Fake disk usage: starts above threshold, drops as clips
    # disappear (one per delete in this stub).
    state = {"used_pct": 95}

    def fake_disk_usage(path):
        used = state["used_pct"]
        return DiskUsage(total=100, used=used, free=100 - used)

    def shrink_after_delete():
        state["used_pct"] -= 10

    # Patch shutil.disk_usage in the module under test.
    monkeypatch.setattr(
        "web.services.retention.shutil.disk_usage", fake_disk_usage,
    )
    # Wrap _delete_clip_files to also shrink the fake disk.
    import web.services.retention as ret
    orig = ret._delete_clip_files
    def wrapped(*a, **kw):
        out = orig(*a, **kw)
        shrink_after_delete()
        return out
    monkeypatch.setattr(ret, "_delete_clip_files", wrapped)

    summary = sweep(
        db, str(rec), max_days=0, disk_pct=80,
        protect_ro=True, _now=86400 * 365,
    )
    # 95 -> 85 -> 75 (under 80) — should stop after 2 deletes.
    assert summary["deleted_disk"] == 2
    assert _index_count(db) == 1
    # Confirm the remaining clip is the newest one.
    with db.conn() as c:
        remaining = c.execute(
            "SELECT basename FROM clip_index"
        ).fetchone()["basename"]
    assert remaining == "C.MP4"


def test_sweep_disk_below_threshold_is_noop(env, monkeypatch) -> None:
    rec, db = env
    _make_clip(rec, db, basename="A.MP4", ts=100)

    monkeypatch.setattr(
        "web.services.retention.shutil.disk_usage",
        lambda p: DiskUsage(total=100, used=50, free=50),
    )
    summary = sweep(
        db, str(rec), max_days=0, disk_pct=80,
        protect_ro=True, _now=86400 * 365,
    )
    assert summary["deleted_disk"] == 0
    assert _index_count(db) == 1


def _patch_quota_scanner(monkeypatch, half_gib: int) -> None:
    """Helper: each .MP4 on disk counts as half_gib bytes in the
    scanner, and _delete_clip_files returns half_gib so the cache
    bookkeeping stays consistent with the live scan."""
    import web.services.retention as ret
    ret._size_cache.clear()
    monkeypatch.setattr(
        ret, "_scan_dir_bytes",
        lambda p: len(list(Path(p).rglob("*.MP4"))) * half_gib,
    )
    orig_del = ret._delete_clip_files
    def del_returning(*a, **kw):
        orig_del(*a, **kw)
        return half_gib
    monkeypatch.setattr(ret, "_delete_clip_files", del_returning)


def test_sweep_quota_alone_triggers_at_absolute_gib(env, monkeypatch) -> None:
    """quota_gb set, disk_pct=0: trip on bytes-under-recordings ≥ quota."""
    rec, db = env
    half_gib = (1 << 30) // 2
    _patch_quota_scanner(monkeypatch, half_gib)
    # Filesystem looks empty — if code still consults it, nothing would
    # be deleted. Sweep must rely on the quota alone.
    monkeypatch.setattr(
        "web.services.retention.shutil.disk_usage",
        lambda p: DiskUsage(total=10**12, used=0, free=10**12),
    )

    _make_clip(rec, db, basename="A.MP4", ts=100)
    _make_clip(rec, db, basename="B.MP4", ts=200)
    _make_clip(rec, db, basename="C.MP4", ts=300)
    # 3 clips × 0.5 GiB = 1.5 GiB used. Quota = 1 GiB. Delete oldest
    # until under 1 GiB (i.e. ≤ 2 clips remaining counts at 1 GiB, still
    # equal so loop continues; 1 clip = 0.5 GiB, under, stop).
    summary = sweep(
        db, str(rec), max_days=0, disk_pct=0,
        protect_ro=True, quota_gb=1, _now=86400 * 365,
    )
    assert summary["deleted_disk"] == 2
    assert _index_count(db) == 1
    with db.conn() as c:
        remaining = c.execute(
            "SELECT basename FROM clip_index"
        ).fetchone()["basename"]
    assert remaining == "C.MP4"


def test_sweep_quota_zero_only_runs_filesystem_rule(env, monkeypatch) -> None:
    """quota_gb=0 must keep the legacy shutil.disk_usage path intact
    and not invoke any tree-scan."""
    rec, db = env
    _make_clip(rec, db, basename="A.MP4", ts=100)

    scan_calls = {"n": 0}
    import web.services.retention as ret
    def fake_scan(p):
        scan_calls["n"] += 1
        return 0
    monkeypatch.setattr(ret, "_scan_dir_bytes", fake_scan)

    monkeypatch.setattr(
        "web.services.retention.shutil.disk_usage",
        lambda p: DiskUsage(total=100, used=50, free=50),
    )
    summary = sweep(
        db, str(rec), max_days=0, disk_pct=80,
        protect_ro=True, quota_gb=0, _now=86400 * 365,
    )
    assert summary["deleted_disk"] == 0
    assert scan_calls["n"] == 0  # quota path never engaged


def test_sweep_both_rules_or_semantics_fs_pct_fires(env, monkeypatch) -> None:
    """Both rules set; only the filesystem-% rule is breached.
    Sweep must still run."""
    rec, db = env
    half_gib = (1 << 30) // 2
    _patch_quota_scanner(monkeypatch, half_gib)
    # 1 clip = 0.5 GiB used vs quota=10 GiB → quota NOT breached.
    # Filesystem fake says 99% used vs disk_pct=80 → pct IS breached.
    monkeypatch.setattr(
        "web.services.retention.shutil.disk_usage",
        lambda p: DiskUsage(total=100, used=99, free=1),
    )
    _make_clip(rec, db, basename="A.MP4", ts=100)

    summary = sweep(
        db, str(rec), max_days=0, disk_pct=80,
        protect_ro=False, quota_gb=10, _now=86400 * 365,
    )
    # Note: pct stays >= 80 forever in this fake — loop should keep
    # going until the only clip is gone (then `if not rows: break`).
    assert summary["deleted_disk"] == 1
    assert _index_count(db) == 0


def test_sweep_both_rules_or_semantics_quota_fires(env, monkeypatch) -> None:
    """Both rules set; only the quota rule is breached. Sweep must
    still run, proving the pct rule isn't gating the quota rule."""
    rec, db = env
    half_gib = (1 << 30) // 2
    _patch_quota_scanner(monkeypatch, half_gib)
    # 3 clips = 1.5 GiB used vs quota=1 GiB → quota IS breached.
    # Filesystem says 10% used vs disk_pct=80 → pct NOT breached.
    monkeypatch.setattr(
        "web.services.retention.shutil.disk_usage",
        lambda p: DiskUsage(total=100, used=10, free=90),
    )
    _make_clip(rec, db, basename="A.MP4", ts=100)
    _make_clip(rec, db, basename="B.MP4", ts=200)
    _make_clip(rec, db, basename="C.MP4", ts=300)

    summary = sweep(
        db, str(rec), max_days=0, disk_pct=80,
        protect_ro=True, quota_gb=1, _now=86400 * 365,
    )
    # 1.5 GiB → 1.0 GiB → 0.5 GiB (under quota), stop.
    assert summary["deleted_disk"] == 2
    assert _index_count(db) == 1


def test_sweep_both_rules_zero_is_disabled(env, monkeypatch) -> None:
    """Neither rule set → disk-pressure phase is skipped entirely."""
    rec, db = env
    _make_clip(rec, db, basename="A.MP4", ts=100)
    # Both stubs would scream "over threshold" if asked.
    import web.services.retention as ret
    monkeypatch.setattr(ret, "_scan_dir_bytes", lambda p: 10**20)
    monkeypatch.setattr(
        "web.services.retention.shutil.disk_usage",
        lambda p: DiskUsage(total=100, used=100, free=0),
    )
    summary = sweep(
        db, str(rec), max_days=0, disk_pct=0,
        protect_ro=True, quota_gb=0, _now=86400 * 365,
    )
    assert summary["deleted_disk"] == 0
    assert _index_count(db) == 1


def test_scan_dir_bytes_sums_recursively(tmp_path: Path) -> None:
    """The size walker must sum everything under the root, recursively."""
    import web.services.retention as ret
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"y" * 250)
    deeper = sub / "deeper"
    deeper.mkdir()
    (deeper / "c.bin").write_bytes(b"z" * 50)
    assert ret._scan_dir_bytes(str(tmp_path)) == 400


def test_cache_subtract_reflects_deletes_without_rescan(tmp_path: Path) -> None:
    """The bookkeeping cache must let the inner loop see deletes
    immediately without paying for a tree walk per file."""
    import web.services.retention as ret
    (tmp_path / "f.bin").write_bytes(b"x" * 1000)
    ret._size_cache.clear()
    # Prime cache via a fresh scan.
    assert ret._cached_used_bytes(str(tmp_path)) == 1000
    # Simulate a delete freeing 400 bytes.
    ret._cache_subtract(str(tmp_path), 400)
    # Inner check (no refresh) should see the new total.
    assert ret._cached_used_bytes(str(tmp_path)) == 600
    # A forced refresh should ignore the cache and rescan.
    assert ret._cached_used_bytes(str(tmp_path), refresh=True) == 1000


def test_sweep_disk_skips_ro_when_protected(env, monkeypatch) -> None:
    rec, db = env
    _make_clip(rec, db, basename="LOCK.MP4", ts=100, event_type="ro")
    _make_clip(rec, db, basename="DRIVE.MP4", ts=200)

    # Pretend disk stays full — the only deletable target is DRIVE.
    monkeypatch.setattr(
        "web.services.retention.shutil.disk_usage",
        lambda p: DiskUsage(total=100, used=99, free=1),
    )
    # Run with disk_pct=80 and protect_ro=True; loop should run
    # exactly once (deletes DRIVE) then bail out because the only
    # remaining row is RO and ineligible.
    summary = sweep(
        db, str(rec), max_days=0, disk_pct=80,
        protect_ro=True, _now=86400 * 365,
    )
    assert summary["deleted_disk"] == 1
    # Disk stayed full and one RO clip survived; report it.
    assert summary["protected"] == 1
    with db.conn() as c:
        remaining = c.execute(
            "SELECT basename FROM clip_index"
        ).fetchone()["basename"]
    assert remaining == "LOCK.MP4"


def test_sweep_logs_examining_header_when_rows_exist(env, caplog) -> None:
    """When there are eligible-by-time clips, sweep logs a single
    'retention sweep: N clip(s) older than D days — examining' header
    at INFO before starting deletions. Says 'examining' rather than
    'deleting' because RO-protected rows are filtered inside the
    loop, so N is the candidate count, not the eventual delete count."""
    import logging
    rec, db = env
    now = 86400 * 365
    _make_clip(rec, db, basename="A.MP4", ts=0)
    _make_clip(rec, db, basename="B.MP4", ts=100)
    _make_clip(rec, db, basename="C.MP4", ts=200)

    with caplog.at_level(logging.INFO, logger="viofosync.retention"):
        sweep(
            db, str(rec), max_days=30, disk_pct=0,
            protect_ro=False, _now=now,
        )

    headers = [
        r.message for r in caplog.records
        if "older than" in r.message and "examining" in r.message
    ]
    assert len(headers) == 1, f"expected one header log, got {headers}"
    assert "3 clip(s) older than 30 days" in headers[0]


def test_sweep_silent_when_no_eligible_clips(env, caplog) -> None:
    """Steady-state: no eligible clips, no log noise."""
    import logging
    rec, db = env
    now = 86400 * 365
    _make_clip(rec, db, basename="NEW.MP4", ts=now - 3600)

    with caplog.at_level(logging.INFO, logger="viofosync.retention"):
        sweep(
            db, str(rec), max_days=30, disk_pct=0,
            protect_ro=False, _now=now,
        )

    assert caplog.records == [], \
        f"sweep with no eligible clips must not log, got {[r.message for r in caplog.records]}"


def test_sweep_logs_progress_every_10_deletions(env, caplog) -> None:
    """A long time-phase sweep emits an INFO progress line at every
    10th deletion, formatted with running count and MB freed."""
    import logging
    rec, db = env
    now = 86400 * 365
    # 25 eligible-by-time clips. Expect progress lines at the 10th
    # and 20th deletions — two lines total.
    for i in range(25):
        _make_clip(rec, db, basename=f"OLD-{i:02d}.MP4", ts=i)

    with caplog.at_level(logging.INFO, logger="viofosync.retention"):
        sweep(
            db, str(rec), max_days=30, disk_pct=0,
            protect_ro=False, _now=now,
        )

    progress = [
        r.message for r in caplog.records
        if "/25 clip(s) deleted" in r.message
    ]
    assert len(progress) == 2, \
        f"expected two progress lines, got {progress}"
    assert progress[0].startswith("retention sweep: 10/25 clip(s) deleted")
    assert progress[1].startswith("retention sweep: 20/25 clip(s) deleted")
    assert "MB freed so far" in progress[0]
