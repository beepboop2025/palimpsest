#!/usr/bin/env python3
"""Publish the Palimpsest Wire from the normalized China OSINT board.

This is a renderer, not a collector. ``core.newsroom`` owns the strict editorial
contract; this module turns that already-validated contract into static HTML,
per-story JSON, JSON Feed, RSS and a sitemap. Every output is built in memory
before any destination is replaced, so invalid source data cannot erase the
last known-good edition.

    PYTHONPATH=. python -m scripts.build_newsroom
    PYTHONPATH=. python -m scripts.build_newsroom --check
"""

from __future__ import annotations

import argparse
import copy
import email.utils
import hashlib
import html
import ipaddress
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr as xml_quoteattr

from core import china_analysis as china_analysis_model
from core import china_article_stream as china_stream_model
from core import dragon_whispers as dragon_whispers_model
from core import economic_pulse as economic_pulse_model
from core import event_analysis as event_analysis_model
from core import evidence_mesh as evidence_mesh_model
from core import peer_context as peer_context_model
from core import investigations as investigations_model
from core import machine_investigations as machine_investigations_model
from core import newsroom
from core import newswire as newswire_model
from core import telegram_watch as telegram_watch_model
from scripts import site_nav


ROOT = Path(__file__).resolve().parent.parent
NEWS = ROOT / "news"
READING = ROOT / "readings" / "newsroom-latest.json"
NEWSWIRE_READING = ROOT / "readings" / "newswire-latest.json"
ECONOMIC_READING = ROOT / "readings" / "china-economic-pulse-latest.json"
INVESTIGATIONS_READING = ROOT / "readings" / "investigations-latest.json"
MACHINE_INVESTIGATIONS_READING = (
    ROOT / "readings" / "machine-investigations-latest.json"
)
EVIDENCE_MESH_READING = ROOT / "readings" / "evidence-mesh-latest.json"
TELEGRAM_WATCH_READING = ROOT / "readings" / "telegram-watch-latest.json"
DRAGON_WHISPERS_READING = ROOT / "readings" / "dragon-whispers-latest.json"
PEER_CONTEXT_READING = ROOT / "readings" / "peer-context-latest.json"
PUBLIC_DATA_CATALOG = ROOT / "config" / "public_data_catalog.json"
SITE = "https://palimpsest.info"
PUBLISHER = "Palimpsest Observatory"
DESCRIPTION = (
    "Evidence-linked dispatches from Palimpsest's China censorship, network, "
    "erasure, state-telemetry and model measurements."
)
OG_IMAGE = f"{SITE}/brand/palimpsest-og2.png"

DRAGON_DEN_TELEGRAM_CHANNELS = (
    (
        "all",
        "All raw signals",
        "@DragonDenWhispers",
        "https://t.me/DragonDenWhispers",
        "The catch-all transmission: China news, cyber reporting, and regional context.",
    ),
    (
        "cyber",
        "Cyber / technology",
        "@DragonDenCyber",
        "https://t.me/DragonDenCyber",
        "Source-attributed forwards from the configured cyber and technology routes.",
    ),
    (
        "borderlands",
        "Regional / borderlands",
        "@DragonDenBorderlands",
        "https://t.me/DragonDenBorderlands",
        "Source-attributed forwards from the configured regional and borderlands routes.",
    ),
)
DRAGON_DEN_TELEGRAM_BOT = (
    "@DragonDenWhispersBot",
    "https://t.me/DragonDenWhispersBot",
)

EVENT_DESKS = {
    "economy": "Economy",
    "politics": "Politics & law",
    "rights": "Rights",
    "security": "Security",
    "censorship": "Censorship",
    "connectivity": "Connectivity & networks",
    "technology": "Technology",
}

EVIDENCE_LABELS = {
    "measurement-corroborated": "Measurement + independent source groups",
    "primary-corroborated": "Primary record + independent source groups",
    "multi-source": "Multiple independent source groups",
    "single-measurement-source": "Single measurement source",
    "single-primary-source": "Single primary source",
    "single-source": "Single attributed source",
}

HOME_EVENTS_PER_DESK = 5
WIRE_PAGE_SIZE = 60
CHINA_STREAM_PAGE_SIZE = 40
CHINA_ANALYSIS_READING = ROOT / "readings" / "china-censorship-analysis-latest.json"

_GENERATED_MANIFEST_PATH = Path("news/generated-manifest.json")
_ANALYSIS_ROOT = Path("news/analysis")
_PAGINATION_LAYOUTS = {
    Path("news/wire/page"): b'<body class="ps newsroom-page newsroom-page--archive">',
    Path("news/china/page"): b'<body class="ps newsroom-page china-stream-page">',
}
_PAGINATION_PAGE_NUMBER = re.compile(r"(?:[2-9]|[1-9][0-9]+)")
_MACHINE_REVISION_FILENAME = re.compile(r"machinev-[0-9a-f]{24}\.json")
_EVENT_ANALYSIS_REVISION_FILENAME = re.compile(r"analysisv-[0-9a-f]{24}\.json")
_EVENT_REVISION_FILENAME = re.compile(r"eventv-[0-9a-f]{24}\.json")
_WIRE_EVENT_DIRECTORY = re.compile(r"event-[0-9a-f]{24}")
_MACHINE_EVIDENCE_FILENAME = re.compile(r"sha256-[0-9a-f]{64}\.json")
_ANALYSIS_CASE_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_MACHINE_EVIDENCE_CAPSULE_SCHEMA = "palimpsest-machine-evidence-capsule.v1"
_MACHINE_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_MACHINE_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\s().-]*){9,}(?!\d)")
_MACHINE_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_MACHINE_IPV6_TOKEN = re.compile(r"(?<![0-9A-Fa-f:])[0-9A-Fa-f:]{2,}(?![0-9A-Fa-f:])")
_MACHINE_SOURCE_DOMAIN = re.compile(
    r"(?<![A-Za-z0-9.-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?![A-Za-z0-9.-])"
)
_MACHINE_CAPSULE_FIELDS = {
    "schema_version", "capsule_type", "content_address", "original_input",
    "citations", "privacy",
}
_MACHINE_CAPSULE_CITATION_FIELDS = {
    "evidence_id", "title", "role", "source_class", "source_id", "selector",
    "source_timestamp", "independence_group", "upstream_groups", "value",
    "denominator", "interpretation_limit", "freshness", "rights",
    "attribution",
}
_MACHINE_PROVIDER_LINKS = {
    "OONI": {
        "source_url": "https://api.ooni.io/",
        "terms_url": "https://github.com/ooni/license/blob/master/data/LICENSE.md",
    },
    "Globalping": {
        "source_url": "https://globalping.io/docs/api.globalping.io",
        "terms_url": "https://globalping.io/terms",
    },
    "Team Cymru": {
        "source_url": "https://www.team-cymru.com/ip-asn-mapping",
        "terms_url": "https://www.team-cymru.com/terms",
    },
}

_SOURCE_LANGUAGES = {
    "bbc-chinese": "zh-Hant",
    "rfa-mandarin": "zh-Hans",
    "voa-chinese": "zh-Hans",
}

_LEAD_STRENGTH_RANK = {
    "measurement-corroborated": 5,
    "primary-corroborated": 4,
    "multi-source": 3,
    "single-measurement-source": 2,
    "single-primary-source": 1,
    "single-source": 0,
}
_DATA_RELEASE_TERMS = (
    "tender results",
    "survey on business",
    "exchange rate index",
    "consumer price",
    "producer price",
    "factory-gate prices",
    "gross domestic product",
    "gdp",
    "inflation",
    "unemployment",
    "retail sales",
    "industrial production",
    "trade balance",
    "money supply",
)


def _h(value: object) -> str:
    return html.escape(str(value), quote=True)


def _contains_han(value: object) -> bool:
    """Return whether text contains a Han ideograph used by the Chinese feeds."""

    return any(
        "\u3400" <= character <= "\u4dbf"
        or "\u4e00" <= character <= "\u9fff"
        or "\uf900" <= character <= "\ufaff"
        for character in str(value)
    )


def _text_language(value: object, *, source_id: str | None = None) -> str:
    """Infer rendered text language without treating a source as the text itself.

    A Chinese-language desk can publish an English translation, so the Han-script
    check is the gate. The source identity then supplies the script variant that
    cannot be inferred reliably from a short headline alone.
    """

    if not _contains_han(value):
        return "en"
    return _SOURCE_LANGUAGES.get(source_id or "", "zh")


def _event_language(event: Mapping[str, Any]) -> str:
    """Infer the headline language, preferring the receipt that supplied it."""

    headline = str(event["headline"])
    refs = event.get("evidence_refs", [])
    matching_ref = next(
        (ref for ref in refs if str(ref.get("title", "")).strip() == headline.strip()),
        refs[0] if refs else None,
    )
    source_id = str(matching_ref["source_id"]) if matching_ref else None
    return _text_language(headline, source_id=source_id)


def _json_script(value: object) -> str:
    """Serialize JSON safely inside a script element, including hostile text."""

    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _pretty_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def _machine_exact_mapping(
    value: object, fields: set[str], label: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise newsroom.NewsroomError(f"{label} has an invalid closed shape")
    return value


def _machine_safe_public_string(value: object, label: str) -> str:
    """Return public capsule text, rejecting contact and network identifiers.

    Evidence capsules are deliberately narrower than the validated machine-case
    schema.  A cited aggregate may be public while adjacent raw answer records
    contain IP addresses or contact-like values, so capsule text gets a final
    fail-closed privacy check before publication.
    """

    if not isinstance(value, str) or not value:
        raise newsroom.NewsroomError(f"{label} must be non-empty public text")
    for match in _MACHINE_IPV4.finditer(value):
        try:
            ipaddress.ip_address(match.group(0))
        except ValueError:
            continue
        raise newsroom.NewsroomError(f"{label} contains an IP address")
    for match in _MACHINE_IPV6_TOKEN.finditer(value):
        candidate = match.group(0)
        if candidate.count(":") < 2:
            continue
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        raise newsroom.NewsroomError(f"{label} contains an IP address")
    if _MACHINE_EMAIL.search(value) or _MACHINE_PHONE.search(value):
        raise newsroom.NewsroomError(f"{label} contains contact-like data")
    return value


def _machine_https_url(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise newsroom.NewsroomError(f"{label} is not an HTTPS URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise newsroom.NewsroomError(f"{label} is not a safe HTTPS URL")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise newsroom.NewsroomError(f"{label} uses an IP-address host")
    return value


def _machine_external_source_urls(source_statement: str | None) -> list[str]:
    """Extract only provider hostnames explicitly named by the raw receipt."""

    if not source_statement:
        return []
    urls: list[str] = []
    for match in _MACHINE_SOURCE_DOMAIN.finditer(source_statement):
        host = match.group(0).lower()
        if host == "palimpsest.info":
            continue
        url = f"https://{host}/"
        if url not in urls:
            urls.append(url)
    return urls


def _load_machine_evidence_context() -> dict[str, Any]:
    """Load rights and attribution metadata bound by the validated mesh."""

    try:
        mesh_raw = EVIDENCE_MESH_READING.read_bytes()
        catalog_raw = PUBLIC_DATA_CATALOG.read_bytes()
    except OSError as exc:
        raise newsroom.NewsroomError(
            "cannot load machine-evidence rights metadata"
        ) from exc
    mesh = newswire_model.strict_json_loads(mesh_raw, label=str(EVIDENCE_MESH_READING))
    catalog_value = newswire_model.strict_json_loads(
        catalog_raw, label=str(PUBLIC_DATA_CATALOG)
    )
    try:
        evidence_mesh_model.validate_evidence_mesh(mesh)
        catalog = evidence_mesh_model._validate_catalog(catalog_value)
    except Exception as exc:
        raise newsroom.NewsroomError(
            f"invalid machine-evidence rights metadata: {exc}"
        ) from exc

    catalog_receipts = [
        receipt
        for receipt in mesh["inputs"]
        if receipt["input_id"] == "palimpsest-catalog"
    ]
    if len(catalog_receipts) != 1:
        raise newsroom.NewsroomError(
            "evidence mesh does not contain one public-catalog receipt"
        )
    catalog_receipt = catalog_receipts[0]
    if (
        catalog_receipt["sha256"] != hashlib.sha256(catalog_raw).hexdigest()
        or catalog_receipt["bytes"] != len(catalog_raw)
        or catalog_receipt["locator"] != "config/public_data_catalog.json"
    ):
        raise newsroom.NewsroomError(
            "evidence mesh is not bound to the current public catalog"
        )

    resources: dict[str, Mapping[str, Any]] = {}
    for resource in mesh["resources"]:
        source_id = resource["source_id"]
        current = resources.get(source_id)
        if current is None or (
            current["namespace"] != "osint" and resource["namespace"] == "osint"
        ):
            resources[source_id] = resource
    datasets = {dataset["id"]: dataset for dataset in catalog["datasets"]}
    return {"resources": resources, "datasets": datasets}


def _machine_attribution_metadata(
    evidence: Mapping[str, Any],
    raw_document: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    source_id = str(evidence["source_id"])
    resource = context["resources"].get(source_id)
    dataset = context["datasets"].get(source_id)
    if not isinstance(resource, Mapping) or not isinstance(dataset, Mapping):
        raise newsroom.NewsroomError(
            f"machine evidence lacks mesh/catalog metadata: {source_id}"
        )
    if resource["source_id"] != source_id or dataset["id"] != source_id:
        raise newsroom.NewsroomError(
            f"machine evidence attribution mismatch: {source_id}"
        )

    source_value = raw_document.get("source")
    source_statement = (
        _machine_safe_public_string(source_value, f"{source_id}.source")
        if isinstance(source_value, str) and source_value
        else None
    )
    source_urls = _machine_external_source_urls(source_statement)
    providers = []
    normalized_source_urls: list[str] = []
    for provider in dataset["sources"]:
        name = _machine_safe_public_string(provider, f"{source_id}.provider")
        registered = _MACHINE_PROVIDER_LINKS.get(name, {})
        matched_url = next(
            (
                url
                for url in source_urls
                if name.lower().replace(" ", "")
                in (urlsplit(url).hostname or "").replace("-", "")
            ),
            None,
        )
        # A reviewed provider registry is the canonical public attribution
        # target.  A hostname extracted from a source statement can identify
        # provenance, but it may be an API root that is not a usable landing
        # page (Globalping's API root currently returns 404).
        source_url = registered.get("source_url") or matched_url
        terms_url = registered.get("terms_url")
        provider_metadata = {
            "name": name,
            "source_url": (
                _machine_https_url(source_url, f"{source_id}.{name}.source_url")
                if source_url else None
            ),
            "terms_url": (
                _machine_https_url(terms_url, f"{source_id}.{name}.terms_url")
                if terms_url else None
            ),
        }
        providers.append(provider_metadata)
        if matched_url is not None and provider_metadata["source_url"] is not None:
            canonical_url = provider_metadata["source_url"]
            if canonical_url not in normalized_source_urls:
                normalized_source_urls.append(canonical_url)

    # Preserve a safely extracted source URL only when it did not identify a
    # provider with a reviewed canonical landing page.  This keeps provenance
    # complete without publishing known-unusable inferred API roots.
    matched_hosts = {
        urlsplit(url).hostname
        for provider in providers
        for url in source_urls
        if provider["source_url"] is not None
        and provider["name"].lower().replace(" ", "")
        in (urlsplit(url).hostname or "").replace("-", "")
    }
    for source_url in source_urls:
        if urlsplit(source_url).hostname not in matched_hosts:
            normalized_source_urls.append(source_url)

    license_value = dataset["license"]
    license_metadata = {
        "name": _machine_safe_public_string(
            license_value["name"], f"{source_id}.license.name"
        ),
        "url": _machine_https_url(
            license_value["url"], f"{source_id}.license.url"
        ),
    }
    rights = _machine_exact_mapping(
        resource["rights"], {"redistribution", "reuse", "training"},
        f"{source_id}.rights",
    )
    upstream_groups = [
        _machine_safe_public_string(group, f"{source_id}.upstream_group")
        for group in resource["upstream_groups"]
    ]
    return {
        "rights": {
            "redistribution": _machine_safe_public_string(
                rights["redistribution"], f"{source_id}.redistribution"
            ),
            "reuse": _machine_safe_public_string(
                rights["reuse"], f"{source_id}.reuse"
            ),
            "training": _machine_safe_public_string(
                rights["training"], f"{source_id}.training"
            ),
        },
        "attribution": {
            "attribution_required": rights["redistribution"]
            == "ATTRIBUTION_REQUIRED",
            "providers": providers,
            "upstream_groups": upstream_groups,
            "public_source_url": _machine_https_url(
                resource["public_url"], f"{source_id}.public_source_url"
            ),
            "upstream_source_urls": normalized_source_urls,
            "source_statement": source_statement,
            "license": license_metadata,
        },
    }


def _machine_read_cited_input(evidence: Mapping[str, Any]) -> tuple[bytes, Mapping[str, Any]]:
    artifact_id = evidence.get("artifact_id")
    if (
        not isinstance(artifact_id, str)
        or Path(artifact_id).name != artifact_id
        or not artifact_id.endswith(".json")
    ):
        raise newsroom.NewsroomError(
            f"unsafe machine evidence artifact_id: {artifact_id!r}"
        )
    source_path = ROOT / "readings" / artifact_id
    try:
        raw = source_path.read_bytes()
    except OSError as exc:
        raise newsroom.NewsroomError(
            f"cannot read cited machine evidence: {source_path}"
        ) from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != evidence.get("artifact_sha256"):
        raise newsroom.NewsroomError(
            f"machine evidence bytes do not match receipt: {artifact_id}"
        )
    document = newswire_model.strict_json_loads(raw, label=str(source_path))
    if not isinstance(document, Mapping):
        raise newsroom.NewsroomError(
            f"machine evidence input is not a JSON object: {artifact_id}"
        )
    return raw, document


def _machine_capsule_citation(
    evidence: Mapping[str, Any],
    raw_document: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    metadata = _machine_attribution_metadata(evidence, raw_document, context)
    value = evidence["value"]
    if isinstance(value, str):
        value = _machine_safe_public_string(value, f"{evidence['evidence_id']}.value")
    elif type(value) not in {int, float}:
        raise newsroom.NewsroomError(
            f"{evidence['evidence_id']}.value is not an aggregate scalar"
        )
    denominator = evidence["denominator"]
    typed_denominator = None
    if denominator is not None:
        typed_denominator = {
            "type": "aggregate-count",
            "label": _machine_safe_public_string(
                denominator["label"], f"{evidence['evidence_id']}.denominator.label"
            ),
            "value": denominator["value"],
        }
    return {
        "evidence_id": _machine_safe_public_string(
            evidence["evidence_id"], "evidence_id"
        ),
        "title": _machine_safe_public_string(
            evidence["title"], f"{evidence['evidence_id']}.title"
        ),
        "role": _machine_safe_public_string(
            evidence["role"], f"{evidence['evidence_id']}.role"
        ),
        "source_class": _machine_safe_public_string(
            evidence["source_class"], f"{evidence['evidence_id']}.source_class"
        ),
        "source_id": _machine_safe_public_string(
            evidence["source_id"], f"{evidence['evidence_id']}.source_id"
        ),
        "selector": _machine_safe_public_string(
            evidence["selector"], f"{evidence['evidence_id']}.selector"
        ),
        "source_timestamp": _machine_safe_public_string(
            evidence["source_timestamp"], f"{evidence['evidence_id']}.source_timestamp"
        ),
        "independence_group": _machine_safe_public_string(
            evidence["independence_group"],
            f"{evidence['evidence_id']}.independence_group",
        ),
        "upstream_groups": [
            _machine_safe_public_string(
                group, f"{evidence['evidence_id']}.upstream_group"
            )
            for group in evidence["upstream_groups"]
        ],
        "value": {
            "type": _machine_safe_public_string(
                evidence["value_type"], f"{evidence['evidence_id']}.value.type"
            ),
            "value": value,
        },
        "denominator": typed_denominator,
        "interpretation_limit": _machine_safe_public_string(
            evidence["interpretation_limit"],
            f"{evidence['evidence_id']}.interpretation_limit",
        ),
        "freshness": _machine_safe_public_string(
            evidence["freshness"], f"{evidence['evidence_id']}.freshness"
        ),
        **metadata,
    }


def _machine_evidence_capsule(
    evidence_rows: Sequence[Mapping[str, Any]],
    *,
    raw: bytes,
    raw_document: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    if not evidence_rows:
        raise newsroom.NewsroomError("cannot publish an empty evidence capsule")
    digest = hashlib.sha256(raw).hexdigest()
    if any(row.get("artifact_sha256") != digest for row in evidence_rows):
        raise newsroom.NewsroomError(
            "machine evidence capsule mixes different original inputs"
        )
    artifact_ids = {row.get("artifact_id") for row in evidence_rows}
    artifact_times = {row.get("artifact_generated_at") for row in evidence_rows}
    integrities = {row.get("integrity") for row in evidence_rows}
    if len(artifact_ids) != 1 or len(artifact_times) != 1 or len(integrities) != 1:
        raise newsroom.NewsroomError(
            "machine evidence capsule has inconsistent input receipts"
        )
    capsule = {
        "schema_version": _MACHINE_EVIDENCE_CAPSULE_SCHEMA,
        "capsule_type": "redacted-aggregate-evidence",
        "content_address": {
            "algorithm": "sha256",
            "scope": "original-input-bytes",
            "sha256": digest,
        },
        "original_input": {
            "artifact_id": next(iter(artifact_ids)),
            "generated_at": next(iter(artifact_times)),
            "sha256": digest,
            "integrity": next(iter(integrities)),
        },
        "citations": [
            _machine_capsule_citation(row, raw_document, context)
            for row in evidence_rows
        ],
        "privacy": {
            "aggregate_only": True,
            "raw_input_included": False,
            "person_level_data_included": False,
            "contact_data_included": False,
            "ip_addresses_included": False,
        },
    }
    _validate_machine_evidence_capsule(capsule, expected_digest=digest)
    return capsule


def _validate_machine_evidence_capsule(
    value: object, *, expected_digest: str
) -> Mapping[str, Any]:
    capsule = _machine_exact_mapping(
        value, _MACHINE_CAPSULE_FIELDS, "machine evidence capsule"
    )
    if (
        capsule["schema_version"] != _MACHINE_EVIDENCE_CAPSULE_SCHEMA
        or capsule["capsule_type"] != "redacted-aggregate-evidence"
    ):
        raise newsroom.NewsroomError("unknown machine evidence capsule schema")
    address = _machine_exact_mapping(
        capsule["content_address"], {"algorithm", "scope", "sha256"},
        "machine evidence content address",
    )
    if address != {
        "algorithm": "sha256",
        "scope": "original-input-bytes",
        "sha256": expected_digest,
    }:
        raise newsroom.NewsroomError(
            "machine evidence capsule is not bound to its original input"
        )
    original = _machine_exact_mapping(
        capsule["original_input"],
        {"artifact_id", "generated_at", "sha256", "integrity"},
        "machine evidence original input",
    )
    if original["sha256"] != expected_digest:
        raise newsroom.NewsroomError(
            "machine evidence original-input hash does not match its address"
        )
    artifact_id = original["artifact_id"]
    if (
        not isinstance(artifact_id, str)
        or Path(artifact_id).name != artifact_id
        or not artifact_id.endswith(".json")
    ):
        raise newsroom.NewsroomError("machine evidence capsule artifact ID is unsafe")
    _machine_safe_public_string(original["generated_at"], "capsule.generated_at")
    _machine_safe_public_string(original["integrity"], "capsule.integrity")

    privacy = _machine_exact_mapping(
        capsule["privacy"],
        {
            "aggregate_only", "raw_input_included", "person_level_data_included",
            "contact_data_included", "ip_addresses_included",
        },
        "machine evidence privacy receipt",
    )
    if privacy != {
        "aggregate_only": True,
        "raw_input_included": False,
        "person_level_data_included": False,
        "contact_data_included": False,
        "ip_addresses_included": False,
    }:
        raise newsroom.NewsroomError(
            "machine evidence capsule violates the public privacy boundary"
        )

    citations = capsule["citations"]
    if not isinstance(citations, list) or not citations or len(citations) > 32:
        raise newsroom.NewsroomError(
            "machine evidence capsule citations must be non-empty and bounded"
        )
    seen: set[str] = set()
    for position, citation_value in enumerate(citations):
        citation = _machine_exact_mapping(
            citation_value, _MACHINE_CAPSULE_CITATION_FIELDS,
            f"machine evidence citation {position}",
        )
        evidence_id = _machine_safe_public_string(
            citation["evidence_id"], f"capsule.citations[{position}].evidence_id"
        )
        if evidence_id in seen:
            raise newsroom.NewsroomError("machine evidence capsule repeats a citation")
        seen.add(evidence_id)
        for field in (
            "title", "role", "source_class", "source_id", "selector",
            "source_timestamp", "independence_group", "interpretation_limit",
            "freshness",
        ):
            _machine_safe_public_string(
                citation[field], f"capsule.citations[{position}].{field}"
            )
        for field in ("upstream_groups",):
            if not isinstance(citation[field], list):
                raise newsroom.NewsroomError(
                    f"capsule.citations[{position}].{field} is not an array"
                )
            for item in citation[field]:
                _machine_safe_public_string(
                    item, f"capsule.citations[{position}].{field}[]"
                )
        typed_value = _machine_exact_mapping(
            citation["value"], {"type", "value"},
            f"capsule.citations[{position}].value",
        )
        _machine_safe_public_string(
            typed_value["type"], f"capsule.citations[{position}].value.type"
        )
        if isinstance(typed_value["value"], str):
            _machine_safe_public_string(
                typed_value["value"], f"capsule.citations[{position}].value.value"
            )
        elif type(typed_value["value"]) not in {int, float}:
            raise newsroom.NewsroomError("capsule value is not an aggregate scalar")
        denominator = citation["denominator"]
        if denominator is not None:
            denominator = _machine_exact_mapping(
                denominator, {"type", "label", "value"},
                f"capsule.citations[{position}].denominator",
            )
            if denominator["type"] != "aggregate-count" or type(
                denominator["value"]
            ) not in {int, float}:
                raise newsroom.NewsroomError("capsule denominator is not typed")
            _machine_safe_public_string(
                denominator["label"],
                f"capsule.citations[{position}].denominator.label",
            )
        rights = _machine_exact_mapping(
            citation["rights"], {"redistribution", "reuse", "training"},
            f"capsule.citations[{position}].rights",
        )
        for field in rights:
            _machine_safe_public_string(
                rights[field], f"capsule.citations[{position}].rights.{field}"
            )
        attribution = _machine_exact_mapping(
            citation["attribution"],
            {
                "attribution_required", "providers", "upstream_groups",
                "public_source_url", "upstream_source_urls", "source_statement",
                "license",
            },
            f"capsule.citations[{position}].attribution",
        )
        if type(attribution["attribution_required"]) is not bool:
            raise newsroom.NewsroomError("capsule attribution flag is not boolean")
        _machine_https_url(
            attribution["public_source_url"],
            f"capsule.citations[{position}].attribution.public_source_url",
        )
        for url in attribution["upstream_source_urls"]:
            _machine_https_url(
                url, f"capsule.citations[{position}].attribution.source_url"
            )
        if attribution["source_statement"] is not None:
            _machine_safe_public_string(
                attribution["source_statement"],
                f"capsule.citations[{position}].attribution.source_statement",
            )
        for group in attribution["upstream_groups"]:
            _machine_safe_public_string(
                group,
                f"capsule.citations[{position}].attribution.upstream_group",
            )
        for provider_value in attribution["providers"]:
            provider = _machine_exact_mapping(
                provider_value, {"name", "source_url", "terms_url"},
                f"capsule.citations[{position}].attribution.provider",
            )
            _machine_safe_public_string(
                provider["name"],
                f"capsule.citations[{position}].attribution.provider.name",
            )
            if provider["source_url"] is not None:
                _machine_https_url(
                    provider["source_url"],
                    f"capsule.citations[{position}].attribution.provider.source_url",
                )
            if provider["terms_url"] is not None:
                _machine_https_url(
                    provider["terms_url"],
                    f"capsule.citations[{position}].attribution.provider.terms_url",
                )
        license_value = _machine_exact_mapping(
            attribution["license"], {"name", "url"},
            f"capsule.citations[{position}].attribution.license",
        )
        _machine_safe_public_string(
            license_value["name"],
            f"capsule.citations[{position}].attribution.license.name",
        )
        _machine_https_url(
            license_value["url"],
            f"capsule.citations[{position}].attribution.license.url",
        )
    return capsule


def _machine_evidence_capsule_bytes(
    raw: bytes, *, expected_digest: str
) -> Mapping[str, Any] | None:
    if len(raw) > 512 * 1024:
        return None
    try:
        value = newswire_model.strict_json_loads(raw, label="machine evidence capsule")
        return _validate_machine_evidence_capsule(
            value, expected_digest=expected_digest
        )
    except (ValueError, TypeError, KeyError, newsroom.NewsroomError):
        return None


def _load_extension_documents(
    *,
    newswire_path: Path = NEWSWIRE_READING,
    economic_path: Path = ECONOMIC_READING,
    investigations_path: Path = INVESTIGATIONS_READING,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    """Load optional publication planes through their strict runtime validators.

    The instrument newsroom remains independently buildable for recovery and
    focused tests. Once an extension file exists, however, corruption is fatal:
    silently falling back would make a broken intake look like an empty news day.
    """

    wire = pulse = investigations = None
    if newswire_path.exists():
        wire = newswire_model.strict_json_loads(
            newswire_path.read_bytes(), label=str(newswire_path)
        )
        newswire_model.validate_newswire_document(wire)
    if economic_path.exists():
        pulse = newswire_model.strict_json_loads(
            economic_path.read_bytes(), label=str(economic_path)
        )
        economic_pulse_model.validate_economic_pulse(pulse)
    if investigations_path.exists():
        investigations = newswire_model.strict_json_loads(
            investigations_path.read_bytes(), label=str(investigations_path)
        )
        investigations_model.validate_investigations(
            investigations,
            readings_dir=ROOT / "readings",
        )
    return wire, pulse, investigations


def _load_machine_investigations(
    path: Path = MACHINE_INVESTIGATIONS_READING,
) -> dict[str, Any] | None:
    """Load the optional machine-analysis desk and fail closed once present."""

    if not path.exists():
        return None
    document = newswire_model.strict_json_loads(path.read_bytes(), label=str(path))
    machine_investigations_model.validate_machine_investigations(
        document,
        readings_dir=ROOT / "readings",
    )
    return document


def _load_telegram_watch(
    path: Path = TELEGRAM_WATCH_READING,
) -> dict[str, Any] | None:
    """Load only a human-promoted public aggregate; private summaries are refused."""

    if not path.exists():
        return None
    document = newswire_model.strict_json_loads(path.read_bytes(), label=str(path))
    telegram_watch_model.validate_telegram_watch(document)
    return document


def _load_dragon_whispers(
    path: Path = DRAGON_WHISPERS_READING,
) -> dict[str, Any] | None:
    """Load only the reviewed/sanitized website artifact, never a raw queue."""

    if not path.exists():
        return None
    document = newswire_model.strict_json_loads(path.read_bytes(), label=str(path))
    dragon_whispers_model.validate_dragon_whispers(document)
    return document


def _revision_id(value: Mapping[str, Any], prefix: str = "revision") -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


def _site_path(url: str) -> str:
    prefix = SITE + "/"
    if not url.startswith(prefix):
        raise newsroom.NewsroomError(f"public story URL is outside {SITE}: {url!r}")
    return "/" + url.removeprefix(SITE).lstrip("/")


def _parse_time(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise newsroom.NewsroomError(f"timestamp is timezone-free: {value!r}")
    return parsed.astimezone(timezone.utc)


def _human_time(value: str | None) -> str:
    if not value:
        return "not observed"
    return _parse_time(value).strftime("%d %b %Y · %H:%M UTC")


def _rfc2822(value: str) -> str:
    return email.utils.format_datetime(_parse_time(value), usegmt=True)


def _number(value: int | float | None) -> str:
    if value is None:
        return "not reported"
    if isinstance(value, int):
        return f"{value:,}"
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _metric_value(story: Mapping[str, Any]) -> str:
    metric = story["metric"]
    if metric["value"] is None:
        return "No current value"
    value = _number(metric["value"])
    unit = metric["unit"]
    if unit == "percent":
        return f"{value}%"
    if unit == "ratio":
        return f"{_number(metric['value'] * 100)}%"
    return f"{value} {unit}".strip()


def _metric_caption(story: Mapping[str, Any]) -> str:
    metric = story["metric"]
    if metric["label"] is None:
        return f"Source status: {story['status']}"
    text = metric["label"]
    denominator = metric["denominator"]
    if denominator["value"] is not None:
        text += f" · across {_number(denominator['value'])} {denominator['label']}"
    return text


def _status_label(status: str) -> str:
    return {
        "live": "Current evidence",
        "degraded": "Coverage degraded",
        "stale": "Evidence stale",
        "missing": "Source missing",
        "corrupt": "Source unreadable",
    }[status]


def _head(
    *,
    title: str,
    description: str,
    canonical: str,
    page_type: str,
    published_at: str | None = None,
    modified_at: str | None = None,
    json_ld: object,
    feed_base: str = "/news",
    extra_styles: Sequence[str] = (),
) -> str:
    article_meta = ""
    if published_at:
        article_meta += (
            f'<meta property="article:published_time" content="{_h(published_at)}">\n'
        )
    if modified_at:
        article_meta += (
            f'<meta property="article:modified_time" content="{_h(modified_at)}">\n'
        )
    style_links = "".join(
        f'<link rel="stylesheet" href="{_h(path)}">\n' for path in extra_styles
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_h(title)}</title>
<meta name="description" content="{_h(description)}">
<meta name="author" content="{PUBLISHER}">
<meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1">
<link rel="canonical" href="{_h(canonical)}">
<link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg">
<link rel="alternate" type="application/feed+json" title="Palimpsest Wire JSON Feed" href="{_h(feed_base)}/feed.json">
<link rel="alternate" type="application/rss+xml" title="Palimpsest Wire RSS" href="{_h(feed_base)}/feed.xml">
<meta name="theme-color" content="#0b131c">
<meta property="og:type" content="{_h(page_type)}">
<meta property="og:site_name" content="Palimpsest Wire">
<meta property="og:title" content="{_h(title)}">
<meta property="og:description" content="{_h(description)}">
<meta property="og:url" content="{_h(canonical)}">
<meta property="og:image" content="{OG_IMAGE}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{_h(title)}">
<meta name="twitter:description" content="{_h(description)}">
<meta name="twitter:image" content="{OG_IMAGE}">
{article_meta}<script type="application/ld+json">{_json_script(json_ld)}</script>
{site_nav.HEAD}
<link rel="stylesheet" href="/assets/newsroom.css">
{style_links}
</head>"""


def _organization() -> dict[str, Any]:
    return {
        "@type": "NewsMediaOrganization",
        "@id": f"{SITE}/#organization",
        "name": PUBLISHER,
        "url": f"{SITE}/",
        "logo": {
            "@type": "ImageObject",
            "url": f"{SITE}/brand/palimpsest-icon-512.png",
            "width": 512,
            "height": 512,
        },
    }


def _receipt(story: Mapping[str, Any]) -> str:
    evidence = story["evidence"]
    source_time = evidence["source_timestamp"]
    digest = evidence["input"]["sha256"]
    filename = evidence["input"]["filename"]
    bytes_value = evidence["input"]["bytes"]
    status_class = "" if story["status"] == "live" else " nw-receipt__state--warning"
    sha = digest or "not available"
    size = f"{bytes_value:,} bytes" if bytes_value is not None else "not available"
    return f"""<aside class="nw-receipt" aria-label="Evidence receipt">
  <p class="nw-receipt__label">Evidence receipt</p>
  <dl>
    <dt>Status</dt>
    <dd><span class="nw-receipt__state{status_class}"><span class="nw-dot" aria-hidden="true"></span>{_h(_status_label(story['status']))}</span></dd>
    <dt>Observed</dt>
    <dd>{_h(_human_time(source_time))}</dd>
    <dt>Source file</dt>
    <dd><a href="{_h(evidence['url'])}">{_h(filename)}</a></dd>
    <dt>Source size</dt>
    <dd>{_h(size)}</dd>
    <dt>SHA-256</dt>
    <dd><code>{_h(sha)}</code></dd>
    <dt>Claim seal</dt>
    <dd><code>{_h(story['claim_fingerprint'])}</code></dd>
  </dl>
</aside>"""


def _story_json_ld(story: Mapping[str, Any], section_title: str) -> dict[str, Any]:
    evidence = story["evidence"]
    return {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "@id": story["url"],
        "mainEntityOfPage": {"@type": "WebPage", "@id": story["url"]},
        "headline": story["headline"],
        "description": story["dek"],
        "datePublished": story["published_at"],
        "dateModified": story["modified_at"],
        "articleSection": section_title,
        "inLanguage": "en",
        "isAccessibleForFree": True,
        "author": _organization(),
        "publisher": _organization(),
        "image": [OG_IMAGE],
        "isBasedOn": evidence["url"],
        "citation": evidence["url"],
        "keywords": [story["section"], story["signal_id"], "China", "open source intelligence"],
    }


def _index_json_ld(feed: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@graph": [
            _organization(),
            {
                "@type": "CollectionPage",
                "@id": feed["url"],
                "url": feed["url"],
                "name": feed["title"],
                "description": feed["scope"],
                "dateModified": feed["generated_at"],
                "publisher": {"@id": f"{SITE}/#organization"},
                "mainEntity": {
                    "@type": "ItemList",
                    "numberOfItems": feed["n_stories"],
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": index,
                            "url": story["url"],
                            "name": story["headline"],
                        }
                        for index, story in enumerate(feed["stories"], 1)
                    ],
                },
            },
        ],
    }


def _story_card(story: Mapping[str, Any], section_title: str) -> str:
    evidence = story["evidence"]
    digest = evidence["input"]["sha256"]
    short_hash = digest[:12] if digest else "no-source-hash"
    status_class = "" if story["status"] == "live" else " nw-kicker--warning"
    return f"""<article class="nw-card" data-status="{_h(story['status'])}">
  <p class="nw-card__kicker{status_class}">{_h(section_title)} · {_h(_status_label(story['status']))}</p>
  <h3><a class="nw-card__link" href="/{_h(story['url'].removeprefix(SITE).lstrip('/'))}">{_h(story['headline'])}</a></h3>
  <p class="nw-card__dek">{_h(story['dek'])}</p>
  <p class="nw-card__metric"><strong>{_h(_metric_value(story))}</strong>{_h(_metric_caption(story))}</p>
  <p class="nw-card__meta"><time datetime="{_h(story['published_at'])}">{_h(_human_time(story['published_at']))}</time><span class="nw-card__hash">sha {short_hash}</span></p>
</article>"""


def _lead(
    story: Mapping[str, Any],
    section_title: str,
    *,
    heading_level: int = 1,
    heading_id: str = "lead-headline",
) -> str:
    if heading_level not in {1, 2}:
        raise ValueError("lead heading level must be 1 or 2")
    heading = f"h{heading_level}"
    qualifier = story["limitations"][0]
    status_class = "" if story["status"] == "live" else " nw-kicker--warning"
    return f"""<section class="nw-lead" aria-labelledby="{_h(heading_id)}">
  <div>
    <p class="nw-kicker{status_class}">Palimpsest measurement · {_h(section_title)} · {_h(_status_label(story['status']))}</p>
    <{heading} id="{_h(heading_id)}">{_h(story['headline'])}</{heading}>
    <p class="nw-lead__dek">{_h(story['dek'])}</p>
    <p class="nw-lead__qualifier"><strong>Read with this qualifier:</strong> {_h(qualifier)}</p>
    <div class="nw-actions">
      <a class="nw-actions__primary" href="/{_h(story['url'].removeprefix(SITE).lstrip('/'))}">Open result and receipt</a>
      <a href="/readings/newsroom-latest.json">Structured edition</a>
      <a href="/news/instruments/feed.xml">Measurements-only RSS</a>
    </div>
  </div>
  {_receipt(story)}
</section>"""


def _select_instrument_lead(
    stories: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    lead = next(
        (
            story
            for story in stories
            if story["priority"] == "lead" and story["status"] == "live"
        ),
        None,
    )
    if lead is None:
        lead = next((story for story in stories if story["status"] == "live"), None)
    if lead is None:
        lead = stories[0]
    return lead


def _wire_index_json_ld(
    feed: Mapping[str, Any], wire: Mapping[str, Any]
) -> dict[str, Any]:
    entries = [
        {"url": event["url"], "name": event["headline"]}
        for event in wire["events"]
    ] + [
        {"url": story["url"], "name": story["headline"]}
        for story in feed["stories"]
    ]
    return {
        "@context": "https://schema.org",
        "@graph": [
            _organization(),
            {
                "@type": "CollectionPage",
                "@id": feed["url"],
                "url": feed["url"],
                "name": "Palimpsest evidence desk",
                "description": (
                    "Palimpsest measurements and an attributed publisher source "
                    "index kept in separate, labeled sections."
                ),
                "dateModified": max(feed["generated_at"], wire["generated_at"]),
                "publisher": {"@id": f"{SITE}/#organization"},
                "mainEntity": {
                    "@type": "ItemList",
                    "numberOfItems": len(entries),
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": index,
                            "url": item["url"],
                            "name": item["name"],
                        }
                        for index, item in enumerate(entries, 1)
                    ],
                },
            },
        ],
    }


def _wire_items(wire: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {item["item_id"]: item for item in wire["items"]}


def _event_braid(
    event: Mapping[str, Any], wire: Mapping[str, Any], *, compact: bool = False
) -> str:
    items = _wire_items(wire)
    refs = event["evidence_refs"][:3] if compact else event["evidence_refs"]
    rows = []
    for ref in refs:
        item = items[ref["item_id"]]
        digest = item["feed_sha256"][:12]
        title_language = _text_language(ref["title"], source_id=ref["source_id"])
        rows.append(f"""<li class="nw-braid__node" data-role="{_h(ref['role'])}">
  <p class="nw-braid__role">{_h(ref['role'])} · {_h(ref['independence_group'])}</p>
  <p class="nw-braid__source"><a href="{_h(ref['url'])}">{_h(ref['source_name'])}</a></p>
  <p class="nw-braid__title" lang="{_h(title_language)}">{_h(ref['title'])}</p>
  <p class="nw-braid__time"><time datetime="{_h(ref['published_at'])}">{_h(_human_time(ref['published_at']))}</time> · feed sha {_h(digest)}</p>
</li>""")
    scan_ids = event["declared_links"]["scan_signal_ids"]
    economic_ids = event["declared_links"]["economic_signal_ids"]
    if scan_ids or economic_ids:
        linked = [f"scan:{value}" for value in scan_ids] + [
            f"economic:{value}" for value in economic_ids
        ]
        rows.append(f"""<li class="nw-braid__node nw-braid__node--link" data-role="topic-link">
  <p class="nw-braid__role">Declared topic surfaces · not a causal match</p>
  <p class="nw-braid__title">{_h(' · '.join(linked))}</p>
  <p class="nw-braid__time">A timed measurement join has not been asserted by this dossier.</p>
</li>""")
    return '<ol class="nw-braid" aria-label="Evidence braid">' + "".join(rows) + "</ol>"


def _event_source_label(event: Mapping[str, Any]) -> str:
    groups = len(event["evidence_groups"])
    if groups > 1:
        return f"Source report · {groups} independent publisher groups"
    return "Single-source report · not independently verified by Palimpsest"


def _event_source_boundary(event: Mapping[str, Any]) -> str:
    groups = len(event["evidence_groups"])
    if groups > 1:
        return (
            f"{groups} independent publisher groups reported related facts. "
            "That is corroborated reporting, not proof of truth, intent, impact "
            "or causation."
        )
    return (
        "This record indexes one publisher group. Palimpsest has not "
        "independently verified or refuted the publisher's claims."
    )


def _event_lead(event: Mapping[str, Any], wire: Mapping[str, Any]) -> str:
    groups = len(event["evidence_groups"])
    coverage = wire["coverage"]
    coverage_class = "" if coverage["status"] == "healthy" else " nw-receipt__state--warning"
    language = _event_language(event)
    dek_language = _text_language(
        event["dek"], source_id=event["evidence_refs"][0]["source_id"]
    )
    return f"""<section class="nw-wire-lead" id="source-index" aria-labelledby="source-index-headline">
  <div class="nw-wire-lead__copy">
    <p class="nw-kicker">Source index · {_h(_event_source_label(event))}</p>
    <h2 id="source-index-headline" lang="{_h(language)}">{_h(event['headline'])}</h2>
    <p class="nw-lead__dek" lang="{_h(dek_language)}">{_h(event['dek'])}</p>
    <p class="nw-lead__qualifier"><strong>Verification status:</strong> {_h(_event_source_boundary(event))}</p>
    <div class="nw-actions">
      <a class="nw-actions__primary" href="{_h(event['evidence_refs'][0]['url'])}">Read the original report</a>
      <a href="{_h(_site_path(event['url']))}">Open Palimpsest source record</a>
      <a href="/readings/newswire-latest.json">Structured source index</a>
    </div>
  </div>
  <aside class="nw-wire-lead__rail">
    <div class="nw-receipt" aria-label="Source index receipt">
      <p class="nw-receipt__label">Source index receipt</p>
      <dl>
        <dt>Record</dt><dd>{_h(_event_source_label(event))}</dd>
        <dt>Groups</dt><dd>{groups} independent evidence group{'s' if groups != 1 else ''}</dd>
        <dt>Published</dt><dd>{_h(_human_time(event['published_at']))}</dd>
        <dt>Version</dt><dd><code>{_h(event['version_id'])}</code></dd>
        <dt>Intake</dt><dd><span class="nw-receipt__state{coverage_class}"><span class="nw-dot" aria-hidden="true"></span>{_h(coverage['status'])}</span></dd>
      </dl>
    </div>
  </aside>
  <div class="nw-wire-lead__braid">{_event_braid(event, wire, compact=True)}</div>
</section>"""


def _event_card(event: Mapping[str, Any]) -> str:
    group_count = len(event["evidence_groups"])
    state = "multiple source groups" if group_count > 1 else "not independently verified"
    language = _event_language(event)
    dek_language = _text_language(
        event["dek"], source_id=event["evidence_refs"][0]["source_id"]
    )
    return f"""<article class="nw-event-card" data-strength="{_h(event['evidence_strength'])}" data-lead="{_h(str(event['lead']).lower())}">
  <p class="nw-card__kicker">{_h(_event_source_label(event))}</p>
  <h3 lang="{_h(language)}"><a class="nw-card__link" href="{_h(_site_path(event['url']))}">{_h(event['headline'])}</a></h3>
  <p class="nw-card__dek" lang="{_h(dek_language)}">{_h(event['dek'])}</p>
  <div class="nw-event-card__facts">
    <span>{len(event['evidence_refs'])} source receipt{'s' if len(event['evidence_refs']) != 1 else ''}</span>
    <span>{group_count} independent group{'s' if group_count != 1 else ''}</span>
    <span>{_h(state)}</span>
  </div>
  <p class="nw-card__meta"><time datetime="{_h(event['updated_at'])}">{_h(_human_time(event['updated_at']))}</time><span class="nw-card__hash">{_h(event['version_id'])}</span></p>
</article>"""


def _select_event_lead(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Choose a deterministic evidence-first lead from eligible current events.

    ``lead`` is the intake eligibility gate. This second ordering favours source
    structure, then an explicit release title, then recency; it does not invent a
    subjective truth or importance score.
    """

    eligible = [event for event in events if event["lead"]] or list(events)

    def key(event: Mapping[str, Any]) -> tuple[int, int, int, str, str]:
        headline = event["headline"].casefold()
        explicit_release = int(any(term in headline for term in _DATA_RELEASE_TERMS))
        return (
            _LEAD_STRENGTH_RANK[event["evidence_strength"]],
            explicit_release,
            len(event["evidence_groups"]),
            event["updated_at"],
            event["event_id"],
        )

    return max(eligible, key=key)


def _select_lead(entries: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Dispatch lead selection without conflating two publication contracts."""

    if entries and "event_id" in entries[0]:
        return _select_event_lead(entries)
    return _select_instrument_lead(entries)


def _event_sections(
    wire: Mapping[str, Any], *, lead_event_id: str
) -> tuple[str, str]:
    navigation = []
    blocks = []
    for order, (desk_id, title) in enumerate(EVENT_DESKS.items(), 1):
        events = [
            event for event in wire["events"]
            if event["desk"] == desk_id and event["event_id"] != lead_event_id
        ]
        if not events:
            continue
        navigation.append(f'<li><a href="#wire-{_h(desk_id)}">{_h(title)}</a></li>')
        visible_events = events[:HOME_EVENTS_PER_DESK]
        cards = "".join(_event_card(event) for event in visible_events)
        archive_link = ""
        if len(events) > len(visible_events):
            archive_link = (
                '<p class="nw-section__more"><a href="/news/wire/">'
                f'View all {len(events)} { _h(title).lower() } source records →</a></p>'
            )
        blocks.append(f"""<section class="nw-section nw-section--events" id="wire-{_h(desk_id)}">
  <div class="nw-section__head">
    <div><p class="nw-section__label">Source index · {_h(title)}</p><h2>{_h(title)}</h2></div>
    <p class="nw-section__dek">These are attributed publisher reports. Single-source records are not independently verified by Palimpsest; corroboration counts independent source groups, not mirrors.</p>
  </div>
  <div class="nw-event-grid">{cards}</div>{archive_link}
</section>""")
    return "".join(navigation), "".join(blocks)


def _economic_panel(pulse: Mapping[str, Any] | None) -> str:
    if pulse is None:
        return """<section class="nw-econ" id="economy"><div><p class="nw-kicker nw-kicker--warning">Economic state unavailable</p><h2>No validated economic pulse was published</h2></div><p>The instrument newsroom remains available, but no state-of-economy synthesis is shown without its structured evidence contract.</p></section>"""
    gates = "".join(
        f"""<li data-passed="{_h(str(gate['passed']).lower())}"><span>{_h(gate['label'])}</span><strong>{gate['observed']} / {gate['minimum']}</strong></li>"""
        for gate in pulse["readiness"]["gates"]
    )
    desks = "".join(
        f"""<div class="nw-econ__desk"><span>{_h(desk['title'])}</span><strong>{desk['n_metrics']}</strong><small>{len(desk['independent_group_ids'])} groups · {_h(desk['status'])}</small></div>"""
        for desk in pulse["desks"]
    )
    coverage = pulse["coverage"]
    return f"""<section class="nw-econ" id="economy" aria-labelledby="economy-title">
  <div class="nw-econ__statement">
    <p class="nw-kicker nw-kicker--economic">China economic state · {_h(pulse['economic_state']['status'])}</p>
    <h2 id="economy-title">The evidence is broadening. The composite still abstains.</h2>
    <p>{_h(pulse['economic_state']['claim'])}</p>
    <a class="nw-text-link" href="/news/economy/">Open all metrics, releases and revision receipts →</a>
  </div>
  <div class="nw-econ__readiness">
    <p class="nw-receipt__label">Composite readiness gates</p>
    <ul>{gates}</ul>
    <p>{len(coverage['observed_independent_group_ids'])} observed independent groups · {coverage['registered_sources']} registered sources · {_h(pulse['readiness']['abstention_reason'])}</p>
  </div>
  <div class="nw-econ__desks">{desks}</div>
</section>"""


def _case_public_url(case: Mapping[str, Any]) -> str:
    """Return the absolute form of a validator-owned investigation route."""

    path = str(case["url"])
    if not path.startswith("/news/investigations/"):
        raise newsroom.NewsroomError(f"invalid investigation URL: {path!r}")
    return SITE + path


def _investigation_href(value: object) -> str:
    """Allow only explicit web URLs and root-relative public artifacts."""

    href = str(value)
    if href.startswith("https://"):
        return href
    if href.startswith("/") and not href.startswith("//"):
        return href
    return "#"


def _case_publication_state(case: Mapping[str, Any]) -> str:
    status = case["status"]
    if status == "published":
        return "published"
    if status == "abstained":
        return "abstained"
    return "open"


def _case_status_label(case: Mapping[str, Any]) -> tuple[str, str]:
    """Keep an open automated lead visually distinct from reviewed reporting."""

    status = case["status"]
    if status == "published":
        if case["correction"]["status"] == "corrected":
            return "Investigation", "CORRECTED"
        if case["published_at"] != case["updated_at"]:
            return "Investigation", "UPDATED"
        return "Investigation", "PUBLISHED"
    return {
        "evidence_gathering": ("Research lead", "OPEN INVESTIGATION"),
        "review_ready": ("Research lead", "REVIEW READY"),
        "abstained": ("Research lead", "ABSTAINED"),
    }[status]


def _case_language(case: Mapping[str, Any]) -> str:
    return _text_language(case["title"])


def _investigation_citations(case: Mapping[str, Any]) -> list[str]:
    citations = []
    for evidence in case["evidence"]:
        for candidate in (evidence["source_url"], evidence["artifact_url"]):
            href = _investigation_href(candidate)
            if href != "#" and href not in citations:
                citations.append(href)
    return citations


def _investigations_index_json_ld(
    investigations: Mapping[str, Any],
) -> dict[str, Any]:
    url = f"{SITE}/news/investigations/"
    return {
        "@context": "https://schema.org",
        "@graph": [
            _organization(),
            {
                "@type": "CollectionPage",
                "@id": url,
                "url": url,
                "name": "Palimpsest Investigations",
                "description": investigations["scope"],
                "dateModified": investigations["generated_at"],
                "publisher": {"@id": f"{SITE}/#organization"},
                "mainEntity": {
                    "@type": "ItemList",
                    "numberOfItems": investigations["n_cases"],
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": position,
                            "url": _case_public_url(case),
                            "name": case["title"],
                        }
                        for position, case in enumerate(investigations["cases"], 1)
                    ],
                },
            },
        ],
    }


def _investigation_case_json_ld(case: Mapping[str, Any]) -> dict[str, Any]:
    public_url = _case_public_url(case)
    common = {
        "@id": public_url,
        "url": public_url,
        "name": case["title"],
        "description": case["dek"],
        "dateModified": case["updated_at"],
        "inLanguage": _case_language(case),
        "isAccessibleForFree": True,
        "publisher": _organization(),
        "about": case["testable_question"],
        "citation": _investigation_citations(case),
    }
    if case["status"] == "published":
        return {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            **common,
            "headline": case["title"],
            "datePublished": case["published_at"],
            "articleSection": "Investigations",
            "mainEntityOfPage": {"@type": "WebPage", "@id": public_url},
            "author": _organization(),
            "image": [OG_IMAGE],
        }
    return {
        "@context": "https://schema.org",
        "@type": "Report",
        **common,
        "creativeWorkStatus": f"Research lead — {case['status'].replace('_', ' ')}",
    }


def _investigation_card(case: Mapping[str, Any]) -> str:
    kind, status = _case_status_label(case)
    state = _case_publication_state(case)
    language = _case_language(case)
    question_language = _text_language(case["testable_question"])
    n_groups = len({evidence["independence_group"] for evidence in case["evidence"]})
    return f"""<article class="nw-investigation-card" data-publication-state="{_h(state)}">
  <p class="nw-investigation-card__status"><span class="nw-dot" aria-hidden="true"></span>{_h(kind)} · {_h(status)}</p>
  <h3 lang="{_h(language)}"><a href="{_h(case['url'])}">{_h(case['title'])}</a></h3>
  <p class="nw-investigation-card__question" lang="{_h(question_language)}"><strong>Question under test:</strong> {_h(case['testable_question'])}</p>
  <p>{_h(case['status_reason'])}</p>
  <p class="nw-investigation-card__meta">{len(case['claims'])} claim record{'s' if len(case['claims']) != 1 else ''} · {len(case['evidence'])} evidence receipt{'s' if len(case['evidence']) != 1 else ''} · {n_groups} upstream group{'s' if n_groups != 1 else ''}<br>Updated <time datetime="{_h(case['updated_at'])}">{_h(_human_time(case['updated_at']))}</time></p>
</article>"""


def _investigation_register(
    *,
    title: str,
    label: str,
    description: str,
    cases: Sequence[Mapping[str, Any]],
    section_id: str,
) -> str:
    cards = "".join(_investigation_card(case) for case in cases)
    if not cards:
        cards = (
            '<div class="nw-empty-register"><strong>No case currently carries '
            f"this status.</strong><p>{_h(description)}</p></div>"
        )
    return f"""<section class="nw-investigation-register" aria-labelledby="{_h(section_id)}">
  <header><div><p class="nw-section__label">{_h(label)}</p><h2 id="{_h(section_id)}">{_h(title)}</h2></div><p>{_h(description)}</p></header>
  <div class="nw-investigation-grid">{cards}</div>
</section>"""


def _investigations_feature(
    investigations: Mapping[str, Any] | None,
) -> str:
    if investigations is None:
        return ""
    cases = investigations["cases"]
    published = [case for case in cases if case["status"] == "published"]
    open_cases = [
        case for case in cases
        if case["status"] in {"evidence_gathering", "review_ready"}
    ]
    abstained = [case for case in cases if case["status"] == "abstained"]
    featured = next(iter(published), cases[0] if cases else None)
    featured_case = ""
    if featured is not None:
        kind, status = _case_status_label(featured)
        featured_case = f"""<p class="nw-case-status" data-publication-state="{_h(_case_publication_state(featured))}">{_h(kind)} · {_h(status)}</p>
    <h3 lang="{_h(_case_language(featured))}">{_h(featured['title'])}</h3>
    <p><strong>Question under test:</strong> {_h(featured['testable_question'])}</p>"""
    return f"""<section class="nw-investigations-feature" id="investigations" aria-labelledby="investigations-feature-title" data-file-code="INV / {investigations['n_cases']:03d}">
  <div class="nw-investigations-feature__rail"><p class="nw-section__label">Investigations desk</p><strong>{investigations['n_cases']}</strong><span>{len(published)} published · {len(open_cases)} open · {len(abstained)} abstained</span></div>
  <div><h2 id="investigations-feature-title">The evidence threshold is part of the story</h2>
    <p>An investigation is a reviewed evidence synthesis, not a truth score. Open automated work remains a research lead and cannot borrow the authority of a published investigation.</p>
    {featured_case}
    <div class="nw-actions"><a class="nw-actions__primary" href="/news/investigations/">Open the investigations register</a><a href="/readings/investigations-latest.json">Structured desk</a><a href="/docs/INVESTIGATIONS.md">Publication method</a></div>
  </div>
</section>"""


def _machine_analysis_feature(
    analyses: Mapping[str, Any] | None,
) -> str:
    """Render the newsroom's machine-analysis lane without blurring authorship."""

    if analyses is None:
        return ""
    cases = analyses["cases"]
    published = [case for case in cases if case["report_type"] == "AnalysisReport"]
    abstained = [case for case in cases if case["report_type"] == "AbstentionReport"]
    featured = next(iter(published), cases[0] if cases else None)
    feature = ""
    if featured is not None:
        feature = f"""<p class="nw-case-status" data-publication-state="{_h(featured['status'])}">Deterministic machine analysis · {_h(featured['report_type'])}</p>
    <h3 lang="{_h(_text_language(featured['title']))}">{_h(featured['title'])}</h3>
    <p>{_h(featured['dek'])}</p>"""
    return f"""<section class="nw-analysis-feature" id="machine-analysis" aria-labelledby="machine-analysis-title" data-file-code="MACHINE / {analyses['n_cases']:03d}">
  <div class="nw-analysis-feature__rail"><p class="nw-section__label">Analysis desk</p><strong>{analyses['n_cases']}</strong><span>{len(published)} analyses · {len(abstained)} abstentions</span></div>
  <div><h2 id="machine-analysis-title">The machine can analyse. It cannot interview.</h2>
    <p><strong>Deterministic machine analysis · no human interview.</strong> Every sentence is bound to named evidence receipts. Source-lineage de-duplication, countercases, limits, falsifiers and evaluation gates stay visible; a failed gate publishes an abstention, not synthetic certainty.</p>
    {feature}
    <div class="nw-actions"><a class="nw-actions__primary" href="/news/analysis/">Open the machine-analysis desk</a><a href="/readings/machine-investigations-latest.json">Structured reports</a><a href="/docs/MACHINE-INVESTIGATIONS.md">Deterministic method</a></div>
  </div>
</section>"""


def _accountability_tape(wire: Mapping[str, Any]) -> str:
    coverage = wire["coverage"]
    counts = coverage["counts"]
    source_rows = "".join(
        f"""<li data-status="{_h(source['status'])}"><strong>{_h(source['source_name'])}</strong><span>{_h(source['status'])}</span><small>{source['accepted_items']} accepted · {source['rejected_items']} rejected</small></li>"""
        for source in coverage["sources"]
    )
    return f"""<aside class="nw-tape" aria-labelledby="tape-title">
  <div class="nw-tape__head"><div><p class="nw-kicker">Accountability tape</p><h2 id="tape-title">Every feed answered for</h2></div>
  <p>{coverage['accepted_items']} accepted items · {coverage['rejected_items']} rejected or out-of-window · {counts['fetch_error']} fetch failures · {counts['parse_error']} malformed feeds · {counts['stale']} stale feeds.</p></div>
  <ul>{source_rows}</ul>
</aside>"""


def _instrument_sections(
    feed: Mapping[str, Any], *, exclude_signal_id: str | None = None
) -> str:
    blocks = []
    for section in feed["sections"]:
        stories = [
            story for story in feed["stories"]
            if story["section"] == section["id"]
            and story["signal_id"] != exclude_signal_id
        ]
        if not stories:
            continue
        cards = "".join(_story_card(story, section["title"]) for story in stories)
        blocks.append(f"""<section class="nw-section nw-section--instruments" id="instrument-{_h(section['id'])}">
  <div class="nw-section__head">
    <div><p class="nw-section__label">Palimpsest measurements</p><h2>{_h(section['title'])}</h2></div>
    <p class="nw-section__dek">{_h(section['dek'])}</p>
  </div>
  <div class="nw-grid">{cards}</div>
</section>""")
    return "".join(blocks)


def render_evidence_index(
    feed: Mapping[str, Any],
    wire: Mapping[str, Any],
    pulse: Mapping[str, Any] | None,
    investigations: Mapping[str, Any] | None = None,
    machine_analyses: Mapping[str, Any] | None = None,
) -> str:
    events = wire["events"]
    if not events:
        return render_index(feed)
    source_lead = _select_lead(events)
    instrument_lead = _select_instrument_lead(feed["stories"])
    sections = {section["id"]: section for section in feed["sections"]}
    event_navigation, event_blocks = _event_sections(
        wire, lead_event_id=source_lead["event_id"]
    )
    coverage = wire["coverage"]
    instrument_coverage = feed["coverage"]
    investigations_nav = (
        '<li><a href="#investigations">Investigations</a></li>'
        if investigations is not None else ""
    )
    investigations_count = (
        f" · {investigations['n_cases']} investigation case files"
        if investigations is not None else ""
    )
    analysis_nav = (
        '<li><a href="#machine-analysis">Machine analysis</a></li>'
        if machine_analyses is not None else ""
    )
    analysis_count = (
        f" · {machine_analyses['n_cases']} machine reports"
        if machine_analyses is not None else ""
    )
    body = f"""<body class="ps newsroom-page newsroom-page--evidence-wire">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <header class="nw-masthead">
    <div class="nw-masthead__top">
      <p class="nw-wordmark">Palimpsest <span>Evidence desk</span></p>
      <p class="nw-edition"><strong>Current edition</strong>{_h(_human_time(wire['generated_at']))}<br>{feed['n_stories']} measurements · {wire['n_events']} source records{investigations_count}{analysis_count}</p>
    </div>
    <h1 class="nw-masthead__headline">Measurements first. Source reports clearly labeled.</h1>
    <p class="nw-masthead__dek">This is not a replacement newspaper. Palimpsest publishes its own measured results, then keeps publisher reports in a separate source index with attribution, source structure, revisions and unknowns visible.</p>
  </header>
  <div class="nw-meta-line"><span>Results · source index · investigations</span><span>Window {_h(_human_time(wire['window']['from']))} → {_h(_human_time(wire['window']['to']))}</span><a href="/news/instruments/feed.xml">Measurements-only RSS</a><a href="/feeds/">All feeds</a><a href="/readings/newswire-latest.json">Structured source index</a></div>
  <div class="nw-status-strip" role="status" aria-label="Edition coverage">
    <span><i class="nw-dot nw-dot--live" aria-hidden="true"></i><strong>{instrument_coverage['live']}/{instrument_coverage['total']}</strong> measurements live</span>
    <span><i class="nw-dot nw-dot--warning" aria-hidden="true"></i><strong>{coverage['successful_sources']}/{coverage['registry_sources']}</strong> feeds answered</span>
    <span><i class="nw-dot nw-dot--missing" aria-hidden="true"></i><strong>{coverage['rejected_items']}</strong> rejected / out-of-window</span>
    <span><strong>{wire['n_events']}</strong> attributed source records</span>
  </div>
  <nav class="nw-task-strip" aria-label="Start with a task"><a href="#latest-measurement"><strong>See a Palimpsest result</strong><span>Measurement + receipt + limit</span></a><a href="#source-index"><strong>Look up a publisher report</strong><span>Attributed source index</span></a><a href="/feeds/"><strong>Choose a feed</strong><span>Purpose and boundary first</span></a><a href="/developers.html"><strong>Use the data</strong><span>API + MCP + files</span></a></nav>
  <nav aria-label="Evidence desk sections"><ul class="nw-section-nav"><li><a href="#latest-measurement">Latest measurement</a></li><li><a href="#economy">Economic state</a></li>{analysis_nav}{investigations_nav}<li><a href="#instruments">More measurements</a></li><li><a href="#source-index">Source index</a></li>{event_navigation}<li><a href="#tape-title">Feed coverage</a></li></ul></nav>
  <div id="latest-measurement">{_lead(instrument_lead, sections[instrument_lead['section']]['title'], heading_level=2, heading_id='latest-measurement-title')}</div>
  {_economic_panel(pulse)}
  {_machine_analysis_feature(machine_analyses)}
  {_investigations_feature(investigations)}
  <div id="instruments" class="nw-instrument-heading"><p class="nw-kicker">Palimpsest results</p><h2>More current measurements</h2><p>These are Palimpsest's own mutable latest-state briefs. Each one names its source bytes, freshness, denominator and limitation.</p></div>
  {_instrument_sections(feed, exclude_signal_id=instrument_lead['signal_id'])}
  {_event_lead(source_lead, wire)}
  {event_blocks}
  {_accountability_tape(wire)}
</main>
<footer class="nw-footer"><div class="nw-shell">Palimpsest publishes measurements and maintains an attributed publisher source index. A source record is not an independent finding. <a href="/feeds/">Feeds by purpose</a> · <a href="/news/analysis/">Machine analysis</a> · <a href="/news/investigations/">Investigations register</a> · <a href="/news/standards/">Reporting standards</a> · <a href="https://github.com/beepboop2025/palimpsest">Source code</a>.</div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    return _head(
        title="Palimpsest evidence desk · measurements and attributed source reports",
        description="Palimpsest measurements first, plus a clearly labeled publisher source index with receipts, revisions, source independence and limits.",
        canonical=feed["url"],
        page_type="website",
        modified_at=max(feed["generated_at"], wire["generated_at"]),
        json_ld=_wire_index_json_ld(feed, wire),
    ) + "\n" + body


def render_investigations_index(investigations: Mapping[str, Any]) -> str:
    cases = investigations["cases"]
    published = [case for case in cases if case["status"] == "published"]
    open_cases = [
        case for case in cases
        if case["status"] in {"evidence_gathering", "review_ready"}
    ]
    abstained = [case for case in cases if case["status"] == "abstained"]
    body = f"""<body class="ps newsroom-page newsroom-page--investigations">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <header class="nw-investigations-head">
    <p class="nw-section__label">Public case register</p>
    <h1>Investigations and research leads</h1>
    <p class="nw-investigations-head__dek">Reviewed reporting, open evidence gathering and editorial abstention remain separate public states. Every case shows the question, receipts, counterevidence, falsifiers and unresolved collection targets.</p>
  </header>
  <div class="nw-meta-line"><span>Aggregate public evidence · no person-level records</span><span>Updated <time datetime="{_h(investigations['generated_at'])}">{_h(_human_time(investigations['generated_at']))}</time></span><a href="/readings/investigations-latest.json">Structured desk</a><a href="/docs/INVESTIGATIONS.md">Publication method</a></div>
  <div class="nw-status-strip" role="status" aria-label="Investigation publication states">
    <span><i class="nw-dot nw-dot--live" aria-hidden="true"></i><strong>{len(published)}</strong> published</span>
    <span><i class="nw-dot nw-dot--warning" aria-hidden="true"></i><strong>{len(open_cases)}</strong> open research leads</span>
    <span><i class="nw-dot nw-dot--missing" aria-hidden="true"></i><strong>{len(abstained)}</strong> abstained</span>
    <span><strong>{investigations['n_cases']}</strong> total case files</span>
  </div>
  <nav aria-label="Investigation registers"><ul class="nw-section-nav"><li><a href="#published-investigations">Published</a></li><li><a href="#open-research">Open research</a></li><li><a href="#editorial-abstentions">Abstentions</a></li></ul></nav>
  <p class="nw-investigation-notice"><strong>Publication boundary.</strong> An investigation is a reviewed evidence synthesis, not a truth score. Each finding shows supporting evidence, disconfirming evidence, a falsification test and limits. Automated work is labelled <strong>RESEARCH LEAD</strong>, never presented as a completed investigation.</p>
  {_investigation_register(title='Published investigations', label='Reviewed publication', description='Only cases that passed the structured publication gate and editorial review appear here.', cases=published, section_id='published-investigations')}
  {_investigation_register(title='Open research leads', label='Evidence gathering', description='Questions and draft claims remain under test. These cases are not published findings.', cases=open_cases, section_id='open-research')}
  {_investigation_register(title='Editorial abstentions', label='Threshold not met', description='The desk records why available evidence cannot support publication and what would be needed to revisit the question.', cases=abstained, section_id='editorial-abstentions')}
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/">← Palimpsest Wire</a> · <a href="/news/standards/">Reporting standards</a> · <a href="/readings/investigations-latest.json">Structured investigations desk</a> · <a href="/docs/INVESTIGATIONS.md">Method and safety boundary</a></div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    return _head(
        title="Palimpsest Investigations · public evidence case files",
        description=(
            "Reviewed investigations and open research leads with claims, "
            "counterevidence, falsification tests, limitations and revision receipts."
        ),
        canonical=f"{SITE}/news/investigations/",
        page_type="website",
        modified_at=investigations["generated_at"],
        json_ld=_investigations_index_json_ld(investigations),
    ) + "\n" + body


def _investigation_value(evidence: Mapping[str, Any]) -> str:
    if evidence["value_type"] == "null":
        return "No scalar value asserted"
    value = evidence["value"]
    if evidence["value_type"] == "boolean":
        return "true" if value else "false"
    if evidence["value_type"] in {"integer", "number"}:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    return str(value)


def _investigation_evidence_table(case: Mapping[str, Any]) -> str:
    rows = []
    for evidence in case["evidence"]:
        source_link = (
            f'<a href="{_h(_investigation_href(evidence["source_url"]))}">Source record</a>'
            if evidence["source_url"]
            else "No source URL recorded"
        )
        rows.append(f"""<tr>
  <td><span class="nw-evidence-relation" data-relation="{_h(evidence['role'])}">{_h(evidence['role'])}</span><small>{_h(evidence['source_class'])}</small></td>
  <td><strong lang="{_h(_text_language(evidence['label']))}">{_h(evidence['label'])}</strong><small><code>{_h(evidence['evidence_id'])}</code> · {_h(evidence['independence_group'])}</small></td>
  <td><strong>{_h(_investigation_value(evidence))}</strong><small>Selector <code>{_h(evidence['selector'])}</code></small></td>
  <td>{_h(evidence['interpretation_limit'])}</td>
  <td>{source_link}<small><a href="{_h(_investigation_href(evidence['artifact_url']))}">Artifact</a> · <time datetime="{_h(evidence['source_timestamp'])}">{_h(_human_time(evidence['source_timestamp']))}</time> · sha {_h(evidence['artifact_sha256'][:12])} · {_h(evidence['freshness'])}</small></td>
</tr>""")
    if not rows:
        return '<div class="nw-empty-register"><strong>No evidence receipt is recorded.</strong></div>'
    return f"""<p class="nw-table-cue" id="investigation-evidence-cue">Scroll horizontally to inspect every evidence field.</p>
<div class="nw-table-wrap" role="region" tabindex="0" aria-labelledby="case-evidence-title" aria-describedby="investigation-evidence-cue"><table class="nw-evidence-table"><caption>Evidence receipts for this case file</caption><thead><tr><th scope="col">Relation / class</th><th scope="col">Receipt / upstream group</th><th scope="col">Recorded value</th><th scope="col">Interpretation limit</th><th scope="col">Provenance / integrity</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"""


def _investigation_claims(case: Mapping[str, Any]) -> str:
    evidence_by_id = {
        evidence["evidence_id"]: evidence for evidence in case["evidence"]
    }
    counter_by_id = {
        item["counterevidence_id"]: item for item in case["counterevidence"]
    }
    limitation_by_id = {
        item["limitation_id"]: item for item in case["limitations"]
    }
    rows = []
    for claim in case["claims"]:
        linked_evidence = "".join(
            f"<li><code>{_h(evidence_id)}</code> · {_h(evidence_by_id[evidence_id]['label'])} · {_h(evidence_by_id[evidence_id]['role'])}</li>"
            for evidence_id in claim["evidence_ids"]
        ) or "<li>No evidence receipt is linked.</li>"
        linked_counter = "".join(
            f"<li><code>{_h(counter_id)}</code> · {_h(counter_by_id[counter_id]['statement'])} · {_h(counter_by_id[counter_id]['disposition'])}</li>"
            for counter_id in claim["counterevidence_ids"]
        ) or "<li>No counterevidence record is linked.</li>"
        linked_limits = "".join(
            f"<li><strong>{_h(limitation_by_id[limit_id]['statement'])}</strong> {_h(limitation_by_id[limit_id]['consequence'])}</li>"
            for limit_id in claim["limitation_ids"]
        ) or "<li>No claim-specific limitation is linked.</li>"
        noun = "Finding" if case["status"] == "published" else "Claim under test"
        rows.append(f"""<li class="nw-finding" data-confidence="{_h(claim['confidence'])}">
  <p class="nw-finding__label">{_h(noun)} · {_h(claim['type'].replace('_', ' '))} · {_h(claim['confidence'])} · {_h(claim['publication_state'])}</p>
  <h3 lang="{_h(_text_language(claim['statement']))}">{_h(claim['statement'])}</h3>
  <div class="nw-case-columns"><div class="nw-case-panel"><h4>Linked evidence receipts</h4><ul>{linked_evidence}</ul></div><div class="nw-case-panel nw-case-panel--counter"><h4>Linked counterevidence</h4><ul>{linked_counter}</ul></div></div>
  <div class="nw-finding__boundary"><strong>Claim limits.</strong><ul>{linked_limits}</ul></div>
</li>""")
    return "".join(rows) or (
        '<li class="nw-empty-register"><strong>No claim has been recorded. '
        "The case therefore makes no finding.</strong></li>"
    )


def _hypotheses_panel(case: Mapping[str, Any]) -> str:
    rows = "".join(
        f"""<li><strong lang="{_h(_text_language(item['statement']))}">{_h(item['statement'])}</strong><span>{_h(item['status'])} · <code>{_h(item['hypothesis_id'])}</code></span><p><strong>Linked falsification tests:</strong> {_h(', '.join(item['falsification_condition_ids']) or 'none linked')}</p></li>"""
        for item in case["hypotheses"]
    ) or "<li>No hypothesis is recorded. The case therefore cannot advance beyond evidence gathering.</li>"
    return f"""<div class="nw-case-panel"><h3>Hypotheses under test</h3><ul class="nw-case-record-list">{rows}</ul></div>"""


def _counterevidence_panel(case: Mapping[str, Any]) -> str:
    rows = "".join(
        f"""<li><strong lang="{_h(_text_language(item['statement']))}">{_h(item['statement'])}</strong><span>{_h(item['review_status'])} · {_h(item['disposition'])} · evidence {_h(', '.join(item['evidence_ids']) or 'none linked')}</span></li>"""
        for item in case["counterevidence"]
    ) or "<li>No counterevidence record is currently available.</li>"
    return f"""<div class="nw-case-panel nw-case-panel--counter"><h3>Counterevidence and competing records</h3><ul class="nw-case-record-list">{rows}</ul></div>"""


def _falsification_panel(case: Mapping[str, Any]) -> str:
    rows = "".join(
        f"""<li><strong lang="{_h(_text_language(item['statement']))}">{_h(item['statement'])}</strong><span>Status: {_h(item['status'])}</span><p><strong>Evidence needed:</strong> {_h(item['evidence_needed'])}</p></li>"""
        for item in case["falsification_conditions"]
    ) or "<li>No falsification condition is recorded; the publication gate must remain blocked.</li>"
    return f"""<div class="nw-case-panel nw-case-panel--target"><h3>Falsification tests</h3><ul class="nw-case-record-list">{rows}</ul></div>"""


def _publication_gate(case: Mapping[str, Any]) -> str:
    gate = case["publication_gate"]
    rows = "".join(
        f"""<tr><td><strong>{_h(check['label'])}</strong><small><code>{_h(check['check_id'])}</code></small></td><td>{_h(check['minimum'])}</td><td>{_h(check['observed'])}</td><td>{'Passed' if check['passed'] else 'Not passed'}</td><td>{_h(check['detail'])}</td></tr>"""
        for check in gate["checks"]
    )
    return f"""<p class="nw-table-cue" id="publication-gate-cue">Scroll horizontally to inspect every publication check.</p>
<div class="nw-table-wrap" role="region" tabindex="0" aria-labelledby="publication-gate-title" aria-describedby="publication-gate-cue"><table class="nw-evidence-table"><caption>Structured publication-gate checks</caption><thead><tr><th scope="col">Check</th><th scope="col">Minimum</th><th scope="col">Observed</th><th scope="col">Result</th><th scope="col">Detail</th></tr></thead><tbody>{rows}</tbody></table></div>
<p class="nw-method-note"><strong>Gate {_h(gate['status'])}.</strong> Publishable: {_h(str(gate['publishable']).lower())}. Failed checks: {_h(', '.join(gate['failed_check_ids']) or 'none')}.</p>"""


def _collection_targets(case: Mapping[str, Any]) -> str:
    rows = []
    for target in case["collection_targets"]:
        evidence_link = (
            f'<a href="{_h(_investigation_href(target["evidence_url"]))}">Collected evidence</a>'
            if target["evidence_url"]
            else "No evidence URL recorded"
        )
        blocker = target["blocker"] or "No blocker recorded"
        rows.append(f"""<div class="nw-case-panel nw-case-panel--target"><p class="nw-finding__label">{_h(target['status'])} · {_h(target['data_level'])}</p><h3>{_h(target['source_id'])}</h3><p>{_h(target['question_answered'])}</p><p><strong>Blocker:</strong> {_h(blocker)}</p><p>{evidence_link}</p></div>""")
    return "".join(rows) or (
        '<div class="nw-empty-register"><strong>No collection target is recorded.</strong></div>'
    )


def _methodology_steps(case: Mapping[str, Any]) -> str:
    return "".join(
        f"""<li><strong>{_h(step['step_id'])}</strong><span>{_h(step['description'])}</span><small>Reproducible: {_h(str(step['reproducible']).lower())}</small></li>"""
        for step in case["methodology"]
    ) or "<li>No methodology step is recorded.</li>"


def _safety_lists(case: Mapping[str, Any]) -> str:
    safety = case["safety"]
    prohibited = "".join(
        f"<li>{_h(value)}</li>" for value in safety["prohibited_interpretations"]
    ) or "<li>No prohibited interpretation is recorded.</li>"
    allegations = "".join(
        f"<li>{_h(value)}</li>" for value in safety["allegations"]
    ) or "<li>No allegation is made.</li>"
    motives = "".join(
        f"<li>{_h(value)}</li>" for value in safety["inferred_motives"]
    ) or "<li>No motive is inferred.</li>"
    return f"""<div class="nw-case-columns"><div class="nw-case-panel nw-case-panel--safety"><h3>Prohibited interpretations</h3><ul>{prohibited}</ul></div><div class="nw-case-panel"><h3>Allegations and motives</h3><ul>{allegations}{motives}</ul></div></div>"""


def render_investigation_case(case: Mapping[str, Any]) -> str:
    kind, status = _case_status_label(case)
    state = _case_publication_state(case)
    published = case["published_at"]
    published_display = _human_time(published) if published else "Not published"
    correction = case["correction"]
    reply = case["right_to_reply"]
    safety = case["safety"]
    correction_time = (
        _human_time(correction["last_corrected_at"])
        if correction["last_corrected_at"] else "No correction timestamp"
    )
    reply_parties = "".join(
        f"<li><strong>{_h(party['display_name'])}</strong> · {_h(party['party_type'])} · {_h(party['disposition'])}</li>"
        for party in reply["parties"]
    ) or "<li>No institution is recorded for reply.</li>"
    limits = "".join(
        f"<li><strong>{_h(item['statement'])}</strong><span>{_h(item['consequence'])}</span></li>"
        for item in case["limitations"]
    ) or "<li>No case-level limitation is recorded.</li>"
    open_notice = ""
    if case["status"] != "published":
        open_notice = (
            '<p class="nw-investigation-notice"><strong>RESEARCH LEAD · NOT A '
            "PUBLISHED INVESTIGATION.</strong> Draft claims remain under test and "
            "must not be read as findings.</p>"
        )
    body = f"""<body class="ps newsroom-page newsroom-page--investigation-case">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <article class="nw-case-file" data-publication-state="{_h(state)}">
    <header class="nw-case-file__header">
      <p class="nw-case-status" data-publication-state="{_h(state)}"><span class="nw-dot" aria-hidden="true"></span>{_h(kind)} · {_h(status)}</p>
      <h1 lang="{_h(_case_language(case))}">{_h(case['title'])}</h1>
      <p class="nw-case-file__question" lang="{_h(_text_language(case['testable_question']))}"><strong>Testable question:</strong> {_h(case['testable_question'])}</p>
      <p>{_h(case['dek'])}</p>
      <p><strong>Current status:</strong> {_h(case['status_reason'])}</p>
    </header>
    {open_notice}
    <div class="nw-case-file__meta">
      <div><dl><dt>Opened</dt><dd><time datetime="{_h(case['opened_at'])}">{_h(_human_time(case['opened_at']))}</time></dd></dl></div>
      <div><dl><dt>Updated</dt><dd><time datetime="{_h(case['updated_at'])}">{_h(_human_time(case['updated_at']))}</time></dd></dl></div>
      <div><dl><dt>Published</dt><dd>{_h(published_display)}</dd></dl></div>
      <div><dl><dt>Version receipt</dt><dd><code>{_h(case['version_id'])}</code><br><a href="revisions/{_h(case['version_id'])}.json">Immutable revision JSON</a></dd></dl></div>
    </div>
    <section class="nw-case-section" aria-labelledby="case-findings-title">
      <header><p class="nw-section__label">Claims and challenges</p><h2 id="case-findings-title">What is asserted—and what could overturn it</h2><p>Claim wording is reproduced from the structured record. Confidence and review state are not probability scores.</p></header>
      {_hypotheses_panel(case)}
      <ol class="nw-finding-list">{_investigation_claims(case)}</ol>
      <div class="nw-case-columns">{_counterevidence_panel(case)}{_falsification_panel(case)}</div>
    </section>
    <section class="nw-case-section" aria-labelledby="case-evidence-title">
      <header><p class="nw-section__label">Evidence ledger</p><h2 id="case-evidence-title">Inspect every receipt and interpretation limit</h2></header>
      {_investigation_evidence_table(case)}
    </section>
    <section class="nw-case-section" aria-labelledby="publication-gate-title">
      <header><p class="nw-section__label">Editorial threshold</p><h2 id="publication-gate-title">Publication gate</h2><p>A blocked gate keeps this work in the research-lead register regardless of how striking an individual measurement appears.</p></header>
      {_publication_gate(case)}
    </section>
    <section class="nw-case-section" aria-labelledby="limitations-title">
      <header><p class="nw-section__label">Epistemic boundary</p><h2 id="limitations-title">Limitations and consequences</h2></header>
      <ul class="nw-case-record-list">{limits}</ul>
    </section>
    <section class="nw-case-section" aria-labelledby="collection-targets-title">
      <header><p class="nw-section__label">Open collection</p><h2 id="collection-targets-title">Evidence still needed</h2><p>Targets name aggregate public evidence to collect. They never identify people to target.</p></header>
      <div class="nw-case-columns">{_collection_targets(case)}</div>
    </section>
    <section class="nw-case-section" aria-labelledby="methodology-title">
      <header><p class="nw-section__label">Reproducibility</p><h2 id="methodology-title">Methodology steps</h2></header>
      <ol class="nw-case-record-list">{_methodology_steps(case)}</ol>
    </section>
    <section class="nw-case-section" aria-labelledby="editorial-state-title">
      <header><p class="nw-section__label">Accountability</p><h2 id="editorial-state-title">Correction, reply and safety state</h2></header>
      <div class="nw-case-state-grid">
        <div><dl><dt>Correction</dt><dd>{_h(correction['status'])}<br>{_h(correction['note'])}<br>{_h(correction_time)}<br><a href="{_h(_investigation_href(correction['policy_url']))}">Correction policy</a></dd></dl></div>
        <div><dl><dt>Right to reply</dt><dd>{_h(reply['status'])}<br>{_h(reply['applicability_reason'])}<br>{len(reply['parties'])} institution{'s' if len(reply['parties']) != 1 else ''} recorded</dd></dl></div>
        <div><dl><dt>Safety</dt><dd>{_h(safety['data_level'])}<br>Person-level data: {_h(str(safety['person_level_data']).lower())}</dd></dl></div>
        <div><dl><dt>Current structured record</dt><dd><a href="case.json">case.json</a><br><code>{_h(case['case_id'])}</code></dd></dl></div>
      </div>
      <div class="nw-case-columns"><div class="nw-case-panel"><h3>Right-to-reply boundary</h3><p>No response is not evidence that a finding is true.</p></div><div class="nw-case-panel"><h3>Correction boundary</h3><p>Corrections append an immutable revision and preserve the previous public version.</p></div></div>
      <div class="nw-case-panel"><h3>Institutional reply register</h3><ul>{reply_parties}</ul></div>
      <p class="nw-investigation-notice"><strong>Safety boundary.</strong> Public, aggregate evidence only; private contact details, volunteer identifiers and person-level records are excluded.</p>
      {_safety_lists(case)}
    </section>
  </article>
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/investigations/">← Investigations register</a> · <a href="case.json">Current case JSON</a> · <a href="/readings/investigations-latest.json">Structured desk</a> · <a href="/docs/INVESTIGATIONS.md">Method</a></div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    is_published = case["status"] == "published"
    return _head(
        title=f"{case['title']} · Palimpsest Investigations",
        description=case["dek"],
        canonical=_case_public_url(case),
        page_type="article" if is_published else "website",
        published_at=case["published_at"] if is_published else None,
        modified_at=case["updated_at"],
        json_ld=_investigation_case_json_ld(case),
    ) + "\n" + body


def _machine_case_public_url(case: Mapping[str, Any]) -> str:
    path = str(case["url"])
    if path.startswith(f"{SITE}/news/analysis/"):
        return path
    if not path.startswith("/news/analysis/") or path.startswith("//"):
        raise newsroom.NewsroomError(f"invalid machine-analysis URL: {path!r}")
    return SITE + path


def _machine_is_article(case: Mapping[str, Any]) -> bool:
    """Only a successful analysis may receive article discovery metadata."""

    return (
        case["status"] == "published"
        and case["report_type"] == "AnalysisReport"
    )


def _machine_evidence_id(evidence: Mapping[str, Any]) -> str:
    for key in (
        "evidence_id", "citation_id", "resource_id", "receipt_id", "id"
    ):
        value = evidence.get(key)
        if isinstance(value, str) and value:
            return value
    return "unlabelled-evidence"


def _machine_fragment(prefix: str, value: object) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _machine_case_attributions(
    case: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    context = _load_machine_evidence_context()
    result: dict[str, Mapping[str, Any]] = {}
    for evidence in case["evidence"]:
        _raw, raw_document = _machine_read_cited_input(evidence)
        evidence_id = _machine_evidence_id(evidence)
        if evidence_id in result:
            raise newsroom.NewsroomError(
                f"duplicate machine evidence attribution: {evidence_id}"
            )
        result[evidence_id] = _machine_attribution_metadata(
            evidence, raw_document, context
        )
    return result


def _machine_evidence_urls(
    case: Mapping[str, Any],
    attributions: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    urls: list[str] = []
    for evidence in case["evidence"]:
        for key in ("source_url", "artifact_url", "public_url", "url"):
            value = evidence.get(key)
            if not value:
                continue
            href = _investigation_href(value)
            if href != "#" and href not in urls:
                urls.append(href)
        if attributions is not None:
            attribution = attributions[_machine_evidence_id(evidence)]["attribution"]
            for value in (
                attribution["public_source_url"],
                *attribution["upstream_source_urls"],
            ):
                href = _investigation_href(value)
                if href != "#" and href not in urls:
                    urls.append(href)
    return urls


def _machine_index_json_ld(analyses: Mapping[str, Any]) -> dict[str, Any]:
    url = f"{SITE}/news/analysis/"
    return {
        "@context": "https://schema.org",
        "@graph": [
            _organization(),
            {
                "@type": "CollectionPage",
                "@id": url,
                "url": url,
                "name": "Palimpsest Machine Analysis",
                "description": analyses["scope"],
                "dateModified": analyses["generated_at"],
                "publisher": {"@id": f"{SITE}/#organization"},
                "mainEntity": {
                    "@type": "ItemList",
                    "numberOfItems": analyses["n_cases"],
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": position,
                            "url": _machine_case_public_url(case),
                            "name": case["title"],
                        }
                        for position, case in enumerate(analyses["cases"], 1)
                    ],
                },
            },
        ],
    }


def _machine_case_json_ld(
    case: Mapping[str, Any],
    attributions: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    public_url = _machine_case_public_url(case)
    common: dict[str, Any] = {
        "@id": public_url,
        "url": public_url,
        "name": case["title"],
        "description": case["dek"],
        "dateModified": case["updated_at"],
        "inLanguage": _text_language(case["title"]),
        "isAccessibleForFree": True,
        "publisher": _organization(),
        "citation": _machine_evidence_urls(case, attributions),
        "genre": "Deterministic machine analysis",
        "usageInfo": "No human interview; public aggregate evidence only.",
    }
    if _machine_is_article(case):
        return {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            **common,
            "additionalType": f"{SITE}/protocol/machine-investigations-v1.schema.json#AnalysisReport",
            "headline": case["title"],
            "datePublished": case["published_at"],
            "articleSection": "Machine analysis",
            "mainEntityOfPage": {"@type": "WebPage", "@id": public_url},
            "author": {
                "@type": "Organization",
                "name": "Palimpsest Machine Analysis Desk",
                "url": f"{SITE}/news/analysis/",
            },
            "image": [OG_IMAGE],
        }
    return {
        "@context": "https://schema.org",
        "@type": "Report",
        **common,
        "additionalType": f"{SITE}/protocol/machine-investigations-v1.schema.json#AbstentionReport",
        "creativeWorkStatus": "Abstained — evidence gate not met",
    }


def _machine_case_card(case: Mapping[str, Any]) -> str:
    is_article = _machine_is_article(case)
    state = "published" if is_article else "abstained"
    label = "ANALYSIS REPORT" if is_article else "ABSTENTION REPORT"
    return f"""<article class="nw-investigation-card nw-analysis-card" data-publication-state="{state}">
  <p class="nw-investigation-card__status"><span class="nw-dot" aria-hidden="true"></span>Deterministic machine analysis · {label}</p>
  <h3 lang="{_h(_text_language(case['title']))}"><a href="{_h(case['url'])}">{_h(case['title'])}</a></h3>
  <p>{_h(case['dek'])}</p>
  <p><strong>Gate result:</strong> {_h(case['status_reason'])}</p>
  <p class="nw-investigation-card__meta">{len(case['claim_blocks'])} cited block{'s' if len(case['claim_blocks']) != 1 else ''} · {len(case['evidence'])} evidence receipt{'s' if len(case['evidence']) != 1 else ''}<br>Updated <time datetime="{_h(case['updated_at'])}">{_h(_human_time(case['updated_at']))}</time></p>
</article>"""


def render_machine_analysis_index(analyses: Mapping[str, Any]) -> str:
    published = [case for case in analyses["cases"] if _machine_is_article(case)]
    abstained = [case for case in analyses["cases"] if not _machine_is_article(case)]
    reports = "".join(_machine_case_card(case) for case in published) or (
        '<div class="nw-empty-register"><strong>No analysis report passed its '
        "gate in this edition.</strong></div>"
    )
    abstentions = "".join(_machine_case_card(case) for case in abstained) or (
        '<div class="nw-empty-register"><strong>No abstention report in this '
        "edition.</strong></div>"
    )
    body = f"""<body class="ps newsroom-page newsroom-page--analysis">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <header class="nw-investigations-head nw-analysis-head">
    <p class="nw-section__label">Deterministic analysis register</p>
    <h1>Machine analysis, with every boundary attached</h1>
    <p class="nw-investigations-head__dek">Evidence-linked analysis generated by a deterministic program from checked-in aggregate records. This desk conducts no human interviews and cannot fill reporting gaps with inference.</p>
  </header>
  <p class="nw-analysis-disclosure" role="note"><strong>DETERMINISTIC MACHINE ANALYSIS · NO HUMAN INTERVIEW.</strong> An AnalysisReport is a reproducible synthesis, not human-reported journalism. An AbstentionReport is a public record that the evidence gate did not pass; it is never marked as a NewsArticle.</p>
  <div class="nw-meta-line"><span>Public aggregate evidence · no person-level records</span><span>Updated <time datetime="{_h(analyses['generated_at'])}">{_h(_human_time(analyses['generated_at']))}</time></span><a href="/readings/machine-investigations-latest.json">Structured desk</a><a href="/readings/evidence-mesh-latest.json">Evidence mesh</a></div>
  <div class="nw-status-strip" role="status" aria-label="Machine analysis publication states">
    <span><i class="nw-dot nw-dot--live" aria-hidden="true"></i><strong>{len(published)}</strong> analysis reports</span>
    <span><i class="nw-dot nw-dot--warning" aria-hidden="true"></i><strong>{len(abstained)}</strong> abstention reports</span>
    <span><strong>{analyses['n_cases']}</strong> deterministic case files</span>
  </div>
  <nav aria-label="Analysis registers"><ul class="nw-section-nav"><li><a href="#analysis-reports">Analysis reports</a></li><li><a href="#abstention-reports">Abstentions</a></li><li><a href="/news/investigations/">Human-reviewed investigations</a></li></ul></nav>
  <section class="nw-investigation-register" aria-labelledby="analysis-reports"><header><div><p class="nw-section__label">Gate passed</p><h2 id="analysis-reports">Analysis reports</h2></div><p>Published only when the configured evidence, citation, lineage, safety and evaluation checks pass.</p></header><div class="nw-investigation-grid">{reports}</div></section>
  <section class="nw-investigation-register" aria-labelledby="abstention-reports"><header><div><p class="nw-section__label">Gate did not pass</p><h2 id="abstention-reports">Abstention reports</h2></div><p>The missing requirement and a route to falsification remain visible instead of becoming model-written certainty.</p></header><div class="nw-investigation-grid">{abstentions}</div></section>
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/">← Palimpsest Wire</a> · <a href="/news/investigations/">Human-reviewed investigations</a> · <a href="/readings/machine-investigations-latest.json">Structured machine desk</a> · <a href="/docs/MACHINE-INVESTIGATIONS.md">Method</a></div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    return _head(
        title="Palimpsest Machine Analysis · deterministic evidence reports",
        description=(
            "Deterministic machine analyses and abstentions with sentence-level "
            "citations, countercases, limits, falsifiers and evaluation receipts."
        ),
        canonical=f"{SITE}/news/analysis/",
        page_type="website",
        modified_at=analyses["generated_at"],
        json_ld=_machine_index_json_ld(analyses),
    ) + "\n" + body


def _machine_record_title(record: Mapping[str, Any], fallback: str) -> str:
    for key in (
        "title", "statement", "hypothesis", "label", "name", "text",
        "step", "description", "finding", "observation", "test", "condition",
        "countercase", "limitation",
    ):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def _machine_display_value(value: object) -> str:
    if value is None:
        return "not recorded"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _machine_human_time(value: object) -> str:
    try:
        return _human_time(str(value))
    except (ValueError, newsroom.NewsroomError):
        return str(value)


def _machine_citation_links(ids: Sequence[object]) -> str:
    links = []
    for citation_id in ids:
        if isinstance(citation_id, Mapping):
            label = _machine_evidence_id(citation_id)
        else:
            label = str(citation_id)
        links.append(
            f'<a href="#{_machine_fragment("evidence", label)}">{_h(label)}</a>'
        )
    return ", ".join(links) if links else "No evidence receipt cited"


def _machine_claim_blocks(case: Mapping[str, Any]) -> str:
    blocks = []
    for position, block in enumerate(case["claim_blocks"], 1):
        sentences = block.get("sentences", [])
        rendered_sentences = []
        for sentence_position, sentence in enumerate(sentences, 1):
            if isinstance(sentence, Mapping):
                text = next(
                    (
                        sentence[key]
                        for key in ("text", "sentence", "claim")
                        if isinstance(sentence.get(key), str)
                    ),
                    "",
                )
                citation_ids = sentence.get(
                    "citation_ids",
                    sentence.get("evidence_ids", sentence.get("citations", [])),
                )
            else:
                text = str(sentence)
                citation_ids = []
            rendered_sentences.append(
                f"""<li><p>{_h(text)}</p><p class="nw-analysis-citations"><strong>Sentence {sentence_position} citations:</strong> {_machine_citation_links(citation_ids)}</p></li>"""
            )
        paragraph = block.get("paragraph", "")
        block_ids = block.get(
            "citation_ids", block.get("evidence_ids", block.get("citations", []))
        )
        groups = block.get(
            "independence_group_ids",
            block.get(
                "dependency_group_ids",
                block.get("independence_groups", block.get("source_groups", [])),
            ),
        )
        blocks.append(f"""<article class="nw-analysis-claim-block">
  <p class="nw-finding__label">Claim block {position} · <code>{_h(block.get('block_id', position))}</code></p>
  <h3>{_h(paragraph)}</h3>
  <ol class="nw-analysis-sentences">{''.join(rendered_sentences)}</ol>
  <p class="nw-analysis-block-receipt"><strong>Block evidence union:</strong> {_machine_citation_links(block_ids)}<br><strong>Independent lineage groups:</strong> {_h(', '.join(map(str, groups)) if groups else 'none recorded')}</p>
</article>""")
    return "".join(blocks)


def _machine_attribution_html(metadata: Mapping[str, Any]) -> str:
    rights = metadata["rights"]
    attribution = metadata["attribution"]
    providers = []
    for provider in attribution["providers"]:
        if provider["source_url"]:
            providers.append(
                f'<a href="{_h(provider["source_url"])}">{_h(provider["name"])}</a>'
            )
        else:
            providers.append(_h(provider["name"]))
        if provider["terms_url"]:
            providers[-1] += (
                f' (<a href="{_h(provider["terms_url"])}">provider terms</a>)'
            )
    source_links = [
        f'<a href="{_h(attribution["public_source_url"])}">Local public source receipt</a>'
    ]
    source_links.extend(
        f'<a href="{_h(url)}">Upstream source</a>'
        for url in attribution["upstream_source_urls"]
    )
    requirement = (
        "Attribution required for redistribution."
        if attribution["attribution_required"]
        else "No additional attribution flag in the mesh."
    )
    source_statement = (
        f'<span>Source statement: {_h(attribution["source_statement"])}</span>'
        if attribution["source_statement"]
        else ""
    )
    return (
        '<div class="nw-analysis-attribution">'
        f'<strong>{_h(requirement)}</strong>'
        f'<span>Redistribution: <code>{_h(rights["redistribution"])}</code> · '
        f'Reuse: <code>{_h(rights["reuse"])}</code> · '
        f'Training: <code>{_h(rights["training"])}</code></span>'
        f'<span>Provider(s): {", ".join(providers)}</span>'
        f'<span>Upstream lineage: {_h(", ".join(attribution["upstream_groups"]))}</span>'
        f'<span>License / provider terms: <a href="{_h(attribution["license"]["url"])}">'
        f'{_h(attribution["license"]["name"])}</a></span>'
        f'<span>{" · ".join(source_links)}</span>{source_statement}'
        "</div>"
    )


def _machine_evidence_table(
    case: Mapping[str, Any],
    attributions: Mapping[str, Mapping[str, Any]],
) -> str:
    rows = []
    for evidence in case["evidence"]:
        evidence_id = _machine_evidence_id(evidence)
        label = _machine_record_title(evidence, evidence_id)
        source_class = evidence.get(
            "source_class", evidence.get("evidence_class", "aggregate record")
        )
        group = evidence.get(
            "independence_group",
            evidence.get(
                "independence_group_id",
                evidence.get(
                    "dependency_group_id",
                    evidence.get("lineage_group_id", "not recorded"),
                ),
            ),
        )
        timestamp = next(
            (
                evidence[key]
                for key in ("observed_at", "as_of", "source_timestamp", "published_at")
                if evidence.get(key)
            ),
            None,
        )
        if timestamp is None and isinstance(evidence.get("clocks"), Mapping):
            timestamp = next(
                (
                    evidence["clocks"][key]
                    for key in ("event_time", "knowledge_time", "publication_time")
                    if evidence["clocks"].get(key)
                ),
                None,
            )
        if timestamp is None and isinstance(evidence.get("freshness"), Mapping):
            timestamp = evidence["freshness"].get("observed_at")
        role = evidence.get(
            "role", evidence.get("relation", evidence.get("allowed_role", "supports"))
        )
        value = evidence.get("value", evidence.get("summary", evidence.get("claim", "Receipt metadata only")))
        value_type = evidence.get("value_type", evidence.get("unit"))
        denominator = evidence.get("denominator")
        value_detail = _machine_display_value(value)
        if value_type:
            value_detail += f" {_machine_display_value(value_type)}"
        if denominator is not None:
            if isinstance(denominator, Mapping):
                denominator_text = (
                    f"{_machine_display_value(denominator.get('value'))} "
                    f"{_machine_display_value(denominator.get('label'))}"
                )
            else:
                denominator_text = _machine_display_value(denominator)
            value_detail += f"; denominator: {denominator_text}"
        limit = evidence.get(
            "interpretation_limit",
            evidence.get(
                "limitation",
                evidence.get(
                    "limitations",
                    evidence.get("caveat", "See source receipt and case limitations."),
                ),
            ),
        )
        artifact_url = _investigation_href(evidence.get("artifact_url"))
        capsule_link = (
            f'<a href="{_h(artifact_url)}">Open redacted evidence capsule '
            "(addressed by original input hash)</a>"
            if artifact_url != "#"
            else "No public evidence capsule recorded"
        )
        attribution_html = _machine_attribution_html(attributions[evidence_id])
        artifact = evidence.get("artifact_id", evidence.get("input_id", "artifact not recorded"))
        artifact_sha = evidence.get("artifact_sha256", evidence.get("sha256"))
        receipt = _h(str(artifact))
        if artifact_sha:
            receipt += f" · sha256 <code>{_h(str(artifact_sha))}</code>"
        rows.append(f"""<tr id="{_machine_fragment('evidence', evidence_id)}">
  <td><span class="nw-evidence-relation" data-relation="{_h(role)}">{_h(role)}</span><small>{_h(source_class)}</small></td>
  <td><strong>{_h(label)}</strong><small><code>{_h(evidence_id)}</code> · {_h(group)}</small><small>{receipt}</small></td>
  <td>{_h(value_detail)}</td>
  <td>{_h(_machine_human_time(timestamp) if timestamp else 'not dated')}</td>
  <td>{_h(_machine_display_value(limit))}<small>{capsule_link}</small>{attribution_html}</td>
</tr>""")
    return f"""<div class="nw-table-wrap" role="region" tabindex="0" aria-label="Machine-analysis evidence receipts"><table class="nw-evidence-table">
<caption>Evidence receipts cited sentence by sentence in this report</caption>
<thead><tr><th scope="col">Relation</th><th scope="col">Evidence / lineage</th><th scope="col">Value or summary</th><th scope="col">Observed</th><th scope="col">Interpretation limit</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


def _machine_evidence_timeline(case: Mapping[str, Any]) -> str:
    points = []
    dated_evidence = []
    for source_position, evidence in enumerate(case["evidence"]):
        timestamp = next(
            (
                str(evidence[key])
                for key in ("observed_at", "as_of", "source_timestamp", "published_at")
                if evidence.get(key)
            ),
            None,
        )
        if timestamp is None and isinstance(evidence.get("clocks"), Mapping):
            timestamp = next(
                (
                    str(evidence["clocks"][key])
                    for key in ("event_time", "knowledge_time", "publication_time")
                    if evidence["clocks"].get(key)
                ),
                None,
            )
        if timestamp is None and isinstance(evidence.get("freshness"), Mapping):
            observed = evidence["freshness"].get("observed_at")
            timestamp = str(observed) if observed else None
        dated_evidence.append((timestamp or "9999", source_position, evidence))
    for position, (timestamp_sort, _source_position, evidence) in enumerate(
        sorted(dated_evidence, key=lambda item: (item[0], item[1])), 1
    ):
        evidence_id = _machine_evidence_id(evidence)
        timestamp = None if timestamp_sort == "9999" else timestamp_sort
        when = (
            f'<time datetime="{_h(timestamp)}">{_h(_machine_human_time(timestamp))}</time>'
            if timestamp else f"Evidence order {position}"
        )
        group = evidence.get(
            "independence_group",
            evidence.get(
                "independence_group_id",
                evidence.get(
                    "dependency_group_id",
                    evidence.get("lineage_group_id", "lineage not recorded"),
                ),
            ),
        )
        points.append(f"""<li><span class="nw-analysis-timeline__marker" aria-hidden="true">{position}</span><div><p>{when}</p><strong>{_h(_machine_record_title(evidence, evidence_id))}</strong><span>{_h(evidence.get('role', evidence.get('relation', evidence.get('allowed_role', 'evidence'))))} · {_h(group)}</span><a href="#{_machine_fragment('evidence', evidence_id)}">Inspect receipt</a></div></li>""")
    return f"""<figure class="nw-analysis-timeline" aria-labelledby="analysis-timeline-title">
  <figcaption id="analysis-timeline-title"><strong>Evidence chronology and source lineage</strong><span>Ordering exposes which observations predate the report and which receipts share a lineage. It is descriptive, not a causal sequence.</span></figcaption>
  <ol>{''.join(points)}</ol>
</figure>"""


def _machine_record_cards(value: object, *, empty: str) -> str:
    if isinstance(value, Mapping):
        records = [
            ({"name": key, **item} if isinstance(item, Mapping)
             else {"name": key, "value": item})
            for key, item in value.items()
        ]
    else:
        records = value if isinstance(value, list) else [value]
    cards = []
    for position, item in enumerate(records, 1):
        if isinstance(item, Mapping):
            title = _machine_record_title(item, f"Record {position}")
            details = []
            citation_ids: Sequence[object] = []
            for key, field_value in item.items():
                if key in {
                    "title", "statement", "hypothesis", "label", "name", "text",
                    "step", "description", "finding", "observation", "test",
                    "condition", "countercase", "limitation",
                }:
                    continue
                if key in {"citation_ids", "evidence_ids"} and isinstance(field_value, list):
                    citation_ids = field_value
                    continue
                details.append(
                    f"<dt>{_h(key.replace('_', ' '))}</dt><dd>{_h(_machine_display_value(field_value))}</dd>"
                )
            citations = (
                f'<p class="nw-analysis-citations"><strong>Evidence:</strong> {_machine_citation_links(citation_ids)}</p>'
                if citation_ids else ""
            )
            cards.append(
                f"<li><strong>{_h(title)}</strong><dl>{''.join(details)}</dl>{citations}</li>"
            )
        else:
            cards.append(f"<li><strong>{_h(_machine_display_value(item))}</strong></li>")
    return "".join(cards) if cards else f"<li>{_h(empty)}</li>"


def _machine_correction_history(case: Mapping[str, Any]) -> str:
    correction = case["corrections"]
    history = correction.get("history", [])
    rows = []
    for position, revision in enumerate(history, 1):
        if not isinstance(revision, Mapping):
            rows.append(f"<li>{_h(_machine_display_value(revision))}</li>")
            continue
        revision_id = revision.get("revision_id", f"revision-{position}")
        published_at = revision.get("published_at")
        rows.append(f"""<li>
  <strong>{_h(revision.get('summary', revision.get('change_type', 'Revision')))}</strong>
  <dl><dt>Revision</dt><dd><code>{_h(revision_id)}</code></dd><dt>Change type</dt><dd>{_h(revision.get('change_type', 'not recorded'))}</dd><dt>Published</dt><dd>{_h(_machine_human_time(published_at) if published_at else 'not dated')}</dd></dl>
</li>""")
    if not rows:
        rows.append("<li>No revision history recorded.</li>")
    corrected = correction.get("last_corrected_at")
    corrected_text = _machine_human_time(corrected) if corrected else "No material correction recorded"
    return f"""<div class="nw-analysis-receipt"><dl>
  <dt>Correction status</dt><dd>{_h(correction.get('status', 'not recorded'))}</dd>
  <dt>Last corrected</dt><dd>{_h(corrected_text)}</dd>
  <dt>Policy</dt><dd>{_h(correction.get('policy', 'No correction policy recorded.'))}</dd>
</dl></div><ol class="nw-case-record-list">{''.join(rows)}</ol>"""


def _machine_evaluation_receipt(case: Mapping[str, Any]) -> str:
    receipt = case["evaluation_receipt"]
    checks = receipt.get("checks", receipt.get("gates", []))
    if isinstance(checks, Mapping):
        checks = [
            ({"check_id": check_id, **value} if isinstance(value, Mapping)
             else {"check_id": check_id, "status": value})
            for check_id, value in checks.items()
        ]
    summary_rows = []
    for key, value in receipt.items():
        if key in {"checks", "gates"}:
            continue
        summary_rows.append(
            f"<dt>{_h(key.replace('_', ' '))}</dt><dd>{_h(_machine_display_value(value))}</dd>"
        )
    check_rows = []
    for position, check in enumerate(checks, 1):
        if isinstance(check, Mapping):
            check_id = check.get(
                "check_id", check.get("gate_id", check.get("id", f"check-{position}"))
            )
            label = check.get("label", check.get("name", check_id))
            result = check.get("passed", check.get("status", "not recorded"))
            detail = check.get("detail", check.get("reason", check.get("message", "")))
            observed = check.get("observed")
            required = check.get("required", check.get("minimum"))
            threshold = (
                f"{_machine_display_value(observed)} / {_machine_display_value(required)}"
                if observed is not None or required is not None else "not recorded"
            )
        else:
            check_id, label, result, threshold, detail = (
                f"check-{position}", f"Check {position}", check, "not recorded", ""
            )
        check_rows.append(
            f"<tr><td><strong>{_h(label)}</strong><small><code>{_h(check_id)}</code></small></td><td>{_h(_machine_display_value(result))}</td><td>{_h(threshold)}</td><td>{_h(detail)}</td></tr>"
        )
    checks_table = ""
    if check_rows:
        checks_table = f"""<div class="nw-table-wrap" role="region" tabindex="0" aria-label="Machine-analysis evaluation checks"><table class="nw-evidence-table"><caption>Deterministic evaluation and publication-gate checks</caption><thead><tr><th scope="col">Gate</th><th scope="col">Result</th><th scope="col">Observed / required</th><th scope="col">Receipt detail</th></tr></thead><tbody>{''.join(check_rows)}</tbody></table></div>"""
    return f'<div class="nw-analysis-receipt"><dl>{"".join(summary_rows)}</dl>{checks_table}</div>'


def render_machine_analysis_case(case: Mapping[str, Any]) -> str:
    is_article = _machine_is_article(case)
    state = "published" if is_article else "abstained"
    report_label = "ANALYSIS REPORT" if is_article else "ABSTENTION REPORT"
    published_display = _human_time(case["published_at"]) if case["published_at"] else "Not published"
    attributions = _machine_case_attributions(case)
    body = f"""<body class="ps newsroom-page newsroom-page--analysis-case">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <article class="nw-case-file nw-analysis-file" data-publication-state="{state}" data-report-type="{_h(case['report_type'])}">
    <header class="nw-case-file__header">
      <p class="nw-case-status" data-publication-state="{state}"><span class="nw-dot" aria-hidden="true"></span>Deterministic machine analysis · {report_label}</p>
      <h1 lang="{_h(_text_language(case['title']))}">{_h(case['title'])}</h1>
      <p class="nw-case-file__question">{_h(case['dek'])}</p>
      <p><strong>Current gate result:</strong> {_h(case['status_reason'])}</p>
    </header>
    <p class="nw-analysis-disclosure" role="note"><strong>DETERMINISTIC MACHINE ANALYSIS · NO HUMAN INTERVIEW.</strong> This report uses checked-in aggregate evidence only. It cannot add testimony, observe private conduct or treat missing reporting as confirmation.</p>
    <div class="nw-case-file__meta">
      <div><dl><dt>Report type</dt><dd>{_h(case['report_type'])}<br>{_h(case['profile'])}</dd></dl></div>
      <div><dl><dt>Updated</dt><dd><time datetime="{_h(case['updated_at'])}">{_h(_human_time(case['updated_at']))}</time></dd></dl></div>
      <div><dl><dt>Published</dt><dd>{_h(published_display)}</dd></dl></div>
      <div><dl><dt>Revision receipt</dt><dd><code>{_h(case['revision_id'])}</code><br><a href="revisions/{_h(case['revision_id'])}.json">Immutable revision JSON</a></dd></dl></div>
    </div>
    <section class="nw-case-section" aria-labelledby="analysis-claims-title"><header><p class="nw-section__label">Sentence-level provenance</p><h2 id="analysis-claims-title">Analysis with citations attached</h2><p>The paragraph is the published unit; the ledger beneath it shows the exact evidence IDs attached to each sentence and the de-duplicated lineage union for the block.</p></header><div class="nw-finding-list">{_machine_claim_blocks(case)}</div></section>
    <section class="nw-case-section" aria-labelledby="analysis-timeline-section"><header><p class="nw-section__label">Explanatory view</p><h2 id="analysis-timeline-section">When the evidence arrived—and which sources travel together</h2></header>{_machine_evidence_timeline(case)}</section>
    <section class="nw-case-section" aria-labelledby="analysis-evidence-title"><header><p class="nw-section__label">Evidence ledger</p><h2 id="analysis-evidence-title">Receipts, values, rights and interpretation limits</h2><p>Each public archive is a redacted aggregate capsule bound to the original input hash. Raw readings, IP-valued answers, contact data and person-level records are not copied into the archive.</p></header>{_machine_evidence_table(case, attributions)}</section>
    <section class="nw-case-section" aria-labelledby="analysis-challenges-title"><header><p class="nw-section__label">Adversarial reading</p><h2 id="analysis-challenges-title">Countercases, limitations and falsifiers</h2><p>These records are part of the report, not caveats hidden after the conclusion.</p></header><div class="nw-analysis-record-grid"><div class="nw-case-panel nw-case-panel--counter"><h3>Countercases</h3><ul class="nw-case-record-list">{_machine_record_cards(case['countercases'], empty='No countercase recorded.')}</ul></div><div class="nw-case-panel"><h3>Limitations</h3><ul class="nw-case-record-list">{_machine_record_cards(case['limitations'], empty='No limitation recorded.')}</ul></div><div class="nw-case-panel nw-case-panel--target"><h3>Falsifiers</h3><ul class="nw-case-record-list">{_machine_record_cards(case['falsifiers'], empty='No falsifier recorded.')}</ul></div><div class="nw-case-panel"><h3>Hypotheses tested</h3><ul class="nw-case-record-list">{_machine_record_cards(case['hypotheses'], empty='No hypothesis recorded.')}</ul></div></div></section>
    <section class="nw-case-section" aria-labelledby="analysis-gate-title"><header><p class="nw-section__label">Evaluation receipt</p><h2 id="analysis-gate-title">Why this became {_h(case['report_type'])}</h2><p>The report type is the result of declared deterministic checks. Passing a gate does not imply human reporting occurred.</p></header>{_machine_evaluation_receipt(case)}</section>
    <section class="nw-case-section" aria-labelledby="analysis-method-title"><header><p class="nw-section__label">Reproducibility</p><h2 id="analysis-method-title">Methodology</h2></header><ol class="nw-case-record-list">{_machine_record_cards(case['methodology'], empty='No methodology step recorded.')}</ol></section>
    <section class="nw-case-section" aria-labelledby="analysis-revisions-title"><header><p class="nw-section__label">Correction and revision history</p><h2 id="analysis-revisions-title">Current head and preserved revisions</h2><p>The mutable report JSON points at the current case; this revision stays addressable even after a correction.</p></header>{_machine_correction_history(case)}<div class="nw-case-state-grid"><div><dl><dt>Current report</dt><dd><a href="report.json">report.json</a></dd></dl></div><div><dl><dt>Current revision</dt><dd><a href="revisions/{_h(case['revision_id'])}.json"><code>{_h(case['revision_id'])}</code></a></dd></dl></div><div><dl><dt>Source case</dt><dd><code>{_h(case['source_case_id'])}</code></dd></dl></div><div><dl><dt>Source revision</dt><dd><code>{_h(case['source_revision_id'])}</code></dd></dl></div></div></section>
    <section class="nw-case-section" aria-labelledby="analysis-safety-title"><header><p class="nw-section__label">Safety boundary</p><h2 id="analysis-safety-title">What the machine was not allowed to use</h2></header><ul class="nw-case-record-list">{_machine_record_cards(case['safety'], empty='No safety record.')}</ul></section>
  </article>
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/analysis/">← Machine-analysis desk</a> · <a href="report.json">Current report JSON</a> · <a href="/readings/machine-investigations-latest.json">Structured desk</a> · <a href="/readings/evidence-mesh-latest.json">Evidence mesh</a></div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    return _head(
        title=f"{case['title']} · Palimpsest Machine Analysis",
        description=case["dek"],
        canonical=_machine_case_public_url(case),
        page_type="article" if is_article else "website",
        published_at=case["published_at"] if is_article else None,
        modified_at=case["updated_at"],
        json_ld=_machine_case_json_ld(case, attributions),
    ) + "\n" + body


def render_index(feed: Mapping[str, Any]) -> str:
    stories = feed["stories"]
    sections = {section["id"]: section for section in feed["sections"]}
    lead = _select_lead(stories)
    coverage = feed["coverage"]
    live = coverage["live"]
    reporting = coverage["reporting"]
    warnings = coverage["total"] - live
    navigation = "".join(
        f'<li><a href="#{_h(section["id"])}">{_h(section["title"])}</a></li>'
        for section in feed["sections"]
    )
    section_blocks = []
    for section in feed["sections"]:
        section_stories = [
            story for story in stories
            if story["section"] == section["id"] and story["id"] != lead["id"]
        ]
        if not section_stories:
            continue
        cards = "\n".join(_story_card(story, section["title"]) for story in section_stories)
        section_blocks.append(f"""<section class="nw-section" id="{_h(section['id'])}">
  <div class="nw-section__head">
    <div><p class="nw-section__label">{section['order']:02d} / Desk</p><h2>{_h(section['title'])}</h2></div>
    <p class="nw-section__dek">{_h(section['dek'])}</p>
  </div>
  <div class="nw-grid">{cards}</div>
</section>""")
    gaps = [story for story in stories if story["status"] != "live"]
    gap_items = "\n".join(
        f"""<div class="nw-coverage__item"><strong>{_h(story['headline'])}</strong><p>{_h(story['limitations'][0])}</p></div>"""
        for story in gaps
    ) or '<div class="nw-coverage__item"><strong>All sources are current</strong><p>No coverage qualifier is active in this edition.</p></div>'
    body = f"""<body class="ps newsroom-page">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <header class="nw-masthead">
    <div class="nw-masthead__top">
      <p class="nw-wordmark">Palimpsest <span>Wire</span></p>
      <p class="nw-edition"><strong>Verified edition</strong>{_h(_human_time(feed['generated_at']))}<br>{feed['n_stories']} evidence-linked dispatches</p>
    </div>
    <p class="nw-masthead__dek">Measurements become readable reports without losing their source, denominator, freshness or limits. Automated wording; no causal inference.</p>
  </header>
  <div class="nw-meta-line"><span>China · open-source evidence</span><span>Updated <time datetime="{_h(feed['generated_at'])}">{_h(_human_time(feed['generated_at']))}</time></span><a href="/news/feed.xml">RSS</a><a href="/news/feed.json">JSON Feed</a><a href="/readings/newsroom-latest.json">Full structured edition</a></div>
  <div class="nw-status-strip" role="status" aria-label="Edition coverage">
    <span><i class="nw-dot nw-dot--live" aria-hidden="true"></i><strong>{live}</strong> live</span>
    <span><i class="nw-dot nw-dot--warning" aria-hidden="true"></i><strong>{reporting}</strong> reporting</span>
    <span><i class="nw-dot nw-dot--missing" aria-hidden="true"></i><strong>{warnings}</strong> qualified</span>
    <span><strong>{coverage['total']}</strong> total instruments</span>
  </div>
  <nav aria-label="News desks"><ul class="nw-section-nav">{navigation}</ul></nav>
  {_lead(lead, sections[lead['section']]['title'])}
  {''.join(section_blocks)}
  <aside class="nw-coverage" aria-labelledby="coverage-title">
    <div><p class="nw-kicker nw-kicker--warning">Coverage desk</p><h2 id="coverage-title">What we cannot currently claim</h2></div>
    <div class="nw-coverage__items">{gap_items}</div>
  </aside>
</main>
<footer class="nw-footer"><div class="nw-shell">Palimpsest Wire is generated deterministically from the public <a href="/readings/osint-china-latest.json">OSINT China roll-up</a>. Every story links to its exact evidence bytes. <a href="/docs/NEWSROOM.md">Editorial rules</a> · <a href="https://github.com/beepboop2025/palimpsest">Source code</a>.</div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    title = "Palimpsest Wire · evidence-linked China intelligence"
    return _head(
        title=title,
        description=DESCRIPTION,
        canonical=feed["url"],
        page_type="website",
        modified_at=feed["generated_at"],
        json_ld=_index_json_ld(feed),
    ) + "\n" + body


def render_story(
    story: Mapping[str, Any],
    *,
    section: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    claim_items = "\n".join(
        f'<p><strong>{_h(claim["type"].replace("_", " ").title())}.</strong> {_h(claim["statement"])}</p>'
        for claim in story["claims"]
    )
    limitations = "\n".join(f"<li>{_h(item)}</li>" for item in story["limitations"])
    related = "\n".join(
        f'<a href="/{_h(by_id[signal_id]["url"].removeprefix(SITE).lstrip("/"))}">{_h(by_id[signal_id]["headline"])}</a>'
        for signal_id in story["related_signal_ids"]
        if signal_id in by_id
    ) or "<p>No related dispatch is declared for this instrument.</p>"
    metric = ""
    if story["metric"]["value"] is not None:
        metric = f"""<div class="nw-metric-block" aria-label="Headline metric"><strong>{_h(_metric_value(story))}</strong><span>{_h(_metric_caption(story))}</span></div>"""
    status_class = "" if story["status"] == "live" else " nw-kicker--warning"
    body = f"""<body class="ps newsroom-page">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <article class="nw-article">
    <header class="nw-article__header">
      <p class="nw-article__kicker{status_class}">{_h(section['title'])} · {_h(_status_label(story['status']))}</p>
      <h1>{_h(story['headline'])}</h1>
      <p class="nw-article__dek">{_h(story['dek'])}</p>
      <p class="nw-article__meta"><span>By {PUBLISHER}</span><time datetime="{_h(story['published_at'])}">{_h(_human_time(story['published_at']))}</time><span>Automated evidence brief</span></p>
    </header>
    <div class="nw-article__layout">
      <div class="nw-article__body">
{metric}        <h2>What the record says</h2>
        {claim_items}
        <p>This report is scoped to <strong>{_h(story['signal_id'])}</strong>. It does not merge unlike instruments or infer a cause from co-movement.</p>
        <h2>How it was measured</h2>
        <p>{_h(story['method']['summary'])}</p>
        <h2>What this cannot establish</h2>
        <ul class="nw-limitations">{limitations}</ul>
        <h2>Read the evidence</h2>
        <p>The exact source reading is <a href="{_h(story['evidence']['url'])}">{_h(story['evidence']['input']['filename'])}</a>. The structured version of this dispatch is <a href="story.json">published beside the article</a>.</p>
      </div>
      <aside class="nw-article__rail">
        {_receipt(story)}
        <div class="nw-related"><p class="nw-receipt__label">Related dispatches</p>{related}</div>
      </aside>
    </div>
  </article>
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/">← Latest edition</a> · <a href="/osint-china.html">Evidence desk</a> · <a href="/news/feed.xml">RSS</a> · <a href="/readings/newsroom-latest.json">Structured edition</a></div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    return _head(
        title=f"{story['headline']} · Palimpsest Wire",
        description=story["dek"],
        canonical=story["url"],
        page_type="article",
        published_at=story["published_at"],
        modified_at=story["modified_at"],
        json_ld=_story_json_ld(story, section["title"]),
    ) + "\n" + body


def _event_json_ld(
    event: Mapping[str, Any], analysis: Mapping[str, Any]
) -> dict[str, Any]:
    citations = [ref["url"] for ref in event["evidence_refs"]]
    citations.extend(row["evidence_url"] for row in analysis["collector_context"])
    keywords = [*event["topics"], "open source intelligence"]
    if analysis["scope_status"] == "in-scope":
        keywords.append("China")
    return {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "@id": event["url"],
        "name": event["headline"],
        "description": event["dek"],
        "datePublished": event["published_at"],
        "dateModified": max(event["updated_at"], analysis["generated_at"]),
        "inLanguage": _event_language(event),
        "isAccessibleForFree": True,
        "creator": _organization(),
        "isPartOf": {"@type": "CollectionPage", "url": f"{SITE}/news/"},
        "image": [OG_IMAGE],
        "citation": list(dict.fromkeys(citations)),
        "about": EVENT_DESKS[event["desk"]],
        "keywords": keywords,
    }


_ANALYSIS_DISPOSITION_LABELS = {
    "outside-remit": "Outside declared remit",
    "source-assessment": "Source-structure assessment",
    "collector-context": "Current collector context",
    "collector-abstention": "Collector conclusion withheld",
}


def _event_analysis_html(analysis: Mapping[str, Any]) -> str:
    """Render the validated assessment without strengthening its claims."""

    rationale = "".join(f"<li>{_h(item)}</li>" for item in analysis["rationale"])
    collector_cards: list[str] = []
    for row in analysis["collector_context"]:
        metric = row["metric"]
        metric_block = ""
        if metric["value"] is not None:
            metric_value = _metric_value({"metric": metric})
            metric_caption = _metric_caption(
                {"metric": metric, "status": row["status"]}
            )
            metric_block = (
                '  <p class="nw-assessment-card__metric">'
                f"<strong>{_h(metric_value)}</strong><span>{_h(metric_caption)}</span></p>\n"
            )
        observed = (
            _human_time(row["source_timestamp"])
            if row["source_timestamp"] is not None
            else "No source timestamp"
        )
        digest = row["input_sha256"] or "no current evidence hash"
        collector_cards.append(f"""<article class="nw-assessment-card" data-status="{_h(row['status'])}">
  <p class="nw-assessment-card__state">{_h(_status_label(row['status']))} · {_h(row['signal_id'])}</p>
  <h3><a href="{_h(_site_path(row['story_url']))}">{_h(row['headline'])}</a></h3>
  <p>{_h(row['finding'])}</p>
{metric_block}  <p class="nw-assessment-card__limit">{_h(row['interpretation'])}</p>
  <details><summary>Method and receipt</summary><p>{_h(row['method_summary'])}</p><dl><dt>Observed</dt><dd>{_h(observed)}</dd><dt>Evidence</dt><dd><a href="{_h(row['evidence_url'])}">{_h(row['evidence_url'].rsplit('/', 1)[-1])}</a></dd><dt>SHA-256</dt><dd><code>{_h(digest)}</code></dd></dl></details>
</article>""")
    collector_context = (
        '<div class="nw-assessment-grid">' + "".join(collector_cards) + "</div>"
        if collector_cards
        else (
            '<p class="nw-method-note">No collector finding is used here. '
            "Palimpsest's position is limited to remit, attribution and independent-"
            "source structure.</p>"
        )
    )
    peer_items = "".join(
        f"<li><strong>{_h(row['peer'])}.</strong> {_h(row['sentence'])} "
        f"<small>{_h(row['attribution'])}</small></li>"
        for row in analysis.get("peer_context") or []
    )
    peer_context = (
        f'<h3 class="nw-assessment__subhead">Attributed peer context</h3>'
        f'<ul class="nw-assessment__rationale">{peer_items}</ul>'
        if peer_items
        else ""
    )
    evidence = analysis["evidence_assessment"]
    if collector_cards:
        added_value = (
            "Palimpsest adds current measurement context from the named collector "
            "records below. Those measurements do not verify the publisher's article."
        )
    else:
        added_value = (
            "Palimpsest adds attribution, independent-source grouping, a revision "
            "receipt and follow-up boundaries. It adds no independent factual finding."
        )
    return f"""<section class="nw-dossier__section nw-assessment" data-disposition="{_h(analysis['disposition'])}" aria-labelledby="assessment-title">
  <p class="nw-section__label">Palimpsest addition</p><h2 id="assessment-title">What Palimpsest adds to this source report</h2>
  <div class="nw-assessment__verdict">
    <p class="nw-assessment__status">{_h(_ANALYSIS_DISPOSITION_LABELS[analysis['disposition']])} · as of <time datetime="{_h(analysis['generated_at'])}">{_h(_human_time(analysis['generated_at']))}</time></p>
    <p class="nw-assessment__position"><strong>Added value:</strong> {_h(added_value)}</p>
    <p><strong>Verification status:</strong> {_h(analysis['position'])}</p>
    <p><strong>Source structure:</strong> {_h(evidence['conclusion'])}</p>
  </div>
  <h3 class="nw-assessment__subhead">Why this is the bounded position</h3>
  <ul class="nw-assessment__rationale">{rationale}</ul>
  <h3 class="nw-assessment__subhead">Collector findings used</h3>
  {collector_context}
  {peer_context}
  <p class="nw-assessment__receipt">Analysis <code>{_h(analysis['analysis_id'])}</code> · <a href="analysis.json">structured assessment</a> · <a href="analysis/revisions/{_h(analysis['analysis_id'])}.json">immutable revision</a></p>
</section>"""


def render_event(
    event: Mapping[str, Any],
    *,
    wire: Mapping[str, Any],
    feed: Mapping[str, Any],
    analysis: Mapping[str, Any] | None = None,
) -> str:
    if analysis is None:
        peer = (
            peer_context_model.load_peer_document(PEER_CONTEXT_READING)
            if PEER_CONTEXT_READING.is_file()
            else None
        )
        analysis = event_analysis_model.build_event_analysis(
            event, wire=wire, feed=feed, peer=peer
        )
    event_analysis_model.validate_event_analysis(analysis, event=event)
    items = _wire_items(wire)
    stories = {story["signal_id"]: story for story in feed["stories"]}
    facts = "".join(
        f"""<li><strong>{_h(fact['attribution'])}.</strong> <span lang="{_h(_text_language(fact['statement'], source_id=event['evidence_refs'][0]['source_id']))}">{_h(fact['statement'])}</span> <time datetime="{_h(fact['published_at'])}">{_h(_human_time(fact['published_at']))}</time></li>"""
        for fact in event["reported_facts"]
    )
    evidence_rows = []
    for ref in event["evidence_refs"]:
        item = items[ref["item_id"]]
        title_language = _text_language(ref["title"], source_id=ref["source_id"])
        excerpt_language = _text_language(item["excerpt"], source_id=ref["source_id"])
        evidence_rows.append(f"""<tr>
  <td><span class="nw-role" data-role="{_h(ref['role'])}">{_h(ref['role'])}</span></td>
  <td><a href="{_h(ref['url'])}">{_h(ref['source_name'])}</a><small>{_h(ref['independence_group'])}</small></td>
  <td><span lang="{_h(title_language)}">{_h(ref['title'])}</span><small lang="{_h(excerpt_language)}">{_h(item['excerpt'] or 'No feed excerpt supplied.')}</small></td>
  <td><time datetime="{_h(ref['published_at'])}">{_h(_human_time(ref['published_at']))}</time><small>feed sha {_h(item['feed_sha256'][:12])}</small></td>
</tr>""")
    limitations = "".join(f"<li>{_h(value)}</li>" for value in event["limitations"])
    scan_links = "".join(
        f"""<a href="{_h(_site_path(stories[signal_id]['url']))}"><strong>{_h(stories[signal_id]['headline'])}</strong><span>Current instrument · topical pointer only</span></a>"""
        for signal_id in event["declared_links"]["scan_signal_ids"]
        if signal_id in stories
    )
    economic_links = "".join(
        f"""<a href="/news/economy/"><strong>{_h(signal_id)}</strong><span>Economic surface · topical pointer only</span></a>"""
        for signal_id in event["declared_links"]["economic_signal_ids"]
    )
    declared_links = scan_links + economic_links or (
        "<p>No Palimpsest measurement surface is declared for this event. "
        "That absence is not evidence that no measurable change occurred.</p>"
    )
    mutation = event["mutation"]
    previous = mutation["previous_version_id"] or "none — first retained version"
    publisher_names = list(dict.fromkeys(
        ref["source_name"] for ref in event["evidence_refs"]
    ))
    publisher_label = ", ".join(publisher_names)
    primary_ref = event["evidence_refs"][0]
    language = _event_language(event)
    dek_language = _text_language(
        event["dek"], source_id=event["evidence_refs"][0]["source_id"]
    )
    body = f"""<body class="ps newsroom-page newsroom-page--dossier">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <article class="nw-article nw-dossier">
    <header class="nw-article__header">
      <p class="nw-article__kicker">Source index record · {_h(_event_source_label(event))}</p>
      <h1 lang="{_h(language)}">{_h(event['headline'])}</h1>
      <p class="nw-article__dek" lang="{_h(dek_language)}">{_h(event['dek'])}</p>
      <p class="nw-article__meta"><span>Published by {_h(publisher_label)}</span><time datetime="{_h(event['published_at'])}">{_h(_human_time(event['published_at']))}</time><span>Indexed by Palimpsest · {_h(mutation['kind'])} record version</span></p>
      <div class="nw-source-origin"><a class="nw-actions__primary" href="{_h(primary_ref['url'])}">Read the original at {_h(primary_ref['source_name'])} ↗</a><p>Palimpsest did not write or independently verify this publisher report. The record below adds source structure, measurement context when available, revision history and limits.</p></div>
    </header>
    <div class="nw-dossier__summary">
      <div><p class="nw-receipt__label">Record type</p><strong>{_h(_event_source_label(event))}</strong><p>{_h(_event_source_boundary(event))}</p></div>
      <div><p class="nw-receipt__label">What Palimpsest adds</p><p>Attribution, {len(event['evidence_groups'])} independent source group{'s' if len(event['evidence_groups']) != 1 else ''}, topic links, a revision receipt and explicit unknowns. The group count is not a truth probability.</p></div>
      <div><p class="nw-receipt__label">Revision receipt</p><code>{_h(event['version_id'])}</code><p>Previous: <code>{_h(previous)}</code></p><a href="revisions/{_h(event['version_id'])}.json">Immutable revision JSON</a></div>
    </div>
    <section class="nw-dossier__section" aria-labelledby="reported-title">
      <p class="nw-section__label">Publisher reports · attributed, not adopted</p><h2 id="reported-title">What the registered sources published</h2>
      <ol class="nw-fact-list">{facts}</ol>
    </section>
    {_event_analysis_html(analysis)}
    <section class="nw-dossier__section" aria-labelledby="braid-title">
      <p class="nw-section__label">Evidence braid</p><h2 id="braid-title">Order, provenance and declared surfaces</h2>
      {_event_braid(event, wire)}
      <p class="nw-method-note">The braid reports source ordering. A declared topic surface is not a timed statistical match and cannot establish cause, coordination, censorship, or economic impact.</p>
    </section>
    <section class="nw-dossier__section" aria-labelledby="matrix-title">
      <p class="nw-section__label">Evidence matrix</p><h2 id="matrix-title">Inspect every receipt</h2>
      <p class="nw-table-cue" id="evidence-matrix-cue">Scroll horizontally to inspect every column.</p>
      <div class="nw-table-wrap" role="region" tabindex="0" aria-labelledby="matrix-title" aria-describedby="evidence-matrix-cue"><table class="nw-evidence-table"><caption>Evidence receipts for this dossier</caption><thead><tr><th scope="col">Role</th><th scope="col">Source / group</th><th scope="col">Feed record</th><th scope="col">Published / hash</th></tr></thead><tbody>{''.join(evidence_rows)}</tbody></table></div>
    </section>
    <section class="nw-dossier__section" aria-labelledby="surfaces-title">
      <p class="nw-section__label">Measurement surfaces</p><h2 id="surfaces-title">Where Palimpsest can test the topic</h2>
      <div class="nw-surface-links">{declared_links}</div>
    </section>
    <section class="nw-dossier__section" aria-labelledby="limits-title">
      <p class="nw-section__label">Epistemic boundary</p><h2 id="limits-title">What this dossier cannot establish</h2>
      <ul class="nw-limitations">{limitations}</ul>
    </section>
  </article>
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/#source-index">← Publisher source index</a> · <a href="{_h(primary_ref['url'])}">Original report</a> · <a href="/readings/newswire-latest.json">Structured source index</a> · <a href="story.json">Current record JSON</a> · <a href="analysis.json">Palimpsest addition JSON</a></div></footer>
{site_nav.FOOT}
</body>
</html>
"""
    return _head(
        title=f"{event['headline']} · attributed source record · Palimpsest",
        description=f"Attributed source record from {publisher_label}. {_event_source_boundary(event)}",
        canonical=event["url"],
        page_type="website",
        published_at=event["published_at"],
        modified_at=max(event["updated_at"], analysis["generated_at"]),
        json_ld=_event_json_ld(event, analysis),
    ) + "\n" + body


def render_wire_archive(
    wire: Mapping[str, Any],
    *,
    events: Sequence[Mapping[str, Any]] | None = None,
    page: int = 1,
    n_pages: int = 1,
) -> str:
    page_events = list(events if events is not None else wire["events"])
    cards = "".join(_event_card(event) for event in page_events)
    page_suffix = f" · page {page} of {n_pages}" if n_pages > 1 else ""
    previous_href = (
        "/news/wire/" if page == 2
        else f"/news/wire/page/{page - 1}/" if page > 2
        else ""
    )
    next_href = f"/news/wire/page/{page + 1}/" if page < n_pages else ""
    pagination_links = []
    if previous_href:
        pagination_links.append(f'<a rel="prev" href="{previous_href}">← Newer source records</a>')
    pagination_links.append(f'<span>Page {page} of {n_pages}</span>')
    if next_href:
        pagination_links.append(f'<a rel="next" href="{next_href}">Older source records →</a>')
    pagination = (
        '<nav class="nw-pagination" aria-label="Source index pages">'
        + "".join(pagination_links)
        + "</nav>"
    )
    canonical = (
        f"{SITE}/news/wire/" if page == 1
        else f"{SITE}/news/wire/page/{page}/"
    )
    body = f"""<body class="ps newsroom-page newsroom-page--archive">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <header class="nw-article__header nw-archive-head"><p class="nw-article__kicker">Attributed publisher records{_h(page_suffix)}</p><h1>Publisher source index</h1><p class="nw-article__dek">This is not independent Palimpsest reporting. Every accepted current-window publisher item is assigned to one source record; single-source and multiple-independent-group records remain visibly different.</p></header>
  {pagination}
  <div class="nw-event-grid nw-event-grid--archive">{cards}</div>
  {pagination}
  {_accountability_tape(wire)}
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/#source-index">← Evidence desk</a> · <a href="/news/instruments/feed.xml">Measurements-only RSS</a> · <a href="/readings/newswire-latest.json">Structured source index</a></div></footer>
{site_nav.FOOT}
</body></html>"""
    return _head(
        title=f"Publisher source index{page_suffix} · Palimpsest",
        description="Attributed publisher reports with source grouping, revision receipts and explicit verification boundaries.",
        canonical=canonical,
        page_type="website",
        modified_at=wire["generated_at"],
        json_ld={
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "url": canonical,
            "name": f"Publisher source index{page_suffix}",
            "dateModified": wire["generated_at"],
        },
    ) + "\n" + body


def _china_analysis_citations(citation_ids: Sequence[str]) -> str:
    return ", ".join(
        f'<a href="#evidence-{_h(citation_id)}">{_h(citation_id)}</a>'
        for citation_id in citation_ids
    )


def _china_analysis_json_ld(article: Mapping[str, Any]) -> dict[str, Any]:
    canonical = f"{SITE}{article['url']}"
    return {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": article["title"],
        "description": article["dek"],
        "url": canonical,
        "mainEntityOfPage": canonical,
        "datePublished": article["published_at"],
        "dateModified": article["updated_at"],
        "articleSection": "China censorship analysis",
        "author": {
            "@type": "Organization",
            "name": article["authorship"]["byline"],
            "url": f"{SITE}/news/china/analysis/",
        },
        "publisher": _organization(),
        "isBasedOn": sorted({row["reading_url"] for row in article["evidence"]}),
        "about": [
            "China censorship",
            "internet filtering",
            "information controls",
            "content erasure",
        ],
        "image": [OG_IMAGE],
        "isAccessibleForFree": True,
    }


def _china_analysis_records(
    records: Sequence[Mapping[str, Any]], *, ordered: bool = False
) -> str:
    tag = "ol" if ordered else "ul"
    rows = "".join(
        f'<li><p>{_h(record["text"])}</p><small>Receipts {_china_analysis_citations(record["citation_ids"])}</small></li>'
        for record in records
    )
    return f'<{tag} class="ca-records">{rows}</{tag}>'


def render_china_censorship_analysis(
    article: Mapping[str, Any], *, feed: Mapping[str, Any]
) -> str:
    china_analysis_model.validate(article, feed=feed)
    numbers = "".join(
        f'<li><strong>{_h(item["value"])}</strong><span>{_h(item["label"])}</span><small>{_h(item["note"])}</small></li>'
        for item in article["key_numbers"]
    )
    sections = []
    for index, section in enumerate(article["sections"], 1):
        paragraphs = []
        for paragraph in section["paragraphs"]:
            prose = " ".join(_h(sentence["text"]) for sentence in paragraph["sentences"])
            receipts = sorted(
                {
                    citation_id
                    for sentence in paragraph["sentences"]
                    for citation_id in sentence["citation_ids"]
                }
            )
            paragraphs.append(
                f'<p>{prose}<span class="ca-citations"><b>Receipts</b> {_china_analysis_citations(receipts)}</span></p>'
            )
        sections.append(f"""<section class="ca-section" id="{_h(section['section_id'])}">
  <header><span>{index:02d}</span><h2>{_h(section['heading'])}</h2></header>
  <div>{''.join(paragraphs)}</div>
</section>""")
    evidence_rows = "".join(
        f"""<article class="ca-evidence" id="evidence-{_h(row['evidence_id'])}" data-status="{_h(row['status'])}">
  <p><span>{_h(row['signal_id'])}</span><b>{_h(row['status'])}</b></p>
  <h3><a href="{_h(row['story_url'])}">{_h(row['headline'])}</a></h3>
  <blockquote>{_h(row['claim'])}</blockquote>
  <dl><dt>Source clock</dt><dd>{_h(_human_time(row['source_timestamp'])) if row['source_timestamp'] else 'not available'}</dd><dt>Input SHA-256</dt><dd><code>{_h(row['input_sha256'])}</code></dd></dl>
  <p class="ca-evidence__limit"><strong>Interpretation limit.</strong> {_h(row['interpretation_limit'])}</p>
  <a href="{_h(row['reading_url'])}">Open exact reading</a>
</article>"""
        for row in article["evidence"]
    )
    methods = "".join(
        f'<li><span>{index:02d}</span><div><h3>{_h(item["step"])}</h3><p>{_h(item["detail"])}</p><small>{_china_analysis_citations(item["citation_ids"])}</small></div></li>'
        for index, item in enumerate(article["methodology"], 1)
    )
    gates = "".join(
        f'<li><span aria-hidden="true">✓</span><div><strong>{_h(gate["label"])}</strong><p>{_h(gate["detail"])}</p><code>{_h(gate["gate_id"])}</code></div></li>'
        for gate in article["publication_receipt"]["gates"]
    )
    warning = ""
    if article["publication_receipt"]["availability_warnings"]:
        warning = (
            '<p class="ca-warning" role="status"><strong>Availability warning.</strong> '
            + _h(", ".join(article["publication_receipt"]["availability_warnings"]))
            + " did not publish a current finding in this edition. The article reports those gaps instead of retained values.</p>"
        )
    body = f"""<body class="ps newsroom-page china-analysis-page">
{site_nav.render('/news/china/analysis/')}
<main id="main">
  <header class="ca-hero">
    <div class="ca-shell ca-hero__meta"><span>PALIMPSEST / CHINA CENSORSHIP ANALYSIS</span><time datetime="{_h(article['generated_at'])}">{_h(_human_time(article['generated_at']))}</time></div>
    <div class="ca-shell ca-hero__grid"><div><p class="ca-eyebrow">A current reading across ten declared instruments</p><h1>{_h(article['title'])}</h1></div><div><p>{_h(article['dek'])}</p><a href="/readings/china-censorship-analysis-latest.json">Structured article</a></div></div>
    <div class="ca-shell"><ul class="ca-numbers" aria-label="Current key measurements">{numbers}</ul>{warning}</div>
  </header>
  <div class="ca-shell ca-layout">
    <article class="ca-prose">
      <p class="ca-thesis">{_h(article['thesis'])}</p>
      {''.join(sections)}
    </article>
    <aside class="ca-rail" aria-label="Article publication receipt">
      <p class="ca-eyebrow">Publication receipt</p>
      <strong>{article['publication_receipt']['live_signal_count']}/{article['publication_receipt']['required_signal_count']} instruments current</strong>
      <dl><dt>Finding state</dt><dd>{_h(article['finding_state'])}</dd><dt>Citation coverage</dt><dd>{article['publication_receipt']['citation_coverage']:.0%}</dd><dt>Revision</dt><dd><code>{_h(article['revision_id'])}</code></dd></dl>
      <p>{_h(article['disclosure'])}</p>
      <a href="/news/china/feed.xml">Dispatch RSS</a><a href="/news/china/analysis/feed.xml">Analysis RSS</a>
    </aside>
  </div>
  <section class="ca-challenges">
    <div class="ca-shell"><header><p class="ca-eyebrow">Adversarial reading</p><h2>What else could explain the same observations?</h2></header><div class="ca-challenge-grid"><div><h3>Counterreadings</h3>{_china_analysis_records(article['counterreadings'])}</div><div><h3>Limits that stay attached</h3>{_china_analysis_records(article['limitations'])}</div></div></div>
  </section>
  <section class="ca-evidence-ledger ca-shell" aria-labelledby="ca-evidence-title"><header><p class="ca-eyebrow">Evidence ledger</p><h2 id="ca-evidence-title">Every sentence routes back to a current aggregate receipt.</h2></header><div class="ca-evidence-grid">{evidence_rows}</div></section>
  <section class="ca-method ca-shell" aria-labelledby="ca-method-title"><header><p class="ca-eyebrow">Method</p><h2 id="ca-method-title">A closed path from instrument to article.</h2></header><ol>{methods}</ol></section>
  <section class="ca-gates"><div class="ca-shell"><header><p class="ca-eyebrow">Quality gate</p><h2>Why this edition was allowed to publish.</h2></header><ul>{gates}</ul></div></section>
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/china/">← China dispatch stream</a> · <a href="/news/analysis/">Machine-analysis register</a> · <a href="/readings/newsroom-latest.json">Aggregate newsroom feed</a> · <a href="/docs/NEWSROOM.md">Method</a></div></footer>
{site_nav.FOOT}
</body></html>"""
    return _head(
        title=f"{article['title']} · Palimpsest",
        description=article["dek"],
        canonical=f"{SITE}{article['url']}",
        page_type="article",
        published_at=article["published_at"],
        modified_at=article["updated_at"],
        feed_base="/news/china/analysis",
        extra_styles=("/assets/china-analysis.css",),
        json_ld=_china_analysis_json_ld(article),
    ) + "\n" + body


def build_china_analysis_json_feed(article: Mapping[str, Any]) -> dict[str, Any]:
    text = [article["thesis"]]
    for section in article["sections"]:
        text.append(section["heading"])
        for paragraph in section["paragraphs"]:
            text.append(" ".join(sentence["text"] for sentence in paragraph["sentences"]))
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Palimpsest China Censorship Analysis",
        "home_page_url": f"{SITE}{article['url']}",
        "feed_url": f"{SITE}/news/china/analysis/feed.json",
        "description": "Current cross-instrument analysis of China's information controls with sentence-level evidence receipts.",
        "items": [
            {
                "id": article["revision_id"],
                "url": f"{SITE}{article['url']}",
                "title": "[Palimpsest analysis] " + article["title"],
                "summary": "Palimpsest cross-instrument analysis. " + article["dek"],
                "content_text": "\n\n".join(
                    ["ITEM TYPE: PALIMPSEST CROSS-INSTRUMENT ANALYSIS"] + text
                ),
                "date_published": article["published_at"],
                "date_modified": article["updated_at"],
                "authors": [{"name": article["authorship"]["byline"], "url": f"{SITE}/news/china/analysis/"}],
                "tags": ["China", "censorship", "internet filtering", "information controls"],
                "_palimpsest": {
                    "kind": "china_censorship_analysis",
                    "article_id": article["article_id"],
                    "revision_id": article["revision_id"],
                    "finding_state": article["finding_state"],
                    "citation_coverage": article["publication_receipt"]["citation_coverage"],
                    "verification_status": "palimpsest_bounded_analysis",
                },
            }
        ],
        "language": "en",
    }


def build_china_analysis_rss(article: Mapping[str, Any]) -> bytes:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>Palimpsest China Censorship Analysis</title>
  <link>{SITE}{xml_escape(article['url'])}</link>
  <description>Current cross-instrument China censorship analysis with evidence receipts.</description>
  <language>en</language>
  <lastBuildDate>{_rfc2822(article['updated_at'])}</lastBuildDate>
  <atom:link href="{SITE}/news/china/analysis/feed.xml" rel="self" type="application/rss+xml" />
  <item><title>{xml_escape('[Palimpsest analysis] ' + article['title'])}</title><link>{SITE}{xml_escape(article['url'])}</link><guid isPermaLink="false">{xml_escape(article['revision_id'])}</guid><pubDate>{_rfc2822(article['updated_at'])}</pubDate><description>{xml_escape('Palimpsest cross-instrument analysis. ' + article['dek'])}</description><category>palimpsest-analysis</category><category>China censorship analysis</category></item>
</channel>
</rss>
"""
    return xml.encode("utf-8")


def _china_stream_telegram_panel(stream: Mapping[str, Any]) -> str:
    watch = stream["telegram_watch"]
    if watch.get("schema_version") != telegram_watch_model.SCHEMA_VERSION:
        return f"""<aside class="cs-telegram cs-telegram--quiet" aria-labelledby="telegram-watch-title">
  <div><p class="cs-eyebrow">Telegram watch · review gate closed</p><h2 id="telegram-watch-title">No Telegram signal is being smuggled in as fact.</h2><a href="/docs/EVIDENCE-WIRE.md#telegram-and-scamshield-context">How the ScamShield boundary works</a></div>
  <p>{_h(watch['explanation'])}</p>
</aside>"""

    coverage = watch["coverage"]
    detections = watch["detections"]
    families = detections["reviewed_china_family_counts"]
    family_rows = "".join(
        f"<li><strong>{count:,}</strong><span>{_h(label.replace('_', ' ').title())}</span></li>"
        for label, count in sorted(families.items(), key=lambda row: (-row[1], row[0]))
    )
    family_section = (
        f'<ul class="cs-telegram__families" aria-label="Reviewed China-relevant classifier families">{family_rows}</ul>'
        if family_rows else
        '<p class="cs-telegram__empty">Coverage was reviewed, but no classifier family was approved as China-desk context.</p>'
    )
    return f"""<aside class="cs-telegram" aria-labelledby="telegram-watch-title">
  <div class="cs-telegram__head"><div><p class="cs-eyebrow">Telegram watch · human reviewed · context only</p><h2 id="telegram-watch-title">Configured public-channel pulse</h2></div><span>{_h(_human_time(watch['window']['start']))} → {_h(_human_time(watch['window']['end']))}</span></div>
  <div class="cs-telegram__coverage" aria-label="Telegram sampling coverage"><span><strong>{coverage['messages_observed']:,}</strong> messages observed</span><span><strong>{coverage['sources_observed']:,}</strong> sources observed</span><span><strong>{coverage['messages_flagged']:,}</strong> classifier flags</span><span><strong>{coverage['collection_errors']:,}</strong> collection errors</span></div>
  {family_section}
  <p>{_h(watch['interpretation'])}</p>
  <details><summary>Coverage and privacy boundary</summary><ul>{''.join(f'<li>{_h(value)}</li>' for value in watch['limitations'])}</ul><p>Reviewed by role: {_h(watch['review']['reviewer_role'])}. Source receipt: <code>{_h(watch['review']['source_summary_sha256'][:16])}…</code></p></details>
</aside>"""


def _china_stream_entry(entry: Mapping[str, Any], *, expanded: bool = False) -> str:
    analysis = entry["analysis"]
    dossier = entry["dossier"]
    publisher = entry["publisher"]
    source_status = (
        f"Source report · {dossier['independent_groups']} independent groups"
        if dossier["independent_groups"] > 1
        else "Single-source report · not independently verified"
    )
    excerpt = entry["excerpt"] or "The publisher supplied no feed excerpt. Open the original for the report itself."
    topics = "".join(f"<span>{_h(topic)}</span>" for topic in entry["topics"])
    rationale = "".join(f"<li>{_h(value)}</li>" for value in analysis["rationale"])
    checks = "".join(f"<li>{_h(value)}</li>" for value in analysis["next_checks"])
    unknowns = "".join(f"<li>{_h(value)}</li>" for value in analysis["known_unknowns"])
    collector_rows = "".join(
        f"""<li><a href="{_h(_site_path(row['story_url']))}">{_h(row['headline'])}</a><span>{_h(row['status'])} · {_h(row['relation'])}</span><p>{_h(row['interpretation'])}</p></li>"""
        for row in analysis["collector_context"]
    )
    collector = (
        f"<div class=" + '"cs-analysis__collectors"><h4>Palimpsest collector context</h4><ul>' + collector_rows + "</ul></div>"
        if collector_rows else
        '<p class="cs-analysis__abstention"><strong>Collector abstention:</strong> no current Palimpsest measurement is declared for this event.</p>'
    )
    peer_rows = "".join(
        f"""<li><strong>{_h(row['peer'])}.</strong> {_h(row['sentence'])}</li>"""
        for row in analysis.get("peer_context") or []
    )
    peer = (
        f'<div class="cs-analysis__collectors"><h4>Attributed peer context</h4><ul>'
        f"{peer_rows}</ul></div>"
        if peer_rows
        else ""
    )
    expanded_attr = " open" if expanded else ""
    search_text = " ".join(
        [entry["headline"], excerpt, publisher["name"], entry["desk"], *entry["topics"]]
    ).casefold()
    return f"""<article class="cs-entry" id="dispatch-{_h(entry['entry_id'])}" data-desk="{_h(entry['desk'])}" data-search="{_h(search_text)}">
  <div class="cs-entry__rail"><time datetime="{_h(entry['published_at'])}">{_h(_human_time(entry['published_at']))}</time><span>{_h(publisher['name'])}</span><i aria-hidden="true"></i></div>
  <div class="cs-entry__body">
    <div class="cs-entry__flags"><span data-strength="{_h(dossier['evidence_strength'])}">{_h(source_status)}</span><span>{_h(publisher['role'])}</span><span>{dossier['source_items']} item{'s' if dossier['source_items'] != 1 else ''} / {dossier['independent_groups']} independent group{'s' if dossier['independent_groups'] != 1 else ''}</span></div>
    <h2 lang="{_h(entry['language'])}"><a href="{_h(entry['original_url'])}" rel="external">{_h(entry['headline'])}</a></h2>
    <p class="cs-entry__excerpt" lang="{_h(entry['language'])}">{_h(excerpt)}</p>
    <div class="cs-entry__topics"><span>{_h(EVENT_DESKS[entry['desk']])}</span>{topics}</div>
    <details class="cs-analysis"{expanded_attr}>
      <summary><span>What Palimpsest adds</span><strong>{_h(analysis['disposition'].replace('-', ' '))}</strong><small>Open source structure, measurement context, unknowns and next checks</small></summary>
      <div class="cs-analysis__inside">
        <p class="cs-analysis__position"><strong>Verification status:</strong> {_h(analysis['position'])}</p>
        <div class="cs-analysis__grid">
          <div><h3>Why this is the bounded position</h3><ol>{rationale}</ol></div>
          <div><h3>Next verification moves</h3><ol>{checks}</ol></div>
        </div>
        {collector}
        {peer}
        <details class="cs-analysis__limits"><summary>Known unknowns and method limits</summary><ul>{unknowns}</ul></details>
        <div class="cs-analysis__links"><a href="{_h(_site_path(dossier['url']))}">Open evidence dossier</a><a href="{_h(_site_path(analysis['url']))}">Structured analysis</a><a href="{_h(entry['original_url'])}" rel="external">Read at publisher ↗</a></div>
        <p class="cs-analysis__receipt">Analysis {_h(analysis['analysis_id'])} · item {_h(entry['entry_id'])} · feed receipt {_h(publisher['feed_sha256'][:16])}…</p>
      </div>
    </details>
  </div>
</article>"""


def render_china_article_stream(
    stream: Mapping[str, Any],
    *,
    entries: Sequence[Mapping[str, Any]] | None = None,
    page: int = 1,
    n_pages: int = 1,
) -> str:
    page_entries = list(entries if entries is not None else stream["entries"])
    articles = "".join(
        _china_stream_entry(entry, expanded=(page == 1 and index == 0))
        for index, entry in enumerate(page_entries)
    )
    previous_href = (
        "/news/china/" if page == 2
        else f"/news/china/page/{page - 1}/" if page > 2
        else ""
    )
    next_href = f"/news/china/page/{page + 1}/" if page < n_pages else ""
    pagination = f"""<nav class="cs-pagination" aria-label="China article stream pages">
  <span>{f'<a rel="prev" href="{previous_href}">← Newer dispatches</a>' if previous_href else '<i>Newest dispatches</i>'}</span>
  <strong>Page {page} / {n_pages}</strong>
  <span>{f'<a rel="next" href="{next_href}">Older dispatches →</a>' if next_href else '<i>End of current window</i>'}</span>
</nav>"""
    desk_buttons = "".join(
        f'<button type="button" data-desk-filter="{_h(desk)}">{_h(label)}</button>'
        for desk, label in EVENT_DESKS.items()
    )
    coverage = stream["coverage"]
    canonical = (
        f"{SITE}/news/china/" if page == 1
        else f"{SITE}/news/china/page/{page}/"
    )
    suffix = f" · page {page} of {n_pages}" if n_pages > 1 else ""
    body = f"""<body class="ps newsroom-page china-stream-page">
{site_nav.render('/news/')}
<main id="main">
  <header class="cs-hero">
    <div class="cs-hero__grid"><div><p class="cs-eyebrow">Palimpsest / China publisher index{_h(suffix)}</p><h1>Publisher reports.<br><em>Our additions<br>labeled.</em></h1></div><p class="cs-hero__dek">A chronological index of China/Hong Kong items retained from the monitored publisher registry. Read the publisher for the report; open Palimpsest's panel for source structure, measurement context, unknowns and next verification moves.</p></div>
    <div class="cs-hero__stats" aria-label="Current stream coverage"><span><strong>{coverage['china_entries']}</strong> China entries</span><span><strong>{coverage['successful_sources']}/{coverage['registered_sources']}</strong> feeds answered</span><span><strong>{coverage['excluded_global_feed_items']}</strong> off-remit items excluded</span><span><strong>{_h(_human_time(stream['generated_at']))}</strong> rebuilt</span></div>
  </header>
  <div class="cs-shell">
    <nav class="cs-subnav" aria-label="China source index formats"><a href="/news/china/situation/">Situation synthesis</a><a href="/news/china/analysis/">Palimpsest censorship analysis</a><a href="/news/">Evidence desk</a><a href="/news/wire/">Publisher source records</a><a href="/news/china/whispers/">Whispers · unverified context</a><a href="/news/china/feed.xml">RSS</a><a href="/news/china/feed.json">JSON Feed</a><a href="/readings/china-article-stream-latest.json">Structured index</a></nav>
    {_china_stream_telegram_panel(stream)}
    <section class="cs-controls" aria-label="Filter this page">
      <label><span>Search this page</span><input id="china-stream-search" type="search" placeholder="publisher, topic, headline…" autocomplete="off"></label>
      <div class="cs-controls__desks"><button class="is-active" type="button" data-desk-filter="all">All desks</button>{desk_buttons}</div>
      <p id="china-stream-count" role="status" aria-live="polite">Showing {len(page_entries)} publisher records on this page</p>
    </section>
    {pagination}
    <section class="cs-stream" aria-label="China publisher source records">{articles}<p class="cs-no-results" id="china-stream-empty" hidden>No source records on this page match that filter.</p></section>
    {pagination}
    <aside class="cs-method"><p class="cs-eyebrow">What “every” means here</p><h2>Complete across the declared feeds—not the entire internet.</h2><div><p>{_h(stream['scope'])}</p><p>{_h(stream['method']['analysis'])} {_h(stream['method']['rights'])}</p></div></aside>
  </div>
</main>
<footer class="nw-footer"><div class="nw-shell">Publisher reports remain attributed and are not converted into Palimpsest findings. Telegram aggregates remain unverified context. <a href="/news/china/situation/">Combine reports with Observatory measurements</a> · <a href="/feeds/">Feed directory</a> · <a href="/news/standards/">Standards</a> · <a href="/config/news_sources.json">Source registry</a>.</div></footer>
<script src="/assets/china-stream.js" defer></script>
{site_nav.FOOT}
</body></html>"""
    return _head(
        title=f"China publisher source index{suffix} · Palimpsest",
        description="Attributed China and Hong Kong publisher reports with Palimpsest source structure, measurement context, unknowns and next checks clearly labeled.",
        canonical=canonical,
        page_type="website",
        modified_at=stream["generated_at"],
        feed_base="/news/china",
        extra_styles=("/assets/china-stream.css",),
        json_ld={
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "url": canonical,
            "name": f"Palimpsest China publisher source index{suffix}",
            "dateModified": stream["generated_at"],
            "numberOfItems": len(page_entries),
            "isPartOf": {"@type": "WebSite", "url": f"{SITE}/news/"},
        },
    ) + "\n" + body


def build_china_stream_json_feed(stream: Mapping[str, Any]) -> dict[str, Any]:
    items = []
    for entry in stream["entries"]:
        analysis = entry["analysis"]
        dossier = entry["dossier"]
        source_label = (
            f"Source report with {dossier['independent_groups']} independent groups"
            if dossier["independent_groups"] > 1
            else "Single-source report not independently verified by Palimpsest"
        )
        content = [
            "ITEM TYPE: " + source_label,
            "Published by: " + entry["publisher"]["name"],
            "Palimpsest verification status: " + analysis["position"],
            "Palimpsest adds: " + " ".join(analysis["rationale"]),
            "Next checks: " + " ".join(analysis["next_checks"]),
            "Known unknowns: " + " ".join(analysis["known_unknowns"]),
            "Read the original: " + entry["original_url"],
        ]
        items.append({
            "id": entry["entry_id"],
            "url": dossier["url"],
            "external_url": entry["original_url"],
            "title": "[Source report] " + entry["headline"],
            "summary": source_label + ". " + analysis["position"],
            "content_text": "\n\n".join(content),
            "date_published": entry["published_at"],
            "date_modified": entry["collected_at"],
            "language": entry["language"],
            "authors": [{"name": entry["publisher"]["name"]}],
            "tags": [
                "source-report",
                (
                    "multiple-independent-source-groups"
                    if dossier["independent_groups"] > 1
                    else "not-independently-verified"
                ),
                entry["desk"],
                *entry["topics"],
            ],
            "attachments": [
                {"url": dossier["url"], "mime_type": "text/html", "title": "Palimpsest evidence dossier"},
                {"url": analysis["url"], "mime_type": "application/json", "title": "Palimpsest structured analysis"},
            ],
            "_palimpsest": {
                "kind": "publisher_source_record_with_analysis",
                "item_version_id": entry["version_id"],
                "event_id": dossier["event_id"],
                "event_version_id": dossier["version_id"],
                "analysis_id": analysis["analysis_id"],
                "evidence_strength": dossier["evidence_strength"],
                "independent_groups": dossier["independent_groups"],
                "position": analysis["position"],
                "next_checks": analysis["next_checks"],
                "verification_status": (
                    "multiple_independent_source_groups"
                    if dossier["independent_groups"] > 1
                    else "not_independently_verified"
                ),
            },
        })
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Palimpsest China publisher source index",
        "home_page_url": stream["url"],
        "feed_url": stream["json_feed_url"],
        "description": (
            "Attributed China and Hong Kong publisher reports with Palimpsest "
            "source structure, measurement context, unknowns and next checks. "
            "Publisher reports are not converted into Palimpsest findings."
        ),
        "authors": [{"name": PUBLISHER, "url": f"{SITE}/"}],
        "items": items,
    }


def build_china_stream_rss(stream: Mapping[str, Any]) -> bytes:
    rows = []
    for entry in stream["entries"]:
        analysis = entry["analysis"]
        dossier = entry["dossier"]
        source_label = (
            f"Source report with {dossier['independent_groups']} independent groups"
            if dossier["independent_groups"] > 1
            else "Single-source report not independently verified by Palimpsest"
        )
        description = "\n\n".join([
            "Item type: " + source_label,
            "Published by: " + entry["publisher"]["name"],
            "Palimpsest verification status: " + analysis["position"],
            "Palimpsest adds: " + " ".join(analysis["rationale"]),
            "Next checks: " + " ".join(analysis["next_checks"]),
            "Known unknowns: " + " ".join(analysis["known_unknowns"]),
            "Read original: " + entry["original_url"],
            "Palimpsest source record: " + dossier["url"],
        ])
        rows.append(f"""  <item>
    <title>{xml_escape('[Source report] ' + entry['headline'])}</title>
    <link>{xml_escape(dossier['url'])}</link>
    <guid isPermaLink="false">{xml_escape(entry['entry_id'])}</guid>
    <pubDate>{_rfc2822(entry['published_at'])}</pubDate>
    <description>{xml_escape(description)}</description>
    <category>{xml_escape(entry['desk'])}</category>
    <source url={xml_quoteattr(entry['original_url'])}>{xml_escape(entry['publisher']['name'])}</source>
  </item>""")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>Palimpsest China publisher source index</title>
  <link>{xml_escape(stream['url'])}</link>
  <description>Attributed China and Hong Kong publisher reports with Palimpsest source structure, measurement context, unknowns and next checks. Publisher reports are not converted into Palimpsest findings.</description>
  <language>en</language>
  <lastBuildDate>{_rfc2822(stream['generated_at'])}</lastBuildDate>
  <atom:link href="{SITE}/news/china/feed.xml" rel="self" type="application/rss+xml" />
{chr(10).join(rows)}
</channel>
</rss>
"""
    return xml.encode("utf-8")


def _whisper_label(value: str) -> str:
    return value.replace("_", " ").title()


def _dragon_whisper_entry(
    entry: Mapping[str, Any], *, sequence: int, expanded: bool = False,
) -> str:
    analysis = entry["analysis"]
    signal = entry["signal"]
    review = entry["review"]
    families = "".join(
        f"<span>{_h(_whisper_label(value))}</span>" for value in signal["families"]
    ) or "<span>Pattern family withheld</span>"
    counts = "".join(
        f"<li><strong>{count}</strong><span>{_h(_whisper_label(kind))} observed</span></li>"
        for kind, count in sorted(signal["ioc_counts"].items())
    ) or "<li><strong>0</strong><span>Exact indicators exposed</span></li>"
    checks = "".join(f"<li>{_h(value)}</li>" for value in analysis["next_checks"])
    limitations = "".join(f"<li>{_h(value)}</li>" for value in entry["limitations"])
    scripts = ", ".join(_whisper_label(value) for value in signal["script_hints"])
    search_text = " ".join(
        [
            analysis["headline"], analysis["summary"], analysis["why_it_matters"],
            signal["tier"], *signal["families"], *signal["script_hints"],
        ]
    ).casefold()
    open_attr = " open" if expanded else ""
    return f"""<article class="dw-entry" id="{_h(entry['whisper_id'])}" data-tier="{_h(signal['tier'])}" data-search="{_h(search_text)}">
  <div class="dw-entry__rail" aria-hidden="true"><span>{sequence:03d}</span><i></i></div>
  <div class="dw-entry__body">
    <header class="dw-entry__header">
      <div><p class="dw-stamp">Unverified / context only</p><p class="dw-entry__time">Reviewed <time datetime="{_h(entry['published_at'])}">{_h(_human_time(entry['published_at']))}</time> · observed {_h(_human_time(entry['observed_at']))}</p></div>
      <span class="dw-tier" data-tier="{_h(signal['tier'])}">{_h(_whisper_label(signal['tier']))}</span>
    </header>
    <h2>{_h(analysis['headline'])}</h2>
    <p class="dw-entry__summary">{_h(analysis['summary'])}</p>
    <div class="dw-families" aria-label="Reviewed classifier families">{families}</div>
    <details class="dw-analysis"{open_attr}>
      <summary><span>Open the analytical read</span><small>Significance, uncertainty, checks, and receipt</small></summary>
      <div class="dw-analysis__inside">
        <section aria-labelledby="why-{_h(entry['whisper_id'])}"><p class="dw-label">Why it matters</p><h3 id="why-{_h(entry['whisper_id'])}">Pattern-level significance</h3><p>{_h(analysis['why_it_matters'])}</p></section>
        <section class="dw-uncertainty" aria-labelledby="unknown-{_h(entry['whisper_id'])}"><p class="dw-label">Uncertainty</p><h3 id="unknown-{_h(entry['whisper_id'])}">What this does not establish</h3><p>{_h(analysis['uncertainty'])}</p></section>
        <section aria-labelledby="checks-{_h(entry['whisper_id'])}"><p class="dw-label">Verification queue</p><h3 id="checks-{_h(entry['whisper_id'])}">What to check next</h3><ol>{checks}</ol></section>
        <section aria-labelledby="counts-{_h(entry['whisper_id'])}"><p class="dw-label">Redacted structure</p><h3 id="counts-{_h(entry['whisper_id'])}">Counts, never values</h3><ul class="dw-counts">{counts}</ul><p class="dw-script">Script hints: {_h(scripts or 'not recorded')}.</p></section>
        <details class="dw-limits"><summary>Review note and publication limits</summary><p>{_h(review['note'])}</p><ul>{limitations}</ul></details>
        <p class="dw-receipt">{_h(entry['whisper_id'])} · reviewer role {_h(review['reviewer_role'])} · capsule <code>{_h(review['source_capsule_sha256'][:16])}…</code></p>
      </div>
    </details>
  </div>
</article>"""


def render_dragon_whispers(document: Mapping[str, Any]) -> str:
    dragon_whispers_model.validate_dragon_whispers(document)
    entries = document["entries"]
    family_count = len({
        family for entry in entries for family in entry["signal"]["families"]
    })
    indicator_count = sum(
        count
        for entry in entries
        for count in entry["signal"]["ioc_counts"].values()
    )
    ledger = "".join(
        _dragon_whisper_entry(entry, sequence=index, expanded=index == 1)
        for index, entry in enumerate(entries, 1)
    )
    routes = "".join(
        f"""<a class="dw-companion__route" data-route="{_h(route)}" href="{_h(url)}" target="_blank" rel="noopener noreferrer">
  <span class="dw-companion__route-label">{_h(label)}</span>
  <span><strong>{_h(handle)}</strong><small>{_h(description)}</small></span>
  <b aria-hidden="true">Open ↗</b>
</a>"""
        for route, label, handle, url, description in DRAGON_DEN_TELEGRAM_CHANNELS
    )
    if not ledger:
        ledger = """<section class="dw-empty" aria-labelledby="whispers-empty-title">
  <p class="dw-stamp">Review queue / no public artifact</p>
  <h2 id="whispers-empty-title">No sanitized whisper has cleared review.</h2>
  <p>The raw Telegram companion is active, but nothing from it appears here until a public-channel ScamShield capsule is human-reviewed, made China-relevant, stripped of identifiers and exact indicators, and approved as context only.</p>
</section>"""
    body = f"""<body class="ps newsroom-page dragon-whispers-page">
{site_nav.render('/news/')}
<main id="main">
  <header class="dw-hero">
    <div class="dw-shell dw-hero__grid">
      <div><p class="dw-kicker">Palimpsest / China / reviewed Telegram context</p><h1>Whispers from<br><em>the Dragon Den</em></h1></div>
      <div class="dw-hero__brief"><p class="dw-stamp">Unverified · context only · never corroboration</p><p>Detailed analyst notes derived from ScamShield pattern records after human review and deterministic sanitization. The raw Telegram feed is a separate publication surface.</p></div>
    </div>
  </header>
  <div class="dw-shell">
    <nav class="dw-nav" aria-label="China intelligence tabs"><a href="/news/china/situation/">Situation synthesis</a><a href="/news/china/">Article stream</a><a aria-current="page" href="/news/china/whispers/">Whispers</a><a href="/news/wire/">Evidence dossiers</a><a href="/news/china/whispers/feed.xml">Whispers RSS</a><a href="/news/china/whispers/feed.json">JSON Feed</a><a href="/readings/dragon-whispers-latest.json">Structured artifact</a></nav>
    <aside class="dw-warning" aria-labelledby="dw-warning-title">
      <div><p class="dw-kicker">Read before the ledger</p><h2 id="dw-warning-title">This is not verified news.</h2></div>
      <div><p>Entries are sanitized interpretations of automated signals from configured public Telegram sources. The underlying post may be false, incomplete, manipulated, illegal, or malicious.</p><p>Do not use this page to accuse, identify, contact, pay, or investigate a person. Raw wording, source identity, Telegram coordinates, live links, named parties, and exact indicators are withheld. No entry counts as evidence or corroboration.</p></div>
    </aside>
    <aside class="dw-companion" aria-labelledby="dw-companion-title">
      <div class="dw-companion__intro"><p class="dw-kicker">External transmission / Telegram</p><h2 id="dw-companion-title">Open the live, unreviewed feed.</h2><p>These public channels carry automatic native forwards with Telegram source attribution intact. Posts may be false, harmful, manipulated, or illegal. A warning travels with every forward; publication is not verification or endorsement.</p><p class="dw-companion__bot">Publisher identity: <a href="{_h(DRAGON_DEN_TELEGRAM_BOT[1])}" target="_blank" rel="noopener noreferrer">{_h(DRAGON_DEN_TELEGRAM_BOT[0])}</a></p></div>
      <div class="dw-companion__routes" aria-label="Raw Dragon Den Telegram destinations">{routes}</div>
      <p class="dw-companion__boundary"><strong>Raw stays on Telegram.</strong><span>Only reviewed, identifier-free analysis can cross into this ledger.</span></p>
    </aside>
    <section class="dw-stats" aria-label="Reviewed Whispers coverage"><span><strong>{len(entries)}</strong> reviewed whispers</span><span><strong>{family_count}</strong> pattern families</span><span><strong>{indicator_count}</strong> indicators counted, zero exposed</span><span><strong>{_h(_human_time(document['generated_at']))}</strong> ledger rebuilt</span></section>
    <section class="dw-controls" aria-label="Filter reviewed whispers">
      <label><span>Search the sanitized analysis</span><input id="dragon-whispers-search" type="search" placeholder="pattern, significance, uncertainty…" autocomplete="off"></label>
      <div role="group" aria-label="Filter by classifier tier"><button class="is-active" type="button" data-whisper-tier="all" aria-pressed="true">All tiers</button><button type="button" data-whisper-tier="WATCH" aria-pressed="false">Watch</button><button type="button" data-whisper-tier="LIKELY_SCAM" aria-pressed="false">Likely scam</button><button type="button" data-whisper-tier="CONFIRMED_PATTERN" aria-pressed="false">Confirmed pattern</button></div>
      <p id="dragon-whispers-count" role="status" aria-live="polite">Showing {len(entries)} reviewed whisper{'s' if len(entries) != 1 else ''}</p>
    </section>
    <section class="dw-ledger" aria-label="Reviewed sanitized Telegram context">{ledger}<p class="dw-no-results" id="dragon-whispers-empty-filter" hidden>No reviewed whisper matches this filter.</p></section>
    <aside class="dw-method"><p class="dw-kicker">Two lanes, one hard wall</p><h2>Raw on Telegram.<br>Reviewed here.</h2><div><p>The authenticated monitor native-forwards every delivered post from its explicit public-source registry. The dedicated bot adds a mandatory warning. Palimpsest does not ingest that raw feed.</p><p>This page can be built only from the smaller <code>{_h(dragon_whispers_model.SCHEMA_VERSION)}</code> artifact. Human review is mandatory; raw messages, identifiers, exact IOCs, named allegations, and corroboration claims are prohibited by schema and runtime validation.</p></div></aside>
  </div>
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/china/">← China article stream</a> · <a href="/docs/EVIDENCE-WIRE.md#telegram-and-scamshield-context">Evidence boundary</a> · <a href="/protocol/dragon-whispers-v1.schema.json">Public schema</a>.</div></footer>
<script src="/assets/dragon-whispers.js" defer></script>
{site_nav.FOOT}
</body></html>"""
    return _head(
        title="Whispers from the Dragon Den · Palimpsest China",
        description=document["scope"],
        canonical=f"{SITE}/news/china/whispers/",
        page_type="website",
        modified_at=document["generated_at"],
        feed_base="/news/china/whispers",
        extra_styles=("/assets/dragon-whispers.css",),
        json_ld={
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "url": f"{SITE}/news/china/whispers/",
            "name": "Whispers from the Dragon Den",
            "description": document["scope"],
            "dateModified": document["generated_at"],
            "numberOfItems": len(entries),
            "isPartOf": {"@type": "WebSite", "url": f"{SITE}/news/"},
        },
    ) + "\n" + body


def build_dragon_whispers_json_feed(document: Mapping[str, Any]) -> dict[str, Any]:
    dragon_whispers_model.validate_dragon_whispers(document)
    disclaimer = (
        "Unverified context only. This sanitized analysis is not evidence or "
        "corroboration; raw content and identifiers are withheld."
    )
    items = []
    for entry in document["entries"]:
        analysis = entry["analysis"]
        items.append({
            "id": entry["whisper_id"],
            "url": f"{SITE}/news/china/whispers/#{entry['whisper_id']}",
            "title": "[Unverified context] " + analysis["headline"],
            "summary": "Unverified context only; not evidence or corroboration. " + analysis["summary"],
            "content_text": "\n\n".join([
                disclaimer,
                analysis["summary"],
                "Why it matters: " + analysis["why_it_matters"],
                "Uncertainty: " + analysis["uncertainty"],
                "Next checks: " + " ".join(analysis["next_checks"]),
            ]),
            "date_published": entry["published_at"],
            "date_modified": entry["published_at"],
            "authors": [{"name": PUBLISHER}],
            "tags": ["unverified-context", entry["signal"]["tier"], *entry["signal"]["families"]],
            "_palimpsest": {
                "kind": "reviewed_sanitized_telegram_context",
                "relation": dragon_whispers_model.RELATION,
                "counts_as_corroboration": False,
            },
        })
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Palimpsest reviewed Telegram context",
        "home_page_url": f"{SITE}/news/china/whispers/",
        "feed_url": f"{SITE}/news/china/whispers/feed.json",
        "description": "Sanitized, human-reviewed and unverified Telegram context. Items do not count as evidence or corroboration and expose no raw messages or identifiers.",
        "language": "en",
        "authors": [{"name": PUBLISHER, "url": f"{SITE}/"}],
        "items": items,
    }


def build_dragon_whispers_rss(document: Mapping[str, Any]) -> bytes:
    dragon_whispers_model.validate_dragon_whispers(document)
    rows = []
    for entry in document["entries"]:
        analysis = entry["analysis"]
        description = "\n\n".join([
            "UNVERIFIED CONTEXT ONLY — not evidence or corroboration.",
            analysis["summary"],
            "Why it matters: " + analysis["why_it_matters"],
            "Uncertainty: " + analysis["uncertainty"],
            "Next checks: " + " ".join(analysis["next_checks"]),
        ])
        url = f"{SITE}/news/china/whispers/#{entry['whisper_id']}"
        rows.append(f"""  <item>
    <title>{xml_escape('[Unverified context] ' + analysis['headline'])}</title>
    <link>{xml_escape(url)}</link>
    <guid isPermaLink="false">{xml_escape(entry['whisper_id'])}</guid>
    <pubDate>{_rfc2822(entry['published_at'])}</pubDate>
    <description>{xml_escape(description)}</description>
    <category>unverified-context</category>
    <category>{xml_escape(entry['signal']['tier'])}</category>
  </item>""")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>Palimpsest reviewed Telegram context</title>
  <link>{SITE}/news/china/whispers/</link>
  <description>Sanitized, human-reviewed and unverified Telegram context. Items do not count as evidence or corroboration and expose no raw messages or identifiers.</description>
  <language>en</language>
  <lastBuildDate>{_rfc2822(document['generated_at'])}</lastBuildDate>
  <atom:link href="{SITE}/news/china/whispers/feed.xml" rel="self" type="application/rss+xml" />
{chr(10).join(rows)}
</channel>
</rss>
"""
    return xml.encode("utf-8")


def _format_economic_value(metric: Mapping[str, Any]) -> str:
    value = _number(metric["value"])
    if metric["unit"] == "percent":
        return f"{value}%"
    if metric["unit"] == "ratio":
        return f"{_number(metric['value'] * 100)}%"
    return f"{value} {metric['unit']}"


def render_economic_page(pulse: Mapping[str, Any]) -> str:
    gate_rows = "".join(
        f"""<li data-passed="{_h(str(gate['passed']).lower())}"><span>{_h(gate['label'])}</span><strong>{gate['observed']} / {gate['minimum']}</strong></li>"""
        for gate in pulse["readiness"]["gates"]
    )
    desk_blocks = []
    for desk in pulse["desks"]:
        cards = []
        for metric in desk["metrics"]:
            revision = metric["revision"]
            release = _human_time(metric["released_at"]) if metric["released_at"] else "source gives date/period only"
            cards.append(f"""<article class="nw-metric-card" id="{_h(metric['metric_id'])}" data-freshness="{_h(metric['freshness']['status'])}">
  <p class="nw-card__kicker">{_h(metric['source_class'])} · {_h(metric['freshness']['status'])}</p>
  <h3>{_h(metric['label'])}</h3>
  <p class="nw-metric-card__value">{_h(_format_economic_value(metric))}</p>
  <dl><dt>Period</dt><dd>{_h(metric['period_start'])} → {_h(metric['period_end'])}</dd><dt>Released</dt><dd>{_h(release)}</dd><dt>Collected</dt><dd>{_h(_human_time(metric['collected_at']))}</dd><dt>Source group</dt><dd>{_h(metric['independence_group'])}</dd><dt>Comparability</dt><dd>{_h(metric['comparability']['basis'])}</dd><dt>Revision</dt><dd>{_h(revision['status'])}</dd></dl>
  <p class="nw-metric-card__limit">{_h(metric['limitation'])}</p>
  <a href="{_h(metric['evidence']['url'])}">Open evidence receipt</a>
</article>""")
        if not cards:
            cards.append("<div class=\"nw-empty-desk\"><strong>No current metric</strong><p>Not collected is not zero. The source backlog remains visible in the coverage matrix.</p></div>")
        desk_blocks.append(f"""<section class="nw-section nw-econ-desk" id="desk-{_h(desk['id'])}"><div class="nw-section__head"><div><p class="nw-section__label">Economic evidence desk</p><h2>{_h(desk['title'])}</h2></div><p class="nw-section__dek">{_h(desk['limitations'][0])}</p></div><div class="nw-metric-grid">{''.join(cards)}</div></section>""")
    coverage_rows = "".join(
        f"""<tr><td>{_h(row['domain'])}</td><td>{_h(row['status'])}</td><td>{_h(', '.join(row['observed_groups']) or 'none')}</td><td>{_h(', '.join(row['adapter_ready_groups']) or 'none')}</td></tr>"""
        for row in pulse["coverage"]["matrix"]
    )
    body = f"""<body class="ps newsroom-page newsroom-page--economy">
{site_nav.render('/news/')}
<main id="main" class="nw-shell">
  <header class="nw-article__header nw-economy-head"><p class="nw-article__kicker">China economic evidence · {_h(pulse['economic_state']['status'])} · as known {_h(_human_time(pulse['as_of']))}</p><h1>The economic pulse abstains—and shows you exactly why.</h1><p class="nw-article__dek">{_h(pulse['economic_state']['claim'])}</p></header>
  <section class="nw-econ-gates"><div><p class="nw-kicker nw-kicker--economic">Readiness, not rhetoric</p><h2>Composite gates</h2><p>{_h(pulse['readiness']['abstention_reason'])}</p></div><ul>{gate_rows}</ul></section>
  {''.join(desk_blocks)}
  <section class="nw-dossier__section" aria-labelledby="coverage-matrix-title"><p class="nw-section__label">Coverage matrix</p><h2 id="coverage-matrix-title">Observed, adapter-ready and absent</h2><p class="nw-table-cue" id="coverage-matrix-cue">Scroll horizontally to inspect every column.</p><div class="nw-table-wrap" role="region" tabindex="0" aria-labelledby="coverage-matrix-title" aria-describedby="coverage-matrix-cue"><table class="nw-evidence-table"><caption>Economic evidence collection coverage</caption><thead><tr><th scope="col">Domain</th><th scope="col">Status</th><th scope="col">Observed groups</th><th scope="col">Adapter-ready groups</th></tr></thead><tbody>{coverage_rows}</tbody></table></div></section>
  <aside class="nw-coverage"><div><p class="nw-kicker nw-kicker--warning">Prohibited shortcuts</p><h2>What the pulse does not claim</h2></div><div class="nw-coverage__items">{''.join(f'<div class="nw-coverage__item"><p>{_h(value)}</p></div>' for value in pulse['economic_state']['prohibited_interpretations'])}</div></aside>
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/">← Palimpsest Wire</a> · <a href="/readings/china-economic-pulse-latest.json">Structured economic pulse</a> · <a href="/data.html">Evidence Atlas</a></div></footer>
{site_nav.FOOT}
</body></html>"""
    return _head(
        title="China economic state · Palimpsest Wire",
        description=pulse["scope"],
        canonical=f"{SITE}/news/economy/",
        page_type="website",
        modified_at=pulse["generated_at"],
        json_ld={
            "@context": "https://schema.org",
            "@type": "Dataset",
            "name": "Palimpsest China Economic Pulse",
            "description": pulse["scope"],
            "dateModified": pulse["generated_at"],
            "url": f"{SITE}/readings/china-economic-pulse-latest.json",
            "creator": _organization(),
        },
    ) + "\n" + body


def build_json_feed(
    feed: Mapping[str, Any], wire: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    sections = {section["id"]: section["title"] for section in feed["sections"]}
    mixed = wire is not None
    feed_url = (
        f"{SITE}/news/feed.json" if mixed
        else f"{SITE}/news/instruments/feed.json"
    )
    home_page_url = feed["url"] if mixed else f"{SITE}/news/#instruments"
    title = (
        "Palimpsest source index + measurements" if mixed
        else "Palimpsest instrument measurements"
    )
    description = (
        "Palimpsest measurements followed by clearly labeled publisher source "
        "records. Source reports remain attributed and are not independently "
        "verified unless the item states otherwise."
        if mixed else
        "Only Palimpsest's own current instrument measurements, with a result, "
        "source receipt, freshness state and limitation attached."
    )
    event_items = []
    if wire is not None:
        event_items = [
            {
                "id": event["event_id"],
                "url": event["url"],
                "external_url": event["evidence_refs"][0]["url"],
                "title": (
                    "[Corroborated source report] "
                    if len(event["evidence_groups"]) > 1
                    else "[Source report] "
                ) + event["headline"],
                "summary": _event_source_boundary(event),
                "content_text": "\n\n".join(
                    [
                        "ITEM TYPE: " + _event_source_label(event),
                        "Published by: " + ", ".join(dict.fromkeys(
                            ref["source_name"] for ref in event["evidence_refs"]
                        )),
                        (
                            "Palimpsest adds: source grouping, timestamps, topic "
                            "classification and a revision record."
                        ),
                        "Verification status: " + _event_source_boundary(event),
                        "Read the original: " + event["evidence_refs"][0]["url"],
                    ]
                ),
                "date_published": event["published_at"],
                "date_modified": event["updated_at"],
                "tags": [
                    "source-report",
                    (
                        "multiple-independent-source-groups"
                        if len(event["evidence_groups"]) > 1
                        else "not-independently-verified"
                    ),
                    EVENT_DESKS[event["desk"]],
                    event["evidence_strength"],
                    *event["topics"],
                ],
                "attachments": [
                    {
                        "url": ref["url"],
                        "mime_type": "text/html",
                        "title": f"{ref['source_name']}: {ref['title']}",
                    }
                    for ref in event["evidence_refs"]
                ],
                "_palimpsest": {
                    "kind": "publisher_source_record",
                    "version_id": event["version_id"],
                    "evidence_strength": event["evidence_strength"],
                    "independent_groups": len(event["evidence_groups"]),
                    "verification_status": (
                        "multiple_independent_source_groups"
                        if len(event["evidence_groups"]) > 1
                        else "not_independently_verified"
                    ),
                },
            }
            for event in wire["events"]
        ]
    instrument_items = [
        {
            "id": story["id"] + ":" + story["claim_fingerprint"],
            "url": story["url"],
            "external_url": story["evidence"]["url"],
            "title": "[Palimpsest measurement] " + story["headline"],
            "summary": "Palimpsest measurement. " + story["dek"],
            "content_text": "\n\n".join(
                ["ITEM TYPE: PALIMPSEST MEASUREMENT"]
                + ["Result: " + claim["statement"] for claim in story["claims"]]
                + [
                    "Limit: " + " ".join(story["limitations"]),
                    "Evidence: " + story["evidence"]["url"],
                ]
            ),
            "date_published": story["published_at"],
            "date_modified": story["modified_at"],
            "tags": [
                "palimpsest-measurement",
                sections[story["section"]],
                story["signal_id"],
                story["status"],
            ],
            "attachments": [{
                "url": story["evidence"]["url"],
                "mime_type": "application/json",
                "title": story["evidence"]["input"]["filename"],
                **(
                    {"size_in_bytes": story["evidence"]["input"]["bytes"]}
                    if story["evidence"]["input"]["bytes"] is not None
                    else {}
                ),
            }],
            "_palimpsest": {
                "kind": "instrument_measurement",
                "revision_id": _revision_id(story, "storyv"),
                "verification_status": "palimpsest_measurement",
            },
        }
        for story in feed["stories"]
    ]
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": title,
        "home_page_url": home_page_url,
        "feed_url": feed_url,
        "description": description,
        "language": "en",
        "authors": [{"name": PUBLISHER, "url": f"{SITE}/"}],
        "items": instrument_items + event_items,
    }


def build_rss(
    feed: Mapping[str, Any], wire: Mapping[str, Any] | None = None
) -> bytes:
    items = []
    mixed = wire is not None
    channel_title = (
        "Palimpsest source index + measurements" if mixed
        else "Palimpsest instrument measurements"
    )
    channel_link = feed["url"] if mixed else f"{SITE}/news/#instruments"
    channel_description = (
        "Palimpsest measurements followed by clearly labeled publisher source "
        "records. Source reports remain attributed and are not independently "
        "verified unless stated."
        if mixed else
        "Only Palimpsest's own current instrument measurements, each with its "
        "source receipt, freshness state and limitation."
    )
    self_url = (
        f"{SITE}/news/feed.xml" if mixed
        else f"{SITE}/news/instruments/feed.xml"
    )
    for story in feed["stories"]:
        description = (
            "Item type: Palimpsest measurement. Result: "
            + " ".join(claim["statement"] for claim in story["claims"])
            + " Limit: "
            + " ".join(story["limitations"])
            + " Evidence: "
            + story["evidence"]["url"]
        )
        guid = story["id"] + ":" + story["claim_fingerprint"]
        items.append(f"""  <item>
    <title>{xml_escape('[Palimpsest measurement] ' + story['headline'])}</title>
    <link>{xml_escape(story['url'])}</link>
    <guid isPermaLink="false">{xml_escape(guid)}</guid>
    <pubDate>{_rfc2822(story['published_at'])}</pubDate>
    <description>{xml_escape(description)}</description>
    <category>{xml_escape(story['section'])}</category>
    <source url={xml_quoteattr(story['evidence']['url'])}>{xml_escape(story['signal_id'])}</source>
  </item>""")
    if wire is not None:
        for event in wire["events"]:
            prefix = (
                "[Corroborated source report] "
                if len(event["evidence_groups"]) > 1
                else "[Source report] "
            )
            publishers = ", ".join(dict.fromkeys(
                ref["source_name"] for ref in event["evidence_refs"]
            ))
            description = (
                "Item type: "
                + _event_source_label(event)
                + ". Published by: "
                + publishers
                + ". Palimpsest adds source grouping, timestamps, topic "
                "classification and a revision record. Verification status: "
                + _event_source_boundary(event)
                + " Read original: "
                + event["evidence_refs"][0]["url"]
            )
            items.append(f"""  <item>
    <title>{xml_escape(prefix + event['headline'])}</title>
    <link>{xml_escape(event['url'])}</link>
    <guid isPermaLink="false">{xml_escape(event['event_id'])}</guid>
    <pubDate>{_rfc2822(event['published_at'])}</pubDate>
    <description>{xml_escape(description)}</description>
    <category>source-report</category>
    <category>{xml_escape(event['desk'])}</category>
    <source url={xml_quoteattr(event['evidence_refs'][0]['url'])}>{xml_escape(event['evidence_refs'][0]['source_name'])}</source>
  </item>""")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>{xml_escape(channel_title)}</title>
  <link>{xml_escape(channel_link)}</link>
  <description>{xml_escape(channel_description)}</description>
  <language>en</language>
  <lastBuildDate>{_rfc2822(max(feed['generated_at'], wire['generated_at']) if wire is not None else feed['generated_at'])}</lastBuildDate>
  <atom:link href="{self_url}" rel="self" type="application/rss+xml" />
{chr(10).join(items)}
</channel>
</rss>
"""
    return xml.encode("utf-8")


def build_sitemap(
    feed: Mapping[str, Any],
    wire: Mapping[str, Any] | None = None,
    investigations: Mapping[str, Any] | None = None,
    machine_analyses: Mapping[str, Any] | None = None,
    china_stream: Mapping[str, Any] | None = None,
    dragon_whispers: Mapping[str, Any] | None = None,
    china_analysis: Mapping[str, Any] | None = None,
) -> bytes:
    generated_values = [feed["generated_at"]]
    for document in (
        wire,
        investigations,
        machine_analyses,
        china_stream,
        dragon_whispers,
        china_analysis,
    ):
        if document is not None:
            generated_values.append(document["generated_at"])
    reference_time = max(_parse_time(value) for value in generated_values)

    def news_eligible(published_at: str) -> bool:
        age = reference_time - _parse_time(published_at)
        return timedelta(0) <= age <= timedelta(days=2)

    urls = [
        f"""  <url><loc>{SITE}/news/</loc><lastmod>{xml_escape(feed['generated_at'])}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>""",
        f"""  <url><loc>{SITE}/news/standards/</loc><lastmod>{xml_escape(feed['generated_at'])}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>""",
    ]
    if wire is not None:
        archive_pages = max(1, (len(wire["events"]) + WIRE_PAGE_SIZE - 1) // WIRE_PAGE_SIZE)
        urls.append(
            f"  <url><loc>{SITE}/news/wire/</loc><lastmod>{xml_escape(wire['generated_at'])}</lastmod><changefreq>hourly</changefreq></url>"
        )
        urls.extend(
            f"  <url><loc>{SITE}/news/wire/page/{page}/</loc><lastmod>{xml_escape(wire['generated_at'])}</lastmod><changefreq>hourly</changefreq></url>"
            for page in range(2, archive_pages + 1)
        )
        for event in wire["events"]:
            news_markup = ""
            if news_eligible(event["published_at"]):
                news_markup = f"""<news:news><news:publication><news:name>Palimpsest Wire</news:name><news:language>en</news:language></news:publication><news:publication_date>{xml_escape(event['published_at'])}</news:publication_date><news:title>{xml_escape(event['headline'])}</news:title></news:news>"""
            urls.append(
                f"  <url><loc>{xml_escape(event['url'])}</loc><lastmod>{xml_escape(event['updated_at'])}</lastmod>{news_markup}</url>"
            )
        urls.append(
            f"  <url><loc>{SITE}/news/economy/</loc><lastmod>{xml_escape(wire['generated_at'])}</lastmod><changefreq>daily</changefreq></url>"
        )
    if china_stream is not None:
        stream_pages = max(
            1,
            (len(china_stream["entries"]) + CHINA_STREAM_PAGE_SIZE - 1)
            // CHINA_STREAM_PAGE_SIZE,
        )
        urls.append(
            f"  <url><loc>{SITE}/news/china/</loc><lastmod>{xml_escape(china_stream['generated_at'])}</lastmod><changefreq>hourly</changefreq><priority>0.9</priority></url>"
        )
        urls.extend(
            f"  <url><loc>{SITE}/news/china/page/{page}/</loc><lastmod>{xml_escape(china_stream['generated_at'])}</lastmod><changefreq>hourly</changefreq></url>"
            for page in range(2, stream_pages + 1)
        )
        urls.append(
            f"  <url><loc>{SITE}/news/china/situation/</loc><lastmod>{xml_escape(china_stream['generated_at'])}</lastmod><changefreq>hourly</changefreq><priority>0.95</priority></url>"
        )
        urls.append(
            f"  <url><loc>{SITE}/news/china/erasure/</loc><lastmod>{xml_escape(china_stream['generated_at'])}</lastmod><changefreq>hourly</changefreq><priority>0.9</priority></url>"
        )
    if china_analysis is not None:
        news_markup = f"""<news:news><news:publication><news:name>Palimpsest China Desk</news:name><news:language>en</news:language></news:publication><news:publication_date>{xml_escape(china_analysis['published_at'])}</news:publication_date><news:title>{xml_escape(china_analysis['title'])}</news:title></news:news>"""
        urls.append(
            f"  <url><loc>{SITE}/news/china/analysis/</loc><lastmod>{xml_escape(china_analysis['updated_at'])}</lastmod><changefreq>hourly</changefreq><priority>0.95</priority>{news_markup}</url>"
        )
    if dragon_whispers is not None:
        urls.append(
            f"  <url><loc>{SITE}/news/china/whispers/</loc><lastmod>{xml_escape(dragon_whispers['generated_at'])}</lastmod><changefreq>hourly</changefreq><priority>0.8</priority></url>"
        )
    if investigations is not None:
        urls.append(
            f"  <url><loc>{SITE}/news/investigations/</loc><lastmod>{xml_escape(investigations['generated_at'])}</lastmod><changefreq>daily</changefreq><priority>0.9</priority></url>"
        )
        for case in investigations["cases"]:
            news_markup = ""
            if case["status"] == "published" and news_eligible(case["published_at"]):
                news_markup = f"""<news:news><news:publication><news:name>Palimpsest Investigations</news:name><news:language>{xml_escape(_case_language(case))}</news:language></news:publication><news:publication_date>{xml_escape(case['published_at'])}</news:publication_date><news:title>{xml_escape(case['title'])}</news:title></news:news>"""
            urls.append(
                f"  <url><loc>{xml_escape(_case_public_url(case))}</loc><lastmod>{xml_escape(case['updated_at'])}</lastmod>{news_markup}</url>"
            )
    if machine_analyses is not None:
        urls.append(
            f"  <url><loc>{SITE}/news/analysis/</loc><lastmod>{xml_escape(machine_analyses['generated_at'])}</lastmod><changefreq>hourly</changefreq><priority>0.9</priority></url>"
        )
        for case in machine_analyses["cases"]:
            news_markup = ""
            if _machine_is_article(case) and news_eligible(case["published_at"]):
                news_markup = f"""<news:news><news:publication><news:name>Palimpsest Machine Analysis</news:name><news:language>{xml_escape(_text_language(case['title']))}</news:language></news:publication><news:publication_date>{xml_escape(case['published_at'])}</news:publication_date><news:title>{xml_escape(case['title'])}</news:title></news:news>"""
            urls.append(
                f"  <url><loc>{xml_escape(_machine_case_public_url(case))}</loc><lastmod>{xml_escape(case['updated_at'])}</lastmod>{news_markup}</url>"
            )
    for story in feed["stories"]:
        news_markup = ""
        if story["status"] == "live" and news_eligible(story["published_at"]):
            news_markup = f"""<news:news><news:publication><news:name>Palimpsest Wire</news:name><news:language>en</news:language></news:publication><news:publication_date>{xml_escape(story['published_at'])}</news:publication_date><news:title>{xml_escape(story['headline'])}</news:title></news:news>"""
        urls.append(
            f"  <url><loc>{xml_escape(story['url'])}</loc><lastmod>{xml_escape(story['modified_at'])}</lastmod>{news_markup}</url>"
        )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
{chr(10).join(urls)}
</urlset>
"""
    return xml.encode("utf-8")


def _machine_evidence_archive_path(evidence: Mapping[str, Any]) -> Path:
    """Resolve one case receipt to its exact managed capsule path."""

    digest = evidence.get("artifact_sha256")
    filename = f"sha256-{digest}.json"
    if not isinstance(digest, str) or _MACHINE_EVIDENCE_FILENAME.fullmatch(
        filename
    ) is None:
        raise newsroom.NewsroomError("machine evidence has an invalid archive hash")
    expected_url = f"{SITE}/news/analysis/evidence/{filename}"
    if evidence.get("artifact_url") != expected_url:
        raise newsroom.NewsroomError(
            "machine evidence does not use its content-addressed URL: "
            f"{evidence.get('artifact_id')}"
        )
    return _ANALYSIS_ROOT / "evidence" / filename


def _machine_revision_capsule_binding(
    evidence: Mapping[str, Any],
    capsule: Mapping[str, Any],
    *,
    revision_id: str,
) -> None:
    """Prove an archived capsule contains the exact receipt a revision cites."""

    original = capsule["original_input"]
    expected_original = {
        "artifact_id": evidence["artifact_id"],
        "generated_at": evidence["artifact_generated_at"],
        "sha256": evidence["artifact_sha256"],
        "integrity": evidence["integrity"],
    }
    if original != expected_original:
        raise newsroom.NewsroomError(
            f"machine revision {revision_id} has a mismatched evidence capsule input"
        )

    expected_citation = {
        "evidence_id": evidence["evidence_id"],
        "title": evidence["title"],
        "role": evidence["role"],
        "source_class": evidence["source_class"],
        "source_id": evidence["source_id"],
        "selector": evidence["selector"],
        "source_timestamp": evidence["source_timestamp"],
        "independence_group": evidence["independence_group"],
        "upstream_groups": evidence["upstream_groups"],
        "value": {
            "type": evidence["value_type"],
            "value": evidence["value"],
        },
        "denominator": (
            None
            if evidence["denominator"] is None
            else {"type": "aggregate-count", **evidence["denominator"]}
        ),
        "interpretation_limit": evidence["interpretation_limit"],
        "freshness": evidence["freshness"],
    }
    matches = [
        citation
        for citation in capsule["citations"]
        if citation["evidence_id"] == evidence["evidence_id"]
    ]
    if len(matches) != 1 or any(
        matches[0][field] != value for field, value in expected_citation.items()
    ):
        raise newsroom.NewsroomError(
            f"machine revision {revision_id} is not closed over its evidence capsule"
        )


def _validated_archived_machine_revision(
    raw: bytes, *, slug: str, revision_filename: str
) -> Mapping[str, Any]:
    """Validate an immutable revision without consulting today's input files."""

    record = _generated_machine_case(
        raw,
        slug=slug,
        revision_filename=revision_filename,
    )
    if record is None:
        raise newsroom.NewsroomError(
            f"invalid immutable machine-analysis revision: {revision_filename}"
        )
    try:
        machine_investigations_model._scan_no_pii(record, "archived_revision")
        machine_investigations_model._validate_case(
            record,
            "archived_revision",
            record["updated_at"],
            {
                "slug": record["slug"],
                "url": record["url"],
                "title": record["title"],
                "dek": record["dek"],
                "profile": record["profile"],
            },
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise newsroom.NewsroomError(
            f"invalid immutable machine-analysis revision: {revision_filename}"
        ) from exc
    return record


def _read_immutable_analysis_file(
    relative: Path, *, root: Path
) -> bytes:
    """Read a bounded archive file through no-follow directory descriptors."""

    if not _is_immutable_analysis_path(relative):
        raise newsroom.NewsroomError(
            f"refusing to read a non-immutable analysis path: {relative}"
        )
    flags = _directory_open_flags()
    try:
        directory_fd = os.open(root, flags)
    except OSError as exc:
        raise newsroom.NewsroomError(
            f"cannot safely open publication root: {exc}"
        ) from exc
    try:
        for component in relative.parts[:-1]:
            try:
                child_fd = os.open(component, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise newsroom.NewsroomError(
                    f"cannot safely read immutable analysis file {relative}: {exc}"
                ) from exc
            os.close(directory_fd)
            directory_fd = child_fd
        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(relative.name, file_flags, dir_fd=directory_fd)
        except OSError as exc:
            raise newsroom.NewsroomError(
                f"cannot safely read immutable analysis file {relative}: {exc}"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or not 1 <= metadata.st_size <= machine_investigations_model.MAX_OUTPUT_BYTES
            ):
                raise newsroom.NewsroomError(
                    f"immutable analysis file is not a bounded regular file: {relative}"
                )
            chunks: list[bytes] = []
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) != metadata.st_size:
                raise newsroom.NewsroomError(
                    f"immutable analysis file changed while reading: {relative}"
                )
            return raw
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


def _retain_immutable_analysis_output(
    outputs: dict[Path, bytes], relative: Path, raw: bytes
) -> None:
    previous = outputs.get(relative)
    if previous is not None and previous != raw:
        raise newsroom.NewsroomError(
            f"refusing to overwrite immutable analysis bytes: {relative}"
        )
    outputs[relative] = raw


def _event_revision_bytes(
    event: Mapping[str, Any],
    relative: Path,
    *,
    archive_root: Path,
) -> bytes:
    """Return first-published bytes for an event version, or create them once.

    ``mutation`` describes how the mutable head relates to the immediately
    preceding pull and is intentionally outside the event content identity. An
    unchanged future pull must not rewrite that transient label inside the
    immutable revision archive.
    """

    destination = archive_root / relative
    try:
        metadata = destination.lstat()
    except FileNotFoundError:
        return _pretty_json(event)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise newsroom.NewsroomError(
            f"immutable event revision is not a regular file: {relative}"
        )
    raw = _read_immutable_analysis_file(relative, root=archive_root)
    try:
        archived = newswire_model.strict_json_loads(
            raw, label=f"immutable event revision {relative}"
        )
        newswire_model._validate_public_event(archived, str(relative))
    except (TypeError, ValueError, newswire_model.NewswireError) as exc:
        raise newsroom.NewsroomError(
            f"invalid immutable event revision: {relative}"
        ) from exc
    archived_core = {
        key: value for key, value in archived.items() if key != "mutation"
    }
    current_core = {
        key: value for key, value in event.items() if key != "mutation"
    }
    if archived_core != current_core:
        raise newsroom.NewsroomError(
            f"event version collides with unequal archived content: {relative}"
        )
    return raw


def build_outputs(
    feed: Mapping[str, Any],
    *,
    wire: Mapping[str, Any] | None = None,
    pulse: Mapping[str, Any] | None = None,
    investigations: Mapping[str, Any] | None = None,
    machine_analyses: Mapping[str, Any] | None = None,
    telegram_watch: Mapping[str, Any] | None = None,
    dragon_whispers: Mapping[str, Any] | None = None,
    archive_root: Path = ROOT,
) -> dict[Path, bytes]:
    """Return every public output without touching the filesystem."""

    if wire is not None:
        newswire_model.validate_newswire_document(wire)
    if pulse is not None:
        economic_pulse_model.validate_economic_pulse(pulse)
    if investigations is not None:
        investigations_model.validate_investigations(investigations)
    if machine_analyses is not None:
        machine_investigations_model.validate_machine_investigations(machine_analyses)
    if telegram_watch is not None:
        telegram_watch_model.validate_telegram_watch(telegram_watch)
    if dragon_whispers is not None:
        dragon_whispers_model.validate_dragon_whispers(dragon_whispers)
    sections = {section["id"]: section for section in feed["sections"]}
    stories = {story["signal_id"]: story for story in feed["stories"]}
    china_analysis = china_analysis_model.build(feed)
    event_analyses: dict[str, Mapping[str, Any]] = {}
    china_stream: Mapping[str, Any] | None = None
    whispers_document: Mapping[str, Any] | None = None
    if wire is not None:
        peer = (
            peer_context_model.load_peer_document(PEER_CONTEXT_READING)
            if PEER_CONTEXT_READING.is_file()
            else None
        )
        event_analyses = event_analysis_model.build_event_analyses(
            wire, feed, peer=peer
        )
        china_stream = china_stream_model.build_china_article_stream(
            wire, event_analyses, telegram_watch=telegram_watch
        )
        whispers_document = (
            dragon_whispers
            if dragon_whispers is not None
            else dragon_whispers_model.empty_document(wire["generated_at"])
        )
    outputs: dict[Path, bytes] = {
        Path("readings/newsroom-latest.json"): _pretty_json(feed),
        Path("news/index.html"): (
            render_evidence_index(
                feed, wire, pulse, investigations, machine_analyses
            )
            if wire is not None
            else render_index(feed)
        ).encode("utf-8"),
        Path("news/feed.json"): _pretty_json(build_json_feed(feed, wire)),
        Path("news/feed.xml"): build_rss(feed, wire),
        Path("news/sitemap.xml"): build_sitemap(
            feed, wire, investigations, machine_analyses, china_stream,
            whispers_document, china_analysis,
        ),
        Path("readings/china-censorship-analysis-latest.json"): (
            china_analysis_model.pretty_json_bytes(china_analysis)
        ),
        Path("news/china/analysis/index.html"): (
            render_china_censorship_analysis(china_analysis, feed=feed).encode("utf-8")
        ),
        Path("news/china/analysis/feed.json"): _pretty_json(
            build_china_analysis_json_feed(china_analysis)
        ),
        Path("news/china/analysis/feed.xml"): build_china_analysis_rss(
            china_analysis
        ),
    }
    if wire is not None:
        outputs[Path("news/instruments/feed.json")] = _pretty_json(build_json_feed(feed))
        outputs[Path("news/instruments/feed.xml")] = build_rss(feed)
        if china_stream is None:
            raise newsroom.NewsroomError("China stream was not built from the wire")
        if whispers_document is None:
            raise newsroom.NewsroomError("Dragon Whispers desk was not initialized")
        outputs[Path("readings/china-article-stream-latest.json")] = _pretty_json(
            china_stream
        )
        outputs[Path("news/china/feed.json")] = _pretty_json(
            build_china_stream_json_feed(china_stream)
        )
        outputs[Path("news/china/feed.xml")] = build_china_stream_rss(china_stream)
        outputs[Path("readings/dragon-whispers-latest.json")] = _pretty_json(
            whispers_document
        )
        outputs[Path("news/china/whispers/index.html")] = (
            render_dragon_whispers(whispers_document).encode("utf-8")
        )
        outputs[Path("news/china/whispers/feed.json")] = _pretty_json(
            build_dragon_whispers_json_feed(whispers_document)
        )
        outputs[Path("news/china/whispers/feed.xml")] = (
            build_dragon_whispers_rss(whispers_document)
        )
        stream_pages = [
            china_stream["entries"][offset:offset + CHINA_STREAM_PAGE_SIZE]
            for offset in range(0, len(china_stream["entries"]), CHINA_STREAM_PAGE_SIZE)
        ] or [[]]
        for page_number, page_entries in enumerate(stream_pages, 1):
            stream_path = (
                Path("news/china/index.html") if page_number == 1
                else Path("news/china/page") / str(page_number) / "index.html"
            )
            outputs[stream_path] = render_china_article_stream(
                china_stream,
                entries=page_entries,
                page=page_number,
                n_pages=len(stream_pages),
            ).encode("utf-8")
        event_pages = [
            wire["events"][offset:offset + WIRE_PAGE_SIZE]
            for offset in range(0, len(wire["events"]), WIRE_PAGE_SIZE)
        ] or [[]]
        for page_number, page_events in enumerate(event_pages, 1):
            archive_path = (
                Path("news/wire/index.html") if page_number == 1
                else Path("news/wire/page") / str(page_number) / "index.html"
            )
            outputs[archive_path] = render_wire_archive(
                wire,
                events=page_events,
                page=page_number,
                n_pages=len(event_pages),
            ).encode("utf-8")
        archive: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for event in wire["events"]:
            year, month = event["published_at"][:7].split("-")
            archive.setdefault((year, month), []).append(event)
            base = Path("news/wire") / event["event_id"]
            analysis = event_analyses[event["event_id"]]
            outputs[base / "index.html"] = render_event(
                event, wire=wire, feed=feed, analysis=analysis
            ).encode("utf-8")
            outputs[base / "story.json"] = _pretty_json(event)
            event_revision_path = base / "revisions" / f"{event['version_id']}.json"
            outputs[event_revision_path] = _event_revision_bytes(
                event,
                event_revision_path,
                archive_root=archive_root,
            )
            outputs[base / "analysis.json"] = _pretty_json(analysis)
            outputs[
                base / "analysis" / "revisions" / f"{analysis['analysis_id']}.json"
            ] = _pretty_json(analysis)
        for (year, month), events in sorted(archive.items()):
            outputs[Path("news/archive") / year / month / "index.json"] = _pretty_json({
                "schema_version": "palimpsest-news-archive.v1",
                "year": int(year),
                "month": int(month),
                "generated_at": wire["generated_at"],
                "n_events": len(events),
                "events": events,
            })
    if pulse is not None:
        outputs[Path("news/economy/index.html")] = render_economic_page(pulse).encode("utf-8")
    if investigations is not None:
        outputs[Path("news/investigations/index.html")] = (
            render_investigations_index(investigations).encode("utf-8")
        )
        for case in investigations["cases"]:
            base = Path("news/investigations") / case["slug"]
            outputs[base / "index.html"] = render_investigation_case(case).encode(
                "utf-8"
            )
            outputs[base / "case.json"] = _pretty_json(case)
            outputs[base / "revisions" / f"{case['version_id']}.json"] = (
                _pretty_json(case)
            )
    if machine_analyses is not None:
        outputs[Path("news/analysis/index.html")] = (
            render_machine_analysis_index(machine_analyses).encode("utf-8")
        )
        evidence_context = _load_machine_evidence_context()
        archived_evidence: dict[str, dict[str, Any]] = {}
        for case in machine_analyses["cases"]:
            base = Path("news/analysis") / case["slug"]
            case_raw = _pretty_json(case)
            outputs[base / "index.html"] = render_machine_analysis_case(case).encode(
                "utf-8"
            )
            outputs[base / "report.json"] = case_raw
            for history_index, event in enumerate(case["corrections"]["history"]):
                revision_id = event["revision_id"]
                revision_path = base / "revisions" / f"{revision_id}.json"
                if revision_id == case["revision_id"]:
                    revision_raw = case_raw
                else:
                    revision_raw = _read_immutable_analysis_file(
                        revision_path, root=archive_root
                    )
                revision_record = _validated_archived_machine_revision(
                    revision_raw,
                    slug=case["slug"],
                    revision_filename=revision_path.name,
                )
                expected_history = case["corrections"]["history"][
                    : history_index + 1
                ]
                if (
                    revision_record["case_id"] != case["case_id"]
                    or revision_record["source_case_id"] != case["source_case_id"]
                    or revision_record["profile"] != case["profile"]
                    or revision_record["published_at"] != case["published_at"]
                    or revision_record["corrections"]["history"]
                    != expected_history
                ):
                    raise newsroom.NewsroomError(
                        "immutable machine-analysis revision is not the exact "
                        f"history prefix: {revision_path}"
                    )
                _retain_immutable_analysis_output(
                    outputs, revision_path, revision_raw
                )

                # Historical revision JSON is useful only while every capsule
                # it cites remains an addressable part of the same archive.
                if revision_id != case["revision_id"]:
                    for archived_row in revision_record["evidence"]:
                        capsule_path = _machine_evidence_archive_path(archived_row)
                        capsule_raw = _read_immutable_analysis_file(
                            capsule_path, root=archive_root
                        )
                        capsule = _machine_evidence_capsule_bytes(
                            capsule_raw,
                            expected_digest=archived_row["artifact_sha256"],
                        )
                        if capsule is None:
                            raise newsroom.NewsroomError(
                                "invalid immutable machine evidence capsule: "
                                f"{capsule_path}"
                            )
                        _machine_revision_capsule_binding(
                            archived_row,
                            capsule,
                            revision_id=revision_id,
                        )
                        _retain_immutable_analysis_output(
                            outputs, capsule_path, capsule_raw
                        )
            for evidence in case["evidence"]:
                raw, raw_document = _machine_read_cited_input(evidence)
                digest = hashlib.sha256(raw).hexdigest()
                _machine_evidence_archive_path(evidence)
                prior = archived_evidence.setdefault(
                    digest,
                    {"raw": raw, "raw_document": raw_document, "evidence": []},
                )
                if prior["raw"] != raw or prior["raw_document"] != raw_document:
                    raise newsroom.NewsroomError(
                        f"machine evidence digest collision: {digest}"
                    )
                if evidence not in prior["evidence"]:
                    prior["evidence"].append(evidence)
        for digest, archived in archived_evidence.items():
            capsule_path = (
                Path("news/analysis/evidence") / f"sha256-{digest}.json"
            )
            retained_raw = outputs.get(capsule_path)
            if retained_raw is not None:
                retained_capsule = _machine_evidence_capsule_bytes(
                    retained_raw, expected_digest=digest
                )
                if retained_capsule is None:
                    raise newsroom.NewsroomError(
                        f"invalid retained machine evidence capsule: {capsule_path}"
                    )
                for evidence in archived["evidence"]:
                    _machine_revision_capsule_binding(
                        evidence,
                        retained_capsule,
                        revision_id="current-report",
                    )
                continue
            capsule = _machine_evidence_capsule(
                archived["evidence"],
                raw=archived["raw"],
                raw_document=archived["raw_document"],
                context=evidence_context,
            )
            _retain_immutable_analysis_output(
                outputs, capsule_path, _pretty_json(capsule)
            )
    for story in feed["stories"]:
        base = Path("news") / story["slug"]
        outputs[base / "index.html"] = render_story(
            story,
            section=sections[story["section"]],
            by_id=stories,
        ).encode("utf-8")
        outputs[base / "story.json"] = _pretty_json(story)
        if wire is not None:
            revision = _revision_id(story, "storyv")
            outputs[base / "revisions" / f"{revision}.json"] = _pretty_json(story)
    if wire is not None or investigations is not None or machine_analyses is not None:
        manifest_path = Path("news/generated-manifest.json")
        all_paths = sorted([str(path) for path in outputs] + [str(manifest_path)])
        immutable = [
            path for path in all_paths
            if "/revisions/" in path or path.startswith("news/analysis/evidence/sha256-")
        ]
        generated_times = [feed["generated_at"]]
        if wire is not None:
            generated_times.append(wire["generated_at"])
        if investigations is not None:
            generated_times.append(investigations["generated_at"])
        if machine_analyses is not None:
            generated_times.append(machine_analyses["generated_at"])
        if whispers_document is not None:
            generated_times.append(whispers_document["generated_at"])
        generated_times.append(china_analysis["generated_at"])
        outputs[manifest_path] = _pretty_json({
            "schema_version": "palimpsest-news-manifest.v1",
            "generated_at": max(generated_times),
            "n_paths": len(all_paths),
            "paths": all_paths,
            "immutable_revision_paths": immutable,
            "mutable_paths": [path for path in all_paths if path not in immutable],
        })
    return outputs


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _is_managed_analysis_path(relative: Path) -> bool:
    """Return whether a path belongs to the renderer's reserved analysis shape.

    The manifest is an inventory, not authority to delete arbitrary repository
    files. Restricting its entries to the exact generated layout keeps manual
    assets (notes, illustrations, source exports, and so on) outside cleanup.
    Hash-named files below ``revisions`` and content-addressed evidence files are
    reserved generated artifacts even if an interrupted or older publisher
    omitted them from the manifest.
    """

    parts = relative.parts
    if relative.is_absolute() or ".." in parts:
        return False
    if parts == ("news", "analysis", "index.html"):
        return True
    if (
        len(parts) == 4
        and parts[:3] == ("news", "analysis", "evidence")
        and _MACHINE_EVIDENCE_FILENAME.fullmatch(parts[3]) is not None
    ):
        return True
    if len(parts) == 4 and parts[:2] == ("news", "analysis"):
        return (
            _ANALYSIS_CASE_SLUG.fullmatch(parts[2]) is not None
            and parts[3] in {"index.html", "report.json"}
        )
    return (
        len(parts) == 5
        and parts[:2] == ("news", "analysis")
        and _ANALYSIS_CASE_SLUG.fullmatch(parts[2]) is not None
        and parts[3] == "revisions"
        and _MACHINE_REVISION_FILENAME.fullmatch(parts[4]) is not None
    )


def _is_immutable_analysis_path(relative: Path) -> bool:
    """Return whether publication must never replace existing unequal bytes."""

    parts = relative.parts
    event_revision = (
        len(parts) == 6
        and parts[:2] == ("news", "wire")
        and _WIRE_EVENT_DIRECTORY.fullmatch(parts[2]) is not None
        and parts[3:5] == ("analysis", "revisions")
        and _EVENT_ANALYSIS_REVISION_FILENAME.fullmatch(parts[5]) is not None
    )
    event_dossier_revision = (
        len(parts) == 5
        and parts[:2] == ("news", "wire")
        and _WIRE_EVENT_DIRECTORY.fullmatch(parts[2]) is not None
        and parts[3] == "revisions"
        and _EVENT_REVISION_FILENAME.fullmatch(parts[4]) is not None
    )
    machine_revision = _is_managed_analysis_path(relative) and (
        "/revisions/" in relative.as_posix()
        or parts[:3] == ("news", "analysis", "evidence")
    )
    return event_revision or event_dossier_revision or machine_revision


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _read_scanned_file(directory_fd: int, entry: os.DirEntry[str]) -> bytes | None:
    """Read one bounded regular file without following a replaced symlink."""

    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(entry.name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise newsroom.NewsroomError(f"cannot safely read {entry.name}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None
        if metadata.st_size > machine_investigations_model.MAX_OUTPUT_BYTES:
            return None
        chunks: list[bytes] = []
        remaining = metadata.st_size + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        return raw if len(raw) == metadata.st_size else None
    finally:
        os.close(descriptor)


def _generated_machine_case(
    raw: bytes, *, slug: str, revision_filename: str
) -> Mapping[str, Any] | None:
    """Prove bytes have the renderer's exact machine-case JSON identity."""

    try:
        value = newswire_model.strict_json_loads(raw, label=revision_filename)
        if not isinstance(value, dict):
            return None
        if set(value) != machine_investigations_model._CASE_FIELDS:
            return None
        revision_id = value.get("revision_id")
        if (
            value.get("slug") != slug
            or revision_filename != f"{revision_id}.json"
            or _MACHINE_REVISION_FILENAME.fullmatch(revision_filename) is None
            or _machine_case_public_url(value) != f"{SITE}/news/analysis/{slug}/"
            or _pretty_json(value) != raw
        ):
            return None
        corrections = value.get("corrections")
        history = corrections.get("history") if isinstance(corrections, dict) else None
        if (
            not isinstance(history, list)
            or not history
            or not isinstance(history[-1], dict)
            or history[-1].get("revision_id") != revision_id
        ):
            return None

        # Cleanup-only migration proofs: the first development identity bound
        # clocks but excluded history; the next excluded clocks and history.
        # Current publication binds the complete correction chain.  All three
        # remain content-derived, so old generated files can be safely removed,
        # but build_outputs emits only the current validator-owned identity.
        legacy_seed = copy.deepcopy(value)
        legacy_seed["revision_id"] = None
        legacy_seed["corrections"] = dict(corrections, history=[])
        legacy_revision = "machinev-" + hashlib.sha256(
            machine_investigations_model.canonical_json_bytes(legacy_seed)
        ).hexdigest()[:24]
        history_independent_seed = copy.deepcopy(legacy_seed)
        history_independent_seed["published_at"] = None
        history_independent_seed["updated_at"] = None
        history_independent_seed["evaluation_receipt"]["evaluated_at"] = None
        history_independent_revision = "machinev-" + hashlib.sha256(
            machine_investigations_model.canonical_json_bytes(
                history_independent_seed
            )
        ).hexdigest()[:24]
        current_revision = machine_investigations_model._case_revision_id(value)
        if revision_id not in {
            legacy_revision, history_independent_revision, current_revision
        }:
            return None
    except (KeyError, TypeError, ValueError, newsroom.NewsroomError):
        return None
    return value


def _managed_analysis_inventory(*, root: Path) -> dict[Path, bool]:
    """Map managed paths to whether their bytes prove renderer ownership.

    Every reserved-looking file is visible to ``--check``. Publication deletes
    only entries whose content proves it was generated; an ambiguous collision
    blocks the publication and remains untouched.
    """

    flags = _directory_open_flags()
    try:
        analysis_fd = os.open(root / _ANALYSIS_ROOT, flags)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise newsroom.NewsroomError(
            f"cannot safely inspect {_ANALYSIS_ROOT}: {exc}"
        ) from exc

    discovered: dict[Path, bool] = {}
    try:
        with os.scandir(analysis_fd) as analysis_entries:
            root_entries = {entry.name: entry for entry in analysis_entries}
        root_index = root_entries.get("index.html")
        if root_index is not None:
            # There is no per-case JSON receipt from which the desk index can be
            # reproduced in isolation. Detect it, but fail closed rather than
            # authorizing deletion from HTML markers or a stale manifest.
            discovered[_ANALYSIS_ROOT / "index.html"] = False
        try:
            evidence_fd = os.open("evidence", flags, dir_fd=analysis_fd)
        except FileNotFoundError:
            evidence_fd = None
        except OSError as exc:
            raise newsroom.NewsroomError(
                f"cannot safely inspect {_ANALYSIS_ROOT / 'evidence'}: {exc}"
            ) from exc
        if evidence_fd is not None:
            try:
                with os.scandir(evidence_fd) as evidence_files:
                    for evidence in evidence_files:
                        if _MACHINE_EVIDENCE_FILENAME.fullmatch(evidence.name) is None:
                            continue
                        relative = _ANALYSIS_ROOT / "evidence" / evidence.name
                        raw = _read_scanned_file(evidence_fd, evidence)
                        digest = evidence.name.removeprefix("sha256-").removesuffix(
                            ".json"
                        )
                        # Current archives are closed redacted capsules whose
                        # filename addresses the original input bytes.  Accept
                        # legacy exact-input archives only as proven generated
                        # files so this migration can safely remove stale raw
                        # copies; build_outputs never emits that legacy form.
                        discovered[relative] = raw is not None and (
                            _machine_evidence_capsule_bytes(
                                raw, expected_digest=digest
                            )
                            is not None
                            or hashlib.sha256(raw).hexdigest() == digest
                        )
            finally:
                os.close(evidence_fd)
        with os.scandir(analysis_fd) as cases:
            for case in cases:
                if case.name == "evidence" or not case.is_dir(follow_symlinks=False):
                    continue
                try:
                    case_fd = os.open(case.name, flags, dir_fd=analysis_fd)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise newsroom.NewsroomError(
                        f"cannot safely inspect {_ANALYSIS_ROOT / case.name}: {exc}"
                    ) from exc
                try:
                    with os.scandir(case_fd) as case_files:
                        case_entries = {entry.name: entry for entry in case_files}
                    base = _ANALYSIS_ROOT / case.name
                    index_entry = case_entries.get("index.html")
                    report_entry = case_entries.get("report.json")
                    index_raw = (
                        _read_scanned_file(case_fd, index_entry)
                        if index_entry is not None else None
                    )
                    report_raw = (
                        _read_scanned_file(case_fd, report_entry)
                        if report_entry is not None else None
                    )
                    if index_entry is not None:
                        discovered[base / "index.html"] = False
                    if report_entry is not None:
                        discovered[base / "report.json"] = False

                    revision_records: dict[bytes, Mapping[str, Any]] = {}
                    try:
                        revisions_fd = os.open("revisions", flags, dir_fd=case_fd)
                    except FileNotFoundError:
                        revisions_fd = None
                    except OSError as exc:
                        raise newsroom.NewsroomError(
                            "cannot safely inspect "
                            f"{_ANALYSIS_ROOT / case.name / 'revisions'}: {exc}"
                        ) from exc
                    if revisions_fd is not None:
                        try:
                            with os.scandir(revisions_fd) as revisions:
                                for revision in revisions:
                                    if _MACHINE_REVISION_FILENAME.fullmatch(
                                        revision.name
                                    ) is None:
                                        continue
                                    relative = base / "revisions" / revision.name
                                    raw = _read_scanned_file(revisions_fd, revision)
                                    record = (
                                        _generated_machine_case(
                                            raw,
                                            slug=case.name,
                                            revision_filename=revision.name,
                                        )
                                        if raw is not None else None
                                    )
                                    discovered[relative] = record is not None
                                    if record is not None and raw is not None:
                                        revision_records[raw] = record
                        finally:
                            os.close(revisions_fd)

                    record = revision_records.get(report_raw) if report_raw else None
                    if record is not None and index_raw is not None:
                        try:
                            expected_index = render_machine_analysis_case(record).encode(
                                "utf-8"
                            )
                        except (KeyError, TypeError, ValueError, newsroom.NewsroomError):
                            expected_index = None
                        if expected_index == index_raw:
                            discovered[base / "index.html"] = True
                            discovered[base / "report.json"] = True
                finally:
                    os.close(case_fd)
    finally:
        os.close(analysis_fd)
    return discovered


def _extra_managed_analysis_paths(
    outputs: Mapping[Path, bytes], *, root: Path
) -> dict[Path, bool]:
    expected = {Path(relative) for relative in outputs}
    if _GENERATED_MANIFEST_PATH not in expected:
        return {}
    return {
        relative: proven
        for relative, proven in _managed_analysis_inventory(root=root).items()
        if relative not in expected
    }


def _is_managed_pagination_path(relative: Path) -> bool:
    """Return whether ``relative`` is one reserved numbered archive page."""

    if relative.is_absolute() or ".." in relative.parts or relative.name != "index.html":
        return False
    return any(
        relative.parent.parent == archive_root
        and _PAGINATION_PAGE_NUMBER.fullmatch(relative.parent.name) is not None
        for archive_root in _PAGINATION_LAYOUTS
    )


def _generated_pagination_page(raw: bytes, *, relative: Path) -> bool:
    """Prove a numbered page has this renderer's path-bound HTML identity."""

    if not _is_managed_pagination_path(relative) or not raw.startswith(b"<!doctype html>\n"):
        return False
    archive_root = relative.parent.parent
    body_marker = _PAGINATION_LAYOUTS.get(archive_root)
    if body_marker is None:
        return False
    canonical = f'{SITE}/{relative.parent.as_posix()}/'.encode("ascii")
    return all(
        marker in raw
        for marker in (
            b'<meta name="author" content="Palimpsest Observatory">',
            b'<link rel="canonical" href="' + canonical + b'">',
            b'<meta property="og:url" content="' + canonical + b'">',
            body_marker,
            b"</body></html>",
        )
    )


def _managed_pagination_inventory(*, root: Path) -> dict[Path, bool]:
    """Inventory exact numeric archive pages without following directory symlinks."""

    flags = _directory_open_flags()
    discovered: dict[Path, bool] = {}
    for archive_root in _PAGINATION_LAYOUTS:
        try:
            archive_fd = os.open(root / archive_root, flags)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise newsroom.NewsroomError(
                f"cannot safely inspect {archive_root}: {exc}"
            ) from exc
        try:
            with os.scandir(archive_fd) as pages:
                page_entries = list(pages)
            for page in page_entries:
                if (
                    _PAGINATION_PAGE_NUMBER.fullmatch(page.name) is None
                    or not page.is_dir(follow_symlinks=False)
                ):
                    continue
                try:
                    page_fd = os.open(page.name, flags, dir_fd=archive_fd)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise newsroom.NewsroomError(
                        f"cannot safely inspect {archive_root / page.name}: {exc}"
                    ) from exc
                try:
                    with os.scandir(page_fd) as files:
                        index = next((entry for entry in files if entry.name == "index.html"), None)
                    if index is None:
                        continue
                    relative = archive_root / page.name / "index.html"
                    raw = _read_scanned_file(page_fd, index)
                    discovered[relative] = raw is not None and _generated_pagination_page(
                        raw, relative=relative
                    )
                finally:
                    os.close(page_fd)
        finally:
            os.close(archive_fd)
    return discovered


def _extra_managed_pagination_paths(
    outputs: Mapping[Path, bytes], *, root: Path
) -> dict[Path, bool]:
    expected = {Path(relative) for relative in outputs}
    if _GENERATED_MANIFEST_PATH not in expected:
        return {}
    return {
        relative: proven
        for relative, proven in _managed_pagination_inventory(root=root).items()
        if relative not in expected
    }


def _safe_unlink_managed_analysis(relative: Path, *, root: Path) -> bool:
    """Unlink one generated file without following any parent symlink."""

    if not _is_managed_analysis_path(relative):
        raise newsroom.NewsroomError(
            f"refusing to remove non-generated analysis path: {relative}"
        )
    flags = _directory_open_flags()
    try:
        directory_fd = os.open(root, flags)
    except OSError as exc:
        raise newsroom.NewsroomError(f"cannot safely open publication root: {exc}") from exc
    try:
        for component in relative.parts[:-1]:
            try:
                child_fd = os.open(component, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise newsroom.NewsroomError(
                    f"refusing unsafe analysis cleanup for {relative}: {exc}"
                ) from exc
            os.close(directory_fd)
            directory_fd = child_fd
        try:
            entry = os.stat(
                relative.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        if not (stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode)):
            raise newsroom.NewsroomError(
                f"refusing to remove non-file analysis path: {relative}"
            )
        os.unlink(relative.name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        return True
    finally:
        os.close(directory_fd)


def _safe_unlink_managed_pagination(relative: Path, *, root: Path) -> bool:
    """Unlink one proven numbered archive page without following parent symlinks."""

    if not _is_managed_pagination_path(relative):
        raise newsroom.NewsroomError(
            f"refusing to remove non-generated pagination path: {relative}"
        )
    flags = _directory_open_flags()
    try:
        directory_fd = os.open(root, flags)
    except OSError as exc:
        raise newsroom.NewsroomError(f"cannot safely open publication root: {exc}") from exc
    try:
        for component in relative.parts[:-1]:
            try:
                child_fd = os.open(component, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise newsroom.NewsroomError(
                    f"refusing unsafe pagination cleanup for {relative}: {exc}"
                ) from exc
            os.close(directory_fd)
            directory_fd = child_fd
        try:
            entry = os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(entry.st_mode):
            raise newsroom.NewsroomError(
                f"refusing to remove non-file pagination path: {relative}"
            )
        os.unlink(relative.name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        return True
    finally:
        os.close(directory_fd)


def publish(outputs: Mapping[Path, bytes], *, root: Path = ROOT) -> tuple[int, int]:
    changed = unchanged = 0
    stale_analysis = _extra_managed_analysis_paths(outputs, root=root)
    stale_pagination = _extra_managed_pagination_paths(outputs, root=root)
    unverified = sorted(
        (
            relative
            for relative, proven in {**stale_analysis, **stale_pagination}.items()
            if not proven
        ),
        key=str,
    )
    if unverified:
        paths = ", ".join(str(relative) for relative in unverified)
        raise newsroom.NewsroomError(
            "refusing to remove unverified files in the managed layout: "
            f"{paths}"
        )
    ordered = sorted(
        ((Path(relative), payload) for relative, payload in outputs.items()),
        key=lambda item: (
            item[0] == _GENERATED_MANIFEST_PATH,
            str(item[0]),
        ),
    )
    manifest_item: tuple[Path, bytes] | None = None
    for relative, payload in ordered:
        if relative == _GENERATED_MANIFEST_PATH:
            manifest_item = (relative, payload)
            continue
        destination = root / relative
        if _is_immutable_analysis_path(relative):
            try:
                destination.lstat()
            except FileNotFoundError:
                current = None
            else:
                current = _read_immutable_analysis_file(relative, root=root)
        else:
            try:
                current = destination.read_bytes()
            except FileNotFoundError:
                current = None
        if current == payload:
            unchanged += 1
            continue
        if current is not None and _is_immutable_analysis_path(relative):
            raise newsroom.NewsroomError(
                f"refusing to overwrite immutable analysis bytes: {relative}"
            )
        _atomic_write(destination, payload)
        changed += 1
    for relative in sorted(
        stale_analysis, key=lambda path: (len(path.parts), str(path)), reverse=True
    ):
        if _safe_unlink_managed_analysis(relative, root=root):
            changed += 1
    for relative in sorted(stale_pagination, key=str):
        if _safe_unlink_managed_pagination(relative, root=root):
            changed += 1
    if manifest_item is not None:
        relative, payload = manifest_item
        destination = root / relative
        try:
            current = destination.read_bytes()
        except FileNotFoundError:
            current = None
        if current == payload:
            unchanged += 1
        else:
            _atomic_write(destination, payload)
            changed += 1
    return changed, unchanged


def check(outputs: Mapping[Path, bytes], *, root: Path = ROOT) -> list[str]:
    drift = []
    for relative, payload in sorted(outputs.items(), key=lambda item: str(item[0])):
        destination = root / relative
        if _is_immutable_analysis_path(relative):
            try:
                destination.lstat()
            except FileNotFoundError:
                drift.append(f"missing {relative}")
                continue
            try:
                current = _read_immutable_analysis_file(relative, root=root)
            except newsroom.NewsroomError:
                drift.append(f"stale {relative}")
                continue
        else:
            try:
                current = destination.read_bytes()
            except FileNotFoundError:
                drift.append(f"missing {relative}")
                continue
        if current != payload:
            drift.append(f"stale {relative}")
    extras = {
        **_extra_managed_analysis_paths(outputs, root=root),
        **_extra_managed_pagination_paths(outputs, root=root),
    }
    for relative in sorted(extras, key=str):
        drift.append(f"extra {relative}")
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report generated-file drift without writing")
    args = parser.parse_args(argv)
    feed = newsroom.build_news_feed()
    wire, pulse, investigations = _load_extension_documents()
    machine_analyses = _load_machine_investigations()
    telegram_watch = _load_telegram_watch()
    dragon_whispers = _load_dragon_whispers()
    outputs = build_outputs(
        feed,
        wire=wire,
        pulse=pulse,
        investigations=investigations,
        machine_analyses=machine_analyses,
        telegram_watch=telegram_watch,
        dragon_whispers=dragon_whispers,
    )
    if args.check:
        drift = check(outputs)
        for item in drift:
            print(item)
        if drift:
            print(f"newsroom drift: {len(drift)} file(s)")
            return 1
        print(f"newsroom current: {len(outputs)} files")
        return 0
    changed, unchanged = publish(outputs)
    print(
        f"newsroom -> {READING.relative_to(ROOT)} · {feed['n_stories']} instruments · "
        f"{wire['n_events'] if wire else 0} events · "
        f"{machine_analyses['n_cases'] if machine_analyses else 0} machine reports · "
        f"{changed} files updated · {unchanged} unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
