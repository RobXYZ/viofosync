"""Validation rules for the JSON config payload."""
from __future__ import annotations

import pytest

from web.settings_schema import (
    DEFAULT_VALUES,
    EDITABLE_KEYS,
    READONLY_KEYS,
    RESTART_REQUIRED_KEYS,
    SettingsModel,
    validate_partial,
)


def test_default_values_form_a_valid_full_config() -> None:
    model = SettingsModel(**DEFAULT_VALUES)
    assert model.GROUPING == "daily"
    assert model.HTML is True
    assert model.WEB_PORT == 8080


def test_address_must_look_like_hostname_or_ip() -> None:
    SettingsModel(**{**DEFAULT_VALUES, "ADDRESS": "192.168.1.230"})
    SettingsModel(**{**DEFAULT_VALUES, "ADDRESS": "dashcam.local"})
    SettingsModel(**{**DEFAULT_VALUES, "ADDRESS": None})  # unset is fine

    with pytest.raises(ValueError):
        SettingsModel(**{**DEFAULT_VALUES, "ADDRESS": "not a host"})


def test_address_fallback_validates_like_address() -> None:
    SettingsModel(**{**DEFAULT_VALUES, "ADDRESS_FALLBACK": "192.168.2.230"})
    SettingsModel(**{**DEFAULT_VALUES, "ADDRESS_FALLBACK": "dashcam.vpn"})
    SettingsModel(**{**DEFAULT_VALUES, "ADDRESS_FALLBACK": None})   # unset is fine
    SettingsModel(**{**DEFAULT_VALUES, "ADDRESS_FALLBACK": ""})     # empty → None

    with pytest.raises(ValueError):
        SettingsModel(**{**DEFAULT_VALUES, "ADDRESS_FALLBACK": "not a host"})


def test_address_fallback_is_editable() -> None:
    assert "ADDRESS_FALLBACK" in EDITABLE_KEYS


def test_address_fallback_empty_becomes_none() -> None:
    m = SettingsModel(**{**DEFAULT_VALUES, "ADDRESS_FALLBACK": ""})
    assert m.ADDRESS_FALLBACK is None


def test_grouping_enum_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        SettingsModel(**{**DEFAULT_VALUES, "GROUPING": "hourly"})


def test_timeout_range() -> None:
    SettingsModel(**{**DEFAULT_VALUES, "TIMEOUT": 1})
    SettingsModel(**{**DEFAULT_VALUES, "TIMEOUT": 60})
    with pytest.raises(ValueError):
        SettingsModel(**{**DEFAULT_VALUES, "TIMEOUT": 0})
    with pytest.raises(ValueError):
        SettingsModel(**{**DEFAULT_VALUES, "TIMEOUT": 61})


def test_download_concurrency_range() -> None:
    SettingsModel(**{**DEFAULT_VALUES, "DOWNLOAD_CONCURRENCY": 1})
    SettingsModel(**{**DEFAULT_VALUES, "DOWNLOAD_CONCURRENCY": 4})
    with pytest.raises(ValueError):
        SettingsModel(**{**DEFAULT_VALUES, "DOWNLOAD_CONCURRENCY": 0})
    with pytest.raises(ValueError):
        SettingsModel(**{**DEFAULT_VALUES, "DOWNLOAD_CONCURRENCY": 5})


def test_download_concurrency_is_editable() -> None:
    assert "DOWNLOAD_CONCURRENCY" in EDITABLE_KEYS


def test_web_port_range() -> None:
    SettingsModel(**{**DEFAULT_VALUES, "WEB_PORT": 1})
    SettingsModel(**{**DEFAULT_VALUES, "WEB_PORT": 65535})
    with pytest.raises(ValueError):
        SettingsModel(**{**DEFAULT_VALUES, "WEB_PORT": 0})
    with pytest.raises(ValueError):
        SettingsModel(**{**DEFAULT_VALUES, "WEB_PORT": 70000})


def test_export_encoder_enum() -> None:
    for v in ("auto", "software", "videotoolbox", "nvenc", "qsv", "vaapi"):
        SettingsModel(**{**DEFAULT_VALUES, "EXPORT_ENCODER": v})
    with pytest.raises(ValueError):
        SettingsModel(**{**DEFAULT_VALUES, "EXPORT_ENCODER": "av1"})


def test_validate_partial_only_known_editable_keys() -> None:
    out = validate_partial({"TIMEOUT": 20, "GROUPING": "weekly"})
    assert out == {"TIMEOUT": 20, "GROUPING": "weekly"}


def test_validate_partial_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown"):
        validate_partial({"NUKE": "yes"})


def test_validate_partial_rejects_readonly_keys() -> None:
    with pytest.raises(ValueError, match="read-only"):
        validate_partial({"PUID": "1001"})


def test_validate_partial_coerces_string_bools() -> None:
    out = validate_partial({"GPS_EXTRACT": "1"})
    assert out == {"GPS_EXTRACT": True}
    out = validate_partial({"HTML": "false"})
    assert out == {"HTML": False}


def test_validate_partial_coerces_string_ints() -> None:
    out = validate_partial({"TIMEOUT": "15"})
    assert out == {"TIMEOUT": 15}


def test_password_min_length() -> None:
    from web.settings_schema import validate_new_password

    with pytest.raises(ValueError, match="8"):
        validate_new_password("short")
    validate_new_password("eight-or-more")


def test_restart_required_keys_subset_of_editable() -> None:
    assert RESTART_REQUIRED_KEYS <= EDITABLE_KEYS


def test_readonly_keys_disjoint_from_editable() -> None:
    assert READONLY_KEYS.isdisjoint(EDITABLE_KEYS)


def test_delete_after_download_default_false() -> None:
    model = SettingsModel(**DEFAULT_VALUES)
    assert model.DELETE_AFTER_DOWNLOAD is False


def test_delete_after_download_in_editable_keys() -> None:
    assert "DELETE_AFTER_DOWNLOAD" in EDITABLE_KEYS


def test_delete_after_download_partial_update_coerces_string() -> None:
    out = validate_partial({"DELETE_AFTER_DOWNLOAD": "1"})
    assert out == {"DELETE_AFTER_DOWNLOAD": True}


def test_new_retention_and_ro_only_defaults() -> None:
    from web.settings_schema import SettingsModel
    m = SettingsModel()
    assert m.SYNC_RO_ONLY is False
    assert m.RETENTION_MAX_DAYS == 0
    assert m.RETENTION_DISK_PCT == 0
    assert m.RETENTION_PROTECT_RO is True


def test_retention_max_days_rejects_negative() -> None:
    import pytest
    from pydantic import ValidationError

    from web.settings_schema import SettingsModel
    with pytest.raises(ValidationError):
        SettingsModel(RETENTION_MAX_DAYS=-1)


def test_retention_disk_pct_rejects_out_of_range() -> None:
    import pytest
    from pydantic import ValidationError

    from web.settings_schema import SettingsModel
    with pytest.raises(ValidationError):
        SettingsModel(RETENTION_DISK_PCT=100)
    with pytest.raises(ValidationError):
        SettingsModel(RETENTION_DISK_PCT=-1)


def test_distance_units_default_and_validation() -> None:
    """km / miles only — anything else is a validation error."""
    import pytest
    from pydantic import ValidationError

    from web.settings_schema import SettingsModel
    assert SettingsModel().DISTANCE_UNITS == "km"
    SettingsModel(DISTANCE_UNITS="miles")
    SettingsModel(DISTANCE_UNITS="km")
    with pytest.raises(ValidationError):
        SettingsModel(DISTANCE_UNITS="furlongs")


def test_pip_position_default_is_top_right() -> None:
    """PiP defaults to top_right; the four corners are the only
    accepted values."""
    import pytest
    from pydantic import ValidationError

    from web.settings_schema import SettingsModel
    assert SettingsModel().PIP_POSITION == "top_right"
    for pos in ("top_right", "top_left", "bottom_right", "bottom_left"):
        SettingsModel(PIP_POSITION=pos)
    with pytest.raises(ValidationError):
        SettingsModel(PIP_POSITION="middle")


def test_disk_critical_pct_default_is_95():
    from web.settings_schema import SettingsModel
    m = SettingsModel()
    assert m.DISK_CRITICAL_PCT == 95


def test_disk_critical_pct_validates_range():
    import pytest as _pt
    from pydantic import ValidationError

    from web.settings_schema import SettingsModel
    with _pt.raises(ValidationError):
        SettingsModel(DISK_CRITICAL_PCT=101)
    with _pt.raises(ValidationError):
        SettingsModel(DISK_CRITICAL_PCT=-1)
    # Boundaries OK
    assert SettingsModel(DISK_CRITICAL_PCT=0).DISK_CRITICAL_PCT == 0
    assert SettingsModel(DISK_CRITICAL_PCT=100).DISK_CRITICAL_PCT == 100


def test_disk_critical_pct_must_exceed_retention_threshold():
    """A critical threshold below the retention threshold would fire before
    retention even tried — reject the combination at validation time."""
    import pytest as _pt
    from pydantic import ValidationError

    from web.settings_schema import SettingsModel
    with _pt.raises(ValidationError):
        SettingsModel(RETENTION_DISK_PCT=90, DISK_CRITICAL_PCT=80)
    # Equal is allowed (no overlap means no firing before retention)
    SettingsModel(RETENTION_DISK_PCT=90, DISK_CRITICAL_PCT=95)


def test_snapshot_exposes_disk_critical_pct():
    import tempfile

    from web.settings import SettingsProvider
    with tempfile.TemporaryDirectory() as d:
        sp = SettingsProvider(config_path=f"{d}/c.json",
                               env_file_path=f"{d}/v.env",
                               recordings_dir=d)
        assert sp.get().disk_critical_pct == 95


def test_import_path_round_trips_and_defaults_empty():
    from web.settings_schema import (
        DEFAULT_VALUES,
        EDITABLE_KEYS,
        validate_partial,
    )
    # Editable + defaults to empty (meaning "<recordings>/import").
    assert "IMPORT_PATH" in EDITABLE_KEYS
    assert DEFAULT_VALUES["IMPORT_PATH"] == ""
    # Round-trips through validate_partial, stripping whitespace.
    out = validate_partial({"IMPORT_PATH": "  /mnt/usb  "})
    assert out["IMPORT_PATH"] == "/mnt/usb"
    assert validate_partial({"IMPORT_PATH": "  "})["IMPORT_PATH"] == ""
