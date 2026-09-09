"""Pure state-extraction functions for MQTT entity values.

Every function has the signature ``(hub, db, snapshot) -> Optional[str]``
where the string is the exact MQTT payload to publish, or ``None`` to
skip publishing (the entity will appear as Unknown to HA, distinct from
Unavailable which is the LWT-driven state).

Functions read from ``hub.last_state`` (a dict updated by Hub.broadcast),
the SQLite ``Database``, and the settings ``Snapshot``. No I/O beyond
SQLite, plus whatever the retention service does to compute the
quota-aware used-% for the disk gauge.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

from .profiles import profile_for
# Package-private, but shared deliberately: the HA sensor and the web UI
# must use one definition of "held".
from .queue import _held_sql
from .sync_status import compute_sync_status


def _iso_z(ts: int) -> str:
    """ISO 8601 with explicit UTC marker — what HA's timestamp
    device_class expects."""
    return (
        _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


# ---- binary sensors

def state_dashcam(hub, db, snapshot) -> Optional[str]:
    if not snapshot.address:
        return "OFF"
    val = hub.last_state.get("dashcam_online")
    if val is None:
        return None
    return "ON" if val else "OFF"


def state_dashcam_connection(hub, db, snapshot) -> Optional[str]:
    """Which address the dashcam is reached through: ``primary`` /
    ``alternative`` / ``offline``. ``None`` (Unknown) when no address is
    configured or the camera has never been probed this run."""
    if not (snapshot.address or getattr(snapshot, "address_fallback", None)):
        return None
    online = hub.last_state.get("dashcam_online")
    if online is None:
        return None
    if not online:
        return "offline"
    return hub.last_state.get("dashcam_source") or "primary"


def attrs_dashcam_connection(hub, db, snapshot) -> Optional[dict]:
    """JSON attributes for the connection sensor — the live address."""
    return {"address": hub.last_state.get("dashcam_address")}


def state_sync_status(hub, db, snapshot) -> Optional[str]:
    """The four-state unified status string. See sync_status.py."""
    state, _reason = compute_sync_status(hub, db, snapshot)
    return state


def attrs_sync_status(hub, db, snapshot) -> Optional[dict]:
    """JSON attributes payload for the sync_status sensor. Always returns a
    dict with ``reason`` and ``triage_active`` so HA templating needn't guard
    for missing keys. When a triage pass is running, the progress fields are
    included so automations can read it."""
    _state, reason = compute_sync_status(hub, db, snapshot)
    attrs: dict = {"reason": reason, "triage_active": False}
    triage = (getattr(hub, "last_state", None) or {}).get("triage") or {}
    if triage.get("active"):
        attrs.update(
            triage_active=True,
            triaged=triage.get("triaged"),
            triage_total=triage.get("total"),
            triage_eta_s=triage.get("eta_s"),
        )
    return attrs


# ---- queue counts

def _queue_count(db, state: str) -> int:
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM download_queue WHERE state=?",
            (state,),
        ).fetchone()
    return row["n"]


def active_scope(hub, snap) -> Optional[str]:
    """Scope of the connection the worker is using, or None when the camera
    is offline or not yet seen — the same rule the queue router applies, so
    the HA sensor and the web UI agree on what is pending.

    An unknown online state yields no scope: for a passive sensor, counting
    everything until we know which connection we're on can only over-report,
    never hide a backlog.
    """
    if hub.last_state.get("dashcam_online") is not True:
        return None
    source = hub.last_state.get("dashcam_source")
    if source not in ("primary", "alternative"):
        return None
    return profile_for(snap, source).scope


def state_queue_pending(hub, db, snapshot) -> Optional[str]:
    """Clips the ACTIVE connection will actually download: pending minus the
    rows its scope holds back — the same number the web UI shows. With no
    active connection every pending row counts (nothing is held)."""
    held = _held_sql(active_scope(hub, snapshot))
    with db.conn() as c:
        row = c.execute(
            f"SELECT COUNT(*) AS n FROM download_queue "
            f"WHERE state='pending' AND ({held}) = 0"
        ).fetchone()
    return str(row["n"])


def state_queue_failed(hub, db, snapshot) -> Optional[str]:
    return str(_queue_count(db, "failed"))


def state_queue_downloading(hub, db, snapshot) -> Optional[str]:
    return str(_queue_count(db, "downloading"))


# ---- archive

def state_last_downloaded_clip(hub, db, snapshot) -> Optional[str]:
    with db.conn() as c:
        row = c.execute(
            "SELECT MAX(timestamp) AS m FROM clip_index"
        ).fetchone()
    ts = row["m"]
    if not ts:
        return None
    return _iso_z(int(ts))


def state_total_clips(hub, db, snapshot) -> Optional[str]:
    with db.conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM clip_index").fetchone()
    return str(row["n"])


# ---- current download

def state_current_filename(hub, db, snapshot) -> Optional[str]:
    ci = hub.last_state.get("current_item")
    if not ci:
        return None
    return ci.get("filename")


def state_current_progress(hub, db, snapshot) -> Optional[str]:
    ci = hub.last_state.get("current_item") or {}
    total = ci.get("total")
    done = ci.get("bytes")
    if not total or done is None:
        return None
    pct = round(100 * done / total, 1)
    return f"{pct}"


# Suppress publishing the session speed until the window has filled and
# the average has stabilised.
SPEED_PUBLISH_DELAY_S = 30.0


def state_download_speed(hub, db, snapshot) -> Optional[str]:
    """Session moving-average download speed in MB/s.

    Returns ``None`` (no publish) for the first ``SPEED_PUBLISH_DELAY_S``
    of a session or before the average is computable; ``"0"`` when idle.
    Combined with the entity's 60 s ``min_publish_interval_s`` this yields
    a first publish at ~30 s then at most once per 60 s.
    """
    sess = hub.last_state.get("session") or {}
    if not sess.get("active"):
        return "0"
    if (sess.get("elapsed_s") or 0) < SPEED_PUBLISH_DELAY_S:
        return None
    bps = sess.get("avg_speed_bps")
    if bps is None:
        return None
    return f"{bps / (1024 * 1024):.1f}"


# ---- disk

def state_disk_used(hub, db, snapshot) -> Optional[str]:
    """Report the higher of (filesystem %, quota %) — that's the
    rule closest to triggering retention cleanup. Reusing the
    retention service's cache means the sweep and the sensor see
    identical numbers.

    Filesystem mode (no quota set): the rule reports the underlying
    volume's used %. Quota mode (RECORDINGS_QUOTA_GB > 0): the rule
    reports bytes-under-recordings ÷ quota. Independent triggers
    (post-cherry-pick) mean both rules can be active at once; we
    publish the max so a single HA threshold alerts on either.
    """
    from . import retention as _ret
    quota = getattr(snapshot, "recordings_quota_gb", 0) or 0

    # Filesystem rule is always queryable (there's always a mounted
    # volume under recordings). Quota rule is opt-in via the setting.
    candidates = []
    pct_fs = _ret.disk_used_pct(snapshot.recordings, quota_gb=0)
    if pct_fs is not None:
        candidates.append(pct_fs)
    if quota > 0:
        pct_quota = _ret.disk_used_pct(snapshot.recordings, quota_gb=quota)
        if pct_quota is not None:
            candidates.append(pct_quota)
    if not candidates:
        return None
    return str(int(round(max(candidates))))


