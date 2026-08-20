"""Public Baike article snapshots for silent rewrite detection.

Polls already-public encyclopedia HTML from outside China and compares it to
node-local hashes plus the Internet Archive CDX digest timeline. This is not
the disabled Wikipedia-fork collector in ``baike_redaction`` and it does not
use a logged-in Baike API.

Topic and event pages only. Person pages are refused. A login wall, captcha,
or empty fetch is unreachable — never a fabricated rewrite.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from collectors.baike_redaction import extract_baike
from collectors.official_first_seen import html_to_public_text
from collectors.wayback_vantage import parse_cdx_json
from core.china_observation import content_sha256, enrich_observation, iso_z, public_text
from core.governance import KillSwitch, RateCeiling


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "baike_public_watchlist.json"
LOGIN_MARKERS = ("百度安全验证", "passport.baidu.com", "wappass.baidu.com")
Fetch = Callable[[str], tuple[int, str]]
CdxFetch = Callable[[str], str]


def load_pages(path: Path | str = DEFAULT_CONFIG) -> list[dict[str, Any]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    pages = []
    for raw in doc.get("pages") or []:
        if not isinstance(raw, dict):
            continue
        kind = public_text(raw.get("kind"), limit=16) or "topic"
        if kind == "person":
            continue
        url = public_text(raw.get("url"), limit=2048)
        if not url.startswith("https://baike.baidu.com/"):
            continue
        pages.append({
            "url": url,
            "term": public_text(raw.get("term"), limit=80),
            "domain": public_text(raw.get("domain"), limit=40),
            "kind": kind,
            "why": public_text(raw.get("why"), limit=240),
        })
    return pages


def _login_walled(html: str) -> bool:
    return any(marker in (html or "") for marker in LOGIN_MARKERS)


def _article_text(html: str) -> tuple[str, str]:
    parsed = extract_baike(html)
    if parsed.get("present") and parsed.get("text"):
        return public_text(parsed["text"], limit=8000), str(parsed.get("interstitial") or "")
    return html_to_public_text(html), str(parsed.get("interstitial") or "")


def _cdx_trail(url: str, fetch_cdx: CdxFetch | None) -> dict[str, Any] | None:
    if fetch_cdx is None:
        return None
    try:
        payload = fetch_cdx(url)
    except OSError:
        return None
    captures = parse_cdx_json(payload)
    if not captures:
        return None
    latest = captures[-1]
    return {
        "n_captures": len(captures),
        "latest_ts": latest.timestamp,
        "latest_digest": latest.digest,
        "latest_status": latest.statuscode,
        "wayback_snapshot": latest.snapshot_url() if latest.status_class == "live" else None,
    }


def poll_articles(
    *,
    pages: list[Mapping[str, Any]] | None = None,
    fetch: Fetch,
    fetch_cdx: CdxFetch | None = None,
    previous: Mapping[str, Any] | None = None,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    generated = iso_z(now)
    kill = kill_switch or KillSwitch()
    watch = list(pages) if pages is not None else load_pages()
    prior_pages = {}
    if isinstance(previous, Mapping) and isinstance(previous.get("pages"), Mapping):
        prior_pages = dict(previous["pages"])
    observations: list[dict[str, Any]] = []
    states: dict[str, Any] = {}
    n_ok = 0
    n_unreachable = 0
    n_walled = 0

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
        walled = isinstance(status, int) and status == 200 and _login_walled(body)
        text, interstitial = _article_text(body) if status == 200 and not walled else ("", "")
        if interstitial in {"not_created", "deleted", "disambiguation"}:
            text = ""
        digest = content_sha256(url, text) if text else None
        prior = prior_pages.get(url) if isinstance(prior_pages.get(url), Mapping) else {}
        prior_digest = prior.get("content_sha256") if isinstance(prior.get("content_sha256"), str) else None
        first_seen = prior.get("first_seen")
        event = "unreachable"
        confirmations = []
        last_confirmed = None
        alive = isinstance(status, int) and status == 200 and not walled and len(text) >= 40
        if walled:
            n_walled += 1
            n_unreachable += 1
            event = "login_walled"
        elif alive:
            n_ok += 1
            last_confirmed = generated
            first_seen = first_seen or generated
            if not prior_digest:
                event = "first_seen"
                confirmations.append({
                    "status": "first-seen",
                    "observed_at": generated,
                    "source": "baike_public_snapshot",
                    "note": "First successful public HTML fetch of this Baike article",
                })
            elif prior_digest != digest:
                event = "rewrite"
                confirmations.append({
                    "status": "hash-changed",
                    "observed_at": generated,
                    "source": "baike_public_snapshot",
                    "note": "Public Baike article text hash changed; silent rewrite candidate",
                })
            else:
                event = "still_alive"
                confirmations.append({
                    "status": "last-confirmed-alive",
                    "observed_at": generated,
                    "source": "baike_public_snapshot",
                    "note": "Same public hash as the previous successful fetch",
                })
        else:
            n_unreachable += 1
            if prior_digest and interstitial == "deleted":
                event = "disappeared"
                confirmations.append({
                    "status": "disappeared",
                    "observed_at": generated,
                    "source": "baike_public_snapshot",
                    "note": "Public HTML now matches a Baike deleted interstitial",
                })
            elif prior_digest:
                event = "disappeared"
                confirmations.append({
                    "status": "disappeared",
                    "observed_at": generated,
                    "source": "baike_public_snapshot",
                    "note": f"Previously fetched Baike article now status={status}",
                })
        cdx = _cdx_trail(url, fetch_cdx)
        states[url] = {
            "content_sha256": digest or prior_digest,
            "first_seen": first_seen,
            "last_confirmed_alive": last_confirmed or prior.get("last_confirmed_alive"),
            "last_status": status,
            "last_event": event,
            "interstitial": interstitial or None,
            "term": page.get("term"),
            "cdx_digest": (cdx or {}).get("latest_digest"),
        }
        if event in {"unreachable", "login_walled"} and not prior_digest:
            continue
        snapshot = (cdx or {}).get("wayback_snapshot")
        observations.append(enrich_observation(
            {
                "terms": [page["term"]] if page.get("term") else [],
                "detected_at": generated,
                "title": f"[baike:{event}] {page.get('term') or url}",
                "text": text or public_text(page.get("why"), limit=400),
                "url": url,
                "source": "baike_public_snapshot",
                "deletion_signal": event,
                "domain": page.get("domain"),
            },
            text=text or public_text(page.get("why"), limit=400),
            source_url=url,
            first_seen=first_seen,
            last_seen=generated,
            last_confirmed_alive=last_confirmed,
            last_live_snapshot=snapshot,
            confirmations=confirmations,
            provenance={
                "collector": "baike_public_snapshot",
                "method": "public Baike HTML + Wayback CDX; no login API; no person pages",
                "vantage": "outside-china-public-source",
                "http_status": status,
                "interstitial": interstitial or None,
                "cdx_digest": (cdx or {}).get("latest_digest"),
                "schema_version": "palimpsest-china-observation.v1",
                "method_version": 1,
            },
        ))

    return {
        "generated_at": generated,
        "n_pages": len(watch),
        "n_ok": n_ok,
        "n_unreachable": n_unreachable,
        "n_login_walled": n_walled,
        "n_observations": len(observations),
        "pages": states,
        "observations": observations,
    }
