"""The web UI's colour theme, applied before first paint.

The dark palette is the default in styles.css (and setup.css). The light
palette lives in light.css, which each served page links as
``<link id="theme-light" ... media="...">``. The server sets that
``media`` attribute from the THEME setting, so the browser picks the
palette while it parses the head and no page flashes the wrong one:

- light: ``all``, so the light palette always applies;
- dark: ``not all``, so it never does;
- system: the ``prefers-color-scheme`` query, so the browser follows the
  OS and switches live when the OS does.

Mirrored client-side by ``applyTheme`` in web/static/app.js, which swaps
the attribute when THEME is saved: keep the two mappings in sync.
"""
from __future__ import annotations

import re

THEME_MEDIA = {
    "system": "(prefers-color-scheme: light)",
    "light": "all",
    "dark": "not all",
}

_LIGHT_LINK_MEDIA = re.compile(r'(<link\b[^>]*\bid="theme-light"[^>]*\bmedia=")[^"]*(")')


def apply_theme(html: str, theme: str) -> str:
    """Return ``html`` with the light stylesheet's media set for ``theme``."""
    media = THEME_MEDIA.get(theme, THEME_MEDIA["dark"])
    return _LIGHT_LINK_MEDIA.sub(lambda m: m.group(1) + media + m.group(2), html, count=1)
