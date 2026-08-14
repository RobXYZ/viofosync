"""The state DB must not sit silently on a network filesystem.

SQLite's WAL mode needs shared memory and dependable POSIX locking;
NFS/CIFS provide neither, so the failure modes are "database is
locked", "disk I/O error" and corruption. Nothing stops a user
bind-mounting CONFIG_DIR onto the NAS, so startup says so out loud.
"""
from __future__ import annotations

import logging

from web import db as db_mod
from web import fsinfo


def _mounts(tmp_path, body: str) -> str:
    p = tmp_path / "mounts"
    p.write_text(body)
    return str(p)


def test_warns_when_db_is_on_nfs(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(fsinfo, "_PROC_MOUNTS", _mounts(
        tmp_path, "nas:/vol/apps /config nfs4 rw 0 0\n"))
    with caplog.at_level(logging.WARNING, logger="viofosync.db"):
        fstype = db_mod.warn_if_network_volume("/config/viofosync.db")
    assert fstype == "nfs4"
    assert "CONFIG_DIR" in caplog.text
    assert "nfs4" in caplog.text


def test_silent_on_local_storage(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(fsinfo, "_PROC_MOUNTS", _mounts(
        tmp_path, "/dev/sda1 /config ext4 rw 0 0\n"))
    with caplog.at_level(logging.WARNING, logger="viofosync.db"):
        assert db_mod.warn_if_network_volume("/config/viofosync.db") is None
    assert caplog.text == ""


def test_silent_when_fstype_undetectable(tmp_path, monkeypatch, caplog):
    """No /proc/mounts (macOS dev box) must not produce a false alarm."""
    monkeypatch.setattr(fsinfo, "_PROC_MOUNTS", str(tmp_path / "nope"))
    with caplog.at_level(logging.WARNING, logger="viofosync.db"):
        assert db_mod.warn_if_network_volume("/config/viofosync.db") is None
    assert caplog.text == ""
