"""Weiboscope attributed abstention — do not download the 2012 HKU dump.

Weiboscope (Fu, Chan, Chau / HKU JMSC) published 226 million 2012 Weibo
messages at doi:10.25442/hku.16674565. Palimpsest does not store that dump on
this node. Analysis may say so, with the citation.

A tiny public keyword/index door, if one still answers without login, may be
probed. Login walls, HTML homepages, and the DataHub dump are not ingested.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from core.china_observation import iso_z, public_text


DOI = "10.25442/hku.16674565"
CITATION = (
    "Fu KW, Chan CH, Chau M. Assessing Censorship on Microblogs in China: "
    "Discriminatory Keyword Analysis and the Real-Name Registration Policy. "
    "IEEE Internet Computing. 2013; 17(3): 42-50. "
    f"Weiboscope Open Data doi:{DOI}."
)
DATAHUB = f"https://doi.org/{DOI}"
ATTRIBUTION = (
    "Weiboscope / HKU Journalism and Media Studies Centre. Historical 2012 "
    "volume is not stored on this node."
)
SENTENCE = (
    "Historical Weiboscope volume is not on this node "
    f"(Fu, Chan, Chau 2013; doi:{DOI})."
)
# Candidate leftover index doors only. The DataHub dump is deliberately absent.
CANDIDATE_INDEXES = (
    "https://weiboscope.jmsc.hku.hk/",
)
MAX_INDEX_BYTES = 8 * 1024
METHOD_VERSION = 1

Fetch = Callable[[str], tuple[int, str]]


def documented_abstention(*, now: datetime | None = None) -> dict[str, Any]:
    """The warehouse row analysis is allowed to quote."""

    stamp = iso_z(now or datetime.now(timezone.utc))
    return {
        "peer": "weiboscope",
        "status": "abstain",
        "sentence": SENTENCE,
        "as_of": stamp,
        "peer_url": DATAHUB,
        "title": "Weiboscope Open Data (2012)",
        "excerpt": None,
        "host": None,
        "measurement_count": None,
        "anomaly_rate": None,
        "verdict": None,
        "window_days": None,
        "attribution": ATTRIBUTION,
        "relation": "peer-context-not-palimpsest-capture",
        "citation": CITATION,
        "doi": DOI,
        "dump_on_node": False,
        "n_messages_upstream": 226_841_122,
        "note": (
            "The 2012 HKU DataHub dump is 226 million messages. This node does "
            "not download or retain it."
        ),
    }


def _looks_like_tiny_index(body: str, url: str) -> bool:
    """Accept only a tiny public keyword/index payload, never a dump or login wall."""

    text = public_text(body, limit=MAX_INDEX_BYTES + 1)
    if not text or len(text) > MAX_INDEX_BYTES:
        return False
    lowered = text.casefold()
    if any(token in lowered for token in ("login", "sign in", "password", "captcha")):
        return False
    host = urlsplit(url).hostname or ""
    if "datahub.hku.hk" in host or "figshare" in host:
        return False
    return (
        "keyword" in lowered
        or "index" in lowered
        or (text.lstrip().startswith("{") and "keyword" in lowered)
    )


def probe_public_index(
    fetch: Fetch,
    *,
    candidates: tuple[str, ...] = CANDIDATE_INDEXES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Probe leftover public index doors. Default outcome is the documented abstention."""

    now = now or datetime.now(timezone.utc)
    probes: list[dict[str, Any]] = []
    for url in candidates:
        if "datahub.hku.hk" in url or "16674565" in url:
            probes.append({"url": url, "status": "refused-dump"})
            continue
        try:
            status, body = fetch(url)
        except OSError as exc:
            probes.append({
                "url": url,
                "status": "silent",
                "http_status": f"error:{type(exc).__name__}",
            })
            continue
        if status != 200 or not _looks_like_tiny_index(body, url):
            probes.append({
                "url": url,
                "status": "silent" if status != 200 else "not-a-tiny-index",
                "http_status": status,
            })
            continue
        probes.append({
            "url": url,
            "status": "tiny-index",
            "http_status": 200,
            "excerpt": public_text(body, limit=280),
        })
    used = next((row for row in probes if row["status"] == "tiny-index"), None)
    abstention = documented_abstention(now=now)
    return {
        "generated_at": iso_z(now),
        "method_version": METHOD_VERSION,
        "source": "Weiboscope / HKU JMSC (citation only unless a tiny public index answers)",
        "scope": (
            "Documented abstention for the 2012 226M-message dump. A leftover "
            "public keyword/index door may be quoted if it answers without login."
        ),
        "method": (
            "Do not download doi:10.25442/hku.16674565. Probe only named leftover "
            "index URLs; login walls and HTML homepages abstain."
        ),
        "attribution": ATTRIBUTION,
        "citation": CITATION,
        "doi": DOI,
        "dump_on_node": False,
        "probes": probes,
        "index": used,
        "abstention": abstention,
    }


__all__ = [
    "ATTRIBUTION",
    "CANDIDATE_INDEXES",
    "CITATION",
    "DATAHUB",
    "DOI",
    "METHOD_VERSION",
    "SENTENCE",
    "documented_abstention",
    "probe_public_index",
]
