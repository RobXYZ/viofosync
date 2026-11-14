"""The THEME setting picks dark (the default), light or system (follow
the OS).

Both served pages (the SPA's index.html and the first-run /setup page)
carry the light palette as a stylesheet whose ``media`` attribute the
server sets from THEME, so the browser applies the right palette before
first paint: "all" for light, "not all" for dark, and the
prefers-color-scheme query for system.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"

SYSTEM_MEDIA = "(prefers-color-scheme: light)"


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
    c.headers["X-CSRF-Token"] = c.get("/api/auth/csrf").json()["csrf"]


def _light_sheet_media(html: str) -> str:
    tags = re.findall(r'<link\b[^>]*\bid="theme-light"[^>]*>', html)
    assert len(tags) == 1, f"expected one light-theme stylesheet, found {tags!r}"
    tag = tags[0]
    assert re.search(r'\bhref="/static/light\.css(\?v=\d+)?"', tag), tag
    m = re.search(r'\bmedia="([^"]*)"', tag)
    assert m, f"light-theme stylesheet has no media attribute: {tag}"
    return m.group(1)


def test_index_is_dark_by_default(
    tmp_config_dir: Path, tmp_recordings_dir: Path,
) -> None:
    with _make_client() as c:
        _complete_setup(c)
        html = c.get("/").text
    assert _light_sheet_media(html) == "not all"


@pytest.mark.parametrize(("theme", "media"), [
    ("light", "all"),
    ("dark", "not all"),
    ("system", SYSTEM_MEDIA),
])
def test_index_applies_the_chosen_theme(
    tmp_config_dir: Path, tmp_recordings_dir: Path, theme: str, media: str,
) -> None:
    with _make_client() as c:
        _complete_setup(c)
        c.app.state.settings_provider.update({"THEME": theme}, actor="test")
        html = c.get("/").text
    assert _light_sheet_media(html) == media


def test_index_cache_busts_the_light_sheet(
    tmp_config_dir: Path, tmp_recordings_dir: Path,
) -> None:
    with _make_client() as c:
        _complete_setup(c)
        html = c.get("/").text
    assert re.search(r'/static/light\.css\?v=\d+', html)


def test_setup_page_is_dark_by_default(
    tmp_config_dir: Path, tmp_recordings_dir: Path,
) -> None:
    with _make_client() as c:
        html = c.get("/setup").text
    assert _light_sheet_media(html) == "not all"


def test_setup_page_honours_a_theme_set_in_config_json(
    tmp_config_dir: Path, tmp_recordings_dir: Path,
) -> None:
    (tmp_config_dir / "config.json").write_text(json.dumps({"THEME": "system"}))
    with _make_client() as c:
        html = c.get("/setup").text
    assert _light_sheet_media(html) == SYSTEM_MEDIA


def test_theme_is_saved_through_the_settings_api(
    tmp_config_dir: Path, tmp_recordings_dir: Path,
) -> None:
    with _make_client() as c:
        _complete_setup(c)
        assert c.get("/api/settings").json()["editable"]["THEME"] == "dark"
        r = c.put("/api/settings", json={"THEME": "light"})
        assert r.status_code == 200, r.text
        assert c.get("/api/settings").json()["editable"]["THEME"] == "light"
    on_disk = json.loads((tmp_config_dir / "config.json").read_text())
    assert on_disk["THEME"] == "light"


def test_unknown_theme_is_rejected(
    tmp_config_dir: Path, tmp_recordings_dir: Path,
) -> None:
    with _make_client() as c:
        _complete_setup(c)
        r = c.put("/api/settings", json={"THEME": "sepia"})
        assert r.status_code == 400
        assert c.get("/api/settings").json()["editable"]["THEME"] == "dark"


# Tokens that aren't colours, so the light palette has no reason to
# redefine them.
NON_COLOUR_TOKENS = {"--ease", "--t"}


def _root_tokens(css: str) -> set[str]:
    """Custom properties declared in the stylesheet's first :root block."""
    m = re.search(r":root\s*\{(.*?)\n\}", css, re.S)
    assert m, "no :root block"
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return set(re.findall(r"(--[\w-]+)\s*:", body))


@pytest.mark.parametrize("sheet", ["styles.css", "setup.css"])
def test_light_palette_redefines_every_colour_token(sheet: str) -> None:
    """A colour token missing from light.css would keep its dark value
    in the light theme."""
    dark = _root_tokens((STATIC / sheet).read_text("utf-8")) - NON_COLOUR_TOKENS
    light = _root_tokens((STATIC / "light.css").read_text("utf-8"))
    assert dark, f"{sheet} declares no colour tokens"
    assert sorted(dark - light) == []
