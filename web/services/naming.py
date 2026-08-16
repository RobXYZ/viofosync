"""Camera facade for the web layer + export filename derivation.

Pure functions — no DB or HTTP. Two roles:

1. The web app's view of the camera registry
   (viofosync_lib/cameras.py): re-exports plus the derived
   per-camera export job types (``join_<channel>``, ``pip``/
   ``pip_<channel>``) with their letter/partner/main tables, and
   the timeline channel order/labels.

2. Download filenames for joined/PiP exports: ``build_basename``
   turns a set of clips plus a camera label into a stem like
   ``2024-03-15_1430-1502_front_4clips``; ``export_download_name``
   maps an export job type to a label and appends ``.mp4``,
   falling back to ``{instance}_export_{id}.mp4`` (legacy
   ``viofosync_export_{id}.mp4`` when the instance name is unset)
   when the source clips are gone (retention) or the type is
   unknown. (Original, un-joined clips keep their dashcam
   basenames — they don't go through this module.) Timestamps are
   unix seconds formatted in local time, matching how the archive
   UI renders clip times (web/routers/archive.py).
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import re
from typing import List

from viofosync_lib.cameras import (  # noqa: F401 — re-exported
    CAMERAS,
    GPS_CAMERA_LETTER,
    channel_of,
    is_gps_camera,
    pair_slot_of,
)

# --- Per-camera export job types, derived from the registry ----------
#
# Join types exist for every camera (``join_front`` … ``join_interior``).
# PiP types pair the front camera with one partner: the legacy ``pip``
# is front-main + rear inset; ``pip_<channel>`` makes the partner
# fullscreen with the front inset. Adding a camera in
# viofosync_lib/cameras.py extends all of these automatically.

JOIN_LETTER_FOR_TYPE = {
    f"join_{c.channel}": c.letter for c in CAMERAS
}

_PARTNERS = [c.channel for c in CAMERAS if c.channel != "front"]

# job type -> the non-front slot it pairs with
PIP_PARTNER_FOR_TYPE = {"pip": "rear"} | {
    f"pip_{ch}": ch for ch in _PARTNERS
}

# job type -> which side is fullscreen
PIP_MAIN_FOR_TYPE = {"pip": "front"} | {
    f"pip_{ch}": ch for ch in _PARTNERS
}

# Everything enqueue()/the route accept, except "timeline" which has
# its own entry point.
EXPORT_JOB_TYPES = (*JOIN_LETTER_FOR_TYPE, *PIP_PARTNER_FOR_TYPE)

# Export job type -> camera label used in the filename.
LABEL_FOR_TYPE = {
    f"join_{c.channel}": c.channel for c in CAMERAS
} | {"pip": "pip-front"} | {
    f"pip_{ch}": f"pip-{ch}" for ch in _PARTNERS
}


_INSTANCE_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def instance_slug(name: str) -> str:
    """Filename-safe form of the instance name: runs of unsafe chars
    collapse to ``_``; if nothing usable survives, fall back to the
    default so the legacy export filename never goes empty. Case
    variants of the default name normalise to the canonical
    ``viofosync``."""
    slug = _INSTANCE_SLUG_RE.sub("_", name or "").strip("._-")
    if not slug or slug.lower() == "viofosync":
        return "viofosync"
    return slug


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
    job_type: str, clips: List[dict], job_id: int,
    instance_name: str = "viofosync",
) -> str:
    """Filename for an export download. Best-effort: falls back to
    the legacy name when there's nothing to derive from."""
    label = LABEL_FOR_TYPE.get(job_type)
    if not label or not clips:
        return f"{instance_slug(instance_name)}_export_{job_id}.mp4"
    return f"{build_basename(clips, label)}.mp4"


# --- Filename-derived SQL fragments ---------------------------------------
#
# Two on-disk filename layouts exist (see docs/ARCHITECTURE.md):
#   standard: ``YYYY_MMDD_HHMMSS_NNNN[PE]?<letter>.MP4``
#   compact:  ``YYYYMMDDHHMMSS_NNNNNN.MP4`` — some single-channel units list
#             recordings with no datetime separators and no camera suffix;
#             the sole lens is the GPS-bearing one.
# Discriminators: standard has ``_`` at position 5 and a camera letter at -5;
# compact has digits in both places. The GPS letter is interpolated rather
# than bound (it is a one-character registry constant, not user input) so the
# fragments carry no SQL parameters — callers can't get param order wrong.


def suffixless_sql(col: str = "filename") -> str:
    """SQL: the name is compact/suffix-less (a digit where the camera
    letter would sit)."""
    return f"substr({col}, -5, 1) BETWEEN '0' AND '9'"


def gps_lens_sql(col: str = "filename") -> str:
    """SQL: the clip is the GPS-bearing lens — the registry's GPS letter,
    or a suffix-less single-channel name (which IS that lens)."""
    return (
        f"(upper(substr({col}, -5, 1)) = '{GPS_CAMERA_LETTER}' "
        f"OR {suffixless_sql(col)})"
    )


def camera_letter_sql(col: str = "filename") -> str:
    """SQL: the clip's camera letter; suffix-less names default to the
    GPS lens."""
    return (
        f"CASE WHEN {suffixless_sql(col)} THEN '{GPS_CAMERA_LETTER}' "
        f"ELSE upper(substr({col}, -5, 1)) END"
    )


def _stamp14_sql(col: str) -> str:
    """SQL: the filename's 14-digit ``YYYYMMDDHHMMSS`` stamp, normalized
    across every separator layout.

    ``downloaded_filename_re`` makes both datetime separators optional, so
    the stamp occupies 14, 15, or 16 leading characters. Stripping the
    separators out of the first 16 and keeping 14 digits handles all of
    them in one expression — no per-layout branch to add (or forget: the
    branch that took 14 raw characters off an A129 Pro name kept the
    separator and dropped the seconds' last digit, so captures under ten
    seconds apart collapsed onto one key). Anything past the stamp is
    sequence digits, which the truncation discards.
    """
    return f"substr(replace(substr({col}, 1, 16), '_', ''), 1, 14)"


def day_key_sql(col: str = "filename") -> str:
    """SQL: the clip's ``YYYY-MM-DD`` day key from the filename, for every
    layout. Derived from the name rather than ``recorded_at`` so grouping
    is stable for rows missing a timestamp and immune to unixepoch/localtime
    conversion drift."""
    s = _stamp14_sql(col)
    return (
        f"substr({s}, 1, 4) || '-' || substr({s}, 5, 2) "
        f"|| '-' || substr({s}, 7, 2)"
    )


def gps_sibling_sql(col: str = "f.filename") -> str:
    """Correlated SQL: ``col`` names the GPS-bearing sibling of the row
    aliased ``dq``. Same-capture lenses share the filename's 16-char
    timestamp prefix but NOT necessarily the sequence number (parking
    captures give each lens its own), so the sibling is matched by prefix
    range — usable with an index on ``col`` — never by rebuilding a sibling
    filename from the stem. For a compact name the prefix spans the
    timestamp + first sequence digit and the only possible match is the row
    itself: a compact clip is its own GPS sibling."""
    return (
        f"{col} >= substr(dq.filename, 1, 16)"
        f" AND {col} < substr(dq.filename, 1, 16) || '~'"
        f" AND {gps_lens_sql(col)}"
    )


def capture_key_sql(col: str = "filename") -> str:
    """SQL: the clip's 14-digit ``YYYYMMDDHHMMSS`` capture key, normalized
    across every filename layout so ``MAX()`` and grouping are layout-safe
    (raw prefixes don't collate — ``'_'`` sorts after ``'9'``). Same-capture
    lenses share this key; sequence numbers do not affect it."""
    return _stamp14_sql(col)


# --- Timeline camera channels -------------------------------------------

# Channel keys/labels come straight from the registry; "other" is the
# fallback channel_of() uses for unrecognised codes. channel_of itself
# lives in viofosync_lib.cameras and is re-exported above.
CHANNEL_ORDER = [c.channel for c in CAMERAS] + ["other"]
CHANNEL_LABELS = {c.channel: c.label for c in CAMERAS} | {
    "other": "Other",
}
