"""Debug-bundle redaction: bundles are attached to public GitHub
issues, so configured network addresses must be masked everywhere,
including inside log lines."""
from __future__ import annotations

from web.services.debug_bundle import mask_address, redact_text


def test_mask_ipv4_keeps_first_and_last_octet():
    assert mask_address("192.0.2.10") == "192.x.x.10"


def test_mask_hostname_keeps_first_and_last_label():
    assert mask_address("dashcam.home.example.com") == "dashcam.x.x.com"


def test_mask_short_values_degrade_gracefully():
    assert mask_address("dashcam.local") == "dashcam.x"
    assert mask_address("localhost") == "localhost"
    assert mask_address("") == ""
    assert mask_address(None) == ""


def test_mask_colon_forms():
    # Bare IPv6-ish hosts: first+x+last masking applied over ':'.
    assert mask_address("fd00::1") == "fd00:x:1"
    assert mask_address("2001:db8::5") == "2001:x:x:5"
    # host:port: the port survives, the host is masked recursively.
    assert mask_address("192.0.2.10:8080") == "192.x.x.10:8080"
    assert mask_address("dashcam.local:8080") == "dashcam.x:8080"


def test_redact_text_replaces_all_configured_values():
    text = "probe 192.0.2.10 failed; fallback dashcam.local also down"
    out = redact_text(text, ["192.0.2.10", "dashcam.local", "", None])
    assert "192.0.2.10" not in out
    assert "dashcam.local" not in out
    assert "192.x.x.10" in out
    assert "dashcam.x" in out
