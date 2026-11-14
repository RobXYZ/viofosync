"""WebSocket endpoint streaming live download + export events.

Authentication: the handshake's ``Cookie`` header is checked
for a valid session token, and a browser-sent ``Origin`` must
match the request host — browsers attach cookies to
cross-origin WS handshakes and WS is exempt from same-origin
fetch rules, so without this check any page the logged-in
admin visits could read the event stream. Requests without an
Origin header (curl, Home Assistant, scripts) are gated by
auth alone.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..auth import SESSION_COOKIE

router = APIRouter()


def _origin_ok(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    if not origin:
        return True  # non-browser client
    host = ws.headers.get("host") or ""
    return urlparse(origin).netloc.lower() == host.lower()


@router.websocket("/api/progress")
async def progress(ws: WebSocket) -> None:
    if not _origin_ok(ws):
        await ws.close(code=4403)
        return
    auth = ws.app.state.auth
    token = ws.cookies.get(SESSION_COOKIE)
    if auth.password_required and not auth.validate_session(token):
        await ws.close(code=4401)
        return

    hub = ws.app.state.hub
    if not await hub.connect(ws):
        return  # client vanished during the handshake
    try:
        while True:
            # Read messages just to detect disconnects; we
            # don't expect the client to send anything useful.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        # A hub broadcast can fail-send while we're parked in
        # receive_text(), which makes starlette mark the socket
        # DISCONNECTED under our feet; the loop's next state
        # check then raises RuntimeError rather than
        # WebSocketDisconnect. Same meaning: client is gone.
        pass
    finally:
        await hub.disconnect(ws)
