"""Probabilistic event-cluster sidecar — similar is not the same, and not corroboration.

Cross-platform reconstruction may connect Weibo, Bilibili, Douyin, Zhihu,
Telegram public channels, news pages, and archives using public links,
timestamps, titles, hashtags, and lexical similarity. Matches stay
probabilistic. Similar text is never claimed to be the same post.

Published corroboration and fat-object join remain exact-key
(``core.event_interconnection``). This sidecar cannot increment
``independent_source_groups`` or ``n_corroborated_events``.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "palimpsest-event-cluster-sidecar.v1"
METHOD_VERSION = 1
RELATION = "semantic-event-cluster-not-corroboration"
SIDECAR_FIELDS = (
    "event_cluster",
    "platform",
    "surface",
    "time_window",
    "visibility_state",
    "topic_cluster",
    "link_overlap",
    "semantic_match_score",
    "evidence_hash",
)

# Anything at or above this is "similar", never "the same post".
SIMILARITY_FLOOR = 0.35
SAME_POST_CLAIM = False

_TOKEN = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _tokens(text: str) -> set[str]:
    return {t.casefold() for t in _TOKEN.findall(text or "") if len(t) > 1}


def semantic_match_score(left: str, right: str) -> float:
    """Jaccard over tokens. Bounded, replayable, not an embedding."""

    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _day(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _DAY.match(value)
    return match.group(0) if match else None


def _links(row: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()
    for key in ("url", "locator", "source_url"):
        item = row.get(key)
        if isinstance(item, str) and item.startswith("https://"):
            found.add(item.split("#", 1)[0])
    for item in row.get("outbound_urls") or row.get("links") or []:
        if isinstance(item, str) and item.startswith("https://"):
            found.add(item.split("#", 1)[0])
    return found


def _platform(row: Mapping[str, Any]) -> str:
    return str(row.get("platform") or row.get("source") or "public-web")[:40]


def _window(rows: Sequence[Mapping[str, Any]]) -> dict[str, str | None]:
    days = sorted(d for d in (_day(r.get("timestamp") or r.get("detected_at")) for r in rows) if d)
    return {"first": days[0] if days else None, "last": days[-1] if days else None, "unit": "calendar-day"}


def _cluster_id(members: Sequence[Mapping[str, Any]]) -> str:
    keys = sorted(
        f"{_platform(r)}|{r.get('locator') or r.get('url') or ''}|{r.get('title') or ''}"
        for r in members
    )
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16]


def _evidence_hash(row: Mapping[str, Any]) -> str:
    payload = "|".join(
        str(row.get(field) or "")
        for field in SIDECAR_FIELDS
        if field != "evidence_hash"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_sidecar(
    records: Sequence[Mapping[str, Any]],
    *,
    floor: float = SIMILARITY_FLOOR,
) -> dict[str, Any]:
    """Cluster public records by link overlap and lexical similarity."""

    items = [dict(r) for r in records if isinstance(r, Mapping)]
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[b] = a

    scores: dict[tuple[int, int], float] = {}
    for i, left in enumerate(items):
        for j in range(i + 1, len(items)):
            right = items[j]
            links_l, links_r = _links(left), _links(right)
            overlap = 0.0
            if links_l and links_r:
                overlap = len(links_l & links_r) / len(links_l | links_r)
            text_l = f"{left.get('title') or ''} {left.get('text') or ''} {' '.join(left.get('hashtags') or [])}"
            text_r = f"{right.get('title') or ''} {right.get('text') or ''} {' '.join(right.get('hashtags') or [])}"
            sim = semantic_match_score(text_l, text_r)
            score = max(sim, overlap)
            scores[(i, j)] = score
            if score >= floor or overlap > 0:
                union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(items)):
        groups[find(i)].append(i)

    clusters: list[dict[str, Any]] = []
    for members_idx in groups.values():
        members = [items[i] for i in members_idx]
        cid = _cluster_id(members)
        for idx in members_idx:
            row = items[idx]
            peer_scores = [
                scores[tuple(sorted((idx, other)))]
                for other in members_idx
                if other != idx and tuple(sorted((idx, other))) in scores
            ]
            match = max(peer_scores) if peer_scores else 0.0
            links = _links(row)
            peer_links = set().union(*(_links(items[o]) for o in members_idx if o != idx)) if len(members_idx) > 1 else set()
            entry = {
                "event_cluster": cid,
                "platform": _platform(row),
                "surface": str(row.get("surface") or "public-web"),
                "time_window": _window(members),
                "visibility_state": str(row.get("visibility_state") or "unknown"),
                "topic_cluster": str(row.get("topic") or row.get("canonical") or "")[:80],
                "link_overlap": round(
                    (len(links & peer_links) / len(links | peer_links)) if (links or peer_links) else 0.0,
                    4,
                ),
                "semantic_match_score": round(match, 4),
                "same_post": False,
                "locator": row.get("locator") or row.get("url"),
            }
            entry["evidence_hash"] = _evidence_hash(entry)
            clusters.append(entry)

    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "relation": RELATION,
        "publication_policy": {
            "counts_as_corroboration": False,
            "increments_independent_groups": False,
            "join": "probabilistic-sidecar",
            "exact_key_join_unchanged": True,
            "same_post_claim": SAME_POST_CLAIM,
        },
        "n_records": len(items),
        "n_clusters": len(groups),
        "clusters": clusters,
    }


def corroboration_increment(sidecar: Mapping[str, Any] | None) -> int:
    """Semantic matches never raise corroboration. The increment is always 0."""

    if not sidecar:
        return 0
    policy = sidecar.get("publication_policy") if isinstance(sidecar, Mapping) else None
    if isinstance(policy, Mapping) and policy.get("counts_as_corroboration"):
        raise ValueError("event-cluster sidecar must not count as corroboration")
    return 0


def independent_group_increment(sidecar: Mapping[str, Any] | None) -> int:
    if not sidecar:
        return 0
    policy = sidecar.get("publication_policy") if isinstance(sidecar, Mapping) else None
    if isinstance(policy, Mapping) and policy.get("increments_independent_groups"):
        raise ValueError("event-cluster sidecar must not increment independent groups")
    return 0


def attach_without_raising_join(
    interconnection: Mapping[str, Any],
    sidecar: Mapping[str, Any],
) -> dict[str, Any]:
    """Return interconnection unchanged beside a *sibling* sidecar pointer.

    The fat-object schema is exact-key. Extra semantic fields are not merged
    into it — that would either fail validation or look like a join.
    """

    increment = independent_group_increment(sidecar)
    if increment != 0:
        raise ValueError("sidecar attempted to raise independent_source_groups")
    return {
        "interconnection": dict(interconnection),
        "semantic_sidecar": dict(sidecar),
        "independent_source_groups": interconnection.get("independent_source_groups"),
        "joined_count": interconnection.get("joined_count"),
        "sidecar_corroboration_increment": corroboration_increment(sidecar),
    }
