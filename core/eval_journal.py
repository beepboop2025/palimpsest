"""Strict source model for the Palimpsest AI Eval Journal.

The journal is not allowed to turn a measurement into an essay by dropping the
parts that make the measurement checkable.  Every source article therefore names
its claim boundary, falsifier, limitations, verification commands, and exact local
evidence files.  This module validates those editorial commitments and binds each
published article to SHA-256 receipts for the files it cites.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SOURCE_SCHEMA = "palimpsest.eval-journal-source.v1"
ARTICLE_SCHEMA = "palimpsest.eval-journal-article.v1"
JOURNAL_SCHEMA = "palimpsest.eval-journal.v1"
SITE = "https://palimpsest.info"
SOURCE_DIR = Path("content/eval-journal")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SHA256 = re.compile(r"[0-9a-f]{64}")

SOURCE_FIELDS = {
    "schema",
    "slug",
    "title",
    "dek",
    "kind",
    "status",
    "author",
    "published_at",
    "updated_at",
    "claim",
    "sections",
    "evidence",
    "external_sources",
    "limitations",
    "falsifier",
    "verification",
}
SECTION_FIELDS = {"heading", "paragraphs", "points"}
EVIDENCE_FIELDS = {"path", "label", "role"}
EXTERNAL_FIELDS = {"title", "url", "relationship"}


class EvalJournalError(ValueError):
    """Raised when an article would publish without its editorial contract."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EvalJournalError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _constant(value: str) -> None:
    raise EvalJournalError(f"non-finite JSON constant: {value}")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except OSError as exc:
        raise EvalJournalError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvalJournalError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvalJournalError(f"{path} must contain one JSON object")
    return value


def _closed(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    missing = allowed - set(value)
    if unknown or missing:
        raise EvalJournalError(
            f"{label} has unknown={sorted(unknown)} missing={sorted(missing)}"
        )


def _text(value: Any, label: str, *, maximum: int = 10_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvalJournalError(f"{label} must be a non-empty string")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise EvalJournalError(f"{label} exceeds {maximum} characters")
    return cleaned


def _text_list(
    value: Any, label: str, *, minimum: int = 1, maximum: int = 20
) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise EvalJournalError(f"{label} must contain {minimum}..{maximum} items")
    return [_text(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _time(value: Any, label: str) -> str:
    text = _text(value, label, maximum=64)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise EvalJournalError(f"{label} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise EvalJournalError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _latest_timestamp(value: Any) -> datetime | None:
    """Find the latest generated/as-of timestamp in a bounded JSON structure."""
    candidates: list[datetime] = []

    def visit(item: Any, depth: int = 0) -> None:
        if depth > 3:
            return
        if isinstance(item, dict):
            for key, nested in item.items():
                if key in {"generated_at", "updated_at", "as_of", "head_ts"}:
                    try:
                        candidates.append(_as_datetime(_time(nested, key)))
                    except EvalJournalError:
                        pass
                elif depth < 3 and isinstance(nested, dict):
                    visit(nested, depth + 1)

    visit(value)
    return max(candidates) if candidates else None


def _safe_source_path(root: Path, relative: str) -> Path:
    if relative.startswith(("/", "~")) or "\\" in relative:
        raise EvalJournalError(f"evidence path must be repository-relative: {relative}")
    target = (root / relative).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise EvalJournalError(f"evidence path leaves repository: {relative}") from exc
    if not target.is_file():
        raise EvalJournalError(f"evidence file is missing: {relative}")
    return target


def validate_source(value: dict[str, Any], *, source: str) -> dict[str, Any]:
    _closed(value, SOURCE_FIELDS, source)
    if value.get("schema") != SOURCE_SCHEMA:
        raise EvalJournalError(f"{source} has an unsupported schema")
    slug = _text(value["slug"], f"{source}.slug", maximum=100)
    if not SLUG.fullmatch(slug):
        raise EvalJournalError(f"{source}.slug is not URL-safe")
    published = _time(value["published_at"], f"{source}.published_at")
    updated = _time(value["updated_at"], f"{source}.updated_at")
    if _as_datetime(updated) < _as_datetime(published):
        raise EvalJournalError(f"{source}.updated_at precedes publication")

    sections = value["sections"]
    if not isinstance(sections, list) or not 2 <= len(sections) <= 12:
        raise EvalJournalError(f"{source}.sections must contain 2..12 sections")
    clean_sections = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise EvalJournalError(f"{source}.sections[{index}] must be an object")
        _closed(section, SECTION_FIELDS, f"{source}.sections[{index}]")
        paragraphs = _text_list(
            section["paragraphs"], f"{source}.sections[{index}].paragraphs"
        )
        points = section["points"]
        if not isinstance(points, list):
            raise EvalJournalError(f"{source}.sections[{index}].points must be a list")
        clean_sections.append(
            {
                "heading": _text(section["heading"], f"{source}.sections[{index}].heading"),
                "paragraphs": paragraphs,
                "points": [
                    _text(point, f"{source}.sections[{index}].points[{point_index}]")
                    for point_index, point in enumerate(points)
                ],
            }
        )

    evidence = value["evidence"]
    if not isinstance(evidence, list) or not 2 <= len(evidence) <= 20:
        raise EvalJournalError(f"{source}.evidence must contain 2..20 receipts")
    clean_evidence = []
    paths: set[str] = set()
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise EvalJournalError(f"{source}.evidence[{index}] must be an object")
        _closed(item, EVIDENCE_FIELDS, f"{source}.evidence[{index}]")
        path = _text(item["path"], f"{source}.evidence[{index}].path", maximum=240)
        if path in paths:
            raise EvalJournalError(f"{source} repeats evidence path {path}")
        paths.add(path)
        clean_evidence.append(
            {
                "path": path,
                "label": _text(item["label"], f"{source}.evidence[{index}].label"),
                "role": _text(item["role"], f"{source}.evidence[{index}].role"),
            }
        )

    external = value["external_sources"]
    if not isinstance(external, list) or len(external) > 12:
        raise EvalJournalError(f"{source}.external_sources must be a list of at most 12")
    clean_external = []
    for index, item in enumerate(external):
        if not isinstance(item, dict):
            raise EvalJournalError(f"{source}.external_sources[{index}] must be an object")
        _closed(item, EXTERNAL_FIELDS, f"{source}.external_sources[{index}]")
        url = _text(item["url"], f"{source}.external_sources[{index}].url", maximum=500)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise EvalJournalError(f"{source}.external_sources[{index}].url must be public HTTPS")
        clean_external.append(
            {
                "title": _text(item["title"], f"{source}.external_sources[{index}].title"),
                "url": url,
                "relationship": _text(
                    item["relationship"],
                    f"{source}.external_sources[{index}].relationship",
                ),
            }
        )

    return {
        "schema": SOURCE_SCHEMA,
        "slug": slug,
        "title": _text(value["title"], f"{source}.title", maximum=180),
        "dek": _text(value["dek"], f"{source}.dek", maximum=500),
        "kind": _text(value["kind"], f"{source}.kind", maximum=80),
        "status": _text(value["status"], f"{source}.status", maximum=80),
        "author": _text(value["author"], f"{source}.author", maximum=120),
        "published_at": published,
        "updated_at": updated,
        "claim": _text(value["claim"], f"{source}.claim", maximum=700),
        "sections": clean_sections,
        "evidence": clean_evidence,
        "external_sources": clean_external,
        "limitations": _text_list(value["limitations"], f"{source}.limitations", minimum=2),
        "falsifier": _text(value["falsifier"], f"{source}.falsifier", maximum=1_500),
        "verification": _text_list(
            value["verification"], f"{source}.verification", minimum=1, maximum=12
        ),
    }


def _live_context(slug: str, root: Path) -> dict[str, Any]:
    assurance_path = root / "readings/eval-assurance-latest.json"
    assurance = _load_json(assurance_path) if assurance_path.exists() else {}
    summary = assurance.get("summary") if isinstance(assurance.get("summary"), dict) else {}
    if slug == "what-the-evidence-can-claim":
        claim_level = str(
            assurance.get("claim_ceiling", {}).get("level", "unavailable")
        ).replace("-", " ")
        return {
            "label": "Live assurance ceiling",
            "value": claim_level,
            "detail": (
                f"{summary.get('pass', 0)} pass · {summary.get('partial', 0)} partial · "
                f"{summary.get('pending', 0)} pending · {summary.get('open', 0)} open · "
                f"{summary.get('fail', 0)} fail"
            ),
            "url": "/readings/eval-assurance-latest.json",
        }
    if slug == "gfi-v2-answer-after-protocol":
        protocol = root / "readings/gfi-evaluation-protocol-v2.json"
        transcripts = root / "readings/gfi-transcripts-latest.json"
        live = protocol.exists() and transcripts.exists()
        return {
            "label": "Protocol state",
            "value": "sealed evidence live" if live else "staged for next collection",
            "detail": (
                "The exact v2 protocol and full response matrix are both public."
                if live
                else "The guard and workflow are shipped; the current public GFI remains legacy v1 until the next successful model run."
            ),
            "url": (
                "/readings/gfi-evaluation-protocol-v2.json"
                if live
                else "/docs/EVAL-REGISTRY.md"
            ),
        }
    if slug == "when-refusal-phrase-is-an-answer":
        reading_path = root / "readings/refusal-drift-latest.json"
        reading = _load_json(reading_path) if reading_path.exists() else {}
        method = reading.get("method_version")
        current = method == 4
        return {
            "label": "Public baseline",
            "value": "method v4" if current else f"method v{method}; v4 code shipped",
            "detail": (
                "The public series has rebaselined under the quote-aware judge."
                if current
                else "No longitudinal claim crosses the method boundary; the next successful sweep starts the v4 baseline."
            ),
            "url": "/readings/refusal-drift-latest.json",
        }
    claim_level = str(
        assurance.get("claim_ceiling", {}).get("level", "provisional-measurement")
    ).replace("-", " ")
    return {
        "label": "Evidence posture",
        "value": claim_level,
        "detail": "The origin explains why the instrument exists; the registry and assurance report determine what its results may claim.",
        "url": "/readings/eval-assurance-latest.json",
    }


def build_journal(root: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    source_dir = root / SOURCE_DIR
    source_files = sorted(source_dir.glob("*.json"))
    if not source_files:
        raise EvalJournalError(f"no journal sources in {SOURCE_DIR}")

    articles: list[dict[str, Any]] = []
    slugs: set[str] = set()
    for path in source_files:
        source = validate_source(_load_json(path), source=str(path.relative_to(root)))
        if source["slug"] in slugs:
            raise EvalJournalError(f"duplicate article slug: {source['slug']}")
        slugs.add(source["slug"])
        receipts = []
        modified = _as_datetime(source["updated_at"])
        for evidence in source["evidence"]:
            target = _safe_source_path(root, evidence["path"])
            payload = target.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if not SHA256.fullmatch(digest):  # pragma: no cover - hashlib guarantee
                raise EvalJournalError("invalid SHA-256 implementation output")
            json_value = None
            if target.suffix == ".json":
                try:
                    json_value = _load_json(target)
                except EvalJournalError:
                    json_value = None
            observed = _latest_timestamp(json_value) if json_value is not None else None
            if observed is not None:
                modified = max(modified, observed)
            receipts.append(
                {
                    **evidence,
                    "url": "/" + evidence["path"],
                    "sha256": digest,
                    "bytes": len(payload),
                }
            )

        article = {
            **{key: value for key, value in source.items() if key != "schema"},
            "schema": ARTICLE_SCHEMA,
            "url": f"{SITE}/evals/{source['slug']}/",
            "json_url": f"{SITE}/evals/{source['slug']}/article.json",
            "modified_at": modified.isoformat().replace("+00:00", "Z"),
            "evidence": receipts,
            "live_context": _live_context(source["slug"], root),
        }
        canonical = json.dumps(article, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        article["content_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        articles.append(article)

    articles.sort(key=lambda item: (item["published_at"], item["slug"]), reverse=True)
    generated = max(_as_datetime(article["modified_at"]) for article in articles)
    return {
        "schema": JOURNAL_SCHEMA,
        "generated_at": generated.isoformat().replace("+00:00", "Z"),
        "title": "Palimpsest AI Eval Journal",
        "description": "Evidence-bound essays about censorship evaluations, method changes, failures, and what the current record can actually support.",
        "source": "Closed article records in content/eval-journal, joined only to explicitly cited public Palimpsest artifacts.",
        "method": "Deterministic rendering with a closed editorial contract and fresh SHA-256 receipts for every local evidence citation.",
        "scope": "Editorial explanation of Palimpsest AI-evaluation origins, methods, failures, assurance, and findings; live measurement artifacts remain authoritative.",
        "home_page_url": f"{SITE}/evals/",
        "feed_url": f"{SITE}/evals/feed.json",
        "n_articles": len(articles),
        "articles": articles,
    }


def encode_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
