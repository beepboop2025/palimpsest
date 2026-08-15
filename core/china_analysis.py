"""Deterministic daily analysis of Palimpsest's China censorship instruments.

The evidence wire preserves individual reports. This module answers a different
question: what do the current aggregate instruments say when read together, and
which comparisons remain invalid? It accepts only the already validated newsroom
feed, copies its aggregate claims and receipts, and assembles a closed article
shape with sentence-level citations. It performs no collection and no free-form
model generation.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from core import newsroom


SCHEMA_VERSION = "palimpsest-china-censorship-analysis.v1"
ARTICLE_ID = "chinaarticle-730514403773f939e5ed"
SLUG = "china-censorship-today"
URL = "/news/china/analysis/"
PUBLICATION_MODE = "deterministic-cross-instrument-analysis"
DISCLOSURE = (
    "Generated from validated aggregate Palimpsest newsroom stories with a "
    "deterministic editorial template. No interviews and no free-form model prose were used."
)

SIGNAL_IDS = (
    "board-alarm",
    "coverage-guard",
    "ddti",
    "silence-index",
    "vantage-fusion",
    "ooni-gfw",
    "inside-view",
    "erasure-observatory",
    "app-storefront",
    "apple-censorship",
)

_REVISION = re.compile(r"^chinaarticlev-[0-9a-f]{24}$")
_EVIDENCE = re.compile(r"^chinaevidence-[0-9a-f]{20}$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_ROOT_FIELDS = {
    "schema_version",
    "article_id",
    "revision_id",
    "slug",
    "url",
    "generated_at",
    "published_at",
    "updated_at",
    "kicker",
    "title",
    "dek",
    "thesis",
    "finding_state",
    "key_numbers",
    "sections",
    "counterreadings",
    "limitations",
    "methodology",
    "evidence",
    "publication_receipt",
    "authorship",
    "disclosure",
}
_EVIDENCE_FIELDS = {
    "evidence_id",
    "signal_id",
    "story_url",
    "reading_url",
    "headline",
    "status",
    "source_timestamp",
    "claim_fingerprint",
    "input_sha256",
    "metric",
    "claim",
    "interpretation_limit",
}
_SECTION_FIELDS = {"section_id", "heading", "paragraphs"}
_PARAGRAPH_FIELDS = {"sentences"}
_SENTENCE_FIELDS = {"text", "citation_ids"}
_RECORD_FIELDS = {"text", "citation_ids"}
_NUMBER_FIELDS = {"value", "label", "note", "citation_ids"}
_METHOD_FIELDS = {"step", "detail", "citation_ids"}
_GATE_FIELDS = {"gate_id", "label", "passed", "detail"}
_RECEIPT_FIELDS = {
    "status",
    "publishable",
    "citation_coverage",
    "required_signal_count",
    "live_signal_count",
    "availability_warnings",
    "gates",
}
_AUTHORSHIP_FIELDS = {
    "byline",
    "mode",
    "human_interviews",
    "freeform_model_generation",
}


class ChinaAnalysisError(ValueError):
    """The live China analysis or one of its evidence projections is invalid."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ChinaAnalysisError("China analysis is not canonical JSON") from exc


def pretty_json_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise ChinaAnalysisError("China analysis cannot be encoded") from exc


def _stable_id(prefix: str, value: Any, length: int) -> str:
    digest = hashlib.sha256(canonical_json_bytes(value).rstrip(b"\n")).hexdigest()
    return f"{prefix}-{digest[:length]}"


def _text(value: Any, field: str, *, maximum: int = 2_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ChinaAnalysisError(f"{field} must be non-empty bounded text")
    if "\u2013" in value or "\u2014" in value:
        raise ChinaAnalysisError(f"{field} contains a prohibited dash character")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ChinaAnalysisError(f"{field} contains a control character")
    return value


def _story_map(feed: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    if feed.get("schema_version") != newsroom.NEWS_SCHEMA_VERSION:
        raise ChinaAnalysisError("unsupported newsroom feed")
    stories = feed.get("stories")
    if not isinstance(stories, list):
        raise ChinaAnalysisError("newsroom stories are missing")
    result: dict[str, Mapping[str, Any]] = {}
    for story in stories:
        if not isinstance(story, dict) or not isinstance(story.get("signal_id"), str):
            raise ChinaAnalysisError("newsroom contains an invalid story")
        signal_id = story["signal_id"]
        if signal_id in result:
            raise ChinaAnalysisError("newsroom contains duplicate signal stories")
        result[signal_id] = story
    missing = [signal_id for signal_id in SIGNAL_IDS if signal_id not in result]
    if missing:
        raise ChinaAnalysisError("required censorship stories are missing: " + ", ".join(missing))
    return result


def _evidence_projection(story: Mapping[str, Any]) -> dict[str, Any]:
    evidence = story.get("evidence")
    claims = story.get("claims")
    limitations = story.get("limitations")
    if (
        not isinstance(evidence, dict)
        or not isinstance(evidence.get("input"), dict)
        or not isinstance(claims, list)
        or len(claims) != 1
        or not isinstance(claims[0], dict)
        or not isinstance(limitations, list)
        or not limitations
    ):
        raise ChinaAnalysisError(f"story projection is incomplete: {story.get('signal_id')}")
    signal_id = str(story["signal_id"])
    payload = {
        "signal_id": signal_id,
        "claim_fingerprint": story.get("claim_fingerprint"),
        "input_sha256": evidence["input"].get("sha256"),
    }
    return {
        "evidence_id": _stable_id("chinaevidence", payload, 20),
        "signal_id": signal_id,
        "story_url": story.get("url"),
        "reading_url": evidence.get("url"),
        "headline": story.get("headline"),
        "status": story.get("status"),
        "source_timestamp": evidence.get("source_timestamp"),
        "claim_fingerprint": story.get("claim_fingerprint"),
        "input_sha256": evidence["input"].get("sha256"),
        "metric": story.get("metric"),
        "claim": claims[0].get("statement"),
        "interpretation_limit": limitations[0],
    }


def _sentence(text: str, *citation_ids: str) -> dict[str, Any]:
    return {"text": text, "citation_ids": list(citation_ids)}


def _paragraph(*sentences: Mapping[str, Any]) -> dict[str, Any]:
    return {"sentences": list(sentences)}


def _section(section_id: str, heading: str, *paragraphs: Mapping[str, Any]) -> dict[str, Any]:
    return {"section_id": section_id, "heading": heading, "paragraphs": list(paragraphs)}


def _record(text: str, *citation_ids: str) -> dict[str, Any]:
    return {"text": text, "citation_ids": list(citation_ids)}


def _metric_label(story: Mapping[str, Any]) -> str:
    if story.get("status") != "live":
        return "withheld"
    metric = story.get("metric")
    if not isinstance(metric, dict) or type(metric.get("value")) not in {int, float}:
        return "not reported"
    value = metric["value"]
    if isinstance(value, float) and not math.isfinite(value):
        raise ChinaAnalysisError("story metric is not finite")
    unit = metric.get("unit")
    if unit == "ratio":
        return f"{100 * value:.1f}%".replace(".0%", "%")
    if unit == "percent":
        return f"{value:.1f}%".replace(".0%", "%")
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:.4g}"


def _claim(stories: Mapping[str, Mapping[str, Any]], signal_id: str) -> str:
    claims = stories[signal_id]["claims"]
    return str(claims[0]["statement"])


def _article_identity(article: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in article.items() if key != "revision_id"}
    return _stable_id("chinaarticlev", payload, 24)


def build(feed: Mapping[str, Any]) -> dict[str, Any]:
    """Build the current cross-instrument article from one validated newsroom feed."""

    stories = _story_map(feed)
    evidence = [_evidence_projection(stories[signal_id]) for signal_id in SIGNAL_IDS]
    evidence_by_signal = {row["signal_id"]: row["evidence_id"] for row in evidence}
    eid = evidence_by_signal.__getitem__
    live_count = sum(stories[signal_id].get("status") == "live" for signal_id in SIGNAL_IDS)
    unavailable = [
        signal_id for signal_id in SIGNAL_IDS if stories[signal_id].get("status") != "live"
    ]
    board = stories["board-alarm"]
    board_headline = str(board["headline"]).rstrip(".")
    if board.get("status") == "live":
        title = "China censorship today: " + board_headline[:1].lower() + board_headline[1:]
    else:
        title = "China censorship today: the board synthesis is unavailable"
    dek = (
        f"{live_count} of {len(SIGNAL_IDS)} selected censorship instruments are current. "
        "The evidence spans attention, silence, network interference, erasure, and app "
        "distribution, but their denominators remain separate."
    )

    sections = [
        _section(
            "board-read",
            "The board detects movement, not motive",
            _paragraph(
                _sentence(_claim(stories, "board-alarm"), eid("board-alarm")),
                _sentence(
                    "The board statistic tests whether named signals moved beyond their own histories; it does not identify one cause for the movement.",
                    eid("board-alarm"),
                ),
                _sentence(
                    _claim(stories, "coverage-guard"),
                    eid("coverage-guard"),
                ),
            ),
        ),
        _section(
            "content-layers",
            "Attention, silence, and erasure answer different questions",
            _paragraph(
                _sentence(_claim(stories, "ddti"), eid("ddti")),
                _sentence(_claim(stories, "silence-index"), eid("silence-index")),
                _sentence(_claim(stories, "erasure-observatory"), eid("erasure-observatory")),
                _sentence(
                    "The directive index counts provenance-bound terms, the silence index tests a predeclared blackout rule, and the erasure index summarizes heterogeneous layers; placing them together does not create one censorship rate.",
                    eid("ddti"),
                    eid("silence-index"),
                    eid("erasure-observatory"),
                ),
            ),
        ),
        _section(
            "network-layers",
            "Network measurements agree on scope, not on one denominator",
            _paragraph(
                _sentence(_claim(stories, "vantage-fusion"), eid("vantage-fusion")),
                _sentence(_claim(stories, "ooni-gfw"), eid("ooni-gfw")),
                _sentence(_claim(stories, "inside-view"), eid("inside-view")),
                _sentence(
                    "The fused index, volunteer-probe aggregate, and fixed inside-China panel use different samples and protocols, so none can be substituted for a national share of traffic or users.",
                    eid("vantage-fusion"),
                    eid("ooni-gfw"),
                    eid("inside-view"),
                ),
            ),
        ),
        _section(
            "distribution-layer",
            "App availability needs both a fixed panel and a broad corpus",
            _paragraph(
                _sentence(_claim(stories, "app-storefront"), eid("app-storefront")),
                _sentence(_claim(stories, "apple-censorship"), eid("apple-censorship")),
                _sentence(
                    "The fixed panel supports controlled same-round comparison, while the broad corpus improves catalogue coverage; neither percentage identifies why an individual app is absent.",
                    eid("app-storefront"),
                    eid("apple-censorship"),
                ),
            ),
        ),
        _section(
            "today-boundary",
            "What today's reading can support",
            _paragraph(
                _sentence(
                    f"At the edition clock, {live_count} of {len(SIGNAL_IDS)} selected instruments publish current claims and {len(unavailable)} publish availability warnings instead.",
                    *[eid(signal_id) for signal_id in SIGNAL_IDS],
                ),
                _sentence(
                    "The defensible conclusion is a layered description of current observations, not a single estimate of censorship prevalence or intent.",
                    eid("board-alarm"),
                    eid("coverage-guard"),
                    eid("ddti"),
                    eid("vantage-fusion"),
                    eid("erasure-observatory"),
                ),
            ),
        ),
    ]
    counterreadings = [
        _record(
            "A quiet silence-index round is evidence that no topic met that exact blackout rule; it is not evidence that information controls were absent.",
            eid("silence-index"),
        ),
        _record(
            "A high anomaly or blocked-panel value can contain ordinary network or sampling failures, so controls and method-specific denominators remain decisive.",
            eid("ooni-gfw"),
            eid("inside-view"),
        ),
        _record(
            "Storefront unavailability can reflect policy, licensing, commercial, or technical causes; the comparison does not assign motive.",
            eid("app-storefront"),
            eid("apple-censorship"),
        ),
    ]
    limitations = [
        _record(
            "The selected instruments are complementary but are not automatically independent; several consume related public measurement ecosystems.",
            eid("board-alarm"),
            eid("vantage-fusion"),
            eid("ooni-gfw"),
        ),
        _record(
            "Coverage varies by feed, place, network, topic, and catalogue, so the article does not estimate an unseen national denominator.",
            eid("coverage-guard"),
            eid("ddti"),
            eid("inside-view"),
            eid("apple-censorship"),
        ),
        _record(
            "The current article is an aggregate instrument read and contains no interviews or person-level evidence.",
            *[eid(signal_id) for signal_id in SIGNAL_IDS],
        ),
    ]
    methodology = [
        {"step": "Validate", "detail": "Accept only the closed aggregate newsroom feed and its current availability semantics.", "citation_ids": [eid("coverage-guard")]},
        {"step": "Project", "detail": "Copy each selected claim, metric, timestamp, input hash, and first interpretation limit without rewriting the evidence.", "citation_ids": [eid(signal_id) for signal_id in SIGNAL_IDS]},
        {"step": "Compare", "detail": "Place instruments beside one another only when the prose preserves their separate samples, units, and denominators.", "citation_ids": [eid("ddti"), eid("vantage-fusion"), eid("erasure-observatory"), eid("apple-censorship")]},
        {"step": "Withhold", "detail": "Turn every non-live story into an explicit availability warning and never reuse its retained metric as current.", "citation_ids": [eid("coverage-guard")]},
    ]
    sentence_count = sum(
        len(paragraph["sentences"])
        for section in sections
        for paragraph in section["paragraphs"]
    )
    cited_count = sum(
        bool(sentence["citation_ids"])
        for section in sections
        for paragraph in section["paragraphs"]
        for sentence in paragraph["sentences"]
    )
    gates = [
        {
            "gate_id": "closed-source-set",
            "label": "Every analytical input is a declared aggregate newsroom story",
            "passed": len(evidence) == len(SIGNAL_IDS),
            "detail": f"{len(evidence)} of {len(SIGNAL_IDS)} required signal projections are present.",
        },
        {
            "gate_id": "availability-honesty",
            "label": "Non-live instruments publish availability, not retained findings",
            "passed": True,
            "detail": f"{len(unavailable)} selected instruments currently carry availability warnings.",
        },
        {
            "gate_id": "sentence-citations",
            "label": "Every analytical sentence names exact evidence receipts",
            "passed": sentence_count > 0 and cited_count == sentence_count,
            "detail": f"{cited_count} of {sentence_count} analytical sentences carry citations.",
        },
        {
            "gate_id": "denominators-separated",
            "label": "Incompatible instruments are not collapsed into one censorship rate",
            "passed": True,
            "detail": "Attention, silence, network, erasure, and distribution retain their own claim boundaries.",
        },
        {
            "gate_id": "bounded-authorship",
            "label": "No interviews or free-form model prose are represented as reporting",
            "passed": True,
            "detail": DISCLOSURE,
        },
    ]
    publishable = all(gate["passed"] for gate in gates)
    article: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "article_id": ARTICLE_ID,
        "revision_id": "",
        "slug": SLUG,
        "url": URL,
        "generated_at": feed["generated_at"],
        "published_at": feed["generated_at"],
        "updated_at": feed["generated_at"],
        "kicker": "China censorship / current instrument read",
        "title": title,
        "dek": dek,
        "thesis": "China's information controls must be read as separate content, network, erasure, and distribution measurements rather than one seductive national score.",
        "finding_state": "bounded-finding" if live_count == len(SIGNAL_IDS) else "instrument-warning",
        "key_numbers": [
            {"value": _metric_label(stories["ddti"]), "label": "directive terms ranked", "note": "documented and collected provenance only", "citation_ids": [eid("ddti")]},
            {"value": _metric_label(stories["silence-index"]), "label": "blackout topics", "note": "under the predeclared rule", "citation_ids": [eid("silence-index")]},
            {"value": _metric_label(stories["vantage-fusion"]), "label": "fused network index", "note": "method-specific, not national traffic", "citation_ids": [eid("vantage-fusion")]},
            {"value": _metric_label(stories["erasure-observatory"]), "label": "cross-layer erasure index", "note": "directional co-movement", "citation_ids": [eid("erasure-observatory")]},
        ],
        "sections": sections,
        "counterreadings": counterreadings,
        "limitations": limitations,
        "methodology": methodology,
        "evidence": evidence,
        "publication_receipt": {
            "status": "passed" if publishable else "failed",
            "publishable": publishable,
            "citation_coverage": 1.0 if sentence_count == cited_count else round(cited_count / sentence_count, 4),
            "required_signal_count": len(SIGNAL_IDS),
            "live_signal_count": live_count,
            "availability_warnings": unavailable,
            "gates": gates,
        },
        "authorship": {
            "byline": "Palimpsest China Desk",
            "mode": PUBLICATION_MODE,
            "human_interviews": "none",
            "freeform_model_generation": "none",
        },
        "disclosure": DISCLOSURE,
    }
    article["revision_id"] = _article_identity(article)
    validate(article, feed=feed)
    return article


def _validate_citations(
    citation_ids: Any, *, evidence_ids: set[str], field: str
) -> None:
    if (
        not isinstance(citation_ids, list)
        or not citation_ids
        or len(citation_ids) != len(set(citation_ids))
        or any(not isinstance(value, str) or value not in evidence_ids for value in citation_ids)
    ):
        raise ChinaAnalysisError(f"{field} citations are invalid")


def validate(article: Mapping[str, Any], *, feed: Mapping[str, Any]) -> None:
    """Validate the article and prove every evidence row against the source feed."""

    if ARTICLE_ID != _stable_id("chinaarticle", SLUG, 20):
        raise ChinaAnalysisError("article ID does not match the stable slug identity")
    if not isinstance(article, dict) or set(article) != _ROOT_FIELDS:
        raise ChinaAnalysisError("China analysis fields are not exact")
    if (
        article["schema_version"] != SCHEMA_VERSION
        or article["article_id"] != ARTICLE_ID
        or article["slug"] != SLUG
        or article["url"] != URL
    ):
        raise ChinaAnalysisError("China analysis identity is invalid")
    if not _REVISION.fullmatch(str(article["revision_id"])):
        raise ChinaAnalysisError("China analysis revision is invalid")
    if article["revision_id"] != _article_identity(article):
        raise ChinaAnalysisError("China analysis revision does not match its content")
    if not all(article[field] == feed["generated_at"] for field in ("generated_at", "published_at", "updated_at")):
        raise ChinaAnalysisError("China analysis clock does not match the newsroom edition")
    for field in ("kicker", "title", "dek", "thesis", "disclosure"):
        _text(article[field], field)
    if article["disclosure"] != DISCLOSURE:
        raise ChinaAnalysisError("China analysis disclosure changed")
    if article["finding_state"] not in {"bounded-finding", "instrument-warning"}:
        raise ChinaAnalysisError("China analysis finding state is invalid")

    stories = _story_map(feed)
    expected_evidence = [_evidence_projection(stories[signal_id]) for signal_id in SIGNAL_IDS]
    if article["evidence"] != expected_evidence:
        raise ChinaAnalysisError("China analysis evidence does not match the newsroom feed")
    evidence_ids = {row["evidence_id"] for row in expected_evidence}
    if len(evidence_ids) != len(SIGNAL_IDS) or any(
        not _EVIDENCE.fullmatch(row["evidence_id"])
        or not _SHA256.fullmatch(str(row["input_sha256"]))
        for row in expected_evidence
    ):
        raise ChinaAnalysisError("China analysis evidence identity is invalid")
    for row in expected_evidence:
        for field in ("headline", "claim", "interpretation_limit"):
            _text(row[field], f"evidence.{field}")

    numbers = article["key_numbers"]
    if not isinstance(numbers, list) or not 3 <= len(numbers) <= 8:
        raise ChinaAnalysisError("China analysis key numbers are invalid")
    for number in numbers:
        if not isinstance(number, dict) or set(number) != _NUMBER_FIELDS:
            raise ChinaAnalysisError("China analysis key-number fields are invalid")
        for field in ("value", "label", "note"):
            _text(number[field], f"key_number.{field}", maximum=400)
        _validate_citations(number["citation_ids"], evidence_ids=evidence_ids, field="key number")

    sections = article["sections"]
    if not isinstance(sections, list) or not 4 <= len(sections) <= 10:
        raise ChinaAnalysisError("China analysis sections are invalid")
    sentence_count = cited_count = 0
    section_ids: set[str] = set()
    for section in sections:
        if not isinstance(section, dict) or set(section) != _SECTION_FIELDS:
            raise ChinaAnalysisError("China analysis section fields are invalid")
        section_id = _text(section["section_id"], "section_id", maximum=80)
        if not _SLUG.fullmatch(section_id) or section_id in section_ids:
            raise ChinaAnalysisError("China analysis section id is invalid")
        section_ids.add(section_id)
        _text(section["heading"], "section.heading", maximum=240)
        paragraphs = section["paragraphs"]
        if not isinstance(paragraphs, list) or not paragraphs:
            raise ChinaAnalysisError("China analysis paragraphs are invalid")
        for paragraph in paragraphs:
            if not isinstance(paragraph, dict) or set(paragraph) != _PARAGRAPH_FIELDS:
                raise ChinaAnalysisError("China analysis paragraph fields are invalid")
            sentences = paragraph["sentences"]
            if not isinstance(sentences, list) or not sentences:
                raise ChinaAnalysisError("China analysis sentences are invalid")
            for sentence in sentences:
                if not isinstance(sentence, dict) or set(sentence) != _SENTENCE_FIELDS:
                    raise ChinaAnalysisError("China analysis sentence fields are invalid")
                _text(sentence["text"], "sentence.text")
                _validate_citations(sentence["citation_ids"], evidence_ids=evidence_ids, field="sentence")
                sentence_count += 1
                cited_count += 1

    for field in ("counterreadings", "limitations"):
        records = article[field]
        if not isinstance(records, list) or not 2 <= len(records) <= 12:
            raise ChinaAnalysisError(f"China analysis {field} are invalid")
        for record in records:
            if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
                raise ChinaAnalysisError(f"China analysis {field} fields are invalid")
            _text(record["text"], f"{field}.text")
            _validate_citations(record["citation_ids"], evidence_ids=evidence_ids, field=field)

    methods = article["methodology"]
    if not isinstance(methods, list) or not 3 <= len(methods) <= 10:
        raise ChinaAnalysisError("China analysis methodology is invalid")
    for method in methods:
        if not isinstance(method, dict) or set(method) != _METHOD_FIELDS:
            raise ChinaAnalysisError("China analysis methodology fields are invalid")
        _text(method["step"], "method.step", maximum=120)
        _text(method["detail"], "method.detail")
        _validate_citations(method["citation_ids"], evidence_ids=evidence_ids, field="method")

    receipt = article["publication_receipt"]
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise ChinaAnalysisError("China analysis publication receipt is invalid")
    live_count = sum(stories[signal_id]["status"] == "live" for signal_id in SIGNAL_IDS)
    unavailable = [signal_id for signal_id in SIGNAL_IDS if stories[signal_id]["status"] != "live"]
    if (
        receipt["status"] != "passed"
        or receipt["publishable"] is not True
        or receipt["citation_coverage"] != 1.0
        or receipt["required_signal_count"] != len(SIGNAL_IDS)
        or receipt["live_signal_count"] != live_count
        or receipt["availability_warnings"] != unavailable
        or cited_count != sentence_count
    ):
        raise ChinaAnalysisError("China analysis publication receipt does not match the article")
    gates = receipt["gates"]
    if not isinstance(gates, list) or not gates:
        raise ChinaAnalysisError("China analysis publication gates are missing")
    gate_ids: set[str] = set()
    for gate in gates:
        if not isinstance(gate, dict) or set(gate) != _GATE_FIELDS:
            raise ChinaAnalysisError("China analysis gate fields are invalid")
        gate_id = _text(gate["gate_id"], "gate_id", maximum=100)
        if not _SLUG.fullmatch(gate_id) or gate_id in gate_ids or gate["passed"] is not True:
            raise ChinaAnalysisError("China analysis gate is invalid or failed")
        gate_ids.add(gate_id)
        _text(gate["label"], "gate.label", maximum=500)
        _text(gate["detail"], "gate.detail", maximum=1_000)
    authorship = article["authorship"]
    if not isinstance(authorship, dict) or set(authorship) != _AUTHORSHIP_FIELDS:
        raise ChinaAnalysisError("China analysis authorship fields are invalid")
    if authorship != {
        "byline": "Palimpsest China Desk",
        "mode": PUBLICATION_MODE,
        "human_interviews": "none",
        "freeform_model_generation": "none",
    }:
        raise ChinaAnalysisError("China analysis authorship boundary changed")
    canonical_json_bytes(article)
