"""The group-dir pre-flight must not trust ``os.access`` (NFS lie).

On NFS/UID-mapped mounts ``os.access(W_OK)`` checks the client's cached
owner/mode against the local UID and can report a genuinely-writable
directory as unwritable, while real writes are accepted by the server's
own permission mapping (the same lie ``sync_worker._path_is_writable``
was rewritten to avoid). ``download_file`` only ran that check on a
group directory that already *exists* — the day's first download created
the directory via ``makedirs`` (no check) and succeeded, then every
later clip of the day was rejected pre-network with "Not writable":
exactly one clip per day synced, the rest silently marched to failed.

The real writability probe is the ``mkstemp`` a few lines later, which
raises an honest ``OSError`` if the directory truly isn't writable.
"""
from __future__ import annotations

import datetime as _dt
import io
import os
import urllib.request

import viofosync_lib as vfs
from viofosync_lib import _protocol

PAYLOAD = b"\x00\x00\x00\x18ftypmp42" + b"v" * 500


class _FakeResponse(io.BytesIO):
    """Context-manager response serving PAYLOAD for HEAD and GET."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def getheader(self, name, default=None):
        if name.lower() == "content-length":
            return str(len(PAYLOAD))
        return default


def _fake_urlopen(url_or_req, timeout=None):
    if isinstance(url_or_req, urllib.request.Request):
        # HEAD from get_remote_size — headers only.
        return _FakeResponse(b"")
    return _FakeResponse(PAYLOAD)


def test_existing_group_dir_with_lying_os_access_still_downloads(
    tmp_path, monkeypatch
):
    group = "2026-08-13"
    group_dir = tmp_path / group
    group_dir.mkdir()  # the day's first download already created it

    real_access = os.access

    def _lying_access(path, mode, **kw):
        if os.path.abspath(str(path)) == str(group_dir):
            return False  # what NFS UID-mapping makes os.access report
        return real_access(path, mode, **kw)

    monkeypatch.setattr(os, "access", _lying_access)
    monkeypatch.setattr(
        _protocol.urllib.request, "urlopen", _fake_urlopen
    )

    rec = vfs.Recording(
        filename="2026_0813_101500_000123F.MP4",
        filepath="A:\\DCIM\\Movie\\2026_0813_101500_000123F.MP4",
        size=len(PAYLOAD),
        timecode=None,
        datetime=_dt.datetime(2026, 8, 13, 10, 15, 0),
        attr=None,
    )

    ok, _speed = _protocol.download_file(
        "http://cam", rec, str(tmp_path), group
    )

    assert ok is True
    dest = group_dir / rec.filename
    assert dest.read_bytes() == PAYLOAD
