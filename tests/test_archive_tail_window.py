"""A confirmed shutdown tail extends the journey's padded group window, so
the trailing home-dwell stop card is absorbed and the reframed trace runs to
the camera's last fix."""
from __future__ import annotations

import datetime as _dt

from web.db import Database
from web.routers import archive
from web.services import day_tracks, geofence
from web.services import gps as gps_service
from web.settings import Place

DAY = "2026-06-18"
HOME_LAT, HOME_LON = 53.1000, -2.0000
PLACES = (Place("Home", HOME_LAT, HOME_LON, 30, True, True),)


def _epoch(h, m, s=0):
    return _dt.datetime(2026, 6, 18, h, m, s, tzinfo=_dt.UTC).timestamp()


def _iso(t):
    return _dt.datetime.fromtimestamp(t, _dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gpx(points) -> str:
    pts = "".join(
        f'<trkpt lat="{lat}" lon="{lon:.6f}">'
        f"<time>{t}</time><speed>{sp}</speed><course>90</course></trkpt>"
        for t, lat, lon, sp in points
    )
    return ('<?xml version="1.0"?><gpx version="1.0" '
            'xmlns="http://www.topografix.com/GPX/1/0"><trk><trkseg>'
            + pts + "</trkseg></trk></gpx>")


def _seed_clip(db, rec, fn, ts, gpx_points):
    daydir = rec / DAY
    daydir.mkdir(parents=True, exist_ok=True)
    (daydir / fn).write_bytes(b"x")
    (daydir / (fn + ".gpx")).write_text(_gpx(gpx_points))
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " event_type, size_bytes, has_gpx, gps_examined, scanned_at) "
            "VALUES (?,?,?,?,?,?,?,?,1,1,?)",
            (str(daydir / fn), fn, DAY, int(ts), "F", 1, "normal", 1, int(ts)))


def _seed_queue_row(db, fn, ts, event_type="normal"):
    # triaged_at=1 / gps_points=5: these rows must read as already-triaged
    # GPS-lens clips. Task 2's untriaged guard in shutdown_tails defers
    # classification (-> "undecided") while any GPS-lens row in
    # [stop_start-300, stop_start) is untriaged -- so untriaged rows here
    # would suppress the extension under test and make every dwell read as
    # undecided, which is also the real behaviour on a live install until
    # triage catches up.
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, event_type, recorded_at, "
            " triaged_at, gps_points, enqueued_at) VALUES (?,?,?,?,?,1,5,0)",
            (fn, "/DCIM/Movie", "pending", event_type, int(ts)))


def _seed_arrival_day(db, rec):
    """Drive 20:00->20:06 ending at home + 8-min dwell 20:06->20:14, camera
    silent afterwards. Queue rows mirror every minute so the tail classifier
    sees the listing. Returns (arrival_ts, last_fix_ts)."""
    ss = _epoch(20, 6)
    drive = [(_iso(_epoch(20, 0) + 60 * i), HOME_LAT,
              HOME_LON + 0.001 * (6 - i), 13) for i in range(7)]
    dwell = [(_iso(ss + 60 * i), HOME_LAT, HOME_LON, 0) for i in range(9)]
    _seed_clip(db, rec, "2026_0618_200000_0001F.MP4", _epoch(20, 0), drive)
    _seed_clip(db, rec, "2026_0618_200600_0002F.MP4", ss, dwell)
    for i in range(15):  # listing: one file per minute 20:00..20:14
        _seed_queue_row(db, f"2026_0618_20{i:02d}00_{100 + i}F.MP4",
                        _epoch(20, 0) + 60 * i)
    return ss, ss + 480


def _raw_payload(db, rec):
    """An unprocessed route payload (no group-window/trim/reframe applied
    yet) so ``_apply_group_windows`` can be invoked directly, repeatedly,
    against the SAME raw stops. Reusing an already-trimmed/reframed payload
    across two calls would feed the second call a dwell whose start_ts was
    already shifted by the first call's window, corrupting the zone-stop
    lookup -- so each call needs its own fresh raw payload."""
    gpx_paths = day_tracks.day_gpx_paths(db, str(rec), DAY)
    points, stops, journeys = gps_service.aggregate_day(gpx_paths)
    return archive._assemble_route(DAY, points, stops, journeys)


def test_tail_extends_group_window_and_absorbs_stop(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    ss, last_fix = _seed_arrival_day(db, rec)

    # build_route_payload doesn't take a `now` — it resolves through
    # shutdown_tails() to the real wall clock, which is fine here since the
    # seeded day (2026-06-18) is always in the past, but it does mean this
    # assertion depends on wall-clock time rather than being fully hermetic.
    payload = archive.build_route_payload(db, str(rec), DAY, None, PLACES)
    assert len(payload["journeys"]) == 1
    j = payload["journeys"][0]
    # Window reaches the last dwell file + nominal clip length.
    last_file = _epoch(20, 14)
    assert j["group_end_ts"] == last_file + geofence.NOMINAL_CLIP_S
    # Reframed trace/time reach the last recorded fix at home.
    assert j["end_ts"] == last_fix
    assert j["times"][-1] == last_fix
    assert abs(j["end_lat"] - HOME_LAT) < 1e-6
    # The trailing home stop card is absorbed by the extended window --
    # there is nothing left over for it to attach to.
    assert payload["stops"] == []


def test_no_zones_keeps_current_behaviour(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    ss, _ = _seed_arrival_day(db, rec)

    payload = archive.build_route_payload(db, str(rec), DAY, None)
    j = payload["journeys"][0]
    # Without exclusion zones the pad stays capped at MAX_JOURNEY_BUFFER_S
    # past the arrival (raw drive end == ss), and the dwell remains a
    # separate (trimmed) stop card instead of being absorbed.
    assert j["group_end_ts"] == ss + archive.MAX_JOURNEY_BUFFER_S
    assert any(s["start_ts"] >= ss for s in payload["stops"])


def test_tail_extension_respects_adjacent_journey_invariant(tmp_path):
    """A confirmed tail must not push group_end_ts past the next journey's
    padded start. gps.aggregate_day only splits GPS sessions on the 30-min
    SESSION_GAP_SECONDS, so a camera that restarts sooner stays in the same
    session and the next journey's raw start can sit right where the tail's
    stop ends -- an unclamped extension would run past it and overlap."""
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    ss, _ = _seed_arrival_day(db, rec)
    # Same-session restart: 21 min after the dwell's last file, well inside
    # SESSION_GAP_SECONDS (30 min). The next journey shares the dwell's stop
    # boundary (20:14) as its own raw start, so its padded start (20:12) sits
    # BEFORE the tail's unclamped target (20:14 + NOMINAL_CLIP_S = 20:15).
    t2 = _epoch(20, 35)
    drive2 = [(_iso(t2 + 60 * i), HOME_LAT,
               HOME_LON + 0.001 * (i + 1), 13) for i in range(7)]
    _seed_clip(db, rec, "2026_0618_203500_0050F.MP4", t2, drive2)
    for i in range(7):
        _seed_queue_row(db, f"2026_0618_20{35 + i:02d}00_{200 + i}F.MP4", t2 + 60 * i)

    # build_route_payload has no `now` hook -- it resolves the confirmed-tail
    # check through the real wall clock. Safe while 2026-06-18 stays in the
    # past, but this assertion is wall-clock-dependent, not fully hermetic.
    payload = archive.build_route_payload(db, str(rec), DAY, None, PLACES)
    js = sorted(payload["journeys"], key=lambda j: j["group_start_ts"])
    assert len(js) == 2
    # The clamp must cut the extension back exactly to the next journey's
    # padded start, not merely "close enough".
    assert js[0]["group_end_ts"] == _epoch(20, 12)
    assert js[1]["group_start_ts"] == _epoch(20, 12)
    assert js[0]["group_end_ts"] <= js[1]["group_start_ts"]
    # Confirm the extension actually engaged rather than silently no-op'ing:
    # the ordinary (un-extended) pad would only reach raw end (ss) + 120 s.
    assert js[0]["group_end_ts"] > _epoch(20, 8)


def test_tail_extension_unclamped_when_next_journey_is_far(tmp_path):
    """When the next journey starts a fresh GPS session (>= 30 min later),
    there is nothing to clamp against, so the tail extends fully to the last
    dwell file + NOMINAL_CLIP_S."""
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    ss, _ = _seed_arrival_day(db, rec)
    t2 = _epoch(22, 0)  # own session, well past SESSION_GAP_SECONDS
    drive2 = [(_iso(t2 + 60 * i), HOME_LAT,
               HOME_LON + 0.001 * (i + 1), 13) for i in range(7)]
    _seed_clip(db, rec, "2026_0618_220000_0050F.MP4", t2, drive2)
    for i in range(7):
        _seed_queue_row(db, f"2026_0618_22{i:02d}00_{200 + i}F.MP4", t2 + 60 * i)

    # build_route_payload has no `now` hook -- see the wall-clock note above.
    payload = archive.build_route_payload(db, str(rec), DAY, None, PLACES)
    js = sorted(payload["journeys"], key=lambda j: j["group_start_ts"])
    assert len(js) == 2
    assert js[0]["group_end_ts"] == _epoch(20, 15)
    assert js[0]["group_end_ts"] < js[1]["group_start_ts"]


def test_undecided_dwell_does_not_extend_window(tmp_path):
    """A mid-dwell read: the camera may still be recording, so
    ``shutdown_tails`` can't confirm the tail yet and the window must stay
    at the ordinary 120 s pad instead of extending."""
    db = Database(str(tmp_path / "v.db"))
    rec = tmp_path / "rec"
    ss, last_fix = _seed_arrival_day(db, rec)

    confirmed = _raw_payload(db, rec)
    # Explicit now, well past the dwell's last file -> confirmed tail. Passed
    # explicitly (rather than relying on the wall clock) since this call
    # goes through _apply_group_windows directly, which takes `now`.
    archive._apply_group_windows(db, DAY, confirmed, PLACES, now=last_fix + 3600)
    extended = confirmed["journeys"][0]["group_end_ts"]

    undecided = _raw_payload(db, rec)
    # now is only 9 min after the dwell's start -- inside
    # TAIL_GAP_S + TAIL_SLACK_S of the last file, so it may still be
    # recording and the tail can't be confirmed yet.
    archive._apply_group_windows(db, DAY, undecided, PLACES, now=ss + 9 * 60)
    unresolved = undecided["journeys"][0]["group_end_ts"]

    ordinary_pad = _raw_payload(db, rec)
    archive._apply_group_windows(db, DAY, ordinary_pad)  # no zones at all
    assert unresolved == ordinary_pad["journeys"][0]["group_end_ts"]
    assert unresolved < extended
