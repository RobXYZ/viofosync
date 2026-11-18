"""Auth router: login, logout, me, check, csrf."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from ..auth import (
    SESSION_COOKIE,
    Auth,
    get_auth,
    require_session,
)
from ..version import display_version

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    password: str


@router.post("/login")
def login(
    body: LoginBody,
    request: Request,
    response: Response,
    auth: Auth = Depends(get_auth),
) -> dict:
    ip = request.client.host if request.client else "unknown"
    auth.record_login_attempt(ip)
    if not auth.check_password(body.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid password",
        )
    auth.clear_login_attempts(ip)
    token = auth.issue_session(response)
    return {"ok": True, "csrf": auth.issue_csrf(token)}


@router.post("/logout", dependencies=[Depends(require_session)])
def logout(
    response: Response, auth: Auth = Depends(get_auth)
) -> dict:
    auth.clear_session(response)
    return {"ok": True}


@router.get("/me", dependencies=[Depends(require_session)])
def me(auth: Auth = Depends(get_auth)) -> dict:
    return {
        "user": "user",
        "version": display_version(),
        "password_required": auth.password_required,
    }


@router.get("/check")
async def check(request: Request) -> Response:
    """204 if this request may use the app, 401 if not. nginx asks this
    before serving /media/ files (``auth_request``) and caches a yes
    for a short while.

    The same rule as every other endpoint, but async and with nothing to
    read from disk, so it answers straight away even while the app is
    busy. (A sync dependency would wait for a worker thread.)
    """
    require_session(request, get_auth(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/csrf", dependencies=[Depends(require_session)])
def csrf(
    request: Request, response: Response, auth: Auth = Depends(get_auth)
) -> dict:
    """Issue a fresh CSRF token bound to the current session.

    Clients should call this after login and whenever they
    receive a 403 on a mutating request, then retry with the
    new token in ``X-CSRF-Token``. With password login off there
    is no login, so this issues the session cookie when the
    request carries no valid one.
    """
    session_token = request.cookies.get(SESSION_COOKIE)
    if not auth.validate_session(session_token):
        if auth.password_required:  # defensive — require_session caught this
            raise HTTPException(status_code=401)
        session_token = auth.issue_session(response, without_password=True)
    return {"csrf": auth.issue_csrf(session_token)}
