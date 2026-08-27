"""``_parse_gpx`` must detect which clock the GPS atoms used.

Which clock a camera writes into the ``gps `` atoms is
firmware-dependent: most store true GPS UTC, some store the same
TZ-adjusted wall-clock the filenames use. Hardcoding either
interpretation splits clip epochs (filename parsed as
container-local) from journey epochs by the TZ offset and breaks
clip↔journey correlation.

The fix: per sidecar, measure the offset between the GPX times and
the filename's wall-clock, snap it to the nearest 30 minutes (the
smallest real TZ granularity), and interpret the GPX times on the
filename's clock. A UTC-writing camera measures a −1h offset in BST
and keeps today's epochs bit-for-bit; a local-clock camera measures
zero and gets re-parsed in the container TZ, matching the clips.
"""
from __future__ import annotations

import datetime as _dt
import time
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import os

from web.services import gps as gps_service


@contextmanager
def _process_tz(tz: str):
    """Pin the process-local timezone (what naive ``.timestamp()``
    and ``fromtimestamp`` use) for the duration of a test."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = old
        time.tzset()


def _write_gpx(tmp_path, name: str, times: list[_dt.datetime],
               speeds: list[float] | None = None) -> str:
    """Sidecar in generate_gpx's exact format: naive wall times
    stamped ``Z`` regardless of what clock the camera really used."""
    speeds = speeds or [13.0] * len(times)
    pts = []
    for i, (t, sp) in enumerate(zip(times, speeds)):
        iso = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        pts.append(
            f'<trkpt lat="53.0" lon="{-2.0 + i * 0.0008:.6f}">'
            f"<time>{iso}</time><speed>{sp}</speed>"
            f"<course>90</course></trkpt>"
        )
    path = tmp_path / name
    path.write_text(
        '<?xml version="1.0"?><gpx version="1.0" '
        'xmlns="http://www.topografix.com/GPX/1/0">'
        "<trk><trkseg>" + "".join(pts) + "</trkseg></trk></gpx>"
    )
    return str(path)


def _epochs(points):
    return [p.t.timestamp() for p in points]


def test_utc_atom_camera_epochs_unchanged(tmp_path):
    """UTC-writing camera: filename 18:46:43 BST, atoms 17:46:43 UTC.
    The measured offset is −1h → snaps to −3600 → epochs must equal
    the plain read-as-UTC interpretation (today's behaviour)."""
    with _process_tz("Europe/London"):
        wall = [_dt.datetime(2026, 8, 15, 17, 46, 43 + i) for i in range(4)]
        p = _write_gpx(tmp_path, "2026_0815_184643_0001F.MP4.gpx", wall)
        pts = gps_service._parse_gpx(p)
    assert _epochs(pts) == [
        t.replace(tzinfo=_dt.timezone.utc).timestamp() for t in wall
    ]


def test_local_clock_camera_reparsed_in_local_tz(tmp_path):
    """Local-clock camera: GPX times share the filename's wall-clock
    (18:00:38 for file …180035…). Offset snaps to 0 → the times are
    the camera's local clock and must be parsed in the container TZ,
    landing on the same epoch basis as the clip index."""
    with _process_tz("Europe/London"):
        wall = [_dt.datetime(2026, 8, 26, 18, 0, 38 + i) for i in range(4)]
        p = _write_gpx(tmp_path, "20260826180035_000600.MP4.gpx", wall)
        pts = gps_service._parse_gpx(p)
        expected = [
            t.replace(tzinfo=ZoneInfo("Europe/London")).timestamp()
            for t in wall
        ]
    assert _epochs(pts) == expected


def test_stale_leading_fixes_do_not_skew_detection(tmp_path):
    """The chipset repeats its last-known fix until it refreshes:
    the user's real sidecars open with duplicated speed-0 points up
    to ~2 min older than the filename. Those must not drag the
    measured offset away from zero."""
    with _process_tz("Europe/London"):
        stale = _dt.datetime(2026, 8, 26, 17, 58, 22)
        moving = [_dt.datetime(2026, 8, 26, 18, 0, 38 + i) for i in range(4)]
        p = _write_gpx(
            tmp_path, "20260826180035_000600.MP4.gpx",
            [stale, stale, *moving],
            speeds=[0.0, 0.0, 1.3, 13.0, 13.0, 13.0],
        )
        pts = gps_service._parse_gpx(p)
        expected_first_moving = moving[0].replace(
            tzinfo=ZoneInfo("Europe/London")
        ).timestamp()
    assert _epochs(pts)[2] == expected_first_moving


def test_half_hour_timezone_snaps_correctly(tmp_path):
    """India (UTC+5:30): a local-clock camera's offset must snap to
    0, not to ±30 min, and epochs must land 5h30 behind the wall."""
    with _process_tz("Asia/Kolkata"):
        wall = [_dt.datetime(2026, 8, 26, 18, 0, 38 + i) for i in range(4)]
        p = _write_gpx(tmp_path, "20260826180035_000600.MP4.gpx", wall)
        pts = gps_service._parse_gpx(p)
        expected = [
            t.replace(tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()
            for t in wall
        ]
    assert _epochs(pts) == expected


def test_all_stationary_clip_still_detected(tmp_path):
    """A parking clip can be all speed-0 fixes. Detection must fall
    back to using every point rather than bailing to UTC."""
    with _process_tz("Europe/London"):
        wall = [_dt.datetime(2026, 8, 26, 18, 0, 38 + i) for i in range(4)]
        p = _write_gpx(tmp_path, "20260826180035_000600.MP4.gpx", wall,
                       speeds=[0.0, 0.0, 0.0, 0.0])
        pts = gps_service._parse_gpx(p)
        expected = [
            t.replace(tzinfo=ZoneInfo("Europe/London")).timestamp()
            for t in wall
        ]
    assert _epochs(pts) == expected


def test_non_utc_clock_logs_at_info(tmp_path, caplog):
    """A sidecar whose GPS clock is NOT UTC is the surprising case a
    support bundle needs to show — one INFO line with the offset."""
    import logging
    with _process_tz("Europe/London"):
        wall = [_dt.datetime(2026, 8, 26, 18, 0, 38 + i) for i in range(4)]
        p = _write_gpx(tmp_path, "20260826180035_000600.MP4.gpx", wall)
        with caplog.at_level(logging.INFO, logger="viofosync.gps"):
            gps_service._parse_gpx(p)
    msgs = [r.message for r in caplog.records
            if r.levelno == logging.INFO]
    assert any("not UTC" in m and "20260826180035" in m for m in msgs)


def test_utc_clock_does_not_log_at_info(tmp_path, caplog):
    """A UTC-writing camera is the norm — no INFO noise per sidecar
    (a day rebuild parses dozens of files)."""
    import logging
    with _process_tz("Europe/London"):
        wall = [_dt.datetime(2026, 8, 15, 17, 46, 43 + i) for i in range(4)]
        p = _write_gpx(tmp_path, "2026_0815_184643_0001F.MP4.gpx", wall)
        with caplog.at_level(logging.INFO, logger="viofosync.gps"):
            gps_service._parse_gpx(p)
    assert not [r for r in caplog.records if r.levelno == logging.INFO]


def test_unrecognised_filename_falls_back_to_utc(tmp_path):
    """No parseable timestamp in the sidecar name → no reference
    clock → keep the historical read-as-UTC interpretation."""
    with _process_tz("Europe/London"):
        wall = [_dt.datetime(2026, 8, 26, 18, 0, 38 + i) for i in range(4)]
        p = _write_gpx(tmp_path, "merged-day-track.gpx", wall)
        pts = gps_service._parse_gpx(p)
    assert _epochs(pts) == [
        t.replace(tzinfo=_dt.timezone.utc).timestamp() for t in wall
    ]
