"""Closed metadata publication contract for the official-document watch."""
from __future__ import annotations

from collections import Counter

from collectors.china_publication_watch import HASH, METHOD_VERSION, timestamp, url_policy

SCHEMA = "palimpsest.china-publication-watch.v1"
LIMITATIONS = [
    "Changes and availability are observations from one outside-China collection vantage; they do not establish censorship, motive or an actor.",
    "Indexes rotate normally. A link leaving an index does not establish that its document was removed.",
    "Repeated 404/410 responses identify observed URL absence after a successful capture; access limits, redirects and transport errors stay separate.",
    "Numeric-token changes are document differences, not normalized economic series revisions or evidence of fabricated statistics.",
    "Historical methodology cases are attributed official announcements captured later; they are not changes first observed by this watch.",
    "Public output contains original acquisition metadata and editorial summaries; source bodies and numeric tokens remain in the private evidence store.",
]
PUBLIC_KEYS = frozenset({"id", "title", "url", "publisher", "source_group", "kind", "topics", "availability", "event", "checked_at", "first_seen_at", "last_success_at", "raw_sha256", "text_sha256", "previous_text_sha256", "content_characters", "numeric_tokens", "index_links", "change", "capture_id", "last_success_capture_id", "http_status", "not_found_count", "first_not_found_at"})
AVAILABILITY = {"available", "transport_error", "not_found", "access_limited", "redirected", "http_error", "content_unverified"}
EVENTS = {"baseline", "unchanged", "index_updated", "document_revised", "recovered", "recovered_changed", "removal_pending", "removal_observed", "unavailable"}


def build_document(rows: list[dict], config: dict, *, generated_at: str, retained_captures: int) -> dict:
    by_id = {row["id"]: row for row in rows}
    cases = []
    for declared in config.get("methodology_cases", []):
        case = {key: declared[key] for key in ("id", "title", "event_date", "claim", "interpretation", "source_ids")}
        sources = [by_id[s] for s in case["source_ids"]]
        case["source_urls"] = [s["url"] for s in sources]
        case["evidence_status"] = "captured" if all(s["last_success_at"] for s in sources) else "source_unavailable"
        case["capture_ids"] = [s["last_success_capture_id"] for s in sources]
        cases.append(case)
    counts = Counter(r["event"] for r in rows)
    available = sum(r["availability"] == "available" for r in rows)
    document = {"schema": SCHEMA, "generated_at": generated_at, "method_version": METHOD_VERSION,
                "status": "available" if available == len(rows) else "partial" if available else "unavailable",
                "coverage": {"watched_documents": len(rows), "available_documents": available,
                             "source_groups": len({r["source_group"] for r in rows}),
                             "available_source_groups": len({r["source_group"] for r in rows if r["availability"] == "available"}),
                             "retained_documents": sum(bool(r["last_success_at"]) for r in rows),
                             "retained_source_groups": len({r["source_group"] for r in rows if r["last_success_at"]}),
                             "baseline_documents": counts["baseline"],
                             "changed_documents": counts["document_revised"] + counts["recovered_changed"],
                             "updated_indexes": counts["index_updated"],
                             "unavailable_documents": len(rows) - available,
                             "removal_observations": counts["removal_observed"],
                             "retained_captures": retained_captures,
                             "captured_characters": sum(r["content_characters"] for r in rows),
                             "captured_numeric_tokens": sum(r["numeric_tokens"] for r in rows)},
                "documents": rows, "methodology_cases": cases, "limitations": LIMITATIONS}
    validate_document(document)
    return document


def validate_document(document: dict) -> None:
    if set(document) != {"schema", "generated_at", "method_version", "status", "coverage", "documents", "methodology_cases", "limitations"}:
        raise ValueError("publication watch envelope has unknown/missing fields")
    if document["schema"] != SCHEMA or document["method_version"] != METHOD_VERSION or document["limitations"] != LIMITATIONS:
        raise ValueError("publication watch method contract changed")
    generated = timestamp(document["generated_at"])
    rows = document["documents"]
    if not 1 <= len(rows) <= 150 or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("invalid publication watch row count")
    for row in rows:
        if set(row) != PUBLIC_KEYS:
            raise ValueError("publication watch row includes private or unexpected fields")
        url_policy(row["url"])
        if row["availability"] not in AVAILABILITY or row["event"] not in EVENTS:
            raise ValueError("unknown availability or event class")
        for key in ("id", "title", "publisher", "source_group"):
            if not isinstance(row[key], str) or not 1 <= len(row[key]) <= 240:
                raise ValueError("invalid public document metadata")
        if row["kind"] not in {"document", "index"}:
            raise ValueError("unknown document kind")
        for key in ("content_characters", "numeric_tokens", "index_links", "not_found_count"):
            if type(row[key]) is not int or not 0 <= row[key] <= 10_000_000:
                raise ValueError("invalid public document count")
        if set(row["change"]) != {"numeric_tokens_added", "numeric_tokens_removed", "links_added", "links_removed"} or any(type(v) is not int or not 0 <= v <= 10_000_000 for v in row["change"].values()):
            raise ValueError("invalid document change counts")
        for key in ("checked_at", "first_seen_at", "last_success_at", "first_not_found_at"):
            if row[key] is not None and timestamp(row[key]) > generated:
                raise ValueError("watch clock is in the future")
        for key in ("raw_sha256", "text_sha256", "previous_text_sha256", "capture_id", "last_success_capture_id"):
            if row[key] is not None and not HASH.fullmatch(row[key]):
                raise ValueError("invalid capture digest")
        if row["availability"] == "available" and (row["http_status"] != 200 or not row["text_sha256"] or row["last_success_at"] != row["checked_at"]):
            raise ValueError("available page lacks a successful observation")
        if row["event"] == "removal_observed":
            if row["http_status"] not in (404, 410) or not row["last_success_at"] or row["not_found_count"] < 2 or not row["first_not_found_at"]:
                raise ValueError("removal observation lacks repeated absence evidence")
            if (timestamp(row["checked_at"]) - timestamp(row["first_not_found_at"])).total_seconds() < 3600:
                raise ValueError("removal checks were too close together")
    if document["coverage"]["watched_documents"] != len(rows) or document["coverage"]["available_documents"] != sum(r["availability"] == "available" for r in rows):
        raise ValueError("publication watch coverage does not match its rows")
    if any(type(v) is not int or v < 0 for v in document["coverage"].values()):
        raise ValueError("invalid publication watch aggregate count")
    if document["status"] not in {"available", "partial", "unavailable"}:
        raise ValueError("unknown watch status")
    for case in document["methodology_cases"]:
        if set(case) != {"id", "title", "event_date", "claim", "interpretation", "source_ids", "source_urls", "evidence_status", "capture_ids"}:
            raise ValueError("unexpected methodology case fields")
        expected = [next((r for r in rows if r["id"] == source), None) for source in case["source_ids"]]
        if not expected or any(r is None for r in expected) or case["source_urls"] != [r["url"] for r in expected]:
            raise ValueError("methodology source identity mismatch")
        if case["capture_ids"] != [r["last_success_capture_id"] for r in expected]:
            raise ValueError("methodology capture does not bind its sources")
        if case["evidence_status"] != ("captured" if all(r["last_success_at"] for r in expected) else "source_unavailable"):
            raise ValueError("methodology capture status contradicts its sources")
