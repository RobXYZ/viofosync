"""Unit tests for the session download-speed + ETA tracker."""
from __future__ import annotations

from web.services.download_session import DownloadSession


class _Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def _mk(remaining=0, *, clock=None, window_s=30.0):
    c = clock or _Clock()
    s = DownloadSession(lambda: remaining, monotonic=c, window_s=window_s)
    return s, c


def test_inactive_until_started():
    s, _ = _mk()
    assert s.active is False
    assert s.avg_speed_bps is None
    assert s.eta_seconds is None
    assert s.snapshot()["active"] is False


def test_avg_speed_none_below_min_span():
    s, c = _mk()
    s.note_started("a", 1000)
    c.t = 0.5
    s.note_progress("a", 200, 1000)      # one sample → no span
    assert s.avg_speed_bps is None
    c.t = 1.0
    s.note_progress("a", 400, 1000)      # span 0.5 s < 2 s min
    assert s.avg_speed_bps is None


def test_windowed_average_speed():
    s, c = _mk()
    s.note_started("a", 10_000)
    c.t = 1.0
    s.note_progress("a", 1000, 10_000)
    c.t = 5.0
    s.note_progress("a", 5000, 10_000)
    # (5000-1000)/(5-1) = 1000 B/s
    assert s.avg_speed_bps == 1000.0


def test_wire_bytes_monotonic_across_file_boundary():
    s, c = _mk()
    s.note_started("a", 1000)
    c.t = 1.0
    s.note_progress("a", 1000, 1000)
    s.note_finished("a", 1000)
    s.note_started("b", 2000)
    c.t = 2.0
    s.note_progress("b", 500, 2000)
    assert s.session_bytes == 1500   # 1000 (a) + 500 (b)


def test_wire_bytes_monotonic_across_parallel_files():
    s, c = _mk()
    s.note_started("a", 1000)
    s.note_started("b", 2000)
    c.t = 1.0
    s.note_progress("a", 100, 1000)
    c.t = 2.0
    s.note_progress("b", 200, 2000)
    c.t = 3.0
    s.note_progress("a", 300, 1000)

    assert s.session_bytes == 500
    assert s.avg_speed_bps == 200.0


def test_eta_includes_all_active_parallel_files():
    s, c = _mk(remaining=1000)
    s.note_started("a", 1000)
    s.note_started("b", 2000)
    c.t = 1.0
    s.note_progress("a", 500, 1000)
    c.t = 5.0
    s.note_progress("b", 1000, 2000)

    # speed = (1500 - 500) / (5 - 1) = 250 B/s
    # remaining = pending 1000 + a remainder 500 + b remainder 1000
    assert s.avg_speed_bps == 250.0
    assert s.eta_seconds == 10.0


def test_retry_within_file_no_negative_delta():
    """download_file retries reset bytes_done WITHOUT a new item_started.
    The wire-byte counter must clamp the backward jump to zero."""
    s, c = _mk()
    s.note_started("a", 50_000_000)
    c.t = 1.0
    s.note_progress("a", 30_000_000, 50_000_000)
    # Connection drop → retry resumes from a small chunk, same file.
    c.t = 2.0
    s.note_progress("a", 65_536, 50_000_000)
    assert s.session_bytes == 30_000_000           # no decrease
    c.t = 3.0
    s.note_progress("a", 131_072, 50_000_000)
    assert s.session_bytes == 30_000_000 + 65_536  # climbs by the retry delta
    assert (s.avg_speed_bps or 0) >= 0


def test_eta_from_remaining_and_speed():
    s, c = _mk(remaining=9000)
    s.note_started("a", 10_000)
    c.t = 1.0
    s.note_progress("a", 1000, 10_000)
    c.t = 5.0
    s.note_progress("a", 5000, 10_000)
    # speed 1000 B/s; remaining = pending 9000 + current remainder 5000 = 14000
    assert s.avg_speed_bps == 1000.0
    assert s.eta_seconds == 14.0


def test_eta_none_when_no_speed():
    s, c = _mk(remaining=9000)
    s.note_started("a", 10_000)
    c.t = 0.5
    s.note_progress("a", 1000, 10_000)
    assert s.avg_speed_bps is None
    assert s.eta_seconds is None


def test_refresh_remaining_picks_up_new_value():
    box = {"v": 1000}
    c = _Clock()
    s = DownloadSession(lambda: box["v"], monotonic=c, window_s=30.0)
    s.note_started("a", 10_000)
    c.t = 1.0
    s.note_progress("a", 1000, 10_000)
    c.t = 5.0
    s.note_progress("a", 5000, 10_000)
    assert s.eta_seconds == 6.0     # (1000 + 5000) / 1000
    box["v"] = 20_000
    s.refresh_remaining()
    assert s.eta_seconds == 25.0    # (20000 + 5000) / 1000


def test_note_idle_resets():
    s, c = _mk(remaining=9000)
    s.note_started("a", 10_000)
    c.t = 1.0
    s.note_progress("a", 1000, 10_000)
    c.t = 5.0
    s.note_progress("a", 5000, 10_000)
    s.note_idle()
    assert s.active is False
    assert s.session_bytes == 0
    assert s.avg_speed_bps is None
    assert s.eta_seconds is None
    assert s._remaining_pending == 0
    assert s.snapshot() == {
        "active": False, "avg_speed_bps": None, "eta_seconds": None,
        "session_bytes": 0, "elapsed_s": 0.0,
    }


def test_old_samples_pruned_outside_window():
    s, c = _mk(window_s=10.0)
    s.note_started("a", 100_000)
    for t, b in [(1, 1000), (2, 2000), (12, 12000), (20, 30000)]:
        c.t = float(t)
        s.note_progress("a", b, 100_000)
    # cutoff = 20 - 10 = 10; (1,*) and (2,*) drop while >2 samples remain.
    assert len(s._samples) == 2
    assert s._samples[0][0] == 12.0
    assert s.avg_speed_bps == (30000 - 12000) / (20 - 12)


def test_progress_without_started_starts_session():
    """A stray item_progress with no prior item_started still begins a
    session rather than being dropped."""
    s, c = _mk()
    c.t = 1.0
    s.note_progress("a", 1000, 10_000)
    assert s.active is True
    assert s.session_bytes == 1000


def test_lifespan_wires_download_session(tmp_config_dir, tmp_recordings_dir):
    from fastapi.testclient import TestClient

    from web import app as app_mod
    from web import settings as settings_mod
    settings_mod.reset_for_tests()
    app = app_mod.create_app()
    with TestClient(app):
        assert isinstance(app.state.download_session, DownloadSession)
        # The Hub holds the same tracker instance it feeds.
        assert app.state.hub._session is app.state.download_session
