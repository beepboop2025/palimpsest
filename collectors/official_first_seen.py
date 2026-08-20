"""Poll public official landing pages for first-seen text and rewrite trails.

This is a thin live companion to Wayback reconstruction. It fetches already-public
institutional landing pages (never Baike, never a person, never a court docket)
and records:

* first-seen public text and ``content_sha256``
* last-confirmed-alive when the page still answers
* a deletion trail when a previously-live page becomes 404/403/empty
* a rewrite trail when the hash changes

Absence of a fetch is a coverage gap, not a zero. The collector does not scrape
logged-in surfaces and does not invent a live reading when every page is silent.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Callable, Mapping

from core.china_observation import (
    content_sha256,
    enrich_observation,
    iso_z,
    public_text,
)
from core.governance import KillSwitch, RateCeiling
from core.visibility_event import stamp_visibility_event


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "official_first_seen.json"
SKIP_HOSTS = ("baike.baidu.com",)
Fetch = Callable[[str], tuple[int, str]]


def load_pages(path: Path | str = DEFAULT_CONFIG) -> list[dict[str, Any]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    skips = tuple(doc.get("_meta", {}).get("skip_host_substrings") or SKIP_HOSTS)
    pages = []
    for raw in doc.get("pages") or []:
        url = public_text(raw.get("url"), limit=2048)
        if not url.startswith("https://"):
            continue
        if any(skip in url for skip in skips):
            continue
        pages.append({
            "url": url,
            "term": public_text(raw.get("term"), limit=80),
            "domain": public_text(raw.get("domain"), limit=40),
            "kind": public_text(raw.get("kind"), limit=16) or "landing",
            "why": public_text(raw.get("why"), limit=240),
        })
    return pages


def html_to_public_text(html: str, *, limit: int = 8000) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html or "")
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return public_text(text, limit=limit)


def _alive(status: int, text: str) -> bool:
    return status == 200 and len(text) >= 40


def poll_pages(
    *,
    pages: list[Mapping[str, Any]] | None = None,
    fetch: Fetch,
    previous: Mapping[str, Any] | None = None,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compare current public text to the last node-local state. No network invention."""

    now = now or datetime.now(timezone.utc)
    generated = iso_z(now)
    kill = kill_switch or KillSwitch()
    watch = list(pages) if pages is not None else load_pages()
    prior_pages = {}
    if isinstance(previous, Mapping):
        raw_pages = previous.get("pages")
        if isinstance(raw_pages, Mapping):
            prior_pages = dict(raw_pages)
    observations: list[dict[str, Any]] = []
    states: dict[str, Any] = {}
    n_ok = 0
    n_unreachable = 0

    for page in watch:
        url = page["url"]
        kill.require_live()
        if rate_ceiling is not None:
            rate_ceiling.acquire()
        status: int | str
        body = ""
        try:
            status, body = fetch(url)
        except OSError as exc:
            status = f"error:{type(exc).__name__}"
            body = ""
        text = html_to_public_text(body) if status == 200 else ""
        digest = content_sha256(url, text) if text else None
        prior = prior_pages.get(url) if isinstance(prior_pages.get(url), Mapping) else {}
        prior_digest = prior.get("content_sha256") if isinstance(prior.get("content_sha256"), str) else None
        first_seen = prior.get("first_seen")
        event = "unreachable"
        confirmations = []
        last_confirmed = None
        if isinstance(status, int) and _alive(status, text):
            n_ok += 1
            last_confirmed = generated
            first_seen = first_seen or generated
            if not prior_digest:
                event = "first_seen"
                confirmations.append({
                    "status": "first-seen",
                    "observed_at": generated,
                    "source": "official_first_seen",
                    "note": "First successful public fetch of this official landing page",
                })
            elif prior_digest != digest:
                event = "rewrite"
                confirmations.append({
                    "status": "hash-changed",
                    "observed_at": generated,
                    "source": "official_first_seen",
                    "note": "Public landing-page text hash changed; not a deletion claim",
                })
            else:
                event = "still_alive"
                confirmations.append({
                    "status": "last-confirmed-alive",
                    "observed_at": generated,
                    "source": "official_first_seen",
                    "note": "Same public hash as the previous successful fetch",
                })
        else:
            n_unreachable += 1
            if prior_digest:
                event = "disappeared"
                confirmations.append({
                    "status": "disappeared",
                    "observed_at": generated,
                    "source": "official_first_seen",
                    "note": (
                        f"Previously fetched official page now status={status}. "
                        "This is a fetch outcome, not a proven takedown."
                    ),
                })
            first_seen = prior.get("first_seen")
        states[url] = {
            "content_sha256": digest or prior_digest,
            "first_seen": first_seen,
            "last_confirmed_alive": last_confirmed or prior.get("last_confirmed_alive"),
            "last_status": status,
            "last_event": event,
            "term": page.get("term"),
        }
        if event == "unreachable" and not prior_digest:
            continue
        title = page.get("term") or url
        observations.append(stamp_visibility_event(enrich_observation(
            {
                "terms": [page["term"]] if page.get("term") else [],
                "detected_at": generated,
                "title": f"[official:{event}] {title}",
                "text": text or public_text(page.get("why"), limit=400),
                "url": url,
                "source": "official_first_seen",
                "deletion_signal": event,
                "domain": page.get("domain"),
            },
            text=text or public_text(page.get("why"), limit=400),
            source_url=url,
            first_seen=first_seen,
            last_seen=generated,
            last_confirmed_alive=last_confirmed,
            confirmations=confirmations,
            provenance={
                "collector": "official_first_seen",
                "method": "public official landing-page poll; hash trail; no Baike; no dockets",
                "vantage": "outside-china-public-source",
                "http_status": status,
                "schema_version": "palimpsest-china-observation.v1",
                "method_version": 1,
            },
        )))

    return {
        "generated_at": generated,
        "n_pages": len(watch),
        "n_ok": n_ok,
        "n_unreachable": n_unreachable,
        "n_observations": len(observations),
        "pages": states,
        "observations": observations,
    }
