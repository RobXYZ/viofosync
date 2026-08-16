"""Shutdown-tail classification: a bypass-parking camera keeps recording
normal clips for a few minutes after parking, then powers off. That dwell
must be kept as journey tail, not geofence-skipped."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from web.db import Database
from web.services import geofence
from web.services import queue as q
from web.settings import Place

DAY = "2026-06-18"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "v.db"))


def _epoch(h, m, s=0):
    return _dt.datetime(2026, 6, 18, h, m, s, tzinfo=_dt.UTC).timestamp()


def _seed_row(db, filename, recorded_at, event_type="normal"):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, event_type, recorded_at, enqueued_at) "
            "VALUES (?,?,?,?,?,0)",
            (filename, "/DCIM/Movie", "pending", event_type, int(recorded_at)),
        )


def _seed_listing(db, times, event_type="normal", prefix="2026_0618", cam="F"):
    """One queue row per epoch in ``times`` (filenames unique per row)."""
    for i, t in enumerate(times):
        lt = _dt.datetime.fromtimestamp(t, _dt.UTC)
        fn = f"{prefix}_{lt:%H%M%S}_{9000 + i}{cam}.MP4"
        _seed_row(db, fn, t, event_type)


def test_tail_confirmed_by_later_file_gap(db):
    ss = _epoch(20, 6)
    # Dwell files 20:06..20:14 (every 60 s), then nothing until 22:06.
    _seed_listing(db, [ss + 60 * i for i in range(9)])
    _seed_listing(db, [_epoch(22, 6)], prefix="2026_0618", cam="F")
    tails, undecided = geofence.shutdown_tails(
        db, DAY, [(ss, ss + 480)], [ss], now=_epoch(23, 0))
    assert undecided == []
    assert tails == [(ss, ss + 480 + geofence.NOMINAL_CLIP_S)]


def test_tail_confirmed_by_wall_clock(db):
    ss = _epoch(20, 6)
    _seed_listing(db, [ss + 60 * i for i in range(5)])  # last file 20:10
    now = ss + 240 + geofence.TAIL_GAP_S + geofence.TAIL_SLACK_S + 1
    tails, undecided = geofence.shutdown_tails(
        db, DAY, [(ss, ss + 240)], [ss], now=now)
    assert undecided == []
    assert tails == [(ss, ss + 240 + geofence.NOMINAL_CLIP_S)]


def test_live_dwell_is_undecided(db):
    ss = _epoch(20, 6)
    _seed_listing(db, [ss + 60 * i for i in range(5)])  # last file 20:10
    now = ss + 240 + 30  # 30 s after the newest file — could still be recording
    tails, undecided = geofence.shutdown_tails(
        db, DAY, [(ss, ss + 240)], [ss], now=now)
    assert tails == []
    assert undecided == [(ss, ss + 240 + geofence.NOMINAL_CLIP_S)]


def test_dwell_outliving_cap_is_not_a_tail(db):
    ss = _epoch(20, 6)
    # Continuous recording for 16 min after arrival, then silence.
    _seed_listing(db, [ss + 60 * i for i in range(17)])
    tails, undecided = geofence.shutdown_tails(
        db, DAY, [(ss, ss + 960)], [ss], now=_epoch(23, 0))
    assert tails == [] and undecided == []


def test_driveless_session_is_not_a_tail(db):
    ss = _epoch(20, 44)
    _seed_listing(db, [ss + 60 * i for i in range(5)])
    # No journey ends near the stop start -> not an arrival stop.
    tails, undecided = geofence.shutdown_tails(
        db, DAY, [(ss, ss + 240)], [], now=_epoch(23, 0))
    assert tails == [] and undecided == []


def test_parking_file_after_arrival_disqualifies(db):
    ss = _epoch(20, 6)
    _seed_listing(db, [ss + 60 * i for i in range(8)])            # normal dwell
    _seed_listing(db, [ss + 540], event_type="parking", cam="F")  # P file 9 min in
    # P timelapse files are ~30 min apart: the "gap" after the P file must
    # NOT read as a shutdown.
    tails, undecided = geofence.shutdown_tails(
        db, DAY, [(ss, ss + 420)], [ss], now=_epoch(23, 0))
    assert tails == [] and undecided == []


def test_recording_continuing_into_next_drive_is_not_a_tail(db):
    ss = _epoch(20, 6)
    # 10-min home visit, engine on, then straight into another drive —
    # files continue every 60 s well past the cap with no gap.
    _seed_listing(db, [ss + 60 * i for i in range(25)])
    tails, undecided = geofence.shutdown_tails(
        db, DAY, [(ss, ss + 600)], [ss], now=_epoch(23, 0))
    assert tails == [] and undecided == []


def test_empty_listing_yields_nothing(db):
    ss = _epoch(20, 6)
    tails, undecided = geofence.shutdown_tails(
        db, DAY, [(ss, ss + 240)], [ss], now=_epoch(23, 0))
    assert tails == [] and undecided == []


def test_restart_within_stop_ends_tail_at_gap(db):
    # Spec scenario 10: camera restarts minutes after shutdown while the GPS
    # stop is still one dwell — the tail must end at the pre-gap file.
    ss = _epoch(20, 6)
    _seed_listing(db, [ss + 60 * i for i in range(5)])       # 20:06..20:10
    # cam="R" (rear lens, not GPS-bearing) still counts as listing evidence —
    # ts_list is unfiltered by design, since any file proves the camera is on.
    _seed_listing(db, [_epoch(20, 20) + 60 * i for i in range(3)], cam="R")
    tails, undecided = geofence.shutdown_tails(
        db, DAY, [(ss, _epoch(20, 22))], [ss], now=_epoch(23, 0))
    assert tails == [(ss, ss + 240 + geofence.NOMINAL_CLIP_S)]
    assert undecided == []


# ---- evaluate_day integration -----------------------------------------------

HOME_LAT, HOME_LON = 53.1000, -2.0000
ZONES = (Place("Home", HOME_LAT, HOME_LON, 30, True, True),)


def _gpx(points) -> str:
    pts = "".join(
        f'<trkpt lat="{lat}" lon="{lon:.6f}">'
        f"<time>{t}</time><speed>{sp}</speed><course>0</course></trkpt>"
        for t, lat, lon, sp in points
    )
    return (
        '<?xml version="1.0"?><gpx version="1.0" '
        'xmlns="http://www.topografix.com/GPX/1/0">'
        f"<trk><trkseg>{pts}</trkseg></trk></gpx>"
    )


def _iso(t):
    return _dt.datetime.fromtimestamp(t, _dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_skeleton(rec: Path, filename: str, points) -> None:
    (rec / ".triage").mkdir(parents=True, exist_ok=True)
    (rec / ".triage" / (filename + ".gpx")).write_text(_gpx(points))


def _seed_triaged(db, filename, recorded_at, event_type="normal"):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, event_type, recorded_at, "
            " triaged_at, gps_points, enqueued_at) VALUES (?,?,?,?,?,1,5,0)",
            (filename, "/DCIM/Movie", "pending", event_type, int(recorded_at)),
        )


def _arrival_setup(db, rec):
    """A drive west->home 20:00..20:06 then a 7-min home dwell 20:06..20:13.
    Returns (dwell_filenames, arrival_ts)."""
    ss = _epoch(20, 6)
    drive = [
        (_iso(_epoch(20, 0) + 60 * i), HOME_LAT,
         HOME_LON + 0.001 * (6 - i), 13)
        for i in range(7)                       # 20:00..20:06, ends AT home
    ]
    fn_drive = "2026_0618_200000_0001F.MP4"
    _write_skeleton(rec, fn_drive, drive)
    _seed_triaged(db, fn_drive, _epoch(20, 0))

    dwell = [(_iso(ss + 60 * i), HOME_LAT, HOME_LON, 0) for i in range(8)]
    fn_d0 = "2026_0618_200600_0002F.MP4"       # carries the dwell skeleton
    _write_skeleton(rec, fn_d0, dwell)
    _seed_triaged(db, fn_d0, ss)
    dwell_fns = [fn_d0]
    for i in range(1, 8):                       # 20:07..20:13, listing only
        fn = f"2026_0618_20{6 + i:02d}00_{2 + i:04d}F.MP4"
        _seed_row(db, fn, ss + 60 * i)
        dwell_fns.append(fn)
    return dwell_fns, ss


def test_evaluate_day_keeps_confirmed_tail(db, tmp_path):
    rec = tmp_path / "rec"
    dwell_fns, ss = _arrival_setup(db, rec)
    # Recording ends at 20:13; evaluate long after -> silence proven by clock.
    skipped = geofence.evaluate_day(
        db, str(rec), DAY, ZONES, now=_epoch(23, 0))
    assert set(skipped).isdisjoint(dwell_fns)
    with db.conn() as c:
        states = {r["filename"]: r["state"] for r in
                  c.execute("SELECT filename, state FROM download_queue")}
    assert all(states[fn] == "pending" for fn in dwell_fns)


def test_evaluate_day_defers_live_dwell_then_confirms(db, tmp_path):
    rec = tmp_path / "rec"
    dwell_fns, ss = _arrival_setup(db, rec)
    # Mid-dwell sweep: newest file 60 s old -> undecided -> nothing skipped.
    skipped = geofence.evaluate_day(
        db, str(rec), DAY, ZONES, now=ss + 7 * 60 + 60)
    assert set(skipped).isdisjoint(dwell_fns)
    # Later sweep, silence now proven -> still kept (tail confirmed).
    skipped = geofence.evaluate_day(
        db, str(rec), DAY, ZONES, now=_epoch(23, 0))
    assert set(skipped).isdisjoint(dwell_fns)


def test_evaluate_day_skips_dwell_that_keeps_recording(db, tmp_path):
    rec = tmp_path / "rec"
    dwell_fns, ss = _arrival_setup(db, rec)
    # Recording continues past the cap (16 more listing-only files).
    _seed_listing(db, [ss + 60 * i for i in range(8, 24)], prefix="2026_0618")
    skipped = geofence.evaluate_day(
        db, str(rec), DAY, ZONES, now=_epoch(23, 0))
    # The dwell is ordinary idle -> its clips (inside the GPS stop window,
    # minus the journey-pad edge) are skipped. The stop's first ~2 min sit
    # inside the padded drive window, so assert on the later dwell clips.
    assert set(dwell_fns[3:]).issubset(set(skipped))


def test_evaluate_day_parking_day_unchanged(db, tmp_path):
    rec = tmp_path / "rec"
    dwell_fns, ss = _arrival_setup(db, rec)
    # Parking mode engages a minute after the last normal dwell file.
    _seed_triaged(db, "2026_0618_201400_0100PF.MP4", ss + 8 * 60,
                  event_type="parking")
    skipped = geofence.evaluate_day(
        db, str(rec), DAY, ZONES, now=_epoch(23, 0))
    assert set(dwell_fns[3:]).issubset(set(skipped))


def test_evaluate_day_tail_applies_to_non_home_zone(db, tmp_path):
    # The tail rule keys on exclude_recordings, not is_home — a "Work"
    # exclusion zone behaves identically.
    rec = tmp_path / "rec"
    dwell_fns, ss = _arrival_setup(db, rec)
    zones = (Place("Work", HOME_LAT, HOME_LON, 30, True, False),)
    skipped = geofence.evaluate_day(
        db, str(rec), DAY, zones, now=_epoch(23, 0))
    assert set(skipped).isdisjoint(dwell_fns)


def test_untriaged_drive_clips_defer_classification(db):
    # Triage is newest-first: a sweep can see the dwell before its drive has
    # skeletons. With drive clips still untriaged just before the stop, the
    # arrival match can't be trusted — classification must defer, even when
    # no journey is detectable yet (journey_ends empty).
    ss = _epoch(20, 6)
    _seed_listing(db, [ss - 120, ss - 60])            # drive tail, untriaged
    _seed_listing(db, [ss + 60 * i for i in range(5)], cam="R")
    tails, undecided = geofence.shutdown_tails(
        db, DAY, [(ss, ss + 240)], [], now=_epoch(23, 0))
    assert tails == []
    assert undecided == [(ss, ss + 240 + geofence.NOMINAL_CLIP_S)]


def test_evaluate_day_defer_then_skip_when_dwell_continues(db, tmp_path):
    # Live-dwell sweep defers; once the listing shows recording carried on
    # past the cap, a later sweep skips the dwell as ordinary idle.
    rec = tmp_path / "rec"
    dwell_fns, ss = _arrival_setup(db, rec)
    skipped = geofence.evaluate_day(
        db, str(rec), DAY, ZONES, now=ss + 7 * 60 + 60)
    assert set(skipped).isdisjoint(dwell_fns)
    _seed_listing(db, [ss + 60 * i for i in range(8, 24)], prefix="2026_0618")
    skipped = geofence.evaluate_day(
        db, str(rec), DAY, ZONES, now=_epoch(23, 0))
    assert set(dwell_fns[3:]).issubset(set(skipped))


def test_evaluate_day_defers_for_untriaged_pullin_then_confirms(db, tmp_path):
    """The real sequence the untriaged guard exists for: pull-in drive clips
    land in the listing just before the stop starts and are untriaged at
    first (triage runs newest-first, so a sweep reaches the dwell before the
    drive that led into it).

    Built on `_arrival_setup`, but with its baked-in `fn_drive` row removed:
    that single row already carries an already-triaged skeleton for the
    *whole* 20:00-20:06 drive, so on its own it already gives evaluate_day a
    complete, trustworthy arrival journey from minute one. Layering untriaged
    listing rows on top of that (the first thing tried here) changed nothing
    -- the arrival match already succeeds via fn_drive regardless of the
    guard, so the guard's own presence or absence was unobservable. Removing
    fn_drive and replacing it with two pull-in clips that are THEMSELVES
    seeded untriaged makes the guard the only thing standing between an
    incomplete journey picture and a skip -- verified below by neutering the
    guard and watching phase 1 fail (dwell clips get skipped), then restoring
    it.

    Phase 1: pull-in clips untriaged -> no journey reaches the stop yet ->
    without the guard this reads as "not a drive's arrival" (a power-on-only
    session) and the dwell gets skipped; with the guard it defers instead --
    nothing skipped. Phase 2: pull-in clips triaged -> a short journey now
    reaches the stop -> arrival confirmed -> tail confirmed -> still kept."""
    rec = tmp_path / "rec"
    dwell_fns, ss = _arrival_setup(db, rec)
    # Remove _arrival_setup's baked-in whole-drive skeleton (see docstring).
    with db.write() as c:
        c.execute(
            "DELETE FROM download_queue WHERE filename = ?",
            ("2026_0618_200000_0001F.MP4",),
        )

    # Five pull-in clips approaching home in a straight line, one per minute
    # in [ss-300, ss). Offsets (250m down to 90m) are graded closely enough
    # together that GPS's outlier filter (median-of-5, 150 m threshold)
    # doesn't drop any of them, the closest stays > STOP_RADIUS_M (50 m) from
    # home so it doesn't fold into the dwell's own stop cluster, and the
    # total path (~250 m) clears MIN_JOURNEY_DISTANCE_M (200 m) so
    # build_journeys doesn't drop it as a trivial driveway shuffle.
    offsets_deg = [
        0.0037403435839600905, 0.003141888610526476, 0.002543433637092862,
        0.0019449786636592471, 0.0013465236902256326,
    ]
    pullin = [
        (f"2026_0618_20{1 + i:02d}00_900{i}F.MP4", ss - 300 + 60 * i,
         HOME_LON + off, 8)
        for i, off in enumerate(offsets_deg)
    ]
    pullin_fns = [fn for fn, *_ in pullin]
    for fn, t, lon, sp in pullin:
        _write_skeleton(rec, fn, [(_iso(t), HOME_LAT, lon, sp)])
        _seed_row(db, fn, t)  # untriaged: no triaged_at yet

    # Phase 1: pull-in clips still untriaged -> guard defers classification.
    skipped = geofence.evaluate_day(
        db, str(rec), DAY, ZONES, now=_epoch(23, 0))
    assert set(skipped).isdisjoint(dwell_fns)
    assert set(skipped).isdisjoint(pullin_fns)

    # Phase 2: triage catches up on the pull-in clips.
    with db.write() as c:
        c.executemany(
            "UPDATE download_queue SET triaged_at=1, gps_points=1 "
            "WHERE filename=?",
            [(fn,) for fn in pullin_fns],
        )
    skipped = geofence.evaluate_day(
        db, str(rec), DAY, ZONES, now=_epoch(23, 0))
    assert set(skipped).isdisjoint(dwell_fns)  # tail confirmed, still kept


def test_sweep_all_seen_protects_tail_and_records_signature(db, tmp_path):
    """Drive the tail scenario through sweep_all's seen-cache path (not
    evaluate_day directly): a confirmed tail must survive the same-signature
    fast path, and the first sweep must record the day's signature in `seen`
    so an unchanged day isn't re-evaluated on the next tick."""
    rec = tmp_path / "rec"
    dwell_fns, ss = _arrival_setup(db, rec)
    seen: dict[str, int] = {}
    skipped = geofence.sweep_all(
        db, str(rec), ZONES, seen=seen, now=_epoch(23, 0))
    assert skipped == 0  # tail protected -> nothing auto-skipped
    with db.conn() as c:
        states = {r["filename"]: r["state"] for r in
                  c.execute("SELECT filename, state FROM download_queue")}
    assert all(states[fn] == "pending" for fn in dwell_fns)
    expected_sig = q.geofence_day_signatures(db, geofence._DETECT_STATES).get(DAY, 0)
    assert seen == {DAY: expected_sig}
    # Second sweep: signature unchanged -> day skipped without re-evaluation.
    assert geofence.sweep_all(
        db, str(rec), ZONES, seen=seen, now=_epoch(23, 0)) == 0
