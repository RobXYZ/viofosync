# tests/test_triage_sync_worker.py
"""SyncWorker runs the triage pass only when GPS_TRIAGE is on."""
from __future__ import annotations

import time

from web.db import Database
from web.services.hub import Hub
from web.services.sync_worker import SyncWorker


class _Provider:
    def __init__(self, snap):
        self._snap = snap

    def get(self):
        return self._snap


class _Snap:
    def __init__(self, recordings, gps_triage):
        self.recordings = recordings
        # Both connections share the flag in these tests; _active_source is
        # unset so the worker resolves "primary".
        self.gps_triage = gps_triage
        self.primary_gps_triage = gps_triage
        self.alternative_gps_triage = gps_triage
        self.primary_scope = "everything"
        self.alternative_scope = "everything"
        self.timeout = 5.0


def _seed(db, fn, camera="F"):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, camera, event_type, state, enqueued_at) "
            "VALUES (?,?,?,?,?,?)",
            (fn, "/DCIM/Movie", camera, "normal", "pending", int(time.time())),
        )


async def test_pass_runs_when_enabled(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    rec.mkdir()
    _seed(db, "2026_0618_203643_0001F.MP4")
    snap = _Snap(str(rec), gps_triage=True)
    sw = SyncWorker(db, _Provider(snap), Hub())
    sw._active_address = "1.2.3.4"

    called = {"n": 0}
    from web.services import triage
    monkeypatch.setattr(
        triage, "run_pass",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"triaged": 1},
    )
    await sw._run_triage_pass()
    assert called["n"] == 1


async def test_pass_skipped_when_disabled(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    rec.mkdir()
    snap = _Snap(str(rec), gps_triage=False)
    sw = SyncWorker(db, _Provider(snap), Hub())
    sw._active_address = "1.2.3.4"
    from web.services import triage
    monkeypatch.setattr(
        triage, "run_pass",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    await sw._run_triage_pass()   # no exception = skipped


async def test_pass_skipped_when_no_active_address(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    rec.mkdir()
    snap = _Snap(str(rec), gps_triage=True)   # enabled, but no address
    sw = SyncWorker(db, _Provider(snap), Hub())
    # _active_address defaults to None; do NOT set it.
    from web.services import triage
    monkeypatch.setattr(
        triage, "run_pass",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    await sw._run_triage_pass()   # no exception = skipped at the address guard


async def test_triage_pass_clears_stale_cancel(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    rec.mkdir()
    snap = _Snap(str(rec), gps_triage=True)
    sw = SyncWorker(db, _Provider(snap), Hub())
    sw._active_address = "1.2.3.4"
    sw._cancel_current.set()   # stale cancel left over from a prior pause/skip
    from web.services import triage
    monkeypatch.setattr(triage, "run_pass", lambda *a, **k: {"triaged": 0})
    await sw._run_triage_pass()
    assert not sw._cancel_current.is_set()   # pass cleared the stale flag


async def test_triage_pass_emits_progress_and_always_clears(tmp_path, monkeypatch):
    import asyncio

    from web.services import triage as triage_module

    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    rec.mkdir()
    snap = _Snap(str(rec), gps_triage=True)
    sw = SyncWorker(db, _Provider(snap), Hub())
    sw._active_address = "1.2.3.4"
    sw.bind_loop(asyncio.get_running_loop())

    events = []

    async def _bcast(ev):
        events.append(ev)

    def _sched(loop, ev):
        events.append(ev)

    monkeypatch.setattr(sw.hub, "broadcast", _bcast)
    monkeypatch.setattr(sw.hub, "schedule_broadcast", _sched)

    def _fake_run_pass(db_, base, recordings, *, timeout, cancel_check,
                       progress_cb):
        progress_cb(0, 5, None)      # start
        progress_cb(5, 5, 0)         # progress
        return {"triaged": 5, "with_gps": 5}

    monkeypatch.setattr(triage_module, "run_pass", _fake_run_pass)

    await sw._run_triage_pass()

    tp = [e for e in events if e.get("type") == "triage_progress"]
    assert any(e.get("active") is True for e in tp)        # start/progress fired
    assert tp[-1] == {"type": "triage_progress", "active": False}  # always clears


async def test_triage_pass_clears_even_on_error(tmp_path, monkeypatch):
    import asyncio

    from web.services import triage as triage_module

    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    rec.mkdir()
    snap = _Snap(str(rec), gps_triage=True)
    sw = SyncWorker(db, _Provider(snap), Hub())
    sw._active_address = "1.2.3.4"
    sw.bind_loop(asyncio.get_running_loop())

    events = []

    async def _bcast(ev):           # hub.broadcast is a coroutine
        events.append(ev)

    monkeypatch.setattr(sw.hub, "broadcast", _bcast)
    monkeypatch.setattr(sw.hub, "schedule_broadcast", lambda loop, ev: None)

    def _boom(*a, **k):
        raise RuntimeError("triage blew up")

    monkeypatch.setattr(triage_module, "run_pass", _boom)

    await sw._run_triage_pass()   # must not raise (except-guarded)
    assert events[-1] == {"type": "triage_progress", "active": False}


async def test_drain_passes_triage_gate(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    rec.mkdir()
    snap = _Snap(str(rec), gps_triage=True)
    snap.disk_critical_pct = 95
    sw = SyncWorker(db, _Provider(snap), Hub())

    async def _noop(*a, **k):
        return None

    async def _true(*a, **k):
        return True

    async def _addr(*a, **k):
        return ("1.2.3.4", "primary")

    # Stub everything _cycle does before the drain so it reaches next_pending.
    monkeypatch.setattr(sw, "_emit_disk_pct", _noop)
    monkeypatch.setattr(sw, "_check_recordings_writable", _true)
    monkeypatch.setattr(sw, "_select_active_address", _addr)
    monkeypatch.setattr(sw, "_refresh_listing_and_reconcile", _true)
    monkeypatch.setattr(sw, "_run_recording_status_pass", _noop)
    monkeypatch.setattr(sw, "_run_triage_pass", _noop)
    monkeypatch.setattr(sw, "_run_geofence_pass", _noop)

    captured = {}
    from web.services import queue as q

    def spy(_db, *, scope="everything", triage_gate=False, active_guard=False):
        captured["scope"] = scope
        captured["triage_gate"] = triage_gate
        return None          # empty queue -> drain ends immediately

    monkeypatch.setattr(q, "next_pending", spy)

    await sw._cycle()
    assert captured == {"scope": "everything", "triage_gate": True}


def _seed_dl(db, filename, *, recorded_at, triaged_at=None, gps_points=None,
             remote_complete=None):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, camera, "
            "event_type, state, enqueued_at, recorded_at, triaged_at, "
            "gps_points, remote_complete) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (filename, "/DCIM", filename[-5], "normal", "pending",
             recorded_at, recorded_at, triaged_at, gps_points, remote_complete),
        )


def _cycle_stubs(sw, monkeypatch):
    async def _noop(*a, **k):
        return None

    async def _true(*a, **k):
        return True

    async def _addr(*a, **k):
        return ("1.2.3.4", "primary")

    async def _triaged_nothing(*a, **k):
        return 0       # a pass that made no progress (e.g. pause/resume abort)

    monkeypatch.setattr(sw, "_emit_disk_pct", _noop)
    monkeypatch.setattr(sw, "_check_recordings_writable", _true)
    monkeypatch.setattr(sw, "_select_active_address", _addr)
    monkeypatch.setattr(sw, "_refresh_listing_and_reconcile", _true)
    monkeypatch.setattr(sw, "_run_recording_status_pass", _noop)
    monkeypatch.setattr(sw, "_run_triage_pass", _triaged_nothing)
    monkeypatch.setattr(sw, "_run_geofence_pass", _noop)
    monkeypatch.setattr(sw, "_probe_one", _true)


async def test_drain_held_until_triage_complete(tmp_path, monkeypatch):
    # A settled, un-triaged clip means triage is incomplete: NOTHING should
    # download this cycle — not even a co-queued already-triaged clip. This is
    # the pause-during-triage / resume case the user hit.
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    rec.mkdir()
    snap = _Snap(str(rec), gps_triage=True)
    snap.disk_critical_pct = 95
    sw = SyncWorker(db, _Provider(snap), Hub())

    old = int(time.time()) - 10_000     # well past the settle window
    # Both clips are finalized (remote_complete=1); 0002F is finalized but not
    # yet triaged, so triage is genuinely incomplete and the whole drain must
    # hold — the pause-during-triage / resume case. (A clip that is the
    # *unconfirmed* newest capture is instead excluded from triage as
    # actively-recording; that is a different case, covered elsewhere.)
    _seed_dl(db, "2026_0618_203643_0001F.MP4",
             recorded_at=old, triaged_at=old, gps_points=5,
             remote_complete=1)   # triaged
    _seed_dl(db, "2026_0618_203644_0002F.MP4", recorded_at=old,
             remote_complete=1)   # finalized, NOT triaged

    _cycle_stubs(sw, monkeypatch)
    downloaded = []

    async def _dl(item):
        downloaded.append(item.filename)
        return True

    monkeypatch.setattr(sw, "_download_one", _dl)

    await sw._cycle()
    assert downloaded == []              # held: triage not complete


async def test_drain_runs_when_triage_complete(tmp_path, monkeypatch):
    # With every settled clip triaged AND finalized (remote_complete=1),
    # neither the triage gate nor the active-recording guard fires, so the
    # drain downloads normally.
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    rec.mkdir()
    snap = _Snap(str(rec), gps_triage=True)
    snap.disk_critical_pct = 95
    sw = SyncWorker(db, _Provider(snap), Hub())

    old = int(time.time()) - 10_000
    _seed_dl(db, "2026_0618_203643_0001F.MP4",
             recorded_at=old, triaged_at=old, gps_points=5, remote_complete=1)
    _seed_dl(db, "2026_0618_203644_0002F.MP4",
             recorded_at=old, triaged_at=old, gps_points=0, remote_complete=1)

    _cycle_stubs(sw, monkeypatch)
    from web.services import queue as q
    downloaded = []

    async def _dl(item):
        downloaded.append(item.filename)
        q.mark_done(db, item.id)     # advance state so the drain doesn't loop
        return True

    monkeypatch.setattr(sw, "_download_one", _dl)

    await sw._cycle()
    assert set(downloaded) == {
        "2026_0618_203643_0001F.MP4", "2026_0618_203644_0002F.MP4",
    }
