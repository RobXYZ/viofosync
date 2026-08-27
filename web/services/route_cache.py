"""Per-day cache of the aggregated GPS route payload.

Building a day's route re-parses every GPX sidecar for that day, which is
slow on a large archive (tens of seconds for hundreds of clips) and runs
on every day-view / route request. The result only changes when the day's
GPX files change, so cache it keyed by a signature of those files
(path + mtime + size) and rebuild only on a mismatch.

Persisted as a JSON sidecar under ``$RECORDINGS/.route_cache/<date>.json``
so it survives restarts — mirroring the ``.thumbs`` / ``.filmstrips``
caches. Labels are NOT cached here: they come from the geocode cache and
are applied fresh by the caller, so they stay current as that cache fills.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from typing import Any, Iterable, Optional


def _cache_dir(recordings: str) -> str:
    d = os.path.join(recordings, ".route_cache")
    os.makedirs(d, exist_ok=True)
    return d


def clear_all(recordings: str) -> None:
    """Drop the entire per-day route cache; journeys recompute from the on-disk
    GPX on the next request. Best-effort and idempotent (no-op when absent)."""
    shutil.rmtree(os.path.join(recordings, ".route_cache"), ignore_errors=True)


def _cache_path(recordings: str, date: str) -> str:
    return os.path.join(_cache_dir(recordings), f"{date}.json")


# Aggregation-algorithm version, folded into every signature. Bump when
# aggregate_day's semantics change so persisted entries built by older
# code miss and recompute — the file-set part of the signature can't see
# code changes. v2: GPX clock detection (_rebase_to_filename_clock) —
# journey epochs moved for local-clock firmware, so every pre-v2 entry
# is stale even though the GPX bytes are unchanged.
_SCHEMA = "2"


def signature(gpx_paths: Iterable[str]) -> str:
    """Stable fingerprint of the GPX file set for a day. Order-independent;
    changes when any file's mtime/size changes or files are added/removed,
    and when the aggregation schema version bumps.
    Missing files contribute nothing (they can't affect the aggregation)."""
    parts = [f"schema:{_SCHEMA}"]
    for p in sorted(gpx_paths):
        try:
            st = os.stat(p)
        except OSError:
            continue
        parts.append(f"{p}:{st.st_mtime_ns}:{st.st_size}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def load(recordings: str, date: str, sig: str) -> Optional[dict]:
    """Return the cached payload for ``date`` iff it was built from the
    same GPX file set (``sig``); otherwise None."""
    try:
        with open(_cache_path(recordings, date)) as f:
            blob = json.load(f)
    except (OSError, ValueError):
        return None
    if blob.get("signature") != sig:
        return None
    return blob.get("payload")


def store(recordings: str, date: str, sig: str, payload: Any) -> None:
    """Persist ``payload`` for ``date`` under signature ``sig``. Best-effort:
    a write failure just means the next request recomputes."""
    try:
        with open(_cache_path(recordings, date), "w") as f:
            json.dump({"signature": sig, "payload": payload}, f)
    except OSError:
        pass
