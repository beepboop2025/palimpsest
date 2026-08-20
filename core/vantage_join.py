"""Project already-sealed warehouse readings onto the public-vantage desk.

This is not a collector. It reads files Palimpsest already published and
emits a join summary for /news/china/rumour/. Rumour rows stay a separate
slice. Nothing here increments an independent source group.

Exact URL joins stay silent when the key is missing. A shared registrable
host is labeled host-surface-only. Weibo demand is titles and ranks from the
public GitHub archive, not a logged-in session.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from core.china_observation import iso_z, public_text
from core.event_interconnection import registrable_domain
from core.live_paths import load_json_if_present


SCHEMA_VERSION = "palimpsest-vantage-join.v1"
RELATION = "warehouse-join-context-not-corroboration"
DEMAND_RELATION = "demand-rank-context-not-corroboration"
HOST_RELATION = "host-surface-only-not-corroboration"
STATUSES = frozenset({"COVERAGE_ONLY", "WAREHOUSE_JOIN"})

MAX_PULSES = 8
MAX_DEMAND = 12
MAX_ARCHIVE = 8
MAX_BLOCKED = 8
MAX_HOST_JOINS = 8
MAX_TUPLES = 8

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_ROW_ID = re.compile(r"^join-[0-9a-f]{24}$")
_MINOR = re.compile(
    r"(?:1[0-7]|[1-9])\s*岁|未成年|under[\s-]?1[0-7]|minor",
    re.IGNORECASE,
)

_TOP_FIELDS = {
    "schema_version",
    "generated_at",
    "source",
    "method",
    "scope",
    "status",
    "relation",
    "n_pulses",
    "n_demand",
    "n_archive",
    "n_blocked",
    "n_host_joins",
    "n_tuples",
    "pulses",
    "demand",
    "archive",
    "blocked",
    "host_joins",
    "tuples",
    "publication_policy",
    "limitations",
}
_PULSE_FIELDS = {
    "pulse_id",
    "warehouse",
    "title",
    "note",
    "observed_at",
    "relation",
}
_DEMAND_FIELDS = {
    "row_id",
    "surface",
    "title",
    "rank",
    "observed_at",
    "relation",
}
_ARCHIVE_FIELDS = {
    "row_id",
    "url",
    "term",
    "status",
    "n_captures",
    "observed_at",
    "relation",
}
_BLOCKED_FIELDS = {
    "row_id",
    "host",
    "title",
    "anomaly_pct",
    "measurements",
    "observed_at",
    "relation",
}
_HOST_JOIN_FIELDS = {
    "row_id",
    "headline",
    "url",
    "host",
    "wire_source",
    "ooni_note",
    "observed_at",
    "relation",
}
_TUPLE_FIELDS = {
    "tuple_id",
    "headline",
    "url",
    "legs",
    "observed_at",
    "relation",
}
_POLICY = {
    "counts_as_corroboration": False,
    "increments_independent_groups": False,
    "bodies_included": False,
    "media_included": False,
}
_FALSE_FRIEND_TERMS = frozenset({"广场", "散步"})
_SENSE_RISK_CATEGORIES = frozenset({"protest_dissent"})


class VantageJoinError(ValueError):
    """A warehouse join invented a scrape, a body, or a corroboration claim."""


def canonical_json_bytes(value: Any) -> bytes:
    def reject(node: Any, path: str = "vantage_join") -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise VantageJoinError(f"{path} contains a non-finite number")
        if isinstance(node, Mapping):
            for key, child in node.items():
                if type(key) is not str:
                    raise VantageJoinError(f"{path} contains a non-string key")
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


def _exact(value: Any, fields: set[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise VantageJoinError(f"{path} does not use its exact field set")
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or not _TIMESTAMP.fullmatch(value):
        raise VantageJoinError(f"{path} must be canonical UTC")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise VantageJoinError(f"{path} is not a real UTC timestamp") from exc
    return value


def _text(value: Any, path: str, *, maximum: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise VantageJoinError(f"{path} must be non-empty bounded text")
    if value != value.strip():
        raise VantageJoinError(f"{path} has leading or trailing whitespace")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise VantageJoinError(f"{path} contains unsafe Unicode")
    return value


def _optional_https(value: Any, path: str) -> str:
    if type(value) is not str:
        raise VantageJoinError(f"{path} must be a string")
    if value == "":
        return value
    text = _text(value, path, maximum=500)
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise VantageJoinError(f"{path} must be a keyless https URL or empty")
    return text


def _count(value: Any, path: str, *, maximum: int = 1_000_000) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise VantageJoinError(f"{path} must be a bounded non-negative integer")
    return value


def _identifier(value: Any, path: str) -> str:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise VantageJoinError(f"{path} is not a bounded identifier")
    return value


def row_id(*parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"join-{digest[:24]}"


def _clock(*values: Any) -> str | None:
    for value in values:
        stamp = iso_z(value)
        if stamp:
            return stamp
        if type(value) is str and _DAY.fullmatch(value.strip()):
            return f"{value.strip()}T00:00:00Z"
    return None


def _clip(value: Any, *, maximum: int = 180) -> str:
    text = public_text(value, limit=maximum)
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    text = " ".join(text.split())
    return text[:maximum]


def _looks_like_minor(title: str) -> bool:
    return bool(_MINOR.search(title))


def _host_key(value: str) -> str:
    host = (urlsplit(value).hostname if "://" in value else value).casefold().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return registrable_domain(host) if host else ""


def qualify_tuple(legs: Sequence[str]) -> bool:
    """Return True only when these warehouse legs may be named as one product tuple.

    The public product is: one headline on the wire, gone from a demand board,
    blocked in OONI, still in Wayback. Host overlap is not that tuple. An exact
    HTTPS URL shared by two or more legs is the minimum grain worth discussing.

    TODO: implement this gate. Default stays closed so the desk cannot mint a
    four-leg product row from a host-surface join.
    """

    return False


def validate_vantage_join(document: Mapping[str, Any]) -> None:
    top = _exact(document, _TOP_FIELDS, "vantage_join")
    if top["schema_version"] != SCHEMA_VERSION:
        raise VantageJoinError("invalid vantage-join schema version")
    _timestamp(top["generated_at"], "generated_at")
    _text(top["source"], "source", maximum=500)
    _text(top["method"], "method", maximum=1200)
    _text(top["scope"], "scope", maximum=1200)
    if top["status"] not in STATUSES:
        raise VantageJoinError("invalid vantage-join status")
    if top["relation"] != RELATION:
        raise VantageJoinError("vantage-join relation must remain context-only")
    if top["publication_policy"] != _POLICY:
        raise VantageJoinError("publication policy broadened the join boundary")

    pulses = top["pulses"]
    demand = top["demand"]
    archive = top["archive"]
    blocked = top["blocked"]
    host_joins = top["host_joins"]
    tuples = top["tuples"]
    for name, rows, maximum in (
        ("pulses", pulses, MAX_PULSES),
        ("demand", demand, MAX_DEMAND),
        ("archive", archive, MAX_ARCHIVE),
        ("blocked", blocked, MAX_BLOCKED),
        ("host_joins", host_joins, MAX_HOST_JOINS),
        ("tuples", tuples, MAX_TUPLES),
    ):
        if type(rows) is not list or len(rows) > maximum:
            raise VantageJoinError(f"{name} must be a bounded list")
    if top["n_pulses"] != len(pulses):
        raise VantageJoinError("n_pulses does not match pulses")
    if top["n_demand"] != len(demand):
        raise VantageJoinError("n_demand does not match demand")
    if top["n_archive"] != len(archive):
        raise VantageJoinError("n_archive does not match archive")
    if top["n_blocked"] != len(blocked):
        raise VantageJoinError("n_blocked does not match blocked")
    if top["n_host_joins"] != len(host_joins):
        raise VantageJoinError("n_host_joins does not match host_joins")
    if top["n_tuples"] != len(tuples):
        raise VantageJoinError("n_tuples does not match tuples")
    filled = bool(pulses or demand or archive or blocked or host_joins or tuples)
    if (top["status"] == "WAREHOUSE_JOIN") != filled:
        raise VantageJoinError("status does not match join availability")

    seen: set[str] = set()
    for index, raw in enumerate(pulses):
        path = f"pulses[{index}]"
        row = _exact(raw, _PULSE_FIELDS, path)
        if type(row["pulse_id"]) is not str or not _ROW_ID.fullmatch(row["pulse_id"]):
            raise VantageJoinError(f"{path}.pulse_id is invalid")
        if row["pulse_id"] in seen:
            raise VantageJoinError("duplicate join row id")
        seen.add(row["pulse_id"])
        _identifier(row["warehouse"], f"{path}.warehouse")
        _text(row["title"], f"{path}.title", maximum=180)
        _text(row["note"], f"{path}.note", maximum=240)
        _timestamp(row["observed_at"], f"{path}.observed_at")
        if row["relation"] != RELATION:
            raise VantageJoinError(f"{path}.relation is invalid")

    for index, raw in enumerate(demand):
        path = f"demand[{index}]"
        row = _exact(raw, _DEMAND_FIELDS, path)
        if type(row["row_id"]) is not str or not _ROW_ID.fullmatch(row["row_id"]):
            raise VantageJoinError(f"{path}.row_id is invalid")
        if row["row_id"] in seen:
            raise VantageJoinError("duplicate join row id")
        seen.add(row["row_id"])
        _identifier(row["surface"], f"{path}.surface")
        title = _text(row["title"], f"{path}.title", maximum=180)
        if _looks_like_minor(title):
            raise VantageJoinError(f"{path}.title looks like a minor")
        _count(row["rank"], f"{path}.rank", maximum=10_000)
        _timestamp(row["observed_at"], f"{path}.observed_at")
        if row["relation"] != DEMAND_RELATION:
            raise VantageJoinError(f"{path}.relation is invalid")

    for index, raw in enumerate(archive):
        path = f"archive[{index}]"
        row = _exact(raw, _ARCHIVE_FIELDS, path)
        if type(row["row_id"]) is not str or not _ROW_ID.fullmatch(row["row_id"]):
            raise VantageJoinError(f"{path}.row_id is invalid")
        if row["row_id"] in seen:
            raise VantageJoinError("duplicate join row id")
        seen.add(row["row_id"])
        _optional_https(row["url"], f"{path}.url")
        _text(row["term"], f"{path}.term", maximum=80)
        _identifier(row["status"], f"{path}.status")
        _count(row["n_captures"], f"{path}.n_captures", maximum=1_000_000)
        _timestamp(row["observed_at"], f"{path}.observed_at")
        if row["relation"] != RELATION:
            raise VantageJoinError(f"{path}.relation is invalid")

    for index, raw in enumerate(blocked):
        path = f"blocked[{index}]"
        row = _exact(raw, _BLOCKED_FIELDS, path)
        if type(row["row_id"]) is not str or not _ROW_ID.fullmatch(row["row_id"]):
            raise VantageJoinError(f"{path}.row_id is invalid")
        if row["row_id"] in seen:
            raise VantageJoinError("duplicate join row id")
        seen.add(row["row_id"])
        _text(row["host"], f"{path}.host", maximum=253)
        _text(row["title"], f"{path}.title", maximum=180)
        _count(row["anomaly_pct"], f"{path}.anomaly_pct", maximum=100)
        _count(row["measurements"], f"{path}.measurements")
        _timestamp(row["observed_at"], f"{path}.observed_at")
        if row["relation"] != RELATION:
            raise VantageJoinError(f"{path}.relation is invalid")

    for index, raw in enumerate(host_joins):
        path = f"host_joins[{index}]"
        row = _exact(raw, _HOST_JOIN_FIELDS, path)
        if type(row["row_id"]) is not str or not _ROW_ID.fullmatch(row["row_id"]):
            raise VantageJoinError(f"{path}.row_id is invalid")
        if row["row_id"] in seen:
            raise VantageJoinError("duplicate join row id")
        seen.add(row["row_id"])
        _text(row["headline"], f"{path}.headline", maximum=180)
        _optional_https(row["url"], f"{path}.url")
        _text(row["host"], f"{path}.host", maximum=253)
        _text(row["wire_source"], f"{path}.wire_source", maximum=80)
        _text(row["ooni_note"], f"{path}.ooni_note", maximum=180)
        _timestamp(row["observed_at"], f"{path}.observed_at")
        if row["relation"] != HOST_RELATION:
            raise VantageJoinError(f"{path}.relation is invalid")

    for index, raw in enumerate(tuples):
        path = f"tuples[{index}]"
        row = _exact(raw, _TUPLE_FIELDS, path)
        if type(row["tuple_id"]) is not str or not _ROW_ID.fullmatch(row["tuple_id"]):
            raise VantageJoinError(f"{path}.tuple_id is invalid")
        if row["tuple_id"] in seen:
            raise VantageJoinError("duplicate join row id")
        seen.add(row["tuple_id"])
        _text(row["headline"], f"{path}.headline", maximum=180)
        _optional_https(row["url"], f"{path}.url")
        legs = row["legs"]
        if type(legs) is not list or not 2 <= len(legs) <= 8 or legs != sorted(set(legs)):
            raise VantageJoinError(f"{path}.legs must be a sorted unique list of 2 to 8 ids")
        for leg_index, leg in enumerate(legs):
            _identifier(leg, f"{path}.legs[{leg_index}]")
        if not qualify_tuple(legs):
            raise VantageJoinError(f"{path} was named a tuple without clearing the gate")
        _timestamp(row["observed_at"], f"{path}.observed_at")
        if row["relation"] != RELATION:
            raise VantageJoinError(f"{path}.relation is invalid")

    limitations = top["limitations"]
    if type(limitations) is not list or not 3 <= len(limitations) <= 8:
        raise VantageJoinError("limitations must contain 3 to 8 statements")
    for index, value in enumerate(limitations):
        _text(value, f"limitations[{index}]", maximum=500)
    canonical_json_bytes(document)


def empty_document(generated_at: str) -> dict[str, Any]:
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "source": (
            "Already-sealed warehouse readings. This is a join, not a new scrape."
        ),
        "method": (
            "Sit on warehouses incrementally. Project metadata Palimpsest already "
            "published. Exact URL joins stay silent when the key is missing. "
            "Never keep the HTML."
        ),
        "scope": (
            "The way to get more grey data is more public vantages, continuously, "
            "then join them. One headline on Xinhua, gone from Baidu hot, blocked "
            "in OONI, still in Wayback: that tuple is the product. Official text "
            "alone is the censor's story."
        ),
        "status": "COVERAGE_ONLY",
        "relation": RELATION,
        "n_pulses": 0,
        "n_demand": 0,
        "n_archive": 0,
        "n_blocked": 0,
        "n_host_joins": 0,
        "n_tuples": 0,
        "pulses": [],
        "demand": [],
        "archive": [],
        "blocked": [],
        "host_joins": [],
        "tuples": [],
        "publication_policy": dict(_POLICY),
        "limitations": [
            "A warehouse pulse is sitting on someone else's measurement, not a Palimpsest probe.",
            "A shared host is not an exact URL join and does not corroborate a wire claim.",
            "Rumour-board rows stay a separate slice and cannot increment independent source groups.",
        ],
    }
    validate_vantage_join(document)
    return document


def _pct(rate: Any) -> int | None:
    if type(rate) is int and not isinstance(rate, bool) and 0 <= rate <= 100:
        return rate
    if type(rate) is float and math.isfinite(rate) and 0 <= rate <= 1:
        return int(round(rate * 100))
    if type(rate) is float and math.isfinite(rate) and 0 <= rate <= 100:
        return int(round(rate))
    return None


def _from_pulses(readings: Mapping[str, Mapping[str, Any] | None]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ooni = readings.get("ooni-gfw")
    if ooni:
        observed = _clock(ooni.get("generated_at"), ooni.get("until"))
        index = ooni.get("gfw_index")
        measurements = ooni.get("n_completed_measurements")
        if observed and type(measurements) is int:
            title = _clip(f"OONI GFW index {index}")
            note = _clip(
                f"{measurements} completed CN measurements in the declared window. "
                "Anomaly rate, not confirmed blocking."
            )
            rows.append(
                {
                    "pulse_id": row_id("pulse", "ooni-gfw", observed, title),
                    "warehouse": "ooni-gfw",
                    "title": title,
                    "note": note,
                    "observed_at": observed,
                    "relation": RELATION,
                }
            )
    fusion = readings.get("vantage-fusion")
    if fusion:
        observed = _clock(fusion.get("generated_at"), fusion.get("last_changed_at"))
        verdict = _clip(fusion.get("verdict"), maximum=180)
        if observed and verdict:
            rows.append(
                {
                    "pulse_id": row_id("pulse", "vantage-fusion", observed, verdict),
                    "warehouse": "vantage-fusion",
                    "title": _clip("OONI and Censored Planet still disagree"),
                    "note": _clip(fusion.get("verdict"), maximum=240),
                    "observed_at": observed,
                    "relation": RELATION,
                }
            )
    wayback = readings.get("wayback")
    if wayback:
        observed = _clock(wayback.get("generated_at"))
        watched = wayback.get("n_watched")
        if observed and type(watched) is int:
            title = _clip(
                f"Wayback watchlist {watched} URLs, "
                f"{wayback.get('n_reachable') or 0} reachable"
            )
            note = _clip(
                f"{wayback.get('n_deletions') or 0} deletions, "
                f"{wayback.get('n_mutations') or 0} mutations in this sealed slice."
            )
            rows.append(
                {
                    "pulse_id": row_id("pulse", "wayback", observed, title),
                    "warehouse": "wayback",
                    "title": title,
                    "note": note,
                    "observed_at": observed,
                    "relation": RELATION,
                }
            )
    weibo = readings.get("weibo-hotsearch")
    if weibo:
        observed = _clock(weibo.get("generated_at"))
        board = weibo.get("board_entries")
        if observed and type(board) is int:
            title = _clip(f"Weibo hot-search archive {board} board entries")
            note = _clip(
                "Titles and ranks from the public justjavac archive. "
                "Not a logged-in Weibo session."
            )
            rows.append(
                {
                    "pulse_id": row_id("pulse", "weibo-hotsearch", observed, title),
                    "warehouse": "weibo-hotsearch",
                    "title": title,
                    "note": note,
                    "observed_at": observed,
                    "relation": RELATION,
                }
            )
    gdelt = readings.get("gdelt")
    if gdelt:
        observed = _clock(gdelt.get("generated_at"))
        terms = gdelt.get("n_terms")
        global_n = gdelt.get("n_with_global_data")
        if observed and type(terms) is int:
            title = _clip(f"GDELT {global_n or 0} of {terms} terms have global volume")
            note = _clip(
                f"{gdelt.get('n_containment') or 0} containment, "
                f"{gdelt.get('n_blackout') or 0} blackout in the sealed week."
            )
            rows.append(
                {
                    "pulse_id": row_id("pulse", "gdelt", observed, title),
                    "warehouse": "gdelt",
                    "title": title,
                    "note": note,
                    "observed_at": observed,
                    "relation": RELATION,
                }
            )
    ioda = readings.get("ioda-outages")
    if ioda:
        observed = _clock(ioda.get("generated_at"))
        if observed:
            title = _clip(
                f"IODA {ioda.get('instruments_firing') or 0} instruments firing"
            )
            note = _clip(
                "Shutdown-scale CN events from BGP, active probing, and darknet. "
                "Zero is a coverage receipt for this window."
            )
            rows.append(
                {
                    "pulse_id": row_id("pulse", "ioda-outages", observed, title),
                    "warehouse": "ioda-outages",
                    "title": title,
                    "note": note,
                    "observed_at": observed,
                    "relation": RELATION,
                }
            )
    return rows[:MAX_PULSES]


def _keep_demand_breakthrough(row: Mapping[str, Any]) -> bool:
    term = public_text(row.get("term"), limit=40)
    category = public_text(row.get("category"), limit=64)
    if not term or term in _FALSE_FRIEND_TERMS:
        return False
    if row.get("sense_filtered_count"):
        return False
    if category in _SENSE_RISK_CATEGORIES:
        return False
    return True


def _from_demand(weibo: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not weibo:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for breakthrough in weibo.get("gazetteer_breakthroughs") or []:
        if type(breakthrough) is not dict or not _keep_demand_breakthrough(breakthrough):
            continue
        term = public_text(breakthrough.get("term"), limit=40)
        for sample in breakthrough.get("samples") or []:
            if type(sample) is not dict:
                continue
            title = _clip(sample.get("title"))
            rank = sample.get("rank")
            observed = _clock(sample.get("date"), weibo.get("generated_at"))
            if (
                not title
                or type(rank) is not int
                or not observed
                or term not in title
                or _looks_like_minor(title)
                or title in seen
            ):
                continue
            seen.add(title)
            rows.append(
                {
                    "row_id": row_id("demand", "weibo-hotsearch", title, observed),
                    "surface": "weibo-hotsearch",
                    "title": title,
                    "rank": rank,
                    "observed_at": observed,
                    "relation": DEMAND_RELATION,
                }
            )
            if len(rows) >= MAX_DEMAND:
                return rows
    for day in reversed(list(weibo.get("pinned_headlines") or [])):
        if type(day) is not dict:
            continue
        observed = _clock(day.get("date"), weibo.get("generated_at"))
        if not observed:
            continue
        for raw in day.get("pinned") or []:
            title = _clip(raw)
            if not title or _looks_like_minor(title) or title in seen:
                continue
            seen.add(title)
            rows.append(
                {
                    "row_id": row_id("demand", "weibo-hotsearch-pin", title, observed),
                    "surface": "weibo-hotsearch",
                    "title": title,
                    "rank": 0,
                    "observed_at": observed,
                    "relation": DEMAND_RELATION,
                }
            )
            if len(rows) >= MAX_DEMAND:
                rows.sort(key=lambda row: (row["rank"], row["observed_at"], row["row_id"]))
                return rows[:MAX_DEMAND]
    watch = weibo.get("withdrawal_watch")
    if type(watch) is dict:
        for sample in watch.get("candidates") or []:
            if type(sample) is not dict:
                continue
            title = _clip(sample.get("title"))
            rank = sample.get("best_rank")
            observed = _clock(sample.get("date"), weibo.get("generated_at"))
            if (
                not title
                or type(rank) is not int
                or not observed
                or _looks_like_minor(title)
                or title in seen
            ):
                continue
            seen.add(title)
            rows.append(
                {
                    "row_id": row_id("demand", "weibo-hotsearch-exit", title, observed),
                    "surface": "weibo-hotsearch",
                    "title": title,
                    "rank": rank,
                    "observed_at": observed,
                    "relation": DEMAND_RELATION,
                }
            )
            if len(rows) >= MAX_DEMAND:
                break
    rows.sort(key=lambda row: (row["rank"], row["observed_at"], row["row_id"]))
    return rows[:MAX_DEMAND]


def _archive_status(value: Any) -> str:
    text = public_text(value, limit=48).casefold().replace("_", "-")
    text = re.sub(r"[^a-z0-9-]+", "-", text).strip("-")
    if _IDENTIFIER.fullmatch(text):
        return text
    return "unknown"


def _from_archive(wayback: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not wayback:
        return []
    fallback = _clock(wayback.get("generated_at"))
    if not fallback:
        return []
    rows: list[dict[str, Any]] = []
    for item in wayback.get("reconstructions") or []:
        if type(item) is not dict:
            continue
        term = _clip(item.get("term"), maximum=80)
        if not term:
            continue
        url = item.get("url") if type(item.get("url")) is str else ""
        if url and not url.startswith("https://"):
            url = ""
        captures = item.get("n_captures")
        if type(captures) is not int:
            captures = 0
        rows.append(
            {
                "row_id": row_id("archive", term, url or "none"),
                "url": url,
                "term": term,
                "status": _archive_status(item.get("event") or item.get("note")),
                "n_captures": captures,
                "observed_at": fallback,
                "relation": RELATION,
            }
        )
        if len(rows) >= MAX_ARCHIVE:
            break
    return rows


def _from_blocked(ooni: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not ooni:
        return []
    observed = _clock(ooni.get("generated_at"), ooni.get("until"))
    if not observed:
        return []
    ranked: list[tuple[int, dict[str, Any]]] = []
    for item in ooni.get("top_blocked") or []:
        if type(item) is not dict:
            continue
        host = public_text(item.get("domain"), limit=253).casefold()
        measurements = item.get("completed_measurement_count")
        if type(measurements) is not int:
            measurements = item.get("measurement_count")
        pct = _pct(item.get("anomaly_rate"))
        anomalies = item.get("anomaly_count")
        if not host or "." not in host or type(measurements) is not int or pct is None:
            continue
        ranked.append(
            (
                anomalies if type(anomalies) is int else 0,
                {
                    "row_id": row_id("blocked", host, observed),
                    "host": host,
                    "title": _clip(f"{host} {pct}% anomalous in the OONI CN window"),
                    "anomaly_pct": pct,
                    "measurements": measurements,
                    "observed_at": observed,
                    "relation": RELATION,
                },
            )
        )
    ranked.sort(key=lambda item: (-item[0], item[1]["host"]))
    return [row for _, row in ranked[:MAX_BLOCKED]]


def _from_host_joins(
    wire: Mapping[str, Any] | None,
    ooni: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not wire or not ooni:
        return []
    observed_ooni = _clock(ooni.get("generated_at"), ooni.get("until"))
    if not observed_ooni:
        return []
    blocked: dict[str, dict[str, Any]] = {}
    for item in ooni.get("top_blocked") or []:
        if type(item) is not dict:
            continue
        key = _host_key(public_text(item.get("domain"), limit=253))
        if key:
            blocked[key] = item
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in wire.get("events") or []:
        if type(event) is not dict:
            continue
        for ref in event.get("evidence_refs") or []:
            if type(ref) is not dict:
                continue
            url = ref.get("url") if type(ref.get("url")) is str else ""
            key = _host_key(url)
            match = blocked.get(key)
            if not match or not url.startswith("https://"):
                continue
            headline = _clip(ref.get("title") or event.get("headline"))
            observed = _clock(ref.get("published_at"), event.get("published_at"), observed_ooni)
            if not headline or not observed or headline in seen:
                continue
            pct = _pct(match.get("anomaly_rate"))
            if pct is None:
                continue
            seen.add(headline)
            rows.append(
                {
                    "row_id": row_id("host", key, url, observed),
                    "headline": headline,
                    "url": url,
                    "host": key,
                    "wire_source": _clip(ref.get("source_name") or ref.get("source_id"), maximum=80),
                    "ooni_note": _clip(
                        f"{public_text(match.get('domain'), limit=253)} "
                        f"{pct}% anomalous in the OONI CN window"
                    ),
                    "observed_at": observed,
                    "relation": HOST_RELATION,
                }
            )
            break
        if len(rows) >= MAX_HOST_JOINS:
            break
    rows.sort(key=lambda row: (row["observed_at"], row["row_id"]), reverse=True)
    return rows[:MAX_HOST_JOINS]


def _mint_tuples(host_joins: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Mint a product tuple only when qualify_tuple clears the grain."""

    rows: list[dict[str, Any]] = []
    for item in host_joins:
        legs = ["newswire", "ooni-gfw"]
        if not qualify_tuple(legs):
            continue
        rows.append(
            {
                "tuple_id": row_id("tuple", item["row_id"]),
                "headline": item["headline"],
                "url": item["url"],
                "legs": sorted(legs),
                "observed_at": item["observed_at"],
                "relation": RELATION,
            }
        )
        if len(rows) >= MAX_TUPLES:
            break
    return rows


def project_join(
    readings: Mapping[str, Mapping[str, Any] | None],
    *,
    generated_at: str,
) -> dict[str, Any]:
    document = empty_document(generated_at)
    pulses = _from_pulses(readings)
    demand = _from_demand(readings.get("weibo-hotsearch"))
    archive = _from_archive(readings.get("wayback"))
    blocked = _from_blocked(readings.get("ooni-gfw"))
    host_joins = _from_host_joins(readings.get("newswire"), readings.get("ooni-gfw"))
    tuples = _mint_tuples(host_joins)
    document["pulses"] = pulses
    document["demand"] = demand
    document["archive"] = archive
    document["blocked"] = blocked
    document["host_joins"] = host_joins
    document["tuples"] = tuples
    document["n_pulses"] = len(pulses)
    document["n_demand"] = len(demand)
    document["n_archive"] = len(archive)
    document["n_blocked"] = len(blocked)
    document["n_host_joins"] = len(host_joins)
    document["n_tuples"] = len(tuples)
    if pulses or demand or archive or blocked or host_joins or tuples:
        document["status"] = "WAREHOUSE_JOIN"
    validate_vantage_join(document)
    return document


def load_warehouse_readings(readings_dir: Path) -> dict[str, dict[str, Any] | None]:
    names = (
        "ooni-gfw",
        "vantage-fusion",
        "wayback",
        "weibo-hotsearch",
        "gdelt",
        "ioda-outages",
        "newswire",
    )
    return {
        name: load_json_if_present(readings_dir / f"{name}-latest.json")
        for name in names
    }


__all__ = [
    "DEMAND_RELATION",
    "HOST_RELATION",
    "RELATION",
    "SCHEMA_VERSION",
    "VantageJoinError",
    "empty_document",
    "load_warehouse_readings",
    "project_join",
    "qualify_tuple",
    "row_id",
    "validate_vantage_join",
]
