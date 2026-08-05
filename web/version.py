"""Single source of truth for the app version.

CI-built images carry VIOFOSYNC_VERSION (e.g. "2.6.1" from a release
tag, or "edge" for main builds) and VIOFOSYNC_REVISION (the git sha),
both baked in as Dockerfile build args. Anything else — local source
runs, plain `docker build` — reports "dev".
"""
from __future__ import annotations

import os


def raw_version() -> str:
    return os.getenv("VIOFOSYNC_VERSION", "").strip()


def display_version() -> str:
    v = raw_version()
    if not v or v == "dev":
        return "dev"
    if v[0].isdigit():
        return f"v{v}"
    # Non-release builds ("edge" from main, or a branch slug from a
    # manual dispatch): the name alone is ambiguous, add the short sha.
    rev = os.getenv("VIOFOSYNC_REVISION", "").strip()
    return f"{v} ({rev[:7]})" if rev else v
