"""Scanner support for compact Viofo filenames."""
from __future__ import annotations

from web.db import Database
from web.services import scanner


def test_scanner_indexes_compact_single_channel_filename(
    tmp_path,
    tmp_recordings_dir,
):
    db = Database(str(tmp_path / "test.db"))
    day_dir = tmp_recordings_dir / "2026-06-25"
    day_dir.mkdir()
    filename = "20260625171242_000086.MP4"
    (day_dir / filename).write_bytes(b"clip")

    count = scanner.scan(db, str(tmp_recordings_dir), "daily")

    assert count == 1
    with db.conn() as c:
        row = c.execute("SELECT * FROM clip_index").fetchone()
    assert row["basename"] == filename
    assert row["group_name"] == "2026-06-25"
    assert row["camera"] == "F"
    assert row["event_type"] == "normal"
    assert row["sequence"] == 86
