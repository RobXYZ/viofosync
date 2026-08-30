# tests/test_filename_layouts.py
"""Viofo filename layouts differ per firmware in which separators appear.

Some A129 Pro units drop the ``YYYY``/``MMDD`` underscore but keep
everything else the standard layout has:

    standard   2026_0628_133416_0001PF.MP4
    A129 Pro   20260628_133416_0001PF.MP4
    compact    20260628133416_000123.MP4   (single-channel, no camera)

``downloaded_filename_re`` makes both datetime separators independently
optional, so it is the single source of truth for which names are
recordings. On-disk discovery and every filename-derived SQL expression
must agree with it rather than hard-coding one separator layout — a name
the regex accepts but discovery misses reads as "never downloaded" and
gets fetched again on every sync.
"""
from __future__ import annotations

import datetime as _dt
import time

import viofosync_lib as vfs
from web.db import Database
from web.routers import archive
from web.services import queue as q
from web.services import scanner

STANDARD = "2026_0628_133416_0001PF.MP4"
A129PRO = "20260628_133416_0001PF.MP4"
A129PRO_REAR = "20260628_133416_0003PR.MP4"
COMPACT = "20260628133416_000123.MP4"
A139PRO = "2026_0820_045542_F.MP4"          # no sequence number
A139PRO_REAR = "2026_0820_045542_R.MP4"
A139PRO_PARKING = "2026_0820_045542_PF.MP4"


# --- parsing layer -------------------------------------------------------


def test_regex_parses_date_underscore_optional():
    m = vfs.downloaded_filename_re.match(A129PRO)
    assert m is not None
    assert m.group("year") == "2026"
    assert m.group("month") == "06"
    assert m.group("day") == "28"
    assert m.group("hour") == "13"
    assert m.group("minute") == "34"
    assert m.group("second") == "16"
    assert m.group("sequence") == "0001"
    assert m.group("camera") == "PF"


def test_regex_parses_sequenceless_a139pro_names():
    # A139 Pro firmware writes no sequence number at all: the camera
    # letters follow the timestamp directly.
    m = vfs.downloaded_filename_re.match(A139PRO)
    assert m is not None
    assert m.group("year") == "2026"
    assert m.group("month") == "08"
    assert m.group("day") == "20"
    assert m.group("hour") == "04"
    assert m.group("minute") == "55"
    assert m.group("second") == "42"
    assert m.group("sequence") == ""
    assert m.group("camera") == "F"

    m = vfs.downloaded_filename_re.match(A139PRO_PARKING)
    assert m is not None
    assert m.group("sequence") == ""
    assert m.group("camera") == "PF"


def test_regex_rejects_empty_suffix_token():
    # A trailing underscore with nothing after it is not a recording.
    assert vfs.downloaded_filename_re.match("2026_0628_133416_.MP4") is None


def test_discovery_finds_every_layout_on_disk(tmp_path):
    d = tmp_path / "2026-06-28"
    d.mkdir()
    for name in (STANDARD, A129PRO, COMPACT):
        (d / name).write_bytes(b"x")
    got = vfs.get_downloaded_recordings(str(tmp_path), "daily")
    day = _dt.date(2026, 6, 28)
    assert got == {(STANDARD, day), (A129PRO, day), (COMPACT, day)}


def test_discovery_finds_a139pro_layout_on_disk(tmp_path):
    d = tmp_path / "2026-08-20"
    d.mkdir()
    for name in (A139PRO, A139PRO_REAR):
        (d / name).write_bytes(b"x")
    got = vfs.get_downloaded_recordings(str(tmp_path), "daily")
    day = _dt.date(2026, 8, 20)
    assert got == {(A139PRO, day), (A139PRO_REAR, day)}


def test_discovery_ignores_non_recordings(tmp_path):
    d = tmp_path / "2026-06-28"
    d.mkdir()
    (d / STANDARD).write_bytes(b"x")
    for junk in ("notes.MP4", "2026_0628_133416_0001PF.MP4.part",
                 "2026_0628_133416_0001PF.GPX"):
        (d / junk).write_bytes(b"x")
    got = vfs.get_downloaded_recordings(str(tmp_path), "daily")
    assert got == {(STANDARD, _dt.date(2026, 6, 28))}


def test_scanner_meta_reads_camera_and_event(tmp_path):
    d = tmp_path / "2026-06-28"
    d.mkdir()
    (d / A129PRO).write_bytes(b"x")
    meta = scanner._clip_meta_for(str(tmp_path), "daily", A129PRO, "")
    assert meta is not None
    assert meta.camera == "PF"
    assert meta.event_type == "parking"
    assert meta.group_name == "2026-06-28"
    assert meta.sequence == 1


def test_scanner_meta_handles_sequenceless_names(tmp_path):
    d = tmp_path / "2026-08-20"
    d.mkdir()
    (d / A139PRO).write_bytes(b"x")
    meta = scanner._clip_meta_for(str(tmp_path), "daily", A139PRO, "")
    assert meta is not None
    assert meta.camera == "F"
    assert meta.event_type == "normal"
    assert meta.group_name == "2026-08-20"
    assert meta.sequence == 0


# --- SQL layer -----------------------------------------------------------


def _seed(db, filename, *, state="pending", recorded_at=None,
          triaged_at=None, gps_points=None, remote_complete=1):
    now = int(time.time())
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, camera, event_type, state, enqueued_at, "
            " recorded_at, triaged_at, gps_points, remote_complete) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (filename, "/DCIM/Movie", q._camera_from_filename(filename),
             q._event_from_filename(filename), state, now,
             recorded_at, triaged_at, gps_points, remote_complete),
        )


def test_queue_day_key_not_malformed(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    ts = int(_dt.datetime(2026, 6, 28, 13, 34, 16).timestamp())
    _seed(db, A129PRO, recorded_at=ts)
    assert [d["day"] for d in q.list_days(db)] == ["2026-06-28"]
    items = q.list_day_items(db, day="2026-06-28")
    assert [it["filename"] for it in items] == [A129PRO]
    assert items[0]["kind_camera"] == "F"
    assert items[0]["kind_event"] == "parking"


def test_import_picker_regex_mirrors_the_parser():
    """The import modal filters picked files in JS (no bundler, so it
    can't import the parser). Extract its pattern and check it accepts
    exactly what ``downloaded_filename_re`` does, so the picker can't
    silently drop files the server would have imported fine."""
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    js = (repo / "web" / "static" / "app.js").read_text()
    m = re.search(r"const RE = /(?P<pat>\^\\d\{4\}.*?)/i;", js)
    assert m, "import picker regex not found in web/static/app.js"
    picker = re.compile(m.group("pat"), re.IGNORECASE)

    for name in (STANDARD, A129PRO, A129PRO_REAR, COMPACT,
                 A139PRO, A139PRO_REAR, A139PRO_PARKING,
                 "2026_0628133416_0001F.MP4", "2026_0628_133416_0001F.MP4",
                 "notes.MP4", STANDARD + ".part",
                 "2026_0628_133416_0001PF.GPX", "2026_0628_133416_.MP4"):
        assert bool(picker.match(name)) == bool(
            vfs.downloaded_filename_re.match(name)
        ), f"picker and parser disagree on {name}"


def test_open_heals_rows_the_old_parser_left_blank(tmp_path):
    """Rows queued before the layout was recognised carry a NULL camera
    and event_type, which ``remote_day_clips`` reads straight off the
    row — so they'd keep mis-pairing until the card rotated. Opening the
    DB backfills them from the filename."""
    path = str(tmp_path / "v.db")
    db = Database(path)
    now = int(time.time())
    with db.write() as c:
        for name in (A129PRO, A129PRO_REAR):
            c.execute(
                "INSERT INTO download_queue "
                "(filename, source_dir, camera, event_type, state, "
                " enqueued_at) VALUES (?,?,NULL,NULL,'pending',?)",
                (name, "/DCIM/Movie", now),
            )

    Database(path)                       # reopen runs the migration

    with db.conn() as c:
        got = {
            r["filename"]: (r["camera"], r["event_type"])
            for r in c.execute(
                "SELECT filename, camera, event_type FROM download_queue"
            )
        }
    assert got == {
        A129PRO: ("F", "parking"),
        A129PRO_REAR: ("R", "parking"),
    }


def test_queue_day_key_for_sequenceless_names(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    ts = int(_dt.datetime(2026, 8, 20, 4, 55, 42).timestamp())
    _seed(db, A139PRO, recorded_at=ts)
    assert [d["day"] for d in q.list_days(db)] == ["2026-08-20"]
    items = q.list_day_items(db, day="2026-08-20")
    assert [it["filename"] for it in items] == [A139PRO]
    assert items[0]["kind_camera"] == "F"
    assert items[0]["kind_event"] == "normal"


def test_archive_pairs_sequenceless_siblings(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    ts = int(_dt.datetime(2026, 8, 20, 4, 55, 42).timestamp())
    for name in (A139PRO, A139PRO_REAR):
        _seed(db, name, recorded_at=ts,
              triaged_at=int(time.time()), gps_points=5)
    clips = archive.remote_day_clips(db, "2026-08-20")
    assert len(clips) == 1
    assert clips[0]["front"]["basename"] == A139PRO
    assert clips[0]["rear"]["basename"] == A139PRO_REAR


def test_archive_pairs_siblings_across_sequence_numbers(tmp_path):
    # Same capture, different per-lens sequence numbers: the pair is
    # matched on the shared timestamp prefix, so both lenses land on one
    # row rather than two half-empty ones.
    db = Database(str(tmp_path / "v.db"))
    ts = int(_dt.datetime(2026, 6, 28, 13, 34, 16).timestamp())
    for name in (A129PRO, A129PRO_REAR):
        _seed(db, name, recorded_at=ts,
              triaged_at=int(time.time()), gps_points=5)
    clips = archive.remote_day_clips(db, "2026-06-28")
    assert len(clips) == 1
    assert clips[0]["front"]["basename"] == A129PRO
    assert clips[0]["rear"]["basename"] == A129PRO_REAR
