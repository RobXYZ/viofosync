"""Per-connection download profiles: schema keys, defaults, validation."""
from __future__ import annotations

import json

import pytest

from web.settings import SettingsProvider
from web.settings_schema import (
    DEFAULT_VALUES,
    EDITABLE_KEYS,
    SCOPES,
    SettingsModel,
    validate_partial,
)

PROFILE_KEYS = (
    "PRIMARY_SCOPE", "PRIMARY_GPS_TRIAGE",
    "ALTERNATIVE_SCOPE", "ALTERNATIVE_GPS_TRIAGE",
)


def test_scopes_constant() -> None:
    assert SCOPES == ("everything", "no_parking", "ro_only", "nothing")


def test_defaults_reproduce_old_behaviour() -> None:
    m = SettingsModel()
    assert m.PRIMARY_SCOPE == "everything"
    assert m.ALTERNATIVE_SCOPE == "everything"
    assert m.PRIMARY_GPS_TRIAGE is False
    assert m.ALTERNATIVE_GPS_TRIAGE is False


def test_profile_keys_are_editable() -> None:
    for k in PROFILE_KEYS:
        assert k in EDITABLE_KEYS
        assert k in DEFAULT_VALUES


def test_legacy_keys_are_gone() -> None:
    assert "SYNC_RO_ONLY" not in EDITABLE_KEYS
    assert "GPS_TRIAGE" not in EDITABLE_KEYS
    assert "SYNC_RO_ONLY" not in SettingsModel.model_fields
    assert "GPS_TRIAGE" not in SettingsModel.model_fields
    with pytest.raises(ValueError):
        validate_partial({"SYNC_RO_ONLY": True})


@pytest.mark.parametrize("scope", SCOPES)
def test_validate_partial_accepts_every_scope(scope: str) -> None:
    assert validate_partial({"ALTERNATIVE_SCOPE": scope}) == {
        "ALTERNATIVE_SCOPE": scope
    }


def test_validate_partial_rejects_unknown_scope() -> None:
    with pytest.raises(ValueError):
        validate_partial({"PRIMARY_SCOPE": "most_things"})


def test_validate_partial_coerces_triage_bool() -> None:
    assert validate_partial({"PRIMARY_GPS_TRIAGE": "on"}) == {
        "PRIMARY_GPS_TRIAGE": True
    }


def _write_config(tmp_config_dir, data: dict) -> None:
    (tmp_config_dir / "config.json").write_text(json.dumps(data))


def _read_config(tmp_config_dir) -> dict:
    return json.loads((tmp_config_dir / "config.json").read_text())


def test_snapshot_exposes_profile_fields(tmp_config_dir, tmp_recordings_dir):
    p = SettingsProvider()
    snap = p.get()
    assert snap.primary_scope == "everything"
    assert snap.alternative_scope == "everything"
    assert snap.primary_gps_triage is False
    assert snap.alternative_gps_triage is False
    assert snap.gps_triage is False
    assert not hasattr(snap, "sync_ro_only")


def test_snapshot_gps_triage_is_any_connection(tmp_config_dir, tmp_recordings_dir):
    p = SettingsProvider()
    snap = p.update({"ALTERNATIVE_GPS_TRIAGE": True}, actor="test")
    assert snap.primary_gps_triage is False
    assert snap.alternative_gps_triage is True
    assert snap.gps_triage is True


def test_migration_ro_only_true_seeds_both_scopes(tmp_config_dir, tmp_recordings_dir):
    _write_config(tmp_config_dir, {"SYNC_RO_ONLY": True, "GPS_TRIAGE": True})
    p = SettingsProvider()
    snap = p.get()
    assert snap.primary_scope == "ro_only"
    assert snap.alternative_scope == "ro_only"
    assert snap.primary_gps_triage is True
    assert snap.alternative_gps_triage is True
    on_disk = _read_config(tmp_config_dir)
    assert "SYNC_RO_ONLY" not in on_disk
    assert "GPS_TRIAGE" not in on_disk
    assert on_disk["PRIMARY_SCOPE"] == "ro_only"


def test_migration_ro_only_false(tmp_config_dir, tmp_recordings_dir):
    _write_config(tmp_config_dir, {"SYNC_RO_ONLY": False, "GPS_TRIAGE": False})
    snap = SettingsProvider().get()
    assert snap.primary_scope == "everything"
    assert snap.alternative_scope == "everything"
    assert snap.gps_triage is False


def test_migration_only_one_legacy_key_present(tmp_config_dir, tmp_recordings_dir):
    _write_config(tmp_config_dir, {"GPS_TRIAGE": True})
    snap = SettingsProvider().get()
    assert snap.primary_scope == "everything"
    assert snap.primary_gps_triage is True
    assert snap.alternative_gps_triage is True
    assert "GPS_TRIAGE" not in _read_config(tmp_config_dir)


def test_migration_is_idempotent_and_respects_new_keys(tmp_config_dir, tmp_recordings_dir):
    # A config already on the new keys, with a stale legacy key lingering,
    # must keep the new values — the legacy key is only dropped.
    _write_config(tmp_config_dir, {
        "PRIMARY_SCOPE": "no_parking", "ALTERNATIVE_SCOPE": "nothing",
        "PRIMARY_GPS_TRIAGE": True, "ALTERNATIVE_GPS_TRIAGE": False,
        "SYNC_RO_ONLY": True,
    })
    snap = SettingsProvider().get()
    assert snap.primary_scope == "no_parking"
    assert snap.alternative_scope == "nothing"
    assert snap.alternative_gps_triage is False
    first = _read_config(tmp_config_dir)
    assert "SYNC_RO_ONLY" not in first
    # Second boot: no change.
    SettingsProvider().get()
    assert _read_config(tmp_config_dir) == first


@pytest.mark.parametrize(
    ("raw", "expected_scope", "expected_triage"),
    [
        (True, "ro_only", True), ("true", "ro_only", True), ("TRUE", "ro_only", True),
        (1, "ro_only", True), ("on", "ro_only", True), ("yes", "ro_only", True),
        (False, "everything", False), ("false", "everything", False),
        (0, "everything", False), ("", "everything", False), ("off", "everything", False),
    ],
)
def test_migration_coerces_string_booleans(
    tmp_config_dir, tmp_recordings_dir, raw, expected_scope, expected_triage
):
    _write_config(tmp_config_dir, {"SYNC_RO_ONLY": raw, "GPS_TRIAGE": raw})
    snap = SettingsProvider().get()
    assert snap.primary_scope == expected_scope
    assert snap.alternative_scope == expected_scope
    assert snap.primary_gps_triage is expected_triage
    assert snap.alternative_gps_triage is expected_triage


def test_fresh_install_has_no_legacy_or_forced_keys(tmp_config_dir, tmp_recordings_dir):
    SettingsProvider().get()
    on_disk = _read_config(tmp_config_dir)
    assert "SYNC_RO_ONLY" not in on_disk
    # Defaults apply without being materialised on disk.
    assert "PRIMARY_SCOPE" not in on_disk


def test_api_projection_round_trips(tmp_config_dir, tmp_recordings_dir):
    from web.routers.settings import _editable_values
    p = SettingsProvider()
    snap = p.update({"ALTERNATIVE_SCOPE": "ro_only",
                     "PRIMARY_GPS_TRIAGE": True}, actor="test")
    e = _editable_values(snap)
    assert e["ALTERNATIVE_SCOPE"] == "ro_only"
    assert e["PRIMARY_SCOPE"] == "everything"
    assert e["PRIMARY_GPS_TRIAGE"] is True
    assert e["ALTERNATIVE_GPS_TRIAGE"] is False
    assert "SYNC_RO_ONLY" not in e
    assert "GPS_TRIAGE" not in e
