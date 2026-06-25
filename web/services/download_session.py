"""Session-level download throughput + ETA tracking.

A :class:`DownloadSession` accumulates progress events across the files
of one download run and exposes a windowed moving-average speed plus an
ETA. All math lives here — no broker, DB, or socket I/O — so it is
unit-testable with an injected clock and a stubbed remaining-bytes
provider.

It is fed exclusively from ``Hub.broadcast`` on the event loop, so no
locking is required.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from collections import deque
from typing import Callable, Deque, Optional, Tuple


@dataclass
class _ActiveFile:
    bytes_done: int = 0
    total: Optional[int] = None


class DownloadSession:
    # Below this many seconds of sample span the windowed speed is too
    # noisy to report.
    _MIN_SPAN_S = 2.0

    def __init__(
        self,
        remaining_bytes_provider: Callable[[], int],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        window_s: float = 30.0,
    ) -> None:
        self._remaining_provider = remaining_bytes_provider
        self._mono = monotonic
        self._window_s = window_s
        self._active = False
        self._started_at: Optional[float] = None
        self._wire_bytes = 0
        self._samples: Deque[Tuple[float, int]] = deque()
        self._cur_file: Optional[str] = None
        self._cur_file_bytes = 0
        self._cur_total: Optional[int] = None
        self._active_files: dict[str, _ActiveFile] = {}
        self._remaining_pending = 0

    # ---- event feeds (called on the loop) ----

    def note_started(self, filename: str, total: Optional[int]) -> None:
        if not self._active:
            self._active = True
            self._started_at = self._mono()
            self._wire_bytes = 0
            self._samples.clear()
        self._cur_file = filename
        self._cur_file_bytes = 0
        self._cur_total = total
        self._active_files[filename] = _ActiveFile(total=total)
        self._refresh_remaining()

    def note_progress(
        self, filename: str, bytes_done: int, total: Optional[int],
    ) -> None:
        if not self._active:
            self.note_started(filename, total)
        cur = self._active_files.get(filename)
        if cur is None:
            # A file we never saw an item_started for.
            cur = _ActiveFile(total=total)
            self._active_files[filename] = cur
        delta = bytes_done - cur.bytes_done
        if delta < 0:
            # A retry reset bytes_done within the same file — never let the
            # monotonic counter go backwards.
            delta = 0
        self._wire_bytes += delta
        cur.bytes_done = bytes_done
        cur.total = total
        self._cur_file = filename
        self._cur_file_bytes = bytes_done
        self._cur_total = total
        now = self._mono()
        self._samples.append((now, self._wire_bytes))
        self._prune(now)

    def note_finished(
        self, filename: str, bytes_written: Optional[int],
    ) -> None:
        # Progress ticks already accounted for the bytes; just clear the
        # per-file cursor so the next file starts fresh.
        self._active_files.pop(filename, None)
        self._cur_file = None
        self._cur_file_bytes = 0
        self._cur_total = None
        self._refresh_remaining()

    def note_idle(self) -> None:
        self._active = False
        self._started_at = None
        self._wire_bytes = 0
        self._samples.clear()
        self._cur_file = None
        self._cur_file_bytes = 0
        self._cur_total = None
        self._active_files.clear()
        self._remaining_pending = 0

    def refresh_remaining(self) -> None:
        self._refresh_remaining()

    # ---- derived values ----

    @property
    def active(self) -> bool:
        return self._active

    @property
    def elapsed_s(self) -> float:
        if not self._active or self._started_at is None:
            return 0.0
        return max(0.0, self._mono() - self._started_at)

    @property
    def session_bytes(self) -> int:
        return self._wire_bytes

    @property
    def avg_speed_bps(self) -> Optional[float]:
        if len(self._samples) < 2:
            return None
        t0, b0 = self._samples[0]
        t1, b1 = self._samples[-1]
        span = t1 - t0
        if span < self._MIN_SPAN_S:
            return None
        return max(0.0, (b1 - b0) / span)

    @property
    def eta_seconds(self) -> Optional[float]:
        speed = self.avg_speed_bps
        if not speed:            # None or 0
            return None
        remaining = self._remaining_pending
        for cur in self._active_files.values():
            if cur.total:
                remaining += max(0, cur.total - cur.bytes_done)
        if remaining <= 0:
            return 0.0
        return remaining / speed

    def snapshot(self) -> dict:
        return {
            "active": self._active,
            "avg_speed_bps": self.avg_speed_bps,
            "eta_seconds": self.eta_seconds,
            "session_bytes": self._wire_bytes,
            "elapsed_s": self.elapsed_s,
        }

    # ---- internals ----

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        # Always keep at least 2 samples so a steady transfer still has a
        # span to measure; drop older ones once a newer in-window sample
        # exists.
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def _refresh_remaining(self) -> None:
        try:
            self._remaining_pending = int(self._remaining_provider() or 0)
        except Exception:
            self._remaining_pending = 0
