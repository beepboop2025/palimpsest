"""Contextual, content-addressed social-card contracts."""

from __future__ import annotations

import copy
import hashlib
import html
import json
from pathlib import Path

import pytest

from core import newsroom
from scripts import build_newsroom, share_cards


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
