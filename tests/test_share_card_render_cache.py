"""A publication-scoped PNG cache must preserve evidence and disk validation."""

from __future__ import annotations

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from unittest.mock import Mock

import pytest

from core import newsroom
from scripts import build_newsroom, share_cards


def _spec(**overrides):
    value = {
        "schema_version": share_cards.SPEC_VERSION,
        "kind": "instrument-reading",
        "kicker": "Evidence reading",
        "title": "Forecast coverage from scored observations",
        "status": "live",
        "status_label": "Current evidence",
        "metric": {"value": "84.4%", "label": "empirical coverage"},
        "as_of": "2026-09-05T17:00:00Z",
        "source": "forecast-ledger-latest.json",
        "receipt": "SHA256 92dd686d31a4e373",
        "target_url": "https://palimpsest.info/news/forecast-ledger/",
    }
    value.update(overrides)
    return value


@pytest.fixture
def draw_spy(monkeypatch):
    spy = Mock(wraps=share_cards._render_png)
    monkeypatch.setattr(share_cards, "_render_png", spy)
    return spy


def test_warm_render_preserves_exact_png_and_content_address(draw_spy):
    original = share_cards.render_card(_spec())
    with share_cards.png_render_cache() as cache:
        cold = share_cards.render_card(_spec())
        warm = share_cards.render_card(copy.deepcopy(_spec()))
        assert cold == warm == original
        assert cold is not warm
        assert warm.sha256 == hashlib.sha256(original.png).hexdigest()
        assert list(cache.entries) == [share_cards._canonical_json(cold.spec)]
        assert cache.retained_bytes == sum(
            len(key) + len(png) for key, png in cache.entries.items()
        )
    assert draw_spy.call_count == 2


def test_returned_spec_mutation_cannot_poison_later_card(draw_spy):
    with share_cards.png_render_cache():
        first = share_cards.render_card(_spec())
        original_png = first.png
        first.spec["title"] = "Untrusted replacement"
        first.spec["metric"]["value"] = "0"
        second = share_cards.render_card(_spec())
        assert second.spec == share_cards.normalize_spec(_spec())
        assert second.spec is not first.spec
        assert second.spec["metric"] is not first.spec["metric"]
        assert second.png == original_png
        assert "Untrusted replacement" not in second.alt
    assert draw_spy.call_count == 1


def test_mutating_input_requires_new_render(draw_spy):
    value = _spec()
    with share_cards.png_render_cache() as cache:
        first = share_cards.render_card(value)
        value["metric"]["value"] = "85.1%"
        second = share_cards.render_card(value)
        assert first.spec["metric"]["value"] == "84.4%"
        assert second.png != first.png
        assert len(cache.entries) == 2
    assert draw_spy.call_count == 2


def test_site_is_fresh_metadata_while_png_is_reused(draw_spy):
    with share_cards.png_render_cache():
        first = share_cards.render_card(_spec(), site="https://palimpsest.info/")
        second = share_cards.render_card(_spec(), site="https://www.palimpsest.info")
        assert first.png == second.png
        assert first.url == "https://palimpsest.info/" + first.path.as_posix()
        assert second.url == "https://www.palimpsest.info/" + second.path.as_posix()
    assert draw_spy.call_count == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "unsupported"},
        {"extra": "unrecognized field"},
        {"status": "missing"},
        {"target_url": "https://example.com/not-authorized/"},
        {"metric": {"value": "84.4%"}},
    ],
)
def test_warm_cache_never_bypasses_spec_validation(draw_spy, changes):
    with share_cards.png_render_cache():
        share_cards.render_card(_spec())
        with pytest.raises(share_cards.ShareCardError):
            share_cards.render_card(_spec(**changes))
    assert draw_spy.call_count == 1


def test_cache_keys_are_complete_canonical_specs_even_if_digests_collide(monkeypatch):
    png = share_cards.render_card(_spec()).png
    draw = Mock(return_value=png)
    monkeypatch.setattr(share_cards, "_render_png", draw)

    class CollidingDigest:
        def hexdigest(self):
            return "0" * 64

    monkeypatch.setattr(share_cards.hashlib, "sha256", lambda value: CollidingDigest())
    first_spec = _spec()
    second_spec = _spec(title="A distinct evidence specification")
    with share_cards.png_render_cache() as cache:
        share_cards.render_card(first_spec)
        share_cards.render_card(second_spec)
        assert set(cache.entries) == {
            share_cards._canonical_json(share_cards.normalize_spec(first_spec)),
            share_cards._canonical_json(share_cards.normalize_spec(second_spec)),
        }
    assert draw.call_count == 2


def test_default_is_uncached_and_nested_scopes_restore_parent(draw_spy):
    share_cards.render_card(_spec())
    share_cards.render_card(_spec())
    with share_cards.png_render_cache() as outer:
        share_cards.render_card(_spec())
        with share_cards.png_render_cache() as inner:
            share_cards.render_card(_spec())
            assert inner is not outer
        share_cards.render_card(_spec())
    share_cards.render_card(_spec())
    assert draw_spy.call_count == 5


def test_copied_contexts_share_png_but_never_mutable_metadata(draw_spy):
    with share_cards.png_render_cache() as cache:
        first = share_cards.render_card(_spec())
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(copy_context().run, share_cards.render_card, _spec())
                for _ in range(16)
            ]
            cards = [future.result() for future in futures]
        cards[0].spec["metric"]["value"] = "corrupted by caller"
        assert all(card.png == first.png for card in cards)
        assert all(card.spec == first.spec for card in cards[1:])
        assert len({id(card.spec["metric"]) for card in cards}) == len(cards)
        assert len(cache.entries) == 1
        assert cache.retained_bytes == sum(
            len(key) + len(png) for key, png in cache.entries.items()
        )
    assert draw_spy.call_count == 1


def test_entry_limit_evicts_least_recently_used_png():
    with share_cards.png_render_cache(max_entries=2, max_bytes=100) as cache:
        cache.put(b"a", b"11")
        cache.put(b"b", b"22")
        assert cache.get(b"a") == b"11"
        cache.put(b"c", b"33")
        assert cache.get(b"b") is None
        assert set(cache.entries) == {b"a", b"c"}
        assert cache.retained_bytes == 6


def test_byte_limit_counts_keys_replacements_and_oversize_bypass():
    with share_cards.png_render_cache(max_entries=10, max_bytes=6) as cache:
        cache.put(b"aa", b"1234")
        assert cache.retained_bytes == 6
        cache.put(b"aa", b"1")
        assert cache.retained_bytes == 3
        cache.put(b"bb", b"2")
        assert cache.retained_bytes == 6
        cache.put(b"oversize-key", b"3")
        assert cache.get(b"oversize-key") is None
        assert cache.retained_bytes == 6
        cache.put(b"cc", b"34")
        assert list(cache.entries) == [b"cc"]
        assert cache.retained_bytes == 4


@pytest.mark.parametrize("mutation", ["digest", "canonical", "spec_digest"])
def test_warm_cache_preserves_manifest_validation(draw_spy, mutation):
    with share_cards.png_render_cache():
        card = share_cards.render_card(_spec())
        raw = share_cards.manifest_bytes([card])
        assert share_cards.parse_manifest(raw)[0][1] == card
        document = json.loads(raw)
        if mutation == "canonical":
            invalid = json.dumps(document).encode()
        else:
            field = "sha256" if mutation == "digest" else "spec_sha256"
            document["cards"][0][field] = "0" * 64
            invalid = share_cards._canonical_json(document, pretty=True)
        with pytest.raises(share_cards.ShareCardError):
            share_cards.parse_manifest(invalid)
    assert draw_spy.call_count == 1


def test_warm_cache_does_not_hide_disk_tampering_or_missing_png(tmp_path, draw_spy):
    with share_cards.png_render_cache():
        card = share_cards.render_card(_spec())
        image_path = tmp_path / card.path
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(card.png)
        (tmp_path / share_cards.MANIFEST_PATH).write_bytes(
            share_cards.manifest_bytes([card])
        )
        assert build_newsroom._managed_share_card_inventory(root=tmp_path) == {
            card.path: True
        }
        image_path.write_bytes(card.png[:-1] + bytes([card.png[-1] ^ 1]))
        assert build_newsroom._managed_share_card_inventory(root=tmp_path) == {
            card.path: False
        }
        image_path.unlink()
        with pytest.raises(newsroom.NewsroomError, match="missing files"):
            build_newsroom._managed_share_card_inventory(root=tmp_path)
    assert draw_spy.call_count == 1
