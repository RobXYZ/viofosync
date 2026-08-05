"""Auth router: login, logout, me, csrf."""

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
def me() -> dict:
    return {"user": "user", "version": display_version()}


@router.get("/csrf", dependencies=[Depends(require_session)])
def csrf(
    request: Request, auth: Auth = Depends(get_auth)
) -> dict:
    """Issue a fresh CSRF token bound to the current session.

    Clients should call this after login and whenever they
    receive a 403 on a mutating request, then retry with the
    new token in ``X-CSRF-Token``.
    """
    session_token = request.cookies.get(SESSION_COOKIE)
    if not session_token:  # defensive — require_session caught this
        raise HTTPException(status_code=401)
    return {"csrf": auth.issue_csrf(session_token)}
