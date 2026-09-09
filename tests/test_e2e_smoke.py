"""End-to-end: empty /config -> wizard -> settings PUT -> verify."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_full_e2e_flow(tmp_config_dir: Path, tmp_recordings_dir: Path) -> None:
    from web import app as app_mod
    from web import settings as settings_mod
    settings_mod.reset_for_tests()
    app = app_mod.create_app()

    with TestClient(app) as c:
        # 1. Empty config -> redirect to /setup.
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"].endswith("/setup")

        # 2. Submit wizard.
        r = c.post("/setup", data={
            "address": "192.168.1.230",
            "password": "twelve-chars-min!",
            "confirm": "twelve-chars-min!",
        }, follow_redirects=False)
        assert r.status_code == 303

        # 3. /setup is now 404.
        assert c.get("/setup").status_code == 404

        # 4. Acquire CSRF, change a setting.
        # NOTE: the auth router returns {"csrf": "..."}, not {"token": "..."}.
        csrf = c.get("/api/auth/csrf").json()["csrf"]
        r = c.put("/api/settings",
                  json={"TIMEOUT": 25, "GROUPING": "weekly"},
                  headers={"x-csrf-token": csrf})
        assert r.status_code == 200
        assert r.json()["editable"]["TIMEOUT"] == 25

        # 5. Round-trip: GET shows the persisted value.
        r = c.get("/api/settings")
        assert r.json()["editable"]["GROUPING"] == "weekly"

        # 6. Wrong-current rejects password change.
        r = c.post("/api/settings/password",
                   json={"current": "wrong", "new_password": "x" * 14, "logout_others": False},
                   headers={"x-csrf-token": csrf})
        assert r.status_code == 401

        # 7. Correct-current succeeds.
        r = c.post("/api/settings/password",
                   json={"current": "twelve-chars-min!", "new_password": "new-twelve-chars!", "logout_others": False},
                   headers={"x-csrf-token": csrf})
        assert r.status_code == 200

    # Round-trip across a fresh app instance to confirm persistence.
    settings_mod.reset_for_tests()
    app2 = app_mod.create_app()
    with TestClient(app2) as c2:
        r = c2.get("/", follow_redirects=False)
        # No longer in setup mode.
        assert r.status_code != 307


def test_e2e_retention_settings_roundtrip(
    tmp_config_dir: Path, tmp_recordings_dir: Path,
) -> None:
    from web import app as app_mod
    from web import settings as settings_mod
    settings_mod.reset_for_tests()
    app = app_mod.create_app()

    with TestClient(app) as c:
        # Bootstrap through the wizard so we leave setup mode.
        c.post("/setup", data={
            "address": "192.168.1.230",
            "password": "twelve-chars-min!",
            "confirm": "twelve-chars-min!",
        }, follow_redirects=False)
        csrf = c.get("/api/auth/csrf").json()["csrf"]

        r = c.put(
            "/api/settings",
            json={
                "ALTERNATIVE_SCOPE": "ro_only",
                "RETENTION_MAX_DAYS": 14,
                "RETENTION_DISK_PCT": 75,
                "RETENTION_PROTECT_RO": True,
            },
            headers={"x-csrf-token": csrf},
        )
        assert r.status_code == 200, r.text
        e = r.json()["editable"]
        assert e["ALTERNATIVE_SCOPE"] == "ro_only"
        assert e["RETENTION_MAX_DAYS"] == 14
        assert e["RETENTION_DISK_PCT"] == 75
        assert e["RETENTION_PROTECT_RO"] is True

        # GET re-reads from the provider.
        body = c.get("/api/settings").json()
        e2 = body["editable"]
        assert e2["RETENTION_MAX_DAYS"] == 14
        assert e2["ALTERNATIVE_SCOPE"] == "ro_only"
