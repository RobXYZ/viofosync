"""Camera section: reachability gate + the size cross-check
(listing SIZE vs HEAD vs GET Content-Length)."""
from __future__ import annotations

import http.server
import threading

import pytest

from web.services import debug_bundle as dbb

LISTING_XML = """<?xml version="1.0"?><LIST>
<ALLFile><File><NAME>A.MP4</NAME><FPATH>A:\\DCIM\\Movie\\A.MP4</FPATH>
<SIZE>62914560</SIZE><TIMECODE>1</TIMECODE><TIME>2026/06/28 13:34:16</TIME>
<ATTR>32</ATTR></File></ALLFile></LIST>"""

TRUE_SIZE = 714 * 1024 * 1024  # what HEAD/GET report; listing says 60MB
_HUGE = 10 * 1024 * 1024 * 1024  # advertised size from a Range-blind firmware


@pytest.fixture()
def camera():
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _serve(self, body: bytes):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if "cmd=3015" in self.path:
                return self._serve(LISTING_XML.encode())
            if "cmd=3012" in self.path:
                return self._serve(b"<String>VIOFO_TEST_V1.0</String>")
            if "cmd=3014" in self.path:
                return self._serve(b"<Cmd>2001</Cmd><Status>0</Status>")
            if self.path.endswith("A.MP4"):
                if "Range" in self.headers:
                    # Real firmware honours the bytes=0-0 probe.
                    self.send_response(206)
                    self.send_header("Content-Range",
                                      f"bytes 0-0/{TRUE_SIZE}")
                    self.send_header("Content-Length", "1")
                    self.end_headers()
                    self.wfile.write(b"x")
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", str(TRUE_SIZE))
                    self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def do_HEAD(self):
            if self.path.endswith("A.MP4"):
                self.send_response(200)
                self.send_header("Content-Length", str(TRUE_SIZE))
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def camera_range_ignored():
    """A firmware that ignores Range entirely and always streams a
    (falsely) huge Content-Length body — exercises both the plain-200
    fallback branch of ``_get_size`` and the early-abort behaviour
    that keeps an 8-file bundle from pulling gigabytes through a
    single-threaded camera."""
    written = {"n": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.endswith("A.MP4"):
                self.send_response(200)
                self.send_header("Content-Length", str(_HUGE))
                self.end_headers()
                chunk = b"x" * 65536
                try:
                    for _ in range(200000):  # would be ~13GB unbounded
                        self.wfile.write(chunk)
                        written["n"] += len(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return
            self.send_response(404)
            self.end_headers()

        def do_HEAD(self):
            self.send_response(404)
            self.end_headers()

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"127.0.0.1:{srv.server_address[1]}", written
    srv.shutdown()
    srv.server_close()


def test_camera_offline_returns_skip_note():
    out = dbb.collect_camera("127.0.0.1:1", db_rows=[], timeout=0.3)
    assert out["reachable"] is False


def test_camera_offline_ipv6_literal_does_not_raise():
    # A bare IPv6 literal has two colons; the naive host:port split
    # used to feed "" into int() and escape the except OSError guard.
    out = dbb.collect_camera("fd00::1", db_rows=[], timeout=0.3)
    assert out["reachable"] is False


def test_camera_cross_check_flags_stale_listing(camera):
    rows = [{"filename": "A.MP4", "source_dir": "/DCIM/Movie/A.MP4",
             "remote_size": 62914560}]
    out = dbb.collect_camera(camera, db_rows=rows, timeout=3.0)
    assert out["reachable"] is True
    assert out["firmware"] == "VIOFO_TEST_V1.0"
    assert out["record_flag"] == 0
    assert out["listing"]["complete"] is True
    assert out["listing"]["configured_mode"] == "xml"
    f = out["files"][0]
    assert f["listing_size"] == 62914560
    assert f["head_size"] == TRUE_SIZE
    assert f["get_size"] == TRUE_SIZE
    assert f["mismatch"] is True


def test_camera_url_handles_both_listing_path_shapes(camera):
    # source_dir stores the FULL remote filepath including the
    # filename (queue.py writes Recording.filepath verbatim) — both
    # the HTML-mode ("/DCIM/...") and XML-mode ("A:\\DCIM\\...") shapes
    # must resolve to the identical URL and both get probed.
    rows = [
        {"filename": "A.MP4", "source_dir": "/DCIM/Movie/A.MP4"},
        {"filename": "A.MP4", "source_dir": "A:\\DCIM\\Movie\\A.MP4"},
    ]
    out = dbb.collect_camera(camera, db_rows=rows, timeout=3.0)
    assert len(out["files"]) == 2
    for f in out["files"]:
        assert "skipped" not in f
        assert f["head_size"] == TRUE_SIZE


def test_camera_skips_row_with_no_camera_path(camera):
    # Import-origin rows carry no camera-side path.
    rows = [{"filename": "A.MP4", "source_dir": ""}]
    out = dbb.collect_camera(camera, db_rows=rows, timeout=3.0)
    f = out["files"][0]
    assert f["skipped"] == "no camera path"
    assert "head_size" not in f


def test_camera_html_mode_downgrades_truncation_to_note():
    # cmd=3015 unreachable while the app is configured for HTML
    # listing: the XML probe only exists for byte-exact sizes, so a
    # failure there isn't "the listing is truncated" (complete=False)
    # — it's "couldn't get exact sizes this time" (complete=None).
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def do_HEAD(self):
            self.send_response(404)
            self.end_headers()

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    address = f"127.0.0.1:{srv.server_address[1]}"
    try:
        out = dbb.collect_camera(address, db_rows=[], timeout=3.0,
                                  use_html_listing=True)
        assert out["listing"]["configured_mode"] == "html"
        assert out["listing"]["complete"] is None
        assert "note" in out["listing"]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5.0)


def test_camera_range_ignored_falls_back_and_aborts_early(
        camera_range_ignored):
    address, written = camera_range_ignored
    rows = [{"filename": "A.MP4", "source_dir": "/DCIM/Movie/A.MP4"}]
    out = dbb.collect_camera(address, db_rows=rows, timeout=3.0)
    f = out["files"][0]
    assert f["get_size"] == _HUGE
    assert written["n"] < 1024 * 1024


def test_camera_budget_exhausted_stops_remaining_files(camera):
    rows = [{"filename": "A.MP4", "source_dir": "/DCIM/Movie/A.MP4"}] * 3
    out = dbb.collect_camera(camera, db_rows=rows, timeout=3.0, budget_s=0.0)
    assert out["files_budget_exhausted"] is True
    assert out["files"] == []
