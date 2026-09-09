"""profile_for(): resolve the (scope, gps_triage) pair for a connection."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from web.services.profiles import ConnectionProfile, profile_for


def _snap(**kw):
    base = dict(
        primary_scope="everything", primary_gps_triage=False,
        alternative_scope="ro_only", alternative_gps_triage=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_primary() -> None:
    assert profile_for(_snap(), "primary") == ConnectionProfile(
        scope="everything", gps_triage=False
    )


def test_alternative() -> None:
    assert profile_for(_snap(), "alternative") == ConnectionProfile(
        scope="ro_only", gps_triage=True
    )


def test_unknown_source_raises() -> None:
    with pytest.raises(ValueError):
        profile_for(_snap(), "offline")


def test_missing_attribute_is_loud() -> None:
    # A fake snapshot lacking the fields must fail, not silently default.
    with pytest.raises(AttributeError):
        profile_for(SimpleNamespace(primary_scope="everything"), "primary")
