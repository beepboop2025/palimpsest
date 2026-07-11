"""net4people/bbs event stream — the community's live log of China network
blocking and the circumvention arms race, read via the GitHub Issues API.

net4people/bbs is the de-facto real-time board where researchers post new GFW
blocking events (port blocks, SNI/QUIC censorship, active probing) and new
circumvention developments. It is a public overseas GitHub repo, so ingesting
its issues is vantage-insensitive — no probing, no in-China presence. This is
the qualitative "what just happened at the firewall" companion to the
quantitative OONI anomaly signal.

Standard-library only (urllib + json). Keyless works (GitHub's 60/hr anon
limit is plenty for one pull); if GITHUB_TOKEN is set (it is, in Actions) we
use it for the 5000/hr limit.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

API = "https://api.github.com/repos/net4people/bbs/issues"
USER_AGENT = "palimpsest.info observatory (net4people/bbs public-issue ingest)"

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
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def fetch_issues(per_page: int = 60, timeout: float = 25.0) -> list[dict] | None:
    """Most-recently-created issues (any state; net4people rarely closes them).
    Fail-soft: None on error so the runner abstains rather than false-zero."""
    url = f"{API}?state=all&sort=created&direction=desc&per_page={per_page}"
    req = urllib.request.Request(url, headers=_headers())
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read(6 * 1024 * 1024)
        data = json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as e:
        log.warning("net4people fetch failed: %s", e)
        return None
    if not isinstance(data, list):
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
    labels = [l.get("name") for l in (issue.get("labels") or []) if l.get("name")]
    title = issue.get("title") or ""
    return {
        "number": issue.get("number"),
        "title": title,
        "url": issue.get("html_url"),
        "created_at": issue.get("created_at"),
        "labels": labels,
        "comments": issue.get("comments", 0),
        "kind": classify(title),
        # China is the repo's default focus; flag the ones explicitly tagged China
        "china_tagged": any(l.lower() == "china" for l in labels),
    }
