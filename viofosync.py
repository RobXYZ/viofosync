#!/usr/bin/env python3

# Copyright (c) 2024 Rob Smith
# Based on BlackVueSync by Alessandro Colomba (https://github.com/acolomba)
# GPS extraction method by Sergei Franco
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
# OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

__version__ = "1.3"

import argparse
import datetime
import errno
from collections import namedtuple
import logging
import os
import re
import shutil
import socket
import struct
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from urllib.error import URLError

# -----------------------------------------------------------------------------
# Globals / constants
# -----------------------------------------------------------------------------
dry_run = False
max_disk_used_percent = 90
cutoff_date = None
socket_timeout = 10.0

MAX_DOWNLOAD_ATTEMPTS = 3
RETRY_BACKOFF = 5  # seconds between retries, multiplied by attempt number

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Grouping (kept for CLI compatibility; downloads use YYYY/MM/DD layout)
# -----------------------------------------------------------------------------
group_name_globs = {
    "none": None,
    "daily": "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]",
    "weekly": "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]",
    "monthly": "[0-9][0-9][0-9][0-9]-[0-9][0-9]",
    "yearly": "[0-9][0-9][0-9][0-9]",
}

# -----------------------------------------------------------------------------
# Filename parsing for dashcam recordings
# -----------------------------------------------------------------------------
downloaded_filename_re = re.compile(
    r"^(?P<year>\d{4})_(?P<month>\d{2})(?P<day>\d{2})"
    r"_(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})"
    r"_(?P<sequence>\d{6})(?P<camera>[FR])\.MP4$",
    re.IGNORECASE
)

Recording = namedtuple("Recording", "filename filepath size timecode datetime attr")


def to_downloaded_recording(filename, grouping):
    match = downloaded_filename_re.match(filename)
    if not match:
        return None
    gd = match.groupdict()
    dt = datetime.datetime(
        int(gd["year"]), int(gd["month"]), int(gd["day"]),
        int(gd["hour"]), int(gd["minute"]), int(gd["second"])
    )
    return Recording(filename, None, None, None, dt, None)


# -----------------------------------------------------------------------------
# Viofo camera API parsing helpers
# -----------------------------------------------------------------------------
def parse_viofo_datetime(time_str):
    return datetime.datetime.strptime(time_str, "%Y/%m/%d %H:%M:%S")


def get_dashcam_filenames(base_url):
    url = f"{base_url}/?custom=1&cmd=3015&par=1"
    try:
        with urllib.request.urlopen(url, timeout=socket_timeout) as resp:
            if resp.getcode() != 200:
                raise RuntimeError(f"Bad status {resp.getcode()}")
            xml_data = resp.read().decode()
    except Exception as e:
        logger.error(f"Failed to fetch file list: {e}")
        raise

    root = ET.fromstring(xml_data)
    recordings = []
    for fe in root.findall(".//File"):
        name = fe.find("NAME").text
        path = fe.find("FPATH").text
        size = int(fe.find("SIZE").text)
        timecode = int(fe.find("TIMECODE").text)
        ts = parse_viofo_datetime(fe.find("TIME").text)
        attr = int(fe.find("ATTR").text)
        recordings.append(Recording(name, path, size, timecode, ts, attr))
    logger.info(f"Found {len(recordings)} recordings on dashcam")
    return recordings


def get_remote_size(url, timeout):
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        cl = resp.getheader("Content-Length")
    return int(cl) if cl and cl.isdigit() else None


def ensure_destination(path):
    if not os.path.exists(path):
        os.makedirs(path)
    elif not os.path.isdir(path):
        raise RuntimeError(f"Not a directory: {path}")
    elif not os.access(path, os.W_OK):
        raise RuntimeError(f"Not writable: {path}")


def is_camera_online(list_url, timeout):
    try:
        req = urllib.request.Request(list_url, method="HEAD")
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except URLError as e:
        err = e.reason
        if isinstance(err, OSError) and err.errno == errno.EHOSTUNREACH:
            logger.warning("Camera offline (host unreachable)")
        else:
            logger.warning(f"Cannot reach camera ({err})")
    except socket.timeout:
        logger.warning("Camera timed out")
    return False


def human_size_and_speed(num_bytes: int, elapsed: float):
    """
    Returns (size_str, speed_str), e.g. ("325.1 MB", "27.1 MB/s").
    Chooses KB, MB, GB based on magnitude.
    """
    thresholds = [
        (1 << 30, "GB"),
        (1 << 20, "MB"),
        (1 << 10, "KB"),
        (1,       "B"),
    ]
    # size
    for factor, suffix in thresholds:
        if num_bytes >= factor:
            size = num_bytes / factor
            break
    size_str = f"{size:.1f} {suffix}"
    # speed
    bps = num_bytes / max(elapsed, 1e-9)
    for factor, suffix in thresholds:
        if bps >= factor:
            spd = bps / factor
            break
    speed_str = f"{spd:.1f} {suffix}/s"
    return size_str, speed_str


# -----------------------------------------------------------------------------
# Local cleanup (files + empty directories)
# -----------------------------------------------------------------------------
def iter_local_media_files(destination: str):
    """
    Yields full paths under destination to files that matter:
      - *.mp4
      - *.mp4.gpx
      - *.part (partial downloads)
    """
    for root, _, files in os.walk(destination):
        for fn in files:
            lower = fn.lower()
            if lower.endswith(".mp4") or lower.endswith(".mp4.gpx") or lower.endswith(".part"):
                yield os.path.join(root, fn)


def local_file_date_from_name(path: str):
    """
    Extracts datetime.date from the dashcam filename.
    Returns None if filename does not match expected format.
    """
    base = os.path.basename(path)
    if base.lower().endswith(".mp4.gpx"):
        base = base[:-4]  # strip ".gpx"

    m = downloaded_filename_re.match(base)
    if not m:
        return None
    return datetime.date(int(m.group("year")), int(m.group("month")), int(m.group("day")))


def cleanup_old_files(destination: str, cutoff: datetime.date, dry_run: bool):
    """
    Deletes local recordings older than cutoff (based on date in filename).
    Deletes .MP4 and sidecar .MP4.gpx. Also removes any leftover .part files.
    Returns number of removed filesystem paths.
    """
    if cutoff is None:
        return 0

    removed = 0

    # Remove leftover partials
    for fp in iter_local_media_files(destination):
        if fp.lower().endswith(".part"):
            if dry_run:
                logger.info(f"[DRY RUN] Would remove partial {fp}")
                removed += 1
            else:
                try:
                    os.remove(fp)
                    logger.info(f"Removed partial {fp}")
                    removed += 1
                except OSError as e:
                    logger.error(f"Error removing {fp}: {e}")

    # Remove old MP4s and sidecars
    for fp in iter_local_media_files(destination):
        if not (fp.lower().endswith(".mp4") or fp.lower().endswith(".mp4.gpx")):
            continue

        dt = local_file_date_from_name(fp)
        if dt is None:
            continue

        if dt < cutoff:
            base_path = fp[:-4] if fp.lower().endswith(".mp4.gpx") else fp
            candidates = [
                base_path,          # .MP4
                base_path + ".gpx", # .MP4.gpx
            ]
            for c in candidates:
                if os.path.exists(c):
                    if dry_run:
                        logger.info(f"[DRY RUN] Would remove {c}")
                        removed += 1
                    else:
                        try:
                            os.remove(c)
                            logger.info(f"Removed old file {c}")
                            removed += 1
                        except OSError as e:
                            logger.error(f"Error removing {c}: {e}")

    return removed


def cleanup_empty_dirs(root: str, dry_run: bool):
    """
    Recursively removes empty directories under root (bottom-up).
    Never removes root itself.
    Returns number of directories removed.
    """
    removed = 0
    root_abs = os.path.abspath(root)

    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if os.path.abspath(dirpath) == root_abs:
            continue
        if not dirnames and not filenames:
            if dry_run:
                logger.info(f"[DRY RUN] Would remove empty directory {dirpath}")
                removed += 1
            else:
                try:
                    os.rmdir(dirpath)
                    logger.info(f"Removed empty directory {dirpath}")
                    removed += 1
                except OSError as e:
                    # Directory may have become non-empty, or permissions/etc.
                    logger.debug(f"Could not remove {dirpath}: {e}")

    return removed


# -----------------------------------------------------------------------------
# Download logic
# -----------------------------------------------------------------------------
def download_file(base_url, recording, destination, subdir, timeout, dry_run):
    cleaned = recording.filepath.replace('A:', '').replace('\\', '/')
    url = f"{base_url}/{cleaned}"
    dest_dir = os.path.join(destination, subdir) if subdir else destination
    ensure_destination(dest_dir)
    final_path = os.path.join(dest_dir, recording.filename)

    try:
        expected_size = get_remote_size(url, timeout)
    except Exception as e:
        logger.warning(f"Could not HEAD {recording.filename}: {e}")
        expected_size = None

    if expected_size is not None and os.path.exists(final_path):
        if os.path.getsize(final_path) == expected_size:
            size_str, _ = human_size_and_speed(os.path.getsize(final_path), 1)
            logger.debug(f"Skipping complete file: {recording.filename} ({size_str})")
            return False, None

    if dry_run:
        logger.info(f"[DRY RUN] Would download {recording.filename}")
        return True, None

    tmp_fd, tmp_path = tempfile.mkstemp(dir=dest_dir, prefix=recording.filename, suffix=".part")
    os.close(tmp_fd)

    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
        try:
            logger.info(f"Downloading {recording.filename} (attempt {attempt})")
            start = time.perf_counter()
            with urllib.request.urlopen(url, timeout=timeout) as resp, open(tmp_path, "wb") as out:
                shutil.copyfileobj(resp, out)
            elapsed = time.perf_counter() - start
        except Exception as e:
            logger.warning(f"Attempt {attempt} failed: {e}")
            time.sleep(RETRY_BACKOFF * attempt)
        else:
            actual_size = os.path.getsize(tmp_path)
            if expected_size is not None and actual_size != expected_size:
                actual_str, _ = human_size_and_speed(actual_size, 1)
                expected_str, _ = human_size_and_speed(expected_size, 1)
                logger.error(
                    f"Incomplete download of {recording.filename}: {actual_str}/{expected_str}"
                )
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                time.sleep(RETRY_BACKOFF * attempt)
            else:
                size_str, speed_str = human_size_and_speed(actual_size, elapsed)
                os.replace(tmp_path, final_path)
                logger.info(f"Downloaded {recording.filename}: {size_str} in {elapsed:.1f}s ({speed_str})")
                return True, None

    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    logger.error(f"Failed to download {recording.filename} after {MAX_DOWNLOAD_ATTEMPTS} attempts")
    return False, None


# -----------------------------------------------------------------------------
# Sync
# -----------------------------------------------------------------------------
def sync(address, destination, download_priority, recording_filter, args):
    logger.info(f"Starting sync for {address}")
    base_url = f"http://{address}"

    try:
        recs = get_dashcam_filenames(base_url)
    except Exception as e:
        logger.error(f"Aborting sync: {e}")
        return False

    recs.sort(key=lambda r: r.datetime, reverse=(download_priority == "rdate"))

    if recording_filter:
        recs = [r for r in recs if any(f in r.filename for f in recording_filter)]
        logger.info(f"After filter: {len(recs)} recordings")

    total = len(recs)
    for i, rec in enumerate(recs, start=1):
        if cutoff_date and rec.datetime.date() < cutoff_date:
            continue

        subdir = os.path.join(
            str(rec.datetime.year),
            f"{rec.datetime.month:02d}",
            f"{rec.datetime.day:02d}"
        )
        local_dir = os.path.join(destination, subdir)
        ensure_destination(local_dir)
        local_path = os.path.join(local_dir, rec.filename)

        cleaned = rec.filepath.replace('A:', '').replace('\\', '/')
        url = f"{base_url}/{cleaned}"
        try:
            remote_size = get_remote_size(url, args.timeout)
        except Exception:
            remote_size = None

        if remote_size is not None and os.path.exists(local_path):
            if os.path.getsize(local_path) == remote_size:
                size_str, _ = human_size_and_speed(os.path.getsize(local_path), 1)
                logger.debug(f"Skipping unchanged file: {rec.filename} ({size_str})")
                continue

        logger.info(f"[{i}/{total}] Downloading {rec.filename} into {subdir}/")
        downloaded, _ = download_file(
            base_url, rec, destination, subdir,
            args.timeout, args.dry_run
        )
        if downloaded and args.gps_extract:
            extract_gps_data(os.path.join(local_dir, rec.filename))

    logger.info("Sync complete")
    return True


# -----------------------------------------------------------------------------
# GPS Extraction Functions
# -----------------------------------------------------------------------------
def fix_time(hour, minute, second, year, month, day):
    return f"{year + 2000:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"


def fix_coordinates(hemisphere, coordinate):
    minutes = coordinate % 100.0
    degrees = coordinate - minutes
    coordinate = degrees / 100.0 + (minutes / 60.0)
    return -1 * float(coordinate) if hemisphere in ['S', 'W'] else float(coordinate)


def fix_speed(speed):
    return speed * 0.514444


def get_atom_info(eight_bytes):
    try:
        atom_size, atom_type = struct.unpack('>I4s', eight_bytes)
        return int(atom_size), atom_type.decode()
    except (struct.error, UnicodeDecodeError):
        return 0, ''


def get_gps_atom_info(eight_bytes):
    atom_pos, atom_size = struct.unpack('>II', eight_bytes)
    return int(atom_pos), int(atom_size)


def get_gps_data(data):
    gps = {
        'DT': {
            'Year': None, 'Month': None, 'Day': None,
            'Hour': None, 'Minute': None, 'Second': None, 'DT': None
        },
        'Loc': {
            'Lat': {'Raw': None, 'Hemi': None, 'Float': None},
            'Lon': {'Raw': None, 'Hemi': None, 'Float': None},
            'Speed': None, 'Bearing': None,
        },
    }

    offset = 0
    hour, minute, second, year, month, day = struct.unpack_from('<IIIIII', data, offset)
    offset += 24
    active, lat_hemi, lon_hemi = struct.unpack_from('<ccc', data, offset)
    offset += 4
    lat_raw, lon_raw, speed, bearing = struct.unpack_from('<ffff', data, offset)

    gps['DT']['Hour'], gps['DT']['Minute'], gps['DT']['Second'] = hour, minute, second
    gps['DT']['Year'], gps['DT']['Month'], gps['DT']['Day'] = year, month, day
    gps['DT']['DT'] = fix_time(hour, minute, second, year, month, day)

    gps['Loc']['Lat']['Hemi'] = lat_hemi.decode()
    gps['Loc']['Lon']['Hemi'] = lon_hemi.decode()
    gps['Loc']['Lat']['Raw'] = lat_raw
    gps['Loc']['Lon']['Raw'] = lon_raw
    gps['Loc']['Lat']['Float'] = fix_coordinates(gps['Loc']['Lat']['Hemi'], gps['Loc']['Lat']['Raw'])
    gps['Loc']['Lon']['Float'] = fix_coordinates(gps['Loc']['Lon']['Hemi'], gps['Loc']['Lon']['Raw'])
    gps['Loc']['Speed'] = fix_speed(speed)
    gps['Loc']['Bearing'] = bearing

    return gps


def get_gps_atom(gps_atom_info, f):
    atom_pos, atom_size = gps_atom_info
    try:
        f.seek(atom_pos)
        data = f.read(atom_size)
    except OverflowError as e:
        logger.error(f"Skipping at {atom_pos:x}: seek or read error. Error: {str(e)}")
        return None

    expected_type, expected_magic = 'free', 'GPS '
    atom_size1, atom_type, magic = struct.unpack_from('>I4s4s', data)
    try:
        atom_type = atom_type.decode()
        magic = magic.decode()
        if atom_size != atom_size1 or atom_type != expected_type or magic != expected_magic:
            logger.error(
                f"Error! skipping atom at {atom_pos:x} "
                f"(expected size:{atom_size}, actual size:{atom_size1}, "
                f"expected type:{expected_type}, actual type:{atom_type}, "
                f"expected magic:{expected_magic}, actual magic:{magic})!"
            )
            return None
    except UnicodeDecodeError as e:
        logger.error(f"Skipping at {atom_pos:x}: garbage atom type or magic. Error: {str(e)}")
        return None

    return get_gps_data(data[12:])


def parse_moov(in_fh):
    gps_data = []
    offset = 0
    while True:
        atom_size, atom_type = get_atom_info(in_fh.read(8))
        if atom_size == 0:
            break

        if atom_type == 'moov':
            sub_offset = offset + 8
            while sub_offset < (offset + atom_size):
                sub_atom_size, sub_atom_type = get_atom_info(in_fh.read(8))

                if sub_atom_type == 'gps ':
                    gps_offset = 16 + sub_offset  # +16 = skip headers
                    in_fh.seek(gps_offset, 0)
                    while gps_offset < (sub_offset + sub_atom_size):
                        data = get_gps_atom(get_gps_atom_info(in_fh.read(8)), in_fh)
                        if data:
                            gps_data.append(data)
                        gps_offset += 8
                        in_fh.seek(gps_offset, 0)

                sub_offset += sub_atom_size
                in_fh.seek(sub_offset, 0)

        offset += atom_size
        in_fh.seek(offset, 0)
    return gps_data


def generate_gpx(gps_data, out_file):
    gpx = '<?xml version="1.0" encoding="UTF-8"?>\n'
    gpx += '<gpx version="1.0"\n'
    gpx += '\tcreator="Viofo GPS Extractor"\n'
    gpx += '\txmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
    gpx += '\txmlns="http://www.topografix.com/GPX/1/0"\n'
    gpx += '\txsi:schemaLocation="http://www.topografix.com/GPX/1/0 http://www.topografix.com/GPX/1/0/gpx.xsd">\n'
    gpx += f"\t<name>{out_file}</name>\n"
    gpx += f"\t<trk><name>{out_file}</name><trkseg>\n"
    for gps in gps_data:
        if gps:
            gpx += f"\t\t<trkpt lat=\"{gps['Loc']['Lat']['Float']}\" lon=\"{gps['Loc']['Lon']['Float']}\">"
            gpx += f"<time>{gps['DT']['DT']}</time>"
            gpx += f"<speed>{gps['Loc']['Speed']}</speed>"
            gpx += f"<course>{gps['Loc']['Bearing']}</course></trkpt>\n"
    gpx += '\t</trkseg></trk>\n'
    gpx += '</gpx>\n'
    return gpx


def extract_gps_data(file_path):
    logger.info(f"Extracting GPS data from {file_path}")

    with open(file_path, "rb") as in_fh:
        gps_data = parse_moov(in_fh)

    logger.info(f"Found {len(gps_data)} GPS data points.")

    if gps_data:
        gpx_file = file_path + ".gpx"
        gpx_content = generate_gpx(gps_data, os.path.basename(gpx_file))
        with open(gpx_file, "w") as f:
            logger.info(f"Writing GPS data to output file '{gpx_file}'.")
            f.write(gpx_content)
    else:
        logger.warning("No GPS data found in the file.")


# -----------------------------------------------------------------------------
# CLI args
# -----------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Sync Viofo dashcam recordings")
    p.add_argument("address", help="Dashcam IP/hostname")
    p.add_argument("-d", "--destination", default=os.getcwd(), help="Download directory")
    p.add_argument("-g", "--grouping", choices=["none", "daily", "weekly", "monthly", "yearly"], default="none")
    p.add_argument("-p", "--priority", choices=["date", "rdate"], default="date")
    p.add_argument("-f", "--filter", nargs="+", help="Filename substring filter")
    p.add_argument("-k", "--keep", help="Keep for <number>[d|w]")
    p.add_argument("-u", "--max-used-disk", type=int, choices=range(5, 99), default=90, metavar="DISK%")
    p.add_argument("-t", "--timeout", type=float, default=10.0, help="Timeout seconds")
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--gps-extract", action="store_true")
    p.add_argument("--run-once", action="store_true")
    p.add_argument("--monitor", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args()


# -----------------------------------------------------------------------------
# Monitor loop
# -----------------------------------------------------------------------------
def monitor_loop(address, destination, priority, recording_filter, args):
    sleep_time_s = 600
    base_url = f"http://{address}"
    list_url = f"{base_url}/?custom=1&cmd=3015&par=1"

    logger.info("Entering monitor loop (Ctrl+C to exit)")
    while True:
        if not is_camera_online(list_url, socket_timeout):
            logger.info(f"Retrying in {sleep_time_s} seconds")
            time.sleep(sleep_time_s)
            continue

        # Enforce retention each cycle
        if cutoff_date:
            removed_files = cleanup_old_files(destination, cutoff_date, args.dry_run)
            removed_dirs = cleanup_empty_dirs(destination, args.dry_run)
            if removed_files or removed_dirs:
                logger.info(
                    f"Cleanup removed {removed_files} files and {removed_dirs} empty directories this cycle"
                )

        try:
            recs = get_dashcam_filenames(base_url)
        except Exception as e:
            logger.warning(f"Failed to fetch file list: {e}; retrying in {sleep_time_s}s")
            time.sleep(sleep_time_s)
            continue

        recs.sort(key=lambda r: r.datetime, reverse=(priority == "rdate"))
        if recording_filter:
            recs = [r for r in recs if any(f in r.filename for f in recording_filter)]
            logger.info(f"After filter: {len(recs)} recordings")

        to_dl = []
        for rec in recs:
            if cutoff_date and rec.datetime.date() < cutoff_date:
                continue

            cleaned = rec.filepath.replace('A:', '').replace('\\', '/')
            url = f"{base_url}/{cleaned}"
            try:
                remote_size = get_remote_size(url, socket_timeout)
            except Exception:
                continue

            subdir = os.path.join(
                str(rec.datetime.year),
                f"{rec.datetime.month:02d}",
                f"{rec.datetime.day:02d}"
            )
            local_fp = os.path.join(destination, subdir, rec.filename)
            local_size = os.path.getsize(local_fp) if os.path.exists(local_fp) else -1

            if local_size != remote_size:
                to_dl.append((rec, subdir))

        if to_dl:
            total = len(to_dl)
            logger.info(f"{total} files to (re)download")
            for i, (rec, subdir) in enumerate(to_dl, start=1):
                if not is_camera_online(list_url, socket_timeout):
                    logger.info(f"Lost camera mid-batch; aborting downloads and retrying in {sleep_time_s}s")
                    break

                logger.info(f"[{i}/{total}] Downloading {rec.filename} into {subdir}/")
                downloaded, _ = download_file(
                    base_url, rec, destination, subdir,
                    args.timeout, args.dry_run
                )
                if downloaded and args.gps_extract:
                    extract_gps_data(os.path.join(destination, subdir, rec.filename))

                if i == total:
                    logger.info(f"All current files downloaded, sleeping for {sleep_time_s}s")
        else:
            logger.debug("All files up to date")

        time.sleep(sleep_time_s)


# -----------------------------------------------------------------------------
# Main entry
# -----------------------------------------------------------------------------
def run():
    global dry_run, cutoff_date, socket_timeout

    args = parse_args()
    socket_timeout = args.timeout
    socket.setdefaulttimeout(socket_timeout)

    if args.quiet:
        logger.setLevel(logging.ERROR)
    elif args.verbose:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    dry_run = args.dry_run

    if args.keep:
        m = re.fullmatch(r"(\d+)([dw]?)", args.keep)
        if not m:
            raise RuntimeError("KEEP format <number>[d|w]")

        n, unit = int(m.group(1)), (m.group(2) or "d")
        delta = datetime.timedelta(days=n if unit == "d" else 0,
                                   weeks=n if unit == "w" else 0)
        cutoff_date = datetime.date.today() - delta
        logger.info(f"Cutoff date: {cutoff_date}")

        removed_files = cleanup_old_files(args.destination, cutoff_date, dry_run)
        removed_dirs = cleanup_empty_dirs(args.destination, dry_run)
        logger.info(
            f"Cleanup removed {removed_files} files and {removed_dirs} empty directories older than cutoff"
        )

    if args.monitor:
        monitor_loop(args.address, args.destination, args.priority, args.filter, args)
        return 0

    success = sync(args.address, args.destination, args.priority, args.filter, args)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(run())
