"""Pending rows outside the active scope are reported as held, never hidden."""
from __future__ import annotations

from pathlib import Path

import pytest

from web.db import Database
from web.services import queue

DAY = "2026-09-01"
DRIVE = "2026_0901_100000_0001F.MP4"
PARK = "2026_0901_100100_0002PF.MP4"
RO = "2026_0901_100200_0003F.MP4"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


def _add(db, filename, source_dir, enq, *, state="pending",
         requested_at=None, size=100):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, requested_at, remote_size) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (filename, source_dir, state, enq, requested_at, size),
        )


def _seed(db):
    _add(db, DRIVE, "/DCIM/Movie", 1)
    _add(db, PARK, "/DCIM/Movie/Parking", 2)
    _add(db, RO, "/DCIM/Movie/RO", 3)


def _by_name(items):
    return {i["filename"]: i for i in items}


def test_list_day_items_marks_held_under_ro_only(db):
    _seed(db)
    items = _by_name(queue.list_day_items(db, DAY, scope="ro_only"))
    assert items[DRIVE]["held"] == 1
    assert items[PARK]["held"] == 1
    assert items[RO]["held"] == 0
    # Still pending — held is derived, never a state.
    assert items[DRIVE]["state"] == "pending"


def test_list_day_items_no_scope_marks_nothing(db):
    _seed(db)
    items = queue.list_day_items(db, DAY)
    assert all(i["held"] == 0 for i in items)


def test_requested_row_is_not_held(db):
    _add(db, PARK, "/DCIM/Movie/Parking", 1, requested_at=1_700_000_000)
    items = queue.list_day_items(db, DAY, scope="ro_only")
    assert items[0]["held"] == 0


def test_non_pending_rows_are_never_held(db):
    _add(db, DRIVE, "/DCIM/Movie", 1, state="done")
    _add(db, PARK, "/DCIM/Movie/Parking", 2, state="skipped")
    items = queue.list_day_items(db, DAY, scope="nothing")
    assert all(i["held"] == 0 for i in items)


def test_list_page_marks_held(db):
    _seed(db)
    page = queue.list_page(db, scope="no_parking")
    items = _by_name(page["items"])
    assert items[PARK]["held"] == 1
    assert items[DRIVE]["held"] == 0


def test_list_days_splits_pending_and_held(db):
    _seed(db)
    (day,) = queue.list_days(db, scope="ro_only")
    assert day["pending_count"] == 1
    assert day["held_count"] == 2
    # Bytes still describe the whole backlog (feeds the ETA).
    assert day["pending_bytes"] == 300


def test_list_days_without_scope_has_zero_held(db):
    _seed(db)
    (day,) = queue.list_days(db)
    assert day["pending_count"] == 3
    assert day["held_count"] == 0


def test_scope_preview_counts(db):
    _seed(db)
    _add(db, "2026_0901_100300_0004F.MP4", "/DCIM/Movie", 4, state="done")
    assert queue.scope_preview(db, "ro_only") == {
        "now": 1, "held": 2, "stranded": 2,
    }
    assert queue.scope_preview(db, "everything") == {
        "now": 3, "held": 0, "stranded": 0,
    }
    assert queue.scope_preview(db, "nothing") == {
        "now": 0, "held": 3, "stranded": 3,
    }


def test_scope_preview_counts_requested_as_now(db):
    _add(db, PARK, "/DCIM/Movie/Parking", 1, requested_at=1_700_000_000)
    assert queue.scope_preview(db, "nothing") == {
        "now": 1, "held": 0, "stranded": 0,
    }


# ---- stranded: held here AND the other connection won't take it either ----


def test_stranded_when_other_scope_also_excludes(db):
    _seed(db)
    items = _by_name(
        queue.list_day_items(db, DAY, scope="ro_only", other_scope="ro_only")
    )
    assert items[DRIVE]["held"] == 1 and items[DRIVE]["stranded"] == 1
    assert items[PARK]["held"] == 1 and items[PARK]["stranded"] == 1
    assert items[RO]["held"] == 0 and items[RO]["stranded"] == 0


def test_not_stranded_when_other_scope_would_download(db):
    _seed(db)
    items = _by_name(
        queue.list_day_items(db, DAY, scope="ro_only", other_scope="everything")
    )
    assert items[DRIVE]["held"] == 1 and items[DRIVE]["stranded"] == 0
    assert items[PARK]["held"] == 1 and items[PARK]["stranded"] == 0


def test_stranded_partial_other_scope(db):
    _seed(db)
    # Other connection is no_parking: it would take the drive clip but not
    # the parking one.
    items = _by_name(
        queue.list_day_items(db, DAY, scope="nothing", other_scope="no_parking")
    )
    assert items[DRIVE]["stranded"] == 0
    assert items[PARK]["stranded"] == 1
    assert items[RO]["stranded"] == 0


def test_stranded_when_no_other_connection(db):
    _seed(db)
    # No alternative address configured → nothing else will ever take them.
    items = _by_name(queue.list_day_items(db, DAY, scope="ro_only"))
    assert items[DRIVE]["stranded"] == 1
    assert items[RO]["stranded"] == 0


def test_stranded_zero_without_active_scope(db):
    _seed(db)
    items = queue.list_day_items(db, DAY, other_scope="nothing")
    assert all(i["held"] == 0 and i["stranded"] == 0 for i in items)


def test_requested_row_is_not_stranded(db):
    _add(db, PARK, "/DCIM/Movie/Parking", 1, requested_at=1_700_000_000)
    items = queue.list_day_items(db, DAY, scope="nothing", other_scope="nothing")
    assert items[0]["held"] == 0 and items[0]["stranded"] == 0


def test_list_page_carries_stranded(db):
    _seed(db)
    page = queue.list_page(db, scope="no_parking", other_scope="ro_only")
    items = _by_name(page["items"])
    assert items[PARK]["held"] == 1 and items[PARK]["stranded"] == 1
    assert items[DRIVE]["stranded"] == 0


def test_list_days_stranded_count_is_subset_of_held(db):
    _seed(db)
    (day,) = queue.list_days(db, scope="nothing", other_scope="ro_only")
    assert day["pending_count"] == 0
    assert day["held_count"] == 3
    assert day["stranded_count"] == 2
    (day,) = queue.list_days(db, scope="nothing", other_scope="everything")
    assert day["held_count"] == 3
    assert day["stranded_count"] == 0
    (day,) = queue.list_days(db, scope="nothing")
    assert day["stranded_count"] == 3


def test_scope_preview_reports_stranded(db):
    _seed(db)
    assert queue.scope_preview(db, "ro_only", other_scope="everything") == {
        "now": 1, "held": 2, "stranded": 0,
    }
    assert queue.scope_preview(db, "ro_only", other_scope="no_parking") == {
        "now": 1, "held": 2, "stranded": 1,
    }
    # No other connection: every held clip is stranded.
    assert queue.scope_preview(db, "nothing") == {
        "now": 0, "held": 3, "stranded": 3,
    }
