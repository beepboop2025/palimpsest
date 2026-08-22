"""Deterministic per-instrument companions for newsroom stories and readings.

The daily cross-instrument article in ``core/china_analysis.py`` remains the
quality bar. This module copies one already-validated newsroom story (and the
same-edition board) into a closed brief: current number and denominator, live
versus stale/missing, other elevated layer/signal names, and what the reading
does not show. It performs no collection and no free-form model generation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from core import newsroom
from core.event_brief import FORBIDDEN_CAUSAL, causal_hits


SCHEMA_VERSION = "palimpsest-instrument-analysis.v1"
PUBLICATION_MODE = "deterministic-instrument-brief"
DISCLOSURE = (
    "Generated from one validated Palimpsest newsroom story and the same-edition "
    "board using a deterministic editorial template. No interviews and no "
    "free-form model prose were used."
)
METHOD = (
    "Copy the story claim, metric, status, first limitation, and method summary. "
    "Name other same-edition elevated layers and lead/high signal ids. "
    "Non-live stories become availability briefs; retained metrics are never "
    "republished as current. editorial_priority and prequential-robust-mad/v1 "
    "appear only as review rank or warming_up."
)
PRIVATE_SIGNALS = frozenset({"nemesis"})
READING_HTML = {
    "ooni-gfw": Path("readings/ooni-gfw.html"),
    "inside-view": Path("readings/inside-view.html"),
    "in-path-interference": Path("readings/in-path-interference.html"),
    "bleedthrough": Path("readings/bleedthrough.html"),
    "erasure-observatory": Path("readings/erasure-observatory.html"),
    "blocklist": Path("readings/blocklist.html"),
    "app-storefront": Path("readings/app-store.html"),
    "generative-firewall": Path("readings/generative-firewall-index.html"),
}

_ANALYSIS_ID = re.compile(r"^instrumentv-[0-9a-f]{24}$")
_EVIDENCE = re.compile(r"^instrumentevidence-[0-9a-f]{20}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,79}$")

_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "analysis_id",
        "signal_id",
        "story_url",
        "url",
        "reading_url",
        "reading_analysis_url",
        "generated_at",
        "disposition",
        "status",
        "position",
        "key_numbers",
        "brief",
        "counterreadings",
        "limitations",
        "elevated_peers",
        "review_rank",
        "evidence",
        "publication_receipt",
        "authorship",
        "disclosure",
        "method",
    }
)
_BRIEF_FIELDS = frozenset({"current_number", "board_context", "does_not_show"})
_LAYER_FIELDS = frozenset({"status", "sentences"})
_SENTENCE_FIELDS = frozenset({"text", "citation_ids"})
_RECORD_FIELDS = frozenset({"text", "citation_ids"})
_NUMBER_FIELDS = frozenset({"value", "label", "note", "citation_ids"})
_PEER_FIELDS = frozenset({"signal_id", "section", "status", "priority"})
_REVIEW_FIELDS = frozenset(
    {
        "editorial_priority",
        "editorial_priority_role",
        "anomaly_state",
        "anomaly_score_published",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {
        "evidence_id",
        "signal_id",
        "kind",
        "headline",
        "status",
        "claim",
        "story_url",
        "reading_url",
        "source_timestamp",
        "input_sha256",
        "interpretation_limit",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "status",
        "publishable",
        "automatic_publication",
        "citation_coverage",
        "human_review_required",
        "availability_warnings",
        "gates",
    }
)
_GATE_FIELDS = frozenset({"gate_id", "label", "passed", "detail"})
_AUTHORSHIP_FIELDS = frozenset(
    {"byline", "mode", "human_interviews", "freeform_model_generation"}
)
_DISPOSITIONS = frozenset({"live-reading", "availability-brief"})
_STATUSES = frozenset({"live", "degraded", "stale", "missing", "corrupt"})


class InstrumentAnalysisError(ValueError):
    """The instrument companion violates its closed evidence contract."""


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
        raise InstrumentAnalysisError("instrument analysis is not canonical JSON") from exc


def pretty_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InstrumentAnalysisError("instrument analysis cannot be encoded") from exc


def _stable_id(prefix: str, value: Any, length: int) -> str:
    digest = hashlib.sha256(canonical_json_bytes(value).rstrip(b"\n")).hexdigest()
    return f"{prefix}-{digest[:length]}"


def _ascii(value: str) -> str:
    return value.replace("\u2013", "-").replace("\u2014", "-")


def reading_stem(story: Mapping[str, Any]) -> str:
    filename = story.get("evidence", {}).get("input", {}).get("filename")
    if type(filename) is str and filename.endswith("-latest.json"):
        return filename[: -len("-latest.json")]
    signal_id = str(story.get("signal_id") or "instrument")
    return signal_id


def reading_analysis_relpath(story: Mapping[str, Any]) -> Path:
    return Path("readings") / f"{reading_stem(story)}-analysis.json"


def _exact(value: Any, fields: frozenset[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != fields:
        missing = sorted(fields - set(value)) if isinstance(value, Mapping) else sorted(fields)
        extra = sorted(set(value) - fields) if isinstance(value, Mapping) else []
        raise InstrumentAnalysisError(f"{path} fields differ (missing={missing}, extra={extra})")
    return value


def _sentence(text: str, *citation_ids: str) -> dict[str, Any]:
    return {"text": _ascii(text), "citation_ids": list(citation_ids)}


def _record(text: str, *citation_ids: str) -> dict[str, Any]:
    return {"text": _ascii(text), "citation_ids": list(citation_ids)}


def _layer(status: str, *sentences: Mapping[str, Any]) -> dict[str, Any]:
    return {"status": status, "sentences": list(sentences)}


def _metric_phrase(story: Mapping[str, Any], *, live: bool) -> tuple[str, str]:
    metric = story.get("metric") if type(story.get("metric")) is dict else {}
    value = metric.get("value")
    label = metric.get("label") or "headline metric"
    unit = metric.get("unit")
    denominator = metric.get("denominator") if type(metric.get("denominator")) is dict else {}
    denom_label = denominator.get("label") or "declared denominator"
    denom_value = denominator.get("value")
    if not live or value is None:
        return "withheld", f"{denom_label} withheld with the non-live metric"
    if type(value) is float and not math.isfinite(value):
        raise InstrumentAnalysisError("story metric is not finite")
    if unit == "percent":
        number = f"{value:.1f}%".replace(".0%", "%")
    elif unit == "ratio" and type(value) in {int, float}:
        number = f"{100 * value:.1f}%".replace(".0%", "%")
    elif type(value) is int:
        number = f"{value:,}"
    else:
        number = f"{value:.4g}"
    if denom_value is None:
        denom = f"{denom_label} not reported"
    elif type(denom_value) is int:
        denom = f"{denom_value:,} {denom_label}"
    else:
        denom = f"{denom_value} {denom_label}"
    return number, denom


def _elevated_layers(feed: Mapping[str, Any]) -> list[str]:
    headline = str(feed.get("headline") or "")
    multi = re.search(
        r"MULTI-LAYER CO-MOVEMENT:\s*([a-z0-9_ +]+)\s+elevated together",
        headline,
        flags=re.IGNORECASE,
    )
    if multi:
        return [
            part.strip().replace("_", "-")
            for part in multi.group(1).split("+")
            if part.strip()
        ]
    single = re.search(r"single layer elevated:\s*([a-z0-9_-]+)", headline, flags=re.IGNORECASE)
    if single:
        return [single.group(1).replace("_", "-")]
    return []


def _elevated_peers(
    story: Mapping[str, Any], feed: Mapping[str, Any]
) -> list[dict[str, str]]:
    related = set(story.get("related_signal_ids") or [])
    peers: list[dict[str, str]] = []
    for other in feed.get("stories") or []:
        if type(other) is not dict or other.get("signal_id") == story.get("signal_id"):
            continue
        if other.get("signal_id") in PRIVATE_SIGNALS:
            continue
        priority = other.get("priority")
        status = other.get("status")
        if status == "live" and (
            priority in {"lead", "high"} or other.get("signal_id") in related
        ):
            peers.append(
                {
                    "signal_id": str(other["signal_id"]),
                    "section": str(other.get("section") or "unspecified"),
                    "status": str(status),
                    "priority": str(priority or "background"),
                }
            )
    return peers[:16]


def _review_rank(
    story: Mapping[str, Any], reading: Mapping[str, Any] | None
) -> dict[str, Any]:
    priority = None
    state = None
    for source in (story, reading or {}):
        if type(source) is not dict:
            continue
        raw = source.get("editorial_priority")
        if type(raw) in {int, float} and math.isfinite(raw):
            priority = raw
        if source.get("anomaly_state") == "warming_up":
            state = "warming_up"
        features = source.get("model_features")
        if type(features) is dict and features.get("anomaly_state") == "warming_up":
            state = "warming_up"
    return {
        "editorial_priority": priority,
        "editorial_priority_role": "review-rank-only",
        "anomaly_state": state,
        "anomaly_score_published": False,
    }


def _story_claim(story: Mapping[str, Any]) -> str:
    claims = story.get("claims")
    if not isinstance(claims, list) or not claims or type(claims[0]) is not dict:
        raise InstrumentAnalysisError("story claim is missing")
    return _ascii(str(claims[0].get("statement") or ""))


def _first_limitation(story: Mapping[str, Any]) -> str:
    limits = story.get("limitations")
    if not isinstance(limits, list) or not limits or type(limits[0]) is not str:
        raise InstrumentAnalysisError("story limitation is missing")
    return _ascii(limits[0])


def build_instrument_analysis(
    story: Mapping[str, Any],
    feed: Mapping[str, Any],
    *,
    reading: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one cited companion from a validated newsroom story only."""

    if feed.get("schema_version") != newsroom.NEWS_SCHEMA_VERSION:
        raise InstrumentAnalysisError("unsupported newsroom feed")
    signal_id = story.get("signal_id")
    if type(signal_id) is not str or _IDENTIFIER.fullmatch(signal_id) is None:
        raise InstrumentAnalysisError("story signal_id is invalid")
    status = story.get("status")
    if status not in _STATUSES:
        raise InstrumentAnalysisError("story status is invalid")
    private = signal_id in PRIVATE_SIGNALS
    live = status == "live" and not private
    disposition = "live-reading" if live else "availability-brief"
    number, denom = _metric_phrase(story, live=live)
    claim = _story_claim(story)
    limitation = _first_limitation(story)
    layers = _elevated_layers(feed)
    peers = _elevated_peers(story, feed)
    review = _review_rank(story, reading)
    evidence_row = {
        "kind": "newsroom-story",
        "signal_id": signal_id,
        "headline": story.get("headline"),
        "status": status,
        "claim": claim,
        "story_url": story.get("url"),
        "reading_url": (story.get("evidence") or {}).get("url"),
        "source_timestamp": (story.get("evidence") or {}).get("source_timestamp"),
        "input_sha256": ((story.get("evidence") or {}).get("input") or {}).get("sha256"),
        "interpretation_limit": limitation,
    }
    evidence_id = _stable_id("instrumentevidence", evidence_row, 20)
    evidence = [{"evidence_id": evidence_id, **evidence_row}]
    if live:
        current = (
            f"The current {story.get('metric', {}).get('label') or 'headline metric'} "
            f"is {number} over {denom}."
        )
        position = (
            f"Palimpsest's view: {signal_id} is live. The current number is {number} "
            f"over {denom}. This is the instrument reading, not a national "
            "censorship rate."
        )
        current_status = "present"
    else:
        current = (
            f"No current number is published for {signal_id} because the newsroom "
            f"status is {status}. The retained metric is withheld."
        )
        position = (
            f"Palimpsest withholds a current finding for {signal_id}: the source "
            f"status is {status}. This is an availability brief, not a measurement."
        )
        current_status = "abstained"
    layer_names = ", ".join(layers) if layers else "none declared"
    peer_names = ", ".join(row["signal_id"] for row in peers) or "none"
    board = (
        f"Same-edition elevated layers: {layer_names}. "
        f"Other live lead or high signal ids: {peer_names}."
    )
    if review["anomaly_state"] == "warming_up":
        review_sentence = (
            "prequential-robust-mad/v1 remains warming_up; no anomaly score is published."
        )
    elif review["editorial_priority"] is not None:
        review_sentence = (
            f"editorial_priority {review['editorial_priority']} is a review rank only, "
            "not a finding."
        )
    else:
        review_sentence = (
            "No editorial_priority or MAD score is treated as a finding on this reading."
        )
    method_summary = _ascii(str((story.get("method") or {}).get("summary") or "Declared method."))
    brief = {
        "current_number": _layer(
            current_status,
            _sentence(current, evidence_id),
            _sentence(_ascii(claim), evidence_id),
        ),
        "board_context": _layer(
            "present",
            _sentence(board, evidence_id),
            _sentence(review_sentence, evidence_id),
        ),
        "does_not_show": _layer(
            "present",
            _sentence(limitation, evidence_id),
            _sentence(
                "The brief does not assign motive, identify a person, merge unlike "
                "denominators, or treat a missing reading as a zero.",
                evidence_id,
            ),
            _sentence(method_summary, evidence_id),
        ),
    }
    counterreadings = [
        _record(
            "A current or withheld instrument value is not independent corroboration "
            "of any publisher article, and it is not a cause.",
            evidence_id,
        ),
        _record(
            "Co-movement with another live signal on this edition is layer context, "
            "not a shared rate or a coordinated action.",
            evidence_id,
        ),
    ]
    limitations = [
        _record(limitation, evidence_id),
        _record(
            "The companion copies newsroom templates only; it adds no interview, "
            "article body, or generative prose.",
            evidence_id,
        ),
    ]
    sentence_nodes = [
        sentence
        for layer in brief.values()
        for sentence in layer["sentences"]
    ]
    cited = sum(bool(row["citation_ids"]) for row in sentence_nodes)
    gates = [
        {
            "gate_id": "closed-source-set",
            "label": "Every analytical input is the validated newsroom story",
            "passed": True,
            "detail": "The companion projects one story and the same-edition board names.",
        },
        {
            "gate_id": "availability-honesty",
            "label": "Non-live instruments publish availability, not retained findings",
            "passed": live or number == "withheld",
            "detail": f"Story status is {status}; metric publication is {number}.",
        },
        {
            "gate_id": "sentence-citations",
            "label": "Every analytical sentence names exact evidence receipts",
            "passed": sentence_nodes and cited == len(sentence_nodes),
            "detail": f"{cited} of {len(sentence_nodes)} analytical sentences carry citations.",
        },
        {
            "gate_id": "denominators-separated",
            "label": "This instrument is not collapsed into one censorship rate",
            "passed": True,
            "detail": "The brief names this signal's own denominator only.",
        },
        {
            "gate_id": "bounded-authorship",
            "label": "No interviews or free-form model prose are represented as reporting",
            "passed": True,
            "detail": DISCLOSURE,
        },
        {
            "gate_id": "human-review-policy",
            "label": "Human review remains required; automatic publication stays prohibited",
            "passed": True,
            "detail": "The existing human-review and causal-language policy still holds.",
        },
    ]
    publishable = all(gate["passed"] for gate in gates)
    story_url = str(story.get("url") or "")
    reading_url = str((story.get("evidence") or {}).get("url") or "")
    analysis_url = story_url.rstrip("/") + "/analysis.json" if story_url else ""
    reading_analysis_url = (
        f"https://palimpsest.info/{reading_analysis_relpath(story).as_posix()}"
    )
    core = {
        "schema_version": SCHEMA_VERSION,
        "signal_id": signal_id,
        "story_url": story_url,
        "url": analysis_url,
        "reading_url": reading_url,
        "reading_analysis_url": reading_analysis_url,
        "generated_at": story.get("modified_at") or feed.get("generated_at"),
        "disposition": disposition,
        "status": status,
        "position": _ascii(position),
        "key_numbers": [
            {
                "value": number,
                "label": (story.get("metric") or {}).get("label") or "headline metric",
                "note": denom if live else "withheld because the source is not live",
                "citation_ids": [evidence_id],
            }
        ],
        "brief": brief,
        "counterreadings": counterreadings,
        "limitations": limitations,
        "elevated_peers": peers,
        "review_rank": review,
        "evidence": evidence,
        "publication_receipt": {
            "status": "passed" if publishable else "failed",
            "publishable": publishable,
            "automatic_publication": False,
            "citation_coverage": 1.0 if sentence_nodes and cited == len(sentence_nodes) else 0.0,
            "human_review_required": True,
            "availability_warnings": [] if live else [signal_id],
            "gates": gates,
        },
        "authorship": {
            "byline": "Palimpsest China Desk",
            "mode": PUBLICATION_MODE,
            "human_interviews": "none",
            "freeform_model_generation": "none",
        },
        "disclosure": DISCLOSURE,
        "method": METHOD,
    }
    analysis = {
        "schema_version": SCHEMA_VERSION,
        "analysis_id": _stable_id("instrumentv", core, 24),
        **{key: value for key, value in core.items() if key != "schema_version"},
    }
    validate_instrument_analysis(analysis, story=story)
    return analysis


def build_instrument_analyses(
    feed: Mapping[str, Any],
    *,
    readings: Mapping[str, Mapping[str, Any] | None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return one companion for every public newsroom story."""

    if feed.get("schema_version") != newsroom.NEWS_SCHEMA_VERSION:
        raise InstrumentAnalysisError("unsupported newsroom feed")
    stories = feed.get("stories")
    if type(stories) is not list or not stories:
        raise InstrumentAnalysisError("newsroom stories are missing")
    extras = readings or {}
    analyses = {
        story["signal_id"]: build_instrument_analysis(
            story, feed, reading=extras.get(story["signal_id"])
        )
        for story in stories
        if type(story) is dict and type(story.get("signal_id")) is str
    }
    if set(analyses) != {story["signal_id"] for story in stories}:
        raise InstrumentAnalysisError("instrument analysis output does not account for every story")
    return analyses


def validate_instrument_analysis(
    analysis: Any, *, story: Mapping[str, Any] | None = None
) -> None:
    document = _exact(analysis, _ROOT_FIELDS, "analysis")
    if document["schema_version"] != SCHEMA_VERSION:
        raise InstrumentAnalysisError("analysis.schema_version is unsupported")
    if type(document["analysis_id"]) is not str or _ANALYSIS_ID.fullmatch(document["analysis_id"]) is None:
        raise InstrumentAnalysisError("analysis.analysis_id is invalid")
    if type(document["signal_id"]) is not str or _IDENTIFIER.fullmatch(document["signal_id"]) is None:
        raise InstrumentAnalysisError("analysis.signal_id is invalid")
    if document["disposition"] not in _DISPOSITIONS:
        raise InstrumentAnalysisError("analysis.disposition is invalid")
    if document["status"] not in _STATUSES:
        raise InstrumentAnalysisError("analysis.status is invalid")
    if document["status"] != "live" and document["disposition"] != "availability-brief":
        raise InstrumentAnalysisError("non-live stories must emit an availability brief")
    if document["signal_id"] in PRIVATE_SIGNALS and document["disposition"] != "availability-brief":
        raise InstrumentAnalysisError("private signals may not publish a live finding")
    for field in ("position", "disclosure", "method"):
        if type(document[field]) is not str or not document[field].strip():
            raise InstrumentAnalysisError(f"analysis.{field} is invalid")
    if document["disclosure"] != DISCLOSURE or document["method"] != METHOD:
        raise InstrumentAnalysisError("analysis authorship templates drifted")
    evidence = document["evidence"]
    if type(evidence) is not list or len(evidence) != 1:
        raise InstrumentAnalysisError("analysis.evidence is invalid")
    row = _exact(evidence[0], _EVIDENCE_FIELDS, "analysis.evidence[0]")
    if type(row["evidence_id"]) is not str or _EVIDENCE.fullmatch(row["evidence_id"]) is None:
        raise InstrumentAnalysisError("analysis.evidence_id is invalid")
    if row["input_sha256"] is not None and (
        type(row["input_sha256"]) is not str or _SHA256.fullmatch(row["input_sha256"]) is None
    ):
        raise InstrumentAnalysisError("analysis.evidence input hash is invalid")
    if row["source_timestamp"] is not None and (
        type(row["source_timestamp"]) is not str
        or _TIMESTAMP.fullmatch(row["source_timestamp"]) is None
    ):
        raise InstrumentAnalysisError("analysis.evidence timestamp is invalid")
    evidence_ids = {row["evidence_id"]}
    brief = _exact(document["brief"], _BRIEF_FIELDS, "analysis.brief")
    sentence_count = 0
    for name, layer in brief.items():
        block = _exact(layer, _LAYER_FIELDS, f"analysis.brief.{name}")
        if block["status"] not in {"present", "abstained"}:
            raise InstrumentAnalysisError(f"analysis.brief.{name}.status is invalid")
        if type(block["sentences"]) is not list or not block["sentences"]:
            raise InstrumentAnalysisError(f"analysis.brief.{name}.sentences is invalid")
        for index, sentence in enumerate(block["sentences"]):
            item = _exact(sentence, _SENTENCE_FIELDS, f"analysis.brief.{name}.sentences[{index}]")
            if (
                type(item["citation_ids"]) is not list
                or not item["citation_ids"]
                or any(value not in evidence_ids for value in item["citation_ids"])
            ):
                raise InstrumentAnalysisError(f"analysis.brief.{name}.sentences[{index}] citations are invalid")
            sentence_count += 1
    numbers = document["key_numbers"]
    if type(numbers) is not list or not 1 <= len(numbers) <= 4:
        raise InstrumentAnalysisError("analysis.key_numbers is invalid")
    for index, number in enumerate(numbers):
        item = _exact(number, _NUMBER_FIELDS, f"analysis.key_numbers[{index}]")
        if any(value not in evidence_ids for value in item["citation_ids"]):
            raise InstrumentAnalysisError("analysis.key_numbers citations are invalid")
        if document["disposition"] == "availability-brief" and item["value"] != "withheld":
            raise InstrumentAnalysisError("availability brief republished a non-live metric")
    for field in ("counterreadings", "limitations"):
        records = document[field]
        if type(records) is not list or not 1 <= len(records) <= 8:
            raise InstrumentAnalysisError(f"analysis.{field} is invalid")
        for index, record in enumerate(records):
            item = _exact(record, _RECORD_FIELDS, f"analysis.{field}[{index}]")
            if any(value not in evidence_ids for value in item["citation_ids"]):
                raise InstrumentAnalysisError(f"analysis.{field} citations are invalid")
    peers = document["elevated_peers"]
    if type(peers) is not list or len(peers) > 16:
        raise InstrumentAnalysisError("analysis.elevated_peers is invalid")
    for index, peer in enumerate(peers):
        item = _exact(peer, _PEER_FIELDS, f"analysis.elevated_peers[{index}]")
        if item["signal_id"] == document["signal_id"]:
            raise InstrumentAnalysisError("analysis.elevated_peers includes self")
    review = _exact(document["review_rank"], _REVIEW_FIELDS, "analysis.review_rank")
    if review["editorial_priority_role"] != "review-rank-only":
        raise InstrumentAnalysisError("editorial_priority is not marked review-rank-only")
    if review["anomaly_score_published"] is not False:
        raise InstrumentAnalysisError("instrument analysis published a MAD score")
    if review["anomaly_state"] not in {None, "warming_up"}:
        raise InstrumentAnalysisError("analysis.review_rank.anomaly_state is invalid")
    receipt = _exact(document["publication_receipt"], _RECEIPT_FIELDS, "analysis.publication_receipt")
    if receipt["automatic_publication"] is not False or receipt["human_review_required"] is not True:
        raise InstrumentAnalysisError("analysis publication policy drifted")
    if receipt["citation_coverage"] != 1.0:
        raise InstrumentAnalysisError("analysis citation coverage is not complete")
    authorship = _exact(document["authorship"], _AUTHORSHIP_FIELDS, "analysis.authorship")
    if authorship["freeform_model_generation"] != "none":
        raise InstrumentAnalysisError("analysis authorship boundary changed")
    if story is not None:
        if document["signal_id"] != story["signal_id"] or document["status"] != story["status"]:
            raise InstrumentAnalysisError("analysis does not match its story")
        if document["url"] != str(story["url"]).rstrip("/") + "/analysis.json":
            raise InstrumentAnalysisError("analysis.url is not the story sibling")
    hits = causal_hits(
        {
            "position": document["position"],
            "brief": document["brief"],
            "counterreadings": document["counterreadings"],
            "limitations": document["limitations"],
        }
    )
    extra = [token for token in FORBIDDEN_CAUSAL if token in json.dumps(document).casefold()]
    if hits or extra:
        raise InstrumentAnalysisError(
            "analysis emits forbidden causal language: " + ", ".join(hits or extra)
        )
    seed = {key: value for key, value in document.items() if key != "analysis_id"}
    expected = _stable_id("instrumentv", seed, 24)
    if document["analysis_id"] != expected:
        raise InstrumentAnalysisError("analysis.analysis_id does not match its content")


__all__ = [
    "DISCLOSURE",
    "METHOD",
    "PRIVATE_SIGNALS",
    "READING_HTML",
    "SCHEMA_VERSION",
    "InstrumentAnalysisError",
    "build_instrument_analyses",
    "build_instrument_analysis",
    "canonical_json_bytes",
    "pretty_json_bytes",
    "reading_analysis_relpath",
    "validate_instrument_analysis",
]
