"""Tests for the app version plumbing (web/version.py, /api/auth/me)."""
from __future__ import annotations

import pytest


class _FakeMqttService:
    """Stand-in so these tests don't carry MQTT side effects (see
    test_storage_endpoint.py for the rationale)."""

    def __init__(self, **kwargs):
        self._last_node_id = ""
        self._last_discovery_prefix = ""

    def start(self): pass
    async def stop(self): pass
    async def on_settings_changed(self, keys, snap): pass
    def get_status(self):
        return {"state": "idle", "detail": None, "last_published_at": None}


def test_display_version_defaults_to_dev(monkeypatch):
    from web.version import display_version

    monkeypatch.delenv("VIOFOSYNC_VERSION", raising=False)
    monkeypatch.delenv("VIOFOSYNC_REVISION", raising=False)
    assert display_version() == "dev"


def test_display_version_edge_includes_short_sha(monkeypatch):
    from web.version import display_version

    monkeypatch.setenv("VIOFOSYNC_VERSION", "edge")
    monkeypatch.setenv("VIOFOSYNC_REVISION", "abc1234def5678")
    assert display_version() == "edge (abc1234)"


def test_display_version_edge_without_sha(monkeypatch):
    from web.version import display_version

    monkeypatch.setenv("VIOFOSYNC_VERSION", "edge")
    monkeypatch.delenv("VIOFOSYNC_REVISION", raising=False)
    assert display_version() == "edge"


def test_display_version_branch_build_shows_name_and_sha(monkeypatch):
    from web.version import display_version

    monkeypatch.setenv("VIOFOSYNC_VERSION", "feat-gps-triage")
    monkeypatch.setenv("VIOFOSYNC_REVISION", "abc1234def5678")
    assert display_version() == "feat-gps-triage (abc1234)"


def test_display_version_release_gets_v_prefix(monkeypatch):
    from web.version import display_version

    monkeypatch.setenv("VIOFOSYNC_VERSION", "2.6.1")
    monkeypatch.setenv("VIOFOSYNC_REVISION", "abc1234def5678")
    assert display_version() == "v2.6.1"


@pytest.fixture
def logged_in_client(tmp_config_dir, tmp_recordings_dir, monkeypatch):
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
    p._store.write(data)
    settings_mod.reset_for_tests()

    monkeypatch.setattr(SyncWorker, "start", lambda self: None)
    monkeypatch.setattr("web.app.MqttService", _FakeMqttService)

    app = create_app()
    c = TestClient(app)
    c.__enter__()
    c.post("/api/auth/login", json={"password": "pwpwpwpwpwpwpwpw"})
    yield c
    c.__exit__(None, None, None)
    settings_mod.reset_for_tests()


def test_me_reports_version(logged_in_client, monkeypatch):
    monkeypatch.setenv("VIOFOSYNC_VERSION", "2.6.1")
    r = logged_in_client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["version"] == "v2.6.1"
