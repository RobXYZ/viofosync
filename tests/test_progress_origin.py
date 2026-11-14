"""Cross-site WebSocket hijacking guard on /api/progress.

Browsers send cookies on cross-origin WS handshakes and WS is exempt
from same-origin fetch rules — without an Origin check, any page the
logged-in admin visits can open the socket and read the live event
stream. Non-browser clients (curl, HA) send no Origin and must still
be allowed; auth alone gates those.
"""
from __future__ import annotations

from types import SimpleNamespace

from web.routers.progress import progress


class _Auth:
    password_required = True

    def validate_session(self, token) -> bool:
        return False  # force the 4401 path when auth is reached


class _WS:
    def __init__(self, *, origin: str | None, host: str = "nas:8080"):
        self.headers = {"host": host}
        if origin is not None:
            self.headers["origin"] = origin
        self.cookies: dict = {}
        self.closed: list = []
        self.app = SimpleNamespace(state=SimpleNamespace(auth=_Auth()))

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)


async def test_cross_origin_handshake_rejected():
    ws = _WS(origin="http://evil.example")
    await progress(ws)
    assert ws.closed == [4403], \
        f"cross-origin WS not rejected with 4403 (got {ws.closed})"


async def test_same_origin_proceeds_to_auth():
    ws = _WS(origin="http://nas:8080")
    await progress(ws)
    assert ws.closed == [4401]  # passed origin, failed (fake) auth


async def test_no_origin_header_proceeds_to_auth():
    # curl / Home Assistant / scripts send no Origin.
    ws = _WS(origin=None)
    await progress(ws)
    assert ws.closed == [4401]


async def test_origin_with_https_scheme_and_same_host_allowed():
    ws = _WS(origin="https://nas:8080")
    await progress(ws)
    assert ws.closed == [4401]
