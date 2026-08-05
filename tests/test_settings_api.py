from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def authed_client(tmp_config_dir: Path, tmp_recordings_dir: Path, monkeypatch):
    from web import app as app_mod
    from web import settings as settings_mod
    monkeypatch.setenv("VIOFOSYNC_RESTART_DISABLED", "1")
    settings_mod.reset_for_tests()
    application = app_mod.create_app()
    with TestClient(application) as c:
        # Bypass setup wizard
        c.post("/setup", data={
            "address": "192.168.1.230",
            "password": "twelve-chars-min!",
            "confirm": "twelve-chars-min!",
        })
        # Acquire CSRF — endpoint returns {"csrf": "..."}
        csrf = c.get("/api/auth/csrf").json()["csrf"]
        c.headers.update({"x-csrf-token": csrf})
        yield c


def test_get_settings_returns_editable_readonly_schema(authed_client) -> None:
    r = authed_client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert "editable" in body and "readonly" in body
    assert body["editable"]["GROUPING"] == "daily"
    assert "WEB_PASSWORD_HASH" not in body["editable"]
    assert "PUID" in body["readonly"]
    assert "CONFIG_FILE" in body["readonly"]
    assert body["readonly"]["CONFIG_FILE"].endswith("config.json")
    assert "WEB_PORT" in body["restart_required_keys"]


def test_put_settings_applies_changes(authed_client) -> None:
    r = authed_client.put("/api/settings", json={"TIMEOUT": 25})
    assert r.status_code == 200
    body = r.json()
    assert body["editable"]["TIMEOUT"] == 25


def test_put_settings_rejects_unknown_keys(authed_client) -> None:
    r = authed_client.put("/api/settings", json={"NUKE": "yes"})
    assert r.status_code == 400


def test_put_settings_marks_web_port_as_restart_required(authed_client) -> None:
    r = authed_client.put("/api/settings", json={"WEB_PORT": 8089})
    assert r.status_code == 200
    body = r.json()
    assert "WEB_PORT" in body["restart_required_keys"]


def test_put_settings_requires_csrf(authed_client) -> None:
    authed_client.headers.pop("x-csrf-token")
    r = authed_client.put("/api/settings", json={"TIMEOUT": 25})
    assert r.status_code == 403


def test_test_dashcam_returns_structured_response(authed_client) -> None:
    r = authed_client.post(
        "/api/settings/test-dashcam",
        json={"address": "127.0.0.1:1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "error" in body


def test_change_password_rejects_wrong_current(authed_client) -> None:
    r = authed_client.post("/api/settings/password", json={
        "current": "wrong-password-here",
        "new_password": "new-twelve-chars!",
        "logout_others": False,
    })
    assert r.status_code == 401


def test_change_password_succeeds_with_correct_current(authed_client) -> None:
    r = authed_client.post("/api/settings/password", json={
        "current": "twelve-chars-min!",
        "new_password": "new-twelve-chars!",
        "logout_others": False,
    })
    assert r.status_code == 200


def test_change_password_with_logout_others_invalidates_old_sessions(authed_client) -> None:
    old_cookie = authed_client.cookies.get("viofosync_session")
    r = authed_client.post("/api/settings/password", json={
        "current": "twelve-chars-min!",
        "new_password": "new-twelve-chars!",
        "logout_others": True,
    })
    assert r.status_code == 200
    new_cookie = authed_client.cookies.get("viofosync_session")
    assert new_cookie != old_cookie


def test_put_web_port_rejects_value_in_use(authed_client) -> None:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    in_use_port = s.getsockname()[1]
    s.listen(1)
    try:
        r = authed_client.put("/api/settings", json={
            "WEB_HOST": "127.0.0.1",
            "WEB_PORT": in_use_port,
        })
        assert r.status_code == 400
        assert "in use" in r.json()["detail"].lower() or "bind" in r.json()["detail"].lower()
    finally:
        s.close()


def test_post_restart_returns_202(authed_client) -> None:
    r = authed_client.post("/api/settings/restart")
    assert r.status_code in (202, 200)


def test_get_settings_includes_delete_after_download(authed_client) -> None:
    r = authed_client.get("/api/settings")
    body = r.json()
    assert "DELETE_AFTER_DOWNLOAD" in body["editable"]
    assert body["editable"]["DELETE_AFTER_DOWNLOAD"] is False


def test_put_delete_after_download_persists(authed_client) -> None:
    r = authed_client.put("/api/settings", json={"DELETE_AFTER_DOWNLOAD": True})
    assert r.status_code == 200
    assert r.json()["editable"]["DELETE_AFTER_DOWNLOAD"] is True
    r = authed_client.get("/api/settings")
    assert r.json()["editable"]["DELETE_AFTER_DOWNLOAD"] is True


def test_address_fallback_round_trips(authed_client) -> None:
    # PUT the fallback, then GET it back through the editable projection.
    put = authed_client.put("/api/settings", json={"ADDRESS_FALLBACK": "10.0.0.9"})
    assert put.status_code == 200
    assert put.json()["editable"]["ADDRESS_FALLBACK"] == "10.0.0.9"

    got = authed_client.get("/api/settings")
    assert got.json()["editable"]["ADDRESS_FALLBACK"] == "10.0.0.9"


def test_get_settings_includes_disk_critical_pct(authed_client) -> None:
    r = authed_client.get("/api/settings")
    body = r.json()
    assert "DISK_CRITICAL_PCT" in body["editable"]
    assert body["editable"]["DISK_CRITICAL_PCT"] == 95


def test_disk_critical_pct_round_trips(authed_client) -> None:
    # PUT a new critical threshold, then GET it back through the projection.
    put = authed_client.put("/api/settings", json={"DISK_CRITICAL_PCT": 90})
    assert put.status_code == 200
    assert put.json()["editable"]["DISK_CRITICAL_PCT"] == 90

    got = authed_client.get("/api/settings")
    assert got.json()["editable"]["DISK_CRITICAL_PCT"] == 90


def test_instance_name_round_trips_through_settings_api(authed_client) -> None:
    # GET exposes the default
    r = authed_client.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["editable"]["INSTANCE_NAME"] == "viofosync"

    # PUT persists and echoes the trimmed value
    r = authed_client.put("/api/settings", json={"INSTANCE_NAME": "  Car A "})
    assert r.status_code == 200
    assert r.json()["editable"]["INSTANCE_NAME"] == "Car A"

    # Verify persistence with a follow-up GET
    r = authed_client.get("/api/settings")
    assert r.json()["editable"]["INSTANCE_NAME"] == "Car A"
