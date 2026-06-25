"""Hub.connect handshake regressions."""
from __future__ import annotations

import types as _types

from starlette.websockets import WebSocketDisconnect

from web.services.hub import Hub


class _FakeWS:
    """Minimal WebSocket stand-in for testing Hub.connect.

    Records the call sequence and lets the test inject the
    behaviour of ``send_json`` (success or various disconnect
    flavours)."""

    def __init__(self, *, send_raises: BaseException | None = None) -> None:
        self.accept_calls = 0
        self.send_calls = 0
        self._send_raises = send_raises

    async def accept(self) -> None:
        self.accept_calls += 1

    async def send_json(self, _payload) -> None:
        self.send_calls += 1
        if self._send_raises is not None:
            raise self._send_raises


# ---- happy path ----

async def test_connect_sends_snapshot_and_keeps_client() -> None:
    hub = Hub()
    ws = _FakeWS()
    await hub.connect(ws)
    assert ws.accept_calls == 1
    assert ws.send_calls == 1
    assert ws in hub._clients


# ---- regression: client disconnects between accept() and send_json() ----

async def test_connect_swallows_disconnect_during_snapshot() -> None:
    """Browser hot-reload / page navigation can close the WS in
    the millisecond between accept() and the first send. The
    handshake must NOT propagate a 500 — and the now-dead client
    must not stay in the broadcast set."""
    hub = Hub()
    ws = _FakeWS(send_raises=WebSocketDisconnect(code=1006))
    # Should not raise.
    await hub.connect(ws)
    assert ws not in hub._clients


async def test_connect_swallows_runtime_error_during_snapshot() -> None:
    """uvicorn's WS impl can raise RuntimeError("Cannot call send
    once a close message has been sent.") on the same race."""
    hub = Hub()
    ws = _FakeWS(send_raises=RuntimeError("send after close"))
    await hub.connect(ws)
    assert ws not in hub._clients


async def test_connect_swallows_oserror_during_snapshot() -> None:
    """Pipe broken / network died between accept and send."""
    hub = Hub()
    ws = _FakeWS(send_raises=OSError("broken pipe"))
    await hub.connect(ws)
    assert ws not in hub._clients


# ---- last_state accumulation for new event types ----

async def test_broadcast_sync_error_stored_in_last_state() -> None:
    hub = Hub()
    await hub.broadcast({
        "type": "sync_error",
        "kind": "recordings_unwritable",
        "message": "recordings path not writable",
    })
    assert hub.last_state["sync_error"] == {
        "kind": "recordings_unwritable",
        "message": "recordings path not writable",
    }


async def test_broadcast_sync_error_with_kind_none_clears() -> None:
    """The worker sends sync_error with kind=None to clear a previously
    sticky error."""
    hub = Hub()
    await hub.broadcast({
        "type": "sync_error", "kind": "config", "message": "x",
    })
    assert hub.last_state["sync_error"] is not None
    await hub.broadcast({"type": "sync_error", "kind": None, "message": None})
    assert hub.last_state["sync_error"] is None


async def test_broadcast_disk_pct_stored_in_last_state() -> None:
    hub = Hub()
    await hub.broadcast({"type": "disk_pct", "pct": 87.3})
    assert hub.last_state["disk_pct"] == 87.3


async def test_initial_last_state_includes_new_keys() -> None:
    """Newly-constructed Hub exposes the new keys as None so consumers
    that read them at startup don't KeyError."""
    hub = Hub()
    assert "sync_error" in hub.last_state
    assert "disk_pct" in hub.last_state
    assert "sync_status" in hub.last_state
    assert "sync_status_reason" in hub.last_state
    assert "current_items" in hub.last_state
    assert hub.last_state["sync_error"] is None
    assert hub.last_state["disk_pct"] is None
    assert hub.last_state["sync_status"] is None
    assert hub.last_state["sync_status_reason"] is None
    assert hub.last_state["current_items"] == {}


async def test_broadcast_tracks_multiple_current_items() -> None:
    hub = Hub()
    await hub.broadcast({"type": "item_started", "filename": "a.MP4", "total": 100})
    await hub.broadcast({"type": "item_started", "filename": "b.MP4", "total": 200})
    await hub.broadcast({
        "type": "item_progress",
        "filename": "a.MP4",
        "bytes": 40,
        "total": 100,
        "speed": 10,
    })

    assert set(hub.last_state["current_items"]) == {"a.MP4", "b.MP4"}
    assert hub.last_state["current_items"]["a.MP4"]["bytes"] == 40

    await hub.broadcast({"type": "item_finished", "filename": "a.MP4"})
    assert set(hub.last_state["current_items"]) == {"b.MP4"}
    assert hub.last_state["current_item"]["filename"] == "b.MP4"


def _stub_provider(**snap_overrides):
    """Tiny provider stub with .get() returning a settings-like object."""
    base = dict(address="192.168.1.50", recordings="/r", disk_critical_pct=95)
    base.update(snap_overrides)
    snap = _types.SimpleNamespace(**base)
    return _types.SimpleNamespace(get=lambda: snap)


class _RecordingWS:
    def __init__(self):
        self.sent = []
    async def accept(self): pass
    async def send_json(self, payload): self.sent.append(payload)


async def test_hub_emits_sync_status_after_dashcam_offline() -> None:
    """Driving the dashcam offline mid-download should produce a follow-up
    sync_status event with state="waiting"."""
    hub = Hub(settings_provider=_stub_provider())
    ws = _RecordingWS()
    await hub.connect(ws)
    ws.sent.clear()
    # Worker says: running, dashcam was online, mid-item.
    await hub.broadcast({"type": "sync_state", "running": True, "paused": False})
    await hub.broadcast({"type": "dashcam_online"})
    await hub.broadcast({"type": "item_started", "filename": "x.mp4", "total": 100})
    # At this point the status should be "downloading".
    assert hub.last_state["sync_status"] == "downloading"
    ws.sent.clear()
    # Dashcam drives away.
    await hub.broadcast({"type": "dashcam_offline"})
    assert hub.last_state["sync_status"] == "waiting"
    # A sync_status follow-up event was emitted.
    follow_ups = [e for e in ws.sent if e.get("type") == "sync_status"]
    assert follow_ups == [{"type": "sync_status",
                            "status": "waiting", "reason": None}]


async def test_hub_does_not_emit_sync_status_when_unchanged() -> None:
    """Re-broadcasting the same upstream event (or one that doesn't
    flip status) must not produce duplicate sync_status events."""
    hub = Hub(settings_provider=_stub_provider())
    ws = _RecordingWS()
    await hub.connect(ws)
    await hub.broadcast({"type": "sync_state", "running": True, "paused": False})
    await hub.broadcast({"type": "dashcam_online"})
    ws.sent.clear()
    # Second dashcam_online — status is still "waiting" (no current item),
    # same as before, so no follow-up event.
    await hub.broadcast({"type": "dashcam_online"})
    follow_ups = [e for e in ws.sent if e.get("type") == "sync_status"]
    assert follow_ups == []


async def test_hub_emits_sync_status_error_with_reason() -> None:
    hub = Hub(settings_provider=_stub_provider())
    ws = _RecordingWS()
    await hub.connect(ws)
    ws.sent.clear()
    await hub.broadcast({
        "type": "sync_error",
        "kind": "recordings_unwritable",
        "message": "recordings path not writable",
    })
    assert hub.last_state["sync_status"] == "error"
    follow_ups = [e for e in ws.sent if e.get("type") == "sync_status"]
    assert follow_ups == [{
        "type": "sync_status",
        "status": "error",
        "reason": "recordings path not writable",
    }]


async def test_hub_emits_sync_status_when_disk_crosses_threshold() -> None:
    hub = Hub(settings_provider=_stub_provider(disk_critical_pct=95))
    ws = _RecordingWS()
    await hub.connect(ws)
    await hub.broadcast({"type": "sync_state", "running": True, "paused": False})
    await hub.broadcast({"type": "dashcam_online"})
    ws.sent.clear()
    await hub.broadcast({"type": "disk_pct", "pct": 80.0})
    assert hub.last_state["sync_status"] == "waiting"  # still under threshold
    follow_ups_before = [e for e in ws.sent if e.get("type") == "sync_status"]
    assert follow_ups_before == []
    ws.sent.clear()
    await hub.broadcast({"type": "disk_pct", "pct": 96.4})
    assert hub.last_state["sync_status"] == "error"
    follow_ups_after = [e for e in ws.sent if e.get("type") == "sync_status"]
    assert follow_ups_after == [{
        "type": "sync_status", "status": "error", "reason": "disk 96% full",
    }]


async def test_hub_initial_snapshot_carries_sync_status() -> None:
    hub = Hub(settings_provider=_stub_provider())
    # Drive into a known state before any client connects.
    await hub.broadcast({"type": "sync_state", "running": True, "paused": False})
    await hub.broadcast({"type": "dashcam_offline"})
    ws = _RecordingWS()
    await hub.connect(ws)
    # First message after connect is the snapshot.
    assert ws.sent[0]["type"] == "snapshot"
    assert ws.sent[0]["state"]["sync_status"] == "waiting"


async def test_hub_snapshot_carries_sync_status_reason() -> None:
    """The WS snapshot must include both sync_status and the reason so
    a freshly-connected client (e.g. after a page refresh) renders the
    error reason in the badge without waiting for a state change."""
    hub = Hub(settings_provider=_stub_provider())
    await hub.broadcast({"type": "sync_state", "running": True, "paused": False})
    await hub.broadcast({
        "type": "sync_error", "kind": "recordings_unwritable",
        "message": "recordings path not writable",
    })
    ws = _RecordingWS()
    await hub.connect(ws)
    snap = ws.sent[0]
    assert snap["type"] == "snapshot"
    assert snap["state"]["sync_status"] == "error"
    assert snap["state"]["sync_status_reason"] == "recordings path not writable"


async def test_hub_rebroadcasts_when_reason_changes_but_status_does_not() -> None:
    """When status stays 'error' but the reason text changes (e.g.
    disk pct climbs from 95% to 99%), the hub MUST re-emit so the UI
    badge updates."""
    hub = Hub(settings_provider=_stub_provider(disk_critical_pct=95))
    ws = _RecordingWS()
    await hub.connect(ws)
    await hub.broadcast({"type": "sync_state", "running": True, "paused": False})
    await hub.broadcast({"type": "disk_pct", "pct": 95.0})
    assert hub.last_state["sync_status"] == "error"
    assert hub.last_state["sync_status_reason"] == "disk 95% full"
    ws.sent.clear()
    # Disk climbs — same status, new reason.
    await hub.broadcast({"type": "disk_pct", "pct": 99.0})
    follow_ups = [e for e in ws.sent if e.get("type") == "sync_status"]
    assert follow_ups == [{
        "type": "sync_status", "status": "error", "reason": "disk 99% full",
    }]
    assert hub.last_state["sync_status_reason"] == "disk 99% full"


async def test_dashcam_online_stores_source_and_address() -> None:
    hub = Hub()
    await hub.broadcast({
        "type": "dashcam_online", "source": "alternative",
        "address": "10.0.0.2",
    })
    assert hub.last_state["dashcam_online"] is True
    assert hub.last_state["dashcam_source"] == "alternative"
    assert hub.last_state["dashcam_address"] == "10.0.0.2"


async def test_dashcam_offline_keeps_last_source() -> None:
    hub = Hub()
    await hub.broadcast({
        "type": "dashcam_online", "source": "alternative",
        "address": "10.0.0.2",
    })
    await hub.broadcast({"type": "dashcam_offline"})
    assert hub.last_state["dashcam_online"] is False
    # Source/address are retained so the HA sensor reads "offline"
    # without losing which address was last live.
    assert hub.last_state["dashcam_source"] == "alternative"
    assert hub.last_state["dashcam_address"] == "10.0.0.2"


async def test_hub_compute_exception_does_not_break_broadcast() -> None:
    """If status computation raises, the upstream event still propagates
    and no sync_status event is emitted."""
    # Use a provider whose .get() raises, to force compute to fail.
    class _Boom:
        def get(self_):
            raise RuntimeError("boom")
    hub = Hub(settings_provider=_Boom())
    ws = _RecordingWS()
    await hub.connect(ws)
    ws.sent.clear()
    await hub.broadcast({"type": "dashcam_online"})
    # Upstream event still delivered.
    assert any(e.get("type") == "dashcam_online" for e in ws.sent)
    # No sync_status follow-up.
    assert not any(e.get("type") == "sync_status" for e in ws.sent)


# ---- regression: one stalled client must not block the others ----

class _StalledWS:
    """send_json applies indefinite backpressure (zombie tab, closed
    laptop lid) — it never raises, it just never completes."""

    def __init__(self):
        self.accept_calls = 0

    async def accept(self) -> None:
        self.accept_calls += 1

    async def send_json(self, _payload) -> None:
        import asyncio
        await asyncio.sleep(60)


class _HealthyWS:
    def __init__(self):
        self.received: list = []

    async def accept(self) -> None:
        pass

    async def send_json(self, payload) -> None:
        self.received.append(payload)


async def test_stalled_client_does_not_block_broadcast(monkeypatch) -> None:
    import asyncio
    import time

    import web.services.hub as hub_mod

    monkeypatch.setattr(hub_mod, "SEND_TIMEOUT_S", 0.1, raising=False)
    hub = Hub()
    stalled = _StalledWS()
    healthy = _HealthyWS()
    hub._clients.update({stalled, healthy})

    started = time.monotonic()
    await asyncio.wait_for(
        hub.broadcast({"type": "ping"}), timeout=2.0,
    )
    elapsed = time.monotonic() - started

    assert {"type": "ping"} in healthy.received, \
        "healthy client starved by a stalled one"
    assert elapsed < 1.5
    assert stalled not in hub._clients, "stalled client not evicted"
    assert healthy in hub._clients
