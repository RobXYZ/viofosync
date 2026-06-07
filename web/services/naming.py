"""Derive sensible download filenames for joined/PiP exports.

Pure functions — no DB or HTTP. ``build_basename`` turns a set of
clips plus a camera label into a stem like
``2024-03-15_1430-1502_front_4clips`` (date + time-range + camera
+ clip count). ``export_download_name`` maps an export job type to
a label and appends ``.mp4``, falling back to the legacy
``viofosync_export_{id}.mp4`` when the source clips are gone
(retention) or the type is unknown.

(Original, un-joined clips are downloaded individually and keep
their dashcam basenames — they don't go through this module.)

Timestamps are unix seconds formatted in local time, matching how
the archive UI renders clip times (web/routers/archive.py).
"""
from __future__ import annotations

import datetime as _dt
import json as _json
from typing import List

# Export job type -> camera label used in the filename.
LABEL_FOR_TYPE = {
    "join_front": "front",
    "join_rear": "rear",
    "pip": "pip-front",       # front-main PiP
    "pip_rear": "pip-rear",   # rear-main PiP
}


def build_basename(clips: List[dict], label: str) -> str:
    """Stem (no extension) for a set of clips and a camera label.

    Same day  -> ``2024-03-15_1430-1502_front_4clips``
    One clip  -> ``2024-03-15_1430_front_1clip`` (range collapses)
    Spans days-> ``2024-03-15_to_2024-03-17_front_12clips`` (no times)
    """
    times = sorted(
        _dt.datetime.fromtimestamp(c["timestamp"]) for c in clips
    )
    start, end = times[0], times[-1]
    n = len(times)
    count = f"{n}clip" if n == 1 else f"{n}clips"

    if start.date() == end.date():
        day = start.strftime("%Y-%m-%d")
        if start.strftime("%H%M") == end.strftime("%H%M"):
            stamp = f"{day}_{start.strftime('%H%M')}"
        else:
            stamp = (
                f"{day}_{start.strftime('%H%M')}-{end.strftime('%H%M')}"
            )
    else:
        stamp = (
            f"{start.strftime('%Y-%m-%d')}_to_{end.strftime('%Y-%m-%d')}"
        )
    return f"{stamp}_{label}_{count}"


def parse_clip_ids(raw: str) -> List[int]:
    """Read the export_jobs.clip_ids JSON column, which is either a
    bare list (legacy) or ``{"clip_ids": [...], "encoder": ...}``.

    Best-effort: returns ``[]`` on bad JSON, an unexpected shape, or
    non-integer ids rather than raising — the download path that
    relies on it degrades to the legacy filename instead of 500ing.
    """
    try:
        data = _json.loads(raw)
        if isinstance(data, dict):
            data = data.get("clip_ids", [])
        if not isinstance(data, list):
            return []
        return [int(x) for x in data]
    except (ValueError, TypeError):
        return []


def export_download_name(
    job_type: str, clips: List[dict], job_id: int
) -> str:
    """Filename for an export download. Best-effort: falls back to
    the legacy name when there's nothing to derive from."""
    label = LABEL_FOR_TYPE.get(job_type)
    if not label or not clips:
        return f"viofosync_export_{job_id}.mp4"
    return f"{build_basename(clips, label)}.mp4"


# --- Timeline camera channels -------------------------------------------

# The lens is the trailing letter of a clip's ``camera`` code:
# F / PF (parking) / EF (event) -> front; R / PR -> rear; a future
# interior lens is I. Anything else falls back to "other" so an
# unexpected code still gets its own track rather than vanishing.
_CHANNEL_FOR_LETTER = {"F": "front", "R": "rear", "I": "interior"}

# Stable display order for channel tracks, and human labels.
CHANNEL_ORDER = ["front", "rear", "interior", "other"]
CHANNEL_LABELS = {
    "front": "Front",
    "rear": "Rear",
    "interior": "Interior",
    "other": "Other",
}


def channel_of(camera: str | None) -> str:
    """Map a clip's ``camera`` code to a timeline channel key."""
    if not camera:
        return "other"
    return _CHANNEL_FOR_LETTER.get(camera[-1].upper(), "other")
