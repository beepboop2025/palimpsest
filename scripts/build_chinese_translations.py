#!/usr/bin/env python3
"""Build the rights-safe Chinese-to-English newswire translation sidecar.

Only source metadata already retained by Palimpsest is submitted for translation:
the title/headline and a collector-bounded feed excerpt/dek.  Article bodies are
never fetched or submitted.  Every output remains attached to an immutable source
record and content digest, while explanatory background is explicitly separated
from the translation.

Normal operation reuses the checked-in sidecar as an incremental cache and calls
the Gemini Interactions REST API only for missing content digests.  ``--offline``
rebuilds exclusively from that cache.  ``--check`` is a no-write, offline exact-byte
reproduction check suitable for CI.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import random
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from core.openrouter_client import (
    ENDPOINT as OPENROUTER_ENDPOINT,
    OpenRouterError,
    chat_completion as openrouter_chat_completion,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NEWS_ROOT = ROOT / "news" / "wire"
DEFAULT_WIRE = ROOT / "readings" / "newswire-latest.json"
DEFAULT_LEDGER = ROOT / "readings" / "newswire-versions.jsonl"
DEFAULT_OUTPUT = ROOT / "readings" / "chinese-translations-latest.json"
DEFAULT_SCHEMA = ROOT / "protocol" / "chinese-translations-v1.schema.json"

SCHEMA_VERSION = "chinese-translations-v1"
MODEL_ID = "gemini-3.1-flash-lite"
API_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
API_KEY_ENV = "GOOGLE_AI_STUDIO_API_KEY"
OPENROUTER_MODEL_ID = "google/gemini-3.1-flash-lite"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
PROMPT_REVISION = "palimpsest-zh-en-metadata-v1"
MAX_CONTEXT_CHARS = 400
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_LEDGER_LINE_BYTES = 64 * 1024
MAX_LEDGER_ROWS = 1_000_000
MAX_LEDGER_HEADLINE_CHARS = 1_000
MIN_HAN_CHARACTERS = 4
MIN_HAN_SHARE = 0.35
DEFAULT_BATCH_SIZE = 36
GOOGLE_SAFE_BATCH_SIZE = 8
DEFAULT_WORKERS = 1
MAX_RETRIES = 3

LEDGER_KEYS = frozenset(
    {
        "event_id",
        "evidence_strength",
        "headline",
        "previous_version_id",
        "published_at",
        "recorded_at",
        "source_ids",
        "version_id",
    }
)

BACKGROUND_BASIS = (
    "Supplied title/headline, bounded feed excerpt/dek, publisher metadata, topics, "
    "and source clocks only; no article body, web retrieval, or outside facts."
)
RIGHTS_NOTICE = (
    "Palimpsest translates only retained metadata: title/headline and the feed's "
    "bounded plain-text excerpt/dek. Originals, canonical publisher links, source "
    "clocks, and version identifiers remain authoritative; no article body is "
    "fetched, copied, or submitted to the model."
)

_REVIEWED_NAME_NORMALIZATIONS = (
    {
        "source_text": "敏辛",
        "variant": re.compile(r"\bMin\s*Xin\b", flags=re.IGNORECASE),
        "canonical": "Min Zin",
        "note": (
            "Proper name standardized to ‘Min Zin’ for the person rendered as "
            "敏辛 in the captured Chinese metadata."
        ),
    },
)


class TranslationBuildError(RuntimeError):
    """Raised when source coverage or model output is incomplete or unsafe."""


class TranslationRateLimitError(TranslationBuildError):
    """A provider rate ceiling with a bounded server-advised retry delay."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = min(max(retry_after, 1.0), 65.0)


def _retry_after_seconds(detail: str) -> float:
    """Parse Google's retry hint and bound it against hostile/absent values."""
    match = re.search(
        r"retry\s+in\s+([0-9]+(?:\.[0-9]+)?)(ms|s)",
        detail,
        flags=re.IGNORECASE,
    )
    if not match:
        return 15.0
    retry_after = float(match.group(1))
    if match.group(2).casefold() == "ms":
        retry_after /= 1000
    return min(max(retry_after, 1.0), 65.0)


@dataclass(frozen=True)
class Candidate:
    record_kind: str
    source_path: str
    event_id: str | None
    event_version_id: str | None
    item_id: str | None
    item_version_id: str | None
    title_zh: str
    context_zh: str
    context_field: str
    palimpsest_url: str | None
    published_at: str | None
    updated_at: str | None
    collected_at: str | None
    recorded_at: str | None
    source_records: tuple[dict[str, Any], ...]
    topics: tuple[str, ...]
    rights_policies: tuple[str, ...]

    @property
    def content_sha256(self) -> str:
        return _sha256_json(
            {
                "context_zh": self.context_zh,
                "title_zh": self.title_zh,
            }
        )

    @property
    def record_sha256(self) -> str:
        return _sha256_json(
            {
                "content_sha256": self.content_sha256,
                "event_id": self.event_id,
                "event_version_id": self.event_version_id,
                "item_id": self.item_id,
                "item_version_id": self.item_version_id,
                "record_kind": self.record_kind,
                "source_path": self.source_path,
            }
        )

    @property
    def translation_id(self) -> str:
        return f"zhtr-{self.record_sha256[:24]}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TranslationBuildError(f"cannot read JSON source {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TranslationBuildError(f"JSON source must be an object: {path}")
    return value


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate object key {key!r}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _required_bounded_string(
    row: dict[str, Any], key: str, *, source_path: str, max_length: int
) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise TranslationBuildError(
            f"ledger {key} must be a nonempty string of at most {max_length} "
            f"characters: {source_path}"
        )
    return value


def _read_ledger(path: Path) -> list[tuple[int, dict[str, Any]]]:
    """Read a frozen event-version ledger with explicit resource bounds."""
    try:
        metadata = path.stat()
    except OSError as exc:
        raise TranslationBuildError(f"cannot stat newswire ledger {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise TranslationBuildError(f"newswire ledger is not a regular file: {path}")
    if metadata.st_size > MAX_LEDGER_BYTES:
        raise TranslationBuildError(
            f"newswire ledger exceeds {MAX_LEDGER_BYTES} bytes: {path}"
        )

    rows: list[tuple[int, dict[str, Any]]] = []
    identities: set[tuple[str, str]] = set()
    try:
        with path.open("rb") as handle:
            line_number = 0
            while True:
                raw = handle.readline(MAX_LEDGER_LINE_BYTES + 2)
                if not raw:
                    break
                line_number += 1
                source_path = f"{_relative(path)}#L{line_number}"
                if len(raw) > MAX_LEDGER_LINE_BYTES:
                    raise TranslationBuildError(
                        f"ledger line exceeds {MAX_LEDGER_LINE_BYTES} bytes: {source_path}"
                    )
                if not raw.strip():
                    raise TranslationBuildError(f"blank ledger line: {source_path}")
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise TranslationBuildError(
                        f"ledger line is not valid UTF-8: {source_path}: {exc}"
                    ) from exc
                try:
                    row = json.loads(
                        text,
                        object_pairs_hook=_strict_json_object,
                        parse_constant=_reject_json_constant,
                    )
                except (json.JSONDecodeError, ValueError) as exc:
                    raise TranslationBuildError(
                        f"invalid ledger JSON: {source_path}: {exc}"
                    ) from exc
                if not isinstance(row, dict):
                    raise TranslationBuildError(
                        f"ledger row must be an object: {source_path}"
                    )
                if frozenset(row) != LEDGER_KEYS:
                    missing = sorted(LEDGER_KEYS - frozenset(row))
                    extra = sorted(frozenset(row) - LEDGER_KEYS)
                    raise TranslationBuildError(
                        f"ledger row has unexpected keys: {source_path}; "
                        f"missing={missing}, extra={extra}"
                    )

                event_id = _required_bounded_string(
                    row, "event_id", source_path=source_path, max_length=256
                )
                version_id = _required_bounded_string(
                    row, "version_id", source_path=source_path, max_length=256
                )
                _required_bounded_string(
                    row,
                    "headline",
                    source_path=source_path,
                    max_length=MAX_LEDGER_HEADLINE_CHARS,
                )
                _required_bounded_string(
                    row,
                    "evidence_strength",
                    source_path=source_path,
                    max_length=128,
                )
                _required_bounded_string(
                    row, "published_at", source_path=source_path, max_length=64
                )
                _required_bounded_string(
                    row, "recorded_at", source_path=source_path, max_length=64
                )
                if not event_id.startswith("event-") or not version_id.startswith(
                    "eventv-"
                ):
                    raise TranslationBuildError(
                        f"ledger event identity has an invalid prefix: {source_path}"
                    )
                previous = row.get("previous_version_id")
                if previous is not None and (
                    not isinstance(previous, str)
                    or not previous.startswith("eventv-")
                    or len(previous) > 256
                ):
                    raise TranslationBuildError(
                        f"ledger previous_version_id is invalid: {source_path}"
                    )
                source_ids = row.get("source_ids")
                if (
                    not isinstance(source_ids, list)
                    or not source_ids
                    or any(
                        not isinstance(source_id, str)
                        or not source_id
                        or len(source_id) > 256
                        for source_id in source_ids
                    )
                ):
                    raise TranslationBuildError(
                        f"ledger source_ids must be a nonempty array of bounded strings: "
                        f"{source_path}"
                    )
                if len(source_ids) != len(set(source_ids)):
                    raise TranslationBuildError(
                        f"ledger source_ids must be unique: {source_path}"
                    )
                identity = (event_id, version_id)
                if identity in identities:
                    raise TranslationBuildError(
                        f"duplicate ledger event/version identity: {source_path}"
                    )
                identities.add(identity)
                rows.append((line_number, row))
                if len(rows) > MAX_LEDGER_ROWS:
                    raise TranslationBuildError(
                        f"newswire ledger exceeds {MAX_LEDGER_ROWS} rows: {path}"
                    )
    except OSError as exc:
        raise TranslationBuildError(f"cannot read newswire ledger {path}: {exc}") from exc
    return rows


def _is_han(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2FA1F
    )


def script_profile(text: str) -> dict[str, Any]:
    """Return the stable detector receipt used for admission into this sidecar."""
    han_characters = sum(1 for character in text if _is_han(character))
    letter_characters = sum(1 for character in text if character.isalpha())
    share = han_characters / letter_characters if letter_characters else 0.0
    return {
        "han_characters": han_characters,
        "letter_characters": letter_characters,
        "han_share_of_letters": round(share, 6),
        "chinese_dominant": bool(
            han_characters >= MIN_HAN_CHARACTERS and share >= MIN_HAN_SHARE
        ),
    }


def is_chinese_dominant(text: str) -> bool:
    return bool(script_profile(text)["chinese_dominant"])


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TranslationBuildError("retained title/excerpt must be a string")
    return " ".join(value.split())


def _bounded_context(value: Any, *, source_path: str) -> str:
    text = _clean_text(value)
    if len(text) > MAX_CONTEXT_CHARS:
        raise TranslationBuildError(
            f"retained context exceeds {MAX_CONTEXT_CHARS} characters: {source_path}"
        )
    return text


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _source_records_from_event(event: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    refs = event.get("evidence_refs", [])
    if refs is None:
        refs = []
    if not isinstance(refs, list):
        raise TranslationBuildError("event evidence_refs must be an array")
    for ref in refs:
        if not isinstance(ref, dict):
            raise TranslationBuildError("event evidence_refs entries must be objects")
        record = {
            "source_id": _clean_text(ref.get("source_id")) or None,
            "source_name": _clean_text(ref.get("source_name")) or None,
            "item_id": _clean_text(ref.get("item_id")) or None,
            "item_version_id": _clean_text(ref.get("version_id")) or None,
            "publisher_url": _clean_text(ref.get("url")) or None,
            "published_at": _clean_text(ref.get("published_at")) or None,
        }
        records.append(record)
    records.sort(
        key=lambda record: (
            record.get("source_id") or "",
            record.get("item_version_id") or "",
            record.get("publisher_url") or "",
        )
    )
    return tuple(records)


def _event_identity(event: dict[str, Any], *, source_path: str) -> tuple[str, str]:
    event_id = _clean_text(event.get("event_id"))
    version_id = _clean_text(event.get("version_id"))
    if not event_id or not version_id:
        raise TranslationBuildError(f"event identity is incomplete: {source_path}")
    return event_id, version_id


def _event_candidate(
    event: dict[str, Any], *, record_kind: str, source_path: str
) -> Candidate | None:
    title = _clean_text(event.get("headline"))
    context = _bounded_context(event.get("dek"), source_path=source_path)
    if not is_chinese_dominant(f"{title}\n{context}"):
        return None
    if not title:
        raise TranslationBuildError(f"Chinese-dominant event has no headline: {source_path}")
    event_id, version_id = _event_identity(event, source_path=source_path)
    topics = event.get("topics", []) or []
    if not isinstance(topics, list):
        raise TranslationBuildError(f"event topics must be an array: {source_path}")
    return Candidate(
        record_kind=record_kind,
        source_path=source_path,
        event_id=event_id,
        event_version_id=version_id,
        item_id=None,
        item_version_id=None,
        title_zh=title,
        context_zh=context,
        context_field="dek",
        palimpsest_url=_clean_text(event.get("url")) or None,
        published_at=_clean_text(event.get("published_at")) or None,
        updated_at=_clean_text(event.get("updated_at")) or None,
        collected_at=None,
        recorded_at=None,
        source_records=_source_records_from_event(event),
        topics=tuple(sorted({_clean_text(topic) for topic in topics if _clean_text(topic)})),
        rights_policies=(),
    )


def _item_candidate(item: dict[str, Any], *, source_path: str) -> Candidate | None:
    title = _clean_text(item.get("title"))
    context = _bounded_context(item.get("excerpt"), source_path=source_path)
    if not is_chinese_dominant(f"{title}\n{context}"):
        return None
    item_id = _clean_text(item.get("item_id")) or None
    version_id = _clean_text(item.get("version_id")) or None
    if not item_id or not version_id:
        raise TranslationBuildError(f"item identity is incomplete: {source_path}")
    topics = item.get("topics", []) or []
    if not isinstance(topics, list):
        raise TranslationBuildError(f"item topics must be an array: {source_path}")
    rights_policy = _clean_text(item.get("rights_policy"))
    source_record = {
        "source_id": _clean_text(item.get("source_id")) or None,
        "source_name": _clean_text(item.get("source_name")) or None,
        "item_id": item_id,
        "item_version_id": version_id,
        "publisher_url": _clean_text(item.get("url")) or None,
        "published_at": _clean_text(item.get("published_at")) or None,
    }
    return Candidate(
        record_kind="current_wire_item",
        source_path=source_path,
        event_id=None,
        event_version_id=None,
        item_id=item_id,
        item_version_id=version_id,
        title_zh=title,
        context_zh=context,
        context_field="excerpt",
        palimpsest_url=None,
        published_at=_clean_text(item.get("published_at")) or None,
        updated_at=None,
        collected_at=_clean_text(item.get("collected_at")) or None,
        recorded_at=None,
        source_records=(source_record,),
        topics=tuple(sorted({_clean_text(topic) for topic in topics if _clean_text(topic)})),
        rights_policies=(rights_policy,) if rights_policy else (),
    )


def _ledger_candidate(
    row: dict[str, Any], *, ledger_path: Path, line_number: int
) -> Candidate | None:
    source_path = f"{_relative(ledger_path)}#L{line_number}"
    title = row["headline"]
    if not is_chinese_dominant(title):
        return None
    source_records = tuple(
        {
            "source_id": source_id,
            "source_name": None,
            "item_id": None,
            "item_version_id": None,
            "publisher_url": None,
            "published_at": row["published_at"],
        }
        for source_id in sorted(row["source_ids"])
    )
    return Candidate(
        record_kind="ledger_event_revision",
        source_path=source_path,
        event_id=row["event_id"],
        event_version_id=row["version_id"],
        item_id=None,
        item_version_id=None,
        title_zh=title,
        context_zh="",
        context_field="headline_only",
        palimpsest_url=None,
        published_at=row["published_at"],
        updated_at=None,
        collected_at=None,
        recorded_at=row["recorded_at"],
        source_records=source_records,
        topics=(),
        rights_policies=(),
    )


def discover_candidates(
    news_root: Path, wire_path: Path, ledger_path: Path = DEFAULT_LEDGER
) -> list[Candidate]:
    """Discover retained, ledger-only, and current Chinese-dominant records."""
    candidates: list[Candidate] = []
    seen_event_identities: set[tuple[str, str]] = set()

    revision_paths = sorted(news_root.glob("event-*/revisions/eventv-*.json"))
    for path in revision_paths:
        event = _read_json(path)
        source_path = _relative(path)
        identity = _event_identity(event, source_path=source_path)
        # The newsroom may retain the same content-derived event version under
        # more than one historical cluster/event directory.  Each file remains
        # a distinct auditable record through source_path + event_id, while the
        # set still prevents a current-wire copy from being added a second time.
        seen_event_identities.add(identity)
        candidate = _event_candidate(
            event,
            record_kind="retained_event_revision",
            source_path=source_path,
        )
        if candidate:
            candidates.append(candidate)

    for path in sorted(news_root.glob("event-*/story.json")):
        event = _read_json(path)
        source_path = _relative(path)
        identity = _event_identity(event, source_path=source_path)
        if identity in seen_event_identities:
            continue
        seen_event_identities.add(identity)
        candidate = _event_candidate(
            event,
            record_kind="retained_current_story",
            source_path=source_path,
        )
        if candidate:
            candidates.append(candidate)

    # Only the immutable revision tree suppresses a ledger row.  This preserves
    # every historical ledger-only composite identity and never assumes that a
    # content-derived version ID is globally unique across event clusters.
    retained_event_identities = frozenset(seen_event_identities)
    for line_number, row in _read_ledger(ledger_path):
        identity = (row["event_id"], row["version_id"])
        if identity in retained_event_identities:
            continue
        candidate = _ledger_candidate(
            row, ledger_path=ledger_path, line_number=line_number
        )
        if candidate:
            candidates.append(candidate)
            seen_event_identities.add(identity)

    wire = _read_json(wire_path)
    events = wire.get("events", [])
    items = wire.get("items", [])
    if not isinstance(events, list) or not isinstance(items, list):
        raise TranslationBuildError("newswire events/items must be arrays")

    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise TranslationBuildError("newswire event entries must be objects")
        source_path = f"{_relative(wire_path)}#/events/{index}"
        identity = _event_identity(event, source_path=source_path)
        if identity in seen_event_identities:
            continue
        seen_event_identities.add(identity)
        candidate = _event_candidate(
            event,
            record_kind="current_wire_event",
            source_path=source_path,
        )
        if candidate:
            candidates.append(candidate)

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise TranslationBuildError("newswire item entries must be objects")
        version_id = _clean_text(item.get("version_id"))
        if not version_id:
            raise TranslationBuildError(f"current wire item {index} has no version_id")
        candidate = _item_candidate(
            item,
            source_path=f"{_relative(wire_path)}#/items/{index}",
        )
        if candidate:
            candidates.append(candidate)

    candidates.sort(
        key=lambda candidate: (
            candidate.record_kind,
            candidate.event_version_id or "",
            candidate.item_version_id or "",
            candidate.source_path,
        )
    )
    identities = [candidate.translation_id for candidate in candidates]
    if len(identities) != len(set(identities)):
        raise TranslationBuildError("translation candidate identities are not unique")
    return candidates


def _translation_cache(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not path.exists():
        return {}, _empty_usage()
    artifact = _read_json(path)
    cache: dict[str, dict[str, Any]] = {}
    for record in artifact.get("translations", []):
        if not isinstance(record, dict):
            continue
        original = record.get("original_zh", {})
        english = record.get("english", {})
        provenance = record.get("translation_provenance", {})
        if not isinstance(original, dict) or not isinstance(english, dict):
            continue
        digest = original.get("content_sha256")
        if (
            isinstance(digest, str)
            and provenance.get("prompt_revision") == PROMPT_REVISION
            and _valid_english(english)
        ):
            cached = {
                "english": english,
                "translation_provenance": _normalise_provenance(provenance),
            }
            previous = cache.get(digest)
            if previous is not None and previous != cached:
                raise TranslationBuildError(
                    f"cache has conflicting translations for content digest {digest}"
                )
            cache[digest] = cached
    usage = artifact.get("generation_usage", _empty_usage())
    return cache, _normalise_usage(usage)


def _load_work_cache(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not path.exists():
        return {}, _empty_usage()
    document = _read_json(path)
    if (
        document.get("schema_version") != "chinese-translation-work-cache-v1"
        or document.get("prompt_revision") != PROMPT_REVISION
    ):
        raise TranslationBuildError(f"incompatible translation work cache: {path}")
    entries = document.get("entries")
    if not isinstance(entries, dict):
        raise TranslationBuildError(f"translation work cache entries are invalid: {path}")
    admitted: dict[str, dict[str, Any]] = {}
    for digest, cached in entries.items():
        if (
            isinstance(digest, str)
            and len(digest) == 64
            and isinstance(cached, dict)
            and _valid_english(cached.get("english"))
        ):
            admitted[digest] = {
                "english": cached["english"],
                "translation_provenance": _normalise_provenance(
                    cached.get("translation_provenance")
                ),
            }
    return admitted, _normalise_usage(document.get("generation_usage"))


def _save_work_cache(
    path: Path,
    cache: dict[str, dict[str, Any]],
    usage: dict[str, Any],
) -> None:
    document = {
        "schema_version": "chinese-translation-work-cache-v1",
        "prompt_revision": PROMPT_REVISION,
        "entries": dict(sorted(cache.items())),
        "generation_usage": _normalise_usage(usage),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(_render(document), encoding="utf-8")
    temporary.replace(path)


def _valid_english(english: Any) -> bool:
    if not isinstance(english, dict):
        return False
    required = ("title_en", "context_en", "background_en", "translation_notes_en")
    if not all(isinstance(english.get(field), str) for field in required):
        return False
    if not english.get("title_en", "").strip():
        return False
    return not any(
        _is_han(character)
        for field in ("title_en", "context_en", "background_en")
        for character in english[field]
    )


def _normalise_provenance(value: Any) -> dict[str, Any]:
    """Admit either reviewed transport while retaining exact provider identity."""
    if not isinstance(value, dict):
        raise TranslationBuildError("translation provenance must be an object")
    provider = value.get("provider")
    if provider == "Google Gemini API":
        return {
            "provider": provider,
            "api": "Interactions REST v1beta",
            "endpoint": API_ENDPOINT,
            "model_id": MODEL_ID,
            "base_model_id": MODEL_ID,
            "prompt_revision": PROMPT_REVISION,
            "store": False,
            "generated_at": value.get("generated_at"),
        }
    if provider == "OpenRouter":
        return {
            "provider": provider,
            "api": "Chat Completions REST v1",
            "endpoint": OPENROUTER_ENDPOINT,
            "model_id": OPENROUTER_MODEL_ID,
            "base_model_id": MODEL_ID,
            "prompt_revision": PROMPT_REVISION,
            "store": None,
            "generated_at": value.get("generated_at"),
        }
    raise TranslationBuildError("translation cache uses an unreviewed provider")


def _empty_usage() -> dict[str, Any]:
    return {
        "scope": (
            "Cumulative provider-reported usage for API batches that created entries "
            "in this incremental cache; cached/offline rebuilds add zero tokens."
        ),
        "api_calls": 0,
        "unreported_usage_calls": 0,
        "total_cached_tokens": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_thought_tokens": 0,
        "total_tokens": 0,
        "total_tool_use_tokens": 0,
    }


def _normalise_usage(value: Any) -> dict[str, Any]:
    output = _empty_usage()
    if not isinstance(value, dict):
        return output
    for key in output:
        if key == "scope":
            continue
        raw = value.get(key, 0)
        if isinstance(raw, int) and raw >= 0:
            output[key] = raw
    return output


def _merge_usage(
    total: dict[str, Any], addition: dict[str, Any], *, provider_reported: bool
) -> None:
    total["api_calls"] += 1
    if not provider_reported:
        total["unreported_usage_calls"] += 1
    for key in (
        "total_cached_tokens",
        "total_input_tokens",
        "total_output_tokens",
        "total_thought_tokens",
        "total_tokens",
        "total_tool_use_tokens",
    ):
        value = addition.get(key, 0)
        if isinstance(value, int) and value >= 0:
            total[key] += value


def _add_usage_totals(total: dict[str, Any], addition: dict[str, Any]) -> None:
    for key in total:
        if key == "scope":
            continue
        value = addition.get(key, 0)
        if isinstance(value, int) and value >= 0:
            total[key] += value


def _unique_missing(
    candidates: Sequence[Candidate], cache: dict[str, dict[str, Any]]
) -> list[Candidate]:
    by_digest: dict[str, Candidate] = {}
    for candidate in candidates:
        if candidate.content_sha256 not in cache:
            by_digest.setdefault(candidate.content_sha256, candidate)
    return [by_digest[digest] for digest in sorted(by_digest)]


def _response_schema(ids: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "translations": {
                "type": "array",
                "minItems": len(ids),
                "maxItems": len(ids),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string", "enum": list(ids)},
                        "title_en": {"type": "string"},
                        "context_en": {"type": "string"},
                        "translation_notes_en": {"type": "string"},
                        "background_en": {"type": "string"},
                    },
                    "required": [
                        "id",
                        "title_en",
                        "context_en",
                        "translation_notes_en",
                        "background_en",
                    ],
                },
            }
        },
        "required": ["translations"],
    }


def _translation_task(batch: Sequence[Candidate]) -> dict[str, Any]:
    rows = [
        {
            "id": candidate.content_sha256,
            "title_zh": candidate.title_zh,
            "context_zh": candidate.context_zh,
            "context_field": candidate.context_field,
            "publisher_names": sorted(
                {
                    str(record["source_name"])
                    for record in candidate.source_records
                    if record.get("source_name")
                }
            ),
            "published_at": candidate.published_at,
            "topics": list(candidate.topics),
        }
        for candidate in batch
    ]
    return {
        "task": "Translate each supplied Chinese news metadata record into English.",
        "requirements": [
            "Translate title_zh and context_zh faithfully, completely, and idiomatically.",
            "Preserve all numbers, dates, currencies, units, quotations, uncertainty, attribution, and political or legal terminology.",
            "Do not soften, intensify, editorialize, censor, or add facts to title_en or context_en.",
            "title_en, context_en, and background_en must contain no Han characters. When the source discusses an alternate Chinese spelling, explain that fact in English without reproducing the Chinese spelling; translation_notes_en alone may quote original characters when essential to explain ambiguity.",
            "Transliterate proper names consistently; use an established English form only when unambiguous from the supplied text.",
            "Use translation_notes_en only for a material ambiguity, idiom, pun, abbreviation, or name choice; otherwise return an empty string.",
            "background_en is not a translation. Write one concise contextual sentence explaining what the supplied metadata says is driving or situating the event. Use only the supplied fields, clearly attribute publisher claims, and state when the metadata does not establish a cause.",
            "Never infer from or claim access to an article body, URL content, web search, or outside knowledge.",
            "Return every id exactly once and no other ids.",
        ],
        "records": rows,
    }


def _request_payload(batch: Sequence[Candidate]) -> dict[str, Any]:
    ids = [candidate.content_sha256 for candidate in batch]
    return {
        "model": MODEL_ID,
        "store": False,
        "system_instruction": (
            "You are a meticulous Simplified- and Traditional-Chinese news translator. "
            "Keep translation and contextual explanation strictly separated. The input "
            "is publisher metadata, not an article body. Output only the requested JSON."
        ),
        "input": _canonical_json(_translation_task(batch)),
        "generation_config": {
            "max_output_tokens": 32768,
            "seed": 41721,
            "thinking_level": "minimal",
        },
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": _response_schema(ids),
        },
    }


def _openrouter_prompt(batch: Sequence[Candidate]) -> str:
    return (
        "You are a meticulous Simplified- and Traditional-Chinese news translator. "
        "Keep translation and contextual explanation strictly separated. The input "
        "is publisher metadata, not an article body. Return only one JSON object "
        "matching this JSON Schema, without markdown fences:\n"
        + _canonical_json(_response_schema([row.content_sha256 for row in batch]))
        + "\nTASK:\n"
        + _canonical_json(_translation_task(batch))
    )


def _extract_response_text(response: dict[str, Any]) -> str:
    fragments: list[str] = []
    steps = response.get("steps", [])
    if not isinstance(steps, list):
        raise TranslationBuildError("Interactions response has no steps array")
    for step in steps:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        for content in step.get("content", []) or []:
            if isinstance(content, dict) and content.get("type") == "text":
                text = content.get("text")
                if isinstance(text, str):
                    fragments.append(text)
    if not fragments:
        raise TranslationBuildError("Interactions response has no model text output")
    return "".join(fragments)


def _post_interaction(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        API_ENDPOINT,
        data=_canonical_json(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # Never include request headers or the API key in diagnostics.
        detail = exc.read(2048).decode("utf-8", errors="replace")
        if exc.code == 429:
            raise TranslationRateLimitError(
                "Interactions API rate limit reached", _retry_after_seconds(detail)
            ) from exc
        raise TranslationBuildError(
            f"Interactions API HTTP {exc.code}: {detail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TranslationBuildError(f"Interactions API request failed: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TranslationBuildError("Interactions API returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise TranslationBuildError("Interactions API response must be an object")
    return value


def _validate_model_text(
    batch: Sequence[Candidate], response_text: str
) -> dict[str, dict[str, Any]]:
    try:
        decoded = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise TranslationBuildError("model output is not valid structured JSON") from exc
    rows = decoded.get("translations") if isinstance(decoded, dict) else None
    if not isinstance(rows, list):
        raise TranslationBuildError("model output has no translations array")
    expected = {candidate.content_sha256 for candidate in batch}
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise TranslationBuildError("model translation entry must be an object")
        digest = row.get("id")
        if not isinstance(digest, str) or digest not in expected or digest in output:
            raise TranslationBuildError("model returned an unknown or duplicate translation id")
        english = {
            "title_en": _clean_text(row.get("title_en")),
            "context_en": _clean_text(row.get("context_en")),
            "translation_notes_en": _clean_text(row.get("translation_notes_en")),
            "background_en": _clean_text(row.get("background_en")),
            "background_basis": BACKGROUND_BASIS,
            "background_status": "machine-generated-context-not-translation",
            "status": "machine-draft-not-human-certified",
        }
        if not _valid_english(english) or not english["background_en"]:
            raise TranslationBuildError(f"model returned incomplete English for {digest}")
        output[digest] = english
    if set(output) != expected:
        missing = sorted(expected - set(output))
        raise TranslationBuildError(
            f"model output did not cover every requested id: {', '.join(missing[:5])}"
        )
    return output


def _validate_batch_output(
    batch: Sequence[Candidate], response: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    return _validate_model_text(batch, _extract_response_text(response))


def _translate_google_batch(
    batch: Sequence[Candidate], api_key: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
    last_error: Exception | None = None
    attempt = 0
    rate_limit_retries = 0
    while attempt < MAX_RETRIES:
        try:
            response = _post_interaction(_request_payload(batch), api_key)
            translations = _validate_batch_output(batch, response)
            usage = response.get("usage", {})
            if not isinstance(usage, dict):
                usage = {}
            generated_at = _clean_text(response.get("updated") or response.get("created"))
            provenance = {
                "provider": "Google Gemini API",
                "api": "Interactions REST v1beta",
                "endpoint": API_ENDPOINT,
                "model_id": MODEL_ID,
                "base_model_id": MODEL_ID,
                "prompt_revision": PROMPT_REVISION,
                "store": False,
                "generated_at": generated_at or None,
            }
            return translations, usage, provenance
        except TranslationRateLimitError as exc:
            last_error = exc
            rate_limit_retries += 1
            if rate_limit_retries > 12:
                raise
            time.sleep(exc.retry_after + 0.5 + random.random())
        except TranslationBuildError as exc:
            last_error = exc
            attempt += 1
            if attempt < MAX_RETRIES:
                time.sleep((2**attempt) + random.random())
    assert last_error is not None
    raise last_error


def _translate_openrouter_batch(
    batch: Sequence[Candidate], api_key: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            text = openrouter_chat_completion(
                api_key,
                OPENROUTER_MODEL_ID,
                _openrouter_prompt(batch),
                max_tokens=4096,
                title="Palimpsest Chinese Translations",
                timeout=120,
            )
            translations = _validate_model_text(batch, text)
            provenance = {
                "provider": "OpenRouter",
                "api": "Chat Completions REST v1",
                "endpoint": OPENROUTER_ENDPOINT,
                "model_id": OPENROUTER_MODEL_ID,
                "base_model_id": MODEL_ID,
                "prompt_revision": PROMPT_REVISION,
                "store": None,
                "generated_at": None,
            }
            # The reviewed narrow client intentionally returns only bounded text,
            # so provider token accounting is unavailable on this transport.
            return translations, {}, provenance
        except (OpenRouterError, TranslationBuildError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < MAX_RETRIES:
                time.sleep((2**attempt) + random.random())
    assert last_error is not None
    raise TranslationBuildError(f"OpenRouter translation failed: {last_error}") from last_error


def _translate_with_split(
    batch: Sequence[Candidate],
    api_key: str,
    transport: str,
    cache: dict[str, dict[str, Any]],
    usage_total: dict[str, Any],
) -> None:
    try:
        if transport == "google":
            translated, usage, provenance = _translate_google_batch(batch, api_key)
        elif transport == "openrouter":
            translated, usage, provenance = _translate_openrouter_batch(batch, api_key)
        else:  # pragma: no cover - selected internally
            raise TranslationBuildError(f"unreviewed translation transport: {transport}")
    except TranslationRateLimitError:
        # A quota ceiling is transport-wide; splitting would only amplify it.
        raise
    except TranslationBuildError:
        if len(batch) <= 1:
            raise
        midpoint = len(batch) // 2
        _translate_with_split(
            batch[:midpoint], api_key, transport, cache, usage_total
        )
        _translate_with_split(
            batch[midpoint:], api_key, transport, cache, usage_total
        )
        return
    _merge_usage(
        usage_total,
        usage,
        provider_reported=transport == "google" and bool(usage),
    )
    for digest, english in translated.items():
        cache[digest] = {
            "english": english,
            "translation_provenance": provenance,
        }


def translate_missing(
    candidates: Sequence[Candidate],
    cache: dict[str, dict[str, Any]],
    usage_total: dict[str, Any],
    *,
    api_key: str,
    transport: str,
    batch_size: int,
    workers: int = DEFAULT_WORKERS,
    work_cache_path: Path | None = None,
) -> int:
    missing = _unique_missing(candidates, cache)
    batches = [
        missing[offset : offset + batch_size]
        for offset in range(0, len(missing), batch_size)
    ]

    def translate_one(
        batch: Sequence[Candidate],
    ) -> tuple[Sequence[Candidate], dict[str, dict[str, Any]], dict[str, Any]]:
        local_cache: dict[str, dict[str, Any]] = {}
        local_usage = _empty_usage()
        _translate_with_split(
            batch, api_key, transport, local_cache, local_usage
        )
        return batch, local_cache, local_usage

    completed = 0
    errors: list[Exception] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(translate_one, batch) for batch in batches]
        for future in concurrent.futures.as_completed(futures):
            try:
                batch, completed_cache, completed_usage = future.result()
            except Exception as exc:  # preserve every other completed checkpoint
                errors.append(exc)
                continue
            cache.update(completed_cache)
            _add_usage_totals(usage_total, completed_usage)
            completed += len(batch)
            if work_cache_path is not None:
                _save_work_cache(work_cache_path, cache, usage_total)
            print(
                f"translated unique content {completed}/{len(missing)}",
                file=sys.stderr,
            )
    if errors:
        raise TranslationBuildError(
            f"{len(errors)} translation batch(es) failed; first error: {errors[0]}"
        ) from errors[0]
    return len(missing)


def _record(candidate: Candidate, cached: dict[str, Any]) -> dict[str, Any]:
    profile = script_profile(f"{candidate.title_zh}\n{candidate.context_zh}")
    if not profile["chinese_dominant"]:
        raise TranslationBuildError(
            f"non-dominant record reached translation output: {candidate.source_path}"
        )
    english = cached.get("english")
    provenance = cached.get("translation_provenance")
    if not _valid_english(english) or not isinstance(provenance, dict):
        raise TranslationBuildError(
            f"Chinese-dominant source record lacks English output: {candidate.source_path}"
        )
    if candidate.context_zh and not english["context_en"].strip():
        raise TranslationBuildError(
            f"nonempty Chinese context lacks English translation: {candidate.source_path}"
        )
    # A headline-only ledger record has no retained context to translate.  Do
    # not publish a model-generated placeholder as though it were source text.
    english = dict(english)
    if not candidate.context_zh:
        english["context_en"] = ""
    source_text = f"{candidate.title_zh}\n{candidate.context_zh}"
    for normalization in _REVIEWED_NAME_NORMALIZATIONS:
        if normalization["source_text"] not in source_text:
            continue
        for field in ("title_en", "context_en", "background_en"):
            english[field] = normalization["variant"].sub(
                normalization["canonical"], english[field]
            )
        note = normalization["note"]
        prior_notes = english["translation_notes_en"].strip()
        if note not in prior_notes:
            english["translation_notes_en"] = " ".join(
                part for part in (prior_notes, note) if part
            )
    return {
        "translation_id": candidate.translation_id,
        "record_sha256": candidate.record_sha256,
        "record_kind": candidate.record_kind,
        "source_path": candidate.source_path,
        "identity": {
            "event_id": candidate.event_id,
            "event_version_id": candidate.event_version_id,
            "item_id": candidate.item_id,
            "item_version_id": candidate.item_version_id,
        },
        "source_clocks": {
            "published_at": candidate.published_at,
            "updated_at": candidate.updated_at,
            "collected_at": candidate.collected_at,
            "recorded_at": candidate.recorded_at,
        },
        "palimpsest_url": candidate.palimpsest_url,
        "source_records": list(candidate.source_records),
        "topics": list(candidate.topics),
        "rights_policies": list(candidate.rights_policies),
        "original_zh": {
            "title": candidate.title_zh,
            "context": candidate.context_zh,
            "context_field": candidate.context_field,
            "content_sha256": candidate.content_sha256,
            "script_profile": profile,
        },
        "english": english,
        "translation_provenance": provenance,
    }


def _revision_tree_digest(news_root: Path) -> tuple[str, int, int]:
    paths = sorted(news_root.glob("event-*/revisions/eventv-*.json"))
    stories = sorted(news_root.glob("event-*/story.json"))
    entries = [
        {"path": _relative(path), "sha256": _sha256_file(path)}
        for path in [*paths, *stories]
    ]
    return _sha256_json(entries), len(paths), len(stories)


def build_artifact(
    candidates: Sequence[Candidate],
    cache: dict[str, dict[str, Any]],
    usage: dict[str, Any],
    *,
    wire_path: Path,
    news_root: Path,
    ledger_path: Path,
) -> dict[str, Any]:
    wire = _read_json(wire_path)
    translations = []
    for candidate in candidates:
        cached = cache.get(candidate.content_sha256)
        if cached is None:
            raise TranslationBuildError(
                "Chinese-dominant source record lacks English output: "
                f"{candidate.source_path} ({candidate.content_sha256})"
            )
        translations.append(_record(candidate, cached))
    tree_sha, revision_count, story_count = _revision_tree_digest(news_root)
    ledger_rows = _read_ledger(ledger_path)
    try:
        ledger_bytes = ledger_path.stat().st_size
    except OSError as exc:
        raise TranslationBuildError(f"cannot stat newswire ledger {ledger_path}: {exc}") from exc
    unique_content = {candidate.content_sha256 for candidate in candidates}
    record_kinds: dict[str, int] = {}
    for candidate in candidates:
        record_kinds[candidate.record_kind] = record_kinds.get(candidate.record_kind, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _clean_text(wire.get("generated_at")),
        "language_pair": {"source": "zh", "target": "en"},
        "method": {
            "detector": (
                "Unicode Han count divided by all Unicode alphabetic characters; "
                f"requires at least {MIN_HAN_CHARACTERS} Han characters and a share "
                f"of at least {MIN_HAN_SHARE:.2f}."
            ),
            "min_han_characters": MIN_HAN_CHARACTERS,
            "min_han_share_of_letters": MIN_HAN_SHARE,
            "max_context_characters": MAX_CONTEXT_CHARS,
            "selection": (
                "Every Chinese-dominant retained event revision, every Chinese-dominant "
                "ledger event/version identity absent from the retained revision tree, "
                "and every eligible current wire item. Current story/event rows are added "
                "only when their exact event/event-version identity is otherwise absent."
            ),
        },
        "rights": {
            "notice": RIGHTS_NOTICE,
            "submitted_fields": ["title/headline", "bounded feed excerpt/dek"],
            "article_bodies_submitted": False,
            "originals_preserved": True,
        },
        "model": {
            "provider": "Google Gemini API",
            "api": "Interactions REST v1beta",
            "endpoint": API_ENDPOINT,
            "model_id": MODEL_ID,
            "prompt_revision": PROMPT_REVISION,
            "store": False,
            "transport_selection": (
                f"Prefer {API_KEY_ENV}; otherwise use the reviewed OpenRouter client "
                f"when {OPENROUTER_API_KEY_ENV} is available."
            ),
            "fallback": {
                "provider": "OpenRouter",
                "api": "Chat Completions REST v1",
                "endpoint": OPENROUTER_ENDPOINT,
                "model_id": OPENROUTER_MODEL_ID,
                "store_control": "not-exposed-by-reviewed-client",
            },
            "output_status": "machine-draft-not-human-certified",
        },
        "source_snapshot": {
            "newswire_path": _relative(wire_path),
            "newswire_sha256": _sha256_file(wire_path),
            "newswire_generated_at": _clean_text(wire.get("generated_at")),
            "source_registry_sha256": _clean_text(wire.get("source_registry_sha256")),
            "newswire_ledger_path": _relative(ledger_path),
            "newswire_ledger_sha256": _sha256_file(ledger_path),
            "newswire_ledger_rows": len(ledger_rows),
            "newswire_ledger_bytes": ledger_bytes,
            "revision_tree_path": _relative(news_root),
            "revision_tree_sha256": tree_sha,
            "retained_revision_files": revision_count,
            "retained_story_files": story_count,
        },
        "coverage": {
            "candidate_records": len(candidates),
            "translated_records": len(translations),
            "unique_content_digests": len(unique_content),
            "record_kinds": dict(sorted(record_kinds.items())),
            "eligible_event_revisions": record_kinds.get(
                "retained_event_revision", 0
            ),
            "translated_event_revisions": record_kinds.get(
                "retained_event_revision", 0
            ),
            "eligible_ledger_event_revisions": record_kinds.get(
                "ledger_event_revision", 0
            ),
            "translated_ledger_event_revisions": record_kinds.get(
                "ledger_event_revision", 0
            ),
            "eligible_current_items": record_kinds.get("current_wire_item", 0),
            "translated_current_items": record_kinds.get("current_wire_item", 0),
            "eligible_current_events": record_kinds.get("current_wire_event", 0),
            "translated_current_events": record_kinds.get("current_wire_event", 0),
            "missing_records": 0,
        },
        "generation_usage": _normalise_usage(usage),
        "translations": translations,
    }


def _render(artifact: dict[str, Any]) -> str:
    return json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _validate_schema(artifact: dict[str, Any], schema_path: Path) -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:  # pragma: no cover - production environment includes it
        raise TranslationBuildError("jsonschema is required to validate the sidecar") from exc
    schema = _read_json(schema_path)
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(artifact),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path) or "<root>"
        raise TranslationBuildError(f"schema validation failed at {location}: {first.message}")


def run(
    *,
    news_root: Path = DEFAULT_NEWS_ROOT,
    wire_path: Path = DEFAULT_WIRE,
    ledger_path: Path = DEFAULT_LEDGER,
    output_path: Path = DEFAULT_OUTPUT,
    schema_path: Path = DEFAULT_SCHEMA,
    offline: bool = False,
    check: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    workers: int = DEFAULT_WORKERS,
    work_cache_path: Path | None = None,
) -> dict[str, Any]:
    if batch_size < 1 or batch_size > 100:
        raise TranslationBuildError("batch size must be between 1 and 100")
    if workers < 1 or workers > 8:
        raise TranslationBuildError("workers must be between 1 and 8")
    candidates = discover_candidates(news_root, wire_path, ledger_path)
    cache, usage = _translation_cache(output_path)
    if work_cache_path is None:
        work_cache_path = output_path.with_name(f".{output_path.name}.work-cache")
    if not check and work_cache_path.exists():
        work_cache, work_usage = _load_work_cache(work_cache_path)
        for digest, cached in work_cache.items():
            existing = cache.get(digest)
            if existing is not None and existing != cached:
                raise TranslationBuildError(
                    f"output and work cache disagree for content digest {digest}"
                )
            cache[digest] = cached
        usage = work_usage
    missing = _unique_missing(candidates, cache)
    if missing and (offline or check):
        raise TranslationBuildError(
            f"offline translation cache is missing {len(missing)} unique content digests"
        )
    if missing:
        google_key = os.environ.get(API_KEY_ENV, "").strip()
        openrouter_key = os.environ.get(OPENROUTER_API_KEY_ENV, "").strip()
        if google_key:
            transport = "google"
            api_key = google_key
            effective_batch_size = min(batch_size, GOOGLE_SAFE_BATCH_SIZE)
        elif openrouter_key:
            transport = "openrouter"
            api_key = openrouter_key
            # The narrow reviewed adapter caps output at 4,096 tokens.
            effective_batch_size = min(batch_size, 8)
        else:
            raise TranslationBuildError(
                f"{API_KEY_ENV} or {OPENROUTER_API_KEY_ENV} is required for "
                f"{len(missing)} uncached translations"
            )
        translate_missing(
            candidates,
            cache,
            usage,
            api_key=api_key,
            transport=transport,
            batch_size=effective_batch_size,
            workers=workers,
            work_cache_path=work_cache_path,
        )
    artifact = build_artifact(
        candidates,
        cache,
        usage,
        wire_path=wire_path,
        news_root=news_root,
        ledger_path=ledger_path,
    )
    _validate_schema(artifact, schema_path)
    rendered = _render(artifact)
    if check:
        if not output_path.exists() or output_path.read_text(encoding="utf-8") != rendered:
            raise TranslationBuildError(f"translation sidecar drift: {output_path}")
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
        work_cache_path.unlink(missing_ok=True)
    return artifact


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--news-root", type=Path, default=DEFAULT_NEWS_ROOT)
    parser.add_argument("--wire", type=Path, default=DEFAULT_WIRE)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="reuse only the checked-in content-addressed cache; do not call an API",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="offline no-write validation and exact-byte reproduction check",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--work-cache",
        type=Path,
        help="atomic resumable cache; defaults beside --output and is removed on success",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        artifact = run(
            news_root=args.news_root,
            wire_path=args.wire,
            ledger_path=args.ledger,
            output_path=args.output,
            schema_path=args.schema,
            offline=args.offline or args.check,
            check=args.check,
            batch_size=args.batch_size,
            workers=args.workers,
            work_cache_path=args.work_cache,
        )
    except TranslationBuildError as exc:
        print(f"chinese-translations: {exc}", file=sys.stderr)
        return 1
    coverage = artifact["coverage"]
    usage = artifact["generation_usage"]
    print(
        "chinese-translations: "
        f"{coverage['translated_records']} records / "
        f"{coverage['unique_content_digests']} unique content digests; "
        f"cumulative API calls={usage['api_calls']}, "
        f"input_tokens={usage['total_input_tokens']}, "
        f"output_tokens={usage['total_output_tokens']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
