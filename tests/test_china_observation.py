"""Offline tests for shared China OSINT observation richness."""

from __future__ import annotations

from datetime import datetime, timezone

from core.china_observation import (
    archive_lookup,
    bilingual_fields,
    content_sha256,
    enrich_observation,
    gazetteer_hits,
    iso_z,
    serialize_observation,
    situation_osint_row,
)


def test_iso_z_normalises_offset_timestamps():
    assert iso_z("2026-08-01T12:00:00+00:00") == "2026-08-01T12:00:00Z"
    assert iso_z(datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)) == "2026-08-01T12:00:00Z"
    assert iso_z("not-a-date") is None


def test_content_hash_is_stable_and_separated():
    assert content_sha256("a", "b") == content_sha256("a", "b")
    assert content_sha256("ab", "c") != content_sha256("a", "bc")


def test_bilingual_fields_do_not_invent_a_translation():
    fields = bilingual_fields("白纸运动 White Paper", "A public English gloss.")
    assert "白纸运动" in fields["text_zh"]
    assert "White Paper" in fields["text_en"] or "English" in fields["text_en"]


def test_gazetteer_hits_are_lexical_over_the_human_lexicon():
    hits = gazetteer_hits("报道提到白纸革命与六四")
    terms = {hit["zh"] for hit in hits}
    assert "白纸革命" in terms
    assert "六四" in terms
    assert all(hit.get("en") for hit in hits if hit["zh"] == "六四")


def test_archive_lookup_is_an_address_not_a_claimed_capture():
    row = archive_lookup("https://www.gov.cn/")
    assert row["wayback_lookup"].startswith("https://web.archive.org/web/*/")
    assert row["archive_today_lookup"].startswith("https://archive.today/")
    assert row["wayback_snapshot"] is None


def test_enrich_observation_is_additive_and_honest():
    detected = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    row = enrich_observation(
        {
            "terms": ["白纸革命"],
            "detected_at": detected,
            "title": "白纸革命",
            "url": "https://chinadigitaltimes.net/example/",
            "source": "cdt_404",
        },
        text="白纸革命 public excerpt",
        first_seen=detected,
        last_seen=detected,
        cdt={"id": "cdt_404", "url": "https://chinadigitaltimes.net/example/"},
    )
    assert row["text"]
    assert row["content_sha256"]
    assert row["first_seen"] == "2026-08-01T12:00:00Z"
    assert row["last_seen"] == "2026-08-01T12:00:00Z"
    assert row["archive"]["wayback_lookup"]
    assert row["cross_links"]["cdt"]["id"] == "cdt_404"
    assert row["cross_links"]["gdelt"] is None
    assert any(hit["zh"] == "白纸革命" for hit in row["gazetteer_hits"])
    card = situation_osint_row(row)
    assert card["relation"] == "topic-or-url-context-not-corroboration"
    assert card["content_sha256"] == row["content_sha256"]
    serialized = serialize_observation(row)
    assert serialized["detected_at"] == "2026-08-01T12:00:00Z"
