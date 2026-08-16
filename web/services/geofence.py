"""GPS geofence exclusion — auto-skip queued clips parked at home.

Reuses the stop detector in :mod:`web.services.gps` (a stop is already a
>= 5-min dwell) over a day's GPX tracks. A *home stop* is a detected stop
whose centre lies within a configured zone's radius; the queued clips whose
``recorded_at`` falls inside a home stop are marked ``skipped``/``geofence``
— except confirmed shutdown-tail dwells, which are protected, and dwells
still undecided, which are deferred to a later sweep (see shutdown_tails).

Pure-ish: detection here, mutations delegated to :mod:`web.services.queue`.
Off / inert unless at least one location is flagged for exclusion (and
GPS_TRIAGE, checked by the caller — without triaged skeletons there are no
tracks).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from ..db import Database
from . import day_tracks, gps
from . import queue as q
from . import triage as triage_service
from .naming import day_key_sql, gps_lens_sql

log = logging.getLogger("viofosync.geofence")

# Geofence detection considers a clip's track even after it has been
# auto-skipped, so re-evaluation still sees the full home dwell. Shared with
# the orphan sweep so skipped skeletons aren't deleted out from under us.
_DETECT_STATES = triage_service.SKELETON_KEEP_STATES

# States the untriaged-tail guard watches: SKELETON_KEEP_STATES minus
# 'skipped'. A clip already skipped at home is stationary — it can't be part
# of a moving drive, so it can't shift a journey end and doesn't need to hold
# up classification. And a rebuild-grouping pass returns skipped clips to
# 'pending' anyway, where the guard sees them like any other candidate.
_UNTRIAGED_GUARD_STATES = tuple(
    s for s in triage_service.SKELETON_KEEP_STATES if s != "skipped"
)

# A clip's recorded_at (filename second) precedes its first GPS fix by the
# receiver's acquisition lag (≈1 s driving, tens of seconds for a parking clip
# that re-acquires). A home stop's window starts at that first fix, so the
# dwell's leading clip lands just before it. Pad the leading edge by one
# nominal clip length to pull it in. Larger would risk swallowing the
# preceding pull-in drive clip (which sits a full clip + lag earlier).
LEADING_EDGE_PAD_S = 60

# Shutdown-tail detection. A camera configured to bypass parking mode keeps
# writing NORMAL clips for a few minutes after parking (the car holds ACC),
# then powers off. That trailing dwell is footage the user counts as part of
# the journey, so it must not be geofence-skipped. A dwell qualifies only when
# the recording session provably ENDS during it: a recording camera writes a
# file every ~60 s, so a TAIL_GAP_S hole in the camera's own filename listing
# is proof of shutdown. Parking-mode cameras must be excluded by event type,
# not the gap test — parking timelapse files land ~30 min apart, so the
# silence between two P files would otherwise read as a shutdown.
TAIL_CAP_S = 900.0      # silence must begin within this of arrival to count
TAIL_GAP_S = 300.0      # listing hole that proves "recording stopped"
TAIL_SLACK_S = 120.0    # guards the server's wall clock, not camera-clock skew
ARRIVAL_MATCH_S = 120.0  # stop-start <-> journey-end tolerance ("arrival stop")
NOMINAL_CLIP_S = 60.0   # extends the tail past its last file's *start* time


def shutdown_tails(
    db: Database,
    date: str,
    zone_stops: Sequence[tuple[float, float]],
    journey_ends: Sequence[float],
    now: float | None = None,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Classify each exclusion-zone stop on ``date`` as a *shutdown tail* or
    not. Returns ``(tails, undecided)``, each ``[(start_ts, end_ts), ...]``.

    A stop is a tail when (a) it is the arrival stop of a drive (its start is
    within ``ARRIVAL_MATCH_S`` of some journey end), (b) the camera's filename
    listing goes silent for > ``TAIL_GAP_S`` after one of its files, (c) that
    silence starts within ``TAIL_CAP_S`` of arrival, and (d) no parking-mode
    file follows the arrival (parking mode continuing IS the old behaviour).

    ``undecided`` spans are stops that may still be live: no later file proves
    the gap yet and wall-clock ``now`` hasn't passed the newest file by
    ``TAIL_GAP_S + TAIL_SLACK_S``. Also undecided: stops whose arriving
    drive's trailing clips haven't triaged yet — triage runs newest-first, so
    a sweep can see a dwell's skeletons before its drive's, making the
    journey shape (and the arrival match below) untrustworthy. Callers must
    not skip those clips yet.

    The gap/cap/parking tests above read only ``recorded_at`` and
    ``event_type`` (present for untriaged clips too), so downloads and skips
    alone don't perturb them. The untriaged guard is the deliberate exception:
    it reads ``state``/``triaged_at``, so a clip's own triage progress can
    change its answer.
    """
    now = time.time() if now is None else now
    day_expr = day_key_sql()
    # Day-scoped: a dwell straddling midnight can't see its continuation on
    # the next day. The pre-midnight portion is kept (no later file to prove
    # the gap wrong). But the post-midnight continuation is evaluated as its
    # own day, has no arriving journey to match, and reads as ordinary idle —
    # it gets skipped. Not a regression (the whole dwell was skipped before
    # this feature); fixing it needs a cross-day listing/journey view.
    #
    # One scan of the day's listing (day_expr has no index, so this is a full
    # table scan on every route read of a day with a zone stop) — the
    # untriaged-guard columns ride along instead of a second pass.
    with db.conn() as c:
        rows = c.execute(
            f"SELECT recorded_at, event_type, state, triaged_at, "
            f"({gps_lens_sql()}) AS is_gps_lens FROM download_queue "
            f"WHERE {day_expr} = ? AND recorded_at IS NOT NULL "
            f"ORDER BY recorded_at ASC",
            (date,),
        ).fetchall()
    ts_list = [r["recorded_at"] for r in rows]
    parking_ts = [
        r["recorded_at"] for r in rows if (r["event_type"] or "") == "parking"
    ]
    untriaged_ts = [
        r["recorded_at"] for r in rows
        if r["state"] in _UNTRIAGED_GUARD_STATES
        and r["triaged_at"] is None
        and r["is_gps_lens"]
    ]

    tails: list[tuple[float, float]] = []
    undecided: list[tuple[float, float]] = []
    for ss, _se in zone_stops:
        if any(ss <= t <= ss + TAIL_CAP_S + TAIL_GAP_S for t in parking_ts):
            continue  # parking mode engaged after arrival — never a tail
        run = [t for t in ts_list if t >= ss - LEADING_EDGE_PAD_S]
        if not run:
            continue
        last = run[0]
        stopped = False
        for t in run[1:]:
            if t - last > TAIL_GAP_S:
                stopped = True  # a later file proves the silence
                break
            last = t
        if last > ss + TAIL_CAP_S:
            continue  # recording outlived the cap — ordinary idle, not a tail
        pending = [t for t in untriaged_ts if ss - TAIL_GAP_S <= t < ss]
        if pending:
            # The arriving drive's trailing clips haven't triaged yet, so the
            # journey picture (and with it the arrival match below) can't be
            # trusted — triage is newest-first, so a sweep can see the dwell
            # before its drive. Defer rather than risk an irreversible skip.
            log.debug(
                "geofence: untriaged guard defers stop at %.0f (%d untriaged "
                "clip(s) in window)", ss, len(pending),
            )
            undecided.append((ss, last + NOMINAL_CLIP_S))
            continue
        if not any(abs(ss - je) <= ARRIVAL_MATCH_S for je in journey_ends):
            continue  # not a drive's arrival (e.g. a power-on-only session)
        if not stopped and now < last + TAIL_GAP_S + TAIL_SLACK_S:
            undecided.append((ss, last + NOMINAL_CLIP_S))
            continue  # may still be recording — defer the decision
        tails.append((ss, last + NOMINAL_CLIP_S))
    return tails, undecided


def home_stops(stops: Sequence[gps.Stop], zones: Sequence) -> list[gps.Stop]:
    """Stops whose centre is within any zone's radius (metres)."""
    out: list[gps.Stop] = []
    for s in stops:
        for z in zones:
            if gps._haversine_ll(
                s.center_lat, s.center_lon, z.lat, z.lon
            ) <= z.radius_m:
                out.append(s)
                break
    return out


def evaluate_day(
    db: Database, recordings: str, date: str, zones: Sequence,
    now: float | None = None,
) -> list[str]:
    """Auto-skip queued clips on ``date`` that dwell inside a home zone.
    Confirmed shutdown tails are protected and undecided dwells are deferred
    (see :func:`shutdown_tails`). Returns the filenames skipped (empty when
    no zones / no home stop)."""
    if not zones:
        return []
    candidates = q.geofence_candidates(db, date)
    if not candidates:
        return []
    paths = day_tracks.day_gpx_paths(
        db, recordings, date, queue_states=_DETECT_STATES
    )
    _points, stops, journeys = gps.aggregate_day(paths)
    home = home_stops(stops, zones)
    if not home:
        return []
    windows = [(s.start_time.timestamp(), s.end_time.timestamp()) for s in home]
    # A clip that's part of a drive must not be skipped even if its timestamp
    # dwells in a home zone — the journey wins over the home dwell, mirroring the
    # archive grid's "journey beats stop" grouping. Use the same padded journey
    # windows (parking-bounded via gps.expand_journey_window) so the pull-away /
    # pull-in clips at the dwell edge survive; only genuinely-parked clips skip.
    parking = day_tracks.day_parking_spans(db, date)
    drives = [
        gps.expand_journey_window(
            j.start_time.timestamp(), j.end_time.timestamp(), parking
        )
        for j in journeys
    ]

    def _in_drive(ts: float) -> bool:
        return any(lo <= ts <= hi for lo, hi in drives)

    # Shutdown tails: a bypass-parking camera keeps recording a few minutes
    # after parking, then powers off. Those dwell clips are journey footage —
    # protect confirmed tails, and defer (don't skip yet) dwells that might
    # still be live. See shutdown_tails for the classification rules.
    tails, undecided = shutdown_tails(
        db, date, windows,
        [j.end_time.timestamp() for j in journeys], now=now,
    )
    protected = tails + undecided

    def _in_tail(ts: float) -> bool:
        return any(lo - LEADING_EDGE_PAD_S <= ts <= hi for lo, hi in protected)

    to_skip = [
        c["filename"]
        for c in candidates
        if any(
            lo - LEADING_EDGE_PAD_S <= c["recorded_at"] <= hi
            for lo, hi in windows
        )
        and not _in_drive(c["recorded_at"])
        and not _in_tail(c["recorded_at"])
    ]
    if to_skip:
        q.geofence_skip(db, to_skip)
    return to_skip


def sweep_all(
    db: Database, recordings: str, zones: Sequence, *,
    seen: dict | None = None, now: float | None = None,
) -> int:
    """Evaluate every day that currently has pending clips. Returns the total
    number auto-skipped. Used for the per-cycle pass and the on-enable backfill.

    ``seen`` (optional) is a caller-owned ``{day: signature}`` cache. When
    given, a day is only re-evaluated if its triaged-skeleton signature changed
    since the cached value, so steady-state ticks skip the GPX re-parse. When
    ``None`` (a full sweep), every pending day is evaluated."""
    if not zones:
        return 0
    sigs = (
        q.geofence_day_signatures(db, _DETECT_STATES)
        if seen is not None else None
    )
    total = 0
    for day in q.pending_days(db):
        if seen is not None:
            sig = sigs.get(day, 0)
            if seen.get(day) == sig:
                continue
            seen[day] = sig
        total += len(evaluate_day(db, recordings, day, zones, now=now))
    if total:
        log.info("geofence: auto-skipped %d clip(s) parked at home", total)
    return total
