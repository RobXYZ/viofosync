"""Maintenance flush: un-skip geofence decisions + clear the route cache, then
re-evaluate the geofence (POST /api/archive/rebuild-grouping)."""
from __future__ import annotations

import datetime as _dt
import os
import types

from web.db import Database
from web.routers import archive
from web.services import geofence, route_cache
from web.services import queue as q
from web.settings import Place


def _seed(db, filename, state, skip_reason=None, released=None):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, "
            " skip_reason, geofence_released_at, enqueued_at) VALUES (?,?,?,?,?,0)",
            (filename, "/DCIM/Movie", state, skip_reason, released),
        )


def test_unskip_geofence_resets_only_geofence_skips(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed(db, "a_geofence.MP4", "skipped", "geofence")              # -> pending
    _seed(db, "b_user.MP4", "skipped", "user")                     # keep (user skip)
    _seed(db, "c_released.MP4", "skipped", "geofence", released=1)  # keep (released)
    _seed(db, "d_pending.MP4", "pending")                          # keep
    _seed(db, "e_done.MP4", "done")                                # keep

    n = q.unskip_geofence(db)

    assert n == 1
    with db.conn() as c:
        st = {
            r["filename"]: (r["state"], r["skip_reason"], r["geofence_released_at"])
            for r in c.execute(
                "SELECT filename, state, skip_reason, geofence_released_at "
                "FROM download_queue"
            )
        }
    # Reset to pending, skip_reason cleared, and crucially NOT marked released
    # (so the next sweep can re-skip it).
    assert st["a_geofence.MP4"] == ("pending", None, None)
    assert st["b_user.MP4"] == ("skipped", "user", None)
    assert st["c_released.MP4"][0] == "skipped"
    assert st["d_pending.MP4"][0] == "pending"
    assert st["e_done.MP4"][0] == "done"


def test_clear_all_removes_route_cache(tmp_path):
    rec = str(tmp_path / "rec")
    route_cache.store(rec, "2026-06-18", "sig", {"x": 1})
    assert os.path.exists(route_cache._cache_path(rec, "2026-06-18"))

    route_cache.clear_all(rec)
    assert not os.path.exists(route_cache._cache_path(rec, "2026-06-18"))

    route_cache.clear_all(rec)  # idempotent when already gone


def _req(db, rec, *, gps_triage=True, locations=(), worker=None):
    snap = types.SimpleNamespace(
        recordings=str(rec), gps_triage=gps_triage, locations=locations
    )
    provider = types.SimpleNamespace(get=lambda: snap)
    state = types.SimpleNamespace(db=db, settings_provider=provider)
    if worker is not None:
        state.sync_worker = worker
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


async def test_rebuild_grouping_endpoint(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    rec.mkdir()
    _seed(db, "a_geofence.MP4", "skipped", "geofence")
    route_cache.store(str(rec), "2026-06-18", "sig", {"x": 1})

    # Stub the re-sweep (it needs real skeletons/zones otherwise).
    monkeypatch.setattr(
        archive.geofence_service, "sweep_all", lambda *a, **k: 4
    )
    worker = types.SimpleNamespace(_geofence_seen={"2026-06-18": 9})
    req = _req(
        db, rec,
        locations=(Place("Home", 53.1, -2.0, 30, True),),
        worker=worker,
    )

    out = await archive.rebuild_grouping(req)

    assert out == {"ok": True, "unskipped": 1, "reskipped": 4}
    # geofence clip is back to pending
    with db.conn() as c:
        row = c.execute(
            "SELECT state FROM download_queue WHERE filename='a_geofence.MP4'"
        ).fetchone()
    assert row["state"] == "pending"
    # route cache cleared and worker's seen-cache reset
    assert not os.path.exists(route_cache._cache_path(str(rec), "2026-06-18"))
    assert worker._geofence_seen == {}


async def test_rebuild_grouping_skips_sweep_without_zones(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    rec.mkdir()
    called = {"n": 0}
    monkeypatch.setattr(
        archive.geofence_service, "sweep_all",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or 0,
    )

    out = await archive.rebuild_grouping(_req(db, rec, locations=()))

    assert out == {"ok": True, "unskipped": 0, "reskipped": 0}
    assert called["n"] == 0   # no zones -> no sweep


# ---- unskip + re-sweep rollout (the branch's "no migration needed" story) --

HOME_LAT, HOME_LON = 53.1000, -2.0000
TAIL_ZONES = (Place("Home", HOME_LAT, HOME_LON, 30, True, True),)


def _epoch(y, m, d, h, mi, s=0):
    return _dt.datetime(y, m, d, h, mi, s, tzinfo=_dt.UTC).timestamp()


def _iso(t):
    return _dt.datetime.fromtimestamp(t, _dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gpx(points):
    pts = "".join(
        f'<trkpt lat="{lat}" lon="{lon:.6f}">'
        f"<time>{t}</time><speed>{sp}</speed><course>0</course></trkpt>"
        for t, lat, lon, sp in points
    )
    return ('<?xml version="1.0"?><gpx version="1.0" '
            'xmlns="http://www.topografix.com/GPX/1/0"><trk><trkseg>'
            + pts + "</trkseg></trk></gpx>")


def _write_skeleton(rec, filename, points):
    (rec / ".triage").mkdir(parents=True, exist_ok=True)
    (rec / ".triage" / (filename + ".gpx")).write_text(_gpx(points))


def _seed_queue(db, filename, recorded_at, *, state="pending",
                 skip_reason=None, triaged_at=None, gps_points=None):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, skip_reason, event_type, "
            " recorded_at, triaged_at, gps_points, enqueued_at) "
            "VALUES (?,?,?,?,?,?,?,?,0)",
            (filename, "/DCIM/Movie", state, skip_reason, "normal",
             int(recorded_at), triaged_at, gps_points),
        )


def test_unskip_then_sweep_keeps_tail_pending_and_reskips_genuine_parking(tmp_path):
    """The endpoint (`archive.rebuild_grouping`) is awkward to drive directly
    here -- it needs real GPS skeletons plus zones for the sweep to do
    anything, which the other tests in this file stub out. So this exercises
    the rollout mechanism itself: q.unskip_geofence + geofence.sweep_all, the
    same two calls the endpoint makes, on data seeded as it would exist on an
    upgrade -- clips an OLD sweep (predating shutdown-tail protection) already
    skipped as 'geofence', with no migration applied.

    Day 1 is a shutdown tail: a drive arrives home, the camera keeps
    recording a few minutes, then stops -- but every dwell clip is already
    ``state='skipped', skip_reason='geofence'`` (the old wrong verdict). Day 2
    is a driveless dwell (camera already at home when it powered on, no
    preceding drive) -- genuinely parked, and also pre-skipped.

    After unskip_geofence resets both to pending and sweep_all re-evaluates:
    the tail's clips must end back at pending (current logic no longer skips
    them), while the genuinely-parked clips must be re-skipped (nothing about
    the fix should touch correct decisions)."""
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"

    # --- Day 1: shutdown tail, wrongly pre-skipped by the old logic.
    ss = _epoch(2026, 6, 18, 20, 6)
    drive = [(_iso(_epoch(2026, 6, 18, 20, 0) + 60 * i), HOME_LAT,
              HOME_LON + 0.001 * (6 - i), 13) for i in range(7)]
    _write_skeleton(rec, "2026_0618_200000_0001F.MP4", drive)
    _seed_queue(db, "2026_0618_200000_0001F.MP4", _epoch(2026, 6, 18, 20, 0),
                triaged_at=1, gps_points=len(drive))

    dwell = [(_iso(ss + 60 * i), HOME_LAT, HOME_LON, 0) for i in range(8)]
    _write_skeleton(rec, "2026_0618_200600_0002F.MP4", dwell)
    tail_fns = ["2026_0618_200600_0002F.MP4"]
    _seed_queue(db, "2026_0618_200600_0002F.MP4", ss, state="skipped",
                skip_reason="geofence", triaged_at=1, gps_points=len(dwell))
    for i in range(1, 8):
        fn = f"2026_0618_20{6 + i:02d}00_{2 + i:04d}F.MP4"
        _seed_queue(db, fn, ss + 60 * i, state="skipped", skip_reason="geofence")
        tail_fns.append(fn)

    # --- Day 2: driveless dwell -- no preceding journey, genuinely parked.
    ps = _epoch(2026, 6, 19, 9, 0)
    parked = [(_iso(ps + 60 * i), HOME_LAT, HOME_LON, 0) for i in range(8)]
    _write_skeleton(rec, "2026_0619_090000_0001F.MP4", parked)
    parked_fns = ["2026_0619_090000_0001F.MP4"]
    _seed_queue(db, "2026_0619_090000_0001F.MP4", ps, state="skipped",
                skip_reason="geofence", triaged_at=1, gps_points=len(parked))
    for i in range(1, 8):
        fn = f"2026_0619_09{i:02d}00_{2 + i:04d}F.MP4"
        _seed_queue(db, fn, ps + 60 * i, state="skipped", skip_reason="geofence")
        parked_fns.append(fn)

    unskipped = q.unskip_geofence(db)
    assert unskipped == len(tail_fns) + len(parked_fns)

    now = _epoch(2026, 6, 20, 0, 0)  # well past both days -> silence proven
    reskipped = geofence.sweep_all(db, str(rec), TAIL_ZONES, now=now)
    assert reskipped == len(parked_fns)

    with db.conn() as c:
        states = {r["filename"]: r["state"] for r in
                  c.execute("SELECT filename, state FROM download_queue")}
    assert all(states[fn] == "pending" for fn in tail_fns)
    assert all(states[fn] == "skipped" for fn in parked_fns)
