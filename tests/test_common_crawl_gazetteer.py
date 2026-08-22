"""Common Crawl gazetteer differential: URL hits, never a deletion label."""
from __future__ import annotations

from processors.common_crawl_gazetteer import build_differential, match_terms


def test_decoded_path_matches_zh_term() -> None:
    url = "https://example.org/wiki/%E5%85%AD%E5%9B%9B"
    assert "六四" in match_terms(url, ["六四", "白纸"])


def test_missing_capture_is_not_a_deletion() -> None:
    rows = [
        {
            "url": "https://example.org/topic/白纸",
            "crawl": "CC-MAIN-2022-05",
            "timestamp": "20220101120000",
        }
    ]
    result = build_differential(rows, gazetteer_terms=["白纸", "六四"], current_ddti_terms=["白纸"])
    assert result["n_matched_rows"] == 1
    assert result["terms"][0]["on_current_ddti"] is True
    assert "never a deletion" in " ".join(result["limitations"]).lower() or "not a takedown" in " ".join(
        result["limitations"]
    ).lower()
    assert result["n_rows"] == 1


def test_unrelated_url_is_not_a_hit() -> None:
    rows = [{"url": "https://example.org/about", "crawl": "CC-MAIN-2022-05"}]
    result = build_differential(rows, gazetteer_terms=["六四"])
    assert result["n_terms_hit"] == 0
