"""Fused public-board term dump — every answering board, titles and ranks only.

Built from keyless GitHub archives, live JSON boards already in tree,
Chinese Wikipedia most-viewed, and optional gazetteer RC titles. Each
board is a curated/censored surface. This is not uncensored public opinion.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from collectors.public_board_archives import FORBIDDEN_PAYLOAD_KEYS, normalize_title


SCHEMA_VERSION = "palimpsest-public-board-terms.v1"
JOB_NAME = "public-board-terms"
BOARD_DISCLOSURE = (
    "Every hot board is itself a curated or censored surface. This dump is "
    "the record of permitted attention, not uncensored public opinion."
)
PUBLICATION_POLICY = {
    "automatic_publication": True,
    "named_person_packages_auto_published": False,
    "counts_as_corroboration": False,
    "user_ids_included": False,
    "post_bodies_included": False,
    "private_profiles_included": False,
    "follower_graphs_included": False,
}

MAX_TITLES = 20_000
MAX_TITLE = 180

SAMPLE_TERM_ROW = {
    "board": "baidu",
    "title": "杭州暴雨",
    "title_sha256": hashlib.sha256(
        unicodedata.normalize("NFC", "杭州暴雨").encode("utf-8")
    ).hexdigest(),
    "best_rank": 3,
    "pinned": False,
    "first_seen": "2026-08-19",
    "last_seen": "2026-08-20",
    "days_present": 2,
    "appearances": 2,
    "source_archives": ["iiecho1-hot-searches:baidu"],
    "role": "hot-board",
}

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "generated_at",
        "job_name",
        "status",
        "source",
        "method",
        "scope",
        "disclaimer",
        "publication_policy",
        "window_days",
        "n_titles",
        "n_boards",
        "n_boards_ok",
        "n_sightings",
        "n_ddti_joined",
        "regimes",
        "boards",
        "terms",
        "ddti_join",
        "skipped",
        "abstained",
    }
)
_TERM_FIELDS = frozenset(SAMPLE_TERM_ROW)
_BOARD_FIELDS = frozenset(
    {
        "name",
        "board",
        "url",
        "http_status",
        "n_items",
        "status",
        "note",
        "license",
        "role",
    }
)


class PublicBoardTermsError(ValueError):
    """The fused board dump crossed its evidence boundary."""


def title_identity(title: str) -> str:
    return hashlib.sha256(unicodedata.normalize("NFC", title).encode("utf-8")).hexdigest()


def term_identity(board: str, title: str) -> tuple[str, str]:
    return board, unicodedata.normalize("NFC", title)


def canonical_json_bytes(value: Any) -> bytes:
    def reject(node: Any, path: str = "public_board_terms") -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise PublicBoardTermsError(f"{path} contains a non-finite number")
        if isinstance(node, Mapping):
            for key, child in node.items():
                if type(key) is not str:
                    raise PublicBoardTermsError(f"{path} contains a non-string key")
                folded = str(key).casefold()
                if folded in FORBIDDEN_PAYLOAD_KEYS:
                    raise PublicBoardTermsError(f"{path} contains forbidden field {key!r}")
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


def aggregate_terms(sightings: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in sightings:
        if not isinstance(row, Mapping):
            continue
        title = normalize_title(str(row.get("title") or ""))
        board = str(row.get("board") or "").strip()
        date = str(row.get("date") or "")
        if not title or not board or len(title) > MAX_TITLE:
            continue
        key = term_identity(board, title)
        rec = seen.get(key)
        rank = row.get("rank")
        archives = [
            str(name)
            for name in (row.get("source_archives") or [])
            if isinstance(name, str) and name
        ]
        if rec is None:
            seen[key] = {
                "board": board,
                "title": title,
                "title_sha256": title_identity(title),
                "best_rank": rank if isinstance(rank, int) else None,
                "pinned": bool(row.get("pinned")),
                "first_seen": date,
                "last_seen": date,
                "days_present": 1,
                "appearances": 1,
                "source_archives": sorted(set(archives)),
                "role": str(row.get("role") or "hot-board"),
                "_dates": {date} if date else set(),
            }
            continue
        rec["appearances"] += 1
        if date:
            rec["_dates"].add(date)
            if not rec["first_seen"] or date < rec["first_seen"]:
                rec["first_seen"] = date
            if date > rec["last_seen"]:
                rec["last_seen"] = date
        rec["pinned"] = rec["pinned"] or bool(row.get("pinned"))
        rec["source_archives"] = sorted(set(rec["source_archives"]) | set(archives))
        if isinstance(rank, int) and (
            rec["best_rank"] is None or rank < rec["best_rank"]
        ):
            rec["best_rank"] = rank
    out = []
    for rec in seen.values():
        rec["days_present"] = len(rec.pop("_dates")) or 1
        out.append(rec)
    out.sort(
        key=lambda row: (
            row["board"],
            row["best_rank"] is None,
            row["best_rank"] or 0,
            row["title"],
        )
    )
    return out[:MAX_TITLES]


def join_ddti(ddti_terms: list[Mapping[str, Any]] | None, terms: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    titles = [str(row.get("title") or "") for row in terms]
    blob = "\n".join(titles)
    joined: list[dict[str, Any]] = []
    for row in ddti_terms or []:
        if not isinstance(row, Mapping):
            continue
        term = str(row.get("term") or "").strip()
        if not term:
            continue
        hits = [title for title in titles if term in title]
        joined.append(
            {
                "term": term,
                "regime": "contained_visible" if hits else "suppressed_invisible",
                "n_title_hits": len(hits),
            }
        )
        if not hits and term not in blob:
            pass
    return joined


def build_public_board_terms(
    collected: Mapping[str, Any] | None,
    *,
    generated_at: str,
    ddti_terms: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    generated_at = _timestamp(generated_at)
    collected = collected or {}
    sightings = list(collected.get("sightings") or [])
    boards = [_board_row(row) for row in (collected.get("boards") or [])]
    terms = aggregate_terms(sightings)
    joined = join_ddti(list(ddti_terms or []), terms)
    skipped = [row for row in boards if row["status"] == "skipped"]
    abstained = [
        row for row in boards if row["status"] in {"silent", "login_walled", "unreachable"}
    ]
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "job_name": JOB_NAME,
        "status": "live" if terms else "abstain",
        "source": (
            "Keyless public GitHub board archives (justjavac/weibo-trending-hot-search, "
            "lonnyzhang423/*-hot-hub MIT markdown day files, iiecho1/hot_searches_for_apps "
            "China-relevant boards), live JSON boards from config/public_hot_boards.json, "
            "zh.wikipedia most-viewed, and in-tree wikipedia-gazetteer-rc titles. "
            "FreeWeChat is a recovered-listing candidate and abstains without a public index."
        ),
        "method": (
            "Fetch each candidate archive URL. Parse titles and ranks only. "
            "Dedup Weibo on (board, title, day) so justjavac and weibo-hot-hub "
            "do not double-count. Login wall / captcha / empty / 暂无数据 is silent. "
            + BOARD_DISCLOSURE
        ),
        "scope": (
            "Public board titles and ranks only. No user ids, follower graphs, "
            "post bodies, comments, DMs, location, or private profiles."
        ),
        "disclaimer": BOARD_DISCLOSURE,
        "publication_policy": dict(PUBLICATION_POLICY),
        "window_days": list(collected.get("window_days") or []),
        "n_titles": len(terms),
        "n_boards": len(boards),
        "n_boards_ok": sum(1 for row in boards if row["status"] == "ok"),
        "n_sightings": len(sightings),
        "n_ddti_joined": len(joined),
        "regimes": {
            "contained_visible": sum(1 for row in joined if row["regime"] == "contained_visible"),
            "suppressed_invisible": sum(
                1 for row in joined if row["regime"] == "suppressed_invisible"
            ),
        },
        "boards": boards,
        "terms": terms,
        "ddti_join": joined,
        "skipped": [{"name": row["name"], "reason": row["note"]} for row in skipped],
        "abstained": [
            {"name": row["name"], "status": row["status"], "note": row["note"]}
            for row in abstained
        ],
    }
    validate_public_board_terms(document)
    return document


def _board_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": str(raw.get("name") or ""),
        "board": str(raw.get("board") or ""),
        "url": str(raw.get("url") or ""),
        "http_status": raw.get("http_status") if raw.get("http_status") is not None else 0,
        "n_items": int(raw.get("n_items") or 0),
        "status": str(raw.get("status") or "silent"),
        "note": str(raw.get("note") or ""),
        "license": str(raw.get("license") or ""),
        "role": str(raw.get("role") or "hot-board"),
    }


def _timestamp(value: str) -> str:
    if type(value) is not str:
        raise PublicBoardTermsError("generated_at must be canonical UTC")
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return value


def validate_public_board_terms(document: Mapping[str, Any]) -> None:
    if type(document) is not dict or set(document) != _TOP_FIELDS:
        raise PublicBoardTermsError("document does not use its exact field set")
    if document["schema_version"] != SCHEMA_VERSION:
        raise PublicBoardTermsError("unsupported public-board-terms schema")
    if document["job_name"] != JOB_NAME:
        raise PublicBoardTermsError("job_name must remain public-board-terms")
    _timestamp(document["generated_at"])
    if document["status"] not in {"live", "abstain"}:
        raise PublicBoardTermsError("status must be live or abstain")
    if document["disclaimer"] != BOARD_DISCLOSURE:
        raise PublicBoardTermsError("board disclosure was weakened")
    if document["publication_policy"] != PUBLICATION_POLICY:
        raise PublicBoardTermsError("publication policy broadens the public boundary")
    terms = document["terms"]
    if type(terms) is not list or len(terms) > MAX_TITLES:
        raise PublicBoardTermsError("terms must be a bounded array")
    if document["n_titles"] != len(terms):
        raise PublicBoardTermsError("n_titles does not match terms")
    if (document["status"] == "live") != bool(terms):
        raise PublicBoardTermsError("status does not match title availability")
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(terms):
        if type(raw) is not dict or set(raw) != _TERM_FIELDS:
            raise PublicBoardTermsError(f"terms[{index}] does not use its exact field set")
        title = raw["title"]
        board = raw["board"]
        key = (board, title)
        if type(title) is not str or not title or key in seen:
            raise PublicBoardTermsError(f"terms[{index}].title is invalid")
        seen.add(key)
        if raw["title_sha256"] != title_identity(title):
            raise PublicBoardTermsError(f"terms[{index}].title_sha256 is inconsistent")
        if raw["best_rank"] is not None and (
            type(raw["best_rank"]) is not int or raw["best_rank"] < 1
        ):
            raise PublicBoardTermsError(f"terms[{index}].best_rank is invalid")
        if type(raw["pinned"]) is not bool:
            raise PublicBoardTermsError(f"terms[{index}].pinned must be boolean")
        if type(raw["source_archives"]) is not list or not raw["source_archives"]:
            raise PublicBoardTermsError(f"terms[{index}].source_archives is invalid")
    for row in document["boards"]:
        if type(row) is not dict or set(row) != _BOARD_FIELDS:
            raise PublicBoardTermsError("boards rows must use the exact field set")
        if row["status"] == "ok" and row["n_items"] == 0:
            raise PublicBoardTermsError("an ok board cannot be a zero")
    for row in document["ddti_join"]:
        if not isinstance(row, Mapping):
            raise PublicBoardTermsError("ddti_join rows must be objects")
        if row.get("regime") == "suppressed_invisible" and not row.get("term"):
            raise PublicBoardTermsError("suppressed_invisible requires a DDTI term")
        if row.get("regime") not in {"contained_visible", "suppressed_invisible"}:
            raise PublicBoardTermsError("unknown DDTI regime")
    if document["n_ddti_joined"] != len(document["ddti_join"]):
        raise PublicBoardTermsError("n_ddti_joined is inconsistent")
    canonical_json_bytes(document)


def write_public_board_terms(
    collected: Mapping[str, Any] | None,
    *,
    generated_at: str,
    readings: Path,
    ddti_terms: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    document = build_public_board_terms(
        collected,
        generated_at=generated_at,
        ddti_terms=ddti_terms,
    )
    if document["status"] != "live":
        return None
    readings.mkdir(parents=True, exist_ok=True)
    latest = readings / "public-board-terms-latest.json"
    history = readings / "public-board-terms-history.jsonl"
    latest.write_bytes(canonical_json_bytes(document))
    with history.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "generated_at": document["generated_at"],
                    "n_titles": document["n_titles"],
                    "n_boards_ok": document["n_boards_ok"],
                    "contained_visible": document["regimes"]["contained_visible"],
                    "suppressed_invisible": document["regimes"]["suppressed_invisible"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
    return document


__all__ = [
    "BOARD_DISCLOSURE",
    "JOB_NAME",
    "PUBLICATION_POLICY",
    "SAMPLE_TERM_ROW",
    "SCHEMA_VERSION",
    "PublicBoardTermsError",
    "aggregate_terms",
    "build_public_board_terms",
    "canonical_json_bytes",
    "title_identity",
    "validate_public_board_terms",
    "write_public_board_terms",
]
