"""``gone`` must mean "really off the card", and must not be terminal.

Two failure modes this pins:

1. A listing that comes back empty (a failed HTML scrape, a camera that
   answers 200 with nothing) used to mark every tracked row ``gone`` in
   one pass. The scanner already refuses to prune on an empty scan while
   the index holds rows; reconcile now does the same.

2. ``gone`` was terminal. A clip wrongly goned by a partial listing —
   the HTML directory page can lag behind the card by minutes — was
   never re-queued, because reconcile skips filenames it already knows
   and ``retry`` only touches ``failed`` rows. A clip that reappears in
   the listing is now revived to ``pending``.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from web.db import Database
from web.services import queue as q


class _Rec:
    def __init__(self, filename: str, *, filepath: str = "/DCIM/Movie",
                 size: int = 1000) -> None:
        self.filename = filename
        self.filepath = filepath
        self.size = size
        self.datetime = _dt.datetime(2026, 6, 28, 13, 34, 16)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / ".viofosync.db"))


def _seed(db: Database, filename: str, state: str, *, attempts: int = 0,
          last_error: str | None = None,
          source_dir: str = "/DCIM/Movie") -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, "
            "attempts, last_error, enqueued_at) VALUES (?,?,?,?,?,0)",
            (filename, source_dir, state, attempts, last_error),
        )


def _row(db: Database, filename: str) -> dict:
    with db.conn() as c:
        return dict(c.execute(
            "SELECT * FROM download_queue WHERE filename=?",
            (filename,)).fetchone())


A = "2026_0628_133416_104430R.MP4"
B = "2026_0628_133516_104431R.MP4"


def test_empty_listing_does_not_mark_rows_gone(db: Database):
    _seed(db, A, "pending")
    _seed(db, B, "failed", attempts=3)
    summary = q.reconcile(db, [], present_filenames=[])
    assert _row(db, A)["state"] == "pending"
    assert _row(db, B)["state"] == "failed"
    assert summary["marked_gone"] == 0
    assert summary["gone_skipped"] == 2


def test_non_empty_listing_still_marks_absent_rows_gone(db: Database):
    """The guard must not disable ordinary card rotation."""
    _seed(db, A, "pending")
    _seed(db, B, "pending")
    summary = q.reconcile(db, [_Rec(B)], present_filenames=[])
    assert _row(db, A)["state"] == "gone"
    assert _row(db, B)["state"] == "pending"
    assert summary["marked_gone"] == 1


def test_reappearing_clip_revives_a_gone_row(db: Database):
    _seed(db, A, "gone", attempts=3, last_error="404")
    summary = q.reconcile(db, [_Rec(A)], present_filenames=[])
    row = _row(db, A)
    assert row["state"] == "pending"
    assert row["attempts"] == 0
    assert row["last_error"] is None
    assert row["finished_at"] is None
    assert summary["revived"] == 1


def test_revived_row_adopts_the_fresh_source_dir(db: Database):
    """A clip locked on the camera moves to /RO/ — revive with the path
    the listing reports now, not the stale one."""
    _seed(db, A, "gone", source_dir="/DCIM/Movie")
    q.reconcile(db, [_Rec(A, filepath="/DCIM/Movie/RO")],
                present_filenames=[])
    assert _row(db, A)["source_dir"] == "/DCIM/Movie/RO"


def test_gone_row_already_on_disk_becomes_done(db: Database):
    """We have the file and the camera still lists it: the gone was
    wrong, and 'done' is the truthful state, not 'pending'."""
    _seed(db, A, "gone")
    q.reconcile(db, [_Rec(A)], present_filenames=[A])
    assert _row(db, A)["state"] == "done"
