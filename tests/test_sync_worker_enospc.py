"""A full disk must not burn the queue's retry budget.

ENOSPC was indistinguishable from a flaky socket: each item retried
through its backoff ladder, failed, and the drain moved to the next —
methodically marking the whole queue ``failed`` while the disk stayed
full. ENOSPC now aborts retries, refunds the attempt, raises a sticky
``disk_full`` sync error, and stops the drain for this cycle.
"""
from __future__ import annotations

import datetime as _dt
import errno
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import viofosync_lib as vfs
from viofosync_lib import _protocol
from web.db import Database
from web.services import queue as q
from web.services import sync_worker as sw_mod
from web.services.hub import Hub
from web.services.sync_worker import DiskFullError, SyncWorker

# ---- protocol layer: ENOSPC short-circuits the retry ladder ----

class _ENOSPCResponse:
    def read(self, n):
        raise OSError(errno.ENOSPC, "No space left on device")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_enospc_raises_without_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(_protocol, "max_download_attempts", 3)
    monkeypatch.setattr(_protocol, "RETRY_BACKOFF", 0)
    attempts = {"n": 0}

    def fake_urlopen(url_or_req, *args, **kwargs):
        if getattr(url_or_req, "get_method", lambda: "GET")() == "HEAD":
            raise OSError("no HEAD")
        attempts["n"] += 1
        return _ENOSPCResponse()

    rec = vfs.Recording(
        "2026_0101_120000_0001F.MP4", "/DCIM/Movie/x.MP4", 1000, 0,
        _dt.datetime(2026, 1, 1, 12, 0), 0,
    )
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(OSError) as exc:
            _protocol.download_file(
                "http://192.0.2.1", rec, str(tmp_path), "",
            )
    assert exc.value.errno == errno.ENOSPC
    assert attempts["n"] == 1, \
        f"ENOSPC was retried {attempts['n']} times instead of raising"


# ---- worker layer: sticky error, attempt refunded, drain stops ----

class _Rec:
    def __init__(self, filename: str):
        self.filename = filename
        self.filepath = "/DCIM/Movie"
        self.size = 1000
        self.datetime = _dt.datetime(2026, 5, 8, 12, 0, 0)


def _snap():
    s = MagicMock()
    s.recordings = "/tmp"
    s.grouping = "daily"
    s.gps_triage = False
    s.primary_scope = "everything"
    s.primary_gps_triage = False
    s.alternative_scope = "everything"
    s.alternative_gps_triage = False
    s.gps_extract = False
    s.delete_after_download = False
    s.download_attempts = 3
    s.timeout = 5.0
    s.max_attempts = 5
    return s


@pytest.fixture
def env(tmp_path: Path):
    db = Database(str(tmp_path / "t.db"))
    provider = MagicMock()
    provider.get.return_value = _snap()
    worker = SyncWorker(db, provider, Hub())
    worker._active_address = "192.0.2.1"
    q.reconcile(db, [_Rec("A.MP4")], [])
    return db, worker


async def test_enospc_refunds_attempt_and_sets_sticky_error(env, monkeypatch):
    db, worker = env
    item = q.next_pending(db)

    def boom(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(vfs, "download_file_with", boom)

    with pytest.raises(DiskFullError):
        await worker._download_one(item)

    with db.conn() as c:
        row = c.execute(
            "SELECT state, attempts FROM download_queue WHERE id=?",
            (item.id,),
        ).fetchone()
    assert row["state"] == "pending", "item not returned to pending"
    assert row["attempts"] == 0, "full disk burned a retry attempt"
    assert worker._last_error_kind == "disk_full"


async def test_successful_download_clears_disk_full_error(env, monkeypatch):
    db, worker = env
    item = q.next_pending(db)
    worker._last_error_kind = "disk_full"

    monkeypatch.setattr(vfs, "download_file_with",
                        lambda *a, **kw: (True, "1 MB/s"))
    monkeypatch.setattr(sw_mod, "_refresh_queue_size",
                        lambda *a, **kw: None)
    monkeypatch.setattr(vfs, "get_filepath",
                        lambda *a, **kw: "/tmp/A.MP4")

    ok = await worker._download_one(item)

    assert ok is True
    assert worker._last_error_kind is None, \
        "disk_full error not cleared after space freed"
