"""Topology integrity tests."""
from __future__ import annotations


def test_topology_has_expected_entities():
    from web.services.mqtt_topology import TOPOLOGY
    obj_ids = {e.object_id for e in TOPOLOGY}
    expected = {
        # binary_sensors / sensors (Task 5)
        "dashcam", "sync_status",
        "queue_pending", "queue_failed", "queue_downloading",
        "last_downloaded_clip", "total_clips", "current_filename",
        "current_progress", "disk_used",
    }
    assert expected.issubset(obj_ids), expected - obj_ids


def test_unique_ids_unique_per_node():
    from web.services.mqtt_topology import (
        TOPOLOGY,
        build_unique_id,
    )
    cfg = {"discovery_prefix": "homeassistant", "node_id": "viofosync",
           "version": "0.2.0"}
    uids = [build_unique_id(e.object_id, cfg) for e in TOPOLOGY]
    assert len(uids) == len(set(uids))


def test_sensors_have_state_fn():
    from web.services.mqtt_topology import TOPOLOGY
    for e in TOPOLOGY:
        if e.component in ("sensor", "binary_sensor"):
            assert e.state_fn is not None, e.object_id


def test_default_enabled_set():
    from web.services.mqtt_topology import TOPOLOGY
    enabled_by_default = {
        e.object_id for e in TOPOLOGY if e.enabled_by_default
    }
    assert {
        "dashcam", "sync_status",
        "queue_pending", "last_downloaded_clip", "disk_used",
    }.issubset(enabled_by_default)
    # The verbose ones are off by default
    disabled = {
        e.object_id for e in TOPOLOGY if not e.enabled_by_default
    }
    assert {
        "queue_failed", "queue_downloading", "current_filename",
        "current_progress", "total_clips",
    }.issubset(disabled)


def test_publish_intervals():
    """Coalescing intervals match the spec's table."""
    from web.services.mqtt_topology import TOPOLOGY
    by_id = {e.object_id: e for e in TOPOLOGY}
    assert by_id["current_filename"].min_publish_interval_s == 2.0
    assert by_id["current_progress"].min_publish_interval_s == 2.0
    assert by_id["queue_pending"].min_publish_interval_s == 1.0
    assert by_id["queue_failed"].min_publish_interval_s == 1.0
    assert by_id["queue_downloading"].min_publish_interval_s == 1.0
    assert by_id["last_downloaded_clip"].min_publish_interval_s == 5.0
    assert by_id["total_clips"].min_publish_interval_s == 5.0


def test_button_entries_present():
    from web.services.mqtt_topology import TOPOLOGY
    button_ids = {e.object_id for e in TOPOLOGY
                  if e.component == "button"}
    assert button_ids == {
        "start_sync", "pause_sync", "skip_current",
        "refresh_queue", "retry_failed", "rescan_archive",
    }


def test_buttons_have_no_state_fn():
    from web.services.mqtt_topology import TOPOLOGY
    for e in TOPOLOGY:
        if e.component == "button":
            assert e.state_fn is None, e.object_id


def test_button_default_enabled():
    from web.services.mqtt_topology import TOPOLOGY
    for e in TOPOLOGY:
        if e.component == "button":
            assert e.enabled_by_default is True, e.object_id


def test_command_handler_present_only_on_buttons():
    from web.services.mqtt_topology import TOPOLOGY
    for e in TOPOLOGY:
        if e.command_handler is not None:
            assert e.component == "button", e.object_id


def test_sync_status_entity_lists_new_affected_events():
    from web.services.mqtt_topology import TOPOLOGY
    entity = next(e for e in TOPOLOGY if e.object_id == "sync_status")
    events = set(entity.affected_by_hub_events)
    assert "dashcam_online" in events
    assert "dashcam_offline" in events
    assert "disk_pct" in events
    assert "sync_error" in events
    # Plus the original ones
    assert "sync_state" in events
    assert "item_started" in events
    assert "item_finished" in events


def test_queue_pending_entity_lists_dashcam_connection_events():
    """queue_pending now excludes clips held by an inactive dashcam
    connection (active_scope), so it must re-derive when the camera
    goes online/offline or fails over, not just on queue/item events."""
    from web.services.mqtt_topology import TOPOLOGY
    entity = next(e for e in TOPOLOGY if e.object_id == "queue_pending")
    events = set(entity.affected_by_hub_events)
    assert "dashcam_online" in events
    assert "dashcam_offline" in events
    # Plus the original ones
    assert "queue_changed" in events
    assert "item_started" in events
    assert "item_finished" in events


def test_sync_status_entity_has_attrs_fn():
    from web.services.mqtt_state import attrs_sync_status
    from web.services.mqtt_topology import TOPOLOGY
    entity = next(e for e in TOPOLOGY if e.object_id == "sync_status")
    assert entity.attrs_fn is attrs_sync_status


def test_current_progress_uses_qos_0():
    """current_progress fires on every item_progress event during a
    download — it's the highest-rate publisher. QoS=1 PUBACK waits
    stall the publisher under broker latency and trip the connection.
    Retained QoS=0 still lets HA pick up the latest value on subscribe;
    losing a single progress update mid-flight is acceptable.
    """
    from web.services.mqtt_topology import TOPOLOGY
    entity = next(e for e in TOPOLOGY if e.object_id == "current_progress")
    assert entity.qos == 0


def test_state_entities_default_to_global_qos():
    """Entities without an explicit qos override fall back to cfg['qos'].
    Verify sync_status / dashcam (the reliability-sensitive state
    entities) leave qos unset so the global setting wins."""
    from web.services.mqtt_topology import TOPOLOGY
    by_id = {e.object_id: e for e in TOPOLOGY}
    assert by_id["sync_status"].qos is None
    assert by_id["dashcam"].qos is None


def test_download_speed_entity_present():
    from web.services.mqtt_topology import TOPOLOGY
    assert "download_speed" in {e.object_id for e in TOPOLOGY}


def test_download_speed_entity_config():
    from web.services.mqtt_state import state_download_speed
    from web.services.mqtt_topology import TOPOLOGY
    e = next(x for x in TOPOLOGY if x.object_id == "download_speed")
    assert e.component == "sensor"
    assert e.device_class == "data_rate"
    assert e.unit_of_measurement == "MB/s"
    assert e.state_class == "measurement"
    assert e.enabled_by_default is True
    assert e.min_publish_interval_s == 60.0
    assert e.qos == 0
    assert e.affected_by_hub_events == (
        "item_progress", "item_started", "item_finished",
        "sync_done", "sync_state", "dashcam_offline",
    )
    assert e.state_fn is state_download_speed


def test_download_speed_discovery_payload():
    from web.services.mqtt_topology import TOPOLOGY, build_discovery_payload
    e = next(x for x in TOPOLOGY if x.object_id == "download_speed")
    cfg = {"discovery_prefix": "homeassistant", "node_id": "viofosync",
           "version": "1", "configuration_url": ""}
    p = build_discovery_payload(e, cfg)
    assert p["device_class"] == "data_rate"
    assert p["unit_of_measurement"] == "MB/s"
    assert p["state_class"] == "measurement"
    assert p["enabled_by_default"] is True
    assert p["state_topic"] == "viofosync/download_speed/state"
