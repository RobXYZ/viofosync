"""SyncWorker applies the ACTIVE connection's profile (scope + triage)."""
from __future__ import annotations

import datetime as _dt
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from web.db import Database
from web.services.hub import Hub
from web.services.sync_worker import SyncWorker


class _Rec:
    def __init__(self, filename: str, filepath: str) -> None:
        self.filename = filename
        self.filepath = filepath
        self.size = 1000
        self.datetime = _dt.datetime(2026, 9, 1, 12, 0, 0)


def _snap(rec: Path, **kw) -> SimpleNamespace:
    base = dict(
        address="192.168.1.230", address_fallback="car.example",
        use_html_listing=True, grouping="daily", recordings=str(rec),
        timeout=5.0, disk_critical_pct=95, gps_extract=False,
        delete_after_download=False, download_attempts=3, max_attempts=3,
        locations=(),
        primary_scope="everything", primary_gps_triage=False,
        alternative_scope="ro_only", alternative_gps_triage=True,
    )
    base.update(kw)
    s = SimpleNamespace(**base)
    s.gps_triage = bool(s.primary_gps_triage or s.alternative_gps_triage)
    return s


def _provider(snap):
    return SimpleNamespace(get=lambda: snap)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "v.db"))


def _stub_cycle_preamble(monkeypatch, sw, source: str):
    async def _noop(*a, **k):
        return None

    async def _true(*a, **k):
        return True

    async def _addr(*a, **k):
        return ("1.2.3.4", source)

    monkeypatch.setattr(sw, "_emit_disk_pct", _noop)
    monkeypatch.setattr(sw, "_check_recordings_writable", _true)
    monkeypatch.setattr(sw, "_select_active_address", _addr)
    monkeypatch.setattr(sw, "_refresh_listing_and_reconcile", _true)
    monkeypatch.setattr(sw, "_run_recording_status_pass", _noop)
    monkeypatch.setattr(sw, "_run_triage_pass", _noop)
    monkeypatch.setattr(sw, "_run_geofence_pass", _noop)


def _spy_next_pending(monkeypatch):
    captured = {}
    from web.services import queue as q

    def spy(_db, *, scope="everything", triage_gate=False, active_guard=False):
        captured.update(scope=scope, triage_gate=triage_gate)
        return None   # empty queue -> drain ends immediately

    monkeypatch.setattr(q, "next_pending", spy)
    return captured


async def test_drain_uses_alternative_profile(db, tmp_path, monkeypatch):
    sw = SyncWorker(db, _provider(_snap(tmp_path)), Hub())
    _stub_cycle_preamble(monkeypatch, sw, "alternative")
    captured = _spy_next_pending(monkeypatch)
    await sw._cycle()
    assert captured == {"scope": "ro_only", "triage_gate": True}
    assert sw.get_status()["source"] == "alternative"


async def test_drain_uses_primary_profile(db, tmp_path, monkeypatch):
    sw = SyncWorker(db, _provider(_snap(tmp_path)), Hub())
    _stub_cycle_preamble(monkeypatch, sw, "primary")
    captured = _spy_next_pending(monkeypatch)
    await sw._cycle()
    assert captured == {"scope": "everything", "triage_gate": False}
    assert sw.get_status()["source"] == "primary"


async def test_status_source_none_before_first_cycle(db, tmp_path):
    sw = SyncWorker(db, _provider(_snap(tmp_path)), Hub())
    assert sw.get_status()["source"] is None


async def test_status_source_none_when_offline(db, tmp_path, monkeypatch):
    sw = SyncWorker(db, _provider(_snap(tmp_path)), Hub())
    sw._active_source = "primary"   # stale from a previous cycle

    async def _noop(*a, **k):
        return None

    async def _true(*a, **k):
        return True

    async def _offline(*a, **k):
        return (None, "offline")

    monkeypatch.setattr(sw, "_emit_disk_pct", _noop)
    monkeypatch.setattr(sw, "_check_recordings_writable", _true)
    monkeypatch.setattr(sw, "_select_active_address", _offline)
    await sw._cycle()
    assert sw.get_status()["source"] is None


async def test_listing_is_never_filtered_by_scope(db, tmp_path):
    """Scope applies at download time; every listed clip is queued."""
    snap = _snap(tmp_path, primary_scope="ro_only")
    sw = SyncWorker(db, _provider(snap), Hub())
    listing = [_Rec("2026_0901_100000_0001F.MP4", "/DCIM/Movie"),
               _Rec("2026_0901_100100_0002F.MP4", "/DCIM/Movie/RO")]
    with patch.object(sw, "_fetch_listing", return_value=listing), \
         patch.object(sw, "_present_filenames", return_value=[]):
        await sw._refresh_listing_and_reconcile()
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM download_queue").fetchone()["n"]
    assert n == 2


def _seed_untriaged(db, fn):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, camera, event_type, state, enqueued_at) "
            "VALUES (?,?,?,?,?,?)",
            (fn, "/DCIM/Movie", "F", "normal", "pending", int(time.time())),
        )


def _count_run_pass(monkeypatch):
    calls = {"n": 0}
    from web.services import triage
    monkeypatch.setattr(
        triage, "run_pass",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or {"triaged": 1},
    )
    return calls


async def test_triage_pass_honours_active_connection(db, tmp_path, monkeypatch):
    _seed_untriaged(db, "2026_0901_100000_0001F.MP4")
    snap = _snap(tmp_path, primary_gps_triage=False, alternative_gps_triage=True)
    sw = SyncWorker(db, _provider(snap), Hub())
    sw._active_address = "1.2.3.4"
    calls = _count_run_pass(monkeypatch)
    sw._active_source = "primary"
    await sw._run_triage_pass()
    assert calls["n"] == 0
    sw._active_source = "alternative"
    await sw._run_triage_pass()
    assert calls["n"] == 1


async def test_triage_pass_defaults_to_primary_when_source_unset(db, tmp_path, monkeypatch):
    _seed_untriaged(db, "2026_0901_100000_0001F.MP4")
    snap = _snap(tmp_path, primary_gps_triage=True, alternative_gps_triage=False)
    sw = SyncWorker(db, _provider(snap), Hub())
    sw._active_address = "1.2.3.4"
    calls = _count_run_pass(monkeypatch)
    await sw._run_triage_pass()
    assert calls["n"] == 1


def test_filter_ro_only_is_gone():
    import web.services.sync_worker as m
    assert not hasattr(m, "_filter_ro_only")


async def test_mid_drain_offline_clears_source(db, tmp_path, monkeypatch):
    """A camera that vanishes between downloads leaves no active connection,
    so the queue stops reporting held rows until the next successful cycle."""
    sw = SyncWorker(db, _provider(_snap(tmp_path)), Hub())
    _stub_cycle_preamble(monkeypatch, sw, "primary")

    from web.services import queue as q
    items = iter([
        SimpleNamespace(id=1, filename="2026_0901_100000_0001F.MP4"),
        SimpleNamespace(id=2, filename="2026_0901_100100_0002F.MP4"),
    ])
    monkeypatch.setattr(
        q, "next_pending",
        lambda _db, **kw: next(items, None),
    )

    async def _download_ok(item):
        return True

    async def _probe_fails(address):
        return False

    monkeypatch.setattr(sw, "_download_one", _download_ok)
    monkeypatch.setattr(sw, "_probe_one", _probe_fails)
    await sw._cycle()
    assert sw.get_status()["source"] is None


def _stub_post_drain(monkeypatch):
    """Neutralise the post-drain archive scan + retention sweep so the test
    stays inside tmp_path (mirrors tests/test_download_failure_logging.py)."""
    import web.services.sync_worker as sw_mod
    monkeypatch.setattr(sw_mod.scanner, "scan", MagicMock())
    monkeypatch.setattr(sw_mod._retention, "sweep", MagicMock())
    monkeypatch.setattr(
        sw_mod._retention, "import_exclude_set", MagicMock(return_value=set())
    )


def _seed_scope_mix(db) -> tuple[str, str, str]:
    """One driving, one parking and one RO clip, all pending. Every row is
    ``remote_complete=1`` so next_pending's active_guard (which holds the
    newest capture until the camera reports it finalized) doesn't hide the
    RO clip."""
    rows = [
        ("2026_0901_100000_0001F.MP4", "/DCIM/Movie"),
        ("2026_0901_100100_0002PF.MP4", "/DCIM/Movie/Parking"),
        ("2026_0901_100200_0003F.MP4", "/DCIM/Movie/RO"),
    ]
    with db.write() as c:
        for i, (fn, src) in enumerate(rows, start=1):
            c.execute(
                "INSERT INTO download_queue "
                "(filename, source_dir, state, enqueued_at, remote_complete) "
                "VALUES (?,?,'pending',?,1)",
                (fn, src, i),
            )
    return tuple(fn for fn, _ in rows)


async def test_alternative_ro_only_drains_only_ro_clips(db, tmp_path, monkeypatch):
    """End to end through the real next_pending: on the ro_only alternative
    connection the drain downloads the RO clip and leaves the driving and
    parking clips pending (held, not skipped)."""
    snap = _snap(
        tmp_path,
        alternative_scope="ro_only", alternative_gps_triage=False,
        retention_max_days=0, retention_disk_pct=0, retention_protect_ro=True,
        recordings_quota_gb=0, import_path="",
    )
    sw = SyncWorker(db, _provider(snap), Hub())
    _stub_cycle_preamble(monkeypatch, sw, "alternative")
    _stub_post_drain(monkeypatch)
    drive, park, ro = _seed_scope_mix(db)

    from web.services import queue as q
    downloaded: list[str] = []

    async def _download(item):
        downloaded.append(item.filename)
        q.mark_done(db, item.id)
        return True

    async def _probe_ok(address):
        return True

    monkeypatch.setattr(sw, "_download_one", _download)
    monkeypatch.setattr(sw, "_probe_one", _probe_ok)

    await sw._cycle()

    assert downloaded == [ro]
    with db.conn() as c:
        states = {
            r["filename"]: r["state"]
            for r in c.execute(
                "SELECT filename, state FROM download_queue"
            ).fetchall()
        }
    assert states == {drive: "pending", park: "pending", ro: "done"}
