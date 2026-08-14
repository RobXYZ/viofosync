"""Section collectors for the debug bundle."""
from __future__ import annotations

import time as _time
from types import SimpleNamespace

from web import fsinfo
from web.db import Database
from web.services import debug_bundle as dbb


def _snap(tmp_path, **kw):
    d = dict(
        recordings=str(tmp_path), instance_name="garage",
        address="192.0.2.10", address_fallback="",
        mqtt_host="", mqtt_password="",
    )
    d.update(kw)
    return SimpleNamespace(**d)


def test_runtime_has_versions_disk_and_fstype(tmp_path):
    out = dbb.collect_runtime(_snap(tmp_path), encoders={"selected": "software"})
    assert out["app_version"]
    assert out["python"].count(".") >= 1
    assert out["disk_free_bytes"] > 0
    assert out["uptime_s"] >= 0
    # macOS dev boxes have no /proc/mounts — must degrade, not raise.
    assert isinstance(out["recordings_fstype"], str)
    assert out["encoders"] == {"selected": "software"}


def test_runtime_fstype_parses_proc_mounts(tmp_path, monkeypatch):
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "nas:/vol/dashcam /recordings nfs4 rw,relatime 0 0\n"
        "/dev/sda1 / ext4 rw 0 0\n"
    )
    monkeypatch.setattr(fsinfo, "_PROC_MOUNTS", str(mounts))
    snap = _snap(tmp_path, recordings="/recordings/clips")
    out = dbb.collect_runtime(snap, encoders=None)
    assert out["recordings_fstype"] == "nfs4"


def test_runtime_fstype_unknown_when_proc_mounts_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(fsinfo, "_PROC_MOUNTS", str(tmp_path / "does-not-exist"))
    out = dbb.collect_runtime(_snap(tmp_path), encoders=None)
    assert out["recordings_fstype"] == "unknown"


def _make_real_snap(tmp_path, **extra):
    """Build a REAL Snapshot through the settings machinery (not a
    SimpleNamespace), so collect_settings exercises the actual
    _editable_values projection and its secret-masking.

    Pointing SettingsProvider at a config.json that already exists
    (written below) makes ConfigStore.migrate_from_env() see
    ``self.path.exists()`` True and return immediately, before it
    ever looks at CONFIG_DIR/env_file_path — so this needs no
    CONFIG_DIR env var or tmp_config_dir fixture, and never touches
    the real /config on the host.

    ``**extra`` merges extra keys (e.g. LOCATIONS, NOMINATIM_EMAIL)
    into the written config.json for tests that need them.
    """
    import json

    import web.settings as settings_mod

    cfg = tmp_path / "config.json"
    payload = {
        "ADDRESS": "192.0.2.10",
        "MQTT_PASSWORD": "hunter2",
    }
    payload.update(extra)
    cfg.write_text(json.dumps(payload))
    provider = settings_mod.SettingsProvider(str(cfg))
    return provider.get()


def test_settings_section_masks_addresses_and_secrets(tmp_path):
    snap = _make_real_snap(tmp_path)
    out = dbb.collect_settings(snap)
    assert out["ADDRESS"] == "192.x.x.10"
    # Secret keys never carry a real value (existing projection masks).
    assert "hunter2" not in str(out)


def test_settings_section_strips_location_data(tmp_path):
    snap = _make_real_snap(
        tmp_path,
        LOCATIONS=[
            {"name": "Home", "lat": 51.5074, "lon": -0.1278,
             "radius_m": 30, "is_home": True},
        ],
        NOMINATIM_EMAIL="user@example.com",
    )
    out = dbb.collect_settings(snap)
    assert "51.5074" not in str(out)
    assert "-0.1278" not in str(out)
    assert "user@example.com" not in str(out)
    assert "redacted" in out["LOCATIONS"]


def _seed_queue(db, filename, *, state="pending", attempts=0,
                 remote_complete=None, enqueued_at=None, remote_size=None,
                 last_attempt_at=None, last_error=None):
    with db.write() as c:
        c.execute(
            "INSERT INTO download_queue (filename, source_dir, state, "
            "attempts, remote_complete, enqueued_at, remote_size, "
            "last_attempt_at, last_error) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (filename, "/DCIM/Movie", state, attempts, remote_complete,
             enqueued_at or int(_time.time()), remote_size,
             last_attempt_at, last_error),
        )


def test_queue_section_counts_and_suspects(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    _seed_queue(db, "2026_0628_133416_104430R.MP4",
                attempts=2, remote_complete=1, remote_size=60 << 20)
    _seed_queue(db, "2026_0813_200128_023063R.MP4")
    _seed_queue(db, "2026_0601_000000_000001F.MP4", state="done")
    out = dbb.collect_queue(db)
    assert out["counts"]["pending"] == 2
    assert out["counts"]["done"] == 1
    suspects = [s["filename"] for s in out["suspects"]]
    # attempts>0 and remote_complete set -> suspect; clean row -> not.
    assert "2026_0628_133416_104430R.MP4" in suspects
    assert "2026_0601_000000_000001F.MP4" not in suspects
    # the clean pending row was just enqueued (enqueued_at=now), so it
    # must not match the age-based suspect criterion either.
    assert "2026_0813_200128_023063R.MP4" not in suspects
    row = next(s for s in out["suspects"]
               if s["filename"] == "2026_0628_133416_104430R.MP4")
    assert row["remote_size"] == 60 << 20
    assert row["attempts"] == 2
    assert row["remote_complete"] == 1
    assert out["tables"]["download_queue"] == 3


def test_queue_section_age_based_suspect(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    stale_at = int(_time.time()) - 8 * 86400
    _seed_queue(db, "2026_0501_000000_000002F.MP4",
                attempts=0, remote_complete=None, enqueued_at=stale_at)
    out = dbb.collect_queue(db)
    suspects = [s["filename"] for s in out["suspects"]]
    assert "2026_0501_000000_000002F.MP4" in suspects


def test_queue_section_released_row_is_not_suspicious(tmp_path):
    """remote_complete is normal operation, not a fault signal: the
    newest-capture lens carries it before it has ever been picked up."""
    db = Database(str(tmp_path / "v.db"))
    _seed_queue(db, "2026_0701_090000_000010R.MP4",
                attempts=0, remote_complete=1, last_attempt_at=None)
    out = dbb.collect_queue(db)
    suspects = [s["filename"] for s in out["suspects"]]
    assert "2026_0701_090000_000010R.MP4" not in suspects


def test_queue_section_error_row_is_suspect(tmp_path):
    """A row carrying last_error is surfaced even with attempts back at
    zero — the error text usually names the real fault."""
    db = Database(str(tmp_path / "v.db"))
    _seed_queue(db, "2026_0702_090000_000011R.MP4",
                attempts=0, last_error="Not writable: /recordings/2026-07-02")
    out = dbb.collect_queue(db)
    row = next(s for s in out["suspects"]
               if s["filename"] == "2026_0702_090000_000011R.MP4")
    assert "Not writable" in row["last_error"]


def test_logs_section_is_oldest_first_and_redacted(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    with db.write() as c:
        for i, msg in enumerate([
            "first line",
            "probing 192.0.2.10 for listing",
            "last line",
        ]):
            c.execute(
                "INSERT INTO app_log (ts, levelno, level, logger, message) "
                "VALUES (?,?,?,?,?)",
                (1000 + i, 20, "INFO", "test", msg),
            )
    lines = dbb.collect_logs(db, redact_values=["192.0.2.10"])
    assert lines[0].endswith("first line")
    assert lines[-1].endswith("last line")
    assert not any("192.0.2.10" in ln for ln in lines)
    assert any("192.x.x.10" in ln for ln in lines)


def test_logs_section_handles_overflowing_timestamp(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    with db.write() as c:
        c.execute(
            "INSERT INTO app_log (ts, levelno, level, logger, message) "
            "VALUES (?,?,?,?,?)",
            (1e30, 20, "INFO", "test", "overflow ts"),
        )
    lines = dbb.collect_logs(db, redact_values=[])
    assert any("1e+30" in ln for ln in lines)


def test_logs_section_includes_redacted_traceback(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    exc_text = (
        "Traceback (most recent call last):\n"
        '  File "worker.py", line 12, in run\n'
        "    connect(host)\n"
        "ConnectionError: could not reach 192.0.2.10"
    )
    with db.write() as c:
        c.execute(
            "INSERT INTO app_log "
            "(ts, levelno, level, logger, message, exc_text) "
            "VALUES (?,?,?,?,?,?)",
            (1000, 40, "ERROR", "test", "download failed", exc_text),
        )
    lines = dbb.collect_logs(db, redact_values=["192.0.2.10"])
    assert any("ConnectionError: could not reach" in ln for ln in lines)
    assert not any("192.0.2.10" in ln for ln in lines)
    assert any("192.x.x.10" in ln for ln in lines)
