from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_config_dir: Path, tmp_recordings_dir: Path):
    from web import app as app_mod
    from web import settings as settings_mod
    settings_mod.reset_for_tests()
    application = app_mod.create_app()
    with TestClient(application) as c:
        yield c


def _config(tmp_config_dir: Path) -> dict:
    return json.loads((tmp_config_dir / "config.json").read_text())


BASE = {"password": "twelve-chars-min!", "confirm": "twelve-chars-min!"}


def test_full_form_persists_every_setting(client, tmp_config_dir: Path) -> None:
    r = client.post("/setup", data={
        **BASE,
        "address": "192.168.1.230",
        "scope": "no_parking",
        "retention_days": "30",
        "retention_disk_pct": "90",
        "quota_gb": "500",
        "units": "miles",
        "geocode": "on",
    }, follow_redirects=False)
    assert r.status_code == 303

    cfg = _config(tmp_config_dir)
    assert cfg["ADDRESS"] == "192.168.1.230"
    assert cfg["PRIMARY_SCOPE"] == "no_parking"
    assert cfg["RETENTION_MAX_DAYS"] == 30
    assert cfg["RETENTION_DISK_PCT"] == 90
    assert cfg["RECORDINGS_QUOTA_GB"] == 500
    assert cfg["DISTANCE_UNITS"] == "miles"
    assert cfg["GEOCODE_ENABLED"] is True


def test_wizard_enables_gps_triage_for_new_installs(client, tmp_config_dir: Path) -> None:
    client.post("/setup", data={**BASE, "address": "192.168.1.230"},
                follow_redirects=False)
    assert _config(tmp_config_dir)["PRIMARY_GPS_TRIAGE"] is True


def test_schema_default_for_triage_stays_off() -> None:
    """Defaults are merged at read time, so a True default here would
    enable triage on existing installs. The wizard writes it explicitly."""
    from web.settings_schema import SettingsModel

    assert SettingsModel().PRIMARY_GPS_TRIAGE is False
    assert SettingsModel().ALTERNATIVE_GPS_TRIAGE is False


def test_existing_config_does_not_gain_triage(tmp_config_dir: Path) -> None:
    from web import settings as settings_mod

    (tmp_config_dir / "config.json").write_text(json.dumps({
        "ADDRESS": "192.168.1.230",
        "WEB_PASSWORD_HASH": "$2b$12$abcdefghijklmnopqrstuv",
        "SESSION_SECRET": "0" * 64,
    }))
    settings_mod.reset_for_tests()
    snap = settings_mod.SettingsProvider(
        config_path=tmp_config_dir / "config.json"
    ).get()
    assert snap.primary_gps_triage is False
    assert snap.gps_triage is False


def test_home_location_is_persisted_and_normalised(client, tmp_config_dir: Path) -> None:
    r = client.post("/setup", data={
        **BASE,
        "home_lat": "53.9591",
        "home_lon": "-1.0815",
        "home_radius": "45",
    }, follow_redirects=False)
    assert r.status_code == 303

    locs = _config(tmp_config_dir)["LOCATIONS"]
    assert len(locs) == 1
    assert locs[0]["name"] == "Home"
    assert locs[0]["radius_m"] == 45
    assert locs[0]["is_home"] is True
    assert locs[0]["exclude_recordings"] is False
    assert locs[0]["lat"] == pytest.approx(53.9591)


def test_home_is_optional(client, tmp_config_dir: Path) -> None:
    r = client.post("/setup", data={**BASE}, follow_redirects=False)
    assert r.status_code == 303
    assert _config(tmp_config_dir).get("LOCATIONS", []) == []


def test_partial_home_coordinates_are_ignored(client, tmp_config_dir: Path) -> None:
    """A latitude with no longitude is not a location."""
    r = client.post("/setup", data={**BASE, "home_lat": "53.9591"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert _config(tmp_config_dir).get("LOCATIONS", []) == []


def test_unchecked_geocode_box_disables_geocoding(client, tmp_config_dir: Path) -> None:
    """An unchecked checkbox submits no field, and the schema default is
    True, so absence must be read as an explicit opt-out."""
    r = client.post("/setup", data={**BASE}, follow_redirects=False)
    assert r.status_code == 303
    assert _config(tmp_config_dir)["GEOCODE_ENABLED"] is False


def test_checked_geocode_box_enables_geocoding(client, tmp_config_dir: Path) -> None:
    r = client.post("/setup", data={**BASE, "geocode": "on"}, follow_redirects=False)
    assert r.status_code == 303
    assert _config(tmp_config_dir)["GEOCODE_ENABLED"] is True


def test_disk_pct_above_critical_gives_a_wizard_level_error(client) -> None:
    r = client.post("/setup", data={**BASE, "retention_disk_pct": "97"},
                    follow_redirects=False)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "95" in detail
    # Reports the limit rather than naming a setting the wizard never shows.
    assert "DISK_CRITICAL_PCT" not in detail


def test_disk_pct_at_the_limit_is_accepted(client, tmp_config_dir: Path) -> None:
    r = client.post("/setup", data={**BASE, "retention_disk_pct": "95"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert _config(tmp_config_dir)["RETENTION_DISK_PCT"] == 95
