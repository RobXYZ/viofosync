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

from pydantic import BaseModel, ConfigDict, Field, field_validator

GROUPING_OPTIONS = ("none", "daily", "weekly", "monthly", "yearly")
ENCODER_OPTIONS = ("auto", "software", "videotoolbox", "nvenc", "qsv", "vaapi")

# Valid hostname per RFC 1123 (relaxed) or IPv4/IPv6.
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9][a-zA-Z0-9-]{0,62})(\.[a-zA-Z0-9][a-zA-Z0-9-]{0,62})*$")
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


class SettingsModel(BaseModel):
    """Strict-typed view of every persisted setting."""

    model_config = ConfigDict(extra="forbid")

    ADDRESS: str | None = None
    WEB_PASSWORD_HASH: str = ""
    SESSION_SECRET: str = Field(default_factory=lambda: secrets.token_hex(32))

    GROUPING: Literal["none", "daily", "weekly", "monthly", "yearly"] = "daily"
    HTML: bool = True
    GPS_EXTRACT: bool = True
    DELETE_AFTER_DOWNLOAD: bool = False
    TIMEOUT: int = Field(default=10, ge=1, le=60)
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

    @field_validator("ADDRESS")
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


# Public taxonomy used by the API + UI.
EDITABLE_KEYS = {
    "ADDRESS", "GROUPING", "HTML", "GPS_EXTRACT", "DELETE_AFTER_DOWNLOAD",
    "TIMEOUT", "DOWNLOAD_ATTEMPTS", "MAX_DOWNLOAD_ATTEMPTS", "SYNC_INTERVAL",
    "ENABLE_SCHEDULED_SYNC", "WEB_HOST", "WEB_PORT", "EXPORT_ENCODER",
    "NOMINATIM_EMAIL", "GEOCODE_ENABLED",
    "SYNC_RO_ONLY", "RETENTION_MAX_DAYS", "RETENTION_DISK_PCT",
    "RETENTION_PROTECT_RO", "RECORDINGS_QUOTA_GB",
    "DISTANCE_UNITS",
    "PIP_POSITION",
}
RESTART_REQUIRED_KEYS = {"WEB_HOST", "WEB_PORT"}
READONLY_KEYS = {"PUID", "PGID", "TZ", "RECORDINGS"}

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
