# tests/test_triage_lifecycle.py
"""Wiring of triage into the app lifespan.

Two behaviours pinned here:
- startup sweep: orphaned skeleton sidecars are removed when the app starts.
- purge on disable: turning off GPS triage on the last enabled connection via
  settings triggers purge_all, removing the .triage directory and clearing
  triage columns.
"""
from __future__ import annotations

import bcrypt
from fastapi.testclient import TestClient

from web import settings as settings_mod


class _FakeMqtt:
    def __init__(self, **kwargs):
        pass

    def start(self):
        pass

    async def stop(self):
        pass

    async def on_settings_changed(self, keys, snap):
        pass


def _setup_provider_with_password():
    """Configure a fresh provider with a hashed password so the app
    doesn't stay stuck in setup mode."""
    settings_mod.reset_for_tests()
    p = settings_mod.get_provider()
    data = p._store.load()
    data["WEB_PASSWORD_HASH"] = bcrypt.hashpw(b"pw" * 8, bcrypt.gensalt()).decode()
    p._store.write(data)
    settings_mod.reset_for_tests()


def test_startup_sweep_removes_orphaned_skeleton(
    tmp_config_dir, tmp_recordings_dir, monkeypatch
):
    """Skeleton sidecars with no live queue row must be swept at boot."""
    from web.app import create_app
    from web.services.sync_worker import SyncWorker

    _setup_provider_with_password()
    monkeypatch.setattr(SyncWorker, "start", lambda self: None)
    monkeypatch.setattr("web.app.MqttService", _FakeMqtt)

    # Write an orphaned skeleton (no matching queue row exists).
    triage_dir = tmp_recordings_dir / ".triage"
    triage_dir.mkdir(parents=True)
    orphan = triage_dir / "2026_0618_120000_0001F.MP4.gpx"
    orphan.write_text("<gpx/>")

    app = create_app()
    with TestClient(app):
        assert not orphan.exists(), (
            "orphaned skeleton sidecar was not swept on startup"
        )

    settings_mod.reset_for_tests()


def test_purge_on_disable_removes_triage_dir(
    tmp_config_dir, tmp_recordings_dir, monkeypatch
):
    """Turning triage off on the last enabled connection must purge .triage."""
    from web.app import create_app
    from web.services.sync_worker import SyncWorker

    _setup_provider_with_password()
    monkeypatch.setattr(SyncWorker, "start", lambda self: None)
    monkeypatch.setattr("web.app.MqttService", _FakeMqtt)

    # Enable GPS triage on the primary connection in the stored config so we
    # can turn it off inside the lifespan and trigger the subscriber.
    settings_mod.reset_for_tests()
    p = settings_mod.get_provider()
    p.update({"PRIMARY_GPS_TRIAGE": True}, actor="test")

    app = create_app()
    with TestClient(app):
        # Create a skeleton sidecar so there's something to purge.
        triage_dir = tmp_recordings_dir / ".triage"
        triage_dir.mkdir(parents=True, exist_ok=True)
        skel = triage_dir / "2026_0618_120000_0001F.MP4.gpx"
        skel.write_text("<gpx/>")

        # Disable triage — the subscriber must call purge_all synchronously.
        provider = settings_mod.get_provider()
        provider.update({"PRIMARY_GPS_TRIAGE": False}, actor="test")

        assert not triage_dir.exists(), (
            ".triage directory was not purged when GPS triage was disabled"
        )

    settings_mod.reset_for_tests()


def test_no_purge_while_other_connection_still_triages(
    tmp_config_dir, tmp_recordings_dir, monkeypatch
):
    """snap.gps_triage stays True while any connection has triage on, so the
    purge must not fire when only one of two enabled connections is switched off."""
    from web.app import create_app
    from web.services.sync_worker import SyncWorker

    _setup_provider_with_password()
    monkeypatch.setattr(SyncWorker, "start", lambda self: None)
    monkeypatch.setattr("web.app.MqttService", _FakeMqtt)

    # Enable GPS triage on both connections in the stored config so we can
    # turn just one off inside the lifespan and confirm the subscriber
    # doesn't purge.
    settings_mod.reset_for_tests()
    p = settings_mod.get_provider()
    p.update({"PRIMARY_GPS_TRIAGE": True, "ALTERNATIVE_GPS_TRIAGE": True}, actor="test")

    app = create_app()
    with TestClient(app):
        # Create a skeleton sidecar so there's something that could be purged.
        triage_dir = tmp_recordings_dir / ".triage"
        triage_dir.mkdir(parents=True, exist_ok=True)
        skel = triage_dir / "2026_0618_120000_0001F.MP4.gpx"
        skel.write_text("<gpx/>")

        # Disable triage on only one connection — the other is still on, so
        # the subscriber must not purge.
        provider = settings_mod.get_provider()
        provider.update({"PRIMARY_GPS_TRIAGE": False}, actor="test")

        assert triage_dir.exists(), (
            ".triage directory was purged while another connection still triages"
        )
        assert skel.exists(), (
            "skeleton sidecar was removed while another connection still triages"
        )

    settings_mod.reset_for_tests()
