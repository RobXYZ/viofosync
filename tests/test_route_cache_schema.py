"""The route cache persists across restarts and its signature only
covers the GPX file set — so a change to the aggregation *algorithm*
(like the GPX clock detection) would keep serving stale journeys for
every historical day forever. The signature must therefore embed a
schema version that gets bumped whenever aggregation semantics change.
"""
from __future__ import annotations

from web.services import route_cache


def test_signature_embeds_schema_version(tmp_path, monkeypatch):
    p = tmp_path / "2026_0618_080000_0001F.MP4.gpx"
    p.write_text("<gpx></gpx>")

    s1 = route_cache.signature([str(p)])
    monkeypatch.setattr(route_cache, "_SCHEMA", "test-bump")
    s2 = route_cache.signature([str(p)])

    assert s1 != s2, "schema version must participate in the signature"


def test_old_cache_entry_rejected_after_schema_bump(tmp_path, monkeypatch):
    """An entry stored under a previous schema must miss on load even
    though the GPX files are byte-for-byte unchanged."""
    rec = tmp_path / "rec"
    rec.mkdir()
    p = tmp_path / "2026_0618_080000_0001F.MP4.gpx"
    p.write_text("<gpx></gpx>")

    monkeypatch.setattr(route_cache, "_SCHEMA", "old")
    old_sig = route_cache.signature([str(p)])
    route_cache.store(str(rec), "2026-06-18", old_sig, {"points": []})
    assert route_cache.load(str(rec), "2026-06-18", old_sig) is not None

    monkeypatch.setattr(route_cache, "_SCHEMA", "new")
    new_sig = route_cache.signature([str(p)])
    assert route_cache.load(str(rec), "2026-06-18", new_sig) is None
