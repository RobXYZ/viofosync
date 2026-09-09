"""build_bundle: per-collector isolation and the no-secrets guarantee."""
from __future__ import annotations

import time as _time
from types import SimpleNamespace

from web.db import Database
from web.services import debug_bundle as dbb


def _snap(tmp_path):
    return SimpleNamespace(
        # Fields build_bundle/collect_runtime/collect_camera read directly.
        recordings=str(tmp_path), instance_name="garage",
        address="192.0.2.10", address_fallback="",
        mqtt_host="", mqtt_password="hunter2",
        use_html_listing=True,
        # Everything else _editable_values(snap) reads, so the real
        # collect_settings can run end-to-end in the secrecy test.
        import_path="", grouping="day",
        gps_extract=True, gps_triage=True,
        derive_thumbs_eager=True, derive_filmstrips_eager=True,
        delete_after_download=False,
        primary_scope="everything", primary_gps_triage=True,
        alternative_scope="ro_only", alternative_gps_triage=True,
        locations=[],
        retention_max_days=30, retention_disk_pct=90,
        retention_protect_ro=True, recordings_quota_gb=0,
        disk_critical_pct=95,
        timeout=10.0, download_attempts=3, max_attempts=5,
        sync_interval_seconds=300, enable_scheduled_sync=True,
        host="0.0.0.0", port=8080,
        export_encoder_pref="auto", pip_position="bottom-right",
        nominatim_email="me@example.com", geocode_enabled=True,
        distance_units="km",
        mqtt_enabled=False, mqtt_port=1883, mqtt_username="",
        mqtt_tls=False, mqtt_client_id="viofosync",
        mqtt_discovery_prefix="homeassistant", mqtt_node_id="viofosync",
        mqtt_discovery_enabled=True, mqtt_qos=0,
    )


def test_bundle_renders_all_sections_and_header(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    monkeypatch.setattr(dbb, "collect_camera",
                        lambda *a, **k: {"reachable": False})
    monkeypatch.setattr(dbb, "collect_settings",
                        lambda snap: {"ADDRESS": "192.x.x.10"})
    md = dbb.build_bundle(db, _snap(tmp_path), encoders=None)
    assert md.startswith("# viofosync debug bundle")
    for heading in ("## Runtime", "## Settings", "## Queue", "## Logs",
                    "## Camera"):
        assert heading in md
    assert "masked for public sharing" in md
    assert "garage" in md


def test_failed_collector_is_isolated(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    def _boom(*a, **k):
        raise RuntimeError("collector exploded")
    monkeypatch.setattr(dbb, "collect_queue", _boom)
    monkeypatch.setattr(dbb, "collect_camera",
                        lambda *a, **k: {"reachable": False})
    monkeypatch.setattr(dbb, "collect_settings",
                        lambda snap: {})
    md = dbb.build_bundle(db, _snap(tmp_path), encoders=None)
    assert "section failed: RuntimeError: collector exploded" in md
    assert "## Logs" in md  # later sections still render


def test_no_secret_or_raw_address_in_output(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    with db.write() as c:
        c.execute(
            "INSERT INTO app_log (ts, levelno, level, logger, message) "
            "VALUES (1, 20, 'INFO', 't', 'dialing 192.0.2.10 now')")
    monkeypatch.setattr(dbb, "collect_camera",
                        lambda *a, **k: {"reachable": False})
    md = dbb.build_bundle(db, _snap(tmp_path), encoders=None)
    assert "hunter2" not in md
    assert "192.0.2.10" not in md


def test_bundle_renders_reachable_camera_and_suspects(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, "
            "attempts, remote_size, enqueued_at) VALUES (?,?,?,?,?,?)",
            ("A.MP4", "/DCIM/Movie/A.MP4", "pending", 2, 60 << 20,
             int(_time.time())),
        )
    monkeypatch.setattr(dbb, "collect_settings", lambda snap: {})
    monkeypatch.setattr(dbb, "collect_camera", lambda *a, **k: {
        "reachable": True,
        "firmware": "1.0",
        "record_flag": None,
        "record_flag_error": "TimeoutError: timed out",
        "listing": {"entries": 1, "complete": True},
        "files": [
            {"filename": "A.MP4", "db_remote_size": 60 << 20,
             "listing_size": 60 << 20, "head_size": 61 << 20,
             "head_error": None, "get_size": 61 << 20, "get_error": None,
             "mismatch": True},
            {"filename": "B.MP4", "skipped": "no camera path"},
        ],
        "files_budget_exhausted": True,
    })
    md = dbb.build_bundle(db, _snap(tmp_path), encoders=None)
    # Suspect line: filename + attempts.
    assert "`A.MP4`" in md
    assert "attempts=2" in md
    # Camera section: measured-file mismatch flag, skipped file, budget note.
    assert "MISMATCH" in md
    assert "no camera path" in md
    assert "probe budget exhausted" in md
    # record_flag probe failure surfaces its reason, same as head/get.
    assert "None (TimeoutError: timed out)" in md


def test_failed_logs_collector_leaves_no_open_fence(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    def _boom(*a, **k):
        raise RuntimeError("logs exploded")
    monkeypatch.setattr(dbb, "collect_logs", _boom)
    monkeypatch.setattr(dbb, "collect_camera",
                        lambda *a, **k: {"reachable": False})
    monkeypatch.setattr(dbb, "collect_settings", lambda snap: {})
    md = dbb.build_bundle(db, _snap(tmp_path), encoders=None)
    # No dangling fence left open by the failed collector.
    assert md.count("````") == 0
    logs_failed_at = md.index("section failed: RuntimeError: logs exploded")
    camera_at = md.index("## Camera")
    assert camera_at > logs_failed_at
    assert "camera unreachable — live probe skipped" in md


def test_empty_address_never_dials(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "v.db"))
    def _spy(*a, **k):
        raise AssertionError("dialed!")
    monkeypatch.setattr(dbb.socket, "create_connection", _spy)
    monkeypatch.setattr(dbb, "collect_settings", lambda snap: {})
    snap = _snap(tmp_path)
    snap.address = ""
    md = dbb.build_bundle(db, snap, encoders=None)
    assert "no camera address configured" in md
