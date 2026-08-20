"""Public Weibo hot-search BOARD dump — every distinct title in the window.

This is the granular permitted-attention archive behind the join summary.
It is built from the same keyless MIT justjavac/weibo-trending-hot-search
ingest as ``collectors/weibo_hotsearch.py``. No Weibo account, no in-China
scrape, no user timelines, no post bodies, no follower graphs.

The board is itself a censored surface (置顶 pin, 撤热搜 withdrawals). This
dump is not uncensored public opinion.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from collectors.weibo_hotsearch import join_ddti, pinned_series, withdrawal_candidates


SCHEMA_VERSION = "palimpsest-weibo-hotsearch-terms.v1"
JOB_NAME = "weibo-hotsearch-terms"
BOARD_DISCLOSURE = (
    "The hot-search board is itself a censored surface (curated 置顶 slot, "
    "on-command withdrawals). This dump is the record of permitted attention, "
    "not uncensored public opinion."
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

FORBIDDEN_FIELDS = frozenset(
    {
        "uid",
        "user_id",
        "userid",
        "weibo_uid",
        "follower",
        "followers",
        "following",
        "location",
        "lat",
        "lon",
        "dm",
        "dms",
        "comment_id",
        "mid",
        "mblog",
        "wechat",
        "weixin",
    }
)

MAX_TITLES = 20_000
MAX_TITLE = 180

SAMPLE_TERM_ROW = {
    "title": "杭州暴雨",
    "title_sha256": hashlib.sha256(
        unicodedata.normalize("NFC", "杭州暴雨").encode("utf-8")
    ).hexdigest(),
    "best_rank": 7,
    "pinned": False,
    "first_seen": "2026-08-19",
    "last_seen": "2026-08-20",
    "days_present": 2,
    "appearances": 2,
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
        "board_entries",
        "n_titles",
        "n_pinned_days",
        "n_withdrawal_candidates",
        "n_ddti_joined",
        "regimes",
        "terms",
        "pinned_headlines",
        "withdrawal_watch",
        "ddti_join",
    }
)
_TERM_FIELDS = frozenset(SAMPLE_TERM_ROW)


class WeiboHotsearchTermsError(ValueError):
    """The public board dump crossed its evidence boundary."""


def title_identity(title: str) -> str:
    return hashlib.sha256(unicodedata.normalize("NFC", title).encode("utf-8")).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    def reject(node: Any, path: str = "weibo_hotsearch_terms") -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise WeiboHotsearchTermsError(f"{path} contains a non-finite number")
        if isinstance(node, Mapping):
            for key, child in node.items():
                if type(key) is not str:
                    raise WeiboHotsearchTermsError(f"{path} contains a non-string key")
                if str(key).casefold() in FORBIDDEN_FIELDS:
                    raise WeiboHotsearchTermsError(f"{path} contains forbidden field {key!r}")
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


def aggregate_board_titles(days: Mapping[str, list[dict]]) -> list[dict[str, Any]]:
    """One row per distinct board title in the parsed window."""

    seen: dict[str, dict[str, Any]] = {}
    for date in sorted(days):
        rows = days[date]
        if not isinstance(rows, list):
            continue
        for entry in rows:
            if not isinstance(entry, Mapping):
                continue
            title = unicodedata.normalize("NFC", str(entry.get("title") or "").strip())
            if not title or len(title) > MAX_TITLE:
                continue
            rec = seen.get(title)
            rank = entry.get("rank")
            pinned = bool(entry.get("pinned"))
            if rec is None:
                seen[title] = {
                    "title": title,
                    "title_sha256": title_identity(title),
                    "best_rank": rank if isinstance(rank, int) else None,
                    "pinned": pinned,
                    "first_seen": date,
                    "last_seen": date,
                    "days_present": 1,
                    "appearances": 1,
                    "_dates": {date},
                }
                continue
            rec["appearances"] += 1
            rec["_dates"].add(date)
            rec["last_seen"] = date
            rec["pinned"] = rec["pinned"] or pinned
            if isinstance(rank, int) and (
                rec["best_rank"] is None or rank < rec["best_rank"]
            ):
                rec["best_rank"] = rank
    out = []
    for rec in seen.values():
        rec["days_present"] = len(rec.pop("_dates"))
        out.append(rec)
    out.sort(key=lambda row: (row["best_rank"] is None, row["best_rank"] or 0, row["title"]))
    return out[:MAX_TITLES]


def build_weibo_hotsearch_terms(
    days: Mapping[str, list[dict]] | None,
    *,
    generated_at: str,
    ddti_terms: list[dict] | None = None,
    sensitive_terms: set[str] | None = None,
) -> dict[str, Any]:
    """Fail-closed board dump. Empty days abstain; they do not invent a quiet week."""

    generated_at = _timestamp(generated_at)
    if not days:
        document = _assemble(
            generated_at=generated_at,
            status="abstain",
            window_days=[],
            board_entries=0,
            terms=[],
            pinned=[],
            withdrawal={
                "baseline_persist_rate": None,
                "candidates": [],
                "sense_filtered": [],
                "note": "window too short — warming up",
            },
            joined=[],
        )
        validate_weibo_hotsearch_terms(document)
        return document

    terms = aggregate_board_titles(days)
    pinned = pinned_series(dict(days))
    joined = join_ddti(list(ddti_terms or []), dict(days))
    withdrawal = dict(
        withdrawal_candidates(
            dict(days),
            sensitive_terms=sensitive_terms
            or {str(row.get("term")) for row in (ddti_terms or []) if row.get("term")},
        )
    )
    withdrawal.setdefault("candidates", [])
    withdrawal.setdefault("sense_filtered", [])
    withdrawal.setdefault("baseline_persist_rate", None)
    document = _assemble(
        generated_at=generated_at,
        status="live" if terms else "abstain",
        window_days=sorted(days),
        board_entries=sum(len(rows) for rows in days.values()),
        terms=terms,
        pinned=pinned,
        withdrawal=withdrawal,
        joined=joined,
    )
    validate_weibo_hotsearch_terms(document)
    return document


def _assemble(
    *,
    generated_at: str,
    status: str,
    window_days: list[str],
    board_entries: int,
    terms: list[dict[str, Any]],
    pinned: list[dict[str, Any]],
    withdrawal: Mapping[str, Any],
    joined: list[dict[str, Any]],
) -> dict[str, Any]:
    suppressed = sum(1 for row in joined if row.get("regime") == "suppressed_invisible")
    contained = sum(1 for row in joined if row.get("regime") == "contained_visible")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "job_name": JOB_NAME,
        "status": status,
        "source": (
            "Sina Weibo hot-search board via the MIT-licensed archive "
            "github.com/justjavac/weibo-trending-hot-search "
            "(hourly captures, per-day union; keyless GitHub raw JSON; "
            "no Weibo account, no in-China scrape)"
        ),
        "method": (
            "Union every distinct board title in the current archive window. "
            "Identity is SHA-256 of the NFC title. Withdrawal candidates come "
            "only from withdrawal_candidates(). DDTI regimes come only from "
            "join_ddti() and therefore require a DDTI term. "
            + BOARD_DISCLOSURE
        ),
        "scope": (
            "Public board titles and ranks only. No Weibo user ids, follower "
            "graphs, post bodies, comments, DMs, location, or private profiles."
        ),
        "disclaimer": BOARD_DISCLOSURE,
        "publication_policy": dict(PUBLICATION_POLICY),
        "window_days": list(window_days),
        "board_entries": board_entries,
        "n_titles": len(terms),
        "n_pinned_days": len(pinned),
        "n_withdrawal_candidates": len(withdrawal.get("candidates") or []),
        "n_ddti_joined": len(joined),
        "regimes": {
            "contained_visible": contained,
            "suppressed_invisible": suppressed,
        },
        "terms": terms,
        "pinned_headlines": pinned,
        "withdrawal_watch": dict(withdrawal),
        "ddti_join": joined,
    }


def _timestamp(value: str) -> str:
    if type(value) is not str:
        raise WeiboHotsearchTermsError("generated_at must be canonical UTC")
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return value


def validate_weibo_hotsearch_terms(document: Mapping[str, Any]) -> None:
    if type(document) is not dict or set(document) != _TOP_FIELDS:
        raise WeiboHotsearchTermsError("document does not use its exact field set")
    if document["schema_version"] != SCHEMA_VERSION:
        raise WeiboHotsearchTermsError("unsupported weibo-hotsearch-terms schema")
    if document["job_name"] != JOB_NAME:
        raise WeiboHotsearchTermsError("job_name must remain weibo-hotsearch-terms")
    _timestamp(document["generated_at"])
    if document["status"] not in {"live", "abstain"}:
        raise WeiboHotsearchTermsError("status must be live or abstain")
    if document["disclaimer"] != BOARD_DISCLOSURE:
        raise WeiboHotsearchTermsError("board disclosure was weakened")
    if document["publication_policy"] != PUBLICATION_POLICY:
        raise WeiboHotsearchTermsError("publication policy broadens the public boundary")
    terms = document["terms"]
    if type(terms) is not list or len(terms) > MAX_TITLES:
        raise WeiboHotsearchTermsError("terms must be a bounded array")
    if document["n_titles"] != len(terms):
        raise WeiboHotsearchTermsError("n_titles does not match terms")
    if (document["status"] == "live") != bool(terms):
        raise WeiboHotsearchTermsError("status does not match title availability")
    seen: set[str] = set()
    for index, raw in enumerate(terms):
        if type(raw) is not dict or set(raw) != _TERM_FIELDS:
            raise WeiboHotsearchTermsError(f"terms[{index}] does not use its exact field set")
        title = raw["title"]
        if type(title) is not str or not title or title in seen:
            raise WeiboHotsearchTermsError(f"terms[{index}].title is invalid")
        seen.add(title)
        if raw["title_sha256"] != title_identity(title):
            raise WeiboHotsearchTermsError(f"terms[{index}].title_sha256 is inconsistent")
        if raw["best_rank"] is not None and (
            type(raw["best_rank"]) is not int or raw["best_rank"] < 1
        ):
            raise WeiboHotsearchTermsError(f"terms[{index}].best_rank is invalid")
        if type(raw["pinned"]) is not bool:
            raise WeiboHotsearchTermsError(f"terms[{index}].pinned must be boolean")
        for field in ("days_present", "appearances"):
            if type(raw[field]) is not int or raw[field] < 1:
                raise WeiboHotsearchTermsError(f"terms[{index}].{field} is invalid")
        if type(raw["first_seen"]) is not str or type(raw["last_seen"]) is not str:
            raise WeiboHotsearchTermsError(f"terms[{index}] dates must be strings")
    for row in document["ddti_join"]:
        if not isinstance(row, Mapping):
            raise WeiboHotsearchTermsError("ddti_join rows must be objects")
        if row.get("regime") == "suppressed_invisible" and not row.get("term"):
            raise WeiboHotsearchTermsError("suppressed_invisible requires a DDTI term")
        if row.get("regime") not in {"contained_visible", "suppressed_invisible"}:
            raise WeiboHotsearchTermsError("unknown DDTI regime")
    watch = document["withdrawal_watch"]
    if type(watch) is not dict:
        raise WeiboHotsearchTermsError("withdrawal_watch must come from withdrawal_candidates()")
    if "baseline_persist_rate" not in watch or "sense_filtered" not in watch:
        raise WeiboHotsearchTermsError("withdrawal_watch dropped its baseline or sense gate")
    if document["n_withdrawal_candidates"] != len(watch.get("candidates") or []):
        raise WeiboHotsearchTermsError("withdrawal candidate count is inconsistent")
    if document["n_ddti_joined"] != len(document["ddti_join"]):
        raise WeiboHotsearchTermsError("n_ddti_joined is inconsistent")
    canonical_json_bytes(document)


def write_weibo_hotsearch_terms(
    days: Mapping[str, list[dict]] | None,
    *,
    generated_at: str,
    readings: Path,
    ddti_terms: list[dict] | None = None,
    sensitive_terms: set[str] | None = None,
) -> dict[str, Any] | None:
    """Write latest + history. Abstain documents are not published as an empty board."""

    document = build_weibo_hotsearch_terms(
        days,
        generated_at=generated_at,
        ddti_terms=ddti_terms,
        sensitive_terms=sensitive_terms,
    )
    if document["status"] != "live":
        return None
    readings.mkdir(parents=True, exist_ok=True)
    latest = readings / "weibo-hotsearch-terms-latest.json"
    history = readings / "weibo-hotsearch-terms-history.jsonl"
    latest.write_bytes(canonical_json_bytes(document))
    with history.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "generated_at": document["generated_at"],
                    "n_titles": document["n_titles"],
                    "board_entries": document["board_entries"],
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
    "WeiboHotsearchTermsError",
    "aggregate_board_titles",
    "build_weibo_hotsearch_terms",
    "canonical_json_bytes",
    "title_identity",
    "validate_weibo_hotsearch_terms",
    "write_weibo_hotsearch_terms",
]
