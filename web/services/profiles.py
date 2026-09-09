"""Per-connection download profile resolution.

The sync worker reaches the camera over the primary or the alternative
address (see ``SyncWorker._select_active_address``) and each has its own
scope (what to download) and GPS-triage flag. This module is the single
place that maps a connection name onto those two values so the worker,
the queue read APIs, and the routers agree.

Pure: takes any object with the four snapshot attributes.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectionProfile:
    scope: str        # one of web.settings_schema.SCOPES
    gps_triage: bool


def profile_for(snap, source: str) -> ConnectionProfile:
    """Resolve the profile for ``source`` ("primary" | "alternative").

    Attribute access is deliberately strict: a snapshot missing the fields
    raises AttributeError rather than silently downloading everything.
    """
    if source == "primary":
        return ConnectionProfile(
            scope=snap.primary_scope, gps_triage=bool(snap.primary_gps_triage)
        )
    if source == "alternative":
        return ConnectionProfile(
            scope=snap.alternative_scope,
            gps_triage=bool(snap.alternative_gps_triage),
        )
    raise ValueError(f"unknown connection source: {source!r}")
