"""Offline tests for public Telegram channel previews and ScamShield inbox drain."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from collectors.telegram_public_channels import (
    collect_channels,
    load_channels,
    load_join_index,
    login_walled,
    mainland_echo_family,
    parse_preview,
)
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


def test_registry_is_the_three_in_tree_dragon_den_channels():
    channels = load_channels()
    handles = [row["handle"] for row in channels]
    assert handles == ["DragonDenWhispers", "DragonDenCyber", "DragonDenBorderlands"]
    assert all(row["preview_url"].startswith("https://t.me/s/") for row in channels)
    assert not any(row["handle"].lower().endswith("bot") for row in channels)
    blob = json.dumps(channels)
    assert "chinadigitaltimes" not in blob
    assert "greatfire" not in blob
    assert "cgtn" not in blob.lower()


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


def test_html_shell_is_a_login_wall():
    assert login_walled(200, WALL) is True
    assert login_walled(403, "") is True
    assert login_walled(200, PREVIEW) is False


def test_echo_family_and_url_join_to_a_real_ledger():
    index = load_join_index({
        "public-deletion-ledgers-latest.json": {
            "observations": [{
                "source": "cdt",
                "title": "deleted youth unemployment",
                "text": "CDT ledger excerpt about 青年失业率 that is long enough",
                "url": "https://chinadigitaltimes.net/2026/08/deleted-youth-unemployment/",
            }]
        }
    })
    result = collect_channels(
        channels=[{
            "handle": "DragonDenWhispers",
            "preview_url": "https://t.me/s/DragonDenWhispers",
            "permalink_base": "https://t.me/DragonDenWhispers",
            "kind": "public_channel",
            "desk": "dragon-den",
            "why": "test",
        }],
        fetch=lambda _url: (200, PREVIEW),
        join_index=index,
        previous={},
        kill_switch=_Live(),
        now=datetime(2026, 8, 20, 7, 0, tzinfo=timezone.utc),
    )
    assert result["n_channels_ok"] == 1
    assert result["n_mainland_echo"] == 1
    echo = next(row for row in result["observations"] if row["echo_family"] == "mainland_echo")
    assert echo["deletion_signal"] == "mainland_echo"
    assert echo["cross_links"]["cdt"]["url"].endswith("deleted-youth-unemployment/")
    assert echo["content_sha256"]
    assert echo["first_seen"]
    assert echo["gazetteer_hits"]


def test_span_join_to_official_first_seen_without_inventing_a_link():
    span = "Official landing text that is long enough to join"
    index = load_join_index({
        "official-first-seen-latest.json": {
            "observations": [{
                "title": "新华网",
                "text": span,
                "url": "https://www.news.cn/",
                "source": "official_first_seen",
            }]
        }
    })
    html = (
        '<div class="tgme_widget_message" data-post="DragonDenCyber/9">'
        f'<div class="tgme_widget_message_text">Desk note: {span}.</div></div>'
    )
    result = collect_channels(
        channels=[{
            "handle": "DragonDenCyber",
            "preview_url": "https://t.me/s/DragonDenCyber",
            "permalink_base": "https://t.me/DragonDenCyber",
            "kind": "public_channel",
            "desk": "dragon-den",
            "why": "test",
        }],
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
    assert mainland_echo_family(
        "原文已删",
        ["https://chinadigitaltimes.net/chinese/deleted/"],
    ) == "mainland_echo"


def test_pull_abstains_when_every_preview_is_walled(tmp_path, monkeypatch):
    import scripts.telegram_public_channels_pull as pull

    monkeypatch.setattr(pull, "OUT", tmp_path / "telegram-public-channels-latest.json")
    monkeypatch.setattr(pull, "HIST", tmp_path / "telegram-public-channels-history.jsonl")
    monkeypatch.setattr(pull, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(pull, "READINGS", tmp_path)
    monkeypatch.setattr(pull, "INBOX", tmp_path / "missing-inbox")
    monkeypatch.setattr(pull, "KillSwitch", lambda: _Live())

    assert pull.main(fetch=lambda url: (200, WALL), readings_dir=tmp_path, inbox=tmp_path / "missing-inbox") is None
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
    monkeypatch.setattr(pull, "HIST", tmp_path / "telegram-public-channels-history.jsonl")
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
