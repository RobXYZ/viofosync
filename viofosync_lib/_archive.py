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

# Downloaded recording filename glob pattern. Some Viofo models write
# files as ``YYYYMMDDHHMMSS_NNNNNN.MP4`` without a camera suffix, while
# newer/multi-channel models use ``YYYY_MMDD_HHMMSS_NNNN[FRTI].MP4``.
downloaded_filename_glob = (
    "[0-9][0-9][0-9][0-9]*_[0-9]*.MP4"
)

# Downloaded recording filename regular expression.
downloaded_filename_re = re.compile(
    r"^(?P<year>\d{4})_?(?P<month>\d{2})(?P<day>\d{2})"
    r"_?(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})"
    r"_(?P<sequence>\d+)(?P<camera>[PE]?[FRTI])?\.MP4$",
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
    """Reads destination dir and returns set of (filename, date)."""
    group_name_glob = group_name_globs[grouping]
    filepath_glob = get_filepath(
        destination, group_name_glob, downloaded_filename_glob
    )
    downloaded_filepaths = glob.glob(filepath_glob)

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
