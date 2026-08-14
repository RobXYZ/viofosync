"""Mount-type lookup used by the startup DB check and the debug bundle."""
from __future__ import annotations

from web import fsinfo


def _mounts(tmp_path, body: str) -> str:
    p = tmp_path / "mounts"
    p.write_text(body)
    return str(p)


MOUNTS = (
    "/dev/sda1 / ext4 rw 0 0\n"
    "nas:/vol/dashcam /recordings nfs4 rw,relatime 0 0\n"
    "/dev/sdb1 /data-old btrfs rw 0 0\n"
)


def test_fstype_of_longest_prefix_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(fsinfo, "_PROC_MOUNTS", _mounts(tmp_path, MOUNTS))
    assert fsinfo.fstype_of("/recordings/2026-08-14/clip.MP4") == "nfs4"
    assert fsinfo.fstype_of("/etc/hosts") == "ext4"


def test_fstype_of_respects_separator_boundary(tmp_path, monkeypatch):
    """/data must not claim /data-old, which is its own mount."""
    body = MOUNTS + "/dev/sdc1 /data xfs rw 0 0\n"
    monkeypatch.setattr(fsinfo, "_PROC_MOUNTS", _mounts(tmp_path, body))
    assert fsinfo.fstype_of("/data-old/x") == "btrfs"
    assert fsinfo.fstype_of("/data/x") == "xfs"


def test_fstype_of_unknown_without_proc_mounts(tmp_path, monkeypatch):
    monkeypatch.setattr(fsinfo, "_PROC_MOUNTS", str(tmp_path / "nope"))
    assert fsinfo.fstype_of("/anything") == "unknown"


def test_is_network_fstype():
    for fs in ("nfs", "nfs4", "cifs", "smb3", "NFS4"):
        assert fsinfo.is_network_fstype(fs), fs
    for fs in ("ext4", "btrfs", "xfs", "apfs", "overlay", "unknown", ""):
        assert not fsinfo.is_network_fstype(fs), fs
