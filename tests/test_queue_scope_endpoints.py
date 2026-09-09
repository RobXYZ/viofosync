"""Queue endpoints: held flag follows the active connection; scope preview."""
from __future__ import annotations

import pytest


class _FakeMqttService:
    def __init__(self, **kwargs): pass
    def start(self): pass
    async def stop(self): pass
    async def on_settings_changed(self, keys, snap): pass
    def get_status(self):
        return {"state": "idle", "detail": None, "last_published_at": None}


@pytest.fixture
def client(tmp_config_dir, tmp_recordings_dir, monkeypatch):
    import bcrypt
    from fastapi.testclient import TestClient

    from web import settings as settings_mod
    from web.app import create_app
    from web.services.sync_worker import SyncWorker

    digest = bcrypt.hashpw(b"pw" * 8, bcrypt.gensalt()).decode()
    settings_mod.reset_for_tests()
    p = settings_mod.get_provider()
    data = p._store.load()
    data["WEB_PASSWORD_HASH"] = digest
    data["ADDRESS"] = "192.168.1.230"
    data["ALTERNATIVE_SCOPE"] = "ro_only"
    p._store.write(data)
    settings_mod.reset_for_tests()

    monkeypatch.setattr(SyncWorker, "start", lambda self: None)
    monkeypatch.setattr("web.app.MqttService", _FakeMqttService)

    app = create_app()
    c = TestClient(app)
    c.__enter__()
    c.post("/api/auth/login", json={"password": "pwpwpwpwpwpwpwpw"})
    csrf = c.get("/api/auth/csrf").json()["csrf"]
    c.headers.update({"x-csrf-token": csrf})
    # TestClient exposes the ASGI app as ``client.app``; tests use it to
    # reach app.state.db / app.state.sync_worker.
    try:
        yield c
    finally:
        c.__exit__(None, None, None)
        settings_mod.reset_for_tests()


def _seed(app):
    with app.state.db.write() as c:
        c.executemany(
            "INSERT INTO download_queue "
            "(filename, source_dir, state, enqueued_at, remote_size) "
            "VALUES (?, ?, 'pending', ?, 100)",
            [
                ("2026_0901_100000_0001F.MP4", "/DCIM/Movie", 1),
                ("2026_0901_100100_0002PF.MP4", "/DCIM/Movie/Parking", 2),
                ("2026_0901_100200_0003F.MP4", "/DCIM/Movie/RO", 3),
            ],
        )


def test_sync_status_reports_source(client):
    assert client.get("/api/sync/status").json()["source"] is None
    client.app.state.sync_worker._active_source = "alternative"
    assert client.get("/api/sync/status").json()["source"] == "alternative"


def test_day_items_held_follows_active_connection(client):
    _seed(client.app)
    # Offline: nothing held.
    items = client.get("/api/queue/day/2026-09-01").json()["items"]
    assert all(i["held"] == 0 for i in items)
    # On the alternative (ro_only): two held.
    client.app.state.sync_worker._active_source = "alternative"
    items = client.get("/api/queue/day/2026-09-01").json()["items"]
    assert sorted(i["held"] for i in items) == [0, 1, 1]
    # On the primary (everything): none held.
    client.app.state.sync_worker._active_source = "primary"
    items = client.get("/api/queue/day/2026-09-01").json()["items"]
    assert all(i["held"] == 0 for i in items)


def test_days_and_page_carry_held(client):
    _seed(client.app)
    client.app.state.sync_worker._active_source = "alternative"
    (day,) = client.get("/api/queue/days").json()["days"]
    assert day["pending_count"] == 1
    assert day["held_count"] == 2
    page = client.get("/api/queue").json()
    assert sum(i["held"] for i in page["items"]) == 2


def test_scope_preview_endpoint(client):
    _seed(client.app)
    r = client.get("/api/queue/scope-preview", params={"scope": "no_parking"})
    assert r.status_code == 200
    assert r.json() == {
        "scope": "no_parking", "now": 2, "held": 1, "stranded": 1,
    }


def test_scope_preview_rejects_unknown_scope(client):
    r = client.get("/api/queue/scope-preview", params={"scope": "bogus"})
    assert r.status_code == 422


def test_held_cleared_when_hub_reports_offline(client):
    _seed(client.app)
    client.app.state.sync_worker._active_source = "alternative"
    client.app.state.hub.last_state["dashcam_online"] = False
    items = client.get("/api/queue/day/2026-09-01").json()["items"]
    assert all(i["held"] == 0 for i in items)
    client.app.state.hub.last_state["dashcam_online"] = True
    items = client.get("/api/queue/day/2026-09-01").json()["items"]
    assert sorted(i["held"] for i in items) == [0, 1, 1]


# ---- stranded: neither connection's scope would download the clip ----


def _set_settings(client, **patch):
    provider = client.app.state.settings_provider
    provider.update(patch, actor="test")


def test_stranded_when_both_scopes_exclude(client):
    _seed(client.app)
    _set_settings(
        client, ADDRESS_FALLBACK="car.example",
        PRIMARY_SCOPE="ro_only", ALTERNATIVE_SCOPE="ro_only",
    )
    client.app.state.sync_worker._active_source = "primary"
    items = client.get("/api/queue/day/2026-09-01").json()["items"]
    by = {i["filename"]: i for i in items}
    assert by["2026_0901_100000_0001F.MP4"]["held"] == 1
    assert by["2026_0901_100000_0001F.MP4"]["stranded"] == 1
    assert by["2026_0901_100200_0003F.MP4"]["stranded"] == 0
    (day,) = client.get("/api/queue/days").json()["days"]
    assert day["held_count"] == 2 and day["stranded_count"] == 2


def test_not_stranded_when_other_connection_would_download(client):
    _seed(client.app)
    _set_settings(client, ADDRESS_FALLBACK="car.example")   # alt is ro_only
    client.app.state.sync_worker._active_source = "alternative"
    items = client.get("/api/queue/day/2026-09-01").json()["items"]
    assert sorted(i["held"] for i in items) == [0, 1, 1]
    assert all(i["stranded"] == 0 for i in items)   # primary = everything


def test_stranded_when_other_address_blank(client):
    _seed(client.app)
    # Primary ro_only, no alternative address at all: held == stranded.
    _set_settings(client, PRIMARY_SCOPE="ro_only")
    client.app.state.sync_worker._active_source = "primary"
    items = client.get("/api/queue/day/2026-09-01").json()["items"]
    assert sum(i["held"] for i in items) == 2
    assert sum(i["stranded"] for i in items) == 2


def test_scope_preview_endpoint_with_other_scope(client):
    _seed(client.app)
    r = client.get(
        "/api/queue/scope-preview",
        params={"scope": "nothing", "other_scope": "no_parking"},
    )
    assert r.status_code == 200
    assert r.json() == {"scope": "nothing", "now": 0, "held": 3, "stranded": 1}
    # Omitted other_scope = no other connection → all held are stranded.
    r = client.get("/api/queue/scope-preview", params={"scope": "nothing"})
    assert r.json() == {"scope": "nothing", "now": 0, "held": 3, "stranded": 3}
    r = client.get(
        "/api/queue/scope-preview",
        params={"scope": "nothing", "other_scope": "bogus"},
    )
    assert r.status_code == 422
