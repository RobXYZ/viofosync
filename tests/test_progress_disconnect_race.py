"""Regression: client disconnects racing the /api/progress handler.

Starlette marks a WebSocket's application_state DISCONNECTED when a
send fails at the transport (websockets.py: ``except OSError`` →
``WebSocketDisconnect(1006)``). Two windows exposed the route to a
``RuntimeError('WebSocket is not connected. Need to call "accept"
first.')`` escaping as "Exception in ASGI application":

1. The client vanishes between ``accept()`` and the hub's initial
   snapshot send — ``Hub.connect()`` swallowed the failure and the
   route entered its receive loop on the dead socket.
2. A hub broadcast fail-sends (evicting the client) while the route
   is parked in ``receive_text()``; the loop's next state check
   raises RuntimeError instead of WebSocketDisconnect.

Both use a real starlette WebSocket over a fake ASGI transport so the
production state machine is exercised, not a test double.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from starlette.websockets import WebSocket

from web.routers.progress import progress
from web.services.hub import Hub


class _Auth:
    password_required = True

    def validate_session(self, token) -> bool:
        return True


def _make_ws(receive, send, hub: Hub) -> WebSocket:
    scope = {
        "type": "websocket",
        "headers": [(b"host", b"nas:8080")],  # no Origin: non-browser path
        "app": SimpleNamespace(state=SimpleNamespace(auth=_Auth(), hub=hub)),
    }
    return WebSocket(scope, receive=receive, send=send)


async def test_client_gone_before_snapshot_send_does_not_raise():
    """Window 1: transport drops between accept and the snapshot send."""
    hub = Hub()
    got_connect = False

    async def receive():
        nonlocal got_connect
        if not got_connect:
            got_connect = True
            return {"type": "websocket.connect"}
        return {"type": "websocket.disconnect", "code": 1006}

    async def send(message):
        if message["type"] == "websocket.accept":
            return
        # First real frame (the snapshot) hits a dead transport;
        # starlette converts this to WebSocketDisconnect and flips
        # application_state to DISCONNECTED.
        raise OSError("connection lost")

    ws = _make_ws(receive, send, hub)
    # Must complete without RuntimeError reaching the ASGI layer.
    await asyncio.wait_for(progress(ws), timeout=2)
    assert not hub._clients, "dead client left registered on the hub"


async def test_broadcast_eviction_midsession_does_not_raise():
    """Window 2: a broadcast fail-send flips the socket to
    DISCONNECTED while the route loop is parked in receive_text();
    a buffered client frame then re-enters the loop."""
    hub = Hub()
    sends = 0
    got_connect = False
    text_ready = asyncio.Event()
    text_delivered = False

    async def receive():
        nonlocal got_connect, text_delivered
        if not got_connect:
            got_connect = True
            return {"type": "websocket.connect"}
        if not text_delivered:
            await text_ready.wait()
            text_delivered = True
            return {"type": "websocket.receive", "text": "ping"}
        return {"type": "websocket.disconnect", "code": 1006}

    async def send(message):
        nonlocal sends
        sends += 1
        if sends <= 2:  # accept + snapshot succeed
            return
        raise OSError("connection lost")  # the broadcast fail-send

    ws = _make_ws(receive, send, hub)
    task = asyncio.create_task(progress(ws))
    # Let the route connect and park in receive_text().
    for _ in range(10):
        await asyncio.sleep(0)
    assert ws in hub._clients

    # Broadcast fail-sends: hub evicts the client, starlette marks
    # the socket DISCONNECTED under the route's feet.
    await hub.broadcast({"type": "dashcam_offline"})
    assert ws not in hub._clients

    # A frame the client sent before dying is still buffered; the
    # route loop consumes it and re-enters receive_text() on the
    # now-DISCONNECTED socket.
    text_ready.set()
    await asyncio.wait_for(task, timeout=2)  # must not raise
