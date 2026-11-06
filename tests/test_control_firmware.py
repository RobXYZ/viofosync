"""Decoding the camera's firmware string (cmd 3012) for the Camera page."""
from __future__ import annotations

import pytest

from viofosync_lib import _control as control


@pytest.mark.parametrize("raw,expected", [
    # A229P: build date, then a second version whose role isn't known.
    ("VIOFO_A229P_V1.5_260813;V1.3_240605",
     {"version": "V1.5", "date": "2026-08-13", "extra": ["V1.3"]}),
    ("VIOFO_A329S_V2.0_260313",
     {"version": "V2.0", "date": "2026-03-13", "extra": []}),
    # 8-digit dates (A119 Mini 2).
    ("VIOFO_A119MINI2_V2.1_20250723",
     {"version": "V2.1", "date": "2025-07-23", "extra": []}),
    # No prefix before the version.
    ("V1.5_260813", {"version": "V1.5", "date": "2026-08-13", "extra": []}),
    # A second part that isn't a version passes through as-is.
    ("VIOFO_A229P_V1.5_260813;MCU_03",
     {"version": "V1.5", "date": "2026-08-13", "extra": ["MCU_03"]}),
    ("VIOFO_A229P_V1.5_260813;",
     {"version": "V1.5", "date": "2026-08-13", "extra": []}),
])
def test_parse_firmware(raw, expected):
    assert control.parse_firmware(raw) == expected


@pytest.mark.parametrize("raw", [
    "VIOFO_A229P_V1.5_261399",     # month 13, day 99
    "VIOFO_A229P_V1.5_260230",     # 30 February
    "VIOFO_A229P_V1.5_20261301",
])
def test_impossible_date_keeps_version_drops_date(raw):
    assert control.parse_firmware(raw) == {
        "version": "V1.5", "date": None, "extra": []}


@pytest.mark.parametrize("raw", [
    "A229P_1.5_260813",            # no V-version
    "VIOFO_A229P_V1.5",            # no date
    "VIOFO_A229P_V1.5_2608",       # neither 6 nor 8 digits
    "", None,
])
def test_undecodable_returns_none(raw):
    assert control.parse_firmware(raw) is None


def test_read_info_includes_decoded_firmware(monkeypatch):
    replies = {
        control.CMD_SYSTEM_INFO: {"String": "VIOFO_A229P_V1.5_260813;V1.3_240605"},
        control.CMD_GET_SENSOR_NUMBER: {"FrontSensor": "1", "Total": "1"},
    }
    monkeypatch.setattr(control, "_send",
                        lambda a, cmd, par=None, s=None: replies.get(cmd, {}))
    info = control.read_info("cam")
    assert info["firmware_parts"] == {
        "version": "V1.5", "date": "2026-08-13", "extra": ["V1.3"]}
