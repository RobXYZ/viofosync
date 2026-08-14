"""HTTP protocol layer for talking to the Viofo dashcam.

Listing endpoints (XML and HTML scrape), HEAD probes, and the
chunked atomic byte downloader.

The module-level ``socket_timeout`` and ``max_download_attempts``
globals are the defaults used when a caller doesn't pass per-call
overrides; :func:`download_file` accepts ``socket_timeout`` /
``max_attempts`` keyword args (threaded through by
:func:`viofosync_lib.download_file_with`) so concurrent downloads
never share mutable state.
"""
from __future__ import annotations

import datetime
import errno
import http.client
import logging
import os
import re
import socket
import tempfile
import time
import urllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from ._archive import Recording, downloaded_filename_re, get_filepath

logger = logging.getLogger("viofosync_lib.protocol")


class DownloadCancelled(Exception):
    """Raised when ``download_file`` is deliberately aborted via its
    ``cancel_check`` (user pause/stop, or lost reachability).

    Distinct from the transient errors the retry loop swallows: a
    cancellation must short-circuit retries and must not be counted as
    a failed attempt by callers.
    """


class DownloadDeferred(Exception):
    """Raised when ``download_file`` aborts because the file is still being
    recorded (the stream outgrew its expected size — the stale-size firmware
    under-reports the active file). Like :class:`DownloadCancelled`, this is
    NOT a failed attempt: the caller must requeue the item without burning a
    retry, so the completeness guard can hold it until the segment finalizes."""


class TruncatedRead(Exception):
    """A byte-range read returned fewer bytes than requested while not at
    EOF — the camera closed the body early. Distinct from a connection
    error: the camera *was* reachable, so the caller treats this as a
    clip-specific read failure, not a camera-offline condition."""


class _StillRecording(Exception):
    """Internal: the file grew past its expected size mid-download (active
    recording). Caught in download_file and re-raised as the public
    :class:`DownloadDeferred` so the caller refunds the attempt."""


# Defaults used when download_file gets no per-call override.
socket_timeout = 10.0
DEFAULT_DOWNLOAD_ATTEMPTS = 1
max_download_attempts = DEFAULT_DOWNLOAD_ATTEMPTS
RETRY_BACKOFF = 5  # seconds, multiplied by attempt number


def parse_viofo_datetime(time_str):
    """Parse the datetime string from Viofo's format."""
    return datetime.datetime.strptime(time_str, "%Y/%m/%d %H:%M:%S")


def _interruptible_sleep(seconds, cancel_check):
    """Sleep up to ``seconds``, polling ``cancel_check`` so a
    pause/stop/unreachable signal is honoured within ~100ms instead
    of after the full backoff. Raises DownloadCancelled if asked to
    stop."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if cancel_check is not None and cancel_check():
            raise DownloadCancelled("cancelled during retry backoff")
        time.sleep(min(0.1, remaining))


def get_dashcam_filenames(
    base_url,
    *,
    include_normal=True,
    include_ro=True,
):
    """Gets the recording filenames from the Viofo dashcam.

    By default returns both normal and read-only (locked)
    recordings. Pass ``include_normal=False`` or
    ``include_ro=False`` to restrict the listing to a single
    kind."""
    try:
        url = f"{base_url}/?custom=1&cmd=3015&par=1"
        request = urllib.request.Request(url)
        # Read the tunable at call time — download_file_with
        # temporarily overrides it. Without a timeout a half-open
        # connection wedges the sync worker's thread forever.
        response = urllib.request.urlopen(
            request, timeout=socket_timeout
        )

        if response.getcode() != 200:
            raise RuntimeError(
                f"Error response from {base_url}; "
                f"status code: {response.getcode()}"
            )

        xml_data = response.read().decode('utf-8')
        root = ET.fromstring(xml_data)

        recordings = []
        for file_elem in root.findall(".//File"):
            attr = int(file_elem.find("ATTR").text)
            is_ro = (attr == 33)
            if is_ro and not include_ro:
                continue
            if not is_ro and not include_normal:
                continue
            name = file_elem.find("NAME").text
            filepath = file_elem.find("FPATH").text
            size = int(file_elem.find("SIZE").text)
            timecode = int(file_elem.find("TIMECODE").text)
            ts = parse_viofo_datetime(
                file_elem.find("TIME").text
            )
            recording = Recording(
                name, filepath, size, timecode, ts, attr
            )
            recordings.append(recording)

        logger.info(f"Found {len(recordings)} recordings on dashcam")
        return recordings
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Cannot obtain recordings from {base_url}: {e}"
        ) from e
    except socket.timeout as e:
        raise UserWarning(
            f"Timeout communicating with dashcam at "
            f"{base_url}: {e}"
        ) from e
    except http.client.RemoteDisconnected as e:
        raise UserWarning(
            f"Dashcam disconnected without response; "
            f"address: {base_url}: {e}"
        ) from e
    except ET.ParseError as e:
        raise RuntimeError(
            f"Error parsing XML response from dashcam: {e}"
        ) from e


# HTML directory listing regex.
html_file_re = re.compile(
    r'<a href="(?P<filepath>[^"]+\.MP4)">'
    r'<b>(?P<filename>[^<]+)</b></a>'
    r'<td align=right>\s*(?P<size>[\d.]+)\s*(?P<unit>[KMGT]?B)',
    re.IGNORECASE,
)

# Directories to scrape on the dashcam.
DCIM_DIRS = ["/DCIM/Movie", "/DCIM/Movie/Parking"]
DCIM_DIRS_RO = ["/DCIM/Movie/RO"]

def parse_html_size(size_str, unit):
    """Converts '102.00 MB' style size to bytes."""
    multipliers = {
        "B": 1, "KB": 1 << 10, "MB": 1 << 20,
        "GB": 1 << 30, "TB": 1 << 40,
    }
    return int(float(size_str) * multipliers.get(unit, 1))


def get_dashcam_filenames_html(
    base_url,
    *,
    include_normal=True,
    include_ro=True,
):
    """Gets recordings by scraping the HTML directory listings.

    Much faster than the XML API on cameras with many files.
    By default scrapes both the normal and the read-only
    folders; pass the relevant flag to restrict the listing."""
    dirs = []
    if include_normal:
        dirs += DCIM_DIRS
    if include_ro:
        dirs += DCIM_DIRS_RO
    recordings = []

    for dir_path in dirs:
        url = f"{base_url}{dir_path}"
        try:
            with urllib.request.urlopen(
                url, timeout=socket_timeout
            ) as resp:
                if resp.getcode() != 200:
                    logger.warning(
                        f"HTTP {resp.getcode()} for {url}, "
                        f"skipping"
                    )
                    continue
                html = resp.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.debug(
                    f"Directory not found: {dir_path}"
                )
                continue
            raise
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Cannot reach dashcam at {base_url}: {e}"
            ) from e
        except socket.timeout as e:
            raise UserWarning(
                f"Timeout communicating with dashcam at "
                f"{base_url}: {e}"
            ) from e

        for m in html_file_re.finditer(html):
            filepath = m.group("filepath")
            filename = m.group("filename")
            size = parse_html_size(
                m.group("size"), m.group("unit").upper()
            )

            # Always extract datetime from the filename
            # (the actual recording timestamp).
            fm = downloaded_filename_re.search(filename)
            if not fm:
                logger.warning(
                    f"Cannot parse date from filename: "
                    f"{filename}, skipping"
                )
                continue

            ts = datetime.datetime(
                int(fm.group("year")),
                int(fm.group("month")),
                int(fm.group("day")),
                int(fm.group("hour")),
                int(fm.group("minute")),
                int(fm.group("second")),
            )

            recordings.append(Recording(
                filename, filepath, size, None, ts, None
            ))

    logger.info(
        f"Found {len(recordings)} recordings on dashcam "
        f"(HTML mode)"
    )
    return recordings


def get_remote_size(url, timeout):
    """HEAD request to get Content-Length of a remote file."""
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        cl = resp.getheader("Content-Length")
    return int(cl) if cl and cl.isdigit() else None


def human_size(num_bytes):
    """Returns human-readable size string, e.g. '325.1 MB'."""
    if num_bytes == 0:
        return "0 B"
    for factor, suffix in [(1 << 30, "GB"), (1 << 20, "MB"),
                           (1 << 10, "KB"), (1, "B")]:
        if num_bytes >= factor:
            return f"{num_bytes / factor:.1f} {suffix}"
    return f"{num_bytes} B"


def human_speed(num_bytes, elapsed):
    """Returns human-readable speed string, e.g. '27.1 MB/s'."""
    bps = num_bytes / max(elapsed, 1e-9)
    if bps == 0:
        return "0 B/s"
    for factor, suffix in [(1 << 30, "GB/s"), (1 << 20, "MB/s"),
                           (1 << 10, "KB/s"), (1, "B/s")]:
        if bps >= factor:
            return f"{bps / factor:.1f} {suffix}"
    return f"{bps:.1f} B/s"


def download_file(base_url, recording, destination, group_name,
                  progress_sink=None, cancel_check=None,
                  *, max_attempts=None, socket_timeout=None):
    """Downloads a file from the Viofo dashcam to the destination.

    Returns (downloaded: bool, speed_str: str|None).
    Uses HEAD to check size, retries up to ``max_attempts``, and
    verifies integrity after download.

    Optional args (used by the web UI):
      progress_sink: object with item_started/item_progress/
        item_finished methods; see viofosync_lib.ProgressSink.
      cancel_check: callable returning True if the download
        should be aborted (e.g. reachability lost, user stopped).
      max_attempts / socket_timeout: per-call overrides. Passed as
        parameters (not via module globals) so concurrent downloads
        can't clobber each other; default to the module-level values
        when omitted.
    """
    # Resolve per-call overrides against the module defaults. ``timeout``
    # shadows the module global of the same name within this function.
    attempts = max_attempts if max_attempts is not None else max_download_attempts
    timeout = socket_timeout if socket_timeout is not None else globals()["socket_timeout"]
    sink = progress_sink
    if group_name:
        group_filepath = os.path.join(destination, group_name)
        if not os.path.exists(group_filepath):
            os.makedirs(group_filepath)
        elif not os.path.isdir(group_filepath):
            raise RuntimeError(
                f"Not a directory: {group_filepath}"
            )
        # No os.access(W_OK) pre-flight: on NFS/UID-mapped mounts it
        # reports genuinely-writable directories as unwritable (cached
        # owner/mode vs local UID, while the server accepts the write).
        # The mkstemp below is the authoritative probe — it raises an
        # honest OSError when the directory truly isn't writable.

    dest_filepath = get_filepath(
        destination, group_name, recording.filename
    )

    # Build download URL — strip drive letter (A: for SD, B: for SSD)
    # and normalise path separators.
    cleaned = re.sub(r'^[A-Z]:', '', recording.filepath).replace(
        '\\', '/'
    )
    url = f"{base_url}/{cleaned.lstrip('/')}"

    # Check expected size via HEAD. Some firmwares drop HEAD under
    # load — fall back to the listing size so integrity verification
    # still runs (a connection closed cleanly mid-stream otherwise
    # archives a truncated file as a success).
    head_started = time.perf_counter()
    try:
        expected_size = get_remote_size(url, timeout)
    except Exception:
        expected_size = None
    head_elapsed = time.perf_counter() - head_started
    if expected_size is None:
        expected_size = recording.size

    # Skip if already downloaded and size matches.
    if os.path.exists(dest_filepath):
        local_size = os.path.getsize(dest_filepath)
        if expected_size is not None:
            if local_size == expected_size:
                logger.debug(
                    f"Skipping {recording.filename} "
                    f"({human_size(local_size)})"
                )
                return False, None
            # Size mismatch — re-download.
            logger.info(
                f"Size mismatch for {recording.filename} "
                f"({human_size(local_size)}/"
                f"{human_size(expected_size)}), "
                f"re-downloading"
            )
        elif recording.size is not None:
            if local_size == recording.size:
                logger.debug(
                    f"Skipping {recording.filename} "
                    f"({human_size(local_size)})"
                )
                return False, None
            logger.info(
                f"Size mismatch for {recording.filename} "
                f"({human_size(local_size)}/"
                f"{human_size(recording.size)}), "
                f"re-downloading"
            )
        else:
            logger.debug(
                f"Already downloaded: {recording.filename}"
            )
            return False, None

    # Atomic download with tempfile + retries.
    dest_dir = os.path.dirname(dest_filepath)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=dest_dir,
        prefix=f".{recording.filename}.",
        suffix=".part",
    )
    os.close(tmp_fd)

    if sink is not None:
        sink.item_started(recording.filename, expected_size)

    try:
        for attempt in range(1, attempts + 1):
            try:
                start = time.perf_counter()
                bytes_done = 0
                last_emit = start
                with urllib.request.urlopen(
                    url, timeout=timeout
                ) as resp, open(tmp_path, "wb") as out:
                    while True:
                        if cancel_check is not None and cancel_check():
                            raise DownloadCancelled(
                                "Download cancelled"
                            )
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        bytes_done += len(chunk)
                        # Defense-in-depth for the stale-size firmware: if the
                        # stream outgrows the expected size by more than the
                        # MB-rounding slack, the file is still being recorded.
                        # Abort and defer rather than loop on "Incomplete
                        # download" (the camera under-reports the active file).
                        if (expected_size is not None
                                and bytes_done > expected_size + 2 * 1024 * 1024):
                            raise _StillRecording(
                                f"file still growing: streamed "
                                f"{bytes_done} > expected {expected_size}"
                            )
                        if sink is not None:
                            now = time.perf_counter()
                            if now - last_emit >= 0.25:
                                speed = bytes_done / max(
                                    now - start, 1e-9
                                )
                                sink.item_progress(
                                    recording.filename,
                                    bytes_done,
                                    expected_size,
                                    speed,
                                )
                                last_emit = now
                elapsed = time.perf_counter() - start
            except DownloadCancelled:
                # Deliberate abort (pause/stop/unreachable) — not a
                # failure. Stop immediately without burning the rest of
                # the retry budget; the caller requeues without penalty.
                logger.info(
                    f"Download of {recording.filename} cancelled"
                )
                raise
            except _StillRecording as e:
                logger.info(f"Deferring {recording.filename}: {e}")
                # Treat like a cancellation: no attempt burned, partial cleaned
                # up by the finally. Propagate as the public DownloadDeferred so
                # the caller requeues without penalty (a plain False return
                # would be counted as a failed attempt). The completeness guard
                # holds it until the segment finalizes.
                raise DownloadDeferred(str(e)) from e
            except Exception as e:
                if isinstance(e, OSError) and e.errno == errno.ENOSPC:
                    # Full disk: retrying can only fail the same way
                    # and would burn the item's retry budget. Raise so
                    # the caller can surface a sticky disk error.
                    raise
                logger.warning(
                    f"Download attempt {attempt} failed for "
                    f"{recording.filename}: {e}"
                )
                if attempt < attempts:
                    _interruptible_sleep(RETRY_BACKOFF * attempt, cancel_check)
                continue

            actual_size = os.path.getsize(tmp_path)

            # Verify integrity.
            if (expected_size is not None
                    and actual_size != expected_size):
                logger.warning(
                    f"Incomplete download of "
                    f"{recording.filename}: "
                    f"{human_size(actual_size)}/"
                    f"{human_size(expected_size)}"
                )
                if attempt < attempts:
                    _interruptible_sleep(RETRY_BACKOFF * attempt, cancel_check)
                continue

            # Success — atomic move into place.
            os.replace(tmp_path, dest_filepath)
            size_str = human_size(actual_size)
            speed_str = human_speed(actual_size, elapsed)
            logger.info(
                f"Downloaded {recording.filename}: "
                f"{size_str} in {elapsed:.1f}s ({speed_str}; "
                f"head {head_elapsed:.1f}s)"
            )
            if sink is not None:
                sink.item_finished(
                    recording.filename, True, None, actual_size
                )
            return True, speed_str

        # All attempts exhausted.
        logger.error(
            f"Failed to download {recording.filename} "
            f"after {attempts} attempts"
        )
        if sink is not None:
            sink.item_finished(
                recording.filename, False,
                "max attempts exhausted", None,
            )
        return False, None
    except socket.timeout as e:
        raise UserWarning(
            f"Timeout communicating with dashcam at "
            f"{base_url}: {e}"
        ) from e
    except http.client.RemoteDisconnected:
        logger.warning(
            f"Remote end closed connection for "
            f"{recording.filename}; ignoring."
        )
        return False, None
    finally:
        # Clean up temp file if it still exists.
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


class RangeReader:
    """Seekable, read-only file-like object backed by a byte-range source.

    ``read_range(a, b)`` returns the inclusive byte range ``[a, b]``. A head
    buffer of ``head_len`` bytes is prefetched on construction so a consumer
    that does many small sequential reads near the start (e.g.
    :func:`viofosync_lib.parse_moov` walking the leading ``moov`` atom) is
    served from memory; only reads outside the buffer hit ``read_range``.

    Designed to drop straight into ``parse_moov`` so the GPS decode + spike
    filter are reused for remote clips without duplicating any parsing.
    """

    def __init__(self, read_range, size, head_len=65536):
        self._read_range = read_range
        self._size = int(size)
        self._pos = 0
        n = min(head_len, self._size)
        self._head = read_range(0, n - 1) if n > 0 else b""

    def seek(self, pos, whence=0):
        if whence == 0:
            self._pos = pos
        elif whence == 1:
            self._pos += pos
        elif whence == 2:
            self._pos = self._size + pos
        else:
            raise ValueError(f"bad whence: {whence}")
        return self._pos

    def tell(self):
        return self._pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self._size - self._pos
        start = self._pos
        end = min(start + n, self._size)        # exclusive, clamped to EOF
        if end <= start:
            return b""
        head_n = len(self._head)
        if end <= head_n:
            data = self._head[start:end]
        elif start >= head_n:
            data = self._read_range(start, end - 1)
        else:
            data = self._head[start:head_n] + self._read_range(head_n, end - 1)
        # A range response shorter than the (EOF-clamped) request means the
        # camera closed the body early — raise so triage doesn't commit a
        # truncated track. `end` is already clamped to EOF, so a short slice
        # here is never a legitimate end-of-file read.
        if len(data) < (end - start):
            raise TruncatedRead(
                f"short read: got {len(data)} of {end - start} bytes "
                f"at offset {start}"
            )
        self._pos = start + len(data)
        return data


def _remote_clip_url(base_url, recording):
    """Build the camera URL for a clip, matching download_file's rules
    (strip the A:/B: drive letter, swap '\\' -> '/')."""
    cleaned = re.sub(r"^[A-Z]:", "", recording.filepath).replace("\\", "/")
    return f"{base_url}/{cleaned.lstrip('/')}"


def open_remote_reader(base_url, recording, *, timeout=None, head_len=65536):
    """Return a RangeReader over a clip on the dashcam's HTTP server.

    Uses HEAD for the total size (needed for EOF), then serves each read via
    a one-shot ``Range`` request (the camera closes the socket per request
    and resets above ~3 concurrent connections, so one in-flight request per
    reader is deliberate)."""
    t = timeout if timeout is not None else globals()["socket_timeout"]
    url = _remote_clip_url(base_url, recording)
    size = get_remote_size(url, t)
    if not size:
        size = getattr(recording, "size", None) or 0
    if not size:
        logger.warning(
            "remote reader: unknown size for %s; GPS triage will read nothing",
            url,
        )

    def _read_range(a, b):
        req = urllib.request.Request(url, headers={"Range": f"bytes={a}-{b}"})
        with urllib.request.urlopen(req, timeout=t) as resp:
            status = getattr(resp, "status", 206)
            body = resp.read()
        # A server that ignores Range answers 200 with the whole body; slice.
        if status == 200 and len(body) > (b - a + 1):
            return body[a : b + 1]
        return body

    return RangeReader(_read_range, size, head_len=head_len)


def extract_remote_gps_points(base_url, recording, *, timeout=None, head_len=65536):
    """Decode a remote clip's embedded GPS track without downloading it.

    Raises :class:`IncompleteRecording` if the clip has no reachable top-level
    ``moov`` (still recording / truncated) — the caller must defer, not cache
    a bogus empty track. A present ``moov`` with no ``gps `` box or no fix
    returns ``[]`` (a real "no GPS" result)."""
    from ._gpx import parse_moov, has_final_moov, IncompleteRecording
    reader = open_remote_reader(
        base_url, recording, timeout=timeout, head_len=head_len
    )
    if not has_final_moov(reader):
        raise IncompleteRecording(recording.filename)
    reader.seek(0)
    return parse_moov(reader)


def remote_moov_reachable(base_url, recording, *, timeout=None, head_len=65536):
    """True if a finalized ``moov`` is reachable on the remote clip (proof the
    segment has closed). Costs one HEAD + a few small range reads. Camera-
    offline errors propagate so the caller can treat them as "unknown" rather
    than "incomplete"."""
    from ._gpx import has_final_moov
    reader = open_remote_reader(
        base_url, recording, timeout=timeout, head_len=head_len
    )
    return has_final_moov(reader)
