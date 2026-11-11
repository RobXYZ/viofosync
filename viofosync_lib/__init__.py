"""Public API for Viofo dashcam sync helpers.

Split into three private submodules by responsibility:

- :mod:`viofosync_lib._archive` — filename patterns, path helpers,
  filesystem walking
- :mod:`viofosync_lib._protocol` — HTTP API to the dashcam (XML
  listing, HTML scrape, byte downloader)
- :mod:`viofosync_lib._gpx` — MP4 atom parsing + GPX generation

plus one deliberately public submodule:

- :mod:`viofosync_lib.cameras` — the camera registry (which
  lenses exist and how a filename's trailing letter maps to one).
  Imported directly (``from viofosync_lib.cameras import …``) by
  both this package and the web layer.
"""
from __future__ import annotations

import http.client
import logging
import posixpath
import re
import urllib.error
import urllib.request

# Re-export the public API.
from ._archive import (
    Recording,
    downloaded_filename_re,
    get_downloaded_recordings,
    get_filepath,
    get_group_name,
)
from ._gpx import (
    IncompleteRecording,
    extract_gps_data,
    generate_gpx,
    has_final_moov,
    parse_moov,
)
from ._protocol import (
    DownloadCancelled,
    DownloadDeferred,
    TruncatedRead,
    download_file,
    extract_remote_gps_points,
    get_dashcam_filenames,
    get_dashcam_filenames_html,
    remote_moov_reachable,
)
from .progress import ProgressSink


def download_file_with(
    *args,
    max_attempts: int | None = None,
    socket_timeout: float | None = None,
    **kwargs,
):
    """Call :func:`download_file` with per-call ``max_attempts`` /
    ``socket_timeout`` overrides. Passes them straight through as
    parameters (download_file resolves None to the module defaults),
    so two concurrent downloads never clobber each other's settings."""
    from . import _protocol as _proto
    return _proto.download_file(
        *args,
        max_attempts=max_attempts,
        socket_timeout=socket_timeout,
        **kwargs,
    )


def dashcam_path(filepath: str) -> str:
    """Normalise a listing ``Recording.filepath`` to the camera's
    absolute forward-slash form.

    The HTML listing already reports ``/DCIM/Movie/X.MP4``; the XML
    listing reports ``A:\\DCIM\\Movie\\X.MP4`` (drive letter ``A:`` for
    the SD card, ``B:`` for an SSD, backslash separators). Both come
    out as ``/DCIM/Movie/X.MP4``.
    """
    cleaned = re.sub(r"^[A-Z]:", "", filepath or "").replace("\\", "/")
    return "/" + cleaned.lstrip("/")


# The camera's verdict in its reply to a command: 0 is success.
_STATUS_RE = re.compile(r"<Status>\s*(-?\d+)\s*</Status>")


def delete_dashcam_file(
    base_url: str,
    filepath: str,
    filename: str,
    *,
    timeout: float = 10.0,
) -> bool:
    """Ask the Viofo dashcam to delete the clip ``filename``.

    ``filepath`` is the clip's FULL remote path as the listing reported
    it — what the download queue stores in ``source_dir`` — in either
    listing mode's shape (see :func:`dashcam_path`). It is not a
    directory: the filename is not appended to it.

    Confirmed protocol against the A229 Pro:

        GET <base_url>/?custom=1&cmd=4003&str=<absolute-path>

    The camera answers 200 whatever happened, so the delete counts as
    made only when its reply says so: ``<Status>0</Status>``. Returns
    True for that, and False for any other reply (another Status, or
    none) and on any HTTP, URL, or timeout error. Never raises —
    failure is the caller's cue to log a warning and continue.

    Also returns False, without sending anything, when ``filepath``
    does not name ``filename`` (a bulk-import row's ``/import/``, an
    empty path, a bare directory): a request for the wrong path would
    be no delete of this clip, whatever the camera answered.
    """
    log = logging.getLogger("viofosync_lib.delete")
    path = dashcam_path(filepath)
    if not filename or posixpath.basename(path) != filename:
        log.warning(
            "dashcam delete %s: %r is not this clip's camera path",
            filename, filepath,
        )
        return False
    url = f"{base_url}/?custom=1&cmd=4003&str={path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if not 200 <= getattr(resp, "status", 0) < 300:
                log.warning(
                    "dashcam delete %s: HTTP %s", filename, resp.status
                )
                return False
            reply = resp.read(4096).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        log.warning("dashcam delete %s: HTTP %s", filename, e.code)
        return False
    except (urllib.error.URLError, TimeoutError, OSError,
            http.client.HTTPException) as e:
        log.warning("dashcam delete %s: %s", filename, e)
        return False
    m = _STATUS_RE.search(reply)
    if m is None:
        # Nothing in the reply says either way.
        log.warning(
            "dashcam delete %s: the camera's reply doesn't confirm the delete "
            "(no Status)", filename,
        )
        return False
    if int(m[1]) != 0:
        log.warning(
            "dashcam delete %s: the camera didn't delete it (Status %s)",
            filename, m[1],
        )
        return False
    return True


__all__ = [
    "DownloadCancelled",
    "DownloadDeferred",
    "IncompleteRecording",
    "TruncatedRead",
    "Recording",
    "ProgressSink",
    "dashcam_path",
    "delete_dashcam_file",
    "download_file",
    "download_file_with",
    "downloaded_filename_re",
    "extract_gps_data",
    "extract_remote_gps_points",
    "generate_gpx",
    "get_dashcam_filenames",
    "get_dashcam_filenames_html",
    "get_downloaded_recordings",
    "get_filepath",
    "get_group_name",
    "has_final_moov",
    "parse_moov",
    "remote_moov_reachable",
]
