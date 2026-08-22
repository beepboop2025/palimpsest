"""CDT campaigns cluster by exact article URL."""
from __future__ import annotations

from processors.cdt_campaigns import cluster_ddti


def test_two_terms_on_one_url_are_a_campaign() -> None:
    ddti = {
        "ranked": [
            {
                "term": "Guo Degang",
                "threat": 0.8,
                "samples": [
                    {"title": "Comedy", "url": "https://chinadigitaltimes.net/2026/08/a/"}
                ],
            },
            {
                "term": "Cultural Revolution",
                "threat": 0.7,
                "samples": [
                    {"title": "Comedy", "url": "https://chinadigitaltimes.net/2026/08/a/"}
                ],
            },
            {
                "term": "lonely",
                "threat": 0.9,
                "samples": [
                    {"title": "Other", "url": "https://chinadigitaltimes.net/2026/08/b/"}
                ],
            },
        ]
    }
    result = cluster_ddti(ddti)
    assert result["n_campaigns"] == 1
    campaign = result["campaigns"][0]
    assert campaign["n_terms"] == 2
    assert "Guo Degang" in campaign["terms"]
    assert campaign["url"].endswith("/a/")


def test_fuzzy_titles_are_not_joined() -> None:
    ddti = {
        "ranked": [
            {
                "term": "alpha",
                "threat": 1,
                "samples": [{"title": "Same story?", "url": "https://example.com/1"}],
            },
            {
                "term": "beta",
                "threat": 1,
                "samples": [{"title": "Same story?", "url": "https://example.com/2"}],
            },
        ]
    }
    assert cluster_ddti(ddti)["n_campaigns"] == 0
