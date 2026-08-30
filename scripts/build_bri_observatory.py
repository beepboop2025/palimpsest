#!/usr/bin/env python3
"""Build the public Belt and Road coverage contract and source-ledger page."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from core import newswire as newswire_model
from core import ucdp_aggregate
from processors.bri_observatory import (
    PUBLIC_BUILD_STATES,
    WDI_ARTIFACT_PATH,
    WDI_OBSERVATION_SCHEMA_PATH,
    WDI_PUBLICATION_RECEIPT_PATH,
    WDI_SERIES_REGISTRY_PATH,
    build_public_artifact,
    build_wdi_observation_descriptor,
    load_wdi_bundle,
    load_registry,
)
from scripts import site_nav
from scripts import build_chinese_translation_pages as chinese_translation_pages


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "config" / "bri_observatory.json"
DEFAULT_JSON = ROOT / "readings" / "belt-and-road-observatory-latest.json"
DEFAULT_HTML = ROOT / "belt-and-road" / "index.html"
DEFAULT_GWADAR_HTML = ROOT / "belt-and-road" / "gwadar" / "index.html"
DEFAULT_BALOCHISTAN_HTML = ROOT / "belt-and-road" / "balochistan" / "index.html"
DEFAULT_MYANMAR_HTML = ROOT / "belt-and-road" / "myanmar" / "index.html"
DEFAULT_GWADAR_ANALYSIS_HTML = (
    ROOT / "belt-and-road" / "gwadar" / "analysis" / "index.html"
)
DEFAULT_GWADAR_ANALYSIS_JSON = (
    ROOT / "belt-and-road" / "gwadar" / "analysis" / "article.json"
)
DEFAULT_BALOCHISTAN_ANALYSIS_HTML = (
    ROOT / "belt-and-road" / "balochistan" / "analysis" / "index.html"
)
DEFAULT_BALOCHISTAN_ANALYSIS_JSON = (
    ROOT / "belt-and-road" / "balochistan" / "analysis" / "article.json"
)
DEFAULT_MYANMAR_ANALYSIS_HTML = (
    ROOT / "belt-and-road" / "myanmar" / "analysis" / "index.html"
)
DEFAULT_MYANMAR_ANALYSIS_JSON = (
    ROOT / "belt-and-road" / "myanmar" / "analysis" / "article.json"
)
DEFAULT_NEWSWIRE = ROOT / "readings" / "newswire-latest.json"
DEFAULT_NEWSWIRE_VERSIONS = ROOT / "readings" / "newswire-versions.jsonl"
DEFAULT_NEWS_SOURCE_REGISTRY = ROOT / "config" / "news_sources.json"
DEFAULT_EDITORIAL_EVIDENCE = ROOT / "config" / "regional_editorials.json"
DEFAULT_CHINESE_TRANSLATIONS = ROOT / "readings" / "chinese-translations-latest.json"
DEFAULT_CHINESE_TRANSLATION_SCHEMA = ROOT / "protocol" / "chinese-translations-v1.schema.json"
DEFAULT_UCDP_AGGREGATE = ROOT / "readings" / "ucdp-aggregate-latest.json"
DEFAULT_UCDP_SCHEMA = ROOT / "protocol" / "ucdp-aggregate-v1.schema.json"
DEFAULT_WDI_BUNDLE = ROOT / WDI_ARTIFACT_PATH
DEFAULT_WDI_SCHEMA = ROOT / WDI_OBSERVATION_SCHEMA_PATH
DEFAULT_WDI_SERIES_REGISTRY = ROOT / WDI_SERIES_REGISTRY_PATH
DEFAULT_WDI_PUBLICATION_RECEIPT = ROOT / WDI_PUBLICATION_RECEIPT_PATH
_AUTO_WDI_PUBLICATION_RECEIPT = object()
_PUBLICATION_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PAKISTAN_GWADAR_GEOGRAPHIES = ("PAK", "PAK-BAL", "PAK-GWD")
_PAKISTAN_GWADAR_TARGETS = (
    "cpec_portfolio",
    "gwadar_port_free_zone",
    "gwadar_connectivity",
    "gwadar_public_services",
    "balochistan_resources_revenue",
)
_REGIONAL_OUTPUT_ROOTS = {
    "bri": ROOT / "belt-and-road" / "data",
    "gwadar": ROOT / "belt-and-road" / "gwadar" / "data",
    "balochistan": ROOT / "belt-and-road" / "balochistan" / "data",
    "myanmar": ROOT / "belt-and-road" / "myanmar" / "data",
}
_REGIONAL_LABELS = {
    "bri": "BRI and regional corridors",
    "gwadar": "CPEC and Gwadar",
    "balochistan": "Balochistan",
    "myanmar": "Myanmar",
}
_REGIONAL_GEOGRAPHIES = {
    "bri": ("CHN", "PAK", "MMR"),
    "gwadar": ("PAK",),
    "balochistan": ("PAK",),
    "myanmar": ("MMR",),
}
_REGIONAL_TARGETS = {
    "bri": (),
    "gwadar": _PAKISTAN_GWADAR_TARGETS,
    "balochistan": (
        "balochistan_resources_revenue",
        "balochistan_movement_history",
    ),
    "myanmar": (
        "cmec_portfolio",
        "kyaukpyu_port_sez",
        "china_myanmar_pipelines",
        "mandalay_muse_rail",
    ),
}
_REGIONAL_DEDICATED_SOURCE_IDS = {
    "gwadar": frozenset(
        {
            "arab-news-pakistan-gwadar-port",
            "daily-cpec-gwadar",
        }
    ),
    "balochistan": frozenset(
        {
            "express-tribune-balochistan",
            "hrc-balochistan",
        }
    ),
    "myanmar": frozenset(
        {
            "kachin-news-group",
            "myanmar-now",
            "shan-news-english",
        }
    ),
}
_REGIONAL_NEWS = {
    "gwadar": {
        "dedicated_source_ids": _REGIONAL_DEDICATED_SOURCE_IDS["gwadar"],
        "terms": (
            "china-pakistan economic corridor",
            "cpec",
            "gwadar",
            "中巴经济走廊",
            "中巴經濟走廊",
            "瓜达尔",
            "瓜達爾",
        ),
    },
    "balochistan": {
        "dedicated_source_ids": _REGIONAL_DEDICATED_SOURCE_IDS["balochistan"],
        "terms": (
            "baloch",
            "balochistan",
            "gwadar",
            "quetta",
            "俾路支",
            "俾路支斯坦",
            "巴洛奇",
            "瓜达尔",
            "瓜達爾",
        ),
    },
    "myanmar": {
        "dedicated_source_ids": _REGIONAL_DEDICATED_SOURCE_IDS["myanmar"],
        "terms": (
            "burma",
            "cmec",
            "kachin",
            "kyaukpyu",
            "myanmar",
            "rakhine",
            "rohingya",
            "shan state",
            "缅甸",
            "緬甸",
            "中缅经济走廊",
            "中緬經濟走廊",
            "皎漂",
            "掸邦",
            "撣邦",
            "克钦",
            "克欽",
            "若开",
            "若開",
        ),
    },
}
_REGIONAL_ANALYSIS = {
    "gwadar": {
        "label": "Gwadar and CPEC",
        "country_code": "PAK",
        "canonical_path": "/belt-and-road/gwadar/analysis/",
        "title": "Gwadar's development story is also a test of whose evidence counts",
        "dek": (
            "A recurring analysis of CPEC and Gwadar reporting, source balance, "
            "economic context, and the gap between development claims and locally "
            "documented effects."
        ),
        "relation_terms": ("baloch", "china", "cpec", "gwadar"),
        "relation_heading": "Development claims and local effects must share the page",
        "position": (
            "Port, corridor and investment announcements deserve measurement against "
            "employment, land, water, fisheries, public services and grievance records. "
            "An announcement is evidence of a claim, not proof of delivery."
        ),
    },
    "balochistan": {
        "label": "Balochistan",
        "country_code": "PAK",
        "canonical_path": "/belt-and-road/balochistan/analysis/",
        "title": "Balochistan's rights record cannot be edited out of the CPEC story",
        "dek": (
            "A recurring, attributed analysis of reported abuses, civic and state "
            "actions, resource governance, Gwadar, and Pakistan-China political economy."
        ),
        "relation_terms": ("china", "cpec", "gwadar"),
        "relation_heading": "The Pakistan-China relationship is a question for evidence, not insinuation",
        "position": (
            "Reports of disappearance, detention, killing, intimidation or collective "
            "punishment require prominent scrutiny and precise attribution. Development "
            "announcements do not rebut rights allegations, and allegations are not "
            "silently upgraded into adjudicated findings."
        ),
    },
    "myanmar": {
        "label": "Myanmar",
        "country_code": "MMR",
        "canonical_path": "/belt-and-road/myanmar/analysis/",
        "title": "Myanmar's corridor politics cannot be separated from conflict and rights",
        "dek": (
            "A recurring analysis of Myanmar reporting across CMEC, Kyaukpyu, conflict, "
            "rights, and regional voices from Rakhine, Kachin and Shan."
        ),
        "relation_terms": ("china", "cmec", "kyaukpyu", "pipeline", "rail"),
        "relation_heading": "Infrastructure exposure sits inside a conflict-affected polity",
        "position": (
            "Infrastructure and investment claims must be read beside conflict, "
            "displacement, consent and distributional evidence. National aggregates "
            "cannot substitute for affected-community records."
        ),
    },
}
_ANALYSIS_INDICATORS = {
    "NY.GDP.MKTP.KD.ZG": "Real GDP growth",
    "NE.TRD.GNFS.ZS": "Trade share of GDP",
    "BX.KLT.DINV.WD.GD.ZS": "FDI net inflows",
    "SL.UEM.TOTL.ZS": "Unemployment rate",
    "EG.ELC.ACCS.ZS": "Electricity access",
    "IS.SHP.GOOD.TU": "Container port traffic",
}


def _resolve_wdi_publication_receipts(
    publication_receipt_path: Path | None | object,
    archived_size_receipt_path: Path | None,
    *,
    source_implementation: str,
) -> tuple[Path | None, Path | None]:
    """Resolve no-flag production proof without weakening explicit fixture mode."""

    if publication_receipt_path is not _AUTO_WDI_PUBLICATION_RECEIPT:
        return publication_receipt_path, archived_size_receipt_path
    if source_implementation != "live":
        return None, archived_size_receipt_path
    if not DEFAULT_WDI_PUBLICATION_RECEIPT.is_file():
        return None, archived_size_receipt_path
    if archived_size_receipt_path is not None:
        return DEFAULT_WDI_PUBLICATION_RECEIPT, archived_size_receipt_path
    try:
        document = json.loads(
            DEFAULT_WDI_PUBLICATION_RECEIPT.read_text(encoding="utf-8")
        )
        publication_sha = document["workflow"]["publication_sha"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(
            "cannot resolve the default WDI Pages publication receipt"
        ) from exc
    if type(publication_sha) is not str or not _PUBLICATION_SHA_RE.fullmatch(
        publication_sha
    ):
        raise ValueError(
            "default WDI Pages receipt publication_sha must be lowercase 40-hex"
        )
    size_receipt = ROOT / (
        ".well-known/receipts/pages-artifact-size-" + publication_sha + ".json"
    )
    return DEFAULT_WDI_PUBLICATION_RECEIPT, size_receipt


def _json_bytes(document: dict) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _load_newswire(path: Path = DEFAULT_NEWSWIRE) -> dict:
    document = newswire_model.strict_json_loads(path.read_bytes(), label=str(path))
    newswire_model.validate_prior_newswire_document(document)
    return document


def _load_ucdp_aggregate(path: Path = DEFAULT_UCDP_AGGREGATE) -> dict:
    return ucdp_aggregate.validate_public_bytes(
        path.read_bytes(),
        schema_path=DEFAULT_UCDP_SCHEMA,
    )


def _load_editorial_evidence(
    path: Path = DEFAULT_EDITORIAL_EVIDENCE,
) -> tuple[dict, str]:
    """Load the human-authored, claim-classed evidence used by stable editorials."""

    raw = path.read_bytes()
    document = newswire_model.strict_json_loads(raw, label=str(path))
    if type(document) is not dict or set(document) != {
        "schema_version",
        "as_of",
        "evidence",
        "editorials",
        "method",
        "limitations",
    }:
        raise ValueError(f"{path} does not match the editorial-evidence contract")
    if document["schema_version"] != "palimpsest.regional-editorial-evidence.v1":
        raise ValueError(f"{path}.schema_version is unsupported")
    if type(document["as_of"]) is not str or not re.fullmatch(
        r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", document["as_of"]
    ):
        raise ValueError(f"{path}.as_of must be an ISO date")

    evidence_by_id = {}
    valid_regions = set(_REGIONAL_LABELS)
    required_evidence_fields = {
        "evidence_id",
        "regions",
        "source_name",
        "source_title",
        "url",
        "published_at",
        "source_role",
        "evidence_state",
        "statement",
        "interpretation_limit",
    }
    if type(document["evidence"]) is not list or not document["evidence"]:
        raise ValueError(f"{path}.evidence must be a non-empty list")
    for position, row in enumerate(document["evidence"]):
        label = f"{path}.evidence[{position}]"
        if type(row) is not dict or set(row) != required_evidence_fields:
            raise ValueError(f"{label} does not match the evidence-row contract")
        evidence_id = row["evidence_id"]
        if (
            type(evidence_id) is not str
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,95}", evidence_id)
            or evidence_id in evidence_by_id
        ):
            raise ValueError(f"{label}.evidence_id is invalid or duplicated")
        regions = row["regions"]
        if (
            type(regions) is not list
            or not regions
            or any(type(region) is not str for region in regions)
            or not set(regions).issubset(valid_regions)
            or len(regions) != len(set(regions))
        ):
            raise ValueError(f"{label}.regions is invalid")
        if type(row["url"]) is not str or not row["url"].startswith("https://"):
            raise ValueError(f"{label}.url must be HTTPS")
        if type(row["published_at"]) is not str or not re.fullmatch(
            r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", row["published_at"]
        ):
            raise ValueError(f"{label}.published_at must be an ISO date")
        for field in (
            "source_name",
            "source_title",
            "source_role",
            "evidence_state",
            "statement",
            "interpretation_limit",
        ):
            value = row[field]
            if type(value) is not str or not value.strip() or len(value) > 2400:
                raise ValueError(f"{label}.{field} is invalid")
        evidence_by_id[evidence_id] = row

    editorials = document["editorials"]
    if type(editorials) is not dict or set(editorials) != valid_regions:
        raise ValueError(f"{path}.editorials must cover every regional lane")
    used_evidence = set()
    for region, editorial in editorials.items():
        label = f"{path}.editorials.{region}"
        if type(editorial) is not dict or set(editorial) != {
            "title",
            "dek",
            "sections",
        }:
            raise ValueError(f"{label} does not match the editorial contract")
        for field in ("title", "dek"):
            if type(editorial[field]) is not str or not editorial[field].strip():
                raise ValueError(f"{label}.{field} is invalid")
        sections = editorial["sections"]
        if type(sections) is not list or not sections:
            raise ValueError(f"{label}.sections must be non-empty")
        section_ids = set()
        for section_position, section in enumerate(sections):
            section_label = f"{label}.sections[{section_position}]"
            if type(section) is not dict or set(section) != {
                "section_id",
                "heading",
                "paragraphs",
                "evidence_ids",
            }:
                raise ValueError(
                    f"{section_label} does not match the editorial-section contract"
                )
            section_id = section["section_id"]
            if (
                type(section_id) is not str
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,95}", section_id)
                or section_id in section_ids
            ):
                raise ValueError(f"{section_label}.section_id is invalid or duplicated")
            section_ids.add(section_id)
            if type(section["heading"]) is not str or not section["heading"].strip():
                raise ValueError(f"{section_label}.heading is invalid")
            paragraphs = section["paragraphs"]
            if (
                type(paragraphs) is not list
                or not paragraphs
                or any(type(paragraph) is not str or not paragraph.strip() for paragraph in paragraphs)
            ):
                raise ValueError(f"{section_label}.paragraphs is invalid")
            evidence_ids = section["evidence_ids"]
            if (
                type(evidence_ids) is not list
                or not evidence_ids
                or len(evidence_ids) != len(set(evidence_ids))
                or any(evidence_id not in evidence_by_id for evidence_id in evidence_ids)
                or any(region not in evidence_by_id[evidence_id]["regions"] for evidence_id in evidence_ids)
            ):
                raise ValueError(f"{section_label}.evidence_ids is invalid for {region}")
            used_evidence.update(evidence_ids)

    for field in ("method", "limitations"):
        values = document[field]
        if (
            type(values) is not list
            or not values
            or any(type(value) is not str or not value.strip() for value in values)
        ):
            raise ValueError(f"{path}.{field} is invalid")
    unused = set(evidence_by_id).difference(used_evidence)
    if unused:
        raise ValueError(f"{path} has uncited editorial evidence: {sorted(unused)}")
    return document, hashlib.sha256(raw).hexdigest()


def _load_newswire_versions(
    path: Path = DEFAULT_NEWSWIRE_VERSIONS,
) -> tuple[list[dict], str]:
    """Load the append-only event-version ledger with a strict public shape."""

    raw = path.read_bytes()
    rows = []
    required = {
        "event_id",
        "evidence_strength",
        "headline",
        "previous_version_id",
        "published_at",
        "recorded_at",
        "source_ids",
        "version_id",
    }
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        row = newswire_model.strict_json_loads(
            line,
            label=f"{path}:{line_number}",
        )
        if type(row) is not dict or set(row) != required:
            raise ValueError(
                f"{path}:{line_number} does not match the event-version ledger contract"
            )
        for field, maximum in (
            ("event_id", 96),
            ("version_id", 96),
            ("evidence_strength", 64),
            ("headline", 600),
            ("published_at", 32),
            ("recorded_at", 32),
        ):
            value = row[field]
            if type(value) is not str or not value or len(value) > maximum:
                raise ValueError(f"{path}:{line_number}.{field} is invalid")
        previous = row["previous_version_id"]
        if previous is not None and (
            type(previous) is not str or not previous or len(previous) > 96
        ):
            raise ValueError(
                f"{path}:{line_number}.previous_version_id is invalid"
            )
        source_ids = row["source_ids"]
        if (
            type(source_ids) is not list
            or not source_ids
            or len(source_ids) > 32
            or any(type(source_id) is not str or not source_id for source_id in source_ids)
            or len(source_ids) != len(set(source_ids))
        ):
            raise ValueError(f"{path}:{line_number}.source_ids is invalid")
        rows.append(row)
    if not rows:
        raise ValueError(f"{path} contains no event versions")
    return rows, hashlib.sha256(raw).hexdigest()


def _source_registry_projection(
    path: Path = DEFAULT_NEWS_SOURCE_REGISTRY,
) -> tuple[dict[str, object], str]:
    registry = newswire_model.load_source_registry(path)
    return {source.id: source for source in registry.sources}, registry.sha256


def _captured_event_seed(row: Mapping[str, object]) -> dict:
    return {
        "event_id": row["event_id"],
        "versions": {},
        "source_ids": set(),
        "region_tags": set(),
        "current": None,
    }


def _translation_index(document: Mapping[str, object] | None) -> dict:
    """Index immutable translation records without mutating publisher evidence."""

    if document is None:
        return {"by_event_and_version": {}, "by_event": {}, "public_paths": {}}
    by_event_and_version = {}
    by_event: dict[str, list[dict]] = {}
    for row in document["translations"]:
        identity = row["identity"]
        event_version_id = identity.get("event_version_id")
        event_id = identity.get("event_id")
        if event_id and event_version_id:
            event_version_key = (event_id, event_version_id)
            prior = by_event_and_version.get(event_version_key)
            if prior is not None and prior["record_sha256"] != row["record_sha256"]:
                raise ValueError(
                    "translation sidecar has conflicting event identity "
                    f"{event_id}/{event_version_id}"
                )
            by_event_and_version[event_version_key] = row
        if event_id:
            by_event.setdefault(event_id, []).append(row)
    return {
        "by_event_and_version": by_event_and_version,
        "by_event": by_event,
        "public_paths": chinese_translation_pages.translation_public_paths(document),
    }


def _translation_projection(
    translation_index: Mapping[str, object],
    *,
    event_id: str,
    event_version_id: str,
    headline: str,
) -> dict | None:
    row = translation_index["by_event_and_version"].get(
        (event_id, event_version_id)
    )
    if row is None:
        candidates = translation_index["by_event"].get(event_id, [])
        if candidates:
            priority = {
                "current_wire_event": 4,
                "retained_current_story": 3,
                "retained_event_revision": 2,
                "current_wire_item": 1,
            }
            row = max(
                candidates,
                key=lambda candidate: (
                    candidate["original_zh"]["title"].strip() == headline.strip(),
                    priority.get(candidate["record_kind"], 0),
                    candidate["source_clocks"].get("updated_at") or "",
                    candidate["translation_id"],
                ),
            )
    if row is None:
        return None
    translated = row["english"]
    original = row["original_zh"]
    provenance = row["translation_provenance"]
    return {
        "translation_id": row["translation_id"],
        "public_url": translation_index["public_paths"][row["translation_id"]],
        "title_en": translated["title_en"],
        "context_en": translated["context_en"],
        "translation_notes_en": translated["translation_notes_en"],
        "status": translated["status"],
        "background_en": translated["background_en"],
        "background_basis": translated["background_basis"],
        "background_status": translated["background_status"],
        "original_title_zh": original["title"],
        "original_context_zh": original["context"],
        "original_content_sha256": original["content_sha256"],
        "provider": provenance["provider"],
        "model_id": provenance["model_id"],
        "prompt_revision": provenance["prompt_revision"],
    }


def _is_chinese_dominant_metadata(*values: object) -> bool:
    """Mirror the translation-sidecar admission rule for regional completeness."""

    text = "\n".join(str(value) for value in values if value)
    han_characters = sum(
        1
        for character in text
        if (
            0x3400 <= ord(character) <= 0x4DBF
            or 0x4E00 <= ord(character) <= 0x9FFF
            or 0xF900 <= ord(character) <= 0xFAFF
            or 0x20000 <= ord(character) <= 0x2FA1F
        )
    )
    letter_characters = sum(character.isalpha() for character in text)
    return bool(
        han_characters >= 4
        and letter_characters
        and han_characters / letter_characters >= 0.35
    )


def _historical_event_page_text(event_id: str) -> str:
    """Recover bounded public dossier text without admitting shell/navigation labels."""

    path = ROOT / "news" / "wire" / event_id / "index.html"
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if not raw or len(raw) > 2 * 1024 * 1024:
        return ""
    try:
        document = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return ""
    marker = '<main id="main"'
    if document.count(marker) != 1:
        return ""
    main = document.split(marker, 1)[1].split("</main>", 1)[0]
    main = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", main, flags=re.I | re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", main)).casefold()


def _build_captured_index(
    wire: Mapping[str, object],
    versions: list[dict],
    *,
    versions_sha256: str,
    source_specs: Mapping[str, object],
    source_registry_sha256: str,
    region: str,
    translations: Mapping[str, object] | None = None,
) -> dict:
    """Build one loss-aware regional index from every retained event version."""

    if region not in _REGIONAL_LABELS:
        raise ValueError(f"unknown captured-index region: {region}")
    if wire["source_registry_sha256"] != source_registry_sha256:
        raise ValueError(
            "current newswire does not bind the exact configured source registry"
        )
    if translations is not None:
        translated_ledger_sha256 = translations["source_snapshot"][
            "newswire_ledger_sha256"
        ]
        if translated_ledger_sha256 != versions_sha256:
            raise ValueError(
                "Chinese translation sidecar does not bind the exact event-version "
                "ledger used by the regional archive"
            )

    grouped: dict[str, dict] = {}
    unknown_sources: set[str] = set()
    for row in versions:
        unknown_sources.update(
            source_id for source_id in row["source_ids"] if source_id not in source_specs
        )
        group = grouped.setdefault(row["event_id"], _captured_event_seed(row))
        group["versions"][row["version_id"]] = row
        group["source_ids"].update(row["source_ids"])
        group["region_tags"].update(
            _regional_tags(set(row["source_ids"]), row["headline"].casefold())
        )
    if unknown_sources:
        raise ValueError(
            f"event-version ledger contains unknown source ids: {sorted(unknown_sources)}"
        )
    for event_id, group in grouped.items():
        page_text = _historical_event_page_text(event_id)
        if page_text:
            group["region_tags"].update(
                _regional_tags(set(group["source_ids"]), page_text)
            )

    current_by_id = {event["event_id"]: event for event in wire["events"]}
    for event in wire["events"]:
        synthetic = {
            "event_id": event["event_id"],
            "evidence_strength": event["evidence_strength"],
            "headline": event["headline"],
            "previous_version_id": None,
            "published_at": event["published_at"],
            "recorded_at": wire["generated_at"],
            "source_ids": sorted(
                {reference["source_id"] for reference in event["evidence_refs"]}
            ),
            "version_id": event["version_id"],
        }
        group = grouped.setdefault(event["event_id"], _captured_event_seed(synthetic))
        group["versions"].setdefault(event["version_id"], synthetic)
        group["source_ids"].update(synthetic["source_ids"])
        group["region_tags"].update(
            _regional_tags(set(synthetic["source_ids"]), _event_content_text(event))
        )
        group["current"] = event

    events = []
    translations_by_event = _translation_index(translations)
    for event_id, group in grouped.items():
        if region not in group["region_tags"]:
            continue
        event_versions = sorted(
            group["versions"].values(),
            key=lambda row: (row["recorded_at"], row["version_id"]),
        )
        latest = event_versions[-1]
        source_ids = sorted(group["source_ids"])
        current = group["current"] or current_by_id.get(event_id)
        publisher_links = []
        if current is not None:
            seen_links: set[tuple[str, str]] = set()
            for reference in current["evidence_refs"]:
                key = (reference["source_id"], reference["url"])
                if key in seen_links:
                    continue
                seen_links.add(key)
                publisher_links.append(
                    {
                        "source_id": reference["source_id"],
                        "title": reference["title"],
                        "url": reference["url"],
                    }
                )
        page_path = ROOT / "news" / "wire" / event_id / "index.html"
        english_translation = _translation_projection(
            translations_by_event,
            event_id=event_id,
            event_version_id=latest["version_id"],
            headline=latest["headline"],
        )
        if translations is not None and _is_chinese_dominant_metadata(
            latest["headline"],
            current["dek"] if current is not None else None,
        ) and english_translation is None:
            raise ValueError(
                "regional Chinese publisher metadata has no English translation: "
                f"{region}/{event_id}/{latest['version_id']}"
            )
        events.append(
            {
                "event_id": event_id,
                "event_url": f"https://palimpsest.info/news/wire/{event_id}/",
                "event_page_available": current is not None or page_path.is_file(),
                "headline": latest["headline"],
                "dek": current["dek"] if current is not None else None,
                "published_at": latest["published_at"],
                "first_recorded_at": event_versions[0]["recorded_at"],
                "last_recorded_at": event_versions[-1]["recorded_at"],
                "source_ids": source_ids,
                "source_names": [source_specs[source_id].name for source_id in source_ids],
                "evidence_strength": latest["evidence_strength"],
                "version_count": len(event_versions),
                "version_ids": [row["version_id"] for row in event_versions],
                "current_in_wire": current is not None,
                "topics": current["topics"] if current is not None else [],
                "region_tags": [
                    tag
                    for tag in ("bri", "gwadar", "balochistan", "myanmar")
                    if tag in group["region_tags"]
                ],
                "publisher_links": publisher_links,
                "english_translation": english_translation,
            }
        )
    events.sort(
        key=lambda event: (event["published_at"], event["event_id"]),
        reverse=True,
    )

    source_counts = Counter(
        source_id for event in events for source_id in event["source_ids"]
    )
    sources = []
    for source_id in sorted(source_counts):
        source = source_specs[source_id]
        sources.append(
            {
                "source_id": source_id,
                "source_name": source.name,
                "feed_url": source.feed_url,
                "role": source.role,
                "independence_group": source.independence_group,
                "rights_policy": source.rights_policy,
                "captured_event_count": source_counts[source_id],
            }
        )

    event_version_count = sum(event["version_count"] for event in events)
    document = {
        "schema_version": "palimpsest.regional-captured-index.v1",
        "region": region,
        "label": _REGIONAL_LABELS[region],
        "generated_at": wire["generated_at"],
        "scope": (
            "Every unique event retained in the append-only Palimpsest event-version "
            "ledger that matches this regional lane by reviewed source identity or "
            "bounded title metadata, plus every matching event in the current wire."
        ),
        "classification": {
            "overlap_allowed": True,
            "method": "reviewed dedicated source ids plus bounded metadata terms",
            "article_body_fetching": "prohibited",
            "bri_lane": "combined index of all three regional lanes plus explicit BRI terms",
            "csv_formula_neutralization": (
                "CSV text cells whose first non-whitespace character is =, +, -, "
                "or @ receive a leading apostrophe; JSON and JSONL preserve the "
                "exact captured text."
            ),
        },
        "rights": {
            "publication_mode": "metadata-link-only",
            "article_bodies_included": False,
            "publisher_copyright_retained": True,
            "source_policy": "config/news_sources.json",
        },
        "inputs": {
            "newswire_generated_at": wire["generated_at"],
            "newswire_window": wire["window"],
            "newswire_versions_sha256": versions_sha256,
            "source_registry_sha256": source_registry_sha256,
            "chinese_translation_sidecar": (
                {
                    "schema_version": translations["schema_version"],
                    "generated_at": translations["generated_at"],
                    "newswire_sha256": translations["source_snapshot"][
                        "newswire_sha256"
                    ],
                    "newswire_ledger_sha256": translations["source_snapshot"][
                        "newswire_ledger_sha256"
                    ],
                    "newswire_ledger_rows": translations["source_snapshot"][
                        "newswire_ledger_rows"
                    ],
                    "translated_records": translations["coverage"][
                        "translated_records"
                    ],
                    "missing_records": translations["coverage"]["missing_records"],
                }
                if translations is not None
                else None
            ),
        },
        "capture_universe": {
            "event_versions": len(versions),
            "unique_events": len(grouped),
            "current_wire_events": wire["n_events"],
        },
        "counts": {
            "event_versions": event_version_count,
            "unique_events": len(events),
            "current_events": sum(event["current_in_wire"] for event in events),
            "historical_events": sum(not event["current_in_wire"] for event in events),
            "event_pages_available": sum(
                event["event_page_available"] for event in events
            ),
            "sources": len(sources),
            "events_with_english_translation": sum(
                event["english_translation"] is not None for event in events
            ),
        },
        "coverage": {
            "earliest_published_at": events[-1]["published_at"] if events else None,
            "latest_published_at": events[0]["published_at"] if events else None,
            "earliest_recorded_at": min(
                (event["first_recorded_at"] for event in events),
                default=None,
            ),
            "latest_recorded_at": max(
                (event["last_recorded_at"] for event in events),
                default=None,
            ),
        },
        "sources": sources,
        "events": events,
        "limitations": [
            "This is the complete retained Palimpsest capture ledger for the declared classifier, not every article ever published on the internet.",
            "Feed metadata can be incomplete, partisan, mistaken, translated, revised or removed; a listing is attribution, not endorsement or adjudication.",
            "Historical ledger entries may lack a retained excerpt or publisher URL; the event page is linked only when a generated public page remains present.",
            "A source-specific lane can include a relevant regional item whose headline omits the place name; records may therefore appear in multiple lanes.",
            "Restricted article bodies and licensed event-level datasets are not copied into this metadata dump.",
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    document["content_sha256"] = digest
    return document


def _jsonl_bytes(index: Mapping[str, object]) -> bytes:
    return b"".join(
        json.dumps(
            {
                "schema_version": index["schema_version"],
                "region": index["region"],
                **event,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
        for event in index["events"]
    )


def _csv_safe(value: object) -> object:
    """Neutralize spreadsheet formulas while leaving typed numeric cells intact."""

    if not isinstance(value, str):
        return value
    significant = value.lstrip(" \t\r\n")
    if significant.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _csv_bytes(index: Mapping[str, object]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "event_id",
            "event_url",
            "event_page_available",
            "headline",
            "published_at",
            "first_recorded_at",
            "last_recorded_at",
            "source_ids",
            "source_names",
            "evidence_strength",
            "version_count",
            "current_in_wire",
            "region_tags",
            "english_headline",
            "english_excerpt",
            "translation_id",
            "translation_status",
            "background_en",
            "translation_public_url",
        )
    )
    for event in index["events"]:
        translation = event["english_translation"] or {}
        writer.writerow(
            tuple(
                _csv_safe(value)
                for value in (
                event["event_id"],
                event["event_url"],
                str(event["event_page_available"]).lower(),
                event["headline"],
                event["published_at"],
                event["first_recorded_at"],
                event["last_recorded_at"],
                "|".join(event["source_ids"]),
                "|".join(event["source_names"]),
                event["evidence_strength"],
                event["version_count"],
                str(event["current_in_wire"]).lower(),
                "|".join(event["region_tags"]),
                translation.get("title_en", ""),
                translation.get("context_en", ""),
                translation.get("translation_id", ""),
                translation.get("status", ""),
                translation.get("background_en", ""),
                translation.get("public_url", ""),
                )
            )
        )
    return output.getvalue().encode("utf-8")


def _build_regional_data_dump(
    artifact: Mapping[str, object],
    captured_index: Mapping[str, object],
    *,
    region: str,
    wdi_bundle: Mapping[str, object],
    ucdp_bundle: Mapping[str, object],
    editorial_evidence: Mapping[str, object],
    editorial_evidence_sha256: str,
) -> dict:
    """Join only independently licensed public context into one regional dump."""

    if region not in _REGIONAL_LABELS:
        raise ValueError(f"unknown regional data-dump lane: {region}")
    artifact_geographies = {
        "bri": {"CHN", "PAK", "MMR", "PAK-BAL", "PAK-GWD", "MMR-RKH"},
        "gwadar": {"PAK", "PAK-BAL", "PAK-GWD"},
        "balochistan": {"PAK-BAL", "PAK-GWD"},
        "myanmar": {"MMR", "MMR-RKH"},
    }[region]
    country_codes = set(_REGIONAL_GEOGRAPHIES[region])
    target_ids = set(_REGIONAL_TARGETS[region])
    targets = [
        target
        for target in artifact["watch_targets"]
        if region == "bri" or target["target_id"] in target_ids
    ]
    sources = [
        source
        for source in artifact["sources"]
        if region == "bri"
        or artifact_geographies.intersection(source["geographies"])
    ]
    observations = [
        row
        for row in wdi_bundle["observations"]
        if row["country_code"] in country_codes
    ]
    country_years = [
        row
        for row in ucdp_bundle["country_years"]
        if row["country_code"] in country_codes
    ]
    conflict_years = [
        row
        for row in ucdp_bundle["conflict_years"]
        if row["country_code"] in country_codes
    ]
    document = {
        "schema_version": "palimpsest.regional-data-dump.v1",
        "region": region,
        "label": _REGIONAL_LABELS[region],
        "generated_at": captured_index["generated_at"],
        "publication_boundary": {
            "role": "public research context",
            "cross_lane_causality": "prohibited",
            "missing_value_policy": "unavailable_not_zero",
            "article_body_fetching": "prohibited",
            "person_or_tactical_records": "excluded",
        },
        "captured_news": captured_index,
        "editorial_evidence": _editorial_projection(
            editorial_evidence,
            editorial_evidence_sha256=editorial_evidence_sha256,
            region=region,
        ),
        "coverage_contract": {
            "artifact_as_of": artifact["as_of"],
            "targets": targets,
            "sources": sources,
            "source_registration_is_observation": False,
        },
        "economic_context": {
            "schema_version": wdi_bundle["schema_version"],
            "generated_at": wdi_bundle["generated_at"],
            "observations_sha256": wdi_bundle["observations_sha256"],
            "source": wdi_bundle["source"],
            "context_policy": wdi_bundle["context_policy"],
            "country_codes": sorted(country_codes),
            "observation_count": len(observations),
            "observations": observations,
        },
        "conflict_context": {
            "schema_version": ucdp_bundle["schema_version"],
            "generated_at": ucdp_bundle["generated_at"],
            "source": ucdp_bundle["source"],
            "scope_policy": ucdp_bundle["scope_policy"],
            "country_years_sha256": ucdp_bundle["country_years_sha256"],
            "conflict_years_sha256": ucdp_bundle["conflict_years_sha256"],
            "country_year_count": len(country_years),
            "conflict_year_count": len(conflict_years),
            "country_years": country_years,
            "conflict_years": conflict_years,
        },
        "movement_taxonomy": (
            artifact["movement_taxonomy"]
            if region in {"bri", "balochistan"}
            else None
        ),
        "limitations": [
            "WDI values are national country-period context and cannot measure a province, corridor, project, community, actor or incident.",
            "UCDP rows retain the dataset's armed-conflict definitions and are not a census of every abuse, grievance, protest or political movement.",
            "The coverage contract includes registered, pending and unavailable routes; registration is not a captured observation.",
            "No cross-lane join in this dump proves that a BRI project caused conflict, repression, benefit or harm.",
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    document["content_sha256"] = digest
    return document


def _event_content_text(event: Mapping[str, object]) -> str:
    return " ".join(
        [str(event["headline"]), str(event["dek"])]
        + [
            str(reference["title"])
            for reference in event["evidence_refs"]
        ]
    ).casefold()


def _text_has_term(text: str, term: str) -> bool:
    return (
        re.search(
            rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])",
            text,
        )
        is not None
    )


def _regional_tags(source_ids: set[str], text: str) -> tuple[str, ...]:
    """Classify one captured publisher record into every applicable public lane.

    The dedicated-source rule is intentionally explicit: a Balochistan or Myanmar
    outlet can publish a relevant item whose title omits the place name. Broad
    publishers still require a bounded textual match. A record may appear in more
    than one lane because Gwadar is both a CPEC and Balochistan question.
    """

    tags = []
    for region in ("gwadar", "balochistan", "myanmar"):
        config = _REGIONAL_NEWS[region]
        source_match = bool(source_ids.intersection(config["dedicated_source_ids"]))
        term_match = any(_text_has_term(text, term) for term in config["terms"])
        if source_match or term_match:
            tags.append(region)
    if tags or any(
        _text_has_term(text, term)
        for term in (
            "belt and road",
            "belt and road initiative",
            "bri",
            "china-pakistan economic corridor",
            "cpec",
            "cmec",
            "一带一路",
            "一帶一路",
            "丝绸之路经济带",
            "絲綢之路經濟帶",
            "海上丝绸之路",
            "海上絲綢之路",
            "中巴经济走廊",
            "中巴經濟走廊",
            "中缅经济走廊",
            "中緬經濟走廊",
        )
    ):
        tags.insert(0, "bri")
    return tuple(tags)


def _regional_events(wire: Mapping[str, object], region: str) -> list[dict]:
    """Select every current regional wire event without fetching article bodies."""

    if region not in {"bri", *_REGIONAL_NEWS}:
        raise ValueError(f"unknown regional news lane: {region}")
    selected = []
    for event in wire["events"]:
        source_ids = {
            reference["source_id"] for reference in event["evidence_refs"]
        }
        text = _event_content_text(event)
        if region in _regional_tags(source_ids, text):
            selected.append(event)
    return sorted(
        selected,
        key=lambda event: (event["published_at"], event["event_id"]),
        reverse=True,
    )


def _latest_wdi_context(bundle: Mapping[str, object], country_code: str) -> list[dict]:
    """Project a small, exact national context panel from validated WDI rows."""

    latest: dict[str, dict] = {}
    for row in bundle["observations"]:
        indicator_id = row["indicator_id"]
        if (
            row["country_code"] != country_code
            or indicator_id not in _ANALYSIS_INDICATORS
            or row["evidence_state"] != "observed"
            or row["value"] is None
        ):
            continue
        prior = latest.get(indicator_id)
        if prior is None or row["period_end"] > prior["period_end"]:
            latest[indicator_id] = row

    rows = []
    for indicator_id, label in _ANALYSIS_INDICATORS.items():
        source = latest.get(indicator_id)
        if source is None:
            continue
        rows.append(
            {
                "indicator_id": indicator_id,
                "label": label,
                "period_end": source["period_end"],
                "value": source["value"],
                "unit": source["unit"],
                "evidence_state": source["evidence_state"],
                "obs_status": source["obs_status"],
                "source_dataset_last_updated": source["source_dataset_last_updated"],
                "retrieved_at": source["retrieved_at"],
                "evidence_url": source["evidence_url"],
            }
        )
    return rows


def _latest_ucdp_context(
    bundle: Mapping[str, object], country_code: str
) -> dict | None:
    rows = [
        row for row in bundle["country_years"] if row["country_code"] == country_code
    ]
    if not rows:
        return None
    row = max(rows, key=lambda item: item["year"])
    return {
        "country_code": country_code,
        "year": row["year"],
        "dataset_version": row["dataset_version"],
        "evidence_state": row["evidence_state"],
        "unit": row["unit"],
        "total": row["total"],
        "state_based": row["state_based"],
        "one_sided": row["one_sided"],
        "non_state": row["non_state"],
        "source_period_end_year": bundle["source"]["source_period_end_year"],
        "source_url": "/readings/ucdp-aggregate-latest.json",
        "scope": "annual national historical context only",
    }


def _counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def _analysis_evidence(
    event: Mapping[str, object],
    *,
    english_translation: Mapping[str, object] | None = None,
) -> dict:
    seen: set[tuple[str, str]] = set()
    sources = []
    for reference in event["evidence_refs"]:
        key = (reference["source_id"], reference["url"])
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "source_id": reference["source_id"],
                "source_name": reference["source_name"],
                "role": reference["role"],
                "independence_group": reference["independence_group"],
                "title": reference["title"],
                "url": reference["url"],
            }
        )
    return {
        "event_id": event["event_id"],
        "event_url": event["url"],
        "headline": event["headline"],
        "dek": event["dek"],
        "published_at": event["published_at"],
        "updated_at": event["updated_at"],
        "desk": event["desk"],
        "topics": event["topics"],
        "evidence_strength": event["evidence_strength"],
        "independent_group_count": len(event["evidence_groups"]),
        "sources": sources,
        "english_translation": english_translation,
    }


def _regional_analysis_projection(
    wire: Mapping[str, object], region: str
) -> dict[str, object]:
    events = _regional_events(wire, region)
    source_names: dict[str, str] = {}
    groups: set[str] = set()
    topic_counts: Counter[str] = Counter()
    role_counts: Counter[str] = Counter()
    strength_counts: Counter[str] = Counter()
    documentation_events = 0
    multi_group_events = 0
    for event in events:
        topic_counts.update(event["topics"])
        strength_counts[event["evidence_strength"]] += 1
        event_roles = set()
        for reference in event["evidence_refs"]:
            source_names[reference["source_id"]] = reference["source_name"]
            groups.add(reference["independence_group"])
            event_roles.add(reference["role"])
        role_counts.update(event_roles)
        documentation_events += "documentation" in event_roles
        multi_group_events += len(event["evidence_groups"]) > 1

    relation_terms = _REGIONAL_ANALYSIS[region]["relation_terms"]
    relation_events = [
        event
        for event in events
        if any(
            _text_has_term(_event_content_text(event), term)
            for term in relation_terms
        )
    ]
    return {
        "event_count": len(events),
        "source_count": len(source_names),
        "source_names": [source_names[key] for key in sorted(source_names)],
        "independence_group_count": len(groups),
        "topic_counts": _counter_dict(topic_counts),
        "role_counts": _counter_dict(role_counts),
        "evidence_strength_counts": _counter_dict(strength_counts),
        "documentation_event_count": documentation_events,
        "multi_group_event_count": multi_group_events,
        "single_group_event_count": len(events) - multi_group_events,
        "relation_event_count": len(relation_events),
        "relation_event_ids": [event["event_id"] for event in relation_events],
        "latest_event_at": events[0]["published_at"] if events else None,
        "earliest_event_at": events[-1]["published_at"] if events else None,
        "events": events,
    }


def _analysis_claims(region: str, projection: Mapping[str, object]) -> list[dict]:
    config = _REGIONAL_ANALYSIS[region]
    events = projection["events"]
    event_ids = [event["event_id"] for event in events]
    if events:
        current_record = (
            f'The current wire contains {projection["event_count"]} attributed event '
            f'dossiers from {projection["source_count"]} named sources across '
            f'{projection["independence_group_count"]} declared independence groups. '
            f'The cumulative regional archive retains {projection["archive_unique_event_count"]} '
            f'unique matching events, including {projection["archive_historical_event_count"]} '
            "that have left the rolling wire. These are publication-structure counts, "
            "not counts of verified incidents."
        )
    else:
        current_record = (
            "No event metadata fell inside this region's current wire window. This is "
            "a coverage result for the exact feed window, not evidence that nothing happened."
        )
    source_structure = (
        f'{projection["single_group_event_count"]} dossiers currently rely on one '
        f'independent source group; {projection["multi_group_event_count"]} contain more '
        "than one. Repetition inside one publisher group is not treated as corroboration."
    )
    if region == "balochistan":
        source_structure += (
            f' {projection["documentation_event_count"]} dossiers include a source '
            "classified as documentation, whose allegations remain attributed to that source."
        )
    relation = (
        f'{projection["relation_event_count"]} of the current '
        f'{projection["event_count"]} dossiers contain at least one retained '
        f'{"/".join(config["relation_terms"])} term. This measures topical overlap in '
        "publisher metadata; it does not establish coordination, responsibility, benefit, "
        "harm, or causation."
    )
    return [
        {
            "section_id": "current-record",
            "heading": "What changed in the current evidence window",
            "paragraph": current_record,
            "evidence_event_ids": event_ids[:12],
        },
        {
            "section_id": "source-structure",
            "heading": "How strong is the publication structure?",
            "paragraph": source_structure,
            "evidence_event_ids": event_ids[:12],
        },
        {
            "section_id": "relationship",
            "heading": config["relation_heading"],
            "paragraph": relation,
            "evidence_event_ids": projection["relation_event_ids"][:12],
        },
        {
            "section_id": "editorial-position",
            "heading": "Palimpsest's editorial reading",
            "paragraph": config["position"],
            "evidence_event_ids": event_ids[:12],
        },
    ]


def _build_regional_analysis(
    artifact: Mapping[str, object],
    wire: Mapping[str, object],
    *,
    region: str,
    wdi_bundle: Mapping[str, object],
    ucdp_bundle: Mapping[str, object],
    captured_index: Mapping[str, object] | None = None,
    editorial_evidence: Mapping[str, object] | None = None,
    editorial_evidence_sha256: str | None = None,
) -> dict:
    config = _REGIONAL_ANALYSIS[region]
    projection = _regional_analysis_projection(wire, region)
    events = projection.pop("events")
    archive_counts = (
        captured_index["counts"]
        if captured_index is not None
        else {
            "unique_events": len(events),
            "event_versions": len(events),
            "current_events": len(events),
            "historical_events": 0,
        }
    )
    projection["archive_unique_event_count"] = archive_counts["unique_events"]
    projection["archive_event_version_count"] = archive_counts["event_versions"]
    projection["archive_current_event_count"] = archive_counts["current_events"]
    projection["archive_historical_event_count"] = archive_counts["historical_events"]
    claims = _analysis_claims(region, {**projection, "events": events})
    country_code = config["country_code"]
    if editorial_evidence is None:
        editorial_evidence, loaded_sha256 = _load_editorial_evidence()
        editorial_evidence_sha256 = loaded_sha256
    if editorial_evidence_sha256 is None:
        raise ValueError("editorial evidence sha256 is required")
    editorial = _editorial_projection(
        editorial_evidence,
        editorial_evidence_sha256=editorial_evidence_sha256,
        region=region,
    )
    document = {
        "schema_version": "palimpsest.regional-analysis.v1",
        "article_id": f"palimpsest-regional-analysis-{region}",
        "region": region,
        "label": config["label"],
        "title": editorial["title"],
        "dek": editorial["dek"],
        "url": config["canonical_path"],
        "generated_at": wire["generated_at"],
        "status": "current-analysis" if events else "coverage-gap-analysis",
        "authorship": {
            "byline": "Palimpsest Evidence Desk",
            "mode": "source-bounded desk editorial plus deterministic evidence refresh",
            "assistance_disclosure": "AI-assisted synthesis with explicit evidence states, source links and interpretation limits",
            "human_interviews": "none",
        },
        "wire": {
            "generated_at": wire["generated_at"],
            "window": wire["window"],
            "total_event_count": wire["n_events"],
            "source_registry_sha256": wire["source_registry_sha256"],
        },
        "coverage": projection,
        "claims": claims,
        "editorial": editorial,
        "national_context": {
            "country_code": country_code,
            "wdi": _latest_wdi_context(wdi_bundle, country_code),
            "ucdp": _latest_ucdp_context(ucdp_bundle, country_code),
            "boundary": (
                "National context only. These values cannot establish a provincial, "
                "project, actor, incident, attribution, or causal claim."
            ),
        },
        "source_readiness": {
            "artifact_as_of": artifact["as_of"],
            "contract_url": "/readings/belt-and-road-observatory-latest.json",
            "meaning": (
                "The contract records source routes and build states; registration is "
                "not evidence that a project claim or rights allegation is true."
            ),
        },
        "evidence": [
            _analysis_evidence(
                event,
                english_translation=(
                    next(
                        (
                            row["english_translation"]
                            for row in captured_index["events"]
                            if row["event_id"] == event["event_id"]
                        ),
                        None,
                    )
                    if captured_index is not None
                    else None
                ),
            )
            for event in events
        ],
        "method": [
            *editorial_evidence["method"],
            "Select events from the sealed seven-day newswire by reviewed source ID or bounded regional term.",
            "Count named sources and declared independence groups before writing any source-strength sentence.",
            "Retain publisher titles, links, timestamps and bounded feed excerpts; do not fetch or republish article bodies.",
            "Attach validated World Bank WDI and UCDP annual aggregates as national context only.",
            "Regenerate the stable article URL whenever the sealed input bytes change.",
        ],
        "limitations": [
            *editorial_evidence["limitations"],
            "Publisher metadata may be incomplete, mistaken, partisan, translated, revised, or later removed.",
            "Source-group counts measure publication structure, not truth or legal proof.",
            "A documentation group's allegation is attributed reporting unless a separately cited adjudicative finding says otherwise.",
            "Topic overlap cannot prove that Pakistan and China coordinated a reported abuse or that a BRI project caused it.",
            "National economic and conflict aggregates cannot describe a specific province, community, project, actor, or incident.",
            "An empty or thin feed window is a coverage limitation, not a zero for events, harm, conflict, opposition, or public concern.",
        ],
        "disclosure": (
            "This page combines a dated, source-bounded desk editorial with a recurring "
            "deterministic refresh from validated public metadata and aggregate datasets. "
            "It uses no article-body scraping and makes no claim of eyewitness reporting."
        ),
        "correction_url": "/challenge.html",
    }
    content = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    document["content_sha256"] = digest
    document["revision_id"] = f"regional-analysisv-{digest[:24]}"
    return document


def _editorial_projection(
    document: Mapping[str, object],
    *,
    editorial_evidence_sha256: str,
    region: str,
) -> dict:
    """Project one complete editorial and every cited evidence record."""

    if region not in _REGIONAL_LABELS:
        raise ValueError(f"unknown editorial region: {region}")
    editorial = document["editorials"][region]
    evidence_by_id = {
        row["evidence_id"]: row for row in document["evidence"]
    }
    evidence_ids = list(
        dict.fromkeys(
            evidence_id
            for section in editorial["sections"]
            for evidence_id in section["evidence_ids"]
        )
    )
    return {
        "schema_version": document["schema_version"],
        "as_of": document["as_of"],
        "source_sha256": editorial_evidence_sha256,
        "title": editorial["title"],
        "dek": editorial["dek"],
        "sections": editorial["sections"],
        "evidence": [evidence_by_id[evidence_id] for evidence_id in evidence_ids],
        "method": document["method"],
        "limitations": document["limitations"],
    }


def _format_context_value(value: object, unit: str) -> str:
    number = float(value)
    if unit in {
        "annual percent",
        "percent of GDP",
        "percent of labor force",
        "percent of population",
    }:
        return f"{number:,.2f}%"
    if abs(number) >= 1_000_000_000:
        return f"{number / 1_000_000_000:,.2f} billion"
    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:,.2f} million"
    return f"{number:,.2f}"


def _render_analysis_html(article: Mapping[str, object]) -> bytes:
    canonical_path = article["url"]
    dossier_path = canonical_path.removesuffix("analysis/")
    coverage = article["coverage"]
    citation_numbers = {
        event["event_id"]: position
        for position, event in enumerate(article["evidence"], 1)
    }
    editorial_citation_numbers = {
        row["evidence_id"]: position
        for position, row in enumerate(
            article["editorial"]["evidence"], len(article["evidence"]) + 1
        )
    }
    citation_numbers.update(editorial_citation_numbers)
    claim_sections = []
    for claim in article["claims"]:
        citations = " ".join(
            f'<a href="#evidence-{_esc(event_id)}">[{citation_numbers[event_id]}]</a>'
            for event_id in claim["evidence_event_ids"]
        )
        claim_sections.append(
            f'<section id="{_esc(claim["section_id"])}" class="bri-analysis-section">'
            f'<h2>{_esc(claim["heading"])}</h2><p>{_esc(claim["paragraph"])}</p>'
            f'<p class="bri-citations">{citations}</p></section>'
        )
    evidence_rows = []
    for position, event in enumerate(article["evidence"], 1):
        sources = "; ".join(
            f'<a href="{_esc(source["url"])}" target="_blank" rel="noopener">'
            f'{_esc(source["source_name"])}</a> ({_esc(source["role"])})'
            for source in event["sources"]
        )
        translation = event.get("english_translation")
        headline = event["headline"]
        dek = event["dek"]
        translation_markup = ""
        if translation is not None:
            headline = translation["title_en"]
            dek = translation["context_en"] or dek
            translation_markup = _translation_markup(translation)
        evidence_rows.append(
            f'<article id="evidence-{_esc(event["event_id"])}" class="bri-evidence-row">'
            f'<p class="bri-eyebrow">[{position}] <time datetime="{_esc(event["published_at"])}">'
            f'{_esc(event["published_at"])}</time> · {_esc(event["evidence_strength"])}</p>'
            f'<h3><a href="{_esc(event["event_url"])}">{_esc(headline)}</a></h3>'
            f'<p>{_esc(dek)}</p>{translation_markup}<p><b>Attributed sources:</b> {sources}. '
            f'<b>Independent groups:</b> {event["independent_group_count"]}.</p></article>'
        )
    if not evidence_rows:
        evidence_rows.append(
            '<p class="bri-empty">No regional event metadata is present in this exact '
            'wire window. The article remains online to expose that coverage gap.</p>'
        )
    editorial_sections = []
    for section in article["editorial"]["sections"]:
        paragraphs = "".join(
            f"<p>{_esc(paragraph)}</p>" for paragraph in section["paragraphs"]
        )
        citations = " ".join(
            f'<a href="#evidence-{_esc(evidence_id)}">'
            f'[{editorial_citation_numbers[evidence_id]}]</a>'
            for evidence_id in section["evidence_ids"]
        )
        editorial_sections.append(
            f'<section id="{_esc(section["section_id"])}" '
            'class="bri-analysis-section bri-editorial-section">'
            f'<h2>{_esc(section["heading"])}</h2>{paragraphs}'
            f'<p class="bri-citations">{citations}</p></section>'
        )
    editorial_evidence_rows = []
    for row in article["editorial"]["evidence"]:
        position = editorial_citation_numbers[row["evidence_id"]]
        editorial_evidence_rows.append(
            f'<article id="evidence-{_esc(row["evidence_id"])}" '
            'class="bri-evidence-row bri-editorial-evidence-row">'
            f'<p class="bri-eyebrow">[{position}] '
            f'<time datetime="{_esc(row["published_at"])}">'
            f'{_esc(row["published_at"])}</time> · '
            f'{_esc(row["evidence_state"].replace("-", " "))}</p>'
            f'<h3><a href="{_esc(row["url"])}" target="_blank" rel="noopener">'
            f'{_esc(row["source_title"])}</a></h3>'
            f'<p><b>{_esc(row["source_name"])}:</b> {_esc(row["statement"])}</p>'
            f'<p><b>Interpretation limit:</b> {_esc(row["interpretation_limit"])}</p>'
            f'<p><small>Source role: {_esc(row["source_role"].replace("-", " "))} · '
            f'evidence key <code>{_esc(row["evidence_id"])}</code></small></p></article>'
        )
    wdi_rows = "".join(
        '<article class="bri-context-card">'
        f'<p class="bri-eyebrow">{_esc(row["indicator_id"])}</p>'
        f'<h3>{_esc(row["label"])}</h3>'
        f'<strong>{_esc(_format_context_value(row["value"], row["unit"]))}</strong>'
        f'<p>{_esc(row["period_end"][:4])} · {_esc(row["unit"])} · '
        f'{_esc(row["evidence_state"])} · source updated '
        f'{_esc(row["source_dataset_last_updated"])}</p>'
        f'<a href="{_esc(row["evidence_url"])}">World Bank API evidence</a></article>'
        for row in article["national_context"]["wdi"]
    )
    ucdp = article["national_context"]["ucdp"]
    if ucdp is None:
        ucdp_panel = '<p class="bri-empty">No validated annual conflict aggregate is available.</p>'
    else:
        total = ucdp["total"]
        ucdp_panel = (
            '<article class="bri-context-card bri-context-card--wide">'
            f'<p class="bri-eyebrow">UCDP {ucdp["dataset_version"]} · {ucdp["year"]}</p>'
            '<h3>National annual organized-violence context</h3>'
            f'<strong>{total["best"]:,} best estimate</strong>'
            f'<p>{total["low"]:,}–{total["high"]:,} uncertainty range, '
            f'{_esc(ucdp["unit"])}. State-based: {ucdp["state_based"]["best"]:,}; '
            f'one-sided: {ucdp["one_sided"]["best"]:,}; non-state: '
            f'{ucdp["non_state"]["best"]:,}.</p>'
            f'<p><b>Boundary:</b> {_esc(ucdp["scope"])}; this is not a provincial or '
            'incident-level rights measure.</p>'
            f'<a href="{_esc(ucdp["source_url"])}">Inspect the aggregate and uncertainty bounds</a>'
            '</article>'
        )
    topic_rows = "".join(
        f'<div><dt>{_esc(topic)}</dt><dd>{count}</dd></div>'
        for topic, count in sorted(
            coverage["topic_counts"].items(), key=lambda item: (-item[1], item[0])
        )
    ) or '<div><dt>Current topics</dt><dd>no observed rows</dd></div>'
    method = "".join(f'<li>{_esc(item)}</li>' for item in article["method"])
    limitations = "".join(
        f'<li>{_esc(item)}</li>' for item in article["limitations"]
    )
    json_ld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "AnalysisNewsArticle",
            "headline": article["title"],
            "description": article["dek"],
            "datePublished": article["generated_at"],
            "dateModified": article["generated_at"],
            "mainEntityOfPage": f"https://palimpsest.info{canonical_path}",
            "author": {"@type": "Organization", "name": "Palimpsest Evidence Desk"},
            "publisher": {"@type": "Organization", "name": "Palimpsest"},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    document = f'''<!doctype html>
<html lang="en" data-tk-theme="light">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(article["title"])} · Palimpsest analysis</title>
<meta name="description" content="{_esc(article["dek"])}">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://palimpsest.info{_esc(canonical_path)}">
<link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg">
<meta name="theme-color" content="#edf2ec">
<script type="application/ld+json">{json_ld}</script>
{site_nav.HEAD}
<link rel="stylesheet" href="/assets/bri.css">
</head>
<body class="ps tk bri-page bri-analysis-page" data-bri-analysis="{_esc(article["region"])}">
<!-- GENERATED BY scripts/build_bri_observatory.py -->
{site_nav.render(canonical_path)}
<main id="main">
  <article>
    <header class="bri-hero bri-analysis-hero">
      <p class="bri-eyebrow">Evidence-led regional editorial · {_esc(article["label"])} · source record as of <time datetime="{_esc(article["editorial"]["as_of"])}">{_esc(article["editorial"]["as_of"])}</time></p>
      <h1>{_esc(article["title"])}</h1>
      <p class="bri-dek">{_esc(article["dek"])}</p>
      <p class="bri-byline">By <strong>{_esc(article["authorship"]["byline"])}</strong> · {_esc(article["authorship"]["mode"])} · revision <code>{_esc(article["revision_id"])}</code></p>
      <dl class="bri-stats"><div><strong>{coverage["archive_unique_event_count"]}</strong><span>retained regional events</span></div><div><strong>{coverage["event_count"]}</strong><span>current event dossiers</span></div><div><strong>{len(article["editorial"]["evidence"])}</strong><span>editorial evidence records</span></div><div><strong>{coverage["source_count"]}</strong><span>current named sources</span></div></dl>
      <p class="bri-actions"><a href="{_esc(dossier_path)}">Open the regional dossier</a><a href="article.json">Download article JSON</a><a href="/news/">Open the live wire</a></p>
    </header>

    <section class="bri-analysis-layout" id="editorial">
      <div class="bri-analysis-copy">{''.join(editorial_sections)}</div>
      <aside class="bri-analysis-aside"><p class="bri-eyebrow">Editorial evidence receipt</p><p><code>sha256:{_esc(article["editorial"]["source_sha256"])}</code></p><p><b>Evidence states</b><br>Official statistics, official claims, modeled estimates, attributed allegations and documented findings remain distinct.</p><p><b>Assistance disclosure</b><br>{_esc(article["authorship"]["assistance_disclosure"])}.</p></aside>
    </section>

    <section class="bri-section" id="editorial-evidence"><p class="bri-eyebrow">Editorial evidence ledger</p><h2>Every source used by the desk analysis</h2><p>Each record carries its source role, evidence state and interpretation limit. Official claims appear as official claims; allegations remain attributed; modeled values are not presented as observations.</p><div class="bri-evidence-ledger">{''.join(editorial_evidence_rows)}</div></section>

    <section class="bri-section" id="current-window"><p class="bri-eyebrow">Recurring evidence-window update · {_esc(article["generated_at"])}</p><h2>What the latest captured reporting adds</h2><div class="bri-analysis-layout"><div class="bri-analysis-copy">{''.join(claim_sections)}</div><aside class="bri-analysis-aside"><p class="bri-eyebrow">Exact current window</p><p><code>{_esc(article["wire"]["window"]["from"])}</code><br>through<br><code>{_esc(article["wire"]["window"]["to"])}</code></p><p><b>Source structure</b><br>{coverage["single_group_event_count"]} single-group · {coverage["multi_group_event_count"]} multi-group</p><p><b>Cumulative archive</b><br>{coverage["archive_current_event_count"]} current · {coverage["archive_historical_event_count"]} historical.</p></aside></div></section>

    <section class="bri-section" id="topic-shape"><p class="bri-eyebrow">Observed topic shape</p><h2>What publishers placed in the current window</h2><p>These are retained wire labels, not a semantic verdict or a measure of social importance.</p><dl class="bri-contract bri-topic-contract">{topic_rows}</dl></section>

    <section class="bri-section" id="national-context"><p class="bri-eyebrow">Measured context · national grain only</p><h2>Economic and conflict context without a causal shortcut</h2><p>{_esc(article["national_context"]["boundary"])}</p><div class="bri-context-grid">{wdi_rows}{ucdp_panel}</div></section>

    <section class="bri-section" id="evidence"><p class="bri-eyebrow">Current-wire evidence ledger</p><h2>Every current event used by this edition</h2><p>Publisher claims remain attributed. Follow the Palimpsest event route for the bounded dossier or the original publisher link for the source record.</p><div class="bri-evidence-ledger">{''.join(evidence_rows)}</div></section>

    <section class="bri-section bri-limit" id="method"><p class="bri-eyebrow">Method and limits</p><h2>How this recurring article is allowed to say what it says</h2><div class="bri-columns"><div><h3>Method</h3><ol>{method}</ol></div><div><h3>What this cannot establish</h3><ul>{limitations}</ul></div></div><p><b>Disclosure:</b> {_esc(article["disclosure"])}</p><p><b>Content receipt:</b> <code>sha256:{_esc(article["content_sha256"])}</code></p><p><a href="{_esc(article["correction_url"])}">Challenge a source, claim, method, rights decision, or interpretation</a></p></section>
  </article>
</main>
<footer class="bri-footer"><p><strong>Palimpsest regional analysis</strong> · Stable URL, content-addressed revision, visible evidence and limitations.</p></footer>
{site_nav.FOOT}
</body></html>
'''
    return document.encode("utf-8")


def _render_analysis_callout(article: Mapping[str, object]) -> str:
    coverage = article["coverage"]
    return f'''<section class="bri-section bri-analysis-callout" aria-labelledby="analysis-title">
    <p class="bri-eyebrow">Current analytical edition · {_esc(article["generated_at"])}</p>
    <h2 id="analysis-title">{_esc(article["title"])}</h2>
    <p>{_esc(article["dek"])}</p>
    <dl class="bri-contract"><div><dt>Event dossiers</dt><dd>{coverage["event_count"]}</dd></div><div><dt>Named sources</dt><dd>{coverage["source_count"]}</dd></div><div><dt>Independent groups</dt><dd>{coverage["independence_group_count"]}</dd></div></dl>
    <p class="bri-actions"><a href="{_esc(article["url"])}">Read the full analysis</a><a href="{_esc(article["url"])}article.json">Download structured article</a></p>
  </section>'''


def _render_bri_overview_editorial(editorial: Mapping[str, object]) -> str:
    """Render the stable BRI-level editorial and its complete citation ledger."""

    citation_numbers = {
        row["evidence_id"]: position
        for position, row in enumerate(editorial["evidence"], 1)
    }
    sections = []
    for section in editorial["sections"]:
        paragraphs = "".join(
            f"<p>{_esc(paragraph)}</p>" for paragraph in section["paragraphs"]
        )
        citations = " ".join(
            f'<a href="#bri-editorial-evidence-{_esc(evidence_id)}">'
            f'[{citation_numbers[evidence_id]}]</a>'
            for evidence_id in section["evidence_ids"]
        )
        sections.append(
            '<section class="bri-analysis-section bri-editorial-section">'
            f'<h3>{_esc(section["heading"])}</h3>{paragraphs}'
            f'<p class="bri-citations">{citations}</p></section>'
        )
    evidence_rows = []
    for row in editorial["evidence"]:
        position = citation_numbers[row["evidence_id"]]
        evidence_rows.append(
            f'<article id="bri-editorial-evidence-{_esc(row["evidence_id"])}" '
            'class="bri-evidence-row bri-editorial-evidence-row">'
            f'<p class="bri-eyebrow">[{position}] {_esc(row["evidence_state"].replace("-", " "))} · '
            f'<time datetime="{_esc(row["published_at"])}">{_esc(row["published_at"])}</time></p>'
            f'<h3><a href="{_esc(row["url"])}" target="_blank" rel="noopener">'
            f'{_esc(row["source_title"])}</a></h3>'
            f'<p><b>{_esc(row["source_name"])}:</b> {_esc(row["statement"])}</p>'
            f'<p><b>Interpretation limit:</b> {_esc(row["interpretation_limit"])}</p>'
            '</article>'
        )
    return f'''<section class="bri-section bri-overview-editorial" id="bri-editorial" aria-labelledby="bri-editorial-title">
    <p class="bri-eyebrow">Palimpsest Evidence Desk editorial · source record as of {_esc(editorial["as_of"])}</p>
    <h2 id="bri-editorial-title">{_esc(editorial["title"])}</h2>
    <p>{_esc(editorial["dek"])}</p>
    <div class="bri-analysis-layout"><div class="bri-analysis-copy">{''.join(sections)}</div><aside class="bri-analysis-aside"><p class="bri-eyebrow">Balance-sheet rule</p><p>Separate trade, investment, lending, construction revenue, project utilization, host-country fiscal cost and local welfare.</p><p><b>Evidence receipt</b><br><code>sha256:{_esc(editorial["source_sha256"])}</code></p><p><a href="/belt-and-road/data/regional-data.json">Download the complete BRI data dump</a></p></aside></div>
    <details class="bri-editorial-ledger"><summary>Open all {len(evidence_rows)} cited evidence records</summary><div class="bri-evidence-ledger">{''.join(evidence_rows)}</div></details>
  </section>'''


def _translation_markup(translation: Mapping[str, object]) -> str:
    notes = translation.get("translation_notes_en")
    notes_markup = f'<p><b>Translation note:</b> {_esc(notes)}</p>' if notes else ""
    return (
        '<section class="bri-translation" aria-label="English translation receipt">'
        '<p class="bri-eyebrow">English translation of captured Chinese metadata</p>'
        f'<p>{_esc(translation["context_en"])}</p>{notes_markup}'
        '<aside class="bri-translation-context"><b>Background / why this matters:</b> '
        f'{_esc(translation["background_en"])} <small>This is Palimpsest context, not '
        'part of the publisher translation.</small></aside>'
        '<details><summary>Original Chinese and translation receipt</summary>'
        f'<div lang="zh"><strong>{_esc(translation["original_title_zh"])}</strong>'
        f'<p>{_esc(translation["original_context_zh"])}</p></div>'
        f'<p><a href="{_esc(translation["public_url"])}">Open the English translation desk record</a> · '
        f'<code>{_esc(translation["translation_id"])}</code> · '
        f'<code>{_esc(translation["status"])}</code> · '
        f'{_esc(translation["provider"])} / {_esc(translation["model_id"])}</p></details>'
        '</section>'
    )


def _regional_event_card(
    event: Mapping[str, object],
    english_translation: Mapping[str, object] | None = None,
) -> str:
    seen_sources: set[tuple[str, str]] = set()
    source_links = []
    for reference in event["evidence_refs"]:
        key = (reference["source_name"], reference["url"])
        if key in seen_sources:
            continue
        seen_sources.add(key)
        source_links.append(
            f'<a href="{_esc(reference["url"])}" target="_blank" '
            f'rel="noopener">{_esc(reference["source_name"])}</a>'
        )
    source_line = "; ".join(source_links)
    group_count = len(event["evidence_groups"])
    headline = event["headline"]
    dek = event["dek"]
    translation_markup = ""
    if english_translation is not None:
        headline = english_translation["title_en"]
        dek = english_translation["context_en"] or dek
        translation_markup = _translation_markup(english_translation)
    return (
        '<article class="bri-card bri-news-card" data-bri-regional-event>'
        f'<p class="bri-eyebrow"><time datetime="{_esc(event["published_at"])}">'
        f'{_esc(event["published_at"])}</time> · '
        f'{_esc(event["evidence_strength"].replace("-", " "))}</p>'
        f'<h3><a href="{_esc(event["url"])}">{_esc(headline)}</a></h3>'
        f'<p>{_esc(dek)}</p>{translation_markup}'
        f'<p><b>Attributed sources:</b> {source_line}. '
        f'<b>Independent evidence groups:</b> {group_count}.</p>'
        f'<ul class="bri-chips">{_chips(event["topics"])}</ul>'
        '</article>'
    )


def _render_regional_news_section(
    wire: Mapping[str, object],
    *,
    region: str,
    title: str,
    introduction: str,
    dedicated_path: str,
    limit: int = 16,
    captured_index: Mapping[str, object] | None = None,
) -> str:
    events = _regional_events(wire, region)
    visible = events[:limit]
    translations = {
        event["event_id"]: event["english_translation"]
        for event in (captured_index["events"] if captured_index is not None else [])
        if event["english_translation"] is not None
    }
    cards = "".join(
        _regional_event_card(event, translations.get(event["event_id"]))
        for event in visible
    )
    if not cards:
        cards = (
            '<p class="bri-empty">No event metadata fell inside the current '
            'seven-day wire window. This is an observed zero for the feed window, '
            'not evidence that nothing happened.</p>'
        )
    return f'''<section class="bri-section bri-regional-news" id="{_esc(region)}-news" aria-labelledby="{_esc(region)}-news-title">
    <p class="bri-eyebrow">Current regional wire · {_esc(wire["generated_at"])}</p>
    <h2 id="{_esc(region)}-news-title">{_esc(title)}</h2>
    <p>{_esc(introduction)} Titles, links, timestamps and bounded feed excerpts are retained; article bodies are neither fetched nor republished.</p>
    <dl class="bri-contract"><div><dt>Current events</dt><dd>{len(events)}</dd></div><div><dt>Visible here</dt><dd>{len(visible)}</dd></div><div><dt>Evidence rule</dt><dd>attributed reports, not automatic findings</dd></div></dl>
    <div class="bri-grid">{cards}</div>
    <p class="bri-actions"><a href="{_esc(dedicated_path)}">Open the complete regional dossier</a><a href="{_esc(_REGIONAL_ANALYSIS[region]["canonical_path"])}">Read the current analysis</a><a href="/news/">Open the complete live wire</a></p>
  </section>'''


def _regional_data_href(region: str, filename: str) -> str:
    prefix = "/belt-and-road/data" if region == "bri" else f"/belt-and-road/{region}/data"
    return f"{prefix}/{filename}"


def _captured_archive_card(event: Mapping[str, object]) -> str:
    translation = event.get("english_translation")
    display_headline = translation["title_en"] if translation else event["headline"]
    headline = _esc(display_headline)
    if event["event_page_available"]:
        headline = f'<a href="/news/wire/{_esc(event["event_id"])}/">{headline}</a>'
    source_names = "; ".join(_esc(name) for name in event["source_names"])
    status = "current wire" if event["current_in_wire"] else "historical capture"
    display_dek = (
        translation["context_en"]
        if translation is not None and translation["context_en"]
        else event["dek"]
    )
    excerpt = f'<p>{_esc(display_dek)}</p>' if display_dek is not None else ""
    translation_markup = _translation_markup(translation) if translation else ""
    publisher_links = ""
    if event["publisher_links"]:
        links = "; ".join(
            f'<a href="{_esc(link["url"])}" target="_blank" rel="noopener">'
            f'{_esc(link["title"])}</a>'
            for link in event["publisher_links"]
        )
        publisher_links = f"<p><b>Publisher record:</b> {links}.</p>"
    return (
        '<article class="bri-card bri-captured-card" data-bri-captured-event>'
        f'<p class="bri-eyebrow"><time datetime="{_esc(event["published_at"])}">'
        f'{_esc(event["published_at"])}</time> · {_esc(status)}</p>'
        f"<h3>{headline}</h3>{excerpt}{translation_markup}"
        f'<p><b>Attributed sources:</b> {source_names}. '
        f'<b>Evidence label:</b> {_esc(event["evidence_strength"].replace("-", " "))}. '
        f'<b>Retained versions:</b> {_esc(event["version_count"])}.</p>'
        f"{publisher_links}"
        f'<p><small>First captured <code>{_esc(event["first_recorded_at"])}</code>; '
        f'last retained <code>{_esc(event["last_recorded_at"])}</code>. '
        'Bodies are not republished.</small></p>'
        "</article>"
    )


def _render_captured_archive_section(
    index: Mapping[str, object],
    *,
    title: str,
    introduction: str,
) -> str:
    counts = index["counts"]
    coverage = index["coverage"]
    cards = "".join(_captured_archive_card(event) for event in index["events"])
    if not cards:
        cards = (
            '<p class="bri-empty">The retained capture ledger has no matching '
            "event metadata for this lane. That is a coverage gap, not evidence "
            "that nothing happened.</p>"
        )
    region = index["region"]
    return f'''<section class="bri-section bri-captured-archive" id="{_esc(region)}-captured-archive" aria-labelledby="{_esc(region)}-captured-title">
    <p class="bri-eyebrow">Complete retained capture archive · {_esc(index["generated_at"])}</p>
    <h2 id="{_esc(region)}-captured-title">{_esc(title)}</h2>
    <p>{_esc(introduction)} This archive is cumulative: a record remains here after it leaves the seven-day current wire. The same record can appear in more than one regional lane when its metadata genuinely overlaps.</p>
    <dl class="bri-contract"><div><dt>Unique captured events</dt><dd>{counts["unique_events"]}</dd></div><div><dt>Retained event versions</dt><dd>{counts["event_versions"]}</dd></div><div><dt>Current / historical</dt><dd>{counts["current_events"]} / {counts["historical_events"]}</dd></div><div><dt>Named sources</dt><dd>{counts["sources"]}</dd></div><div><dt>Published range</dt><dd><code>{_esc(coverage["earliest_published_at"] or "unavailable")}</code><br>to <code>{_esc(coverage["latest_published_at"] or "unavailable")}</code></dd></div></dl>
    <p class="bri-actions"><a href="{_esc(_regional_data_href(region, "captured-index.json"))}">Complete JSON index</a><a href="{_esc(_regional_data_href(region, "captured-index.jsonl"))}">JSONL dump</a><a href="{_esc(_regional_data_href(region, "captured-index.csv"))}">CSV dump</a><a href="{_esc(_regional_data_href(region, "regional-data.json"))}">Full regional data dump</a></p>
    <div class="bri-grid bri-captured-grid">{cards}</div>
    <p class="bri-limit"><b>Boundary:</b> every matching event retained by Palimpsest is listed, but this is not every article ever published on the internet. Publisher bodies, restricted records, person-level data and tactical detail are excluded; listings remain attributed reports rather than findings.</p>
  </section>'''


def _chips(values: list[str]) -> str:
    return "".join(f'<li><code>{_esc(value)}</code></li>' for value in values)


_IMPLEMENTATION_ORDER = (
    "live", "repository_ready", "adapter_ready", "link_only", "planned",
    "blocked", "out_of_scope",
)


def _state_summary(counts: Counter[str], *, preferred: tuple[str, ...] = ()) -> str:
    ordered = [key for key in preferred if counts.get(key)]
    ordered.extend(sorted(key for key in counts if key not in ordered))
    return " · ".join(
        f'{counts[key]} {_esc(key.replace("_", " "))}' for key in ordered
    )


def _target_projection(
    artifact: dict,
    *,
    target_ids: tuple[str, ...],
    card_class: str = "bri-card",
) -> dict:
    """Project exact target/source readiness into escaped human-readable cards."""

    targets_by_id = {row["target_id"]: row for row in artifact["watch_targets"]}
    sources_by_id = {row["source_id"]: row for row in artifact["sources"]}
    missing_targets = [
        target_id for target_id in target_ids if target_id not in targets_by_id
    ]
    if missing_targets:
        raise ValueError(f"regional projection is missing targets {missing_targets}")

    targets = [targets_by_id[target_id] for target_id in target_ids]
    target_source_ids = list(dict.fromkeys(
        source_id
        for target in targets
        for source_id in target["source_ids"]
    ))
    missing_sources = [
        source_id for source_id in target_source_ids if source_id not in sources_by_id
    ]
    if missing_sources:
        raise ValueError(f"regional projection is missing sources {missing_sources}")
    target_sources = [sources_by_id[source_id] for source_id in target_source_ids]
    implementation_counts = Counter(
        source["implementation"] for source in target_sources
    )
    rights_counts = Counter(source["rights_status"] for source in target_sources)
    target_status_counts = Counter(target["evidence_status"] for target in targets)
    build_ready_count = sum(
        count
        for state, count in implementation_counts.items()
        if state in PUBLIC_BUILD_STATES
    )

    cards = []
    for target in targets:
        target_sources_for_card = [
            sources_by_id[source_id] for source_id in target["source_ids"]
        ]
        card_states = Counter(
            source["implementation"] for source in target_sources_for_card
        )
        card_rights = Counter(
            source["rights_status"] for source in target_sources_for_card
        )
        source_routes = "; ".join(
            f'<a href="{_esc(source["url"])}" target="_blank" rel="noopener">'
            f'{_esc(source["name"])}</a>'
            for source in target_sources_for_card
        )
        cards.append(
            f'<article class="{_esc(card_class)}" '
            f'data-bri-region-target="{_esc(target["target_id"])}">'
            f'<p class="bri-eyebrow">{_esc(target["evidence_status"].replace("_", " "))}</p>'
            f'<h3>{_esc(target["label"])}</h3>'
            f'<p><b>Readiness:</b> {_state_summary(card_states, preferred=_IMPLEMENTATION_ORDER)}. '
            f'<b>Rights:</b> {_state_summary(card_rights)}.</p>'
            f'<p><b>Registered source routes:</b> {source_routes}.</p>'
            f'<ul class="bri-chips">{_chips(target["required_coverage"])}</ul>'
            '</article>'
        )

    return {
        "build_ready_count": build_ready_count,
        "cards": "".join(cards),
        "implementation_counts": implementation_counts,
        "rights_counts": rights_counts,
        "source_count": len(target_sources),
        "target_count": len(targets),
        "target_status_counts": target_status_counts,
    }


def _render_region_section(
    artifact: dict,
    *,
    anchor: str,
    eyebrow: str,
    title: str,
    introduction: str,
    geography_codes: tuple[str, ...],
    target_ids: tuple[str, ...],
) -> str:
    """Render a regional reading lane from the public artifact only.

    A missing target, source or geography is a build failure. Quietly dropping
    one would make the human page less complete than its machine contract.
    """

    geography_report = artifact["coverage_report"]["geographies"]
    missing_geographies = [
        code for code in geography_codes if code not in geography_report
    ]
    if missing_geographies:
        raise ValueError(
            f"regional section {anchor} is missing geographies {missing_geographies}"
        )
    projection = _target_projection(artifact, target_ids=target_ids)
    geography_source_ids = {
        source_id
        for code in geography_codes
        for source_id in geography_report[code]["source_ids"]
    }

    return f"""<section class="bri-section" id="{_esc(anchor)}" aria-labelledby="{_esc(anchor)}-title">
    <p class="bri-eyebrow">{_esc(eyebrow)}</p><h2 id="{_esc(anchor)}-title">{_esc(title)}</h2>
    <p>{_esc(introduction)} The broader registry associates {len(geography_source_ids)} source families with this geography; the {projection["target_count"]} target ledgers below name {projection["source_count"]} distinct source routes directly.</p>
    <dl class="bri-contract"><div><dt>Target status</dt><dd>{_state_summary(projection["target_status_counts"])}</dd></div><div><dt>Source readiness</dt><dd>{_state_summary(projection["implementation_counts"], preferred=_IMPLEMENTATION_ORDER)}</dd></div><div><dt>Public build-state gate</dt><dd>{projection["build_ready_count"]} of {projection["source_count"]} named routes</dd></div></dl>
    <p><strong>Rights states:</strong> {_state_summary(projection["rights_counts"])}. Build-ready describes a source route, not a verified project record. Link-only and planned routes are discovery, not ingestion; no status here proves construction, operation, attribution, local benefit or harm.</p>
    <div class="bri-grid">{projection["cards"]}</div>
  </section>"""


def _render_gwadar_html(
    artifact: dict,
    wire: Mapping[str, object] | None = None,
    analysis: Mapping[str, object] | None = None,
    captured_index: Mapping[str, object] | None = None,
) -> bytes:
    """Render the durable Gwadar route from the public BRI artifact only."""

    wire = _load_newswire() if wire is None else wire
    if captured_index is None:
        captured_path = _REGIONAL_OUTPUT_ROOTS["gwadar"] / "captured-index.json"
        if captured_path.is_file():
            captured_index = newswire_model.strict_json_loads(
                captured_path.read_bytes(), label=str(captured_path)
            )

    region = _render_region_section(
        artifact,
        anchor="pakistan-gwadar",
        eyebrow="Pakistan and Gwadar · target-specific readiness",
        title="CPEC, port, connectivity and public-service records stay claim by claim.",
        introduction=(
            "This lane keeps national finance context, provincial records and "
            "Gwadar-specific project sources at their published geographic grain."
        ),
        geography_codes=_PAKISTAN_GWADAR_GEOGRAPHIES,
        target_ids=_PAKISTAN_GWADAR_TARGETS,
    )
    as_of = _esc(artifact["as_of"])
    current_news = _render_regional_news_section(
        wire,
        region="gwadar",
        title="Current CPEC and Gwadar reporting",
        introduction=(
            "This lane keeps official-position reporting, local reporting and "
            "independent media records attributed to their publishers."
        ),
        dedicated_path="/belt-and-road/gwadar/",
        limit=24,
        captured_index=captured_index,
    )
    analysis_callout = _render_analysis_callout(analysis) if analysis else ""
    captured_archive = (
        _render_captured_archive_section(
            captured_index,
            title="Every retained CPEC and Gwadar publisher record",
            introduction=(
                "The archive combines the complete captured history for dedicated "
                "CPEC/Gwadar feeds with bounded matches from broader publishers."
            ),
        )
        if captured_index is not None
        else ""
    )
    document = f"""<!doctype html>
<html lang="en" data-tk-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gwadar and CPEC evidence · Palimpsest</title>
<meta name="description" content="A provenance-first Gwadar and CPEC source-readiness dossier covering the port, free zone, connectivity, public services and Balochistan political economy.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://palimpsest.info/belt-and-road/gwadar/">
<link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg">
<meta name="theme-color" content="#edf2ec">
{site_nav.HEAD}
<link rel="stylesheet" href="/assets/bri.css">
</head>
<body class="ps tk bri-page">
<!-- GENERATED BY scripts/build_bri_observatory.py -->
{site_nav.render('/belt-and-road/gwadar/')}
<main id="main">
  <header class="bri-hero">
    <p class="bri-eyebrow">Gwadar evidence dossier · artifact as of {as_of}</p>
    <h1>Gwadar claims, source routes and evidence gaps in one durable place.</h1>
    <p class="bri-dek">This page projects the public Belt and Road artifact at its recorded geographic grain. A registered route is not presented as an ingested fact, and unavailable evidence is not converted into a zero.</p>
    <p class="bri-actions"><a href="/belt-and-road/">Open the complete BRI observatory</a><a href="/readings/belt-and-road-observatory-latest.json">Download the machine contract</a><a href="/news/">Read live news</a></p>
  </header>

  {analysis_callout}

  {current_news}

  {captured_archive}

  {region}
</main>
<footer class="bri-footer"><p><strong>Palimpsest Gwadar dossier</strong> · Project status, finance, logistics, employment, land, livelihood and environment remain separate evidence fields.</p><p><a href="/belt-and-road/">BRI observatory</a> · <a href="/readings/belt-and-road-observatory-latest.json">JSON</a> · <a href="/challenge.html">Challenge a source or method</a></p></footer>
{site_nav.FOOT}
</body>
</html>
"""
    return document.encode("utf-8")


def _render_regional_dossier_html(
    artifact: dict,
    wire: Mapping[str, object],
    analysis: Mapping[str, object],
    captured_index: Mapping[str, object],
    *,
    region: str,
    canonical_path: str,
    label: str,
    headline: str,
    introduction: str,
    geography_codes: tuple[str, ...],
    target_ids: tuple[str, ...],
) -> bytes:
    readiness = _render_region_section(
        artifact,
        anchor=f"{region}-readiness",
        eyebrow=f"{label} · exact source readiness",
        title="What is live, linked, pending, or unavailable",
        introduction=introduction,
        geography_codes=geography_codes,
        target_ids=target_ids,
    )
    current_news = _render_regional_news_section(
        wire,
        region=region,
        title=f"Current {label} reporting",
        introduction=introduction,
        dedicated_path=canonical_path,
        limit=48,
        captured_index=captured_index,
    )
    analysis_callout = _render_analysis_callout(analysis)
    captured_archive = _render_captured_archive_section(
        captured_index,
        title=f"Every retained {label} publisher record",
        introduction=(
            "The historical ledger keeps admitted publisher metadata visible after "
            "it leaves the current rolling window."
        ),
    )
    document = f'''<!doctype html>
<html lang="en" data-tk-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(label)} evidence · Palimpsest</title>
<meta name="description" content="A current, provenance-first {_esc(label)} evidence dossier with attributed regional reporting and exact source readiness.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://palimpsest.info{_esc(canonical_path)}">
<link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg">
<meta name="theme-color" content="#edf2ec">
{site_nav.HEAD}
<link rel="stylesheet" href="/assets/bri.css">
</head>
<body class="ps tk bri-page">
<!-- GENERATED BY scripts/build_bri_observatory.py -->
{site_nav.render(canonical_path)}
<main id="main">
  <header class="bri-hero">
    <p class="bri-eyebrow">{_esc(label)} evidence dossier · artifact as of {_esc(artifact["as_of"])}</p>
    <h1>{_esc(headline)}</h1>
    <p class="bri-dek">{_esc(introduction)} Claims remain attributed; source agreement, legal findings and causal responsibility are not inferred from repetition.</p>
    <p class="bri-actions"><a href="/belt-and-road/">Open the complete BRI observatory</a><a href="/news/">Open live news</a></p>
  </header>

  {analysis_callout}

  {current_news}

  {captured_archive}

  {readiness}
</main>
<footer class="bri-footer"><p><strong>Palimpsest { _esc(label) } dossier</strong> · Current public evidence, exact clocks and visible limitations.</p><p><a href="/belt-and-road/">BRI observatory</a> · <a href="/challenge.html">Challenge a source or method</a></p></footer>
{site_nav.FOOT}
</body>
</html>
'''
    return document.encode("utf-8")


def _render_observation_datasets(artifact: dict) -> str:
    rows = artifact.get("observation_datasets")
    if not rows:
        return ""
    [dataset] = rows
    coverage = dataset["coverage"]
    rights = dataset["rights"]
    boundary = dataset["context_boundary"]
    receipt = dataset["publication_receipt"]
    if receipt is None:
        publication_proof = ""
    else:
        publication_proof = (
            '    <p><strong>Point-in-time publication proof:</strong> '
            f'<a href="{_esc(receipt["public_url"])}">Inspect the immutable receipt</a>. '
            f'Exact served bytes were verified at <code>{_esc(receipt["verified_at"])}</code>; '
            "the release-verification freshness window ends at "
            f'<code>{_esc(receipt["fresh_until"])}</code> and may already have ended. '
            "This is release-time proof, not continuous monitoring "
            f'(<code>{_esc(receipt["availability_semantics"])}</code>).</p>\n'
        )
    return f"""<section class="bri-section" id="economic-context" aria-labelledby="economic-context-title">
    <p class="bri-eyebrow">Normalized economic context · {_esc(dataset["publication_state"].replace("_", " "))}</p><h2 id="economic-context-title">Country-period context with forecasts, nulls and source clocks intact</h2>
    <p>This exact World Bank WDI bundle contains {_esc(coverage["source_rows"])} country-series-year rows across China, Myanmar and Pakistan. It keeps {_esc(coverage["forecast_rows"])} source-marked forecasts separate from {_esc(coverage["observed_rows"])} observed values and retains {_esc(coverage["unavailable_rows"])} unavailable rows as null—not zero.</p>
    <dl class="bri-contract"><div><dt>Coverage</dt><dd>{_esc(coverage["start_year"])}–{_esc(coverage["end_year"])} · {_esc(coverage["indicators"])} indicators</dd></div><div><dt>Knowledge time</dt><dd><code>{_esc(dataset["clocks"]["retrieved_at"])}</code></dd></div><div><dt>Role</dt><dd>{_esc(boundary["allowed_role"])} only · {_esc(boundary["join_scope"].replace("_", " "))}</dd></div><div><dt>Rights</dt><dd>{_esc(rights["license"])} · {_esc(rights["attribution"])}</dd></div></dl>
{publication_proof}    <p>No row may infer a project, actor, corridor or causal effect. <a href="/{_esc(dataset["artifact"]["path"])}">Inspect the normalized bundle</a> · <a href="/{_esc(dataset["observation_schema"]["path"])}">Validate its schema</a></p>
  </section>"""


def _render_html(
    artifact: dict,
    wire: Mapping[str, object] | None = None,
    captured_index: Mapping[str, object] | None = None,
    bri_editorial: Mapping[str, object] | None = None,
) -> bytes:
    wire = _load_newswire() if wire is None else wire
    report = artifact["coverage_report"]
    states = report["implementation_states"]
    workstreams = "".join(
        (
            '<article class="bri-card">'
            f'<p class="bri-eyebrow">{_esc(item["status"].replace("_", " "))}</p>'
            f'<h3>{_esc(item["label"])}</h3>'
            f'<p>{len(item["source_ids"])} source families · '
            f'{len(item["required_coverage"])} required evidence dimensions</p>'
            f'<ul class="bri-chips">{_chips(item["required_coverage"])}</ul>'
            '</article>'
        )
        for item in artifact["workstreams"]
    )
    targets = "".join(
        (
            f'<li data-bri-record data-bri-text="{_esc((item["label"] + " " + item["target_type"]).lower())}">'
            f'<div><span>{_esc(item["target_type"].replace("_", " "))}</span>'
            f'<strong>{_esc(item["label"])}</strong></div>'
            f'<code>{_esc(item["evidence_status"])}</code>'
            f'<p>{_esc(", ".join(item["required_coverage"]))}</p></li>'
        )
        for item in artifact["watch_targets"]
    )
    lanes = "".join(
        (
            '<article class="bri-lane">'
            f'<p class="bri-eyebrow">{_esc(lane["lane_id"].replace("_", " "))}</p>'
            f'<h3>{_esc(lane["label"])}</h3>'
            f'<p><b>Entities:</b> {_esc(", ".join(lane["entity_types"]))}</p>'
            f'<p><b>Never merge with:</b> {_esc(", ".join(lane["prohibited_merges"]))}</p>'
            '</article>'
        )
        for lane in artifact["movement_taxonomy"]["lanes"]
    )
    sources = "".join(
        (
            f'<li class="bri-source" data-bri-source data-state="{_esc(source["implementation"])}" '
            f'data-class="{_esc(source["source_class"])}" '
            f'data-bri-text="{_esc((source["source_id"] + " " + source["name"] + " " + source["publisher"] + " " + " ".join(source["coverage"])).lower())}">'
            '<div class="bri-source__head">'
            f'<a href="{_esc(source["url"])}" target="_blank" rel="noopener">{_esc(source["name"])}</a>'
            f'<span class="bri-state bri-state--{_esc(source["implementation"].replace("_", "-"))}">{_esc(source["implementation"].replace("_", " "))}</span>'
            '</div>'
            f'<p>{_esc(source["publisher"])} · {_esc(source["authority_role"].replace("_", " "))}</p>'
            f'<p>{_esc(", ".join(source["coverage"]))}</p>'
            f'<small>Rights: {_esc(source["rights_status"].replace("_", " "))}. {_esc(source["notes"])}</small>'
            '</li>'
        )
        for source in artifact["sources"]
    )
    balochistan_targets = _target_projection(
        artifact,
        target_ids=(
            "balochistan_resources_revenue",
            "balochistan_movement_history",
        ),
        card_class="bri-lane",
    )
    pakistan_gwadar = _render_region_section(
        artifact,
        anchor="pakistan-gwadar",
        eyebrow="Pakistan and Gwadar · target-specific readiness",
        title="CPEC, port, connectivity and public-service records stay claim by claim.",
        introduction=(
            "This lane keeps national finance context, provincial records and "
            "Gwadar-specific project sources at their published geographic grain."
        ),
        geography_codes=_PAKISTAN_GWADAR_GEOGRAPHIES,
        target_ids=_PAKISTAN_GWADAR_TARGETS,
    )
    myanmar = _render_region_section(
        artifact,
        anchor="myanmar",
        eyebrow="Myanmar · target-specific readiness",
        title="CMEC, Kyaukpyu, pipelines and rail remain separate evidence records.",
        introduction=(
            "This lane preserves the difference between official proposals, "
            "investment approvals, economic context and observed implementation."
        ),
        geography_codes=("MMR", "MMR-RKH"),
        target_ids=(
            "cmec_portfolio",
            "kyaukpyu_port_sez",
            "china_myanmar_pipelines",
            "mandalay_muse_rail",
        ),
    )
    regional_news = "\n\n".join(
        (
            _render_regional_news_section(
                wire,
                region="gwadar",
                title="Current BRI, CPEC and Gwadar reporting",
                introduction=(
                    "Current feed metadata is separated from the project-readiness "
                    "ledger below."
                ),
                dedicated_path="/belt-and-road/gwadar/",
                captured_index=captured_index,
            ),
            _render_regional_news_section(
                wire,
                region="balochistan",
                title="Current Balochistan rights, civic and political reporting",
                introduction=(
                    "Documentation-group allegations, state positions and media "
                    "reports remain visibly distinct."
                ),
                dedicated_path="/belt-and-road/balochistan/",
                captured_index=captured_index,
            ),
            _render_regional_news_section(
                wire,
                region="myanmar",
                title="Current Myanmar, Rakhine, Kachin and Shan reporting",
                introduction=(
                    "The stream preserves publisher attribution across national "
                    "and regional Myanmar reporting."
                ),
                dedicated_path="/belt-and-road/myanmar/",
                captured_index=captured_index,
            ),
        )
    )
    captured_archive = (
        _render_captured_archive_section(
            captured_index,
            title="Every retained BRI, CPEC, Balochistan and Myanmar publisher record",
            introduction=(
                "This combined index exposes the complete retained regional capture "
                "universe, including overlap across the three dedicated dossiers."
            ),
        )
        if captured_index is not None
        else ""
    )
    overview_editorial = (
        _render_bri_overview_editorial(bri_editorial)
        if bri_editorial is not None
        else ""
    )
    schema_org_document = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": "Palimpsest Belt and Road Observatory coverage contract",
        "description": artifact["scope"],
        "url": "https://palimpsest.info/belt-and-road/",
        "dateModified": artifact["as_of"],
        "license": "https://github.com/beepboop2025/palimpsest/blob/main/LICENSE",
        "distribution": {
            "@type": "DataDownload",
            "encodingFormat": "application/json",
            "contentUrl": "https://palimpsest.info/readings/belt-and-road-observatory-latest.json",
        },
    }
    observation_datasets = artifact.get("observation_datasets", [])
    if observation_datasets:
        [dataset] = observation_datasets
        schema_org_document["hasPart"] = {
            "@type": "Dataset",
            "name": "BRI-country World Development Indicators context",
            "description": (
                "Attributed national country-period context for China, Myanmar "
                "and Pakistan; never project, actor, corridor or causal evidence."
            ),
            "license": dataset["rights"]["license_url"],
            "creator": {
                "@type": "Organization",
                "@id": "https://www.worldbank.org/#organization",
                "name": "World Bank",
                "url": "https://www.worldbank.org/",
            },
            "distribution": {
                "@type": "DataDownload",
                "encodingFormat": dataset["artifact"]["media_type"],
                "contentUrl": dataset["artifact"]["url"],
            },
        }
    schema_org = json.dumps(
        schema_org_document,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    schema_path = artifact["$schema"]
    document = f"""<!doctype html>
<html lang="en" data-tk-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Belt and Road Observatory · Palimpsest</title>
<meta name="description" content="A provenance-first global Belt and Road infrastructure and economics source ledger, with deep Pakistan, Gwadar, Balochistan, Myanmar and Kyaukpyu coverage.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://palimpsest.info/belt-and-road/">
<link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg">
<meta name="theme-color" content="#edf2ec">
<script type="application/ld+json">{schema_org}</script>
{site_nav.HEAD}
<link rel="stylesheet" href="/assets/bri.css">
</head>
<body class="ps tk bri-page">
<!-- GENERATED BY scripts/build_bri_observatory.py -->
{site_nav.render('/belt-and-road/')}
<main id="main">
  <header class="bri-hero">
    <p class="bri-eyebrow">Global BRI evidence backbone · first deep dossiers: Pakistan and Myanmar</p>
    <h1>Every project claim should show what it knows—and what it does not.</h1>
    <p class="bri-dek">{_esc(artifact["scope"])}</p>
    <div class="bri-release"><strong>Evidence coverage contract</strong><span>Publication is not a claim that every registered source has been ingested.</span></div>
    <div class="bri-stats" aria-label="Current registry coverage">
      <div><strong>{report["source_count"]}</strong><span>source families</span></div>
      <div><strong>{report["build_ready_source_count"]}</strong><span>build-ready inputs</span></div>
      <div><strong>{len(artifact["watch_targets"])}</strong><span>priority target ledgers</span></div>
      <div><strong>{len(report["build_ready_gaps"])}</strong><span>build-ready gaps kept visible</span></div>
    </div>
    <p class="bri-actions"><a href="/readings/belt-and-road-observatory-latest.json">Download the machine contract</a><a href="#sources">Inspect the source ledger</a><a href="#balochistan">Read the Balochistan boundary</a></p>
  </header>

  <section class="bri-section" id="bri-corridors" aria-labelledby="meaning-title">
    <p class="bri-eyebrow">The counting rule</p><h2 id="meaning-title">An announcement is not a disbursement. Completion is not operation.</h2>
    <p>Palimpsest records announcement, approval, contract, commitment, disbursement, construction, completion and operation separately. It preserves currency, price basis, sovereign guarantees, revisions and source clocks before any total is calculated.</p>
    <div class="bri-columns"><div><h3>Economic depth</h3><ul class="bri-chips">{_chips(artifact["economic_metrics"])}</ul></div><div><h3>Ground-level outcomes</h3><ul class="bri-chips">{_chips(artifact["local_impact_fields"])}</ul></div></div>
  </section>

  <section class="bri-section" aria-labelledby="workstreams-title">
    <p class="bri-eyebrow">One backbone, several evidence lanes</p><h2 id="workstreams-title">Coverage that can grow without flattening unlike facts</h2>
    <div class="bri-grid">{workstreams}</div>
  </section>

  <section class="bri-section" aria-labelledby="targets-title">
    <p class="bri-eyebrow">Priority ledger</p><h2 id="targets-title">From the global BRI universe to Gwadar and Kyaukpyu</h2>
    <label class="bri-search">Filter targets<input type="search" data-bri-target-search placeholder="Try Gwadar, rail, resources, movement…"></label>
    <ul class="bri-targets" data-bri-targets>{targets}</ul>
  </section>

  {regional_news}

  {captured_archive}

  {overview_editorial}

  <section class="bri-section bri-dark" id="balochistan" aria-labelledby="balochistan-title">
    <p class="bri-eyebrow">Balochistan: plural record, never one label</p><h2 id="balochistan-title">The umbrella term is a research concept, not a database actor.</h2>
    <p>{_esc(artifact["movement_taxonomy"]["identity_rule"])}</p>
    <div class="bri-lanes">{lanes}</div>
    <div class="bri-region-evidence" aria-labelledby="balochistan-targets-title">
      <p class="bri-eyebrow">Artifact targets · exact source readiness</p><h3 id="balochistan-targets-title">Resources and revenue are not movement affiliation; movement history is not one actor.</h3>
      <p>The machine contract exposes {balochistan_targets["target_count"]} separate Balochistan target ledgers backed by {balochistan_targets["source_count"]} distinct named source routes. These are scope and readiness records, not classifications of a person, community or political position.</p>
      <p><b>Target status:</b> {_state_summary(balochistan_targets["target_status_counts"])}. <b>Source readiness:</b> {_state_summary(balochistan_targets["implementation_counts"], preferred=_IMPLEMENTATION_ORDER)}. <b>Rights:</b> {_state_summary(balochistan_targets["rights_counts"])}. <b>Public build-state gate:</b> {balochistan_targets["build_ready_count"]} of {balochistan_targets["source_count"]} named routes.</p>
      <div class="bri-lanes">{balochistan_targets["cards"]}</div>
      <p>Link-only and planned routes are discovery, not ingestion. Build-ready describes a source route, not a verified project record or actor record. The resources/revenue target cannot infer affiliation; the plural movement-history target cannot merge electoral politics, peaceful civic advocacy, armed organizations, state actions, legal designations, rights reporting or political economy.</p>
    </div>
  </section>

  {pakistan_gwadar}

  {myanmar}

  <section class="bri-section" aria-labelledby="narco-title">
    <p class="bri-eyebrow">NarcoScope integration</p><h2 id="narco-title">Country context beside the dossier—not a label on it.</h2>
    <p>The additive v2 bridge covers official country aggregates for China, Pakistan and Myanmar. Its only permitted join is geography plus time. It cannot classify a political movement, armed organization, community, person, project, bilateral route or causal chain.</p>
    <dl class="bri-contract"><div><dt>Contract</dt><dd><code>{_esc(artifact["partner_bridges"][0]["contract"])}</code></dd></div><div><dt>Status</dt><dd>{_esc(artifact["partner_bridges"][0]["status"].replace("_", " "))}</dd></div><div><dt>Inference</dt><dd>{_esc(artifact["partner_bridges"][0]["actor_inference"])}</dd></div></dl>
  </section>

  <section class="bri-section" id="sources" aria-labelledby="sources-title">
    <p class="bri-eyebrow">Source and rights ledger</p><h2 id="sources-title">Open, pending, licensed and blocked routes all stay visible.</h2>
    <p>{states.get("live", 0)} sources are live, {states.get("repository_ready", 0)} input is repository ready, {states.get("adapter_ready", 0)} adapters are ready, and {states.get("blocked", 0)} licensed route is blocked from public redistribution. Link-only entries are discovery, not ingestion.</p>
    <div class="bri-controls"><label>Search<input type="search" data-bri-source-search placeholder="Source, publisher, evidence field…"></label><label>Implementation<select data-bri-state><option value="all">All states</option>{''.join(f'<option value="{_esc(state)}">{_esc(state.replace("_", " "))} ({count})</option>' for state, count in states.items())}</select></label><p data-bri-source-count aria-live="polite"></p></div>
    <ul class="bri-sources" data-bri-sources>{sources}</ul>
  </section>

  <section class="bri-section bri-limit" aria-labelledby="limits-title">
    <p class="bri-eyebrow">Publication boundary</p><h2 id="limits-title">Useful for accountability, deliberately useless for targeting.</h2>
    <p>Conflict events publish only after a delay and at administrative-area grain. The observatory carries no person-level dossiers, live tactical coordinates, vulnerability maps or operational guidance. Allegations, government positions, administrative designations, legal status, coded events and adjudicated findings remain distinct.</p>
  </section>
</main>
<footer class="bri-footer"><p><strong>Palimpsest Belt and Road Observatory</strong> · Source rights, lifecycle states, local impacts and corrections before conclusions.</p><p><a href="/readings/belt-and-road-observatory-latest.json">JSON</a> · <a href="{_esc(schema_path)}">Schema</a> · <a href="/challenge.html">Challenge a source or method</a></p></footer>
{site_nav.FOOT}
<script src="/assets/bri.js" defer></script>
</body>
</html>
"""
    economic_context = _render_observation_datasets(artifact)
    if economic_context:
        marker = '  <section class="bri-section" aria-labelledby="narco-title">'
        if document.count(marker) != 1:
            raise ValueError("BRI page economic-context insertion marker changed")
        document = document.replace(marker, f"  {economic_context}\n\n{marker}")
    return document.encode("utf-8")


def build(
    registry_path: Path = DEFAULT_REGISTRY,
    *,
    wdi_bundle_path: Path | None = DEFAULT_WDI_BUNDLE,
    wdi_artifact_path: str = WDI_ARTIFACT_PATH,
    wdi_observation_schema_path: Path = DEFAULT_WDI_SCHEMA,
    wdi_observation_schema_repository_path: str = WDI_OBSERVATION_SCHEMA_PATH,
    wdi_series_registry_path: Path = DEFAULT_WDI_SERIES_REGISTRY,
    wdi_series_registry_repository_path: str = WDI_SERIES_REGISTRY_PATH,
    wdi_publication_receipt_path: Path | None | object = _AUTO_WDI_PUBLICATION_RECEIPT,
    wdi_archived_size_receipt_path: Path | None = None,
    newswire_path: Path = DEFAULT_NEWSWIRE,
    newswire_versions_path: Path = DEFAULT_NEWSWIRE_VERSIONS,
    news_source_registry_path: Path = DEFAULT_NEWS_SOURCE_REGISTRY,
    editorial_evidence_path: Path = DEFAULT_EDITORIAL_EVIDENCE,
    chinese_translations_path: Path = DEFAULT_CHINESE_TRANSLATIONS,
    chinese_translation_schema_path: Path = DEFAULT_CHINESE_TRANSLATION_SCHEMA,
) -> tuple[bytes, bytes]:
    registry = load_registry(registry_path)
    observation_datasets = None
    if wdi_bundle_path is not None:
        source_implementation = next(
            source["implementation"]
            for source in registry["sources"]
            if source["source_id"] == "world_bank_wdi"
        )
        (
            resolved_publication_receipt,
            resolved_archived_size_receipt,
        ) = _resolve_wdi_publication_receipts(
            wdi_publication_receipt_path,
            wdi_archived_size_receipt_path,
            source_implementation=source_implementation,
        )
        observation_datasets = [
            build_wdi_observation_descriptor(
                registry,
                bundle_path=wdi_bundle_path,
                artifact_path=wdi_artifact_path,
                observation_schema_path=wdi_observation_schema_path,
                observation_schema_repository_path=(
                    wdi_observation_schema_repository_path
                ),
                series_registry_path=wdi_series_registry_path,
                series_registry_repository_path=(
                    wdi_series_registry_repository_path
                ),
                publication_receipt_path=resolved_publication_receipt,
                archived_size_receipt_path=resolved_archived_size_receipt,
            )
        ]
    artifact = build_public_artifact(
        registry,
        observation_datasets=observation_datasets,
    )
    wire = _load_newswire(newswire_path)
    versions, versions_sha256 = _load_newswire_versions(newswire_versions_path)
    source_specs, source_registry_sha256 = _source_registry_projection(
        news_source_registry_path
    )
    translations = chinese_translation_pages.load_translations(
        chinese_translations_path,
        schema_path=chinese_translation_schema_path,
    )
    captured_index = _build_captured_index(
        wire,
        versions,
        versions_sha256=versions_sha256,
        source_specs=source_specs,
        source_registry_sha256=source_registry_sha256,
        region="bri",
        translations=translations,
    )
    editorial_evidence, editorial_evidence_sha256 = _load_editorial_evidence(
        editorial_evidence_path
    )
    bri_editorial = _editorial_projection(
        editorial_evidence,
        editorial_evidence_sha256=editorial_evidence_sha256,
        region="bri",
    )
    return _json_bytes(artifact), _render_html(
        artifact,
        wire,
        captured_index,
        bri_editorial,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--html-output", type=Path, default=DEFAULT_HTML)
    parser.add_argument(
        "--gwadar-html-output", type=Path, default=DEFAULT_GWADAR_HTML
    )
    parser.add_argument(
        "--balochistan-html-output", type=Path, default=DEFAULT_BALOCHISTAN_HTML
    )
    parser.add_argument(
        "--myanmar-html-output", type=Path, default=DEFAULT_MYANMAR_HTML
    )
    parser.add_argument(
        "--gwadar-analysis-html-output",
        type=Path,
        default=DEFAULT_GWADAR_ANALYSIS_HTML,
    )
    parser.add_argument(
        "--gwadar-analysis-json-output",
        type=Path,
        default=DEFAULT_GWADAR_ANALYSIS_JSON,
    )
    parser.add_argument(
        "--balochistan-analysis-html-output",
        type=Path,
        default=DEFAULT_BALOCHISTAN_ANALYSIS_HTML,
    )
    parser.add_argument(
        "--balochistan-analysis-json-output",
        type=Path,
        default=DEFAULT_BALOCHISTAN_ANALYSIS_JSON,
    )
    parser.add_argument(
        "--myanmar-analysis-html-output",
        type=Path,
        default=DEFAULT_MYANMAR_ANALYSIS_HTML,
    )
    parser.add_argument(
        "--myanmar-analysis-json-output",
        type=Path,
        default=DEFAULT_MYANMAR_ANALYSIS_JSON,
    )
    parser.add_argument("--newswire", type=Path, default=DEFAULT_NEWSWIRE)
    parser.add_argument(
        "--newswire-versions",
        type=Path,
        default=DEFAULT_NEWSWIRE_VERSIONS,
    )
    parser.add_argument(
        "--news-source-registry",
        type=Path,
        default=DEFAULT_NEWS_SOURCE_REGISTRY,
    )
    parser.add_argument(
        "--editorial-evidence",
        type=Path,
        default=DEFAULT_EDITORIAL_EVIDENCE,
    )
    parser.add_argument(
        "--chinese-translations",
        type=Path,
        default=DEFAULT_CHINESE_TRANSLATIONS,
    )
    parser.add_argument(
        "--chinese-translation-schema",
        type=Path,
        default=DEFAULT_CHINESE_TRANSLATION_SCHEMA,
    )
    parser.add_argument(
        "--ucdp-aggregate", type=Path, default=DEFAULT_UCDP_AGGREGATE
    )
    parser.add_argument(
        "--wdi-bundle",
        type=Path,
        default=DEFAULT_WDI_BUNDLE,
        help="exact normalized WDI bundle to bind into a v2 observatory build",
    )
    parser.add_argument(
        "--wdi-artifact-path",
        default=WDI_ARTIFACT_PATH,
        help="repository-relative public path advertised for --wdi-bundle",
    )
    parser.add_argument(
        "--wdi-observation-schema",
        type=Path,
        default=DEFAULT_WDI_SCHEMA,
    )
    parser.add_argument(
        "--wdi-series-registry",
        type=Path,
        default=DEFAULT_WDI_SERIES_REGISTRY,
    )
    parser.add_argument(
        "--wdi-publication-receipt",
        type=Path,
        default=_AUTO_WDI_PUBLICATION_RECEIPT,
        help=(
            "canonical production receipt; defaults to the checked-in receipt "
            "when present"
        ),
    )
    parser.add_argument(
        "--wdi-archived-size-receipt",
        type=Path,
        help=(
            "canonical checked-in Pages size receipt; defaults to the exact "
            "publication-SHA path"
        ),
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    json_payload, html_payload = build(
        args.registry,
        wdi_bundle_path=args.wdi_bundle,
        wdi_artifact_path=args.wdi_artifact_path,
        wdi_observation_schema_path=args.wdi_observation_schema,
        wdi_series_registry_path=args.wdi_series_registry,
        wdi_publication_receipt_path=args.wdi_publication_receipt,
        wdi_archived_size_receipt_path=args.wdi_archived_size_receipt,
        newswire_path=args.newswire,
        newswire_versions_path=args.newswire_versions,
        news_source_registry_path=args.news_source_registry,
        editorial_evidence_path=args.editorial_evidence,
        chinese_translations_path=args.chinese_translations,
        chinese_translation_schema_path=args.chinese_translation_schema,
    )
    artifact = json.loads(json_payload)
    wire = _load_newswire(args.newswire)
    versions, versions_sha256 = _load_newswire_versions(args.newswire_versions)
    source_specs, source_registry_sha256 = _source_registry_projection(
        args.news_source_registry
    )
    editorial_evidence, editorial_evidence_sha256 = _load_editorial_evidence(
        args.editorial_evidence
    )
    translations = chinese_translation_pages.load_translations(
        args.chinese_translations,
        schema_path=args.chinese_translation_schema,
    )
    captured_indices = {
        region: _build_captured_index(
            wire,
            versions,
            versions_sha256=versions_sha256,
            source_specs=source_specs,
            source_registry_sha256=source_registry_sha256,
            region=region,
            translations=translations,
        )
        for region in ("bri", "gwadar", "balochistan", "myanmar")
    }
    wdi_bundle, _ = load_wdi_bundle(
        args.wdi_bundle,
        series_registry_path=args.wdi_series_registry,
    )
    ucdp_bundle = _load_ucdp_aggregate(args.ucdp_aggregate)
    analyses = {
        region: _build_regional_analysis(
            artifact,
            wire,
            region=region,
            wdi_bundle=wdi_bundle,
            ucdp_bundle=ucdp_bundle,
            captured_index=captured_indices[region],
            editorial_evidence=editorial_evidence,
            editorial_evidence_sha256=editorial_evidence_sha256,
        )
        for region in ("gwadar", "balochistan", "myanmar")
    }
    gwadar_payload = _render_gwadar_html(
        artifact,
        wire,
        analyses["gwadar"],
        captured_indices["gwadar"],
    )
    balochistan_payload = _render_regional_dossier_html(
        artifact,
        wire,
        analyses["balochistan"],
        captured_indices["balochistan"],
        region="balochistan",
        canonical_path="/belt-and-road/balochistan/",
        label="Balochistan",
        headline="Rights claims, political economy and public records—source by source.",
        introduction=(
            "This lane keeps reported abuses, civic activity, state actions, "
            "resource governance and CPEC-linked political economy separate."
        ),
        geography_codes=("PAK-BAL", "PAK-GWD"),
        target_ids=("balochistan_resources_revenue", "balochistan_movement_history"),
    )
    myanmar_payload = _render_regional_dossier_html(
        artifact,
        wire,
        analyses["myanmar"],
        captured_indices["myanmar"],
        region="myanmar",
        canonical_path="/belt-and-road/myanmar/",
        label="Myanmar",
        headline="CMEC, conflict, rights and local effects—without flattening the record.",
        introduction=(
            "This lane keeps official project claims, independent reporting, "
            "humanitarian evidence and local political economy distinct."
        ),
        geography_codes=("MMR", "MMR-RKH"),
        target_ids=(
            "cmec_portfolio",
            "kyaukpyu_port_sez",
            "china_myanmar_pipelines",
            "mandalay_muse_rail",
        ),
    )
    outputs = [
        (args.json_output, json_payload),
        (args.html_output, html_payload),
        (args.gwadar_html_output, gwadar_payload),
        (args.balochistan_html_output, balochistan_payload),
        (args.myanmar_html_output, myanmar_payload),
        (
            args.gwadar_analysis_html_output,
            _render_analysis_html(analyses["gwadar"]),
        ),
        (
            args.gwadar_analysis_json_output,
            _json_bytes(analyses["gwadar"]),
        ),
        (
            args.balochistan_analysis_html_output,
            _render_analysis_html(analyses["balochistan"]),
        ),
        (
            args.balochistan_analysis_json_output,
            _json_bytes(analyses["balochistan"]),
        ),
        (
            args.myanmar_analysis_html_output,
            _render_analysis_html(analyses["myanmar"]),
        ),
        (
            args.myanmar_analysis_json_output,
            _json_bytes(analyses["myanmar"]),
        ),
    ]
    for region, captured_index in captured_indices.items():
        output_root = _REGIONAL_OUTPUT_ROOTS[region]
        regional_data = _build_regional_data_dump(
            artifact,
            captured_index,
            region=region,
            wdi_bundle=wdi_bundle,
            ucdp_bundle=ucdp_bundle,
            editorial_evidence=editorial_evidence,
            editorial_evidence_sha256=editorial_evidence_sha256,
        )
        outputs.extend(
            (
                (output_root / "captured-index.json", _json_bytes(captured_index)),
                (output_root / "captured-index.jsonl", _jsonl_bytes(captured_index)),
                (output_root / "captured-index.csv", _csv_bytes(captured_index)),
                (output_root / "regional-data.json", _json_bytes(regional_data)),
            )
        )
    if args.check:
        drift = [str(path.relative_to(ROOT)) for path, payload in outputs if not path.is_file() or path.read_bytes() != payload]
        if drift:
            print("belt-and-road observatory drift: " + ", ".join(drift))
            return 2
        print("belt-and-road observatory: exact")
        return 0
    for path, payload in outputs:
        _atomic_write(path, payload)
        print(f"wrote {_display_path(path)} ({len(payload)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
