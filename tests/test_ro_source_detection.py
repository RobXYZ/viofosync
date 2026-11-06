"""RO (dashcam-locked) detection from the queue's ``source_dir``.

``source_dir`` is the listing filepath verbatim: forward slashes in HTML
listing mode (``/DCIM/Movie/RO/X.MP4``), a drive letter and backslashes
in XML listing mode (``A:\\DCIM\\Movie\\RO\\X.MP4``). Both must read as RO,
in Python and in SQL alike.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from web.db import Database
from web.services import queue as q
from web.services.naming import is_ro_source, ro_source_sql
from web.services.scanner import _event_type_for

_CASES = [
    # HTML listing mode
    ("/DCIM/Movie/RO/2026_1004_181400_041404F.MP4", True),
    ("/DCIM/Movie/2026_1004_181400_041404F.MP4", False),
    ("/DCIM/Movie/Parking/2026_1004_181400_041404PF.MP4", False),
    # XML listing mode
    ("A:\\DCIM\\Movie\\RO\\2026_1004_181400_041404F.MP4", True),
    ("A:\\DCIM\\Movie\\2026_1004_181400_041404F.MP4", False),
    ("A:\\DCIM\\Movie\\Parking\\2026_1004_181400_041404PF.MP4", False),
    # directory-only and import-origin shapes
    ("/DCIM/Movie/RO", True),
    ("A:\\DCIM\\Movie\\RO", True),
    ("/import/RO/", True),
    ("/import/", False),
    ("", False),
    # a folder merely starting with RO is not the RO folder
    ("/DCIM/Movie/ROAD/X.MP4", False),
]


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / ".viofosync.db"))


def _seed(db: Database) -> dict[str, bool]:
    expected = {}
    with db.write() as c:
        for i, (source_dir, ro) in enumerate(_CASES):
            name = f"2026_1004_1814{i:02d}_0000{i:02d}F.MP4"
            c.execute(
                "INSERT INTO download_queue (filename, source_dir, state, "
                "enqueued_at) VALUES (?,?,'pending',0)", (name, source_dir),
            )
            expected[name] = ro
    return expected


@pytest.mark.parametrize("source_dir, ro", _CASES)
def test_is_ro_source(source_dir: str, ro: bool) -> None:
    assert is_ro_source(source_dir) is ro


def test_is_ro_source_none() -> None:
    assert is_ro_source(None) is False


def test_sql_twin_agrees_with_python(db: Database) -> None:
    expected = _seed(db)
    with db.conn() as c:
        got = {r["filename"]: bool(r["ro"]) for r in c.execute(
            f"SELECT filename, {ro_source_sql()} AS ro FROM download_queue"
        ).fetchall()}
    assert got == expected


def test_ro_only_scope_selects_xml_mode_ro_clips(db: Database) -> None:
    expected = _seed(db)
    with db.conn() as c:
        got = {r["filename"] for r in c.execute(
            f"SELECT dq.filename FROM download_queue dq "
            f"WHERE {q.scope_sql('ro_only', 'dq')}"
        ).fetchall()}
    assert got == {name for name, ro in expected.items() if ro}


@pytest.mark.parametrize("source_dir, ro", _CASES)
def test_scanner_event_type_follows_ro(source_dir: str, ro: bool) -> None:
    assert (_event_type_for("F", source_dir) == "ro") is ro
