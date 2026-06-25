"""Validation schema for the JSON config payload.

The model deliberately uses uppercase env-style field names because
they're the same keys persisted in ``config.json`` and historically
exposed as env vars. Pydantic v2's ``ConfigDict(populate_by_name=True)``
plus uppercase fields keeps the JSON shape exactly as documented.
"""
from __future__ import annotations

import re
import secrets
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GROUPING_OPTIONS = ("none", "daily", "weekly", "monthly", "yearly")
ENCODER_OPTIONS = ("auto", "software", "videotoolbox", "nvenc", "qsv", "vaapi")

# Valid hostname per RFC 1123 (relaxed) or IPv4/IPv6.
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9][a-zA-Z0-9-]{0,62})(\.[a-zA-Z0-9][a-zA-Z0-9-]{0,62})*$")
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_MQTT_NODE_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")


class SettingsModel(BaseModel):
    """Strict-typed view of every persisted setting."""

    model_config = ConfigDict(extra="forbid")

    ADDRESS: str | None = None
    ADDRESS_FALLBACK: str | None = None
    IMPORT_PATH: str = ""
    WEB_PASSWORD_HASH: str = ""
    SESSION_SECRET: str = Field(default_factory=lambda: secrets.token_hex(32))

    GROUPING: Literal["none", "daily", "weekly", "monthly", "yearly"] = "daily"
    HTML: bool = True
    GPS_EXTRACT: bool = True
    DERIVE_THUMBS_EAGER: bool = True
    # Filmstrips are ~10-25x the ffmpeg work of a thumbnail, so eager
    # pre-generation is opt-in; off means they build on demand on first
    # timeline view.
    DERIVE_FILMSTRIPS_EAGER: bool = False
    DELETE_AFTER_DOWNLOAD: bool = False
    TIMEOUT: int = Field(default=10, ge=1, le=60)
    DOWNLOAD_CONCURRENCY: int = Field(default=1, ge=1, le=4)
    DOWNLOAD_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    MAX_DOWNLOAD_ATTEMPTS: int = Field(default=5, ge=1, le=20)
    SYNC_INTERVAL: int = Field(default=600, ge=60, le=86400)
    ENABLE_SCHEDULED_SYNC: bool = True
    SYNC_RO_ONLY: bool = False
    RETENTION_MAX_DAYS: int = Field(default=0, ge=0, le=3650)
    RETENTION_DISK_PCT: int = Field(default=0, ge=0, le=99)
    RETENTION_PROTECT_RO: bool = True
    # When > 0, RETENTION_DISK_PCT is measured against this declared
    # quota (in GiB) instead of the filesystem reported by os.statvfs.
    # Needed when the recordings directory lives inside a quota-bound
    # share (Synology shared folder, ZFS dataset quota, etc.) where the
    # OS-level "free space" doesn't reflect the actual constraint.
    RECORDINGS_QUOTA_GB: int = Field(default=0, ge=0, le=1_048_576)
    # When sync sees disk usage >= this percentage, status flips to
    # "error" with reason "disk N% full". Must be >= RETENTION_DISK_PCT
    # so retention gets a chance to clean before we flag a critical state.
    DISK_CRITICAL_PCT: int = Field(default=95, ge=0, le=100)

    WEB_HOST: str = "0.0.0.0"
    WEB_PORT: int = Field(default=8080, ge=1, le=65535)

    EXPORT_ENCODER: Literal[
        "auto", "software", "videotoolbox", "nvenc", "qsv", "vaapi"
    ] = "auto"
    PIP_POSITION: Literal[
        "top_right", "top_left", "bottom_right", "bottom_left"
    ] = "top_right"
    NOMINATIM_EMAIL: str = ""
    GEOCODE_ENABLED: bool = True
    DISTANCE_UNITS: Literal["km", "miles"] = "km"

    MQTT_ENABLED: bool = False
    MQTT_HOST: str = ""
    MQTT_PORT: int = Field(default=1883, ge=1, le=65535)
    MQTT_USERNAME: str = ""
    MQTT_PASSWORD: str = ""
    MQTT_TLS: bool = False
    MQTT_CLIENT_ID: str = ""
    MQTT_DISCOVERY_PREFIX: str = "homeassistant"
    MQTT_NODE_ID: str = "viofosync"
    MQTT_DISCOVERY_ENABLED: bool = True
    MQTT_QOS: Literal[0, 1, 2] = 1

    @field_validator("IMPORT_PATH")
    @classmethod
    def _validate_import_path(cls, v: str) -> str:
        return v.strip()

    @field_validator("ADDRESS", "ADDRESS_FALLBACK")
    @classmethod
    def _validate_address(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        v = v.strip()
        if _IPV4_RE.match(v) or _HOSTNAME_RE.match(v) or ":" in v:
            return v
        raise ValueError(f"not a valid hostname or IP: {v!r}")

    @field_validator("NOMINATIM_EMAIL")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        if not v:
            return ""
        if "@" not in v or " " in v:
            raise ValueError("not a valid email address")
        return v

    @field_validator("MQTT_HOST")
    @classmethod
    def _validate_mqtt_host(cls, v: str) -> str:
        if not v:
            return ""
        v = v.strip()
        if _IPV4_RE.match(v) or _HOSTNAME_RE.match(v) or ":" in v:
            return v
        raise ValueError(f"not a valid MQTT host: {v!r}")

    @field_validator("MQTT_NODE_ID")
    @classmethod
    def _validate_mqtt_node_id(cls, v: str) -> str:
        if not _MQTT_NODE_ID_RE.match(v):
            raise ValueError(
                "MQTT_NODE_ID must match [a-z0-9_]{1,32}"
            )
        return v

    @field_validator("MQTT_DISCOVERY_PREFIX")
    @classmethod
    def _validate_mqtt_discovery_prefix(cls, v: str) -> str:
        if not v:
            raise ValueError("MQTT_DISCOVERY_PREFIX must not be empty")
        if v.startswith("/") or v.endswith("/"):
            raise ValueError(
                "MQTT_DISCOVERY_PREFIX must not start/end with '/'"
            )
        return v

    @model_validator(mode="after")
    def _validate_mqtt_cross_field(self):
        if self.MQTT_ENABLED and not self.MQTT_HOST:
            raise ValueError("MQTT_HOST is required when MQTT_ENABLED is True")
        return self

    @model_validator(mode="after")
    def _validate_disk_critical(self):
        # Allow 0 (disabled) regardless. Otherwise must be >= retention pct.
        if self.DISK_CRITICAL_PCT != 0 and self.RETENTION_DISK_PCT > self.DISK_CRITICAL_PCT:
            raise ValueError(
                f"DISK_CRITICAL_PCT ({self.DISK_CRITICAL_PCT}) must be "
                f">= RETENTION_DISK_PCT ({self.RETENTION_DISK_PCT})"
            )
        return self


# Public taxonomy used by the API + UI.
EDITABLE_KEYS = {
    "ADDRESS", "ADDRESS_FALLBACK", "IMPORT_PATH", "GROUPING", "HTML", "GPS_EXTRACT",
    "DERIVE_THUMBS_EAGER", "DERIVE_FILMSTRIPS_EAGER",
    "DELETE_AFTER_DOWNLOAD",
    "TIMEOUT", "DOWNLOAD_CONCURRENCY", "DOWNLOAD_ATTEMPTS",
    "MAX_DOWNLOAD_ATTEMPTS", "SYNC_INTERVAL",
    "ENABLE_SCHEDULED_SYNC", "WEB_HOST", "WEB_PORT", "EXPORT_ENCODER",
    "NOMINATIM_EMAIL", "GEOCODE_ENABLED",
    "SYNC_RO_ONLY", "RETENTION_MAX_DAYS", "RETENTION_DISK_PCT",
    "RETENTION_PROTECT_RO", "RECORDINGS_QUOTA_GB", "DISK_CRITICAL_PCT",
    "DISTANCE_UNITS",
    "PIP_POSITION",
    "MQTT_ENABLED", "MQTT_HOST", "MQTT_PORT", "MQTT_USERNAME",
    "MQTT_PASSWORD", "MQTT_TLS", "MQTT_CLIENT_ID",
    "MQTT_DISCOVERY_PREFIX", "MQTT_NODE_ID",
    "MQTT_DISCOVERY_ENABLED", "MQTT_QOS",
}
RESTART_REQUIRED_KEYS = {"WEB_HOST", "WEB_PORT"}
READONLY_KEYS = {"PUID", "PGID", "TZ", "RECORDINGS"}

# Secret editable keys the API never echoes back: GET returns this
# sentinel when a value is set, and a PUT carrying the sentinel is
# treated as "leave unchanged" (see validate_partial).
MASKED_SECRET = "••••••••"  # 8 bullets
MASKED_KEYS = {"MQTT_PASSWORD"}

DEFAULT_VALUES = {
    name: getattr(SettingsModel(), name)
    for name in SettingsModel.model_fields
}


def validate_partial(patch: dict) -> dict:
    """Validate a partial settings update and return a coerced dict.

    Only EDITABLE_KEYS are allowed. Unknown or read-only keys raise.
    Type coercion is delegated to pydantic by constructing a model
    that merges DEFAULT_VALUES with the patch and extracting the
    coerced patch values.
    """
    # Drop masked-secret keys whose value is the sentinel the GET
    # handed out — that means "unchanged", so they must not overwrite
    # the stored secret with the placeholder.
    patch = {
        k: v for k, v in patch.items()
        if not (k in MASKED_KEYS and v == MASKED_SECRET)
    }

    for k in patch:
        if k in READONLY_KEYS:
            raise ValueError(f"{k} is read-only")
        if k not in EDITABLE_KEYS:
            # WEB_PASSWORD is not editable here — use SettingsProvider.set_password()
            raise ValueError(f"unknown setting: {k}")

    merged = {**DEFAULT_VALUES, **patch}
    model = SettingsModel(**merged)
    out: dict[str, Any] = {}
    for k in patch:
        out[k] = getattr(model, k)
    return out


def validate_new_password(pw: str) -> None:
    """Raise if ``pw`` is too short or otherwise unacceptable."""
    if len(pw) < 8:
        raise ValueError("password must be at least 8 characters")
