"""Support/debug bundle: a single redacted Markdown report users can
attach to GitHub issues.

Design: docs/superpowers/specs/2026-08-13-debug-bundle-design.md.
Every collector returns a plain dict and is individually try/excepted
by build_bundle() — a diagnostics tool must not fail because the thing
it is diagnosing is broken. All functions here are blocking; callers
on the event loop must use asyncio.to_thread.
"""
from __future__ import annotations

import datetime
import itertools
import os
import platform
import re
import shutil
import socket
import sys
import time
import urllib.request
from collections.abc import Iterable

from ..fsinfo import fstype_of
from ..version import display_version
from . import log_store

# Import-time stamp: approximates process start for the uptime line.
_STARTED = time.time()

# Settings keys whose values are network addresses and get mask_address()'d
# rather than shown raw. Shared with the Settings section collector; Task 7
# is expected to want the same notion elsewhere in the bundle.
_ADDRESS_KEYS = ("ADDRESS", "ADDRESS_FALLBACK", "MQTT_HOST")


def mask_address(value: str | None) -> str:
    """'192.0.2.10' -> '192.x.x.10'. Keeps first and last label
    so two cameras stay distinguishable without revealing the network.

    Also handles colon forms: a trailing ``:port`` (digits-only suffix,
    with a dotted or colon-free remainder) is split off and the host
    masked recursively, e.g. '192.0.2.10:8080' -> '192.x.x.10:8080'
    and 'dashcam.local:8080' -> 'dashcam.x:8080'. A bare IPv6-ish host
    (three or more ':'-separated groups, no unambiguous port suffix)
    gets the same first+x+last treatment over ':' instead of '.', e.g.
    'fd00::1' -> 'fd00:x:1'."""
    if not value:
        return ""
    if ":" in value:
        host, sep, suffix = value.rpartition(":")
        if sep and suffix.isdigit() and ("." in host or ":" not in host):
            return f"{mask_address(host)}:{suffix}"
        parts = value.split(":")
        if len(parts) >= 3:
            return ":".join([parts[0], *["x"] * (len(parts) - 2), parts[-1]])
        return value
    parts = value.split(".")
    if len(parts) >= 3:
        return ".".join([parts[0], *["x"] * (len(parts) - 2), parts[-1]])
    if len(parts) == 2:
        return f"{parts[0]}.x"
    return value


def redact_text(text: str, values: Iterable[str | None]) -> str:
    """Replace every configured address value with its mask."""
    for v in values:
        if v:
            text = text.replace(v, mask_address(v))
    return text


def _existing_ancestor(path: str) -> str:
    """Walk up from *path* to the nearest directory that actually
    exists, so disk_usage() doesn't blow up on a not-yet-created
    recordings dir or a hypothetical path used only for fstype lookup.
    The dirname-loop naturally bottoms out at "/", which always
    exists, so the `parent == p` guard is just belt-and-suspenders."""
    p = path
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p or os.sep


def collect_runtime(snap, *, encoders) -> dict:
    """The Runtime section: versions, uptime, disk headroom, mount type."""
    recordings = os.path.abspath(snap.recordings)
    du = shutil.disk_usage(_existing_ancestor(recordings))
    return {
        "app_version": display_version(),
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.machine()}",
        "uptime_s": int(time.time() - _STARTED),
        "disk_total_bytes": du.total,
        "disk_free_bytes": du.free,
        "recordings_fstype": fstype_of(recordings),
        "encoders": encoders,
    }


def collect_settings(snap) -> dict:
    """The Settings section: the sanitized editable-values projection
    with address-like values additionally masked, and all
    location-derived data (LOCATIONS, NOMINATIM_EMAIL) stripped —
    the bundle promises "no location data" in any section.
    """
    # Deliberate reuse of the Settings API's private projection so there
    # is exactly one sanitized-settings source; imported lazily to keep
    # this service module import-light and router-independent at load
    # time.
    from ..routers.settings import _editable_values
    values = dict(_editable_values(snap))
    for key in _ADDRESS_KEYS:
        if values.get(key):
            values[key] = mask_address(values[key])
    locations = values.get("LOCATIONS")
    if isinstance(locations, list):
        values["LOCATIONS"] = f"[{len(locations)} location(s) — redacted]"
    if values.get("NOMINATIM_EMAIL"):
        values["NOMINATIM_EMAIL"] = "[redacted]"
    return values


_SUSPECT_SQL = (
    "SELECT filename, source_dir, state, attempts, remote_size, "
    "       remote_complete, last_error, enqueued_at, last_attempt_at "
    "FROM download_queue "
    "WHERE state IN ('pending','failed','downloading') AND ("
    "  attempts > 0 "
    "  OR last_error IS NOT NULL "
    "  OR enqueued_at < ?) "
    "ORDER BY attempts DESC, enqueued_at ASC LIMIT 5"
)
# A row is worth surfacing when the downloader has tried and not
# succeeded (attempts), when it carries an error, or when it has sat
# queued for over a week. remote_complete is deliberately NOT a signal:
# run_recording_status_release sets it as normal operation on the
# newest-capture lens before the row has ever been picked up, so every
# healthy released row would match. last_error is the field that
# usually names the real fault (a permission or path error, say).


def collect_queue(db) -> dict:
    """The Queue/DB section: per-state counts, suspect rows (those
    stuck, erroring, or long-queued), table sizes.

    ``db_size_bytes`` is the main DB file only — the WAL sidecar
    (``<path>-wal``) is excluded.
    """
    week_ago = int(time.time()) - 7 * 86400
    with db.conn() as c:
        counts = {
            r["state"]: r["n"] for r in c.execute(
                "SELECT state, COUNT(*) AS n FROM download_queue "
                "GROUP BY state")
        }
        suspects = [dict(r) for r in c.execute(_SUSPECT_SQL, (week_ago,))]
        tables = {}
        for (name,) in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ):
            tables[name] = c.execute(
                f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        oldest = c.execute(
            "SELECT MIN(enqueued_at) FROM download_queue "
            "WHERE state='pending'").fetchone()[0]
    # db.path is a public attribute (see web/db.py) — use it directly
    # rather than the PRAGMA page_count*page_size fallback, which is
    # only needed when the path is private/unavailable.
    try:
        db_size_bytes = os.path.getsize(db.path)
    except OSError:
        db_size_bytes = None
    return {
        "counts": counts,
        "suspects": suspects,
        "tables": tables,
        "oldest_pending_age_s":
            None if oldest is None else int(time.time()) - oldest,
        "db_size_bytes": db_size_bytes,
    }


def _format_ts(ts) -> str:
    """Render an app_log epoch timestamp as ISO-8601 UTC; fall back to
    the raw value when it isn't a usable number (None, corrupt row, or
    an out-of-range float that overflows the C time functions)."""
    try:
        return (
            datetime.datetime.fromtimestamp(float(ts), datetime.UTC)
            .isoformat()
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return str(ts)


def collect_logs(db, *, redact_values, limit: int = 500) -> list[str]:
    """The Logs section: the app_log tail, oldest-first, address-redacted.

    Rows carrying a traceback (``exc_text``) get it appended as indented
    continuation lines on the same list entry, so ordering asserts still
    hold one-element-per-row while a bundle doesn't lose the traceback
    that makes a crash actionable.
    """
    rows = log_store.query_logs(db, min_levelno=0, limit=limit)
    rows.reverse()  # query returns newest-first; render oldest-first
    out = []
    for r in rows:
        line = (f"{_format_ts(r['ts'])} {r['level']:8s} "
                f"{r['logger']}: {r['message']}")
        exc_text = r.get("exc_text")
        if exc_text:
            line += "\n    " + "\n    ".join(exc_text.splitlines())
        out.append(redact_text(line, redact_values))
    return out


_SLACK = 2 * 1024 * 1024  # matches the downloader's overage allowance
# Assumes NAME/FPATH/SIZE child order (the app's real listing parser is
# an order-independent ET walk; this one isn't) — a regex is used here
# instead because we also need the raw response text for the </LIST>
# truncation check, and re-parsing twice (regex + ET) isn't worth it
# for a diagnostics probe.
_LISTING_RE = re.compile(
    r"<NAME>([^<]+)</NAME>\s*<FPATH>([^<]+)</FPATH>\s*<SIZE>(\d+)</SIZE>")


def _parse_address(address: str) -> tuple[str, int]:
    """Split ``host:port``. A bare IPv6 literal has two or more colons
    (e.g. ``fd00::1``), so anything other than exactly one colon with
    an all-digit suffix is treated as the whole string being the host
    on port 80. This keeps ``int("")`` from escaping into an uncaught
    ValueError; a bare IPv6 host then fails inside create_connection()
    as a clean OSError instead.

    This deliberately DIVERGES from sync_worker._probe_one for
    ``host:port`` values: that function treats the whole string as a
    hostname (and would report a camera configured as ``host:port`` as
    offline), while this probe actually parses out the port so the
    debug bundle can still reach a camera on a non-default port."""
    if address.count(":") == 1:
        host, suffix = address.split(":", 1)
        if suffix.isdigit():
            return host, int(suffix)
    return address, 80


def _head_size(url: str, timeout: float):
    """HEAD Content-Length, or (None, reason)."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            cl = resp.headers.get("Content-Length")
            return (int(cl) if cl else None), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _get_size(url: str, timeout: float):
    """Range-probe GET (``bytes=0-0``) — what a download would see,
    without pulling the file's body through the camera's single-
    threaded socket. A firmware that honours Range answers 206 with
    ``Content-Range: bytes 0-0/TOTAL``; one that ignores it answers
    200 with the ordinary Content-Length. We never read the body:
    closing a socket that still has unread buffered data makes the OS
    send an RST rather than a clean FIN, so the camera's in-flight
    write aborts almost immediately — reading even one byte first
    would enlarge the TCP window and triple the bytes a Range-blind
    camera pushes before the abort lands."""
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 206:
                m = re.search(r"/(\d+)$",
                               resp.headers.get("Content-Range", ""))
                if m:
                    return int(m.group(1)), None
                return None, "206 without parseable Content-Range"
            cl = resp.headers.get("Content-Length")
            return (int(cl) if cl else None), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def collect_camera(address: str, *, db_rows, timeout: float = 5.0,
                    listing_timeout: float = 60.0,
                    use_html_listing: bool = False,
                    budget_s: float = 45.0) -> dict:
    """The Camera section: live probe; ``db_rows`` are queue dicts with
    filename, source_dir and remote_size (the size recorded when the row
    was enqueued). Unreachable camera -> {"reachable": False} and
    nothing else.

    Per file it reports the four sizes the camera and the queue disagree
    about in practice — enqueued, current listing, HEAD, and a Range GET
    — and flags a spread wider than the downloader's slack. Firmwares do
    under-report a segment they are still writing, so a mismatch is
    expected for an in-progress clip and notable for a finished one.

    ``source_dir`` stores the FULL remote filepath including the
    filename (queue.py writes ``Recording.filepath`` verbatim, e.g.
    ``/DCIM/Movie/X.MP4`` in HTML-listing mode or
    ``A:\\DCIM\\Movie\\X.MP4`` in XML-listing mode) — rows whose
    cleaned path doesn't end with the filename have no camera-side
    path (import-origin rows) and are skipped rather than probed.

    ``budget_s`` bounds the wall-clock cost of the per-file probes;
    worst case is roughly ``budget_s`` plus one in-flight timeout,
    since the probe already underway when the deadline is crossed is
    allowed to finish before the loop notices.
    """
    host, port = _parse_address(address)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return {"reachable": False}

    deadline = time.monotonic() + budget_s
    base = f"http://{address}"
    out: dict = {"reachable": True}

    def _fetch(path, tmo=timeout):
        with urllib.request.urlopen(base + path, timeout=tmo) as r:
            return r.read().decode("utf-8", "replace")

    try:
        m = re.search(r"<String>([^<]+)</String>",
                       _fetch("/?custom=1&cmd=3012"))
        out["firmware"] = m.group(1) if m else "unknown"
    except Exception as e:
        out["firmware"] = f"{type(e).__name__}: {e}"
    try:
        pairs = dict(re.findall(
            r"<Cmd>(\d+)</Cmd>\s*<Status>(-?\d+)</Status>",
            _fetch("/?custom=1&cmd=3014")))
        v = pairs.get("2001")
        out["record_flag"] = None if v is None else int(v)
    except Exception as e:
        out["record_flag"] = None
        out["record_flag_error"] = f"{type(e).__name__}: {e}"

    mode = "html" if use_html_listing else "xml"
    listing_sizes: dict[str, int] = {}
    t0 = time.time()
    listing_tmo = min(listing_timeout, max(1.0, deadline - time.monotonic()))
    try:
        text = _fetch("/?custom=1&cmd=3015&par=1", tmo=listing_tmo)
        entries = _LISTING_RE.findall(text)
        listing_info = {
            "seconds": round(time.time() - t0, 1),
            "entries": len(entries),
            "complete": text.rstrip().endswith("</LIST>"),
            "configured_mode": mode,
        }
        if use_html_listing and (not listing_info["complete"] or not entries):
            # The app doesn't rely on this endpoint for its real
            # listing when HTML mode is configured — it's only probed
            # here for byte-exact sizes — so a failure to parse isn't
            # "the listing is truncated", just "no exact sizes today".
            listing_info["complete"] = None
            listing_info["note"] = (
                "the app uses the HTML listing; XML endpoint probed "
                "for byte-exact sizes only")
        for name, _fpath, size in entries:
            listing_sizes[name] = int(size)
        out["listing"] = listing_info
    except Exception as e:
        listing_info = {
            "error": f"{type(e).__name__}: {e}",
            "seconds": round(time.time() - t0, 1),
            "configured_mode": mode,
        }
        if use_html_listing:
            listing_info["complete"] = None
            listing_info["note"] = (
                "the app uses the HTML listing; XML endpoint probed "
                "for byte-exact sizes only")
        out["listing"] = listing_info

    files = []
    budget_exhausted = False
    for row in itertools.islice(db_rows, 8):
        if time.monotonic() >= deadline:
            budget_exhausted = True
            break
        cleaned = re.sub(r"^[A-Z]:", "", row["source_dir"]).replace(
            "\\", "/")
        if not cleaned.endswith("/" + row["filename"]):
            files.append({"filename": row["filename"],
                          "skipped": "no camera path"})
            continue
        url = f"{base}/{cleaned.lstrip('/')}"
        head, head_err = _head_size(url, timeout)
        got, get_err = _get_size(url, timeout)
        listing_size = listing_sizes.get(row["filename"])
        sizes = [s for s in (listing_size, head, got) if s is not None]
        files.append({
            "filename": row["filename"],
            "db_remote_size": row.get("remote_size"),
            "listing_size": listing_size,
            "head_size": head, "head_error": head_err,
            "get_size": got, "get_error": get_err,
            "mismatch": bool(sizes) and max(sizes) - min(sizes) > _SLACK,
        })
    out["files"] = files
    if budget_exhausted:
        out["files_budget_exhausted"] = True
    return out


def _fmt_bytes(n) -> str:
    """Human-readable byte count for the report; '-' for unknown."""
    if n is None:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0


def _kv_lines(d: dict) -> list[str]:
    return [f"- **{k}**: {v}" for k, v in d.items()]


def build_bundle(db, snap, *, encoders) -> str:
    """Render the full Markdown report. Blocking; run via to_thread.

    Every section is individually isolated: a collector failure renders
    as a 'section failed' line and the rest of the bundle survives. The
    final redact_text pass over the whole document is belt-and-braces on
    top of per-section masking (it is what keeps a future section that
    forgets to redact from leaking configured addresses).
    """
    redact_values = [
        getattr(snap, "address", ""),
        getattr(snap, "address_fallback", ""),
        getattr(snap, "mqtt_host", ""),
    ]
    now = datetime.datetime.now(datetime.UTC)
    lines = [
        "# viofosync debug bundle",
        "",
        f"- generated: {now.isoformat(timespec='seconds')}",
        f"- version: {display_version()}",
        f"- instance: {getattr(snap, 'instance_name', '') or '-'}",
        "",
        "> This report is masked for public sharing (no secrets, no "
        "location data, network addresses partially hidden).",
        "",
    ]

    def _section(title, fn):
        lines.append(f"## {title}")
        lines.append("")
        try:
            fn()
        except Exception as e:  # isolation: partial bundles are normal
            lines.append(f"section failed: {type(e).__name__}: {e}")
        lines.append("")

    def _runtime():
        rt = collect_runtime(snap, encoders=encoders)
        # Rebuild rather than pop-and-append, so disk_total/disk_free
        # keep the collector's original field position instead of
        # jumping to the end of the list.
        formatted = {}
        for k, v in rt.items():
            if k == "disk_total_bytes":
                formatted["disk_total"] = _fmt_bytes(v)
            elif k == "disk_free_bytes":
                formatted["disk_free"] = _fmt_bytes(v)
            else:
                formatted[k] = v
        lines.extend(_kv_lines(formatted))

    def _settings():
        lines.extend(_kv_lines(collect_settings(snap)))

    # Populated by _queue() and reused by _camera() so the two sections
    # share one collect_queue() call/table-scan instead of taking two
    # inconsistent snapshots. Stays None if the Queue section never ran
    # or its collect_queue() call failed.
    queue_snapshot = None

    def _queue():
        nonlocal queue_snapshot
        q = collect_queue(db)
        queue_snapshot = q
        lines.extend(_kv_lines({
            "counts": q["counts"], "tables": q["tables"],
            "oldest_pending_age_s": q["oldest_pending_age_s"],
            "db_size": _fmt_bytes(q["db_size_bytes"]),
        }))
        for s in q["suspects"]:
            lines.append(
                f"- suspect: `{s['filename']}` state={s['state']} "
                f"attempts={s['attempts']} "
                f"remote_size={s['remote_size']} "
                f"remote_complete={s['remote_complete']} "
                f"last_attempt_at={s['last_attempt_at']} "
                f"last_error={s['last_error']}")

    def _logs():
        # Render the body BEFORE the opening fence: if collect_logs()
        # raises, _section's except clause catches it and nothing but
        # a plain "section failed" line is appended — no dangling open
        # fence that would swallow the Camera section into a code
        # block. Four backticks so a log message containing its own
        # ``` can't break out of the fence either.
        body = collect_logs(db, redact_values=redact_values)
        lines.append("````")
        lines.extend(body)
        lines.append("````")

    def _camera():
        address = getattr(snap, "address", "") or ""
        if not address:
            lines.append("no camera address configured")
            return
        if queue_snapshot is None:
            rows = []
            lines.append(
                "queue snapshot unavailable — per-file compare skipped")
        else:
            rows = queue_snapshot["suspects"]
        cam = collect_camera(
            address,
            db_rows=rows,
            use_html_listing=getattr(snap, "use_html_listing", True),
            # Cap at 15s regardless of the configured TIMEOUT (schema
            # allows up to 60s): the camera section makes several of
            # these probes back-to-back, and a 60s-per-request budget
            # would blow the bundle's minute-scale time promise. 15s
            # is plenty of headroom for a LAN camera.
            timeout=min(float(getattr(snap, "timeout", 5.0) or 5.0), 15.0),
        )
        if not cam.get("reachable"):
            lines.append("camera unreachable — live probe skipped")
            return
        record_flag = cam["record_flag"]
        if record_flag is None and cam.get("record_flag_error"):
            record_flag = f"None ({cam['record_flag_error']})"
        lines.extend(_kv_lines({
            "firmware": cam["firmware"],
            "record_flag": record_flag,
            "listing": cam["listing"],
        }))
        for f in cam["files"]:
            if "skipped" in f:
                lines.append(f"- `{f['filename']}`: {f['skipped']}")
                continue
            flag = " **MISMATCH**" if f["mismatch"] else ""
            head_repr = f["head_size"]
            if head_repr is None and f.get("head_error"):
                head_repr = f"None ({f['head_error']})"
            get_repr = f["get_size"]
            if get_repr is None and f.get("get_error"):
                get_repr = f"None ({f['get_error']})"
            lines.append(
                f"- `{f['filename']}` db={f['db_remote_size']} "
                f"listing={f['listing_size']} head={head_repr} "
                f"get={get_repr}{flag}")
        if cam.get("files_budget_exhausted"):
            lines.append("- (probe budget exhausted; remaining files "
                         "not checked)")

    _section("Runtime", _runtime)
    _section("Settings", _settings)
    _section("Queue / DB", _queue)
    _section("Logs (last 500)", _logs)
    _section("Camera", _camera)
    return redact_text("\n".join(lines), redact_values)
