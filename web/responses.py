"""Response classes shared by the routers."""
from __future__ import annotations

import logging
import os
from urllib.parse import quote

import anyio
from fastapi import Request
from starlette.responses import FileResponse, RedirectResponse, Response
from starlette.types import Receive, Scope, Send

log = logging.getLogger("viofosync.responses")

# Both must match nginx/viofosync.conf: the header it sets on requests it
# passes to the app, and where it serves the recordings folder.
MEDIA_HEADER = "X-Viofosync-Media"
MEDIA_PREFIX = "/media/"


class VideoFileResponse(FileResponse):
    """A ``FileResponse`` that stops reading the file when the client hangs up.

    Starlette's ``FileResponse`` streams the whole requested range to the end
    even after the client has disconnected: uvicorn quietly discards the
    writes, but every chunk is still read from disk. Browsers abandon video
    requests constantly, and Safari above all: during playback it opens a
    fresh open-ended ``Range: bytes=N-`` request roughly every 0.6 s and drops
    the previous one after reading a few MB. Each abandoned request then kept
    reading the rest of a multi-GB dashcam clip, so dozens of them ended up
    competing for the disk (a slow one on Docker Desktop's file sharing) with
    the reads playback actually needed, and Safari buffered constantly.

    This listens for ``http.disconnect`` alongside the transfer, the same way
    Starlette's ``StreamingResponse`` does, and cancels the transfer when it
    arrives.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with anyio.create_task_group() as tg:

            async def transfer() -> None:
                await FileResponse.__call__(self, scope, receive, send)
                tg.cancel_scope.cancel()

            tg.start_soon(transfer)
            while (await receive())["type"] != "http.disconnect":
                pass
            tg.cancel_scope.cancel()


def media_link(rel_path: str, filename: str | None = None) -> str:
    """The /media/ URL nginx serves ``rel_path`` (relative to the
    recordings folder) at.

    With ``filename``, nginx sends the file as a download with that name.
    It travels percent-encoded in ``name=``, which is exactly the form
    ``Content-Disposition: attachment; filename*=UTF-8''…`` wants, so
    nginx passes it through without decoding it. nginx ignores a name
    with anything but those characters in it.
    """
    url = MEDIA_PREFIX + quote(rel_path)
    if filename:
        url += "?name=" + quote(filename, safe="")
    return url


# Where nginx/viofosync.conf serves /media/ from, in nginx's container.
NGINX_ROOT = "/recordings"

_warned_outside = False
_noted_root = False


def video_response(
    request: Request,
    path: str,
    recordings: str,
    filename: str | None = None,
) -> Response:
    """Serve a video, or send the browser to nginx for it.

    nginx/viofosync.conf marks every request it passes on with the
    ``X-Viofosync-Media`` header, overwriting any the browser sent. Then
    the reply is a redirect to the file's /media/ URL. Browsers keep
    using the URL they were redirected to for every following range
    request and seek, so after this one request nginx serves the whole
    clip itself, checking the login with the app now and then rather
    than on every request. With ``filename`` it's a download under that
    name, from either.

    The caller has already checked the session and that the file exists.
    Without nginx, or for a file outside the recordings folder, the app
    serves it.
    """
    if request.headers.get(MEDIA_HEADER):
        root = os.path.abspath(recordings)
        target = os.path.abspath(path)
        if os.path.commonpath([root, target]) == root and target != root:
            rel = os.path.relpath(target, root).replace(os.sep, "/")
            global _noted_root
            if not _noted_root:
                _noted_root = True
                if root != NGINX_ROOT:
                    log.info(
                        "Sending video to nginx under /media/, relative to "
                        "the recordings folder %s. nginx/viofosync.conf "
                        "serves /media/ from %s in its own container, so "
                        "mount that same folder there. (Logged once.)",
                        root, NGINX_ROOT,
                    )
            return RedirectResponse(
                media_link(rel, filename),
                status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        global _warned_outside
        if not _warned_outside:
            _warned_outside = True
            log.warning(
                "%s is outside the recordings folder %s, so nginx can't "
                "serve it and the app is sending it instead. Videos like "
                "it will play more slowly. (Logged once.)",
                path, recordings,
            )
    return VideoFileResponse(path, media_type="video/mp4", filename=filename)
