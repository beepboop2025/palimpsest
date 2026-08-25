"""net4people/bbs event stream — the community's live log of China network
blocking and the circumvention arms race, read via the GitHub Issues API.

net4people/bbs is the de-facto real-time board where researchers post new GFW
blocking events (port blocks, SNI/QUIC censorship, active probing) and new
circumvention developments. It is a public overseas GitHub repo, so ingesting
its issues is vantage-insensitive — no probing, no in-China presence. This is
the qualitative "what just happened at the firewall" companion to the
quantitative OONI anomaly signal.

Standard-library only (shared safe transport + json). Keyless works (GitHub's 60/hr anon
limit is plenty for one pull); if GITHUB_TOKEN is set (it is, in Actions) we
use it for the 5000/hr limit.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable

from core.safe_fetch import FetchError, safe_fetch_bytes

log = logging.getLogger(__name__)

API = "https://api.github.com/repos/net4people/bbs/issues"
USER_AGENT = "palimpsest.info observatory (net4people/bbs public-issue ingest)"
MAX_RESPONSE_BYTES = 6 * 1024 * 1024
MAX_TITLE_CHARS = 500
MAX_LABELS = 32

# Title keywords that separate a *blocking/disruption event* from a
# *circumvention development*. Rough but transparent; the raw title is always
# kept so a reader can judge.
BLOCK_HINTS = ("block", "censor", "throttl", "disrupt", "outage", "banned",
               "ban ", "reset", "rst", "dns poison", "sni", "quic", "port 443",
               "port block", "probe", "probing", "interfer", "gfw", "firewall",
               "slowdown", "unreachable", "down in china", "blackout")
CIRCUMVENT_HINTS = ("relay", "proxy", "vless", "reality", "shadowsocks", "vpn",
                    "bridge", "obfs", "tunnel", "circumvent", "bypass", "psiphon",
                    "tor ", "snowflake", "hysteria", "naive", "trojan", "xray")


def _headers() -> dict:
    h = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    tok = os.getenv("GITHUB_TOKEN")
    if tok and len(tok) <= 4096 and not any(ord(char) < 0x21 or ord(char) > 0x7E for char in tok):
        h["Authorization"] = f"Bearer {tok}"
    return h


def _reject_constant(_value: str):
    raise ValueError("non-finite JSON number")


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def fetch_issues(
    per_page: int = 60,
    timeout: float = 25.0,
    *,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
) -> list[dict] | None:
    """Most-recently-created issues (any state; net4people rarely closes them).
    Fail-soft: None on error so the runner abstains rather than false-zero."""
    if type(per_page) is not int or not 1 <= per_page <= 100:
        log.warning("net4people refused an invalid page size")
        return None
    url = f"{API}?state=all&sort=created&direction=desc&per_page={per_page}"

    def exact_url(candidate: str) -> None:
        if candidate != url:
            raise FetchError("net4people request URL changed")

    try:
        raw = fetcher(
            url,
            timeout=timeout,
            max_bytes=MAX_RESPONSE_BYTES,
            max_redirects=0,
            headers=_headers(),
            url_policy=exact_url,
        )
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FetchError("net4people response exceeded its byte budget")
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except Exception as exc:  # noqa: BLE001 — fail soft on hostile upstream
        log.warning("net4people fetch failed (%s)", type(exc).__name__)
        return None
    if (
        not isinstance(data, list)
        or len(data) > per_page
        or any(not isinstance(issue, dict) for issue in data)
    ):
        return None
    return data


def classify(title: str) -> str:
    t = (title or "").lower()
    is_block = any(k in t for k in BLOCK_HINTS)
    is_circ = any(k in t for k in CIRCUMVENT_HINTS)
    if is_block and not is_circ:
        return "blocking"
    if is_circ and not is_block:
        return "circumvention"
    if is_block and is_circ:
        return "mixed"
    return "other"


def normalize(issue: dict) -> dict:
    if not isinstance(issue, dict):
        issue = {}
    raw_labels = issue.get("labels") or []
    labels = []
    if isinstance(raw_labels, list) and len(raw_labels) <= MAX_LABELS:
        labels = [
            label["name"][:100]
            for label in raw_labels
            if isinstance(label, dict)
            and type(label.get("name")) is str
            and label["name"]
        ]
    title = issue.get("title") if type(issue.get("title")) is str else ""
    title = title[:MAX_TITLE_CHARS]
    number = issue.get("number")
    if type(number) is not int or not 1 <= number <= 10**12:
        number = None
    expected_url = f"https://github.com/net4people/bbs/issues/{number}" if number else None
    url = issue.get("html_url") if issue.get("html_url") == expected_url else None
    created_at = issue.get("created_at")
    if type(created_at) is not str or len(created_at) > 64:
        created_at = None
    comments = issue.get("comments", 0)
    if type(comments) is not int or not 0 <= comments <= 10**9:
        comments = 0
    return {
        "number": number,
        "title": title,
        "url": url,
        "created_at": created_at,
        "labels": labels,
        "comments": comments,
        "kind": classify(title),
        # China is the repo's default focus; flag the ones explicitly tagged China
        "china_tagged": any(label.lower() == "china" for label in labels),
    }
