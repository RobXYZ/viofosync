"""VideoFileResponse stops reading when the client hangs up.

Starlette's FileResponse kept reading an abandoned range to its end. Safari
abandons an open-ended ``Range: bytes=N-`` request roughly every 0.6 s during
playback, so each one read the rest of a multi-GB clip and the disk was
swamped with reads nobody would receive.
"""
from __future__ import annotations

import os

import anyio

from web.responses import VideoFileResponse

_SIZE = 4 * 1024 * 1024  # 64 chunks of FileResponse's 64 KiB


def _scope(range_header: bytes | None = b"bytes=0-") -> dict:
    headers = [(b"range", range_header)] if range_header else []
    return {"type": "http", "method": "GET", "path": "/", "headers": headers}


def _clip(tmp_path) -> str:
    path = tmp_path / "clip.mp4"
    path.write_bytes(os.urandom(_SIZE))
    return str(path)


async def test_stops_reading_after_client_disconnects(tmp_path):
    hung_up = anyio.Event()
    body = bytearray()

    async def receive():
        await hung_up.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            body.extend(message.get("body", b""))
            if len(body) >= 256 * 1024:
                hung_up.set()

    with anyio.fail_after(5):
        await VideoFileResponse(_clip(tmp_path), media_type="video/mp4")(
            _scope(), receive, send
        )
    assert 256 * 1024 <= len(body) < _SIZE


async def test_sends_everything_while_client_stays(tmp_path):
    path = _clip(tmp_path)
    messages: list[dict] = []
    first = True

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await anyio.sleep_forever()

    async def send(message):
        messages.append(message)

    with anyio.fail_after(5):
        await VideoFileResponse(path, media_type="video/mp4")(_scope(), receive, send)

    assert messages[0]["status"] == 206
    body = b"".join(m.get("body", b"") for m in messages[1:])
    with open(path, "rb") as f:
        assert body == f.read()
    assert messages[-1].get("more_body", False) is False


async def test_small_range_is_exact(tmp_path):
    path = _clip(tmp_path)
    messages: list[dict] = []

    async def receive():
        await anyio.sleep_forever()

    async def send(message):
        messages.append(message)

    with anyio.fail_after(5):
        await VideoFileResponse(path, media_type="video/mp4")(
            _scope(b"bytes=100-199"), receive, send
        )

    headers = dict(messages[0]["headers"])
    assert headers[b"content-range"] == f"bytes 100-199/{_SIZE}".encode()
    body = b"".join(m.get("body", b"") for m in messages[1:])
    with open(path, "rb") as f:
        f.seek(100)
        assert body == f.read(100)
