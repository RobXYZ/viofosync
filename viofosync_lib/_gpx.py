"""MP4 atom parsing + GPX generation for Viofo recordings.

GPS extraction method by Sergei Franco. Walks the ``moov`` atom
to find the ``gps `` sub-atoms, decodes each fix into a Python
dict, runs a 5-point median outlier filter to drop spikes, and
emits a GPX 1.0 string.
"""
from __future__ import annotations

import datetime
import logging
import math
import os
import struct

logger = logging.getLogger("viofosync_lib.gpx")


class IncompleteRecording(Exception):
    """A clip has no reachable top-level ``moov`` atom — the camera is still
    recording it (or it was truncated). Raised by the remote GPS path so the
    caller can defer the clip instead of caching a bogus "no GPS" result."""


# GPS sanity bounds for the spike filter.
GPS_MAX_REASONABLE_SPEED_MPS = 85.0
GPS_OUTLIER_MIN_JUMP_M = 2000.0
GPS_OUTLIER_RETURN_RADIUS_M = 150.0


def fix_time(hour, minute, second, year, month, day):
    return (
        f"{year + 2000:04d}-{month:02d}-{day:02d}"
        f"T{hour:02d}:{minute:02d}:{second:02d}Z"
    )


def fix_coordinates(hemisphere, coordinate):
    minutes = coordinate % 100.0
    degrees = coordinate - minutes
    coordinate = degrees / 100.0 + (minutes / 60.0)
    if hemisphere in ['S', 'W']:
        return -1 * float(coordinate)
    return float(coordinate)


def fix_speed(speed):
    return speed * 0.514444


def gps_point_time(gps):
    try:
        return datetime.datetime.strptime(
            gps['DT']['DT'], "%Y-%m-%dT%H:%M:%SZ"
        )
    except (KeyError, TypeError, ValueError):
        return None


def gps_point_lat_lon(gps):
    try:
        return (
            float(gps['Loc']['Lat']['Float']),
            float(gps['Loc']['Lon']['Float']),
        )
    except (KeyError, TypeError, ValueError):
        return None


def gps_haversine(a, b):
    """Great-circle distance in metres between two GPS dicts."""
    a_coords = gps_point_lat_lon(a)
    b_coords = gps_point_lat_lon(b)
    if a_coords is None or b_coords is None:
        return 0.0

    lat1, lon1 = a_coords
    lat2, lon2 = b_coords
    r = 6371000.0
    lat1 = math.radians(lat1)
    lat2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2)
         * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(h))


def filter_gps_spikes(gps_data):
    """Drop GPS fixes that lie far from the median of their
    5-point neighbourhood.

    A 5-point window survives pairs of consecutive spikes —
    with median-of-3, two neighbouring bad fixes outvote the
    one good neighbour and both slip through. At 5 points the
    three good fixes dominate."""
    valid = []
    for gps in gps_data:
        if not gps:
            continue
        if gps_point_time(gps) is None:
            continue
        if gps_point_lat_lon(gps) is None:
            continue
        valid.append(gps)

    n = len(valid)
    if n < 5:
        # Fall back to median-of-3 for very short tracks so
        # we still catch lone spikes at the edges.
        if n < 3:
            return valid

    threshold_m = GPS_OUTLIER_RETURN_RADIUS_M
    r = 6371000.0
    half = 2 if n >= 5 else 1  # window radius

    filtered = []
    skipped = 0
    for idx, curr in enumerate(valid):
        # Clamp the window so the size stays constant even at
        # the ends of the track — boundary outliers get voted
        # down by the interior instead of getting a free pass.
        center = min(max(idx, half), n - 1 - half)
        window = valid[center - half : center + half + 1]

        lats = sorted(gps_point_lat_lon(p)[0] for p in window)
        lons = sorted(gps_point_lat_lon(p)[1] for p in window)
        median_lat = lats[len(lats) // 2]
        median_lon = lons[len(lons) // 2]

        curr_lat, curr_lon = gps_point_lat_lon(curr)
        phi1 = math.radians(curr_lat)
        phi2 = math.radians(median_lat)
        dphi = math.radians(median_lat - curr_lat)
        dlam = math.radians(median_lon - curr_lon)
        h = (math.sin(dphi / 2) ** 2
             + math.cos(phi1) * math.cos(phi2)
             * math.sin(dlam / 2) ** 2)
        dist = 2 * r * math.asin(math.sqrt(h))

        if dist > threshold_m:
            skipped += 1
            continue
        filtered.append(curr)

    if skipped:
        logger.info(f"Skipped {skipped} GPS spike(s)")
    return filtered


def get_atom_info(eight_bytes):
    try:
        atom_size, atom_type = struct.unpack('>I4s', eight_bytes)
        return int(atom_size), atom_type.decode()
    except (struct.error, UnicodeDecodeError):
        return 0, ''


def get_gps_atom_info(eight_bytes):
    atom_pos, atom_size = struct.unpack('>II', eight_bytes)
    return int(atom_pos), int(atom_size)


def get_gps_offset(data):
    """Finds GPS payload position by scanning for A{N,S}{E,W}
    pattern. Supports newer VIOFO cameras (e.g. A329S) where
    GPS data sits at a variable offset within the payload."""
    pointer = len(data) - 20
    while pointer > 0:
        try:
            active, lon_hemi, lat_hemi = struct.unpack_from(
                '<sss', data, pointer
            )
            active = active.decode()
            lon_hemi = lon_hemi.decode()
            lat_hemi = lat_hemi.decode()
        except UnicodeDecodeError:
            pointer -= 1
            continue
        if (active == 'A'
                and lon_hemi in ('N', 'S')
                and lat_hemi in ('E', 'W')):
            return pointer - 24
        pointer -= 1
    return -1


def get_gps_data(data):
    gps = {
        'DT': {
            'Year': None, 'Month': None, 'Day': None,
            'Hour': None, 'Minute': None, 'Second': None,
            'DT': None,
        },
        'Loc': {
            'Lat': {'Raw': None, 'Hemi': None, 'Float': None},
            'Lon': {'Raw': None, 'Hemi': None, 'Float': None},
            'Speed': None, 'Bearing': None,
        },
    }

    offset = get_gps_offset(data)
    if offset < 0:
        return None

    try:
        hour, minute, second = struct.unpack_from(
            '<III', data, offset
        )
        offset += 12
        year, month, day = struct.unpack_from(
            '<III', data, offset
        )
        offset += 12
        _, lat_hemi, lon_hemi = struct.unpack_from(
            '<sss', data, offset
        )
        offset += 4
        lat_raw, lon_raw = struct.unpack_from(
            '<ff', data, offset
        )
        offset += 8
        speed, bearing = struct.unpack_from(
            '<ff', data, offset
        )

        gps['Loc']['Lat']['Hemi'] = lat_hemi.decode()
        gps['Loc']['Lon']['Hemi'] = lon_hemi.decode()
    except (struct.error, UnicodeDecodeError) as e:
        logger.debug(f"Skipping: bad GPS data. Error: {e}")
        return None

    gps['DT']['Hour'] = hour
    gps['DT']['Minute'] = minute
    gps['DT']['Second'] = second
    gps['DT']['Year'] = year
    gps['DT']['Month'] = month
    gps['DT']['Day'] = day
    gps['DT']['DT'] = fix_time(
        hour, minute, second, year, month, day
    )

    gps['Loc']['Lat']['Raw'] = lat_raw
    gps['Loc']['Lon']['Raw'] = lon_raw
    gps['Loc']['Lat']['Float'] = fix_coordinates(
        gps['Loc']['Lat']['Hemi'], lat_raw
    )
    gps['Loc']['Lon']['Float'] = fix_coordinates(
        gps['Loc']['Lon']['Hemi'], lon_raw
    )
    gps['Loc']['Speed'] = fix_speed(speed)
    gps['Loc']['Bearing'] = bearing

    return gps


def get_gps_atom(gps_atom_info, f):
    atom_pos, atom_size = gps_atom_info
    try:
        f.seek(atom_pos)
        data = f.read(atom_size)
    except OverflowError as e:
        logger.error(
            f"Skipping at {atom_pos:x}: "
            f"seek or read error: {e}"
        )
        return None

    if len(data) < 12:
        logger.debug(
            f"Skipping at {atom_pos:x}: "
            f"atom too small ({len(data)} bytes)"
        )
        return None

    expected_type, expected_magic = 'free', 'GPS '
    atom_size1, atom_type, magic = struct.unpack_from(
        '>I4s4s', data
    )
    try:
        atom_type = atom_type.decode()
        magic = magic.decode()
        if (atom_size != atom_size1
                or atom_type != expected_type
                or magic != expected_magic):
            logger.error(
                f"Skipping atom at {atom_pos:x} "
                f"(size:{atom_size1}/{atom_size}, "
                f"type:{atom_type}/{expected_type}, "
                f"magic:{magic}/{expected_magic})"
            )
            return None
    except UnicodeDecodeError as e:
        logger.error(
            f"Skipping at {atom_pos:x}: "
            f"garbage atom type or magic: {e}"
        )
        return None

    return get_gps_data(data[12:])


def has_final_moov(in_fh) -> bool:
    """True if a top-level ``moov`` atom is reachable by walking the atom
    chain from the start.

    WARNING — this does NOT prove a clip is finalized on current A229/A329
    firmware. Field-verified 2026-07-02: these cameras write the ``moov`` at
    the FRONT (``ftyp,skip,moov,skip,mdat``), inside a fixed ~65 KB reserved
    region, and keep growing it in place while recording — so a
    still-recording clip has a reachable, populated ``moov`` and this returns
    True mid-recording. Use the camera's ``cmd=3014`` record flag
    (``viofosync_lib._control.record_state``) to decide "is it still
    recording?"; see the active-recording guard docs. What this function
    genuinely tells you: a ``moov`` atom is present and walkable (still useful
    for the GPS path — no ``moov`` means no GPS box to parse yet). A
    short/garbage/zero-size header returns False (treat "can't confirm" as
    not-final), as does a ``TruncatedRead`` from the reader."""
    from ._protocol import TruncatedRead
    try:
        while True:
            header = in_fh.read(8)
            if len(header) < 8:
                return False
            atom_size, atom_type = get_atom_info(header)
            if atom_type == 'moov':
                return True
            # size==1 (64-bit extended size) also lands here and returns
            # False. Parity with parse_moov: these cameras don't emit 64-bit
            # atoms (clips are well under 4 GB).
            if atom_size < 8:            # zero/garbage/64-bit header — stop
                return False
            in_fh.seek(atom_size - 8, 1)  # skip this atom's payload
    except TruncatedRead:
        return False


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
                sub_atom_size, sub_atom_type = get_atom_info(
                    in_fh.read(8)
                )
                # An atom can't be smaller than its own 8-byte
                # header. A zero/short size here (truncated
                # power-loss clip) used to loop forever re-reading
                # the same offset.
                if sub_atom_size < 8:
                    logger.warning(
                        "corrupt atom (size %d) at offset %d; "
                        "stopping GPS walk", sub_atom_size, sub_offset
                    )
                    break

                if sub_atom_type == 'gps ':
                    gps_offset = 16 + sub_offset
                    in_fh.seek(gps_offset, 0)
                    while gps_offset < (sub_offset
                                        + sub_atom_size):
                        data = get_gps_atom(
                            get_gps_atom_info(in_fh.read(8)),
                            in_fh,
                        )
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
    gps_data = filter_gps_spikes(gps_data)
    gpx = '<?xml version="1.0" encoding="UTF-8"?>\n'
    gpx += '<gpx version="1.0"\n'
    gpx += '\tcreator="Viofo GPS Extractor"\n'
    gpx += '\txmlns:xsi='
    gpx += '"http://www.w3.org/2001/XMLSchema-instance"\n'
    gpx += '\txmlns="http://www.topografix.com/GPX/1/0"\n'
    gpx += (
        '\txsi:schemaLocation='
        '"http://www.topografix.com/GPX/1/0 '
        'http://www.topografix.com/GPX/1/0/gpx.xsd">\n'
    )
    gpx += f"\t<name>{out_file}</name>\n"
    gpx += f"\t<trk><name>{out_file}</name><trkseg>\n"
    for gps in gps_data:
        if gps:
            lat = gps['Loc']['Lat']['Float']
            lon = gps['Loc']['Lon']['Float']
            gpx += f'\t\t<trkpt lat="{lat}" lon="{lon}">'
            gpx += f"<time>{gps['DT']['DT']}</time>"
            gpx += f"<speed>{gps['Loc']['Speed']}</speed>"
            gpx += (
                f"<course>{gps['Loc']['Bearing']}</course>"
                f"</trkpt>\n"
            )
    gpx += '\t</trkseg></trk>\n'
    gpx += '</gpx>\n'
    return gpx


def extract_gps_data(file_path):
    logger.info(f"Extracting GPS data from {file_path}")

    with open(file_path, "rb") as in_fh:
        gps_data = parse_moov(in_fh)

    logger.info(f"Found {len(gps_data)} GPS data points")

    if gps_data:
        gpx_file = file_path + ".gpx"
        gpx_content = generate_gpx(
            gps_data, os.path.basename(gpx_file)
        )
        with open(gpx_file, "w") as f:
            logger.info(f"Writing GPS data to '{gpx_file}'")
            f.write(gpx_content)
    else:
        logger.warning("No GPS data found in the file")
