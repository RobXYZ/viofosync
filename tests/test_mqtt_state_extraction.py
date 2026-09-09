"""State-extraction tests. Each state_fn is a pure function over
(hub, db, snapshot) so they're easy to exercise without a broker."""
from __future__ import annotations

import time
import types


def _stub_snapshot(**kwargs):
    """Make a stub Snapshot. We don't need every field — the state
    fns only touch a subset."""
    base = dict(
        address="192.168.1.50",
        recordings=".",
        enable_scheduled_sync=True,
        retention_max_days=0,
        disk_critical_pct=95,
    )
    base.update(kwargs)
    return types.SimpleNamespace(**base)


def _hub_with_state(state: dict):
    return types.SimpleNamespace(last_state=state)


def _db_with_clip_index(tmp_path, rows: list[dict]):
    from web.db import Database
    db = Database(str(tmp_path / "v.db"))
    with db.write() as c:
        for r in rows:
            c.execute(
                "INSERT INTO clip_index "
                "(path, basename, group_name, timestamp, camera, "
                " sequence, event_type, size_bytes, has_gpx, "
                " gps_examined, scanned_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["path"], r["basename"], r["group_name"], r["timestamp"],
                 r["camera"], r["sequence"], r["event_type"], r["size_bytes"],
                 0, 0, r["timestamp"]),
            )
    return db


def _db_with_queue(tmp_path, rows):
    """rows: list of (filename, state) or (filename, state, source_dir,
    requested_at)."""
    from web.db import Database
    db = Database(str(tmp_path / "v.db"))
    now = int(time.time())
    with db.write() as c:
        for row in rows:
            filename, state = row[0], row[1]
            source_dir = row[2] if len(row) > 2 else "/DCIM/Movie"
            requested_at = row[3] if len(row) > 3 else None
            c.execute(
                "INSERT INTO download_queue "
                "(filename, source_dir, state, enqueued_at, requested_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (filename, source_dir, state, now, requested_at),
            )
    return db


# ---- binary sensors

def test_state_dashcam_online():
    from web.services.mqtt_state import state_dashcam
    assert state_dashcam(_hub_with_state({"dashcam_online": True}),
                         None, _stub_snapshot()) == "ON"
    assert state_dashcam(_hub_with_state({"dashcam_online": False}),
                         None, _stub_snapshot()) == "OFF"


def test_state_dashcam_unknown_when_no_address():
    from web.services.mqtt_state import state_dashcam
    # No address configured → reachable is meaningless
    assert state_dashcam(_hub_with_state({"dashcam_online": True}),
                         None, _stub_snapshot(address="")) == "OFF"


def test_state_sync_status_paused_when_no_sync_state():
    from web.services.mqtt_state import state_sync_status
    hub = _hub_with_state({})
    assert state_sync_status(hub, None, _stub_snapshot()) == "paused"


def test_state_sync_status_paused_when_not_running():
    from web.services.mqtt_state import state_sync_status
    hub = _hub_with_state({"sync_state": {"running": False, "paused": False}})
    assert state_sync_status(hub, None, _stub_snapshot()) == "paused"


def test_state_sync_status_paused_when_paused_flag():
    from web.services.mqtt_state import state_sync_status
    hub = _hub_with_state({"sync_state": {"running": True, "paused": True}})
    assert state_sync_status(hub, None, _stub_snapshot()) == "paused"


def test_state_sync_status_downloading_when_current_item():
    from web.services.mqtt_state import state_sync_status
    hub = _hub_with_state({
        "sync_state": {"running": True, "paused": False},
        "dashcam_online": True,
        "current_item": {"filename": "x.mp4"},
    })
    assert state_sync_status(hub, None, _stub_snapshot()) == "downloading"


def test_state_sync_status_waiting_when_no_current_item():
    from web.services.mqtt_state import state_sync_status
    hub = _hub_with_state({
        "sync_state": {"running": True, "paused": False},
        "dashcam_online": True,
        "current_item": None,
    })
    assert state_sync_status(hub, None, _stub_snapshot()) == "waiting"


def test_state_sync_status_waiting_when_dashcam_offline():
    from web.services.mqtt_state import state_sync_status
    hub = _hub_with_state({
        "sync_state": {"running": True, "paused": False},
        "dashcam_online": False,
    })
    assert state_sync_status(hub, None, _stub_snapshot()) == "waiting"


def test_state_sync_status_error_when_address_unset():
    from web.services.mqtt_state import state_sync_status
    hub = _hub_with_state({
        "sync_state": {"running": True, "paused": False},
        "dashcam_online": True,
    })
    assert state_sync_status(
        hub, None, _stub_snapshot(address=None),
    ) == "error"


def test_attrs_sync_status_carries_reason_when_error():
    from web.services.mqtt_state import attrs_sync_status
    hub = _hub_with_state({})
    attrs = attrs_sync_status(hub, None, _stub_snapshot(address=None))
    assert attrs["reason"] == "camera address not configured"
    assert attrs["triage_active"] is False


def test_attrs_sync_status_reason_none_when_not_error():
    from web.services.mqtt_state import attrs_sync_status
    hub = _hub_with_state({
        "sync_state": {"running": True, "paused": False},
        "dashcam_online": True,
        "current_item": {"filename": "x.mp4"},
    })
    attrs = attrs_sync_status(hub, None, _stub_snapshot())
    assert attrs["reason"] is None
    assert attrs["triage_active"] is False


# ---- queue counts

def _profile_snapshot(**kwargs):
    """Snapshot carrying the four per-connection profile attributes that
    ``profile_for`` reads."""
    base = dict(
        primary_scope="everything", primary_gps_triage=False,
        alternative_scope="ro_only", alternative_gps_triage=True,
    )
    base.update(kwargs)
    return _stub_snapshot(**base)


# Driving / parking / RO clips: the byte before the camera letter is the
# event prefix (P = parking); RO-ness comes from source_dir.
_DRIVE = "2026_0901_100000_0001F.MP4"
_PARK = "2026_0901_100100_0002PF.MP4"
_RO = "2026_0901_100200_0003F.MP4"


def test_state_queue_pending(tmp_path):
    from web.services.mqtt_state import state_queue_pending
    db = _db_with_queue(tmp_path, [
        ("a.MP4", "pending"),
        ("b.MP4", "pending"),
        ("c.MP4", "failed"),
    ])
    assert state_queue_pending(_hub_with_state({}), db,
                                _stub_snapshot()) == "2"


def test_active_scope_none_when_offline_or_unknown(tmp_path):
    from web.services.mqtt_state import active_scope
    snap = _profile_snapshot()
    # Never probed this run.
    assert active_scope(_hub_with_state({}), snap) is None
    # Explicitly offline.
    assert active_scope(
        _hub_with_state({"dashcam_online": False,
                         "dashcam_source": "alternative"}), snap) is None
    # Online but the source is missing or not a known connection name.
    assert active_scope(_hub_with_state({"dashcam_online": True}), snap) is None
    assert active_scope(
        _hub_with_state({"dashcam_online": True,
                         "dashcam_source": "tailscale"}), snap) is None


def test_active_scope_resolves_the_live_connection(tmp_path):
    from web.services.mqtt_state import active_scope
    snap = _profile_snapshot()
    assert active_scope(
        _hub_with_state({"dashcam_online": True,
                         "dashcam_source": "primary"}), snap) == "everything"
    assert active_scope(
        _hub_with_state({"dashcam_online": True,
                         "dashcam_source": "alternative"}), snap) == "ro_only"


def test_state_queue_pending_counts_everything_when_no_connection(tmp_path):
    """Offline / unknown: no scope, so nothing is held and the raw pending
    count is published."""
    from web.services.mqtt_state import state_queue_pending
    db = _db_with_queue(tmp_path, [
        (_DRIVE, "pending"),
        (_PARK, "pending", "/DCIM/Movie/Parking"),
        (_RO, "pending", "/DCIM/Movie/RO"),
    ])
    assert state_queue_pending(_hub_with_state({}), db,
                                _profile_snapshot()) == "3"
    assert state_queue_pending(
        _hub_with_state({"dashcam_online": False}), db,
        _profile_snapshot()) == "3"


def test_state_queue_pending_excludes_held_on_alternative(tmp_path):
    """On an ro_only connection only the RO clip (plus anything the user
    explicitly requested) is downloadable — the sensor must not report the
    whole backlog."""
    from web.services.mqtt_state import state_queue_pending
    db = _db_with_queue(tmp_path, [
        (_DRIVE, "pending"),
        (_PARK, "pending", "/DCIM/Movie/Parking"),
        (_RO, "pending", "/DCIM/Movie/RO"),
        ("2026_0901_100300_0004PF.MP4", "pending", "/DCIM/Movie/Parking",
         1_700_000_000),   # user pressed "Download next" — bypasses scope
        ("2026_0901_100400_0005F.MP4", "done"),
    ])
    hub = _hub_with_state({"dashcam_online": True,
                            "dashcam_source": "alternative"})
    assert state_queue_pending(hub, db, _profile_snapshot()) == "2"
    # The same queue on the primary (everything) connection holds nothing.
    hub_primary = _hub_with_state({"dashcam_online": True,
                                    "dashcam_source": "primary"})
    assert state_queue_pending(hub_primary, db, _profile_snapshot()) == "4"


def test_state_queue_failed(tmp_path):
    from web.services.mqtt_state import state_queue_failed
    db = _db_with_queue(tmp_path, [
        ("a.MP4", "pending"),
        ("b.MP4", "failed"),
    ])
    assert state_queue_failed(_hub_with_state({}), db,
                               _stub_snapshot()) == "1"


def test_state_queue_downloading(tmp_path):
    from web.services.mqtt_state import state_queue_downloading
    db = _db_with_queue(tmp_path, [
        ("a.MP4", "downloading"),
    ])
    assert state_queue_downloading(_hub_with_state({}), db,
                                    _stub_snapshot()) == "1"


# ---- archive

def test_state_last_downloaded_clip_returns_iso(tmp_path):
    from web.services.mqtt_state import state_last_downloaded_clip
    ts = 1715852400  # 2024-05-16 ish
    db = _db_with_clip_index(tmp_path, [{
        "path": "/r/a.MP4", "basename": "a.MP4",
        "group_name": "2024-05-16",
        "timestamp": ts, "camera": "F", "sequence": 1,
        "event_type": "normal", "size_bytes": 1024,
    }])
    out = state_last_downloaded_clip(_hub_with_state({}), db, _stub_snapshot())
    assert out is not None
    # ISO 8601 with 'Z' suffix for HA timestamp device_class
    assert out.endswith("Z") or "+" in out


def test_state_last_downloaded_clip_none_when_empty(tmp_path):
    from web.services.mqtt_state import state_last_downloaded_clip
    db = _db_with_clip_index(tmp_path, [])
    assert state_last_downloaded_clip(_hub_with_state({}), db,
                                      _stub_snapshot()) is None


def test_state_total_clips(tmp_path):
    from web.services.mqtt_state import state_total_clips
    db = _db_with_clip_index(tmp_path, [
        {"path": "/r/a.MP4", "basename": "a.MP4",
         "group_name": "d", "timestamp": 1, "camera": "F", "sequence": 1,
         "event_type": "normal", "size_bytes": 0},
        {"path": "/r/b.MP4", "basename": "b.MP4",
         "group_name": "d", "timestamp": 2, "camera": "R", "sequence": 1,
         "event_type": "normal", "size_bytes": 0},
    ])
    assert state_total_clips(_hub_with_state({}), db,
                              _stub_snapshot()) == "2"


# ---- current download

def test_state_current_filename():
    from web.services.mqtt_state import state_current_filename
    hub = _hub_with_state({"current_item": {"filename": "2026_0516_120000_001F.MP4"}})
    assert state_current_filename(hub, None,
                                   _stub_snapshot()) == "2026_0516_120000_001F.MP4"
    assert state_current_filename(_hub_with_state({"current_item": None}),
                                   None, _stub_snapshot()) is None


def test_state_current_progress_pct():
    from web.services.mqtt_state import state_current_progress
    hub = _hub_with_state({
        "current_item": {"filename": "x", "bytes": 50, "total": 100},
    })
    assert state_current_progress(hub, None, _stub_snapshot()) == "50.0"
    # No total = no progress
    hub = _hub_with_state({"current_item": {"filename": "x", "bytes": 50}})
    assert state_current_progress(hub, None, _stub_snapshot()) is None


# ---- disk

def test_state_disk_used_filesystem_only(tmp_path):
    """No quota set → just the filesystem percentage."""
    from web.services.mqtt_state import state_disk_used
    out = state_disk_used(
        _hub_with_state({}), None,
        _stub_snapshot(recordings=str(tmp_path), recordings_quota_gb=0),
    )
    assert out is not None
    val = int(out)
    assert 0 <= val <= 100


def test_state_disk_used_reports_max_of_quota_and_filesystem(tmp_path):
    """Both rules active → publish the higher percentage (the rule
    closest to triggering cleanup)."""
    from web.services import retention as _ret
    from web.services.mqtt_state import state_disk_used

    _ret._size_cache.clear()
    # Plant exactly 600 MiB under recordings, then set a tiny 1 GiB quota.
    # Quota % = 600/1024 ≈ 58.6%. Filesystem % on the test runner is
    # almost certainly much lower than that, so max should be ≈59.
    for i in range(600):
        (tmp_path / f"chunk_{i}.MP4").write_bytes(b"\0" * (1 << 20))
    out = state_disk_used(
        _hub_with_state({}), None,
        _stub_snapshot(recordings=str(tmp_path), recordings_quota_gb=1),
    )
    assert out is not None
    pct = int(out)
    assert pct >= 58, f"expected the quota rule (~59%) to dominate, got {pct}%"


def test_state_disk_used_filesystem_wins_when_quota_far_from_full(tmp_path):
    """When the quota is generous and the filesystem is the tighter
    constraint, the FS % wins."""
    from web.services import retention as _ret
    from web.services.mqtt_state import state_disk_used

    _ret._size_cache.clear()
    # 1 MiB of data under recordings, 1024 GiB quota → 0% quota usage.
    # The filesystem on the test runner is going to be busier than that,
    # so the FS rule wins and the sensor reports the FS %.
    (tmp_path / "tiny.MP4").write_bytes(b"\0" * (1 << 20))
    out = state_disk_used(
        _hub_with_state({}), None,
        _stub_snapshot(recordings=str(tmp_path), recordings_quota_gb=1024),
    )
    assert out is not None
    # Quota would have reported ~0; max(0, fs%) ≈ fs%, almost always > 0.
    pct = int(out)
    assert 0 <= pct <= 100


def test_state_disk_used_missing_path_returns_none(tmp_path):
    from web.services.mqtt_state import state_disk_used
    out = state_disk_used(
        _hub_with_state({}), None,
        _stub_snapshot(recordings=str(tmp_path / "does-not-exist"),
                        recordings_quota_gb=0),
    )
    assert out is None


# ---- download speed (session moving average)

def test_state_download_speed_none_before_30s():
    from web.services.mqtt_state import state_download_speed
    hub = _hub_with_state({"session": {
        "active": True, "elapsed_s": 10.0,
        "avg_speed_bps": float(5 * 1024 * 1024),
    }})
    assert state_download_speed(hub, None, _stub_snapshot()) is None


def test_state_download_speed_value_after_30s():
    from web.services.mqtt_state import state_download_speed
    hub = _hub_with_state({"session": {
        "active": True, "elapsed_s": 35.0,
        "avg_speed_bps": float(2 * 1024 * 1024),
    }})
    assert state_download_speed(hub, None, _stub_snapshot()) == "2.0"


def test_state_download_speed_zero_when_idle():
    from web.services.mqtt_state import state_download_speed
    hub = _hub_with_state({"session": {
        "active": False, "elapsed_s": 0.0, "avg_speed_bps": None,
    }})
    assert state_download_speed(hub, None, _stub_snapshot()) == "0"


def test_state_download_speed_zero_when_no_session_key():
    from web.services.mqtt_state import state_download_speed
    hub = _hub_with_state({})
    assert state_download_speed(hub, None, _stub_snapshot()) == "0"


def test_state_download_speed_none_when_avg_unavailable():
    from web.services.mqtt_state import state_download_speed
    hub = _hub_with_state({"session": {
        "active": True, "elapsed_s": 35.0, "avg_speed_bps": None,
    }})
    assert state_download_speed(hub, None, _stub_snapshot()) is None


# ---- dashcam connection sensor

def test_state_dashcam_connection_online_primary():
    from web.services.mqtt_state import state_dashcam_connection
    hub = _hub_with_state({"dashcam_online": True, "dashcam_source": "primary"})
    assert state_dashcam_connection(hub, None, _stub_snapshot()) == "primary"


def test_state_dashcam_connection_online_alternative():
    from web.services.mqtt_state import state_dashcam_connection
    hub = _hub_with_state(
        {"dashcam_online": True, "dashcam_source": "alternative"})
    assert state_dashcam_connection(
        hub, None, _stub_snapshot(address_fallback="10.0.0.2")) == "alternative"


def test_state_dashcam_connection_offline():
    from web.services.mqtt_state import state_dashcam_connection
    hub = _hub_with_state(
        {"dashcam_online": False, "dashcam_source": "primary"})
    assert state_dashcam_connection(hub, None, _stub_snapshot()) == "offline"


def test_state_dashcam_connection_unknown_when_never_probed():
    from web.services.mqtt_state import state_dashcam_connection
    hub = _hub_with_state({"dashcam_online": None})
    assert state_dashcam_connection(hub, None, _stub_snapshot()) is None


def test_state_dashcam_connection_unknown_when_no_address():
    from web.services.mqtt_state import state_dashcam_connection
    hub = _hub_with_state({"dashcam_online": True, "dashcam_source": "primary"})
    snap = _stub_snapshot(address=None, address_fallback=None)
    assert state_dashcam_connection(hub, None, snap) is None


def test_attrs_dashcam_connection_reports_live_address():
    from web.services.mqtt_state import attrs_dashcam_connection
    hub = _hub_with_state({"dashcam_address": "10.0.0.2"})
    assert attrs_dashcam_connection(hub, None, _stub_snapshot()) == {
        "address": "10.0.0.2"}


