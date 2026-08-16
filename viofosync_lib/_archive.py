"""Filesystem and pattern helpers for the local archive.

Pure-Python: filename regexes, group-name (date bucket) helpers,
path joiners, and a glob-driven scanner that returns the recordings
already on disk under the destination directory.
"""
from __future__ import annotations

import datetime
import glob
import logging
import os
import re
from collections import namedtuple

logger = logging.getLogger("viofosync_lib.archive")

# Recording namedtuple matching Viofo's file information.
Recording = namedtuple(
    "Recording",
    "filename filepath size timecode datetime attr",
)

# Group name globs, keyed by grouping mode.
group_name_globs = {
    "none": None,
    "daily": "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]",
    "weekly": "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]",
    "monthly": "[0-9][0-9][0-9][0-9]-[0-9][0-9]",
    "yearly": "[0-9][0-9][0-9][0-9]",
}

# Downloaded recording filename regular expression — the single source of
# truth for "is this name a recording", and the only place the on-disk
# layouts are encoded. Firmware varies in which separators it writes:
#
#   standard   2026_0628_133416_0001PF.MP4
#   A129 Pro   20260628_133416_0001PF.MP4   (no YYYY/MMDD separator)
#   compact    20260628133416_000123.MP4    (single-channel, no camera)
#
# so both datetime separators are independently optional. The trailing
# letters are an optional event prefix (P=parking, E=impact) plus the
# camera letter (see cameras.py for the registry); compact names put a
# sequence digit where the letter would sit, so an empty ``camera`` group
# identifies them — callers default it to the GPS-bearing lens.
#
# Everything that recognises recordings derives from this pattern rather
# than restating it: a name this accepts but a caller's own pattern misses
# reads as "never downloaded" and gets fetched again on every sync.
downloaded_filename_re = re.compile(
    r"^(?P<year>\d{4})_?(?P<month>\d{2})(?P<day>\d{2})"
    r"_?(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})"
    r"_(?P<sequence>\d+)(?P<camera>[A-Za-z]*)\.MP4$",
    re.IGNORECASE,
)


def get_filepath(destination, group_name, filename):
    """Constructs a path from destination, group name and filename."""
    if group_name:
        return os.path.join(destination, group_name, filename)
    return os.path.join(destination, filename)


def get_group_name(recording_datetime, grouping):
    """Determines the group name for a recording datetime."""
    if grouping == "daily":
        return recording_datetime.strftime("%Y-%m-%d")
    elif grouping == "weekly":
        delta = datetime.timedelta(
            days=recording_datetime.weekday()
        )
        return (recording_datetime - delta).strftime("%Y-%m-%d")
    elif grouping == "monthly":
        return recording_datetime.strftime("%Y-%m")
    elif grouping == "yearly":
        return recording_datetime.strftime("%Y")
    return None


def get_downloaded_recordings(destination, grouping):
    """Reads destination dir and returns set of (filename, date).

    Lists every ``.MP4`` and lets ``downloaded_filename_re`` decide what
    counts, so a layout the regex accepts can't be missed here — matching
    the glob to one layout is what made A129 Pro names invisible and got
    them re-downloaded every sync.
    """
    group_name_glob = group_name_globs[grouping]
    downloaded_filepaths = glob.glob(
        get_filepath(destination, group_name_glob, "*.MP4")
    )

    recordings = set()
    for filepath in downloaded_filepaths:
        filename = os.path.basename(filepath)
        m = downloaded_filename_re.match(filename)
        if m:
            recording_date = datetime.date(
                int(m.group("year")),
                int(m.group("month")),
                int(m.group("day")),
            )
            recordings.add((filename, recording_date))
    return recordings
