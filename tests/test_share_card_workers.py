"""Bulk workers preserve exact rendering and closed manifest admission."""
from __future__ import annotations

from copy import deepcopy

import pytest

from scripts import share_cards


def specs():
    return [{
        "schema_version": share_cards.SPEC_VERSION,
        "kind": "instrument-reading", "kicker": "Source record",
        "title": f"Record {i}: 中国 liquidity, café and an attributed report",
        "status": "live", "status_label": "Current evidence",
        "metric": {"value": str(i), "label": "source records"},
        "as_of": "2026-09-23T20:47:55Z", "source": "test reading",
        "receipt": "SHA256 test", "target_url": f"https://palimpsest.info/news/record-{i}/",
    } for i in range(65)]


@pytest.fixture(scope="module")
def corpus():
    values = specs()
    return values, [share_cards.render_card(value) for value in values]


@pytest.mark.parametrize("workers", [2, 4])
def test_real_worker_batch_is_byte_exact_and_ordered(corpus, workers):
    values, expected = corpus
    assert share_cards.render_cards(values, workers=workers) == expected
    assert share_cards.render_cards(values[::-1], workers=workers) == expected[::-1]


def test_one_worker_and_duplicate_specs_preserve_fresh_metadata(corpus):
    values, expected = corpus
    result = share_cards.render_cards([values[0], values[0], values[1]], workers=1)
    assert result == [expected[0], expected[0], expected[1]]
    result[0].spec["metric"]["value"] = "changed"
    assert result[1].spec == expected[0].spec


@pytest.mark.parametrize("workers", [0, 5, -1, True, 2.0])
def test_worker_admission_is_bounded(workers):
    with pytest.raises(ValueError, match="one to four"):
        share_cards.render_cards([], workers=workers)


def test_bulk_cache_is_invocation_local_and_does_not_skip_spec_validation(corpus, monkeypatch):
    values, expected = corpus
    with share_cards.png_render_cache() as cache:
        for value, card in zip(values, expected, strict=True):
            cache.put(share_cards._canonical_json(share_cards.normalize_spec(value)), card.png)
        def forbidden_draw(*_args, **_kwargs):
            raise AssertionError("a warm invocation should not draw again")
        monkeypatch.setattr(share_cards, "_render_png", forbidden_draw)
        assert share_cards.render_cards(values, workers=2) == expected
        invalid = deepcopy(values)
        invalid[0]["metric"] = {"value": "0"}
        with pytest.raises(share_cards.ShareCardError):
            share_cards.render_cards(invalid, workers=2)
    assert share_cards._PNG_RENDER_CACHE.get() is None


@pytest.mark.parametrize("workers", [2, 4])
def test_manifest_is_independently_reproduced_by_workers(corpus, workers):
    _, cards = corpus
    raw = share_cards.manifest_bytes(cards)
    parsed = share_cards.parse_manifest(raw, workers=workers)
    assert share_cards.manifest_bytes([card for _, card in parsed]) == raw
    assert parsed == share_cards.parse_manifest(raw, workers=1)


@pytest.mark.parametrize("case", ["digest", "byte_count", "spec_digest", "spec", "duplicate", "path"])
@pytest.mark.parametrize("workers", [1, 2, 4])
def test_manifest_corruption_still_fails_closed(corpus, case, workers):
    _, cards = corpus
    document = share_cards.manifest_document(cards)
    row = document["cards"][0]
    if case == "digest": row["sha256"] = "0" * 64
    elif case == "byte_count": row["bytes"] += 1
    elif case == "spec_digest": row["spec_sha256"] = "0" * 64
    elif case == "spec": row["spec"]["title"] = "different record"
    elif case == "duplicate": document["cards"].insert(1, deepcopy(row))
    elif case == "path": row["path"] = "../../escape.png"
    with pytest.raises(share_cards.ShareCardError):
        share_cards.validate_manifest_document(document, workers=workers)


def test_worker_render_failure_is_propagated_and_cache_remains_scoped(monkeypatch):
    def bad_png(*_args, **_kwargs):
        return b"not a png"
    monkeypatch.setattr(share_cards, "_render_png", bad_png)
    with share_cards.png_render_cache():
        with pytest.raises(share_cards.ShareCardError, match="not a PNG"):
            share_cards._render_png_worker(specs()[0])
        assert share_cards._PNG_RENDER_CACHE.get() is not None


@pytest.mark.parametrize("workers", [2, 4])
def test_pool_failure_is_propagated_without_serial_fallback(corpus, monkeypatch, workers):
    values, _ = corpus

    class FailedPool:
        def __init__(self, *, max_workers, mp_context):
            assert max_workers == workers
            assert mp_context.get_start_method() == "spawn"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def map(self, *_args, **_kwargs):
            raise RuntimeError("worker process failed")

    monkeypatch.setattr(share_cards, "ProcessPoolExecutor", FailedPool)
    with share_cards.png_render_cache():
        with pytest.raises(RuntimeError, match="worker process failed"):
            share_cards.render_cards(values, workers=workers)
    assert share_cards._PNG_RENDER_CACHE.get() is None
