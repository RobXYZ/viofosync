"""Tests for the camera -> timeline-channel mapping."""
from __future__ import annotations

from web.services import naming


def test_channel_of_front_variants():
    assert naming.channel_of("F") == "front"
    assert naming.channel_of("PF") == "front"   # parking front
    assert naming.channel_of("EF") == "front"   # event front
    assert naming.channel_of("f") == "front"    # case-insensitive


def test_channel_of_rear_variants():
    assert naming.channel_of("R") == "rear"
    assert naming.channel_of("PR") == "rear"


def test_channel_of_interior():
    assert naming.channel_of("I") == "interior"


def test_channel_of_unknown_and_empty():
    assert naming.channel_of("") == "other"
    assert naming.channel_of("X") == "other"
    assert naming.channel_of(None) == "other"


def test_channel_order_and_labels():
    assert naming.CHANNEL_ORDER == ["front", "rear", "interior", "other"]
    assert naming.CHANNEL_LABELS["front"] == "Front"
    assert naming.CHANNEL_LABELS["rear"] == "Rear"
    assert naming.CHANNEL_LABELS["interior"] == "Interior"
    assert naming.CHANNEL_LABELS["other"] == "Other"
