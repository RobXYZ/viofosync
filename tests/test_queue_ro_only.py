"""next_pending(ro_only=...) — pick only RO clips when filter is on."""
from __future__ import annotations

from pathlib import Path

import pytest

from web.db import Database
from web.services import queue


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


def _add_pending(
    db: Database, *,
    filename: str, source_dir: str, enq: int = 1,
) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at) "
            "VALUES (?, ?, 'pending', ?)",
            (filename, source_dir, enq),
        )


def test_next_pending_ro_only_skips_non_ro(db: Database) -> None:
    _add_pending(db, filename="DRV.MP4", source_dir="/DCIM/Movie", enq=1)
    _add_pending(db, filename="LOCK.MP4", source_dir="/DCIM/Movie/RO", enq=2)
    item = queue.next_pending(db, ro_only=True)
    assert item is not None
    assert item.filename == "LOCK.MP4"


def test_next_pending_ro_only_returns_none_when_no_ro(db: Database) -> None:
    _add_pending(db, filename="DRV.MP4", source_dir="/DCIM/Movie", enq=1)
    assert queue.next_pending(db, ro_only=True) is None


def test_next_pending_default_unchanged(db: Database) -> None:
    """Without ro_only, the worker still picks driving clips first
    when they're enqueued earlier."""
    _add_pending(db, filename="DRV.MP4", source_dir="/DCIM/Movie", enq=1)
    _add_pending(db, filename="LOCK.MP4", source_dir="/DCIM/Movie/RO", enq=2)
    item = queue.next_pending(db)
    assert item is not None
    assert item.filename == "DRV.MP4"


def test_claim_next_pending_marks_row_downloading(db: Database) -> None:
    _add_pending(db, filename="A.MP4", source_dir="/DCIM/Movie", enq=1)

    item = queue.claim_next_pending(db)

    assert item is not None
    assert item.filename == "A.MP4"
    assert item.state == "downloading"
    assert item.attempts == 1
    with db.conn() as c:
        row = c.execute(
            "SELECT state, attempts FROM download_queue WHERE filename=?",
            ("A.MP4",),
        ).fetchone()
    assert row["state"] == "downloading"
    assert row["attempts"] == 1


def test_claim_next_pending_does_not_reclaim_downloading_row(
    db: Database,
) -> None:
    _add_pending(db, filename="A.MP4", source_dir="/DCIM/Movie", enq=1)

    first = queue.claim_next_pending(db)
    second = queue.claim_next_pending(db)

    assert first is not None
    assert second is None
