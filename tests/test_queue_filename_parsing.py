"""Tests for the queue's filename → camera / event-type parsers.

Viofo filenames end ``…NNNNN[PE]?[FRTI].MP4`` — the trailing
letter is the camera (F=front, R=rear, T=telephoto, I=interior)
and the optional P/E prefix encodes parking/event clips.
3-channel models pair F+R with either T or I.
"""
from __future__ import annotations

from viofosync_lib import downloaded_filename_re
from web.services.queue import (
    _camera_from_filename,
    _event_from_filename,
)


def test_compact_single_channel_filename_defaults_to_front_normal():
    filename = "20260625171242_000086.MP4"
    assert _camera_from_filename(filename) == "F"
    assert _event_from_filename(filename) == "normal"

    m = downloaded_filename_re.match(filename)
    assert m is not None
    assert m.group("year") == "2026"
    assert m.group("month") == "06"
    assert m.group("day") == "25"
    assert m.group("hour") == "17"
    assert m.group("minute") == "12"
    assert m.group("second") == "42"
    assert m.group("sequence") == "000086"
    assert m.group("camera") is None


def test_camera_front_rear():
    assert _camera_from_filename("2026_0513_172152_000120F.MP4") == "F"
    assert _camera_from_filename("2026_0513_172152_000121R.MP4") == "R"
    assert _camera_from_filename("2026_0513_172152_000122PF.MP4") == "F"
    assert _camera_from_filename("2026_0513_172152_000123ER.MP4") == "R"


def test_camera_tele():
    assert _camera_from_filename("2026_0513_172152_000124T.MP4") == "T"
    assert _camera_from_filename("2026_0513_172152_000125PT.MP4") == "T"
    assert _camera_from_filename("2026_0513_172152_000126ET.MP4") == "T"


def test_camera_interior():
    assert _camera_from_filename("2026_0109_143514_000487I.MP4") == "I"
    assert _camera_from_filename("2026_0109_145126_000520PI.MP4") == "I"
    assert _camera_from_filename("2026_0109_145126_000521EI.MP4") == "I"


def test_camera_case_insensitive():
    assert _camera_from_filename("2026_0513_172152_000124t.mp4") == "T"


def test_camera_unknown_letter_rejected():
    assert _camera_from_filename("2026_0513_172152_000124X.MP4") is None
    assert _camera_from_filename("notes.txt") is None


def test_event_type_for_third_cameras():
    assert _event_from_filename("2026_0513_172152_000124T.MP4") == "normal"
    assert _event_from_filename("2026_0513_172152_000125PT.MP4") == "parking"
    assert _event_from_filename("2026_0513_172152_000126ET.MP4") == "event"
    assert _event_from_filename("2026_0109_143514_000487I.MP4") == "normal"
    assert _event_from_filename("2026_0109_145126_000520PI.MP4") == "parking"
    assert _event_from_filename("2026_0109_145126_000521EI.MP4") == "event"
