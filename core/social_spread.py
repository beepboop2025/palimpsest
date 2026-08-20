"""Public social-spread join: what is circulating, matched only to stored capture.

This desk answers a coverage question — which already-public board terms are
spreading, and whether the same string already appears in Palimpsest capture.
It does not find that a person is missing, detained, or dead.

Hard boundaries (do not weaken):

* Public surfaces only. No WeChat, private Weibo, DMs, follower graphs,
  location, engagement counts, or consumer profiles.
* Dragon Whispers is not reused. That desk forbids named allegations, named
  parties, accused fields, raw Telegram, and IOCs; its publication_policy stays
  exactly as written in ``core/dragon_whispers.py``.
* A whisper-only / anonymous-only name never becomes a person package.
* A person-level Palimpsest *finding* that someone is missing, detained, or
  dead is prohibited. Matched strings remain topic surfaces.
* Any row that names a person has ``automatic_publication=false`` and requires
  human review before a named package may be published.
* No motive or intent. No generative-model prose as reporting.
* Telegram handles are the in-tree public set only: DragonDenWhispers,
  DragonDenCyber, DragonDenBorderlands.

Match rule: a spreading term joins capture only when the same string already
appears in (a) a China-scoped newswire headline or dek from a registered
source, (b) an official-first-seen / last-alive official page title already
stored, or (c) a deletion-ledger title. Missing collectors abstain.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from collectors.telegram_public_channels import load_channels
from collectors.weibo_hotsearch import _SENSE_RULES, carries_sensitive_sense


ROOT = Path(__file__).resolve().parent.parent

SCHEMA_VERSION = "palimpsest-social-spread.v1"
JOB_NAME = "social-spread"
RELATION = "topic-surface-only"
SECONDARY_RELATION = "attributed-source-report-not-corroboration"
DISCLAIMER = (
    "Palimpsest does not confirm a person is missing, detained, or dead."
)
OFFICIAL_MISSING_WHISPER_REFUSAL = (
    "A whisper-only report that a Chinese official is missing is not a "
    "Palimpsest claim. Palimpsest does not confirm a person is missing, "
    "detained, or dead."
)

REQUIRED_SPREADING = ("weibo-hotsearch", "weibo-hotsearch-terms", "public-hot-boards")
OPTIONAL_SPREADING = ("telegram-public-channels", "social-observations")
MATCH_TARGETS = (
    "newswire",
    "news-wire-live",
    "official-first-seen",
    "public-deletion-ledgers",
)
OPTIONAL_CONTEXT = ("wayback",)
ALL_INPUTS = REQUIRED_SPREADING + OPTIONAL_SPREADING + MATCH_TARGETS + OPTIONAL_CONTEXT

ALLOWED_TELEGRAM_HANDLES = frozenset(
    channel["handle"] for channel in load_channels()
)
assert ALLOWED_TELEGRAM_HANDLES == {
    "DragonDenWhispers",
    "DragonDenCyber",
    "DragonDenBorderlands",
}

WHISPER_SOURCE_IDS = frozenset(
    {"telegram-public-channels"}
    | {f"telegram-public-channels:{handle}" for handle in ALLOWED_TELEGRAM_HANDLES}
)

DISPOSITIONS = frozenset(
    {
        "circulating-unverified",
        "matched-to-wire",
        "matched-to-official-page",
        "abstain",
    }
)
INPUT_STATES = frozenset({"present", "missing", "abstain"})
PUBLICATION_POLICY = {
    "human_review_required": True,
    "named_person_packages_auto_published": False,
    "named_person_findings_included": False,
    "counts_as_corroboration": False,
    "raw_telegram_included": False,
    "private_social_included": False,
    "dragon_whispers_reused": False,
}

PERSON_STATUS_MARKERS = (
    "失踪",
    "失联",
    "被捕",
    "逮捕",
    "拘留",
    "去世",
    "死亡",
    "身亡",
    "被抓",
    "missing",
    "detained",
    "arrested",
    "dead",
    "died",
)
OFFICIAL_CUES = ("official", "官员", "领导干部", "省委", "市委", "书记")
GENERIC_TERMS = frozenset(
    {
        "china",
        "chinese",
        "中国",
        "中國",
        "北京",
        "beijing",
        "news",
        "新闻",
        "weibo",
        "微博",
    }
)
FORBIDDEN_FIELDS = frozenset(
    {
        "accused",
        "allegation",
        "chat_id",
        "dm",
        "dms",
        "engagement",
        "follower",
        "followers",
        "ioc",
        "iocs",
        "like_count",
        "likes",
        "location",
        "message_text",
        "named_allegations",
        "named_party",
        "raw_telegram",
        "view_count",
        "wechat",
    }
)

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_EVENT_ID = re.compile(r"^event-[0-9a-f]{24}$")
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_SOURCE_ID = re.compile(r"^[a-z][a-z0-9:._-]{1,79}$")

MAX_ROWS = 4096
MAX_TERM = 180
MIN_CJK = 2
MIN_LATIN = 4

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "generated_at",
        "job_name",
        "status",
        "source",
        "method",
        "scope",
        "relation",
        "disclaimer",
        "publication_policy",
        "input_status",
        "n_rows",
        "n_abstained",
        "n_refused",
        "rows",
        "refusals",
        "news_story",
    }
)
_ROW_FIELDS = frozenset(
    {
        "term",
        "topic",
        "disposition",
        "relation",
        "disclaimer",
        "spreading",
        "matches",
        "names_a_person",
        "automatic_publication",
        "human_review_required",
    }
)
_SPREAD_FIELDS = frozenset(
    {"source_ids", "n_surfaces", "first_seen", "last_seen"}
)
_MATCH_FIELDS = frozenset(
    {"wire_event_ids", "official_page_last_alive", "ledger_hits"}
)
_STORY_FIELDS = frozenset(
    {
        "headline",
        "dek",
        "body",
        "automatic_publication",
        "relation",
        "disclaimer",
    }
)


class SocialSpreadError(ValueError):
    """A social-spread document crossed its public evidence boundary."""


def canonical_json_bytes(value: Any) -> bytes:
    def reject(node: Any, path: str = "social_spread") -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise SocialSpreadError(f"{path} contains a non-finite number")
        if isinstance(node, Mapping):
            for key, child in node.items():
                if type(key) is not str:
                    raise SocialSpreadError(f"{path} contains a non-string key")
                if str(key).casefold() in FORBIDDEN_FIELDS:
                    raise SocialSpreadError(f"{path} contains forbidden field {key!r}")
                reject(child, f"{path}.{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                reject(child, f"{path}[{index}]")

    reject(value)
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


def _exact(value: Any, fields: frozenset[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise SocialSpreadError(f"{path} does not use its exact field set")
    return value


def _timestamp(value: Any, path: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not _TIMESTAMP.fullmatch(value):
        raise SocialSpreadError(f"{path} must be canonical UTC")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise SocialSpreadError(f"{path} is not a real UTC timestamp") from exc
    return value


def _text(value: Any, path: str, *, maximum: int, empty: bool = False) -> str:
    if type(value) is not str or len(value) > maximum or (not empty and not value.strip()):
        raise SocialSpreadError(f"{path} must be bounded text")
    if value != value.strip() and not empty:
        raise SocialSpreadError(f"{path} has leading or trailing whitespace")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise SocialSpreadError(f"{path} contains unsafe Unicode")
    return value


def normalize_term(value: str) -> str:
    return unicodedata.normalize("NFC", " ".join((value or "").split()))


def term_is_usable(term: str) -> bool:
    if not term or term.casefold() in GENERIC_TERMS:
        return False
    if _CJK.search(term):
        return len(term) >= MIN_CJK
    return len(term) >= MIN_LATIN


def contains_person_status(term: str) -> bool:
    low = term.casefold()
    return any(marker in term or marker in low for marker in PERSON_STATUS_MARKERS)


def looks_like_named_person_package(term: str) -> bool:
    """True when a term would publish a person-status allegation, not a topic."""

    if not contains_person_status(term):
        return False
    for gated in _SENSE_RULES:
        if gated in term:
            keep, _cue = carries_sensitive_sense(gated, term)
            if not keep:
                return False
    stripped = term
    for marker in PERSON_STATUS_MARKERS:
        stripped = re.sub(re.escape(marker), " ", stripped, flags=re.I)
    leftover = normalize_term(stripped)
    if not leftover:
        return True
    if any(cue in leftover or cue in leftover.casefold() for cue in OFFICIAL_CUES):
        return True
    if _CJK.search(leftover) and len(re.sub(r"\s+", "", leftover)) >= 2:
        return True
    if re.search(r"[A-Z][a-z]{1,20}", leftover):
        return True
    return leftover.casefold() not in GENERIC_TERMS


def is_official_missing_whisper(term: str, source_ids: list[str]) -> bool:
    if not is_whisper_only(source_ids):
        return False
    low = term.casefold()
    official = any(cue in term or cue in low for cue in OFFICIAL_CUES)
    return official and contains_person_status(term)


def is_whisper_only(source_ids: list[str]) -> bool:
    return bool(source_ids) and set(source_ids) <= WHISPER_SOURCE_IDS


def refuse_official_missing_whisper() -> str:
    """Exact refusal for the owner example: official missing, whispers only."""

    return OFFICIAL_MISSING_WHISPER_REFUSAL


def _iso_or_none(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if type(value) is not str:
        return None
    return _timestamp_soft(value)


def _timestamp_soft(value: str) -> str | None:
    text = value.strip()
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    if _TIMESTAMP.fullmatch(text):
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_term(
    bucket: dict[str, dict[str, Any]],
    term: str,
    *,
    source_id: str,
    seen_at: str | None,
) -> None:
    term = normalize_term(term)
    if not term_is_usable(term):
        return
    if len(term) > MAX_TERM:
        term = term[:MAX_TERM].rstrip()
    row = bucket.get(term)
    stamp = seen_at or "1970-01-01T00:00:00Z"
    if row is None:
        bucket[term] = {
            "term": term,
            "source_ids": {source_id},
            "first_seen": stamp,
            "last_seen": stamp,
        }
        return
    row["source_ids"].add(source_id)
    if stamp < row["first_seen"]:
        row["first_seen"] = stamp
    if stamp > row["last_seen"]:
        row["last_seen"] = stamp


def _extract_weibo(doc: Mapping[str, Any], bucket: dict[str, dict[str, Any]]) -> None:
    generated = _iso_or_none(doc.get("generated_at"))
    for row in doc.get("gazetteer_breakthroughs") or []:
        if not isinstance(row, Mapping):
            continue
        if row.get("term"):
            _add_term(bucket, str(row["term"]), source_id="weibo-hotsearch", seen_at=generated)
        for sample in row.get("samples") or []:
            if isinstance(sample, Mapping) and sample.get("title"):
                date = str(sample.get("date") or "")
                seen = f"{date}T00:00:00Z" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else generated
                _add_term(
                    bucket,
                    str(sample["title"]),
                    source_id="weibo-hotsearch",
                    seen_at=_iso_or_none(seen) or generated,
                )
    for day in doc.get("pinned_headlines") or []:
        if not isinstance(day, Mapping):
            continue
        date = str(day.get("date") or "")
        seen = f"{date}T00:00:00Z" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else generated
        for title in day.get("pinned") or []:
            _add_term(
                bucket,
                str(title),
                source_id="weibo-hotsearch",
                seen_at=_iso_or_none(seen) or generated,
            )
    watch = doc.get("withdrawal_watch") if isinstance(doc.get("withdrawal_watch"), Mapping) else {}
    for candidate in watch.get("candidates") or []:
        if isinstance(candidate, Mapping) and candidate.get("title"):
            date = str(candidate.get("date") or "")
            seen = f"{date}T00:00:00Z" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else generated
            _add_term(
                bucket,
                str(candidate["title"]),
                source_id="weibo-hotsearch",
                seen_at=_iso_or_none(seen) or generated,
            )
    for record in doc.get("observation_records") or []:
        if isinstance(record, Mapping) and record.get("title"):
            _add_term(
                bucket,
                str(record["title"]),
                source_id="weibo-hotsearch",
                seen_at=_iso_or_none(record.get("first_seen") or record.get("detected_at"))
                or generated,
            )


def _extract_observations(
    doc: Mapping[str, Any],
    bucket: dict[str, dict[str, Any]],
    *,
    source_id: str,
    title_only: bool = False,
) -> None:
    generated = _iso_or_none(doc.get("generated_at"))
    for record in doc.get("observations") or []:
        if not isinstance(record, Mapping):
            continue
        seen = (
            _iso_or_none(record.get("first_seen"))
            or _iso_or_none(record.get("detected_at"))
            or _iso_or_none(record.get("published_at"))
            or generated
        )
        handle = str(record.get("channel_handle") or "")
        row_source = source_id
        if source_id == "telegram-public-channels":
            if handle and handle not in ALLOWED_TELEGRAM_HANDLES:
                continue
            if handle:
                row_source = f"telegram-public-channels:{handle}"
        if record.get("title") and not str(record.get("title")).startswith("[telegram:"):
            _add_term(bucket, str(record["title"]), source_id=row_source, seen_at=seen)
        if not title_only and record.get("text"):
            text = normalize_term(str(record["text"]))
            first_line = text.split("。")[0].split(".")[0]
            if term_is_usable(first_line) and len(first_line) <= MAX_TERM:
                _add_term(bucket, first_line, source_id=row_source, seen_at=seen)
        if record.get("excerpt"):
            excerpt = normalize_term(str(record["excerpt"]))
            first_line = excerpt.split("。")[0].split(".")[0]
            if term_is_usable(first_line) and len(first_line) <= MAX_TERM:
                _add_term(bucket, first_line, source_id=row_source, seen_at=seen)


def _extract_social_observations(
    doc: Mapping[str, Any], bucket: dict[str, dict[str, Any]]
) -> None:
    generated = _iso_or_none(doc.get("generated_at"))
    for record in doc.get("observations") or []:
        if not isinstance(record, Mapping):
            continue
        source = str(record.get("source_id") or "social-observations")
        seen = _iso_or_none(record.get("published_at") or record.get("first_observed_at")) or generated
        if record.get("title"):
            title = normalize_term(str(record["title"]))
            first = title.split("。")[0].split(".")[0]
            _add_term(bucket, first if term_is_usable(first) else title, source_id=source, seen_at=seen)


def _extract_hot_boards(doc: Mapping[str, Any], bucket: dict[str, dict[str, Any]]) -> None:
    generated = _iso_or_none(doc.get("generated_at"))
    for record in doc.get("observations") or []:
        if not isinstance(record, Mapping) or not record.get("title"):
            continue
        source = str(record.get("source") or "public-hot-boards")
        _add_term(
            bucket,
            str(record["title"]),
            source_id=source if source.startswith("public-hot-boards") else "public-hot-boards",
            seen_at=_iso_or_none(record.get("first_seen") or record.get("detected_at"))
            or generated,
        )


def _wire_targets(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for event in doc.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        event_id = event.get("event_id")
        headline = normalize_term(str(event.get("headline") or ""))
        dek = normalize_term(str(event.get("dek") or ""))
        if type(event_id) is not str or not _EVENT_ID.fullmatch(event_id):
            continue
        if not headline:
            continue
        targets.append({"event_id": event_id, "headline": headline, "dek": dek})
    return targets


def _live_wire_targets(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for record in doc.get("observations") or []:
        if not isinstance(record, Mapping):
            continue
        title = normalize_term(str(record.get("title") or ""))
        text = normalize_term(str(record.get("text") or ""))
        if not title:
            continue
        targets.append(
            {
                "event_id": None,
                "headline": title,
                "dek": text,
                "live": True,
            }
        )
    return targets


def _title_targets(doc: Mapping[str, Any], *, kind: str) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    generated = _iso_or_none(doc.get("generated_at"))
    pages = doc.get("pages")
    if isinstance(pages, Mapping):
        for url, page in pages.items():
            if not isinstance(page, Mapping):
                continue
            title = normalize_term(str(page.get("term") or page.get("title") or ""))
            if not title:
                continue
            targets.append(
                {
                    "kind": kind,
                    "title": title,
                    "url": str(url),
                    "last_alive": _iso_or_none(page.get("last_confirmed_alive")) or generated,
                }
            )
    for record in doc.get("observations") or []:
        if not isinstance(record, Mapping):
            continue
        title = normalize_term(str(record.get("title") or ""))
        if not title:
            continue
        targets.append(
            {
                "kind": kind,
                "title": title,
                "url": str(record.get("url") or record.get("source_url") or ""),
                "last_alive": _iso_or_none(
                    record.get("last_confirmed_alive") or record.get("last_seen")
                )
                or generated,
            }
        )
    return targets


def _same_string_in(term: str, haystack: str) -> bool:
    return bool(term) and bool(haystack) and term in haystack


def _extract_weibo_terms(doc: Mapping[str, Any], bucket: dict[str, dict[str, Any]]) -> None:
    generated = _iso_or_none(doc.get("generated_at"))
    for row in doc.get("terms") or []:
        if not isinstance(row, Mapping) or not row.get("title"):
            continue
        first = row.get("first_seen")
        last = row.get("last_seen")
        seen = generated
        if isinstance(first, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", first):
            seen = f"{first}T00:00:00Z"
        elif isinstance(last, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", last):
            seen = f"{last}T00:00:00Z"
        _add_term(
            bucket,
            str(row["title"]),
            source_id="weibo-hotsearch-terms",
            seen_at=_iso_or_none(seen) or generated,
        )
    for day in doc.get("pinned_headlines") or []:
        if not isinstance(day, Mapping):
            continue
        date = str(day.get("date") or "")
        seen = f"{date}T00:00:00Z" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else generated
        for title in day.get("pinned") or []:
            _add_term(
                bucket,
                str(title),
                source_id="weibo-hotsearch-terms",
                seen_at=_iso_or_none(seen) or generated,
            )


def extract_spreading_terms(inputs: Mapping[str, Mapping[str, Any] | None]) -> dict[str, dict[str, Any]]:
    bucket: dict[str, dict[str, Any]] = {}
    weibo = inputs.get("weibo-hotsearch")
    if isinstance(weibo, Mapping):
        _extract_weibo(weibo, bucket)
    terms = inputs.get("weibo-hotsearch-terms")
    if isinstance(terms, Mapping):
        _extract_weibo_terms(terms, bucket)
    boards = inputs.get("public-hot-boards")
    if isinstance(boards, Mapping):
        _extract_hot_boards(boards, bucket)
    social = inputs.get("social-observations")
    if isinstance(social, Mapping):
        _extract_social_observations(social, bucket)
    telegram = inputs.get("telegram-public-channels")
    if isinstance(telegram, Mapping):
        _extract_observations(telegram, bucket, source_id="telegram-public-channels")
    return bucket


def match_capture(
    term: str, inputs: Mapping[str, Mapping[str, Any] | None]
) -> dict[str, list[Any]]:
    wire_ids: list[str] = []
    official: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    for key in ("newswire", "news-wire-live"):
        doc = inputs.get(key)
        if not isinstance(doc, Mapping):
            continue
        targets = _wire_targets(doc) if key == "newswire" else _live_wire_targets(doc)
        for target in targets:
            haystack = f"{target['headline']}\n{target['dek']}"
            if not _same_string_in(term, haystack):
                continue
            event_id = target.get("event_id")
            if type(event_id) is str and event_id not in wire_ids:
                wire_ids.append(event_id)
            elif event_id is None and f"live:{target['headline'][:80]}" not in wire_ids:
                # Live projection has no event id; record the matched headline token.
                wire_ids.append(f"live:{hashlib.sha256(target['headline'].encode('utf-8')).hexdigest()[:24]}")
    official_doc = inputs.get("official-first-seen")
    if isinstance(official_doc, Mapping):
        for target in _title_targets(official_doc, kind="official"):
            if _same_string_in(term, target["title"]):
                official.append(
                    {
                        "title": target["title"][:MAX_TERM],
                        "last_alive": target["last_alive"],
                    }
                )
    ledger_doc = inputs.get("public-deletion-ledgers")
    if isinstance(ledger_doc, Mapping):
        for target in _title_targets(ledger_doc, kind="ledger"):
            if _same_string_in(term, target["title"]):
                ledger.append({"title": target["title"][:MAX_TERM]})
    return {
        "wire_event_ids": wire_ids[:32],
        "official_page_last_alive": official[:32],
        "ledger_hits": ledger[:32],
    }


def _input_status(inputs: Mapping[str, Mapping[str, Any] | None]) -> dict[str, str]:
    status = {}
    for name in ALL_INPUTS:
        value = inputs.get(name)
        status[name] = "present" if isinstance(value, Mapping) and value else "missing"
    return status


def _news_story(
    rows: list[dict[str, Any]], *, generated_at: str, abstained: bool
) -> dict[str, Any] | None:
    publishable = [
        row
        for row in rows
        if row["disposition"] in {"circulating-unverified", "matched-to-wire", "matched-to-official-page"}
        and not row["names_a_person"]
    ]
    if abstained or not publishable:
        return None
    matched = sum(row["disposition"] != "circulating-unverified" for row in publishable)
    body = [
        f"{len(publishable)} public-board term{'s' if len(publishable) != 1 else ''} "
        "were extracted from listed public surfaces.",
        f"{matched} matched a registered newswire headline/dek or an already-stored "
        "official page title.",
        DISCLAIMER,
    ]
    return {
        "headline": "Public-board terms now circulating",
        "dek": DISCLAIMER,
        "body": body,
        "automatic_publication": True,
        "relation": RELATION,
        "disclaimer": DISCLAIMER,
    }


def build_social_spread(
    inputs: Mapping[str, Mapping[str, Any] | None],
    *,
    generated_at: str,
) -> dict[str, Any]:
    """Join public spreading terms to stored capture. Missing collectors abstain."""

    generated_at = _timestamp(generated_at, "generated_at")
    status_map = _input_status(inputs)
    spreading_present = any(status_map[name] == "present" for name in REQUIRED_SPREADING)
    rows: list[dict[str, Any]] = []
    refusals: list[dict[str, str]] = []
    n_abstained = 0

    if not spreading_present:
        n_abstained = 1
        document = _assemble(
            generated_at=generated_at,
            status="abstain",
            status_map=status_map,
            rows=[],
            refusals=[],
            n_abstained=n_abstained,
            news_story=None,
        )
        validate_social_spread(document)
        return document

    extracted = extract_spreading_terms(inputs)
    for term, raw in sorted(extracted.items(), key=lambda item: (-len(item[1]["source_ids"]), item[0])):
        source_ids = sorted(raw["source_ids"])
        if is_official_missing_whisper(term, source_ids) or (
            is_whisper_only(source_ids) and looks_like_named_person_package(term)
        ):
            refusals.append(
                {
                    "term_class": "whisper-only-named-person",
                    "reason": refuse_official_missing_whisper(),
                }
            )
            continue
        matches = match_capture(term, inputs)
        names = looks_like_named_person_package(term)
        if names and is_whisper_only(source_ids):
            refusals.append(
                {
                    "term_class": "whisper-only-named-person",
                    "reason": refuse_official_missing_whisper(),
                }
            )
            continue
        if names and not (
            matches["wire_event_ids"]
            or matches["official_page_last_alive"]
            or matches["ledger_hits"]
        ):
            refusals.append(
                {
                    "term_class": "named-person-without-capture",
                    "reason": (
                        "A person-status term with no registered wire, official-page, "
                        f"or ledger title match is not published. {DISCLAIMER}"
                    ),
                }
            )
            continue
        if matches["official_page_last_alive"]:
            disposition = "matched-to-official-page"
        elif matches["wire_event_ids"]:
            disposition = "matched-to-wire"
        elif not is_whisper_only(source_ids):
            disposition = "circulating-unverified"
        else:
            n_abstained += 1
            continue
        rows.append(
            {
                "term": term,
                "topic": term,
                "disposition": disposition,
                "relation": RELATION,
                "disclaimer": DISCLAIMER,
                "spreading": {
                    "source_ids": source_ids,
                    "n_surfaces": len(source_ids),
                    "first_seen": raw["first_seen"],
                    "last_seen": raw["last_seen"],
                },
                "matches": matches,
                "names_a_person": names,
                "automatic_publication": not names,
                "human_review_required": names,
            }
        )
        if len(rows) >= MAX_ROWS:
            break

    seen_reasons: set[str] = set()
    unique_refusals: list[dict[str, str]] = []
    for refusal in refusals:
        key = f"{refusal['term_class']}|{refusal['reason']}"
        if key in seen_reasons:
            continue
        seen_reasons.add(key)
        unique_refusals.append(refusal)

    document = _assemble(
        generated_at=generated_at,
        status="live" if rows else "abstain",
        status_map=status_map,
        rows=rows,
        refusals=unique_refusals,
        n_abstained=n_abstained if rows else max(n_abstained, int(not rows)),
        news_story=_news_story(rows, generated_at=generated_at, abstained=not rows),
    )
    validate_social_spread(document)
    return document


def _assemble(
    *,
    generated_at: str,
    status: str,
    status_map: Mapping[str, str],
    rows: list[dict[str, Any]],
    refusals: list[dict[str, str]],
    n_abstained: int,
    news_story: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "job_name": JOB_NAME,
        "status": status,
        "source": (
            "Public Weibo hot-search titles (join summary and the term-level "
            "board dump), public aggregate hot-board titles, "
            "in-tree public Telegram channel titles/excerpts "
            "(DragonDenWhispers, DragonDenCyber, DragonDenBorderlands), and "
            "closed-registry social observation titles, joined to already-stored "
            "China-scoped newswire headlines/deks, official-first-seen page titles, "
            "and public deletion-ledger titles. Wayback is context only."
        ),
        "method": (
            "Extract spreading terms from the Weibo board-title dump, public "
            "hot boards, and retained channel excerpts. Match only when the "
            "same string already appears in a registered newswire headline/dek, "
            "an official last-alive page title, or a deletion-ledger title. "
            "Whisper-only names do not emit a person package. Sense-gated "
            "ordinary 失联 accidents stay topic rows. Missing required "
            "spreading collectors abstain."
        ),
        "scope": (
            "Public Chinese attention surfaces already collected outside the "
            "firewall. Not a missing-person desk. Not Dragon Whispers. Not private "
            "social graph collection."
        ),
        "relation": RELATION,
        "disclaimer": DISCLAIMER,
        "publication_policy": dict(PUBLICATION_POLICY),
        "input_status": dict(status_map),
        "n_rows": len(rows),
        "n_abstained": n_abstained,
        "n_refused": len(refusals),
        "rows": rows,
        "refusals": refusals,
        "news_story": news_story,
    }


def validate_social_spread(document: Mapping[str, Any]) -> None:
    top = _exact(document, _TOP_FIELDS, "social_spread")
    if top["schema_version"] != SCHEMA_VERSION:
        raise SocialSpreadError("unsupported social-spread schema")
    if top["job_name"] != JOB_NAME:
        raise SocialSpreadError("job_name must remain social-spread")
    _timestamp(top["generated_at"], "generated_at")
    if top["status"] not in {"live", "abstain"}:
        raise SocialSpreadError("status must be live or abstain")
    _text(top["source"], "source", maximum=800)
    _text(top["method"], "method", maximum=800)
    _text(top["scope"], "scope", maximum=500)
    if top["relation"] != RELATION:
        raise SocialSpreadError("relation must remain topic-surface-only")
    if top["disclaimer"] != DISCLAIMER:
        raise SocialSpreadError("required disclaimer was weakened")
    policy = _exact(top["publication_policy"], frozenset(PUBLICATION_POLICY), "publication_policy")
    if policy != PUBLICATION_POLICY:
        raise SocialSpreadError("publication policy broadens the public boundary")
    status_map = top["input_status"]
    if type(status_map) is not dict or set(status_map) != set(ALL_INPUTS):
        raise SocialSpreadError("input_status must account for every declared input")
    for name, state in status_map.items():
        if state not in INPUT_STATES:
            raise SocialSpreadError(f"input_status.{name} is invalid")
    rows = top["rows"]
    if type(rows) is not list or len(rows) > MAX_ROWS:
        raise SocialSpreadError("rows must be a bounded array")
    if top["n_rows"] != len(rows):
        raise SocialSpreadError("n_rows does not match rows")
    if type(top["n_abstained"]) is not int or top["n_abstained"] < 0:
        raise SocialSpreadError("n_abstained is invalid")
    refusals = top["refusals"]
    if type(refusals) is not list or len(refusals) > MAX_ROWS:
        raise SocialSpreadError("refusals must be a bounded array")
    if top["n_refused"] != len(refusals):
        raise SocialSpreadError("n_refused does not match refusals")
    if (top["status"] == "live") != bool(rows):
        raise SocialSpreadError("status does not match row availability")
    for index, raw in enumerate(refusals):
        path = f"refusals[{index}]"
        if type(raw) is not dict or set(raw) != {"term_class", "reason"}:
            raise SocialSpreadError(f"{path} is invalid")
        _text(raw["term_class"], f"{path}.term_class", maximum=80)
        _text(raw["reason"], f"{path}.reason", maximum=500)
        if DISCLAIMER not in raw["reason"]:
            raise SocialSpreadError(f"{path} must repeat the required disclaimer")
        if any(marker in raw["reason"].casefold() for marker in ("named_party", "accused")):
            raise SocialSpreadError(f"{path} uses a forbidden allegation field")
    seen_terms: set[str] = set()
    for index, raw in enumerate(rows):
        path = f"rows[{index}]"
        row = _exact(raw, _ROW_FIELDS, path)
        term = _text(row["term"], f"{path}.term", maximum=MAX_TERM)
        if term in seen_terms:
            raise SocialSpreadError("duplicate spreading term")
        seen_terms.add(term)
        if row["topic"] != term:
            raise SocialSpreadError(f"{path}.topic must repeat the spreading term")
        if row["disposition"] not in DISPOSITIONS:
            raise SocialSpreadError(f"{path}.disposition is invalid")
        if row["relation"] != RELATION:
            raise SocialSpreadError(f"{path}.relation must remain topic-surface-only")
        if row["disclaimer"] != DISCLAIMER:
            raise SocialSpreadError(f"{path} dropped the required disclaimer")
        spreading = _exact(row["spreading"], _SPREAD_FIELDS, f"{path}.spreading")
        source_ids = spreading["source_ids"]
        if type(source_ids) is not list or not source_ids or source_ids != sorted(set(source_ids)):
            raise SocialSpreadError(f"{path}.spreading.source_ids is invalid")
        for source_id in source_ids:
            if type(source_id) is not str or not _SOURCE_ID.fullmatch(source_id):
                raise SocialSpreadError(f"{path} has an unsafe source_id")
            if source_id.startswith("telegram-public-channels:"):
                handle = source_id.split(":", 1)[1]
                if handle not in ALLOWED_TELEGRAM_HANDLES:
                    raise SocialSpreadError("unknown Telegram handle")
        if spreading["n_surfaces"] != len(source_ids):
            raise SocialSpreadError(f"{path}.spreading.n_surfaces is inconsistent")
        _timestamp(spreading["first_seen"], f"{path}.spreading.first_seen")
        _timestamp(spreading["last_seen"], f"{path}.spreading.last_seen")
        matches = _exact(row["matches"], _MATCH_FIELDS, f"{path}.matches")
        for field in ("wire_event_ids", "official_page_last_alive", "ledger_hits"):
            if type(matches[field]) is not list or len(matches[field]) > 32:
                raise SocialSpreadError(f"{path}.matches.{field} must be bounded")
        for event_id in matches["wire_event_ids"]:
            if type(event_id) is not str or not (
                _EVENT_ID.fullmatch(event_id) or event_id.startswith("live:")
            ):
                raise SocialSpreadError(f"{path} has an invalid wire event id")
        for hit in matches["official_page_last_alive"]:
            if type(hit) is not dict or set(hit) != {"title", "last_alive"}:
                raise SocialSpreadError(f"{path} official hit is invalid")
            _text(hit["title"], f"{path}.matches.official.title", maximum=MAX_TERM)
            _timestamp(hit["last_alive"], f"{path}.matches.official.last_alive")
        for hit in matches["ledger_hits"]:
            if type(hit) is not dict or set(hit) != {"title"}:
                raise SocialSpreadError(f"{path} ledger hit is invalid")
            _text(hit["title"], f"{path}.matches.ledger.title", maximum=MAX_TERM)
        if type(row["names_a_person"]) is not bool:
            raise SocialSpreadError(f"{path}.names_a_person must be boolean")
        if type(row["automatic_publication"]) is not bool:
            raise SocialSpreadError(f"{path}.automatic_publication must be boolean")
        if type(row["human_review_required"]) is not bool:
            raise SocialSpreadError(f"{path}.human_review_required must be boolean")
        if row["names_a_person"]:
            if row["automatic_publication"] is not False:
                raise SocialSpreadError("person-name packages cannot auto-publish")
            if row["human_review_required"] is not True:
                raise SocialSpreadError("person-name packages require human review")
        if row["disposition"] == "matched-to-wire" and not matches["wire_event_ids"]:
            raise SocialSpreadError(f"{path} claims a wire match without event ids")
        if (
            row["disposition"] == "matched-to-official-page"
            and not matches["official_page_last_alive"]
        ):
            raise SocialSpreadError(f"{path} claims an official-page match without hits")
        if is_whisper_only(source_ids) and row["names_a_person"]:
            raise SocialSpreadError("whisper-only name emitted a person package")
        if looks_like_named_person_package(term) and row["disposition"] not in {
            "matched-to-wire",
            "matched-to-official-page",
        }:
            raise SocialSpreadError("named-person term escaped without a capture match")
    story = top["news_story"]
    if story is None:
        pass
    else:
        item = _exact(story, _STORY_FIELDS, "news_story")
        _text(item["headline"], "news_story.headline", maximum=180)
        if item["dek"] != DISCLAIMER or item["disclaimer"] != DISCLAIMER:
            raise SocialSpreadError("news story dropped the required disclaimer")
        body = item["body"]
        if type(body) is not list or not 2 <= len(body) <= 8:
            raise SocialSpreadError("news_story.body must be a short template list")
        for line in body:
            _text(line, "news_story.body", maximum=400)
        if DISCLAIMER not in body:
            raise SocialSpreadError("news story body must include the disclaimer")
        if item["relation"] != RELATION:
            raise SocialSpreadError("news story relation must remain topic-surface-only")
        if item["automatic_publication"] is not True:
            raise SocialSpreadError("news story is only emitted for topic-only rows")
        if any(row["names_a_person"] for row in rows):
            # A mixed document may still carry a topic-only story, but never a
            # person-name headline.
            if "missing" in item["headline"].casefold() or "失联" in item["headline"]:
                raise SocialSpreadError("news story names a missing-person finding")
    if top["status"] == "abstain" and rows:
        raise SocialSpreadError("abstain documents cannot carry rows")
    canonical_json_bytes(document)


SAMPLE_ROW = {
    "term": "杭州暴雨",
    "topic": "杭州暴雨",
    "disposition": "matched-to-wire",
    "relation": RELATION,
    "disclaimer": DISCLAIMER,
    "spreading": {
        "source_ids": ["weibo-hotsearch"],
        "n_surfaces": 1,
        "first_seen": "2026-08-19T00:00:00Z",
        "last_seen": "2026-08-20T02:06:13Z",
    },
    "matches": {
        "wire_event_ids": ["event-" + "ab" * 12],
        "official_page_last_alive": [],
        "ledger_hits": [],
    },
    "names_a_person": False,
    "automatic_publication": True,
    "human_review_required": False,
}


__all__ = [
    "ALLOWED_TELEGRAM_HANDLES",
    "DISCLAIMER",
    "JOB_NAME",
    "OFFICIAL_MISSING_WHISPER_REFUSAL",
    "PUBLICATION_POLICY",
    "RELATION",
    "SAMPLE_ROW",
    "SCHEMA_VERSION",
    "SECONDARY_RELATION",
    "SocialSpreadError",
    "build_social_spread",
    "canonical_json_bytes",
    "contains_person_status",
    "extract_spreading_terms",
    "is_official_missing_whisper",
    "is_whisper_only",
    "looks_like_named_person_package",
    "match_capture",
    "normalize_term",
    "refuse_official_missing_whisper",
    "validate_social_spread",
]
