"""Queue day grouping for compact Viofo filenames."""
from __future__ import annotations

import datetime as dt
import time

from web.db import Database
from web.services import queue


def _recorded_at() -> int:
    return int(
        dt.datetime(2026, 6, 25, 17, 12, 42, tzinfo=dt.UTC).timestamp()
    )


def test_compact_filename_uses_recorded_at_for_day_grouping(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    now = int(time.time())
    with db.write() as c:
        c.execute(
            """
            INSERT INTO download_queue (
                filename, source_dir, remote_size, recorded_at,
                camera, event_type, state, enqueued_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                "20260625171242_000086.MP4",
                "/DCIM/Movie",
                1024,
                _recorded_at(),
                "F",
                "normal",
                "pending",
                now,
            ),
        )

    days = queue.list_days(db)
    assert [d["day"] for d in days] == ["2026-06-25"]

    items = queue.list_day_items(db, "2026-06-25")
    assert len(items) == 1
    assert items[0]["kind_camera"] == "F"
    assert items[0]["kind_event"] == "normal"
