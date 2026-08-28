"""Offline tests for public Telegram channel previews and ScamShield inbox drain."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

import pytest

from collectors.telegram_public_channels import (
    TelegramRegistryError,
    collect_channels,
    load_channels,
    load_join_index,
    load_registry,
    login_walled,
    mainland_echo_family,
    pagination_url,
    parse_preview,
)
from collectors.telegram_public_warehouse import archive_run, database_path
from evidence.scamshield import capsule_from_assessment
from tests.test_scamshield_adapter import _assessment


class _Live:
    def require_live(self):
        return None

    def is_halted(self):
        return False


PREVIEW = """
<html><body>
<div class="tgme_widget_message" data-post="DragonDenWhispers/12">
  <div class="tgme_widget_message_text">原文已删 — CDT archived this Weibo deletion about 青年失业率.
  <a href="https://chinadigitaltimes.net/2026/08/deleted-youth-unemployment/">cdt</a>
  </div>
  <time datetime="2026-08-20T06:00:00+00:00"></time>
</div>
<div class="tgme_widget_message" data-post="DragonDenWhispers/13">
  <div class="tgme_widget_message_text">Routine public China-desk note without an archive claim.</div>
  <time datetime="2026-08-20T06:05:00+00:00"></time>
</div>
</body></html>
"""
WALL = "<html><body>Please log in to Telegram login.telegram.org</body></html>"


def test_registry_keeps_external_coverage_as_discovery_only():
    registry = load_registry()
    channels = load_channels()
    assert len(registry["channels"]) == 50
    assert len(channels) == 3
    handles = {row["handle"] for row in channels}
    discovery_handles = {
        row["handle"]
        for row in registry["channels"]
        if row["collection_authorization"] == "discovery-only"
    }
    assert {
        "shannews47",
        "shannewsburmese",
        "spmnewsagency2019",
        "TachileikNewsAgency",
        "KachinNewsGroup",
        "CGTNOfficial_BJ",
        "PDChinaNews",
        "xinhua_news_agency_en",
    } <= discovery_handles
    assert {row["handle"] for row in channels if row["public_spread"]} == {
        "DragonDenWhispers",
        "DragonDenCyber",
        "DragonDenBorderlands",
    }
    assert all(row["preview_url"].startswith("https://t.me/s/") for row in channels)
    assert not any(row["handle"].lower().endswith("bot") for row in channels)
    group = next(row for row in registry["channels"] if row["kind"] == "public_group")
    assert group["collection_state"] == "candidate"
    assert group["public_projection"] == "disabled"
    external = next(
        row for row in registry["channels"] if row["handle"] == "shannews47"
    )
    assert external["collection_authorization"] == "discovery-only"
    assert external["public_projection"] == "disabled"
    assert external["archive_policy"] == "metadata-only"


def test_parser_keeps_public_text_and_outbound_links():
    posts = parse_preview(PREVIEW, expected_handle="DragonDenWhispers")
    assert [row["permalink"] for row in posts] == [
        "https://t.me/DragonDenWhispers/12",
        "https://t.me/DragonDenWhispers/13",
    ]
    assert "青年失业率" in posts[0]["text"]
    assert posts[0]["outbound_urls"] == [
        "https://chinadigitaltimes.net/2026/08/deleted-youth-unemployment/"
    ]


def test_parser_keeps_media_only_coordinates_without_downloading_media():
    html = """
    <div class="tgme_widget_message" data-post="shannews47/99">
      <a class="tgme_widget_message_photo_wrap" style="background-image:url('/file.jpg')"></a>
      <time datetime="2026-08-20T06:00:00+00:00"></time>
    </div>
    """
    posts = parse_preview(html, expected_handle="shannews47")
    assert len(posts) == 1
    assert posts[0]["text"] == ""
    assert posts[0]["media_kind"] == "photo"
    assert posts[0]["has_media"] is True


def test_html_shell_is_a_login_wall():
    assert login_walled(200, WALL) is True
    assert login_walled(403, "") is True
    assert login_walled(200, PREVIEW) is False


def test_echo_family_and_url_join_to_a_real_ledger():
    index = load_join_index(
        {
            "public-deletion-ledgers-latest.json": {
                "observations": [
                    {
                        "source": "cdt",
                        "title": "deleted youth unemployment",
                        "text": "CDT ledger excerpt about 青年失业率 that is long enough",
                        "url": "https://chinadigitaltimes.net/2026/08/deleted-youth-unemployment/",
                    }
                ]
            }
        }
    )
    result = collect_channels(
        channels=[
            {
                "handle": "DragonDenWhispers",
                "preview_url": "https://t.me/s/DragonDenWhispers",
                "permalink_base": "https://t.me/DragonDenWhispers",
                "kind": "public_channel",
                "desk": "dragon-den",
                "why": "test",
                "collection_authorization": "project-owned",
            }
        ],
        fetch=lambda _url: (200, PREVIEW),
        join_index=index,
        previous={},
        kill_switch=_Live(),
        now=datetime(2026, 8, 20, 7, 0, tzinfo=timezone.utc),
    )
    assert result["n_channels_ok"] == 1
    assert result["n_mainland_echo"] == 1
    echo = next(
        row for row in result["observations"] if row["echo_family"] == "mainland_echo"
    )
    assert echo["deletion_signal"] == "mainland_echo"
    assert echo["cross_links"]["cdt"]["url"].endswith("deleted-youth-unemployment/")
    assert echo["content_sha256"]
    assert echo["first_seen"]
    assert echo["gazetteer_hits"]


def test_stateful_pagination_resumes_backfill_without_replaying_the_cursor():
    base = PREVIEW.replace("DragonDenWhispers/12", "DragonDenWhispers/101").replace(
        "DragonDenWhispers/13", "DragonDenWhispers/100"
    )
    older_80 = PREVIEW.replace("DragonDenWhispers/12", "DragonDenWhispers/81").replace(
        "DragonDenWhispers/13", "DragonDenWhispers/80"
    )
    older_60 = PREVIEW.replace("DragonDenWhispers/12", "DragonDenWhispers/61").replace(
        "DragonDenWhispers/13", "DragonDenWhispers/60"
    )
    seen = []

    def first_fetch(url):
        seen.append(url)
        return (200, older_80 if "before=100" in url else base)

    channel = {
        "source_id": "dragon-den-whispers",
        "handle": "DragonDenWhispers",
        "preview_url": "https://t.me/s/DragonDenWhispers",
        "permalink_base": "https://t.me/DragonDenWhispers",
        "kind": "public_channel",
        "desk": "dragon-den",
        "public_projection": "full-observation",
        "archive_policy": "full-text-private",
        "collection_authorization": "project-owned",
    }
    first = collect_channels(
        channels=[channel],
        fetch=first_fetch,
        kill_switch=_Live(),
        max_pages_per_source=2,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    assert seen == [
        "https://t.me/s/DragonDenWhispers",
        pagination_url("https://t.me/s/DragonDenWhispers", 100),
    ]
    assert first["channel_state"]["dragon-den-whispers"]["next_before"] == 80

    seen.clear()

    def second_fetch(url):
        seen.append(url)
        return (200, older_60 if "before=80" in url else base)

    second = collect_channels(
        channels=[channel],
        fetch=second_fetch,
        previous={
            "posts": first["posts"],
            "channel_state": first["channel_state"],
        },
        kill_switch=_Live(),
        max_pages_per_source=2,
        now=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )
    assert seen[-1] == pagination_url("https://t.me/s/DragonDenWhispers", 80)
    assert second["channel_state"]["dragon-den-whispers"]["next_before"] == 60


def test_external_publisher_is_never_fetched_without_collection_authority():
    source = next(
        row
        for row in load_channels(include_inactive=True)
        if row["handle"] == "shannews47"
    )
    calls = []

    def forbidden_fetch(url):
        calls.append(url)
        return (200, PREVIEW.replace("DragonDenWhispers", "shannews47"))

    with pytest.raises(TelegramRegistryError, match="authorization is absent"):
        collect_channels(
            channels=[source],
            fetch=forbidden_fetch,
            kill_switch=_Live(),
            max_pages_per_source=1,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
    assert calls == []


def test_expired_license_is_rejected_before_network_access():
    source = {
        "source_id": "licensed-fixture",
        "handle": "LicensedFixture",
        "preview_url": "https://t.me/s/LicensedFixture",
        "permalink_base": "https://t.me/LicensedFixture",
        "kind": "public_channel",
        "collection_authorization": "licensed",
        "authorization_ref": "private-rights-ledger:fixture",
        "authorization_expires_at": "2026-08-19T00:00:00Z",
    }
    calls = []

    with pytest.raises(TelegramRegistryError, match="authorization is absent"):
        collect_channels(
            channels=[source],
            fetch=lambda url: calls.append(url) or (200, PREVIEW),
            kill_switch=_Live(),
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
    assert calls == []


def test_redirected_message_coordinates_fail_closed():
    source = next(row for row in load_channels() if row["handle"] == "DragonDenCyber")
    result = collect_channels(
        channels=[source],
        fetch=lambda _url: (200, PREVIEW),
        kill_switch=_Live(),
        max_pages_per_source=1,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    assert result["channels"][0]["status"] == "identity_mismatch"
    assert result["n_messages_observed"] == 0


def test_span_join_to_official_first_seen_without_inventing_a_link():
    span = "Official landing text that is long enough to join"
    index = load_join_index(
        {
            "official-first-seen-latest.json": {
                "observations": [
                    {
                        "title": "新华网",
                        "text": span,
                        "url": "https://www.news.cn/",
                        "source": "official_first_seen",
                    }
                ]
            }
        }
    )
    html = (
        '<div class="tgme_widget_message" data-post="DragonDenCyber/9">'
        f'<div class="tgme_widget_message_text">Desk note: {span}.</div></div>'
    )
    result = collect_channels(
        channels=[
            {
                "handle": "DragonDenCyber",
                "preview_url": "https://t.me/s/DragonDenCyber",
                "permalink_base": "https://t.me/DragonDenCyber",
                "kind": "public_channel",
                "desk": "dragon-den",
                "why": "test",
                "collection_authorization": "project-owned",
            }
        ],
        fetch=lambda _url: (200, html),
        join_index=index,
        kill_switch=_Live(),
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    obs = result["observations"][0]
    assert obs["joins"][0]["family"] == "official-first-seen"
    assert obs["joins"][0]["match"] == "span"
    assert obs["cross_links"]["cdt"] is None


def test_echo_family_requires_archive_or_deletion_language():
    assert mainland_echo_family("Routine desk note", []) is None
    assert (
        mainland_echo_family(
            "原文已删",
            ["https://chinadigitaltimes.net/chinese/deleted/"],
        )
        == "mainland_echo"
    )


def test_pull_abstains_when_every_preview_is_walled(tmp_path, monkeypatch):
    import scripts.telegram_public_channels_pull as pull

    monkeypatch.setattr(pull, "OUT", tmp_path / "telegram-public-channels-latest.json")
    monkeypatch.setattr(
        pull, "HIST", tmp_path / "telegram-public-channels-history.jsonl"
    )
    monkeypatch.setattr(pull, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "INBOX", tmp_path / "missing-inbox")
    monkeypatch.setattr(pull, "KillSwitch", lambda: _Live())

    assert (
        pull.main(
            fetch=lambda url: (200, WALL),
            readings_dir=tmp_path,
            inbox=tmp_path / "missing-inbox",
        )
        is None
    )
    assert not (tmp_path / "telegram-public-channels-latest.json").exists()


def test_scamshield_drain_lands_sanitized_counts_without_iocs(tmp_path, monkeypatch):
    import scripts.telegram_public_channels_pull as pull

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    capsule = capsule_from_assessment(_assessment())
    (inbox / f"{capsule['content_sha256']}.json").write_text(
        json.dumps(capsule, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(pull, "OUT", tmp_path / "telegram-public-channels-latest.json")
    monkeypatch.setattr(
        pull, "HIST", tmp_path / "telegram-public-channels-history.jsonl"
    )
    monkeypatch.setattr(pull, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "KillSwitch", lambda: _Live())

    out = pull.main(
        fetch=lambda url: (200, PREVIEW),
        readings_dir=tmp_path,
        inbox=inbox,
        now=datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc),
    )
    assert out is not None
    assert out["scamshield"]["status"] == "drained"
    assert out["scamshield"]["n_candidates"] == 1
    assert out["scamshield"]["automatic_publication"] is False
    dumped = json.dumps(out["scamshield"])
    assert "@private_handle" not in dumped
    assert "+91" not in dumped
    assert "ivory" not in dumped
    assert not (tmp_path / "telegram-watch-latest.json").exists()


def test_private_warehouse_is_idempotent_and_preserves_edits(tmp_path):
    source = next(
        row
        for row in load_channels(include_inactive=True)
        if row["handle"] == "shannews47"
    )
    record = {
        "source_id": source["source_id"],
        "message_id": "12",
        "permalink": "https://t.me/shannews47/12",
        "published_at": "2026-08-20T06:00:00Z",
        "first_seen": "2026-08-20T07:00:00Z",
        "text": "first public preview text",
        "outbound_urls": ["https://shannews.org/example"],
        "has_media": False,
        "media_kind": None,
        "content_sha256": "a" * 64,
        "archive_policy": "full-text-private",
    }
    receipt = {
        "source_id": source["source_id"],
        "page_number": 1,
        "locator_sha256": "b" * 64,
        "http_status": 200,
        "status": "ok",
        "body_sha256": "c" * 64,
        "n_posts": 1,
    }
    first = archive_run(
        generated_at="2026-08-20T07:00:00Z",
        registry_sha256="d" * 64,
        sources=[source],
        records=[record],
        receipts=[receipt],
        sources_attempted=1,
        sources_ok=1,
        pages_fetched=1,
        warehouse=tmp_path,
    )
    again = archive_run(
        generated_at="2026-08-20T07:00:00Z",
        registry_sha256="d" * 64,
        sources=[source],
        records=[record],
        receipts=[receipt],
        sources_attempted=1,
        sources_ok=1,
        pages_fetched=1,
        warehouse=tmp_path,
    )
    edited = dict(record, text="edited public preview text", content_sha256="e" * 64)
    final = archive_run(
        generated_at="2026-08-20T08:00:00Z",
        registry_sha256="d" * 64,
        sources=[source],
        records=[edited],
        receipts=[receipt],
        sources_attempted=1,
        sources_ok=1,
        pages_fetched=1,
        warehouse=tmp_path,
    )
    assert (
        first["total_messages"]
        == again["total_messages"]
        == final["total_messages"]
        == 1
    )
    assert final["total_versions"] == 2
    assert os.stat(database_path(tmp_path)).st_mode & 0o777 == 0o600
    with sqlite3.connect(database_path(tmp_path)) as connection:
        text_private, digest = connection.execute(
            "SELECT text_private, current_content_sha256 FROM messages"
        ).fetchone()
    assert text_private == "edited public preview text"
    assert digest == "e" * 64
