"""A custom INSTANCE_NAME is injected — HTML-escaped — into the served
index.html (title, login subtitle, header badge). The default name
leaves the page exactly as shipped: no label anywhere."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def _make_client() -> TestClient:
    from web import app as app_mod
    from web import settings as settings_mod
    settings_mod.reset_for_tests()
    return TestClient(app_mod.create_app())


def _complete_setup(c: TestClient) -> None:
    c.post("/setup", data={
        "address": "192.168.1.230",
        "password": "twelve-chars-min!",
        "confirm": "twelve-chars-min!",
    }, follow_redirects=False)


def test_default_name_shows_no_label(
    tmp_config_dir: Path, tmp_recordings_dir: Path,
) -> None:
    with _make_client() as c:
        _complete_setup(c)
        html = c.get("/").text
    assert "<title>Viofosync</title>" in html
    assert '<p id="instance-login" class="instance-label" hidden></p>' in html
    assert '<span id="instance-badge" class="instance-badge" hidden></span>' in html


def test_custom_name_is_injected_escaped(
    tmp_config_dir: Path, tmp_recordings_dir: Path,
) -> None:
    with _make_client() as c:
        _complete_setup(c)
        c.app.state.settings_provider.update(
            {"INSTANCE_NAME": "Car <A> & Co"}, actor="test",
        )
        html = c.get("/").text
    esc = "Car &lt;A&gt; &amp; Co"
    assert f"<title>{esc} — Viofosync</title>" in html
    assert f'<p id="instance-login" class="instance-label">{esc}</p>' in html
    assert f'<span id="instance-badge" class="instance-badge">{esc}</span>' in html
    # Raw (unescaped) name must never appear in the markup.
    assert "Car <A> & Co" not in html


def test_default_name_case_insensitive(
    tmp_config_dir: Path, tmp_recordings_dir: Path,
) -> None:
    with _make_client() as c:
        _complete_setup(c)
        c.app.state.settings_provider.update(
            {"INSTANCE_NAME": "ViofoSync"}, actor="test",
        )
        html = c.get("/").text
    assert "<title>Viofosync</title>" in html
    assert 'id="instance-badge" class="instance-badge" hidden' in html
