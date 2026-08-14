"""Which mount holds a path, and whether that mount is over a network.

Kept out of any one feature because three callers need it: the startup
check that warns when the state DB lands on NFS, the debug bundle's
runtime section, and anything else that has to reason about a
NAS-backed volume. Read-only and cheap — one pass over /proc/mounts.
"""
from __future__ import annotations

import os

_PROC_MOUNTS = "/proc/mounts"

# Filesystems served over a network. SQLite's WAL mode and POSIX
# locking are unreliable on these, and attribute caching makes stat()
# answers stale — the same class of lie that made os.access() report
# writable directories as unwritable.
NETWORK_FSTYPES = frozenset({
    "nfs", "nfs3", "nfs4", "cifs", "smbfs", "smb3", "afs", "9p",
    "fuse.sshfs", "glusterfs", "ceph",
})


def fstype_of(path: str) -> str:
    """Filesystem type of the mount holding *path*.

    ``"unknown"`` when /proc/mounts can't be read (macOS, a container
    without /proc) — callers must treat that as "no answer", never as
    "local". Longest-prefix match with a separator boundary, so a
    ``/data`` mount doesn't claim ``/data-old/x``.
    """
    try:
        best = ("", "unknown")
        with open(_PROC_MOUNTS, encoding="utf-8") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) < 3:
                    continue
                mnt, fstype = fields[1], fields[2]
                boundary = mnt.rstrip(os.sep) + os.sep
                matches = path == mnt or path.startswith(boundary)
                if matches and len(mnt) > len(best[0]):
                    best = (mnt, fstype)
        return best[1]
    except OSError:
        return "unknown"


def is_network_fstype(fstype: str) -> bool:
    """True for filesystems served over a network. ``"unknown"`` is
    False: an undetectable mount must not raise a false alarm."""
    return fstype.lower() in NETWORK_FSTYPES
