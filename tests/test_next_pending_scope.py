"""Per-connection scope applied in next_pending; manual requests bypass it."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from web.db import Database
from web.services import queue

# Filenames follow the Viofo pattern; the byte before the camera letter is
# the event prefix (P = parking). RO-ness comes from source_dir.
DRIVE = "2026_0901_100000_0001F.MP4"
PARK = "2026_0901_100100_0002PF.MP4"
RO_DRIVE = "2026_0901_100200_0003F.MP4"
RO_PARK = "2026_0901_100300_0004PF.MP4"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


def _add(db: Database, filename: str, source_dir: str, enq: int,
         *, state: str = "pending", requested_at: int | None = None) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, requested_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (filename, source_dir, state, enq, requested_at),
        )


def _seed_all(db: Database) -> None:
    _add(db, DRIVE, "/DCIM/Movie", 1)
    _add(db, PARK, "/DCIM/Movie/Parking", 2)
    _add(db, RO_DRIVE, "/DCIM/Movie/RO", 3)
    _add(db, RO_PARK, "/DCIM/Movie/RO/2026", 4)


def _drain(db: Database, **kw) -> list[str]:
    """Pop every in-scope pending row in order, marking each done."""
    out: list[str] = []
    while True:
        item = queue.next_pending(db, **kw)
        if item is None:
            return out
        out.append(item.filename)
        queue.mark_done(db, item.id)


def test_scope_sql_values() -> None:
    assert queue.scope_sql("everything") == "1"
    assert queue.scope_sql("nothing") == "0"
    assert "RO" in queue.scope_sql("ro_only")
    assert "'P'" in queue.scope_sql("no_parking")
    with pytest.raises(ValueError):
        queue.scope_sql("most_things")


def test_scope_sql_alias_prefixes_columns() -> None:
    s = queue.scope_sql("no_parking", alias="dq")
    assert "dq.source_dir" in s and "dq.filename" in s


def test_everything(db: Database) -> None:
    _seed_all(db)
    assert _drain(db, scope="everything") == [DRIVE, PARK, RO_DRIVE, RO_PARK]


def test_no_parking_keeps_ro_parking(db: Database) -> None:
    _seed_all(db)
    assert _drain(db, scope="no_parking") == [DRIVE, RO_DRIVE, RO_PARK]


def test_ro_only(db: Database) -> None:
    _seed_all(db)
    assert _drain(db, scope="ro_only") == [RO_DRIVE, RO_PARK]


def test_nothing(db: Database) -> None:
    _seed_all(db)
    assert queue.next_pending(db, scope="nothing") is None


def test_default_scope_is_everything(db: Database) -> None:
    _seed_all(db)
    assert queue.next_pending(db).filename == DRIVE


def test_requested_bypasses_scope(db: Database) -> None:
    _add(db, DRIVE, "/DCIM/Movie", 1, requested_at=1_700_000_000)
    _add(db, PARK, "/DCIM/Movie/Parking", 2)
    assert _drain(db, scope="nothing") == [DRIVE]


def test_download_next_sets_requested_at(db: Database) -> None:
    _add(db, PARK, "/DCIM/Movie/Parking", 1)
    queue.download_next(db, [PARK])
    with db.conn() as c:
        row = c.execute(
            "SELECT requested_at FROM download_queue WHERE filename=?",
            (PARK,),
        ).fetchone()
    assert row["requested_at"] is not None
    assert isinstance(row["requested_at"], int)


def test_skip_clears_requested_at(db: Database) -> None:
    _add(db, PARK, "/DCIM/Movie/Parking", 1, requested_at=1_700_000_000)
    queue.skip(db, [PARK])
    with db.conn() as c:
        assert c.execute(
            "SELECT requested_at FROM download_queue WHERE filename=?", (PARK,)
        ).fetchone()["requested_at"] is None


def test_geofence_skip_clears_requested_at(db: Database) -> None:
    _add(db, PARK, "/DCIM/Movie/Parking", 1, requested_at=1_700_000_000)
    queue.geofence_skip(db, [PARK])
    with db.conn() as c:
        assert c.execute(
            "SELECT requested_at FROM download_queue WHERE filename=?", (PARK,)
        ).fetchone()["requested_at"] is None


def test_imported_clip_flag_does_not_bypass_scope(db: Database) -> None:
    """``manual`` is the importer's "came from disk" flag, not a request."""
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, manual) "
            "VALUES (?, ?, 'pending', 1, 1)",
            (PARK, "/DCIM/Movie/Parking"),
        )
    assert queue.next_pending(db, scope="ro_only") is None


@pytest.mark.parametrize("state", ["skipped", "failed"])
def test_download_next_requests_non_pending_rows(db: Database, state: str) -> None:
    _add(db, PARK, "/DCIM/Movie/Parking", 1, state=state)
    queue.download_next(db, [PARK])
    with db.conn() as c:
        row = c.execute(
            "SELECT state, requested_at FROM download_queue WHERE filename=?",
            (PARK,),
        ).fetchone()
    assert row["state"] == "pending"
    assert row["requested_at"] is not None


class _Rec:
    """Minimal stand-in for a viofosync Recording in a remote listing."""

    def __init__(self, filename: str, filepath: str) -> None:
        self.filename = filename
        self.filepath = filepath
        self.size = 1000
        self.datetime = _dt.datetime(2026, 9, 1, 10, 0, 0)


def test_reconcile_revive_clears_requested_at(db: Database) -> None:
    """A clip the user once requested, that then rotated off the card and
    later reappeared, must not keep bypassing scope forever."""
    _add(db, PARK, "/DCIM/Movie/Parking", 1, state="gone",
         requested_at=1_700_000_000)
    queue.reconcile(db, [_Rec(PARK, "/DCIM/Movie/Parking")], [])
    with db.conn() as c:
        row = c.execute(
            "SELECT state, requested_at FROM download_queue WHERE filename=?",
            (PARK,),
        ).fetchone()
    assert row["state"] == "pending"
    assert row["requested_at"] is None
    assert queue.next_pending(db, scope="ro_only") is None


def test_delete_clips_clears_requested_at(db: Database, tmp_path: Path) -> None:
    _add(db, PARK, "/DCIM/Movie/Parking", 1, requested_at=1_700_000_000)
    queue.delete_clips(db, [PARK], str(tmp_path), force=True)
    with db.conn() as c:
        row = c.execute(
            "SELECT state, requested_at FROM download_queue WHERE filename=?",
            (PARK,),
        ).fetchone()
    assert row["state"] == "skipped"
    assert row["requested_at"] is None
