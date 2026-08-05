"""Tests for export/originals filename derivation.

build_basename turns a set of clips + a camera label into a
sensible download stem: date + time-range + camera + clip count.
"""
from __future__ import annotations

import datetime as _dt

from web.services.naming import (
    build_basename,
    export_download_name,
    instance_slug,
    parse_clip_ids,
)


def _ts(y, mo, d, h, mi) -> int:
    # Local-time wall clock -> unix seconds, matching how the
    # app formats timestamps for the archive UI.
    return int(_dt.datetime(y, mo, d, h, mi).timestamp())


def _clip(ts: int) -> dict:
    return {"timestamp": ts}


def test_same_day_multi_clip() -> None:
    clips = [
        _clip(_ts(2024, 3, 15, 14, 30)),
        _clip(_ts(2024, 3, 15, 14, 45)),
        _clip(_ts(2024, 3, 15, 15, 2)),
    ]
    assert build_basename(clips, "front") == "2024-03-15_1430-1502_front_3clips"


def test_single_clip_collapses_range_and_singular() -> None:
    clips = [_clip(_ts(2024, 3, 15, 14, 30))]
    assert build_basename(clips, "front") == "2024-03-15_1430_front_1clip"


def test_multi_day_drops_times() -> None:
    clips = [
        _clip(_ts(2024, 3, 15, 14, 30)),
        _clip(_ts(2024, 3, 17, 9, 5)),
    ]
    assert build_basename(clips, "pip-front") == (
        "2024-03-15_to_2024-03-17_pip-front_2clips"
    )


def test_input_order_does_not_matter() -> None:
    later = _clip(_ts(2024, 3, 15, 15, 2))
    earlier = _clip(_ts(2024, 3, 15, 14, 30))
    assert build_basename([later, earlier], "rear") == (
        "2024-03-15_1430-1502_rear_2clips"
    )


def test_each_label_passes_through() -> None:
    clips = [_clip(_ts(2024, 3, 15, 14, 30))]
    for label in (
        "front", "rear", "tele", "interior",
        "pip-front", "pip-rear", "pip-tele", "pip-interior",
    ):
        assert build_basename(clips, label).endswith(f"_{label}_1clip")


def test_export_download_name_maps_type_and_adds_ext() -> None:
    clips = [
        _clip(_ts(2024, 3, 15, 14, 30)),
        _clip(_ts(2024, 3, 15, 15, 2)),
    ]
    assert export_download_name("join_front", clips, 7) == (
        "2024-03-15_1430-1502_front_2clips.mp4"
    )
    assert export_download_name("join_rear", clips, 7) == (
        "2024-03-15_1430-1502_rear_2clips.mp4"
    )
    assert export_download_name("pip", clips, 7) == (
        "2024-03-15_1430-1502_pip-front_2clips.mp4"
    )
    assert export_download_name("pip_rear", clips, 7) == (
        "2024-03-15_1430-1502_pip-rear_2clips.mp4"
    )
    assert export_download_name("join_tele", clips, 7) == (
        "2024-03-15_1430-1502_tele_2clips.mp4"
    )
    assert export_download_name("join_interior", clips, 7) == (
        "2024-03-15_1430-1502_interior_2clips.mp4"
    )
    assert export_download_name("pip_tele", clips, 7) == (
        "2024-03-15_1430-1502_pip-tele_2clips.mp4"
    )
    assert export_download_name("pip_interior", clips, 7) == (
        "2024-03-15_1430-1502_pip-interior_2clips.mp4"
    )


def test_export_download_name_falls_back_when_no_clips() -> None:
    assert export_download_name("join_front", [], 42) == (
        "viofosync_export_42.mp4"
    )


def test_export_download_name_falls_back_on_unknown_type() -> None:
    clips = [_clip(_ts(2024, 3, 15, 14, 30))]
    assert export_download_name("mystery", clips, 9) == (
        "viofosync_export_9.mp4"
    )


def test_parse_clip_ids_list_and_dict_and_garbage() -> None:
    assert parse_clip_ids('[1, 2, 3]') == [1, 2, 3]
    assert parse_clip_ids('{"clip_ids": [4, 5], "encoder": "software"}') == [4, 5]
    assert parse_clip_ids("not json") == []
    assert parse_clip_ids('{"encoder": "software"}') == []
    # Corrupt / unexpected shapes degrade to [] rather than raising.
    assert parse_clip_ids('["abc", 2]') == []
    assert parse_clip_ids("null") == []


def test_instance_slug_sanitises_for_filenames() -> None:
    assert instance_slug("viofosync") == "viofosync"
    assert instance_slug("Rob's Car") == "Rob_s_Car"
    assert instance_slug("car-a.2") == "car-a.2"
    assert instance_slug("  ") == "viofosync"       # degenerate → default
    assert instance_slug("!!!") == "viofosync"      # nothing survives
    assert instance_slug("") == "viofosync"
    assert instance_slug("ViofoSync") == "viofosync"   # default is case-insensitive
    assert instance_slug("VIOFOSYNC") == "viofosync"


def test_export_download_name_fallback_uses_instance_slug() -> None:
    assert export_download_name("join_front", [], 42, instance_name="Car A") == (
        "Car_A_export_42.mp4"
    )
    # Default keeps today's byte-identical behaviour.
    assert export_download_name("join_front", [], 42) == (
        "viofosync_export_42.mp4"
    )
