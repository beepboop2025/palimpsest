"""Contract, parser, evidence, mutation, and atomic-publication tests for newswire v1."""

from __future__ import annotations

import copy
import html
import json
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core.newswire as nw
from core.newswire import (
    FeedParseError,
    NoSuccessfulSources,
    NewswireError,
    RegistryError,
    SourceRegistry,
    SourceSpec,
    canonicalize_article_url,
    collect_newswire,
    load_source_registry,
    parse_feed,
    strict_json_loads,
    validate_prior_newswire_document,
    validate_newswire_document,
)


ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)
NOW_RFC = "Tue, 11 Aug 2026 10:00:00 +0000"


def _source(source_id: str) -> SourceSpec:
    registry = load_source_registry()
    return next(source for source in registry.sources if source.id == source_id)


def _rss(
    source: SourceSpec,
    *,
    title: str = "Network policy measurement published",
    url: str | None = None,
    excerpt: str = "A bounded <b>feed summary</b> with reported context.",
    published: str = NOW_RFC,
    extra: str = "",
) -> bytes:
    url = url or f"https://{source.article_hosts[0]}/news/example"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><item>'
        f"<title>{html.escape(title)}</title>"
        f"<link>{html.escape(url)}</link>"
        f"<description>{html.escape(excerpt)}</description>"
        f"<pubDate>{html.escape(published)}</pubDate>"
        f"{extra}</item></channel></rss>"
    ).encode()


def _atom(
    source: SourceSpec,
    *,
    title: str = "Network policy measurement published",
    url: str | None = None,
    summary: str = "A short Atom summary.",
    published: str = "2026-08-11T10:00:00+00:00",
) -> bytes:
    url = url or f"https://{source.article_hosts[0]}/research/example"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<title>Research feed</title><entry>'
        f"<title>{html.escape(title)}</title>"
        '<link rel="self" href="https://github.com/self/feed"/>'
        f'<link rel="alternate" href="{html.escape(url, quote=True)}"/>'
        f"<summary>{html.escape(summary)}</summary>"
        f"<updated>{html.escape(published)}</updated>"
        '<category term="measurement"/><category term="security"/>'
        '</entry></feed>'
    ).encode()


def _registry(*sources: SourceSpec, max_items: int = 128) -> SourceRegistry:
    return SourceRegistry(
        schema_version=nw.REGISTRY_SCHEMA_VERSION,
        window_hours=168,
        max_items_per_source=max_items,
        max_events=max(2048, max_items),
        sources=tuple(sorted(sources, key=lambda source: source.id)),
        sha256="0" * 64,
    )


def _fetch_map(mapping):
    def fetch(url, **_kwargs):
        value = mapping[url]
        if isinstance(value, Exception):
            raise value
        return value

    return fetch


def _all_live_mapping(registry: SourceRegistry, *, title_prefix: str = "Source update"):
    return {
        source.feed_url: _rss(source, title=f"{title_prefix}: {source.id}")
        for source in registry.sources
    }


def test_closed_registry_contains_only_the_exact_reviewed_v1_sources():
    registry = load_source_registry()

    assert len(registry.sources) == 23
    assert {source.id for source in registry.sources} == set(nw._CLOSED_SOURCES)
    assert all(source.feed_url.startswith("https://") for source in registry.sources)
    assert all(source.rights_policy == "metadata-link-only" for source in registry.sources)
    secondary_ids = {
        "bbc-chinese",
        "hong-kong-free-press",
        "scmp-china",
        "scmp-china-economy",
        "scmp-china-tech",
        "voa-chinese",
    }
    assert all(_source(source_id).role == "media" for source_id in secondary_ids)
    assert all(_source(source_id).rights_policy == "metadata-link-only" for source_id in secondary_ids)
    assert _source("github-government-takedowns").feed_url.endswith("/commits/master.atom")
    assert {
        _source("scmp-china").independence_group,
        _source("scmp-china-economy").independence_group,
        _source("scmp-china-tech").independence_group,
    } == {"south-china-morning-post-editorial"}
    assert registry.max_events >= registry.max_items_per_source * len(registry.sources)
    assert {
        _source("bbc-chinese").feed_url,
        _source("hong-kong-free-press").feed_url,
        _source("scmp-china").feed_url,
        _source("scmp-china-economy").feed_url,
        _source("scmp-china-tech").feed_url,
        _source("voa-chinese").feed_url,
    } == {
        "https://feeds.bbci.co.uk/zhongwen/trad/rss.xml",
        "https://hongkongfp.com/feed/",
        "https://www.scmp.com/rss/4/feed/",
        "https://www.scmp.com/rss/318421/feed/",
        "https://www.scmp.com/rss/320663/feed/",
        "https://www.voachinese.com/api/zm_yql-vomx-tpeybti",
    }


def test_registry_rejects_duplicate_json_keys_and_nonfinite_numbers():
    with pytest.raises(RegistryError, match="duplicate JSON key"):
        strict_json_loads('{"schema_version":"x","schema_version":"y"}')
    with pytest.raises(RegistryError, match="non-finite"):
        strict_json_loads('{"window_hours":NaN}')


@pytest.mark.parametrize("field", ["feed_url", "independence_group", "role", "article_hosts"])
def test_registry_rejects_endpoint_or_evidence_boundary_broadening(tmp_path: Path, field: str):
    data = json.loads((ROOT / "config" / "news_sources.json").read_text())
    row = data["sources"][0]
    replacements = {
        "feed_url": "https://chinadigitaltimes.net/another-feed/",
        "independence_group": "inflated-independent-copy",
        "role": "primary",
        "article_hosts": ["chinadigitaltimes.net", "example.com"],
    }
    row[field] = replacements[field]
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(RegistryError):
        load_source_registry(path)


def test_registry_rejects_an_incomplete_or_extra_source_set(tmp_path: Path):
    data = json.loads((ROOT / "config" / "news_sources.json").read_text())
    data["sources"].pop()
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(RegistryError, match="incomplete"):
        load_source_registry(path)


def test_rss_parser_retains_only_bounded_plain_metadata():
    source = _source("china-digital-times")
    raw = _rss(
        source,
        title="A reported policy change",
        excerpt="Summary with <strong>visible detail</strong> and &amp; context.",
        url="https://chinadigitaltimes.net/story?utm_source=x&b=2&a=1#fragment",
    )

    parsed = parse_feed(source, raw, now=NOW)
    item = parsed.items[0]

    assert parsed.items_seen == 1 and parsed.rejected_items == 0
    assert item["title"] == "A reported policy change"
    assert item["excerpt"] == "Summary with visible detail and & context."
    assert item["url"] == "https://chinadigitaltimes.net/story?a=1&b=2"
    assert item["published_at"] == "2026-08-11T10:00:00Z"
    assert item["rights_policy"] == "metadata-link-only"
    assert len(item["feed_sha256"]) == 64


def test_atom_parser_prefers_alternate_link_and_reads_categories():
    source = _source("github-government-takedowns")
    parsed = parse_feed(source, _atom(source), now=NOW)
    item = parsed.items[0]

    assert item["url"] == "https://github.com/research/example"
    assert item["excerpt"] == "A short Atom summary."
    assert {"measurement", "security"}.issubset(item["topics"])


def test_article_body_elements_are_not_used_as_the_excerpt():
    source = _source("china-digital-times")
    raw = (
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        '<channel><item><title>Metadata title</title>'
        '<link>https://chinadigitaltimes.net/story</link>'
        '<content:encoded>FULL ARTICLE BODY MUST NOT BE COPIED</content:encoded>'
        f"<pubDate>{NOW_RFC}</pubDate></item></channel></rss>"
    ).encode()

    item = parse_feed(source, raw, now=NOW).items[0]
    assert item["excerpt"] == ""
    assert "FULL ARTICLE" not in json.dumps(item)


@pytest.mark.parametrize(
    "raw,match",
    [
        (b"<html><body>challenge</body></html>", "interstitial"),
        (b"<root><item/></root>", "root"),
        (b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel/></rss>', "DOCTYPE"),
        (b"<rss><channel>", "well-formed"),
    ],
)
def test_parser_rejects_interstitial_ambiguous_entity_or_malformed_documents(raw: bytes, match: str):
    with pytest.raises(FeedParseError, match=match):
        parse_feed(_source("ooni"), raw, now=NOW)


def test_parser_rejects_oversize_documents_and_entry_counts():
    source = _source("ooni")
    with pytest.raises(FeedParseError, match="document cap"):
        parse_feed(source, b"<rss>" + b"x" * nw.MAX_FEED_BYTES + b"</rss>", now=NOW)

    entries = "".join(
        f"<item><title>T{i}</title><link>https://ooni.org/{i}</link><pubDate>{NOW_RFC}</pubDate></item>"
        for i in range(nw.MAX_FEED_ENTRIES + 1)
    )
    raw = f"<rss><channel>{entries}</channel></rss>".encode()
    with pytest.raises(FeedParseError, match="entry-count cap"):
        parse_feed(source, raw, now=NOW)


def test_parser_requires_utf8_rejects_oversize_titles_and_truncates_only_the_excerpt():
    source = _source("ooni")
    with pytest.raises(FeedParseError, match="strict UTF-8"):
        parse_feed(source, _rss(source).decode().encode("utf-16"), now=NOW)

    oversize_title = parse_feed(source, _rss(source, title="T" * (nw.MAX_TITLE_CHARS + 1)), now=NOW)
    assert oversize_title.items == () and oversize_title.rejected_items == 1

    bounded_excerpt = parse_feed(source, _rss(source, excerpt="x" * 1000), now=NOW).items[0]
    assert bounded_excerpt["excerpt"] == "x" * nw.MAX_EXCERPT_CHARS


def test_article_url_query_field_bomb_is_an_item_rejection():
    source = _source("ooni")
    query = "&".join(f"k{index}=v" for index in range(65))
    with pytest.raises(FeedParseError, match="query"):
        canonicalize_article_url(f"https://ooni.org/story?{query}", source)
    parsed = parse_feed(source, _rss(source, url=f"https://ooni.org/story?{query}"), now=NOW)
    assert parsed.items == () and parsed.rejected_items == 1


@pytest.mark.parametrize(
    "title,published",
    [
        ("Unsafe \u202e title", NOW_RFC),
        ("Timestamp without zone", "Tue, 11 Aug 2026 10:00:00"),
        ("Future timestamp", "Wed, 12 Aug 2026 10:00:00 +0000"),
        ("Invalid timestamp", "definitely-not-a-date"),
    ],
)
def test_invalid_future_timezone_free_or_bidi_entries_are_counted_and_rejected(title, published):
    parsed = parse_feed(_source("ooni"), _rss(_source("ooni"), title=title, published=published), now=NOW)
    assert parsed.items_seen == 1
    assert parsed.rejected_items == 1
    assert parsed.items == ()


def test_one_bad_entry_does_not_erase_a_valid_entry_but_is_accounted():
    source = _source("ooni")
    valid = _rss(source).decode().split("<item>", 1)[1].split("</item>", 1)[0]
    invalid = (
        "<title>Missing link</title>"
        f"<pubDate>{NOW_RFC}</pubDate><description>still invalid</description>"
    )
    raw = f"<rss><channel><item>{valid}</item><item>{invalid}</item></channel></rss>".encode()

    parsed = parse_feed(source, raw, now=NOW)
    assert len(parsed.items) == 1
    assert parsed.items_seen == 2
    assert parsed.rejected_items == 1


def test_article_url_rejects_credentials_self_recursion_private_hosts_and_wrong_hosts():
    source = _source("ooni")
    with pytest.raises(FeedParseError):
        credential_url = "https://user:secret" + chr(64) + "ooni.org/x"
        canonicalize_article_url(credential_url, source)
    with pytest.raises(FeedParseError):
        canonicalize_article_url("https://example.com/x", source)

    self_source = replace(source, article_hosts=("palimpsest.info",))
    with pytest.raises(FeedParseError, match="allowlist"):
        canonicalize_article_url("https://palimpsest.info/readings/newswire-latest.json", self_source)

    private_source = replace(source, article_hosts=("127.0.0.1",))
    with pytest.raises(FeedParseError, match="non-public"):
        canonicalize_article_url("https://127.0.0.1/admin", private_source)


def test_default_desk_is_stable_and_economic_links_activate_only_for_economic_items():
    ooni = _source("ooni")
    generic = parse_feed(ooni, _rss(ooni, title="A new research release"), now=NOW).items[0]
    assert generic["desk"] == "connectivity"

    rfa = _source("rfa-mandarin")
    political = parse_feed(rfa, _rss(rfa, title="政府公布一项新的法律政策"), now=NOW).items[0]
    economic = parse_feed(rfa, _rss(rfa, title="中国经济贸易与人民币市场出现新变化"), now=NOW).items[0]
    assert political["declared_economic_ids"] == []
    assert economic["desk"] == "economy"
    assert economic["declared_economic_ids"]


def test_keyword_boundaries_and_materiality_prevent_false_economic_promotion():
    scmp = _source("scmp-china")
    shiyuan = parse_feed(
        scmp,
        _rss(
            scmp,
            title="Chinese paratrooper killed in training exercise",
            excerpt="State media identified the soldier as Xia Shiyuan.",
        ),
        now=NOW,
    ).items[0]
    assert shiyuan["desk"] == "politics"
    assert shiyuan["declared_economic_ids"] == []

    voa = _source("voa-chinese")
    arms_export = parse_feed(
        voa,
        _rss(
            voa,
            title="一名试图获取美国敏感军事设备的中国公民认罪",
            excerpt="案件涉及违反武器出口管制法。",
        ),
        now=NOW,
    ).items[0]
    assert arms_export["desk"] == "politics"
    assert arms_export["declared_economic_ids"] == []


def test_curated_economy_desk_and_specific_official_releases_remain_material():
    scmp_economy = _source("scmp-china-economy")
    automotive = parse_feed(
        scmp_economy,
        _rss(scmp_economy, title="Nio registrations fall 93.6% in Germany"),
        now=NOW,
    ).items[0]
    assert automotive["desk"] == "economy"
    assert automotive["declared_economic_ids"]

    hksar = _source("hksar-releases")
    release = parse_feed(
        hksar,
        _rss(hksar, title="Effective Exchange Rate Index"),
        now=NOW,
    ).items[0]
    assert release["desk"] == "economy"
    assert "economy" in release["topics"]
    assert release["declared_economic_ids"]

    office_opening = parse_feed(
        hksar,
        _rss(
            hksar,
            title="SCED opens Hong Kong Economic and Trade Office in Kuala Lumpur",
        ),
        now=NOW,
    ).items[0]
    assert office_opening["desk"] == "politics"
    assert office_opening["declared_economic_ids"] == []


def test_global_and_generic_primary_items_post_without_spurious_surface_links():
    ooni = _source("ooni")
    generic = parse_feed(
        ooni, _rss(ooni, title="A global network measurement release"), now=NOW
    ).items[0]
    china = parse_feed(
        ooni, _rss(ooni, title="A China network measurement release"), now=NOW
    ).items[0]
    assert generic["declared_scan_ids"] == []
    assert china["declared_scan_ids"]

    hksar = _source("hksar-releases")
    document = collect_newswire(
        _registry(hksar),
        lambda _u, **_k: _rss(hksar, title="Government opens temporary heat shelters"),
        now=NOW,
    )
    event = document["events"][0]
    assert event["declared_links"]["scan_signal_ids"] == []
    assert event["declared_links"]["economic_signal_ids"] == []
    assert event["lead"] is False


def test_collection_emits_explicit_success_stale_empty_fetch_and_parse_receipts():
    registry = load_source_registry()
    success_source = _source("ooni")
    stale_source = _source("hksar-releases")
    empty_source = _source("article19")
    parse_source = _source("apnic-blog")
    stale_rfc = "Sun, 09 Aug 2026 10:00:00 +0000"  # current window, older than HKSAR's 24h SLA
    mapping = {source.feed_url: RuntimeError("offline") for source in registry.sources}
    mapping[success_source.feed_url] = _rss(success_source)
    mapping[stale_source.feed_url] = _rss(stale_source, published=stale_rfc)
    mapping[empty_source.feed_url] = b"<rss><channel/></rss>"
    mapping[parse_source.feed_url] = b"<html>edge challenge</html>"

    document = collect_newswire(registry, _fetch_map(mapping), now=NOW)
    counts = document["coverage"]["counts"]
    receipts = {row["source_id"]: row for row in document["coverage"]["sources"]}

    assert counts == {"success": 1, "stale": 1, "empty": 1, "parse_error": 1, "fetch_error": 19}
    assert document["coverage"]["status"] == "degraded"
    assert document["coverage"]["successful_sources"] == 2
    assert document["n_items"] == 2
    assert receipts["hksar-releases"]["status"] == "stale"
    assert receipts["article19"]["document_sha256"] is not None
    assert receipts["apnic-blog"]["accepted_items"] == 0


def test_all_empty_unparseable_or_unreachable_sources_fail_closed():
    registry = load_source_registry()
    mapping = {source.feed_url: b"<rss><channel/></rss>" for source in registry.sources}
    with pytest.raises(NoSuccessfulSources, match="zero registered sources"):
        collect_newswire(registry, _fetch_map(mapping), now=NOW)


def test_all_invalid_entries_are_a_parse_error_not_a_quiet_empty_source():
    registry = load_source_registry()
    mapping = {source.feed_url: RuntimeError("offline") for source in registry.sources}
    source = _source("ooni")
    mapping[source.feed_url] = _rss(source, published="timezone-free")
    with pytest.raises(NoSuccessfulSources):
        collect_newswire(registry, _fetch_map(mapping), now=NOW)


def test_duplicate_out_of_window_and_per_source_cap_drops_are_counted():
    source = _source("ooni")
    recent = _rss(source, title="Recent", url="https://ooni.org/duplicate").decode()
    duplicate_item = recent.split("<item>", 1)[1].split("</item>", 1)[0]
    second = _rss(source, title="Second", url="https://ooni.org/second").decode().split("<item>", 1)[1].split("</item>", 1)[0]
    old = _rss(
        source,
        title="Old",
        url="https://ooni.org/old",
        published="Mon, 03 Aug 2026 10:00:00 +0000",
    ).decode().split("<item>", 1)[1].split("</item>", 1)[0]
    raw = f"<rss><channel><item>{duplicate_item}</item><item>{duplicate_item}</item><item>{second}</item><item>{old}</item></channel></rss>".encode()
    registry = _registry(source, max_items=1)

    document = collect_newswire(registry, lambda _url, **_kwargs: raw, now=NOW)
    receipt = document["coverage"]["sources"][0]

    assert document["n_items"] == 1
    assert receipt["items_seen"] == 4
    assert receipt["out_of_window_items"] == 1
    assert receipt["rejected_items"] == 3  # duplicate + old + over-cap


def test_item_id_is_stable_but_version_changes_when_metadata_changes():
    source = _source("ooni")
    registry = _registry(source)
    first = collect_newswire(registry, lambda _u, **_k: _rss(source, excerpt="First"), now=NOW)
    second = collect_newswire(
        registry,
        lambda _u, **_k: _rss(source, excerpt="Corrected"),
        now=NOW + timedelta(hours=1),
        previous=first,
    )

    assert first["items"][0]["item_id"] == second["items"][0]["item_id"]
    assert first["items"][0]["version_id"] != second["items"][0]["version_id"]
    assert first["events"][0]["event_id"] == second["events"][0]["event_id"]
    assert second["events"][0]["mutation"] == {
        "kind": "updated",
        "previous_version_id": first["events"][0]["version_id"],
    }


def test_unchanged_event_version_survives_a_new_collection_timestamp():
    source = _source("ooni")
    registry = _registry(source)
    raw = _rss(source)
    first = collect_newswire(registry, lambda _u, **_k: raw, now=NOW)
    second = collect_newswire(
        registry, lambda _u, **_k: raw, now=NOW + timedelta(hours=1), previous=first
    )

    assert first["events"][0]["version_id"] == second["events"][0]["version_id"]
    assert first["items"][0]["collected_at"] != second["items"][0]["collected_at"]
    assert second["events"][0]["mutation"]["kind"] == "unchanged"


def test_event_id_persists_when_a_new_corroborating_source_is_added():
    first_source = replace(_source("ooni"), declared_scan_ids=(), declared_economic_ids=())
    second_source = replace(_source("citizen-lab"), declared_scan_ids=(), declared_economic_ids=())
    title = "Independent measurement finds a specific network disruption"
    first_registry = _registry(first_source)
    first = collect_newswire(
        first_registry,
        lambda _u, **_k: _rss(first_source, title=title),
        now=NOW,
    )
    second_registry = _registry(first_source, second_source)
    mapping = {
        first_source.feed_url: _rss(first_source, title=title),
        second_source.feed_url: _rss(second_source, title=title),
    }
    second = collect_newswire(
        second_registry,
        _fetch_map(mapping),
        now=NOW,
        previous=first,
    )

    assert len(second["events"]) == 1
    assert second["events"][0]["event_id"] == first["events"][0]["event_id"]
    assert second["events"][0]["evidence_strength"] == "measurement-corroborated"
    assert second["events"][0]["lead"] is True


def test_same_independence_group_never_inflates_corroboration():
    one = replace(_source("github-government-takedowns"), declared_scan_ids=())
    two = replace(_source("github-dmca"), declared_scan_ids=())
    title = "Repository publishes a transparency notice for one request"
    mapping = {one.feed_url: _rss(one, title=title), two.feed_url: _rss(two, title=title)}

    document = collect_newswire(_registry(one, two), _fetch_map(mapping), now=NOW)
    event = document["events"][0]

    assert len(event["evidence_refs"]) == 2
    assert len(event["evidence_groups"]) == 1
    assert event["evidence_groups"][0]["source_ids"] == sorted([one.id, two.id])
    assert event["evidence_strength"] == "single-primary-source"


def test_three_scmp_desks_are_one_syndication_group_in_an_event():
    sources = [
        replace(_source(source_id), declared_scan_ids=(), declared_economic_ids=())
        for source_id in ("scmp-china", "scmp-china-economy", "scmp-china-tech")
    ]
    title = "One China policy report appears across three editorial desks"
    mapping = {source.feed_url: _rss(source, title=title) for source in sources}
    document = collect_newswire(_registry(*sources), _fetch_map(mapping), now=NOW)
    event = document["events"][0]

    assert len(event["evidence_refs"]) == 3
    assert len(event["evidence_groups"]) == 1
    assert event["evidence_strength"] == "single-source"
    assert event["lead"] is False


def test_single_unlinked_media_item_is_retained_but_not_promoted_as_a_lead():
    source = replace(
        _source("rfa-mandarin"),
        declared_scan_ids=(),
        declared_economic_ids=(),
    )
    document = collect_newswire(_registry(source), lambda _u, **_k: _rss(source), now=NOW)
    event = document["events"][0]

    assert event["lead"] is False
    assert event["evidence_strength"] == "single-source"
    assert "not promoted" in event["lead_reason"]


def test_single_media_economic_link_is_visible_but_cannot_create_a_lead():
    source = _source("scmp-china-economy")
    document = collect_newswire(
        _registry(source),
        lambda _u, **_k: _rss(
            source, title="China inflation report with yuan market detail"
        ),
        now=NOW,
    )
    event = document["events"][0]

    assert event["declared_links"]["economic_signal_ids"]
    assert event["lead"] is False
    assert "not promoted" in event["lead_reason"]


def test_declared_scan_or_economic_link_is_visible_but_never_called_corroboration():
    source = _source("hksar-releases")
    document = collect_newswire(
        _registry(source),
        lambda _u, **_k: _rss(source, title="Effective Exchange Rate Index"),
        now=NOW,
    )
    event = document["events"][0]

    assert event["lead"] is True
    assert event["declared_links"]["relation"] == "topic-surface-only"
    assert event["declared_links"]["economic_signal_ids"]
    assert "topical" in event["lead_reason"]
    assert "truth" in " ".join(event["limitations"]).lower()


def test_stale_source_is_retained_with_links_but_can_never_create_a_lead():
    source = replace(_source("scmp-china-economy"), stale_after_hours=1)
    document = collect_newswire(
        _registry(source),
        lambda _u, **_k: _rss(
            source, title="China inflation and yuan markets enter a new phase"
        ),
        now=NOW,
    )
    event = document["events"][0]

    assert document["coverage"]["sources"][0]["status"] == "stale"
    assert event["declared_links"]["economic_signal_ids"]
    assert event["lead"] is False
    assert "stale" in event["lead_reason"].lower()
    validate_newswire_document(document)


def test_every_accepted_item_is_partitioned_into_exactly_one_event():
    registry = load_source_registry()
    document = collect_newswire(registry, _fetch_map(_all_live_mapping(registry)), now=NOW)
    refs = [ref["item_id"] for event in document["events"] for ref in event["evidence_refs"]]

    assert len(refs) == document["n_items"] == 23
    assert len(refs) == len(set(refs))
    assert set(refs) == {item["item_id"] for item in document["items"]}
    assert "confidence" not in json.dumps(document).casefold()
    assert "truth_score" not in json.dumps(document).casefold()


def test_feed_entry_order_does_not_change_item_or_event_ids_and_versions():
    source = _source("ooni")
    item_a = _rss(source, title="A specific outage report", url="https://ooni.org/a").decode().split("<item>", 1)[1].split("</item>", 1)[0]
    item_b = _rss(source, title="A separate censorship study", url="https://ooni.org/b").decode().split("<item>", 1)[1].split("</item>", 1)[0]
    forward = f"<rss><channel><item>{item_a}</item><item>{item_b}</item></channel></rss>".encode()
    reverse = f"<rss><channel><item>{item_b}</item><item>{item_a}</item></channel></rss>".encode()
    registry = _registry(source)

    first = collect_newswire(registry, lambda _u, **_k: forward, now=NOW)
    second = collect_newswire(registry, lambda _u, **_k: reverse, now=NOW)

    assert {(item["item_id"], item["version_id"]) for item in first["items"]} == {
        (item["item_id"], item["version_id"]) for item in second["items"]
    }
    assert {(event["event_id"], event["version_id"]) for event in first["events"]} == {
        (event["event_id"], event["version_id"]) for event in second["events"]
    }


def test_public_event_order_is_reverse_chronological_not_lead_first():
    economy = _source("hksar-releases")
    politics = _source("rfa-mandarin")
    mapping = {
        economy.feed_url: _rss(
            economy,
            title="Effective Exchange Rate Index",
            published="Tue, 11 Aug 2026 09:00:00 +0000",
        ),
        politics.feed_url: _rss(
            politics,
            title="A regional cultural note",
            published="Tue, 11 Aug 2026 11:00:00 +0000",
        ),
    }

    document = collect_newswire(
        _registry(economy, politics), _fetch_map(mapping), now=NOW
    )

    assert [event["updated_at"] for event in document["events"]] == sorted(
        [event["updated_at"] for event in document["events"]], reverse=True
    )
    assert document["events"][0]["lead"] is False
    assert document["events"][1]["lead"] is True


def test_concurrent_fetch_completion_cannot_change_output():
    registry = load_source_registry()
    mapping = _all_live_mapping(registry)
    serial = collect_newswire(registry, _fetch_map(mapping), now=NOW, max_workers=1)
    concurrent = collect_newswire(registry, _fetch_map(mapping), now=NOW, max_workers=8)

    assert serial == concurrent


def test_fetch_boundary_enforces_exact_no_redirect_transport_options():
    source = _source("ooni")
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return _rss(source)

    collect_newswire(_registry(source), fetch, now=NOW)

    assert calls[0][0] == source.feed_url
    assert calls[0][1]["max_redirects"] == 0
    assert calls[0][1]["max_bytes"] == nw.MAX_FEED_BYTES
    assert "Palimpsest" in calls[0][1]["headers"]["User-Agent"]


def test_runtime_validator_rejects_unknown_fields_and_broken_accounting():
    source = _source("ooni")
    document = collect_newswire(_registry(source), lambda _u, **_k: _rss(source), now=NOW)
    unknown = copy.deepcopy(document)
    unknown["truth_score"] = 1.0
    with pytest.raises(NewswireError, match="top-level"):
        validate_newswire_document(unknown)

    broken = copy.deepcopy(document)
    broken["events"][0]["evidence_refs"] = []
    with pytest.raises(NewswireError):
        validate_newswire_document(broken)

    tampered = copy.deepcopy(document)
    tampered["events"][0]["headline"] = "A fabricated replacement headline"
    with pytest.raises(NewswireError, match="version_id|editorial"):
        validate_newswire_document(tampered)

    wrong_prefix = copy.deepcopy(document)
    wrong_prefix["items"][0]["item_id"] = "event-" + "0" * 24
    with pytest.raises(NewswireError, match="item_id"):
        validate_newswire_document(wrong_prefix)

    bad_coverage = copy.deepcopy(document)
    bad_coverage["coverage"]["rejected_items"] += 1
    with pytest.raises(NewswireError, match="rejected"):
        validate_newswire_document(bad_coverage)


def test_generated_document_conforms_to_the_published_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    registry = load_source_registry()
    document = collect_newswire(registry, _fetch_map(_all_live_mapping(registry)), now=NOW)
    schema = json.loads((ROOT / "protocol" / "newswire-v1.schema.json").read_text())

    jsonschema.Draft202012Validator(schema).validate(document)


def test_cli_atomically_writes_latest_and_deduplicates_the_bounded_version_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import scripts.newswire_pull as cli

    registry = load_source_registry()
    mapping = _all_live_mapping(registry)

    def fake_safe_fetch(url, **_kwargs):
        return mapping[url]

    monkeypatch.setattr(cli, "safe_fetch_bytes", fake_safe_fetch)
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    args = [
        "--config", str(ROOT / "config" / "news_sources.json"),
        "--output", str(output),
        "--ledger", str(ledger),
        "--workers", "1",
        "--now", "2026-08-11T12:00:00Z",
    ]

    assert cli.main(args) == 0
    first = strict_json_loads(output.read_bytes())
    first_ledger = ledger.read_bytes().splitlines()
    assert len(first_ledger) == first["n_events"]

    assert cli.main(args) == 0
    second = strict_json_loads(output.read_bytes())
    assert all(event["mutation"]["kind"] == "unchanged" for event in second["events"])
    assert ledger.read_bytes().splitlines() == first_ledger
    validate_newswire_document(second)
    assert stat.S_IMODE(output.stat().st_mode) == 0o644
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o644


def test_prior_validator_allows_only_derived_editorial_state_migration():
    registry = load_source_registry()
    document = collect_newswire(registry, _fetch_map(_all_live_mapping(registry)), now=NOW)
    legacy = copy.deepcopy(document)
    legacy["events"] = sorted(legacy["events"], key=lambda event: (not event["lead"], event["event_id"]))
    event = legacy["events"][0]
    event["lead"] = not event["lead"]
    event["lead_reason"] = "A bounded prior editorial rule."
    payload = {
        key: value
        for key, value in event.items()
        if key not in {"event_id", "url", "version_id", "mutation"}
    }
    event["version_id"] = nw._stable_id("eventv", payload)

    with pytest.raises(NewswireError, match="lead eligibility|reverse-chronological"):
        validate_newswire_document(legacy)
    validate_prior_newswire_document(legacy)

    corrupt = copy.deepcopy(legacy)
    corrupt["events"][0]["evidence_refs"][0]["url"] = "http://unsafe.example/"
    with pytest.raises(NewswireError, match="role/url"):
        validate_prior_newswire_document(corrupt)


def test_cli_rejects_a_duplicate_or_malformed_version_ledger(tmp_path: Path):
    import scripts.newswire_pull as cli

    record = {
        "event_id": "event-" + "1" * 24,
        "version_id": "eventv-" + "2" * 24,
        "recorded_at": "2026-08-11T12:00:00Z",
        "published_at": "2026-08-11T10:00:00Z",
        "headline": "A bounded headline",
        "evidence_strength": "single-source",
        "source_ids": ["bbc-chinese"],
        "previous_version_id": None,
    }
    line = json.dumps(record, sort_keys=True).encode() + b"\n"
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(line + line)
    with pytest.raises(ValueError, match="duplicates"):
        cli._load_ledger(path)

    record["source_ids"] = ["unsafe/source"]
    path.write_bytes(json.dumps(record).encode() + b"\n")
    with pytest.raises(ValueError, match="source_ids"):
        cli._load_ledger(path)


def test_cli_zero_success_preserves_last_good_latest_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import scripts.newswire_pull as cli

    registry = load_source_registry()
    mapping = _all_live_mapping(registry)
    monkeypatch.setattr(cli, "safe_fetch_bytes", lambda url, **_kwargs: mapping[url])
    output = tmp_path / "newswire-latest.json"
    ledger = tmp_path / "newswire-versions.jsonl"
    args = [
        "--output", str(output),
        "--ledger", str(ledger),
        "--workers", "1",
        "--now", "2026-08-11T12:00:00Z",
    ]
    assert cli.main(args) == 0
    latest_before = output.read_bytes()
    ledger_before = ledger.read_bytes()

    def offline(_url, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr(cli, "safe_fetch_bytes", offline)
    assert cli.main(args) == 2
    assert output.read_bytes() == latest_before
    assert ledger.read_bytes() == ledger_before


def test_cli_network_boundary_has_no_direct_urllib_callsite():
    source = (ROOT / "scripts" / "newswire_pull.py").read_text()
    assert "safe_fetch_bytes" in source
    assert "urllib" not in source
