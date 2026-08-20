"""Project fat China observations from the existing public newswire events.

Does not scrape article HTML. Title + excerpt already held on the event
(``headline`` / ``dek`` / ``evidence_refs``) become the observation text.
Publisher URL comes from ``evidence_refs``, never the palimpsest.info event permalink.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.china_observation import enrich_observation, public_text
from core.live_paths import resolve_newswire_path
from core.visibility_event import stamp_visibility_event

DEFAULT_WIRE = resolve_newswire_path(preferred=Path("readings/newswire-latest.json"))


def load_wire_events(path: Path | str = DEFAULT_WIRE) -> list[dict[str, Any]]:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(doc, dict):
        return []
    events = doc.get("events")
    return [row for row in events if isinstance(row, dict)] if isinstance(events, list) else []


def publisher_url(event: dict[str, Any]) -> str | None:
    for ref in event.get("evidence_refs") or []:
        if not isinstance(ref, dict):
            continue
        url = public_text(ref.get("url"), limit=2048)
        if url.startswith("https://") and "palimpsest.info/" not in url:
            return url
        if url.startswith("http://") and "palimpsest.info/" not in url:
            return url
    return None


def observation_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    url = publisher_url(event)
    if not url:
        return None
    title = public_text(event.get("headline") or event.get("title"), limit=1000)
    dek = public_text(event.get("dek"), limit=2000)
    text = " — ".join(part for part in (title, dek) if part)
    if not text:
        return None
    captured = public_text(
        event.get("updated_at") or event.get("published_at") or event.get("observed_at"),
        limit=40,
    ) or None
    refs = event.get("evidence_refs") or []
    source_id = ""
    if isinstance(refs, list) and refs and isinstance(refs[0], dict):
        source_id = public_text(refs[0].get("source_id"), limit=80)
    outlet = public_text(event.get("desk"), limit=80)
    topics = [
        topic for topic in (event.get("topics") or [])
        if isinstance(topic, str) and topic
    ]
    seed: dict[str, Any] = {
        "terms": [],
        "detected_at": captured,
        "title": title or None,
        "text": text,
        "url": url,
        "source": "news-wire-live",
        "rights_policy": "metadata-link-only",
        "retention_class": "public-rss-metadata",
    }
    if topics:
        seed["topics"] = topics
    return stamp_visibility_event(enrich_observation(
        seed,
        text=text,
        source_url=url,
        first_seen=captured,
        last_seen=captured,
        last_confirmed_alive=captured,
        provenance={
            "collector": "news_wire_live",
            "method": "projection of public RSS/Atom metadata already held on the evidence wire",
            "vantage": "outside-china-public-source",
            "schema_version": "palimpsest-china-observation.v1",
            "method_version": 1,
            "event_id": public_text(event.get("event_id"), limit=80),
            "source_id": source_id or None,
            "outlet": outlet or None,
        },
    ))


def observations_from_events(events: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows = events if events is not None else load_wire_events()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in rows:
        if not isinstance(event, dict):
            continue
        obs = observation_from_event(event)
        if obs is None:
            continue
        key = obs.get("url")
        if isinstance(key, str) and key in seen:
            continue
        if isinstance(key, str):
            seen.add(key)
        out.append(obs)
    return out
