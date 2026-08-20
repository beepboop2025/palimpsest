"""Honest China cross-signal joins: attach only committed related records."""

from __future__ import annotations

from core.china_joins import (
    attach_common_crawl_join,
    attach_joins,
    cluster_by_url,
    gdelt_index,
    instrument_bleedthrough,
    instrument_ooni,
    merge_observations,
    weibo_index,
)
from core.china_observation import enrich_observation


def test_instrument_joins_are_labeled_not_url_corroboration():
    ooni = instrument_ooni({"generated_at": "2026-08-20T01:55:29Z", "gfw_index": 59.3})
    bleed = instrument_bleedthrough({
        "generated_at": "2026-08-17T00:57:15Z",
        "distinct_pools": 6,
        "events": [{"kind": "pool_rotation"}],
    })
    assert ooni is not None
    assert "instrument-context-not-url-corroboration" in ooni["note"]
    assert bleed is not None
    assert "instrument-context-not-url-corroboration" in bleed["note"]
    assert instrument_ooni({}) is None
    assert instrument_bleedthrough({}) is None


def test_gdelt_and_weibo_index_only_committed_terms():
    gdelt = gdelt_index({
        "generated_at": "2026-08-20T02:12:09Z",
        "ranked": [{"term": "subway", "label": "containment", "global_norm": 0.1}],
    })
    weibo = weibo_index({
        "generated_at": "2026-08-20T02:06:13Z",
        "gazetteer_breakthroughs": [{
            "term": "天安门",
            "samples": [{"title": "天安门下半旗悼念朱镕基同志"}],
        }],
    })
    assert "subway" in gdelt
    assert "invented" not in gdelt
    assert "天安门" in weibo


def test_attach_joins_fills_cdt_from_public_url_and_keeps_nulls():
    row = enrich_observation({
        "terms": ["subway"],
        "title": "public CDT title",
        "url": "https://chinadigitaltimes.net/2026/08/example/",
        "source": "undertext:fusion:ddti",
    }, text="public CDT title")
    joined = attach_joins(
        row,
        gdelt=gdelt_index({"ranked": [{"term": "subway", "label": "unknown"}]}),
        ooni=instrument_ooni({"generated_at": "2026-08-20T01:55:29Z", "gfw_index": 59.3}),
    )
    assert joined["cross_links"]["cdt"]["url"].startswith("https://chinadigitaltimes.net/")
    assert joined["cross_links"]["gdelt"]["id"] == "gdelt:subway"
    assert joined["cross_links"]["weibo"] is None
    assert joined["cross_links"]["common_crawl"] is None
    assert "instrument-context-not-url-corroboration" in joined["cross_links"]["ooni"]["note"]


def test_cluster_by_url_merges_skinny_wrappers_into_one_fat_record():
    left = enrich_observation({
        "terms": ["subway"],
        "title": "Translation: Extreme Security",
        "url": "https://chinadigitaltimes.net/2026/08/same/",
        "source": "undertext:fusion:ddti",
    }, text="subway")
    right = enrich_observation({
        "terms": ["Tiananmen"],
        "title": "Translation: Extreme Security",
        "url": "https://chinadigitaltimes.net/2026/08/same/",
        "source": "undertext:fusion:ddti",
    }, text="Tiananmen")
    clustered = cluster_by_url([left, right])
    assert len(clustered) == 1
    assert set(clustered[0]["terms"]) == {"subway", "Tiananmen"}
    merged = merge_observations(left, right)
    assert "subway" in merged["text"] or "Tiananmen" in merged["text"]


def test_attach_common_crawl_join_from_receipt_stays_sanitized():
    row = enrich_observation({
        "title": "NBS release",
        "url": "https://www.stats.gov.cn/sj/zxfb/",
        "source": "undertext:fusion:wayback",
    }, text="NBS release")
    from core.china_observation import observation_key

    receipt = {
        "kind": "common-crawl-china-observation-joins",
        "status": "ok",
        "matches": [{
            "observation_key": observation_key(row),
            "url_sha256": "a" * 64,
            "match_kind": "url",
            "target_id": "nbs",
            "host": "www.stats.gov.cn",
            "crawl": "CC-MAIN-2026-30",
            "capture_at": "2026-07-24T12:30:00Z",
            "mime_type": "text/html",
            "languages": "zho",
            "content_digest": "A" * 32,
            "locator_sha256": "b" * 64,
            "relation": "archive-coverage-not-deletion",
            "uncertainty": "Common Crawl capture on the node lake.",
        }],
    }
    joined = attach_common_crawl_join(row, receipt=receipt)
    assert joined["common_crawl"]["match_kind"] == "url"
    assert joined["common_crawl"]["host"] == "www.stats.gov.cn"
    assert "canonical_url" not in joined["common_crawl"]
    assert joined["cross_links"]["common_crawl"]["url"] is None
    assert "archive coverage" in " ".join(joined["uncertainty"]).lower() or any(
        "Common Crawl" in note for note in joined["uncertainty"]
    )


def test_attach_common_crawl_join_abstains_on_no_data_receipt():
    row = enrich_observation({
        "title": "NBS release",
        "url": "https://www.stats.gov.cn/sj/zxfb/",
        "source": "undertext:fusion:wayback",
    }, text="NBS release")
    joined = attach_common_crawl_join(row, receipt={"status": "no_data", "matches": []})
    assert joined["common_crawl"] is None
    assert joined["cross_links"]["common_crawl"] is None
