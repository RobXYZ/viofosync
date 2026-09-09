"""Settings provider — JSON-backed, hot-reloadable.

Replaces the boot-time frozen-dataclass model. Callers ask the
provider for a Snapshot per operation; mutations go through
``update()``/``set_password()``/``rotate_session_secret()`` and
broadcast a "settings changed" event to subscribers.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import bcrypt

from .config_store import ConfigStore, MigrationResult
from .settings_schema import (
    DEFAULT_VALUES,
    SettingsModel,
    normalize_locations,
    validate_new_password,
    validate_partial,
)

log = logging.getLogger("viofosync.settings")


def _config_dir() -> Path:
    """Resolved each call so tests can monkeypatch CONFIG_DIR after import."""
    return Path(os.environ.get("CONFIG_DIR", "/config"))


Subscriber = Callable[[set[str], "Snapshot"], None]


@dataclass(frozen=True)
class Place:
    """Immutable snapshot view of one named location (metres for radius)."""

    name: str
    lat: float
    lon: float
    radius_m: int
    exclude_recordings: bool
    is_home: bool = False


@dataclass(frozen=True)
class Snapshot:
    """Immutable view of every setting the running app may need."""

    address: str | None
    address_fallback: str | None
    recordings: str
    import_path: str
    grouping: str
    use_html_listing: bool
    gps_extract: bool
    # Derived: True when GPS triage is on for at least one connection. Gates
    # everything that only needs skeleton tracks to *exist* (archive merge,
    # geofence pass, Locations UI). Per-connection flags below decide whether
    # the worker actually runs triage on the link it is currently using.
    gps_triage: bool
    derive_thumbs_eager: bool
    derive_filmstrips_eager: bool
    delete_after_download: bool
    timeout: float
    download_attempts: int
    max_attempts: int
    sync_interval_seconds: int
    enable_scheduled_sync: bool
    primary_scope: str
    primary_gps_triage: bool
    alternative_scope: str
    alternative_gps_triage: bool
    retention_max_days: int
    retention_disk_pct: int
    retention_protect_ro: bool
    recordings_quota_gb: int
    disk_critical_pct: int
    locations: tuple[Place, ...]

    password_hash: str
    session_secret: str

    host: str
    port: int

    export_encoder_pref: str
    pip_position: str
    nominatim_email: str
    geocode_enabled: bool
    distance_units: str
    instance_name: str

    is_unconfigured: bool

    mqtt_enabled: bool
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    mqtt_tls: bool
    mqtt_client_id: str
    mqtt_discovery_prefix: str
    mqtt_node_id: str
    mqtt_discovery_enabled: bool
    mqtt_qos: int


_LEGACY_PROFILE_KEYS = ("SYNC_RO_ONLY", "GPS_TRIAGE")


def _as_bool(v: object) -> bool:
    """Coerce a JSON/env-style boolean the way Pydantic's lax mode would."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _migrate_connection_profiles(data: dict) -> bool:
    """Fold the pre-2026-09 global SYNC_RO_ONLY / GPS_TRIAGE toggles into the
    per-connection profile keys. Returns True if ``data`` was changed.

    Legacy values seed BOTH connections so behaviour is unchanged after
    upgrade. A key already present is never overwritten, which keeps the
    migration idempotent; only the legacy keys are dropped. No-op (and no
    write) when no legacy key is present.
    """
    if not any(k in data for k in _LEGACY_PROFILE_KEYS):
        return False
    scope = "ro_only" if _as_bool(data.get("SYNC_RO_ONLY")) else "everything"
    triage = _as_bool(data.get("GPS_TRIAGE", False))
    if "SYNC_RO_ONLY" in data:
        data.setdefault("PRIMARY_SCOPE", scope)
        data.setdefault("ALTERNATIVE_SCOPE", scope)
    if "GPS_TRIAGE" in data:
        data.setdefault("PRIMARY_GPS_TRIAGE", triage)
        data.setdefault("ALTERNATIVE_GPS_TRIAGE", triage)
    for k in _LEGACY_PROFILE_KEYS:
        data.pop(k, None)
    return True


class SettingsProvider:
    def __init__(
        self,
        config_path: Path | str | None = None,
        env_file_path: Path | str | None = None,
        recordings_dir: str | None = None,
    ) -> None:
        cd = _config_dir()
        self._store = ConfigStore(config_path or (cd / "config.json"))
        self._env_file = Path(env_file_path or (cd / "viofosync.env"))
        self._audit_path = cd / "settings-audit.log"
        self._recordings = recordings_dir or os.environ.get("RECORDINGS", "/recordings")
        self._lock = threading.RLock()
        self._subscribers: list[Subscriber] = []
        self._snapshot: Snapshot = self._load_snapshot()

    # ------------------------------------------------------------------ get

    def get(self) -> Snapshot:
        return self._snapshot

    @property
    def config_path(self) -> Path:
        """Absolute path of the JSON file settings persist to.
        Surfaced in Settings → System so operators can find it
        for hand-edits or backup."""
        return self._store.path

    # ----------------------------------------------------------------- set

    def update(self, patch: dict, actor: str) -> Snapshot:
        coerced = validate_partial(patch)  # rejects WEB_PASSWORD (use set_password)
        with self._lock:
            data = self._store.load()
            data.update(coerced)
            # Best-effort: if WEB_HOST/WEB_PORT changed and we're not already
            # bound to the new pair, try to bind briefly to confirm it works.
            if {"WEB_HOST", "WEB_PORT"} & set(coerced.keys()):
                new_host = coerced.get("WEB_HOST", data.get("WEB_HOST", "0.0.0.0"))
                new_port = int(coerced.get("WEB_PORT", data.get("WEB_PORT", 8080)))
                cur_host = self._snapshot.host
                cur_port = self._snapshot.port
                if (new_host, new_port) != (cur_host, cur_port):
                    import socket as _sock
                    try:
                        with _sock.socket() as s:
                            s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
                            s.bind((new_host, new_port))
                    except OSError as e:
                        raise ValueError(
                            f"cannot bind {new_host}:{new_port}: {e}"
                        ) from e
            self._validate_full(data)
            self._store.write(data)
            self._snapshot = self._make_snapshot(data)
            self._audit(actor, set(coerced.keys()))
            self._broadcast(set(coerced.keys()))
        return self._snapshot

    def set_password(self, plaintext: str, actor: str) -> Snapshot:
        validate_new_password(plaintext)
        digest = bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode()
        with self._lock:
            data = self._store.load()
            data["WEB_PASSWORD_HASH"] = digest
            self._store.write(data)
            self._snapshot = self._make_snapshot(data)
            self._audit(actor, {"WEB_PASSWORD_HASH"})
            self._broadcast({"WEB_PASSWORD_HASH"})
        return self._snapshot

    def rotate_session_secret(self, actor: str) -> str:
        new_secret = secrets.token_hex(32)
        with self._lock:
            data = self._store.load()
            data["SESSION_SECRET"] = new_secret
            self._store.write(data)
            self._snapshot = self._make_snapshot(data)
            self._audit(actor, {"SESSION_SECRET"})
            self._broadcast({"SESSION_SECRET"})
        return new_secret

    # ---------------------------------------------------------- subscribe

    def subscribe(self, callback: Subscriber):
        """Register ``callback`` for settings-change notifications.
        Returns a zero-arg function that unsubscribes it — the app
        lifespan calls these on shutdown so the module-level singleton
        provider doesn't accumulate callbacks pinning dead app objects
        across repeated lifespans (tests, uvicorn reload)."""
        self._subscribers.append(callback)

        def _unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return _unsubscribe

    def _broadcast(self, keys: set[str]) -> None:
        for cb in list(self._subscribers):
            try:
                cb(keys, self._snapshot)
            except Exception:  # pragma: no cover — subscribers must not raise
                log.exception("settings subscriber raised")

    # -------------------------------------------------------- internals

    def _load_snapshot(self) -> Snapshot:
        # One-shot migration; no-op if config.json already exists.
        result = self._store.migrate_from_env(self._env_file)
        if result == MigrationResult.MIGRATED:
            self._post_migrate_password_hash()

        # Mint and persist a SESSION_SECRET if one isn't already on
        # disk; otherwise every restart would invalidate every
        # session cookie. Has to run before the snapshot is built.
        data = self._store.load()
        # One-shot: fold a pre-existing GEOFENCE_ZONES (the earlier geofence
        # design) into the LOCATIONS list, preserving whether exclusion was on.
        if "LOCATIONS" not in data and data.get("GEOFENCE_ZONES"):
            excl = bool(data.get("GEOFENCE_ENABLED"))
            valid = [
                z for z in data["GEOFENCE_ZONES"]
                if z.get("lat") is not None and z.get("lon") is not None
            ]
            data["LOCATIONS"] = [
                {
                    "name": "Home" if i == 0 else f"Location {i + 1}",
                    "lat": z["lat"],
                    "lon": z["lon"],
                    "radius_m": z.get("radius_m", 30),
                    "exclude_recordings": excl,
                }
                for i, z in enumerate(valid)
            ]
            data.pop("GEOFENCE_ZONES", None)
            data.pop("GEOFENCE_ENABLED", None)
            self._store.write(data)
        # One-shot: back-fill is_home on locations written by the previous
        # design (no is_home key). normalize_locations promotes the first place
        # to Home; persist so the invariant holds on disk, not just per-read.
        locs = data.get("LOCATIONS")
        if locs and any("is_home" not in loc for loc in locs):
            data["LOCATIONS"] = normalize_locations(locs)
            self._store.write(data)
        # One-shot: global SYNC_RO_ONLY / GPS_TRIAGE → per-connection profiles.
        if _migrate_connection_profiles(data):
            self._store.write(data)
        if "SESSION_SECRET" not in data:
            data["SESSION_SECRET"] = secrets.token_hex(32)
            self._store.write(data)

        return self._make_snapshot(data)

    def _post_migrate_password_hash(self) -> None:
        """If migration brought in a plaintext WEB_PASSWORD, hash it now
        and replace it with WEB_PASSWORD_HASH."""
        data = self._store.load()
        plain = data.pop("WEB_PASSWORD", None)
        if plain and "WEB_PASSWORD_HASH" not in data:
            data["WEB_PASSWORD_HASH"] = bcrypt.hashpw(
                str(plain).encode("utf-8"), bcrypt.gensalt()
            ).decode()
        self._store.write(data)

    def _validate_full(self, data: dict) -> None:
        """Run the model over the merged config so cross-field rules
        catch invalid combinations on disk."""
        SettingsModel(**{**DEFAULT_VALUES, **{
            k: v for k, v in data.items()
            if k in DEFAULT_VALUES
        }})

    def _make_snapshot(self, data: dict) -> Snapshot:
        merged = {**DEFAULT_VALUES, **{
            k: v for k, v in data.items() if k in DEFAULT_VALUES
        }}
        m = SettingsModel(**merged)
        return Snapshot(
            address=m.ADDRESS,
            address_fallback=m.ADDRESS_FALLBACK,
            recordings=self._recordings,
            import_path=m.IMPORT_PATH,
            grouping=m.GROUPING,
            use_html_listing=m.HTML,
            gps_extract=m.GPS_EXTRACT,
            gps_triage=bool(m.PRIMARY_GPS_TRIAGE or m.ALTERNATIVE_GPS_TRIAGE),
            derive_thumbs_eager=m.DERIVE_THUMBS_EAGER,
            derive_filmstrips_eager=m.DERIVE_FILMSTRIPS_EAGER,
            delete_after_download=m.DELETE_AFTER_DOWNLOAD,
            timeout=float(m.TIMEOUT),
            download_attempts=m.DOWNLOAD_ATTEMPTS,
            max_attempts=m.MAX_DOWNLOAD_ATTEMPTS,
            sync_interval_seconds=m.SYNC_INTERVAL,
            enable_scheduled_sync=m.ENABLE_SCHEDULED_SYNC,
            primary_scope=m.PRIMARY_SCOPE,
            primary_gps_triage=m.PRIMARY_GPS_TRIAGE,
            alternative_scope=m.ALTERNATIVE_SCOPE,
            alternative_gps_triage=m.ALTERNATIVE_GPS_TRIAGE,
            retention_max_days=m.RETENTION_MAX_DAYS,
            retention_disk_pct=m.RETENTION_DISK_PCT,
            retention_protect_ro=m.RETENTION_PROTECT_RO,
            recordings_quota_gb=m.RECORDINGS_QUOTA_GB,
            disk_critical_pct=m.DISK_CRITICAL_PCT,
            locations=tuple(
                Place(loc.name, loc.lat, loc.lon, loc.radius_m,
                      loc.exclude_recordings, loc.is_home)
                for loc in m.LOCATIONS
            ),
            password_hash=m.WEB_PASSWORD_HASH,
            session_secret=m.SESSION_SECRET,
            host=m.WEB_HOST,
            port=m.WEB_PORT,
            export_encoder_pref=m.EXPORT_ENCODER,
            pip_position=m.PIP_POSITION,
            nominatim_email=m.NOMINATIM_EMAIL,
            geocode_enabled=m.GEOCODE_ENABLED,
            distance_units=m.DISTANCE_UNITS,
            instance_name=m.INSTANCE_NAME,
            is_unconfigured=not m.WEB_PASSWORD_HASH,
            mqtt_enabled=m.MQTT_ENABLED,
            mqtt_host=m.MQTT_HOST,
            mqtt_port=m.MQTT_PORT,
            mqtt_username=m.MQTT_USERNAME,
            mqtt_password=m.MQTT_PASSWORD,
            mqtt_tls=m.MQTT_TLS,
            mqtt_client_id=m.MQTT_CLIENT_ID,
            mqtt_discovery_prefix=m.MQTT_DISCOVERY_PREFIX,
            mqtt_node_id=m.MQTT_NODE_ID,
            mqtt_discovery_enabled=m.MQTT_DISCOVERY_ENABLED,
            mqtt_qos=m.MQTT_QOS,
        )

    # ---------------------------------------------------------- audit log

    def _audit(self, actor: str, keys: set[str]) -> None:
        import datetime
        import json as _json
        entry = {
            "ts": datetime.datetime.now(datetime.UTC).isoformat(),
            "actor": actor,
            "keys": sorted(keys),
        }
        try:
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(entry) + "\n")
            os.chmod(self._audit_path, 0o600)
        except OSError as e:  # pragma: no cover — non-fatal
            log.warning("could not append audit log: %s", e)


# Module-level singleton used by web.app at startup.
_provider: SettingsProvider | None = None


def get_provider() -> SettingsProvider:
    """Lazy singleton — created on first call."""
    global _provider
    if _provider is None:
        _provider = SettingsProvider()
    return _provider


def reset_for_tests() -> None:
    """Allow tests to clear the singleton between cases."""
    global _provider
    _provider = None
