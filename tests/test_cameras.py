"""Camera registry invariants + derivation pins.

The registry (viofosync_lib/cameras.py) is the single source of
truth; naming.py derives the export-type tables from it. These
tests pin the derived values to the exact literals that used to
be hardcoded, so a registry edit that silently changes behavior
fails here first.
"""
from __future__ import annotations

from viofosync_lib.cameras import (
    CAMERA_LETTERS,
    CAMERAS,
    CHANNEL_FOR_LETTER,
    pair_slot_of,
)
from web.services import naming
from web.services.queue import _camera_from_filename


def test_registry_invariants():
    letters = [c.letter for c in CAMERAS]
    channels = [c.channel for c in CAMERAS]
    assert len(set(letters)) == len(letters), "duplicate letter"
    assert len(set(channels)) == len(channels), "duplicate channel"
    assert all(len(c.letter) == 1 and c.letter.isupper() for c in CAMERAS)
    assert "other" not in channels, '"other" is the fallback, not a camera'


def test_letters_and_front_rear_positions():
    # Front and rear are load-bearing: front is ffmpeg input 0 /
    # audio source, and both always render in the archive grid.
    assert CAMERA_LETTERS == "FRTI"
    assert CAMERAS[0].channel == "front"
    assert CAMERAS[1].channel == "rear"


def test_channel_for_letter_matches_registry():
    assert CHANNEL_FOR_LETTER == {
        "F": "front", "R": "rear", "T": "tele", "I": "interior",
    }


def test_pair_slot_of_rear_fallback():
    # channel_of sends unknowns to "other"; the pairers' slot
    # helper keeps their historical rear fallback instead.
    assert pair_slot_of("F") == "front"
    assert pair_slot_of("PT") == "tele"
    assert pair_slot_of("X") == "rear"
    assert pair_slot_of("") == "rear"
    assert pair_slot_of(None) == "rear"


def test_every_registry_letter_is_recognised_in_a_filename():
    # Filename recognition reads the camera letter set from the registry,
    # so adding a camera there makes its clips parse without further edits.
    for letter in CAMERA_LETTERS:
        assert _camera_from_filename(
            f"2026_0628_133416_0001{letter}.MP4"
        ) == letter.upper()
    assert _camera_from_filename("2026_0628_133416_0001Z.MP4") is None


def test_js_mirror_matches_registry():
    """app.js hand-mirrors the registry (no bundler, so it can't
    import it). Extract the JS CAMERAS table and compare, so the
    two can't drift apart silently."""
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    js = (repo / "web" / "static" / "app.js").read_text()
    rows = re.findall(
        r'\{\s*letter:\s*"(\w)",\s*channel:\s*"(\w+)",\s*'
        r'label:\s*"(\w+)"\s*\}',
        js,
    )
    assert rows == [
        (c.letter, c.channel, c.label) for c in CAMERAS
    ], "web/static/app.js CAMERAS table is out of sync with the registry"


# --- Derived export-type tables pin the pre-registry literals ----


def test_export_job_types():
    assert set(naming.EXPORT_JOB_TYPES) == {
        "join_front", "join_rear", "join_tele", "join_interior",
        "pip", "pip_rear", "pip_tele", "pip_interior",
    }


def test_join_letter_for_type():
    assert naming.JOIN_LETTER_FOR_TYPE == {
        "join_front": "F",
        "join_rear": "R",
        "join_tele": "T",
        "join_interior": "I",
    }


def test_pip_partner_and_main_for_type():
    assert naming.PIP_PARTNER_FOR_TYPE == {
        "pip": "rear",
        "pip_rear": "rear",
        "pip_tele": "tele",
        "pip_interior": "interior",
    }
    assert naming.PIP_MAIN_FOR_TYPE == {
        "pip": "front",
        "pip_rear": "rear",
        "pip_tele": "tele",
        "pip_interior": "interior",
    }


def test_label_for_type():
    assert naming.LABEL_FOR_TYPE == {
        "join_front": "front",
        "join_rear": "rear",
        "join_tele": "tele",
        "join_interior": "interior",
        "pip": "pip-front",
        "pip_rear": "pip-rear",
        "pip_tele": "pip-tele",
        "pip_interior": "pip-interior",
    }
