"""Tests for the dashcam delete HTTP helper.

We mock urllib.request.urlopen so the tests don't hit a real
dashcam. The helper builds the URL — the assertions check we
hit /?custom=1&cmd=4003&str=<path>, and count a delete as made only when
the camera's reply says so.

The ``filepath`` argument is what the queue stores in ``source_dir``:
the clip's FULL remote path as the listing reported it
(``/DCIM/Movie/X.MP4`` in HTML mode, ``A:\\DCIM\\Movie\\X.MP4`` in XML
mode) — never a bare directory.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from viofosync_lib import dashcam_path, delete_dashcam_file

# The camera's answer to a delete it made (cmd 4003): Status 0. Any other
# Status is a delete it didn't make.
DELETED = (b'<?xml version="1.0" encoding="UTF-8" ?>\n<Function>\n<Cmd>4003</Cmd>\n'
           b'<Status>0</Status>\n</Function>\n')


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = DELETED) -> None:
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self, n: int = -1) -> bytes:
        return self.body


def test_delete_returns_true_on_success() -> None:
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req if isinstance(req, str) else req.full_url
        captured["timeout"] = timeout
        return _FakeResponse(status=200)

    with patch("urllib.request.urlopen", fake_urlopen):
        ok = delete_dashcam_file(
            "http://192.168.1.230",
            "/DCIM/Movie/2026_0508_104020_001234F.MP4",
            "2026_0508_104020_001234F.MP4",
            timeout=5.0,
        )
    assert ok is True
    assert captured["url"] == (
        "http://192.168.1.230/?custom=1&cmd=4003"
        "&str=/DCIM/Movie/2026_0508_104020_001234F.MP4"
    )
    assert captured["timeout"] == 5.0


@pytest.mark.parametrize("body, says", [
    # The camera says it didn't.
    (DELETED.replace(b"<Status>0<", b"<Status>-1<"), "the camera didn't delete it (Status -1)"),
    # Nothing in these says either way: they only don't confirm it.
    (b"", "the camera's reply doesn't confirm the delete (no Status)"),
    (b"<html><body>busybox</body></html>",
     "the camera's reply doesn't confirm the delete (no Status)"),
], ids=["status-1", "empty", "html"])
def test_a_200_counts_only_when_the_camera_says_deleted(body, says, caplog) -> None:
    import logging

    caplog.set_level(logging.WARNING, logger="viofosync_lib.delete")
    with patch("urllib.request.urlopen",
               lambda req, timeout=None: _FakeResponse(200, body)):
        ok = delete_dashcam_file(
            "http://192.168.1.230",
            "/DCIM/Movie/2026_0508_104020_001234F.MP4",
            "2026_0508_104020_001234F.MP4",
        )
    assert ok is False
    assert [r.getMessage() for r in caplog.records] == [
        f"dashcam delete 2026_0508_104020_001234F.MP4: {says}"]


def test_delete_returns_false_on_http_error() -> None:
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            "http://x", 500, "boom", {}, None,
        )

    with patch("urllib.request.urlopen", fake_urlopen):
        ok = delete_dashcam_file(
            "http://192.168.1.230",
            "/DCIM/Movie/2026_0508_104020_001234F.MP4",
            "2026_0508_104020_001234F.MP4",
        )
    assert ok is False


def test_delete_returns_false_on_url_error() -> None:
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", fake_urlopen):
        ok = delete_dashcam_file(
            "http://192.168.1.230",
            "/DCIM/Movie/2026_0508_104020_001234F.MP4",
            "2026_0508_104020_001234F.MP4",
        )
    assert ok is False


def test_delete_returns_false_on_socket_timeout() -> None:
    import socket

    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timeout")

    with patch("urllib.request.urlopen", fake_urlopen):
        ok = delete_dashcam_file(
            "http://192.168.1.230",
            "/DCIM/Movie/2026_0508_104020_001234F.MP4",
            "2026_0508_104020_001234F.MP4",
        )
    assert ok is False
    # Sanity: socket.timeout is a subclass of OSError in modern Python.
    assert issubclass(socket.timeout, OSError)


_NAME = "2026_1004_181400_041404F.MP4"


def _capture_urls():
    urls = []

    def fake_urlopen(req, timeout=None):
        urls.append(req if isinstance(req, str) else req.full_url)
        return _FakeResponse(status=200)

    return urls, fake_urlopen


@pytest.mark.parametrize("filepath, expected", [
    # HTML listing mode: the href, already absolute.
    (f"/DCIM/Movie/{_NAME}", f"/DCIM/Movie/{_NAME}"),
    (f"/DCIM/Movie/Parking/{_NAME}", f"/DCIM/Movie/Parking/{_NAME}"),
    # XML listing mode: FPATH, drive letter + backslashes (A: SD, B: SSD).
    (f"A:\\DCIM\\Movie\\{_NAME}", f"/DCIM/Movie/{_NAME}"),
    (f"B:\\DCIM\\Movie\\Parking\\{_NAME}", f"/DCIM/Movie/Parking/{_NAME}"),
])
def test_delete_sends_the_clips_own_path_once(filepath, expected) -> None:
    """The path is the listing filepath normalised — the filename is
    not appended a second time."""
    urls, fake_urlopen = _capture_urls()
    with patch("urllib.request.urlopen", fake_urlopen):
        ok = delete_dashcam_file("http://192.168.1.230", filepath, _NAME)
    assert ok is True
    assert urls == [
        f"http://192.168.1.230/?custom=1&cmd=4003&str={expected}"
    ]


@pytest.mark.parametrize("filepath", [
    "/import/",                              # bulk-import origin row
    "",                                      # no path recorded
    "/DCIM/Movie",                           # a directory, not a clip
    "/DCIM/Movie/2026_1004_181500_041405F.MP4",  # some other clip
])
def test_delete_refuses_a_path_that_is_not_this_clip(filepath, caplog) -> None:
    """No request is sent — the camera answers 200 to anything, so a
    wrong path would be reported as a successful delete."""
    import logging

    urls, fake_urlopen = _capture_urls()
    caplog.set_level(logging.WARNING, logger="viofosync_lib.delete")
    with patch("urllib.request.urlopen", fake_urlopen):
        ok = delete_dashcam_file("http://192.168.1.230", filepath, _NAME)
    assert ok is False
    assert urls == []
    assert any(_NAME in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("filepath, expected", [
    ("/DCIM/Movie/RO/X.MP4", "/DCIM/Movie/RO/X.MP4"),
    ("A:\\DCIM\\Movie\\RO\\X.MP4", "/DCIM/Movie/RO/X.MP4"),
    ("DCIM/Movie/X.MP4", "/DCIM/Movie/X.MP4"),
    ("", "/"),
])
def test_dashcam_path_normalises_both_listing_modes(filepath, expected) -> None:
    assert dashcam_path(filepath) == expected
