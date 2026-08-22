"""Common Crawl / public-web differential against the human gazetteer.

Historical web text is a bulk baseline for what used to be sayable on the public
web. Palimpsest's live Common Crawl lake is currently allowlisted to reviewed
institutional hosts for financial evidence. This module is the gazetteer
differential that can run on a public URL-index export without fetching page
bodies and without contacting origin hosts.

A missing monthly capture is an archive coverage gap, never a deletion label.
A URL match is not a claim that the page still exists, and not a claim that
the term is currently being censored.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse


SCHEMA_VERSION = "palimpsest-common-crawl-gazetteer.v1"
METHOD_VERSION = 1

LIMITATIONS = (
    "Matches are substring hits in the decoded URL path and query only. Page bodies are not read.",
    "A hit in crawl C and silence in crawl D is a coverage or URL-shape fact, not a takedown.",
    "Current DDTI presence is an annotation from the sealed content-layer index, not a rate.",
    "The live Common Crawl warehouse remains finance-host allowlisted; this differential does not widen that allowlist.",
)


def match_terms(url: str, terms: Sequence[str]) -> list[str]:
    """Return gazetteer terms that appear in the decoded URL path or query."""
    if not url or not terms:
        return []
    parsed = urlparse(url)
    blob = unquote(f"{parsed.path} {parsed.query} {parsed.fragment}").lower()
    hits = []
    for term in terms:
        needle = str(term or "").strip()
        if len(needle) < 2:
            continue
        if needle.lower() in blob and needle not in hits:
            hits.append(needle)
    return hits


def build_differential(
    rows: Iterable[Mapping[str, Any]],
    *,
    gazetteer_terms: Sequence[str],
    current_ddti_terms: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Summarise URL-index rows against the gazetteer and the current DDTI list."""
    ddti = set(current_ddti_terms or ())
    by_term: dict[str, dict[str, Any]] = {}
    n_rows = 0
    n_matched_rows = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        n_rows += 1
        url = str(row.get("url") or "")
        crawl = str(row.get("crawl") or row.get("filename") or "")
        timestamp = str(row.get("timestamp") or row.get("capture_timestamp") or "")
        hits = match_terms(url, gazetteer_terms)
        if not hits:
            continue
        n_matched_rows += 1
        for term in hits:
            bucket = by_term.setdefault(
                term,
                {
                    "term": term,
                    "n_url_hits": 0,
                    "crawls": [],
                    "first_timestamp": timestamp or None,
                    "last_timestamp": timestamp or None,
                    "on_current_ddti": term in ddti,
                    "sample_url": url,
                },
            )
            bucket["n_url_hits"] += 1
            if crawl and crawl not in bucket["crawls"]:
                bucket["crawls"].append(crawl)
            if timestamp:
                if bucket["first_timestamp"] is None or timestamp < bucket["first_timestamp"]:
                    bucket["first_timestamp"] = timestamp
                if bucket["last_timestamp"] is None or timestamp > bucket["last_timestamp"]:
                    bucket["last_timestamp"] = timestamp

    terms = [by_term[key] for key in sorted(by_term)]
    terms.sort(key=lambda item: (-item["n_url_hits"], item["term"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "n_rows": n_rows,
        "n_matched_rows": n_matched_rows,
        "n_terms_hit": len(terms),
        "n_terms_also_on_ddti": sum(1 for item in terms if item["on_current_ddti"]),
        "terms": terms,
        "method": (
            "Public Common Crawl URL Index rows, gazetteer substring match on decoded "
            "path/query, optional join to the current DDTI term list. No origin fetch."
        ),
        "limitations": list(LIMITATIONS),
    }
