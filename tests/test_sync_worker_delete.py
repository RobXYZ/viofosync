"""Tests for the post-download dashcam-delete decision.

The actual SyncWorker is too tightly bound to the live event loop
to drive in unit tests, so we test the *helper* that holds the
three-guard logic. Task 4 extracts ``_should_delete_after_download``
plus a thin caller; the tests pin the logic in isolation.
"""
from __future__ import annotations

from unittest.mock import patch

from web.services.sync_worker import (
    _maybe_delete_from_dashcam,
    _should_delete_after_download,
)


class _Item:
    """Minimal QueueItem stand-in — only the fields the helper reads."""

    def __init__(self, filename: str, source_dir: str, remote_size) -> None:
        self.filename = filename
        self.source_dir = source_dir
        self.remote_size = remote_size


# ---- _should_delete_after_download ----

def test_should_delete_off_setting_returns_false() -> None:
    item = _Item("X.MP4", "/DCIM/Movie/X.MP4", 1000)
    ok, reason = _should_delete_after_download(
        item, dest_path="/tmp/X.MP4",
        delete_enabled=False,
        local_size=1000,
        local_exists=True,
    )
    assert ok is False
    assert reason == "setting_off"


def test_should_delete_ro_clip_returns_false() -> None:
    item = _Item("X.MP4", "/DCIM/Movie/RO/X.MP4", 1000)
    ok, reason = _should_delete_after_download(
        item, dest_path="/tmp/X.MP4",
        delete_enabled=True,
        local_size=1000,
        local_exists=True,
    )
    assert ok is False
    assert reason == "locked"


def test_should_delete_ro_clip_xml_listing_path_returns_false() -> None:
    """XML-mode FPATH uses backslashes; RO must still be recognised."""
    item = _Item("X.MP4", "A:\\DCIM\\Movie\\RO\\X.MP4", 1000)
    ok, reason = _should_delete_after_download(
        item, dest_path="/tmp/X.MP4",
        delete_enabled=True,
        local_size=1000,
        local_exists=True,
    )
    assert ok is False
    assert reason == "locked"


def test_should_delete_legacy_ro_directory_returns_false() -> None:
    item = _Item("X.MP4", "/DCIM/Movie/RO", 1000)
    ok, reason = _should_delete_after_download(
        item, dest_path="/tmp/X.MP4",
        delete_enabled=True,
        local_size=1000,
        local_exists=True,
    )
    assert ok is False
    assert reason == "locked"


def test_should_delete_missing_local_file_returns_false() -> None:
    item = _Item("X.MP4", "/DCIM/Movie/X.MP4", 1000)
    ok, reason = _should_delete_after_download(
        item, dest_path="/tmp/X.MP4",
        delete_enabled=True,
        local_size=0,
        local_exists=False,
    )
    assert ok is False
    assert reason == "local_missing"


def test_should_delete_size_mismatch_returns_false() -> None:
    item = _Item("X.MP4", "/DCIM/Movie/X.MP4", 1000)
    ok, reason = _should_delete_after_download(
        item, dest_path="/tmp/X.MP4",
        delete_enabled=True,
        local_size=999,
        local_exists=True,
    )
    assert ok is False
    assert reason == "size_mismatch"


def test_should_delete_all_guards_pass_returns_true() -> None:
    item = _Item("X.MP4", "/DCIM/Movie/X.MP4", 1000)
    ok, reason = _should_delete_after_download(
        item, dest_path="/tmp/X.MP4",
        delete_enabled=True,
        local_size=1000,
        local_exists=True,
    )
    assert ok is True
    assert reason == "ok"


def test_should_delete_unknown_remote_size_returns_false() -> None:
    """If we don't know the dashcam's reported size we can't verify
    the local copy. Skip rather than guess."""
    item = _Item("X.MP4", "/DCIM/Movie/X.MP4", None)
    ok, reason = _should_delete_after_download(
        item, dest_path="/tmp/X.MP4",
        delete_enabled=True,
        local_size=1000,
        local_exists=True,
    )
    assert ok is False
    assert reason == "size_mismatch"


# ---- _maybe_delete_from_dashcam ----

def test_maybe_delete_calls_helper_when_guards_pass(tmp_path) -> None:
    item = _Item("X.MP4", "/DCIM/Movie/X.MP4", 100)
    dest_path = tmp_path / "X.MP4"
    dest_path.write_bytes(b"a" * 100)
    calls = []

    def fake_delete(base_url, source_dir, filename, **kwargs):
        calls.append((base_url, source_dir, filename))
        return True

    with patch(
        "web.services.sync_worker.vfs.delete_dashcam_file",
        fake_delete,
    ):
        _maybe_delete_from_dashcam(
            item=item,
            dest_path=str(dest_path),
            delete_enabled=True,
            base_url="http://192.168.1.230",
        )
    assert calls == [
        ("http://192.168.1.230", "/DCIM/Movie/X.MP4", "X.MP4"),
    ]


def test_maybe_delete_skips_when_setting_off(tmp_path) -> None:
    item = _Item("X.MP4", "/DCIM/Movie/X.MP4", 100)
    dest_path = tmp_path / "X.MP4"
    dest_path.write_bytes(b"a" * 100)
    calls = []

    def fake_delete(*args, **kwargs):
        calls.append(args)
        return True

    with patch(
        "web.services.sync_worker.vfs.delete_dashcam_file",
        fake_delete,
    ):
        _maybe_delete_from_dashcam(
            item=item,
            dest_path=str(dest_path),
            delete_enabled=False,
            base_url="http://192.168.1.230",
        )
    assert calls == []


def test_maybe_delete_skips_ro_clip(tmp_path) -> None:
    item = _Item("X.MP4", "/DCIM/Movie/RO/X.MP4", 100)
    dest_path = tmp_path / "X.MP4"
    dest_path.write_bytes(b"a" * 100)
    calls = []

    def fake_delete(*args, **kwargs):
        calls.append(args)
        return True

    with patch(
        "web.services.sync_worker.vfs.delete_dashcam_file",
        fake_delete,
    ):
        _maybe_delete_from_dashcam(
            item=item,
            dest_path=str(dest_path),
            delete_enabled=True,
            base_url="http://192.168.1.230",
        )
    assert calls == []


def test_maybe_delete_logs_warning_on_helper_failure(tmp_path, caplog) -> None:
    import logging

    item = _Item("X.MP4", "/DCIM/Movie/X.MP4", 100)
    dest_path = tmp_path / "X.MP4"
    dest_path.write_bytes(b"a" * 100)

    def fake_delete(*args, **kwargs):
        return False

    caplog.set_level(logging.WARNING, logger="viofosync.web")
    with patch(
        "web.services.sync_worker.vfs.delete_dashcam_file",
        fake_delete,
    ):
        # Should not raise — failure is non-fatal.
        _maybe_delete_from_dashcam(
            item=item,
            dest_path=str(dest_path),
            delete_enabled=True,
            base_url="http://192.168.1.230",
        )
    assert any(
        "delete failed" in rec.getMessage().lower()
        for rec in caplog.records
    )


# ---- _refresh_queue_size ----
#
# Background: HTML directory listing reports sizes rounded to MB
# precision (e.g. "102.00 MB" → 102 * 2**20 bytes), so the queue's
# remote_size disagrees with the actual byte-precise content of the
# downloaded file. The download path itself uses HEAD to get the
# exact size and verifies it during streaming, so the on-disk file
# is the authoritative size. _refresh_queue_size brings the queue
# row in sync with reality before the size-equality delete guard
# runs.

def test_refresh_queue_size_updates_db_and_item(tmp_path) -> None:
    from pathlib import Path

    from web.db import Database
    from web.services.queue import QueueItem
    from web.services.sync_worker import _refresh_queue_size

    db = Database(str(Path(tmp_path) / "test.db"))
    with db.write() as c:
        cur = c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, remote_size, state, enqueued_at) "
            "VALUES ('X.MP4', '/DCIM/Movie', ?, 'downloading', 0)",
            (106954752,),  # 102.0 MB rounded
        )
        row_id = cur.lastrowid

    dest = Path(tmp_path) / "X.MP4"
    actual = 106937412  # the real byte-precise size
    dest.write_bytes(b"x" * actual)

    item = QueueItem(
        id=row_id, filename="X.MP4", source_dir="/DCIM/Movie",
        remote_size=106954752, recorded_at=None, camera="F",
        event_type="normal", state="downloading", priority=0,
        attempts=1, last_error=None, last_attempt_at=None,
    )
    _refresh_queue_size(db, item, str(dest))

    assert item.remote_size == actual
    with db.conn() as c:
        row = c.execute(
            "SELECT remote_size FROM download_queue WHERE id=?",
            (row_id,),
        ).fetchone()
    assert row["remote_size"] == actual


def test_refresh_queue_size_swallows_missing_file(tmp_path) -> None:
    from pathlib import Path

    from web.db import Database
    from web.services.queue import QueueItem
    from web.services.sync_worker import _refresh_queue_size

    db = Database(str(Path(tmp_path) / "test.db"))
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue "
            "(filename, source_dir, remote_size, state, enqueued_at) "
            "VALUES ('Y.MP4', '/DCIM/Movie', 1000, 'downloading', 0)",
        )

    item = QueueItem(
        id=1, filename="Y.MP4", source_dir="/DCIM/Movie",
        remote_size=1000, recorded_at=None, camera="F",
        event_type="normal", state="downloading", priority=0,
        attempts=1, last_error=None, last_attempt_at=None,
    )
    # Doesn't exist on disk.
    _refresh_queue_size(db, item, "/nonexistent/Y.MP4")
    # Untouched.
    assert item.remote_size == 1000


# ---- dashcam_delete broadcast carries sizes on mismatch ----

def test_size_mismatch_broadcast_includes_sizes(tmp_path) -> None:
    item = _Item("X.MP4", "/DCIM/Movie/X.MP4", 1000)
    dest_path = tmp_path / "X.MP4"
    dest_path.write_bytes(b"a" * 999)  # one byte short

    captured = []

    class _SinkStub:
        def dashcam_delete(self, filename, *, ok, reason,
                           local_size=None, remote_size=None):
            captured.append({
                "filename": filename, "ok": ok, "reason": reason,
                "local_size": local_size, "remote_size": remote_size,
            })

    _maybe_delete_from_dashcam(
        item=item,
        dest_path=str(dest_path),
        delete_enabled=True,
        base_url="http://x",
        sink=_SinkStub(),
    )
    assert len(captured) == 1
    ev = captured[0]
    assert ev["reason"] == "size_mismatch"
    assert ev["ok"] is False
    assert ev["local_size"] == 999
    assert ev["remote_size"] == 1000
