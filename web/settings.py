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
    validate_new_password,
    validate_partial,
)

log = logging.getLogger("viofosync.settings")


def _config_dir() -> Path:
    """Resolved each call so tests can monkeypatch CONFIG_DIR after import."""
    return Path(os.environ.get("CONFIG_DIR", "/config"))


Subscriber = Callable[[set[str], "Snapshot"], None]


@dataclass(frozen=True)
class Snapshot:
    """Immutable view of every setting the running app may need."""

    address: str | None
    recordings: str
    grouping: str
    use_html_listing: bool
    gps_extract: bool
    delete_after_download: bool
    timeout: float
    download_attempts: int
    max_attempts: int
    sync_interval_seconds: int
    enable_scheduled_sync: bool
    sync_ro_only: bool
    retention_max_days: int
    retention_disk_pct: int
    retention_protect_ro: bool
    recordings_quota_gb: int

    password_hash: str
    session_secret: str

    host: str
    port: int

    export_encoder_pref: str
    pip_position: str
    nominatim_email: str
    geocode_enabled: bool
    distance_units: str

    is_unconfigured: bool


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

    def subscribe(self, callback: Subscriber) -> None:
        self._subscribers.append(callback)

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
            recordings=self._recordings,
            grouping=m.GROUPING,
            use_html_listing=m.HTML,
            gps_extract=m.GPS_EXTRACT,
            delete_after_download=m.DELETE_AFTER_DOWNLOAD,
            timeout=float(m.TIMEOUT),
            download_attempts=m.DOWNLOAD_ATTEMPTS,
            max_attempts=m.MAX_DOWNLOAD_ATTEMPTS,
            sync_interval_seconds=m.SYNC_INTERVAL,
            enable_scheduled_sync=m.ENABLE_SCHEDULED_SYNC,
            sync_ro_only=m.SYNC_RO_ONLY,
            retention_max_days=m.RETENTION_MAX_DAYS,
            retention_disk_pct=m.RETENTION_DISK_PCT,
            retention_protect_ro=m.RETENTION_PROTECT_RO,
            recordings_quota_gb=m.RECORDINGS_QUOTA_GB,
            password_hash=m.WEB_PASSWORD_HASH,
            session_secret=m.SESSION_SECRET,
            host=m.WEB_HOST,
            port=m.WEB_PORT,
            export_encoder_pref=m.EXPORT_ENCODER,
            pip_position=m.PIP_POSITION,
            nominatim_email=m.NOMINATIM_EMAIL,
            geocode_enabled=m.GEOCODE_ENABLED,
            distance_units=m.DISTANCE_UNITS,
            is_unconfigured=not m.WEB_PASSWORD_HASH,
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
