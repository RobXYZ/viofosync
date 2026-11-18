"""nginx in front serves video: the app redirects to /media/ and answers
nginx's login checks.

nginx/viofosync.conf marks the requests it passes on with
X-Viofosync-Media. With it, the video endpoints check the session and look
the file up as before, then redirect to the file's /media/ URL. nginx
checks each /media/ request with GET /api/auth/check (caching a yes for a
short while) and serves the file itself. Without the header the endpoints
stream the file.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import bcrypt
import pytest
from fastapi.testclient import TestClient

from web import responses
from web import settings as settings_mod
from web.auth import SESSION_COOKIE
from web.responses import media_link

PASSWORD = "pwpwpwpwpwpwpwpw"
NGINX = {"X-Viofosync-Media": "1"}

# The pattern nginx/viofosync.conf's $media_disposition map accepts.
NGINX_NAME = re.compile(r"[A-Za-z0-9%._~-]+")


def _name_arg(url: str) -> str:
    """name= as nginx's $arg_name sees it: still percent-encoded."""
    for arg in urlsplit(url).query.split("&"):
        if arg.startswith("name="):
            return arg[len("name="):]
    return ""


def test_conf_name_pattern_matches_this_test():
    conf = (Path(__file__).resolve().parent.parent / "nginx" / "viofosync.conf").read_text()
    assert '"~^' + NGINX_NAME.pattern + '$"' in conf


# --- media_link ---


def test_media_link_escapes_the_path():
    assert media_link("2026-11-15/2026_1115_080000_0001F.MP4") == (
        "/media/2026-11-15/2026_1115_080000_0001F.MP4"
    )
    assert media_link("Road trip/clip #1?.MP4") == (
        "/media/Road%20trip/clip%20%231%3F.MP4"
    )


def test_download_name_is_encoded_for_nginx():
    url = media_link(".exports/5.mp4", 'Café "trip" 2026/11/15 a&b=c;.mp4')
    assert url == (
        "/media/.exports/5.mp4?name=Caf%C3%A9%20%22trip%22%202026%2F11%2F15"
        "%20a%26b%3Dc%3B.mp4"
    )
    # nginx puts it into the header as it is, so it must hold nothing
    # that would need quoting there, and nginx must accept it.
    assert NGINX_NAME.fullmatch(_name_arg(url))


# --- endpoints ---


class _FakeMqttService:
    def __init__(self, **kwargs):
        self._last_node_id = ""
        self._last_discovery_prefix = ""

    def start(self): pass
    async def stop(self): pass
    async def on_settings_changed(self, keys, snap): pass
    def get_status(self):
        return {"state": "idle", "detail": None, "last_published_at": None}


@pytest.fixture
def make_client(tmp_config_dir, tmp_recordings_dir, monkeypatch):
    from web.app import create_app
    from web.services.sync_worker import SyncWorker

    monkeypatch.setattr(SyncWorker, "start", lambda self: None)
    monkeypatch.setattr("web.app.MqttService", _FakeMqttService)
    monkeypatch.setattr(responses, "_warned_outside", False)
    monkeypatch.setattr(responses, "_noted_root", False)
    opened: list[TestClient] = []

    def _make(**config) -> TestClient:
        digest = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()
        (tmp_config_dir / "config.json").write_text(
            json.dumps({"WEB_PASSWORD_HASH": digest, **config})
        )
        settings_mod.reset_for_tests()
        c = TestClient(create_app())
        c.__enter__()
        opened.append(c)
        return c

    yield _make
    for c in opened:
        c.__exit__(None, None, None)
    settings_mod.reset_for_tests()


def _add_clip(client, recordings) -> tuple[int, bytes]:
    day = recordings / "2026-11-15"
    day.mkdir(exist_ok=True)
    clip = day / "2026_1115_080000_0001F.MP4"
    data = bytes(range(256)) * 4
    clip.write_bytes(data)
    with client.app.state.db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(id, path, basename, group_name, timestamp, camera, "
            " sequence, event_type, has_gpx, gps_examined, scanned_at) "
            "VALUES (7,?,?,?,1,'F',1,'normal',0,0,1)",
            (str(clip), clip.name, "2026-11-15"),
        )
    return 7, data


def _add_export(client, recordings) -> int:
    out = recordings / ".exports" / "5.mp4"
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(b"\0" * 1024)
    with client.app.state.db.write() as c:
        return c.execute(
            "INSERT INTO export_jobs "
            "(type, clip_ids, state, output_path, created_at, finished_at) "
            "VALUES ('join_front', ?, 'done', ?, 1, 2)",
            (json.dumps({"clip_ids": [], "encoder": "software"}), str(out)),
        ).lastrowid


def _login(client) -> str:
    client.post("/api/auth/login", json={"password": PASSWORD})
    return client.cookies[SESSION_COOKIE]


def test_clip_video_redirects_to_media_behind_nginx(make_client, tmp_recordings_dir):
    c = make_client()
    clip_id, _ = _add_clip(c, tmp_recordings_dir)
    _login(c)

    r = c.get(f"/api/archive/clip/{clip_id}/video", headers=NGINX,
              follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["cache-control"] == "no-store"
    # Also the archive's download link: named as the app would name it.
    assert r.headers["location"] == (
        "/media/2026-11-15/2026_1115_080000_0001F.MP4"
        "?name=2026_1115_080000_0001F.MP4"
    )


def test_clip_video_still_needs_login(make_client, tmp_recordings_dir):
    c = make_client()
    clip_id, _ = _add_clip(c, tmp_recordings_dir)
    r = c.get(f"/api/archive/clip/{clip_id}/video", headers=NGINX,
              follow_redirects=False)
    assert r.status_code == 401
    assert "location" not in r.headers


def test_export_video_plays_inline_and_download_keeps_its_name(
    make_client, tmp_recordings_dir
):
    c = make_client()
    job_id = _add_export(c, tmp_recordings_dir)
    _login(c)

    r = c.get(f"/api/exports/{job_id}/video", headers=NGINX,
              follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/media/.exports/5.mp4"

    r = c.get(f"/api/exports/{job_id}/download", headers=NGINX,
              follow_redirects=False)
    assert r.status_code == 302
    url = r.headers["location"]
    assert url.startswith("/media/.exports/5.mp4?name=")
    name = unquote(_name_arg(url))
    assert name != "5.mp4"
    assert NGINX_NAME.fullmatch(_name_arg(url))

    # The same name the app gives the download without nginx.
    r = c.get(f"/api/exports/{job_id}/download", follow_redirects=False)
    assert r.status_code == 200
    assert r.content == b"\0" * 1024
    want = r.headers["content-disposition"]
    assert f'filename="{name}"' in want or quote(name) in want


def test_without_nginx_the_app_streams_it(make_client, tmp_recordings_dir):
    c = make_client()
    clip_id, data = _add_clip(c, tmp_recordings_dir)
    _login(c)
    r = c.get(f"/api/archive/clip/{clip_id}/video", follow_redirects=False)
    assert r.status_code == 200
    assert r.content == data
    assert "2026_1115_080000_0001F.MP4" in r.headers["content-disposition"]


def test_file_outside_recordings_is_streamed_and_warned_once(
    make_client, tmp_recordings_dir, tmp_path, monkeypatch
):
    c = make_client()
    _login(c)
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"x" * 100)
    with c.app.state.db.write() as db:
        db.execute(
            "INSERT INTO clip_index "
            "(id, path, basename, group_name, timestamp, camera, "
            " sequence, event_type, has_gpx, gps_examined, scanned_at) "
            "VALUES (8,?,?,?,1,'F',1,'normal',0,0,1)",
            (str(outside), outside.name, "2026-11-15"),
        )
    warnings: list[str] = []
    monkeypatch.setattr(
        responses.log, "warning", lambda msg, *a: warnings.append(msg % a)
    )
    for _ in range(3):
        r = c.get("/api/archive/clip/8/video", headers=NGINX,
                  follow_redirects=False)
        assert r.status_code == 200
        assert r.content == b"x" * 100
    assert len(warnings) == 1
    assert str(outside) in warnings[0]


def test_a_recordings_folder_other_than_nginxs_is_noted_once(
    make_client, tmp_recordings_dir, monkeypatch
):
    """nginx serves /media/ from its /recordings, and the app links
    relative to its own recordings folder: with a different one (as here)
    say which folder nginx must have there."""
    c = make_client()
    clip_id, _ = _add_clip(c, tmp_recordings_dir)
    _login(c)
    notes: list[str] = []
    monkeypatch.setattr(responses.log, "info", lambda msg, *a: notes.append(msg % a))
    for _ in range(3):
        r = c.get(f"/api/archive/clip/{clip_id}/video", headers=NGINX,
                  follow_redirects=False)
        assert r.status_code == 302
    assert len(notes) == 1
    assert str(tmp_recordings_dir) in notes[0]


# --- nginx's login check ---


def test_check_needs_a_login(make_client):
    c = make_client()
    r = c.get("/api/auth/check")
    assert r.status_code == 401

    _login(c)
    r = c.get("/api/auth/check")
    assert r.status_code == 204
    assert r.content == b""
    # nginx caches the answer by cookie: nothing here may stop that or
    # change the cookie.
    assert "set-cookie" not in r.headers
    assert "cache-control" not in r.headers


def test_check_fails_once_other_sessions_are_logged_out(make_client):
    c = make_client()
    old = _login(c)
    csrf = c.get("/api/auth/csrf").json()["csrf"]
    r = c.post(
        "/api/settings/password",
        headers={"X-CSRF-Token": csrf},
        json={"current": PASSWORD, "new_password": PASSWORD + "2",
              "logout_others": True},
    )
    assert r.status_code == 200
    new = c.cookies[SESSION_COOKIE]
    assert new != old

    c.cookies.set(SESSION_COOKIE, old)
    assert c.get("/api/auth/check").status_code == 401
    c.cookies.set(SESSION_COOKIE, new)
    assert c.get("/api/auth/check").status_code == 204


def test_check_with_password_login_off(make_client):
    c = make_client(WEB_PASSWORD_DISABLED=True)
    r = c.get("/api/auth/check")
    assert r.status_code == 204
    assert "set-cookie" not in r.headers


def test_check_does_not_wait_for_a_worker_thread():
    """It's async so it answers straight away while sync endpoints have
    every worker thread busy."""
    import asyncio

    from web.routers.auth import check

    assert asyncio.iscoroutinefunction(check)
