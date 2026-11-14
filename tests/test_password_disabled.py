"""WEB_PASSWORD_DISABLED: an opt-out of password login, set by hand in
config.json for a web UI kept behind something else that authenticates.

With it on, nothing asks for a password, but browser requests still get
a session cookie and a CSRF token, so a cross-site page still can't fire
mutating requests. The Settings API can't change it, first-run setup
still asks for a password, and every startup logs a warning. With it
absent, login works exactly as before.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from web import settings as settings_mod
from web.auth import SESSION_COOKIE

PASSWORD = "twelve-chars-min!"


def _write_config(config_dir: Path, **extra) -> None:
    digest = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()
    data = {"WEB_PASSWORD_HASH": digest, **extra}
    (config_dir / "config.json").write_text(json.dumps(data))


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def make_client(tmp_config_dir: Path, tmp_recordings_dir: Path):
    """Start the app on a config written by the test; yields a factory
    returning (client, startup WARNING messages from viofosync.web)."""
    opened: list[TestClient] = []
    web_log = logging.getLogger("viofosync.web")
    handlers: list[_Records] = []

    def _make(**extra) -> tuple[TestClient, list[str]]:
        from web import app as app_mod
        _write_config(tmp_config_dir, **extra)
        settings_mod.reset_for_tests()
        app = app_mod.create_app()
        records = _Records()
        web_log.addHandler(records)
        handlers.append(records)
        c = TestClient(app)
        c.__enter__()
        opened.append(c)
        return c, records.messages

    yield _make
    for c in opened:
        c.__exit__(None, None, None)
    for h in handlers:
        web_log.removeHandler(h)
    settings_mod.reset_for_tests()


# --- password disabled ----------------------------------------------------

def test_api_answers_without_logging_in(make_client) -> None:
    c, _ = make_client(WEB_PASSWORD_DISABLED=True)
    r = c.get("/api/settings")
    assert r.status_code == 200
    me = c.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["password_required"] is False


def test_csrf_endpoint_hands_out_a_session_and_mutations_work(make_client) -> None:
    c, _ = make_client(WEB_PASSWORD_DISABLED=True)
    r = c.get("/api/auth/csrf")
    assert r.status_code == 200
    assert SESSION_COOKIE in r.cookies
    c.headers["X-CSRF-Token"] = r.json()["csrf"]
    put = c.put("/api/settings", json={"DISTANCE_UNITS": "miles"})
    assert put.status_code == 200, put.text


def test_mutations_still_need_a_csrf_token(make_client) -> None:
    c, _ = make_client(WEB_PASSWORD_DISABLED=True)
    assert c.put("/api/settings", json={"DISTANCE_UNITS": "miles"}).status_code == 403
    c.get("/api/auth/csrf")  # session cookie now set, token not sent
    assert c.put("/api/settings", json={"DISTANCE_UNITS": "miles"}).status_code == 403
    assert c.get("/api/settings").json()["editable"]["DISTANCE_UNITS"] == "km"


def test_live_events_socket_opens_without_logging_in(make_client) -> None:
    c, _ = make_client(WEB_PASSWORD_DISABLED=True)
    with c.websocket_connect("/api/progress") as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_startup_warns_that_login_is_off(make_client) -> None:
    _, warnings = make_client(WEB_PASSWORD_DISABLED=True)
    assert any(
        "WEB_PASSWORD_DISABLED" in m and "anyone" in m for m in warnings
    ), warnings


def test_settings_api_cannot_change_it(make_client) -> None:
    c, _ = make_client(WEB_PASSWORD_DISABLED=True)
    c.headers["X-CSRF-Token"] = c.get("/api/auth/csrf").json()["csrf"]
    r = c.put("/api/settings", json={"WEB_PASSWORD_DISABLED": False})
    assert r.status_code == 400
    assert c.get("/api/auth/me").json()["password_required"] is False


def test_first_run_setup_still_asks_for_a_password(
    tmp_config_dir: Path, tmp_recordings_dir: Path,
) -> None:
    from web import app as app_mod
    (tmp_config_dir / "config.json").write_text(
        json.dumps({"WEB_PASSWORD_DISABLED": True})
    )
    settings_mod.reset_for_tests()
    try:
        with TestClient(app_mod.create_app()) as c:
            r = c.get("/api/settings", follow_redirects=False)
            assert r.status_code == 307
            assert r.headers["location"].endswith("/setup")
            r = c.post("/setup", data={"password": "", "confirm": ""},
                       follow_redirects=False)
            assert r.status_code in (400, 422)
            assert c.app.state.settings_provider.get().is_unconfigured
    finally:
        settings_mod.reset_for_tests()


def test_turning_login_back_on_ends_passwordless_sessions(
    make_client, tmp_config_dir: Path,
) -> None:
    c, _ = make_client(WEB_PASSWORD_DISABLED=True)
    passwordless = c.get("/api/auth/csrf").cookies[SESSION_COOKIE]
    secret = json.loads((tmp_config_dir / "config.json").read_text())["SESSION_SECRET"]

    # Same config and session secret, with the key removed.
    c2, _ = make_client(SESSION_SECRET=secret)
    c2.cookies.set(SESSION_COOKIE, passwordless)
    assert c2.get("/api/settings").status_code == 401
    # A real login still works afterwards.
    assert c2.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    assert c2.get("/api/settings").status_code == 200


# --- default: password required -------------------------------------------

def test_by_default_the_api_needs_a_login(make_client) -> None:
    c, warnings = make_client()
    assert c.get("/api/settings").status_code == 401
    assert c.get("/api/auth/me").status_code == 401
    assert c.get("/api/auth/csrf").status_code == 401
    assert not any("WEB_PASSWORD_DISABLED" in m for m in warnings)


def test_by_default_the_live_events_socket_needs_a_login(make_client) -> None:
    c, _ = make_client()
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/progress") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_by_default_me_reports_a_password_is_required(make_client) -> None:
    c, _ = make_client()
    assert c.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    assert c.get("/api/auth/me").json()["password_required"] is True


def test_settings_api_cannot_turn_it_on(make_client) -> None:
    c, _ = make_client()
    c.post("/api/auth/login", json={"password": PASSWORD})
    c.headers["X-CSRF-Token"] = c.get("/api/auth/csrf").json()["csrf"]
    r = c.put("/api/settings", json={"WEB_PASSWORD_DISABLED": True})
    assert r.status_code == 400
    on_disk = json.loads(
        (Path(c.app.state.settings_provider.config_path)).read_text()
    )
    assert "WEB_PASSWORD_DISABLED" not in on_disk
