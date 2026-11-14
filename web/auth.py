"""Authentication — single password, session cookie, CSRF.

Design:
- The plaintext ``WEB_PASSWORD`` env var is hashed with bcrypt
  **once** at startup and kept in memory. The plaintext is never
  written to disk.
- Login issues a signed, HTTP-only session cookie via
  itsdangerous. Cookie payload is ``{"sub": "user", "iat": ts}``
  with a 14-day max age.
- Mutating endpoints (POST/DELETE) require a matching
  ``X-CSRF-Token`` header, issued by ``GET /api/auth/csrf``.
  The token is also signed by itsdangerous and bound to the
  session cookie so a stolen token can't be replayed.
- Login is rate-limited to 5 attempts per minute per IP via a
  tiny in-memory sliding window. Good enough for a single-user
  home deployment.
- With ``password_required`` False (WEB_PASSWORD_DISABLED in
  config.json) the session check passes without a cookie, and
  ``GET /api/auth/csrf`` issues the session cookie a login would
  have. Mutating endpoints still need the CSRF token, which only
  a same-origin page can read. Those sessions carry ``"nopw"`` in
  the payload, and a session check while a password is required
  rejects them, so turning login back on ends them.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Deque, Dict, Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE = "viofosync_session"
SESSION_MAX_AGE = 14 * 24 * 3600   # 14 days
CSRF_MAX_AGE = 4 * 3600            # 4 hours
LOGIN_WINDOW_SECONDS = 60
LOGIN_MAX_ATTEMPTS = 5


class Auth:
    """Holds auth state. Instantiated once at app startup; mutated
    in place when the password or session secret rotates so live
    request handlers see the new state on the next call."""

    def __init__(
        self, *, password_hash: str, secret: str, password_required: bool = True,
    ) -> None:
        self.password_required = password_required
        self._password_hash = (
            password_hash.encode("utf-8")
            if isinstance(password_hash, str)
            else password_hash
        )
        self._signer = URLSafeTimedSerializer(
            secret, salt="viofosync.session"
        )
        self._csrf_signer = URLSafeTimedSerializer(
            secret, salt="viofosync.csrf"
        )
        self._login_attempts: Dict[str, Deque[float]] = {}

    # --- Mutators (called by SettingsProvider subscribers) ---

    def update_password_hash(self, new_hash: str) -> None:
        self._password_hash = (
            new_hash.encode("utf-8")
            if isinstance(new_hash, str)
            else new_hash
        )

    def rotate_secret(self, new_secret: str) -> None:
        self._signer = URLSafeTimedSerializer(
            new_secret, salt="viofosync.session"
        )
        self._csrf_signer = URLSafeTimedSerializer(
            new_secret, salt="viofosync.csrf"
        )

    # --- Password / rate limiting ---

    def check_password(self, candidate: str) -> bool:
        try:
            return bcrypt.checkpw(
                candidate.encode("utf-8"), self._password_hash
            )
        except ValueError:
            return False

    def record_login_attempt(self, ip: str) -> None:
        """Prune old attempts and append ``now``. Raises
        HTTPException 429 if the window is full.

        NB: ``ip`` is ``request.client.host`` — behind a reverse
        proxy that is the proxy's address, so the window is shared
        across clients (a deliberate LAN-deployment trade-off; we
        don't trust X-Forwarded-For, which is spoofable)."""
        now = time.monotonic()
        # Sweep buckets with nothing left inside the window so the
        # map can't grow unbounded from one-shot attempts by IPs that
        # never return.
        stale = [
            k for k, b in self._login_attempts.items()
            if not b or now - b[-1] > LOGIN_WINDOW_SECONDS
        ]
        for k in stale:
            del self._login_attempts[k]
        bucket = self._login_attempts.setdefault(ip, deque())
        while bucket and now - bucket[0] > LOGIN_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= LOGIN_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many login attempts, slow down",
            )
        bucket.append(now)

    def clear_login_attempts(self, ip: str) -> None:
        self._login_attempts.pop(ip, None)

    # --- Session cookie ---

    def issue_session(
        self, response: Response, *, without_password: bool = False,
    ) -> str:
        """Set a signed session cookie on ``response`` and
        return the token (so tests can inspect it).
        ``without_password`` marks a session handed out while
        password login is off."""
        payload = {"sub": "user"}
        if without_password:
            payload["nopw"] = True
        token = self._signer.dumps(payload)
        response.set_cookie(
            key=SESSION_COOKIE,
            value=token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=False,  # TLS is typically terminated upstream
            path="/",
        )
        return token

    def clear_session(self, response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/")

    def validate_session(self, token: Optional[str]) -> bool:
        if not token:
            return False
        try:
            payload = self._signer.loads(token, max_age=SESSION_MAX_AGE)
        except (BadSignature, SignatureExpired):
            return False
        return not (self.password_required and payload.get("nopw"))

    # --- CSRF ---

    def issue_csrf(self, session_token: str) -> str:
        """Issue a CSRF token bound to the session token.
        Binding means a stolen CSRF token from one session
        can't be replayed in another."""
        return self._csrf_signer.dumps({"s": session_token})

    def validate_csrf(
        self, csrf_token: Optional[str], session_token: Optional[str]
    ) -> bool:
        if not csrf_token or not session_token:
            return False
        try:
            payload = self._csrf_signer.loads(
                csrf_token, max_age=CSRF_MAX_AGE
            )
        except (BadSignature, SignatureExpired):
            return False
        return payload.get("s") == session_token


# --- FastAPI dependencies ---
#
# These are attached to the app in app.py via ``app.state.auth``.
# Using ``Request`` for the injection so both HTTP routes and
# the WebSocket endpoint can share the same cookie check.


def get_auth(request: Request) -> Auth:
    auth = getattr(request.app.state, "auth", None)
    if auth is None:  # pragma: no cover
        raise RuntimeError("Auth not configured on app.state")
    return auth


def require_session(
    request: Request, auth: Auth = Depends(get_auth)
) -> None:
    """Dependency: blocks the request if the session cookie
    is missing or invalid, unless password login is off."""
    if not auth.password_required:
        return
    token = request.cookies.get(SESSION_COOKIE)
    if not auth.validate_session(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
        )


def require_csrf(
    request: Request, auth: Auth = Depends(get_auth)
) -> None:
    """Dependency: for mutating endpoints. Runs AFTER
    ``require_session`` implicitly because a missing session
    cookie will already have been rejected by ``require_session``
    in the same route."""
    session_token = request.cookies.get(SESSION_COOKIE)
    csrf_token = request.headers.get("x-csrf-token")
    if not auth.validate_csrf(csrf_token, session_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid CSRF token",
        )
