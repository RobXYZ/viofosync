"""Live GPS (cmd 8058) and camera geofence zones (cmd 9350).

Replies are shaped on a real A229 Pro (fw V1.5_260813); coordinates are made
up. The camera reports latitude/longitude WITHOUT a hemisphere sign, so the
layer restores it from the user's saved locations.
"""
from __future__ import annotations

import pytest

from viofosync_lib import _control as control

GPS_REPLY = """<?xml version="1.0" encoding="UTF-8" ?>
<Function>
<timestamp>1790526750</timestamp><lng>1.500000</lng>
<lat>52.250000</lat>
<alt>62.351002</alt>
<spd>0.000000</spd>
<LIST>
<SAT>
<SatNum>10</SatNum>
<SNR>27</SNR>
</SAT>
<SAT>
<SatNum>12</SatNum>
<SNR>0</SNR>
</SAT>
<SAT>
<SatNum>19</SatNum>
<SNR>35</SNR>
</SAT>
</LIST>
</Function>"""

NO_FIX_REPLY = """<?xml version="1.0" encoding="UTF-8" ?>
<Function>
<timestamp>0</timestamp><lng>0.000000</lng>
<lat>0.000000</lat>
<alt>0.000000</alt>
<spd>0.000000</spd>
<LIST>
</LIST>
</Function>"""

ZONES_REPLY = """<?xml version="1.0" encoding="UTF-8" ?>
<Function>
<Status>0</Status>
<Error>ok</Error>
<Zone1>1</Zone1>
<Zone2>0</Zone2>
<Zone3>0</Zone3>
<GPS1>52.2500,1.5000,50</GPS1>
<GPS2>0.0000,0.0000,0</GPS2>
<GPS3>0.0000,0.0000,0</GPS3>
</Function>"""

HOME_WEST = [(52.2501, -1.5001)]   # a saved location west of Greenwich


@pytest.fixture
def fake_camera(monkeypatch):
    """Route _http_get to canned replies; record every URL requested."""
    calls: list[str] = []
    replies: dict[str, str] = {}

    def fake_get(url):
        calls.append(url)
        for needle, body in replies.items():
            if needle in url:
                return body
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr(control, "_http_get", fake_get)
    return calls, replies


# --------------------------------------------------------------------------- #
# Hemisphere restoration
# --------------------------------------------------------------------------- #

def test_signed_coords_picks_sign_nearest_a_reference():
    assert control._signed_coords(52.25, 1.5, HOME_WEST) == (52.25, -1.5)
    assert control._signed_coords(52.25, 1.5, [(52.25, 1.49)]) == (52.25, 1.5)
    assert control._signed_coords(33.9, 151.2, [(-33.8, 151.1)]) == (-33.9, 151.2)


def test_signed_coords_without_references_is_unchanged():
    assert control._signed_coords(52.25, 1.5, []) == (52.25, 1.5)


def test_signed_coords_keeps_an_already_signed_value():
    # If a firmware does sign its output, a reference must not flip it back.
    assert control._signed_coords(52.25, -1.5, HOME_WEST) == (52.25, -1.5)


# --------------------------------------------------------------------------- #
# Live GPS
# --------------------------------------------------------------------------- #

def test_read_gps_parses_fix_and_satellites(fake_camera):
    calls, replies = fake_camera
    replies["cmd=8058"] = GPS_REPLY
    g = control.read_gps("cam", HOME_WEST)
    assert g["supported"] is True
    assert g["fix"] is True
    assert (g["lat"], g["lon"]) == (52.25, -1.5)
    assert g["sign_inferred"] is True
    assert g["timestamp"] == 1790526750
    assert g["altitude_m"] == pytest.approx(62.351)
    assert g["speed"] == 0.0
    assert g["satellites"] == [{"num": 10, "snr": 27}, {"num": 12, "snr": 0},
                               {"num": 19, "snr": 35}]
    assert (g["tracked"], g["in_view"]) == (2, 3)


def test_read_gps_no_fix(fake_camera):
    _, replies = fake_camera
    replies["cmd=8058"] = NO_FIX_REPLY
    g = control.read_gps("cam", HOME_WEST)
    assert g["fix"] is False
    assert g["lat"] is None and g["lon"] is None
    assert (g["tracked"], g["in_view"]) == (0, 0)


def test_read_gps_unsupported_command(fake_camera):
    _, replies = fake_camera
    replies["cmd=8058"] = "<Function><Cmd>8058</Cmd><Status>-256</Status></Function>"
    assert control.read_gps("cam", HOME_WEST) == {"supported": False}


# --------------------------------------------------------------------------- #
# Geofence zones
# --------------------------------------------------------------------------- #

def test_geofence_cmd_from_map():
    assert control.geofence_cmd("A229P") == 9350
    assert control.geofence_cmd("S340") == 9355
    assert control.geofence_cmd("A119 MINI") is None


def test_read_geofence_parses_zones(fake_camera):
    calls, replies = fake_camera
    replies["cmd=9350"] = ZONES_REPLY
    z = control.read_geofence("cam", "A229P", HOME_WEST)
    assert z["supported"] is True
    assert z["zones"][0] == {"zone": 1, "enabled": True, "configured": True,
                             "lat": 52.25, "lon": -1.5, "radius_m": 50}
    assert z["zones"][1] == {"zone": 2, "enabled": False, "configured": False,
                             "lat": None, "lon": None, "radius_m": None}
    assert len(z["zones"]) == 3
    assert calls == ["http://cam/?custom=1&cmd=9350"]


def test_read_geofence_unsupported_model_sends_nothing(fake_camera):
    calls, _ = fake_camera
    assert control.read_geofence("cam", "A119 MINI", []) == {
        "supported": False, "zones": []}
    assert calls == []


def test_read_geofence_reply_without_zones_is_unsupported(fake_camera):
    _, replies = fake_camera
    replies["cmd=9350"] = "<Function><Status>-256</Status></Function>"
    assert control.read_geofence("cam", "A229P", [])["supported"] is False


def test_enable_zone_sends_zero_based_index(fake_camera):
    calls, replies = fake_camera
    replies["cmd=9350"] = ZONES_REPLY.replace("<Zone3>0", "<Zone3>1")
    z = control.set_geofence_zone("cam", "A229P", 3, True, [])
    assert calls == ["http://cam/?custom=1&cmd=9350&str=on&zone=2"]
    assert z["zones"][2]["enabled"] is True


def test_disable_zone_is_verified_from_the_reply(fake_camera):
    _, replies = fake_camera
    replies["cmd=9350"] = ZONES_REPLY          # Zone1 still reads enabled
    with pytest.raises(control.ControlError, match="didn't"):
        control.set_geofence_zone("cam", "A229P", 1, False, [])


def test_capture_zone_sends_update_with_range(fake_camera):
    calls, replies = fake_camera
    replies["cmd=9350"] = ZONES_REPLY.replace(
        "<GPS2>0.0000,0.0000,0", "<GPS2>52.2500,1.5000,120")
    z = control.capture_geofence_zone("cam", "A229P", 2, 120, HOME_WEST)
    assert calls == ["http://cam/?custom=1&cmd=9350&str=update&zone=1&range=120"]
    assert z["zones"][1]["radius_m"] == 120
    assert z["zones"][1]["lon"] == -1.5


def test_capture_zone_without_gps_fix(fake_camera):
    _, replies = fake_camera
    replies["cmd=9350"] = ("<Function><Status>-1</Status>"
                           "<Error>GPS Not Available</Error></Function>")
    with pytest.raises(control.GeofenceNoFix):
        control.capture_geofence_zone("cam", "A229P", 1, 50, [])


@pytest.mark.parametrize("zone,radius", [(0, 50), (4, 50), (1, 5), (1, 5000)])
def test_zone_and_radius_are_validated(fake_camera, zone, radius):
    calls, _ = fake_camera
    with pytest.raises(control.ValidationError):
        control.capture_geofence_zone("cam", "A229P", zone, radius, [])
    assert calls == []


def test_unsupported_model_refuses_writes(fake_camera):
    calls, _ = fake_camera
    with pytest.raises(control.ValidationError):
        control.set_geofence_zone("cam", "A119 MINI", 1, True, [])
    assert calls == []


# --------------------------------------------------------------------------- #
# The generic settings list must not offer the dead geofence dropdown
# --------------------------------------------------------------------------- #

def test_catalog_omits_geofence_and_generic_write_refuses_it(monkeypatch):
    monkeypatch.setattr(control, "read_settings", lambda a, m=None: {
        "model": "A229P", "camera_tag": "F", "recording": 1, "settings": []})
    cat = control.writable_catalog("cam", "A229P")
    assert all(s["key"] != "CMD_PARKING_GEOFENCING" for s in cat["settings"])
    with pytest.raises(control.ValidationError, match="Geofence"):
        control.set_setting("cam", "CMD_PARKING_GEOFENCING", 0, model="A229P")
