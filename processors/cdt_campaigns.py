"""CDT campaign clustering from a sealed DDTI reading.

A campaign here is not a motive claim. It is a CDT article URL that carries two
or more gazetteer or tag terms in the current DDTI window. That is a clustering
of the numerator, not a census of coordinated operations.

Exact URL match only. Title similarity is not used: fuzzy matching would invent
joins the source did not make.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence


MAX_CAMPAIGNS = 20
MIN_TERMS = 2


def cluster_ddti(ddti: Mapping[str, Any], *, top_n: int = MAX_CAMPAIGNS) -> dict[str, Any]:
    """Group current DDTI ranked terms by the CDT article URLs they cite."""
    ranked = ddti.get("ranked")
    if not isinstance(ranked, list):
        return {
            "n_campaigns": 0,
            "campaigns": [],
            "abstention": {
                "code": "unreadable",
                "reason": "DDTI reading has no ranked list",
            },
        }

    by_url: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"terms": [], "title": None, "max_threat": 0.0, "url": None}
    )
    for row in ranked:
        if not isinstance(row, Mapping):
            continue
        term = str(row.get("term") or "").strip()
        threat = row.get("threat")
        samples = row.get("samples")
        if not term or not isinstance(samples, list):
            continue
        for sample in samples:
            if not isinstance(sample, Mapping):
                continue
            url = str(sample.get("url") or "").strip()
            if not url.startswith("https://"):
                continue
            bucket = by_url[url]
            bucket["url"] = url
            title = str(sample.get("title") or "").strip()
            if title and not bucket["title"]:
                bucket["title"] = title
            if term not in bucket["terms"]:
                bucket["terms"].append(term)
            if isinstance(threat, (int, float)) and float(threat) > bucket["max_threat"]:
                bucket["max_threat"] = float(threat)

    campaigns = []
    for bucket in by_url.values():
        if len(bucket["terms"]) < MIN_TERMS:
            continue
        campaigns.append(
            {
                "url": bucket["url"],
                "title": bucket["title"],
                "n_terms": len(bucket["terms"]),
                "terms": sorted(bucket["terms"]),
                "max_threat": round(bucket["max_threat"], 4),
            }
        )
    campaigns.sort(key=lambda item: (-item["max_threat"], -item["n_terms"], item["url"]))
    trimmed = campaigns[:top_n]
    return {
        "n_campaigns": len(trimmed),
        "n_articles_with_multiple_terms": len(campaigns),
        "campaigns": trimmed,
        "abstention": None,
        "method": (
            "exact CDT article URL; a campaign is an article that names two or more "
            "current DDTI terms. Title fuzzy-matching is refused."
        ),
        "limitations": [
            "CDT editorial packaging can place several topics in one article; that is a "
            "source structure, not proof of a single directive.",
            "Terms that never share a URL are not clustered, even if they are related.",
        ],
    }


def campaigns_from_ranked(ranked: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return cluster_ddti({"ranked": list(ranked)})
