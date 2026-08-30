"""Contextual, content-addressed social-card contracts."""

from __future__ import annotations

import copy
import hashlib
import html
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core import newsroom
from core.china_econ_export import load_source_policy
from scripts import build_newsroom, share_cards, stage_pages_rights


def _spec(**overrides):
    value = {
        "schema_version": share_cards.SPEC_VERSION,
        "kind": "instrument-reading",
        "kicker": "Command desk / evidence reading",
        "title": "Forecast bands covered 84.4% of 1,606 scored readings",
        "status": "live",
        "status_label": "Current evidence",
        "metric": {"value": "84.4%", "label": "empirical coverage"},
        "as_of": "2026-08-30T07:32:12Z",
        "source": "forecast-ledger-latest.json",
        "receipt": "SHA256 92dd686d31a4e373",
        "target_url": "https://palimpsest.info/news/forecast-ledger/",
    }
    value.update(overrides)
    return value


def test_renderer_is_deterministic_content_addressed_and_exact_dimensions():
    first = share_cards.render_card(_spec())
    second = share_cards.render_card(copy.deepcopy(_spec()))

    assert first.png == second.png
    assert first.sha256 == hashlib.sha256(first.png).hexdigest()
    assert first.path.name == f"sha256-{first.sha256}.png"
    assert first.url.endswith(first.path.as_posix())
    assert share_cards.png_dimensions(first.png) == (1200, 630)


def test_evidence_change_busts_the_card_url():
    original = share_cards.render_card(_spec())
    revised = share_cards.render_card(
        _spec(
            title="Forecast bands covered 84.5% of 1,607 scored readings",
            metric={"value": "84.5%", "label": "empirical coverage"},
            receipt="SHA256 revised000000000",
        )
    )

    assert original.spec_sha256 != revised.spec_sha256
    assert original.png != revised.png
    assert original.url != revised.url


def test_missing_state_cannot_render_zero_or_any_retained_metric():
    with pytest.raises(
        share_cards.ShareCardError,
        match="missing cards must not render a current metric",
    ):
        share_cards.render_card(
            _spec(
                status="missing",
                status_label="Source missing",
                metric={"value": "0", "label": "current value"},
            )
        )

    feed = newsroom.build_news_feed()
    story = copy.deepcopy(feed["stories"][0])
    section = next(row for row in feed["sections"] if row["id"] == story["section"])
    story["status"] = "missing"
    story["metric"] = {
        "label": "retained value",
        "value": 0,
        "unit": "count",
        "denominator": {"label": None, "value": None},
    }
    spec = build_newsroom._story_share_card_spec(story, section=section)
    card = share_cards.render_card(spec)

    assert spec["metric"] is None
    assert "Source missing" in card.alt
    assert " 0 " not in f" {card.alt} "


def test_policy_denied_economic_values_render_contextual_withheld_cards() -> None:
    assert (
        frozenset(build_newsroom._PUBLIC_VALUE_WITHHELD_SHARE_CARDS)
        == stage_pages_rights.DERIVED_INSTRUMENTS
    )
    feed = newsroom.build_news_feed()
    sections = {row["id"]: row for row in feed["sections"]}
    stories = {row["signal_id"]: copy.deepcopy(row) for row in feed["stories"]}
    fixtures = {
        "china-econ": {
            "headline": "3 official money-market benchmark families are reporting",
            "public_value": "3 count",
            "metric": {
                "label": "benchmark families reporting",
                "value": 3,
                "unit": "count",
                "denominator": {"label": None, "value": None},
            },
        },
        "cny-fix-gap": {
            "headline": (
                "The official yuan fix gap is -0.8883% against the independent "
                "reference"
            ),
            "public_value": "-0.8883%",
            "metric": {
                "label": "fix gap",
                "value": -0.8883,
                "unit": "percent",
                "denominator": {"label": None, "value": None},
            },
        },
    }
    clock = datetime(2026, 8, 30, 16, 0, tzinfo=UTC)
    policy = load_source_policy(
        build_newsroom.ROOT / stage_pages_rights.POLICY_RELATIVE_PATH
    )
    allowed = frozenset(
        source_id
        for source_id, decision in policy.decisions.items()
        if stage_pages_rights._effective_decision(decision, evaluated_at=clock)
        == "allow"
    )
    denied = frozenset(set(policy.decisions) - set(allowed))
    lineage_pattern = stage_pages_rights._lineage_pattern(denied)

    for signal_id, fixture in fixtures.items():
        story = stories[signal_id]
        story["status"] = "live"
        story.update(fixture)
        source_filename = f"{signal_id}-forbidden-value-sentinel.json"
        source_digest = "fedcba9876543210" * 4
        source_timestamp = "2099-12-31T23:59:58Z"
        story["evidence"] = copy.deepcopy(story["evidence"])
        story["evidence"]["source_timestamp"] = source_timestamp
        story["evidence"]["input"] = {
            "filename": source_filename,
            "sha256": source_digest,
        }
        spec = build_newsroom._story_share_card_spec(
            story,
            section=sections[story["section"]],
        )
        card = share_cards.render_card(spec)
        serialized = json.dumps(spec, sort_keys=True)

        assert spec["status"] == "restricted"
        assert spec["metric"] is None
        assert "withheld under public source policy" in spec["title"]
        assert spec["as_of"] is None
        assert spec["source"] == "china-publication-rights-latest.json"
        assert spec["receipt"] is None
        assert fixture["headline"] not in card.alt
        assert fixture["public_value"] not in card.alt
        assert source_filename not in serialized
        assert source_digest not in serialized
        assert source_timestamp not in serialized
        target_path = stage_pages_rights._share_card_target_path(
            build_newsroom.ROOT, spec
        )
        assert not stage_pages_rights._contains_denied_payload(
            build_newsroom.ROOT,
            target_path,
            stage_pages_rights._canonical_json(spec),
            denied_source_ids=denied,
            allowed_source_ids=allowed,
            lineage_pattern=lineage_pattern,
        )


def test_edition_card_never_promotes_a_policy_withheld_instrument() -> None:
    feed = copy.deepcopy(newsroom.build_news_feed())
    cny_story = next(
        story for story in feed["stories"] if story["signal_id"] == "cny-fix-gap"
    )
    safe_story = next(
        story
        for story in feed["stories"]
        if story["signal_id"] not in build_newsroom._PUBLIC_VALUE_WITHHELD_SHARE_CARDS
    )
    for story in feed["stories"]:
        story["status"] = "stale"
        story["priority"] = "standard"
    cny_story.update(
        status="live",
        priority="lead",
        headline="The official yuan fix gap is -0.8883%",
        metric={
            "label": "fix gap",
            "value": -0.8883,
            "unit": "percent",
            "denominator": {"label": None, "value": None},
        },
    )
    safe_story.update(
        status="live",
        headline="An unrestricted current evidence reading",
    )

    spec = build_newsroom._edition_share_card_spec(feed)

    assert spec["title"] == safe_story["headline"]
    assert "-0.8883" not in json.dumps(spec)


def test_edition_card_refuses_an_all_policy_withheld_feed() -> None:
    feed = copy.deepcopy(newsroom.build_news_feed())
    feed["stories"] = [
        story
        for story in feed["stories"]
        if story["signal_id"] in build_newsroom._PUBLIC_VALUE_WITHHELD_SHARE_CARDS
    ]

    with pytest.raises(
        newsroom.NewsroomError,
        match="no public-value-safe share-card lead",
    ):
        build_newsroom._edition_share_card_spec(feed)


def test_hostile_text_is_bounded_direction_safe_and_html_escaped():
    hostile = '<script data-x="&">ALERT</script>\u202e\nNEXT'
    card = share_cards.render_card(_spec(title=hostile))
    page = build_newsroom._head(
        title=hostile,
        description=hostile,
        canonical="https://palimpsest.info/news/hostile/",
        page_type="article",
        json_ld={},
        image_url=card.url,
        image_alt=card.alt,
    )

    assert "\u202e" not in card.alt
    assert "\n" not in card.alt
    assert hostile not in page
    assert html.escape(hostile, quote=True) in page
    assert '<script data-x="&">' not in page
    assert card.url in page


def test_manifest_reproduces_each_card_and_rejects_digest_drift():
    cards = [
        share_cards.render_card(_spec()),
        share_cards.render_card(
            _spec(
                status="stale",
                status_label="Evidence stale",
                metric=None,
                receipt="SHA256 stale00000000000",
            )
        ),
    ]
    raw = share_cards.manifest_bytes(cards)
    parsed = share_cards.parse_manifest(raw)
    assert [row["path"] for row, _ in parsed] == sorted(
        card.path.as_posix() for card in cards
    )

    document = json.loads(raw)
    document["cards"][0]["sha256"] = "0" * 64
    document["cards"][0]["path"] = f"assets/share-cards/sha256-{'0' * 64}.png"
    with pytest.raises(share_cards.ShareCardError, match="does not reproduce"):
        share_cards.validate_manifest_document(document)


def test_publisher_removes_only_prior_manifest_proven_superseded_cards(tmp_path):
    first = share_cards.render_card(_spec())
    first_outputs = {
        first.path: first.png,
        share_cards.MANIFEST_PATH: share_cards.manifest_bytes([first]),
    }
    assert build_newsroom.publish(first_outputs, root=tmp_path) == (2, 0)
    assert (tmp_path / first.path).is_file()

    second = share_cards.render_card(
        _spec(title="A revised exact evidence headline", receipt="SHA256 revised")
    )
    second_outputs = {
        second.path: second.png,
        share_cards.MANIFEST_PATH: share_cards.manifest_bytes([second]),
    }
    changed, unchanged = build_newsroom.publish(second_outputs, root=tmp_path)

    assert (changed, unchanged) == (3, 0)
    assert not (tmp_path / first.path).exists()
    assert (tmp_path / second.path).read_bytes() == second.png
    assert build_newsroom.check(second_outputs, root=tmp_path) == []


def test_common_chinese_headlines_use_real_bitmap_glyphs():
    rows = share_cards.share_card_cjk_data.glyph_rows("中")
    card = share_cards.render_card(
        _spec(title="中国海警位中国黄岩岛领海及周边区域执法巡查")
    )

    assert rows is not None
    assert any(rows)
    assert "中国海警" in card.alt
    assert share_cards.png_dimensions(card.png) == (1200, 630)


def test_story_renderer_publishes_complete_contextual_image_metadata():
    feed = newsroom.build_news_feed()
    sections = {row["id"]: row for row in feed["sections"]}
    stories = {row["signal_id"]: row for row in feed["stories"]}
    story = next(row for row in feed["stories"] if row["status"] == "live")
    card = share_cards.render_card(
        build_newsroom._story_share_card_spec(
            story,
            section=sections[story["section"]],
        )
    )
    page = build_newsroom.render_story(
        story,
        section=sections[story["section"]],
        by_id=stories,
        share_card=card,
    )

    expected = {
        f'<meta property="og:image" content="{card.url}">',
        f'<meta property="og:image:secure_url" content="{card.url}">',
        '<meta property="og:image:type" content="image/png">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        f'<meta property="og:image:alt" content="{html.escape(card.alt, quote=True)}">',
        f'<meta name="twitter:image" content="{card.url}">',
        f'<meta name="twitter:image:alt" content="{html.escape(card.alt, quote=True)}">',
    }
    for tag in expected:
        assert tag in page
    assert (
        card.url
        in json.loads(
            page.split('<script type="application/ld+json">', 1)[1].split(
                "</script>", 1
            )[0]
        )["image"]
    )


def test_generated_story_and_data_families_cannot_fall_back_to_generic_card():
    feed = newsroom.build_news_feed()
    outputs = build_newsroom.build_outputs(feed)
    declared = [
        Path("news/index.html"),
        Path("news/china/analysis/index.html"),
        *(Path("news") / story["slug"] / "index.html" for story in feed["stories"]),
    ]

    build_newsroom._assert_contextual_share_coverage(outputs, required_paths=declared)
    generic = dict(outputs)
    generic[declared[0]] = build_newsroom.render_index(feed).encode("utf-8")
    with pytest.raises(newsroom.NewsroomError, match="generic image news/index.html"):
        build_newsroom._assert_contextual_share_coverage(
            generic, required_paths=declared
        )

    duplicate = dict(outputs)
    duplicate[declared[0]] = duplicate[declared[0]].replace(
        b"</head>",
        f'<meta property="og:image" content="{build_newsroom.OG_IMAGE}">\n</head>'.encode(),
    )
    with pytest.raises(newsroom.NewsroomError, match="generic image news/index.html"):
        build_newsroom._assert_contextual_share_coverage(
            duplicate, required_paths=declared
        )


def test_historical_wire_alias_gets_its_own_complete_contextual_metadata(tmp_path):
    wire = json.loads(Path("readings/newswire-latest.json").read_text(encoding="utf-8"))
    event = wire["events"][0]
    event_id = event["event_id"]
    directory = tmp_path / "news" / "wire" / event_id
    directory.mkdir(parents=True)
    (directory / "story.json").write_bytes(build_newsroom._pretty_json(event))
    (directory / "index.html").write_text(
        f'''<!doctype html><html><head>
<link rel="canonical" href="{event["url"]}">
<meta property="og:image" content="{build_newsroom.OG_IMAGE}">
<meta name="twitter:image" content="{build_newsroom.OG_IMAGE}">
<link rel="stylesheet" href="/assets/newsroom.css">

</head><body><h1>Retained dossier</h1></body></html>''',
        encoding="utf-8",
    )

    cards, pages = build_newsroom._historical_wire_share_outputs(
        archive_root=tmp_path,
        current_event_ids=frozenset(),
    )
    page_path = Path("news/wire") / event_id / "index.html"
    page = pages[page_path].decode("utf-8")
    card = cards[event_id]

    assert card.spec["title"] == event["headline"]
    assert card.spec["receipt"] == event["version_id"]
    assert build_newsroom.OG_IMAGE not in page
    assert page.count(f'<meta property="og:image" content="{card.url}">') == 1
    build_newsroom._assert_contextual_share_coverage(pages, required_paths=[page_path])

    (directory / "index.html").write_bytes(pages[page_path])
    second_cards, second_pages = build_newsroom._historical_wire_share_outputs(
        archive_root=tmp_path,
        current_event_ids=frozenset(),
    )

    assert second_cards[event_id].png == card.png
    assert second_pages[page_path] == pages[page_path]
