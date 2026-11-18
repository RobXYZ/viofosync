"""nginx/viofosync.conf, run for real in front of the app.

The rules that keep recordings private live in the nginx config, not the
app: /media/ asks the app about the login, answers are cached by the
whole Cookie header, a no is cached too, download names are filtered,
requests to the app are marked, uploads pass straight through. These
tests start nginx with the shipped config (pointed at a temp folder and
an in-process app, with a 1-second cache) and check each from outside.

Skipped where nginx isn't installed, unless VIOFOSYNC_REQUIRE_NGINX is
set, as CI does, so they can't be skipped there without anyone noticing.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tests.test_video_nginx import PASSWORD, _add_clip, _add_export, _FakeMqttService
from web import settings as settings_mod
from web.auth import SESSION_COOKIE

CONF = Path(__file__).resolve().parent.parent / "nginx" / "viofosync.conf"
CACHE_SECONDS = 1


def _outlast_the_cache() -> None:
    # nginx counts in whole seconds, so a 1-second answer can last nearly 3.
    time.sleep(CACHE_SECONDS + 2.1)


def _nginx_binary() -> str | None:
    found = shutil.which("nginx")
    if found:
        return found
    for path in ("/usr/sbin/nginx", "/usr/local/sbin/nginx"):
        if os.access(path, os.X_OK):
            return path
    return None


NGINX_BIN = _nginx_binary()
if NGINX_BIN is None:
    if os.environ.get("VIOFOSYNC_REQUIRE_NGINX"):
        raise RuntimeError("VIOFOSYNC_REQUIRE_NGINX is set but nginx isn't installed")
    pytest.skip("nginx isn't installed", allow_module_level=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, what: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"{what} didn't start listening on {port}")


def _swap(conf: str, old: str, new: str) -> str:
    # Exactly once, so a change to the shipped config that these tests
    # don't follow fails here rather than testing something else.
    assert conf.count(old) == 1, f"expected {old!r} once in {CONF.name}"
    return conf.replace(old, new)


def _write_nginx_conf(tmp: Path, nginx_port: int, app_port: int, recordings: Path) -> Path:
    conf = CONF.read_text()
    conf = _swap(conf, "listen 8080;", f"listen 127.0.0.1:{nginx_port};")
    # An address needs no resolver; Docker's isn't here anyway.
    conf = _swap(conf, "resolver 127.0.0.11 valid=10s ipv6=off;", "")
    conf = _swap(conf, "set $viofosync http://viofosync:8080;",
                 f"set $viofosync http://127.0.0.1:{app_port};")
    conf = _swap(conf, "alias /recordings/;", f"alias {recordings}/;")
    conf = _swap(conf, "/var/cache/nginx/viofosync_auth", str(tmp / "auth_cache"))
    conf = _swap(conf, "proxy_cache_valid 204 401 30s;",
                 f"proxy_cache_valid 204 401 {CACHE_SECONDS}s;")
    (tmp / "viofosync.conf").write_text(conf)

    temp_paths = "\n".join(
        f"    {kind}_temp_path {tmp / kind};"
        for kind in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi")
    )
    main = tmp / "nginx.conf"
    # One process as the current user: no worker switching to a user that
    # can't read the temp folders.
    main.write_text(
        f"daemon off;\nmaster_process off;\npid {tmp / 'nginx.pid'};\n"
        f"error_log {tmp / 'error.log'} warn;\n"
        "events {}\n"
        "http {\n"
        f"    access_log {tmp / 'access.log'};\n"
        f"{temp_paths}\n"
        f"    include {tmp / 'viofosync.conf'};\n"
        "}\n"
    )
    return main


class _App:
    """The app on a real port, in a thread, so it can be stopped."""

    def __init__(self, app, port: int):
        import uvicorn

        self.app = app
        self.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning",
            loop="asyncio", http="h11", ws="none", lifespan="on",
        ))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the app didn't start")
            time.sleep(0.05)

    def stop(self) -> None:
        if self.thread.is_alive():
            self.server.should_exit = True
            self.thread.join(15)


@pytest.fixture
def stack(tmp_config_dir, tmp_recordings_dir, tmp_path, monkeypatch):
    import bcrypt

    from web.app import create_app
    from web.services.sync_worker import SyncWorker

    monkeypatch.setattr(SyncWorker, "start", lambda self: None)
    monkeypatch.setattr("web.app.MqttService", _FakeMqttService)
    digest = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()
    (tmp_config_dir / "config.json").write_text(
        json.dumps({"WEB_PASSWORD_HASH": digest})
    )
    settings_mod.reset_for_tests()

    app_port, nginx_port = _free_port(), _free_port()
    app = _App(create_app(), app_port)
    main = _write_nginx_conf(tmp_path, nginx_port, app_port, tmp_recordings_dir)
    for kind in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"):
        (tmp_path / kind).mkdir()
    nginx = subprocess.Popen(
        [NGINX_BIN, "-p", str(tmp_path), "-c", str(main), "-e", str(tmp_path / "error.log")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(nginx_port, "nginx")
    except RuntimeError as e:
        nginx.kill()
        nginx.wait(10)
        app.stop()
        log = tmp_path / "error.log"
        raise RuntimeError(log.read_text() if log.exists() else str(e)) from e

    clip_id, data = _add_clip(SimpleNamespace(app=app.app), tmp_recordings_dir)
    export_id = _add_export(SimpleNamespace(app=app.app), tmp_recordings_dir)
    client = httpx.Client(base_url=f"http://127.0.0.1:{nginx_port}")
    try:
        yield SimpleNamespace(
            app=app, client=client, recordings=tmp_recordings_dir,
            clip_id=clip_id, data=data, export_id=export_id,
            direct=f"http://127.0.0.1:{app_port}",
        )
    finally:
        client.close()
        nginx.terminate()
        nginx.wait(10)
        app.stop()
        settings_mod.reset_for_tests()


def _login(s) -> str:
    r = s.client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200
    # Every request says which cookie it sends, or none.
    s.client.cookies.clear()
    return r.cookies[SESSION_COOKIE]


def _as(session: str) -> dict:
    return {"Cookie": f"{SESSION_COOKIE}={session}"}


def _media(s) -> str:
    r = s.client.get(f"/api/archive/clip/{s.clip_id}/video",
                     headers=_as(_login(s)))
    assert r.status_code == 302
    return r.headers["location"].split("?")[0]


def _get(s, url: str, cookie: str | None) -> int:
    headers = {"Range": "bytes=0-0"}
    if cookie is not None:
        headers["Cookie"] = cookie
    return s.client.get(url, headers=headers).status_code


def test_media_is_served_by_nginx_only_when_logged_in(stack):
    s = stack
    url = _media(s)
    assert url == "/media/2026-11-15/2026_1115_080000_0001F.MP4"
    cookie = f"{SESSION_COOKIE}={_login(s)}"

    r = s.client.get(url, headers={"Cookie": cookie, "Range": "bytes=100-299"})
    assert r.status_code == 206
    assert r.content == s.data[100:300]

    assert _get(s, url, None) == 401
    assert _get(s, url, f"{SESSION_COOKIE}=made-up") == 401


def test_a_session_cookie_sent_twice_files_nothing_for_others(stack):
    """nginx would read the first, the app the last; the answer must be
    filed under exactly what the app was asked about."""
    s = stack
    url = _media(s)
    real = _login(s)

    _get(s, url, f"{SESSION_COOKIE}=; {SESSION_COOKIE}={real}")
    _get(s, url, f"{SESSION_COOKIE}=junk; {SESSION_COOKIE}={real}")
    assert _get(s, url, None) == 401
    assert _get(s, url, f"{SESSION_COOKIE}=") == 401
    assert _get(s, url, f"{SESSION_COOKIE}=junk") == 401


def test_the_app_is_asked_about_the_cookies_as_cached(stack, monkeypatch):
    """Cookies sent as two headers are cached joined into one, so the app
    must be sent that one header too, not the two, which it could read
    differently."""
    from web.routers import auth as auth_router

    s = stack
    url = _media(s)
    real = _login(s)
    asked: list[list[str]] = []
    check = auth_router.require_session

    def recording(request, *args, **kwargs):
        asked.append(request.headers.getlist("cookie"))
        return check(request, *args, **kwargs)

    monkeypatch.setattr(auth_router, "require_session", recording)
    r = s.client.get(url, headers=[
        ("Range", "bytes=0-0"),
        ("Cookie", f"{SESSION_COOKIE}=junk"),
        ("Cookie", f"{SESSION_COOKIE}={real}"),
    ])
    assert r.status_code == 206
    assert asked == [[f"{SESSION_COOKIE}=junk; {SESSION_COOKIE}={real}"]]


def test_playback_does_not_wait_for_a_slow_app(stack, monkeypatch):
    """Once the answer runs out, nginx keeps using it while it asks the app
    again in the background."""
    from web.routers import auth as auth_router

    s = stack
    url = _media(s)
    cookie = f"{SESSION_COOKIE}={_login(s)}"
    assert _get(s, url, cookie) == 206

    real = auth_router.require_session

    def stalled(*args, **kwargs):
        time.sleep(3)  # the app's event loop held up, as by a slow disk
        return real(*args, **kwargs)

    monkeypatch.setattr(auth_router, "require_session", stalled)
    _outlast_the_cache()
    started = time.monotonic()
    assert _get(s, url, cookie) == 206
    assert time.monotonic() - started < 1.5


def test_logging_others_out_stops_their_video_within_the_cache_time(stack):
    s = stack
    url = _media(s)
    other = f"{SESSION_COOKIE}={_login(s)}"
    assert _get(s, url, other) == 206

    me = _login(s)
    csrf = s.client.get("/api/auth/csrf", headers=_as(me)).json()["csrf"]
    r = s.client.post(
        "/api/settings/password",
        headers={**_as(me), "X-CSRF-Token": csrf},
        json={"current": PASSWORD, "new_password": PASSWORD + "2",
              "logout_others": True},
    )
    assert r.status_code == 200

    # Once the remembered yes runs out, one more request may still get it
    # while nginx asks again in the background; after that, no.
    deadline = time.monotonic() + CACHE_SECONDS + 5
    while _get(s, url, other) == 206:
        assert time.monotonic() < deadline, "still playing after logout"
        time.sleep(0.2)
    assert _get(s, url, other) == 401
    assert _get(s, url, f"{SESSION_COOKIE}={r.cookies[SESSION_COOKIE]}") == 206


def test_video_keeps_playing_while_the_app_is_down(stack):
    s = stack
    url = _media(s)
    cookie = f"{SESSION_COOKIE}={_login(s)}"
    assert _get(s, url, cookie) == 206

    s.app.stop()
    _outlast_the_cache()
    for _ in range(3):
        assert _get(s, url, cookie) == 206
    # With nothing remembered there's nothing to go on: still no.
    assert _get(s, url, None) not in (200, 206)
    assert _get(s, url, f"{SESSION_COOKIE}=made-up") not in (200, 206)


def test_downloads_are_named_and_odd_names_are_dropped(stack):
    s = stack
    me = _as(_login(s))
    r = s.client.get(f"/api/exports/{s.export_id}/download", headers=me)
    assert r.status_code == 302
    r = s.client.get(r.headers["location"], headers=me)
    assert r.status_code == 200
    assert r.content == b"\0" * 1024
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment; filename=")
    assert "filename*=UTF-8''" in disposition

    r = s.client.get(f"/api/exports/{s.export_id}/video", headers=me)
    r = s.client.get(r.headers["location"], headers=me)
    assert r.status_code == 200
    assert "content-disposition" not in r.headers

    # Kept as they are by httpx, and each has something the name filter
    # turns away.
    for name in ("evil%22%0d%0aX-Bad:%20y", "a;b", "a=b", "a,b"):
        r = s.client.get(f"/media/.exports/5.mp4?name={name}", headers=me)
        assert r.status_code == 200
        assert "content-disposition" not in r.headers
        assert "x-bad" not in r.headers


def test_only_requests_through_nginx_are_sent_to_media(stack):
    s = stack
    me = _as(_login(s))
    video = f"/api/archive/clip/{s.clip_id}/video"
    # nginx replaces whatever the browser sent.
    r = s.client.get(video, headers={**me, "X-Viofosync-Media": ""})
    assert r.status_code == 302
    # Straight at the app, it sends the video itself.
    r = httpx.get(s.direct + video, headers=me)
    assert r.status_code == 200
    assert r.content == s.data


def test_uploads_reach_the_app_while_still_being_sent(stack):
    s = stack
    me = _as(_login(s))
    csrf = s.client.get("/api/auth/csrf", headers=me).json()["csrf"]
    name = "2026_1116_100000_0005F.MP4"
    part = s.recordings / ".import_tmp" / (name + ".part")
    chunk, chunks = b"\0" * (1 << 20), 8
    seen: list[int] = []

    def body():
        for i in range(chunks):
            if i == chunks - 2:
                # 6 of 8 MB sent: passed straight through, the app has
                # been writing it down all along.
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    size = part.stat().st_size if part.exists() else 0
                    if size >= len(chunk):
                        break
                    time.sleep(0.05)
                seen.append(size)
            yield chunk

    r = s.client.post(
        "/api/import/upload",
        headers={
            **me,
            "X-CSRF-Token": csrf,
            "X-Import-Path": name,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(chunk) * chunks),
        },
        content=body(),
        timeout=30,
    )
    assert r.status_code == 200
    assert seen and seen[0] >= len(chunk), (
        f"the app had {seen} bytes with 6 MB sent: nginx is holding uploads back"
    )
