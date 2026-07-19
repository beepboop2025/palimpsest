"""Weibo hot-search — the ALLOWED-ATTENTION denominator the DDTI has been missing.

`processors/ddti_index.py` states its own limit plainly: CDT gives a numerator
(censored items) with no denominator (what the public was allowed to attend to),
so its "selectivity" is censor attention allocation, not a true rate. This
collector supplies the missing axis from the public record: the Sina Weibo
hot-search board (微博热搜), as archived hourly since 2020-11-24 by the
MIT-licensed community repo `justjavac/weibo-trending-hot-search` (one JSON
file per day, fetched keyless from raw.githubusercontent.com — no in-China
vantage, no Weibo account, no scraping by us).

HONEST SCOPE, stated up front: the hot-search board is itself a censored
surface. Weibo curates it under CAC direction (top slot 置顶 is editorially
pinned state messaging; entries are withdrawn on command — 撤热搜). So this is
NOT a neutral topic-volume denominator; it is the record of what the apparatus
PERMITS the public to attend to. That is exactly why the JOIN is informative.
Crossing the deletion stream (what the censor removes) with the hot-search
board (what it permits to trend) separates two regimes a deletion count alone
cannot distinguish:

  CONTAINED-VISIBLE — a term the DDTI shows under deletion pressure that
      nonetheless trends: the topic is too big to disappear, so the censor
      prunes posts while conceding attention. Deletion is the *rearguard*.
  SUPPRESSED-INVISIBLE — a term under deletion pressure that never surfaces
      on the board at all: numerator hot, denominator zero. The apparatus is
      both deleting the posts AND holding the topic off the attention surface.
      This is the content-layer sibling of the Silence Index's news blackout.

For each DDTI term the collector reports presence, best rank, and appearance
count over the window, the regime label above, and an attention-normalised
selectivity (censor attention share over permitted public attention share) —
published as a RATIO WITH ITS PARTS, never as a bare number.

The pinned top slot (Refer=new_time, no rank) is kept as its own series: it is
the state's chosen headline, a small daily read of what the apparatus wants
attended to — the exact complement of what it deletes.

Standard-library only (urllib + json). Fetch is fail-soft: a missing day is a
statement of absence, never zero.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

RAW_URL = ("https://raw.githubusercontent.com/justjavac/"
           "weibo-trending-hot-search/master/raw/{date}.json")
USER_AGENT = ("palimpsest.info observatory (public-archive ingest; "
              "contact desk@palimpsest.info)")
SPACING_S = 0.6            # polite gap between per-day fetches
_BAND_RANK = re.compile(r"band_rank=(\d+)")


def _get_raw(date_iso: str, timeout: float = 30.0) -> str | None:
    """Fetch one day's archive file. Fail-soft: None on any transport error."""
    req = urllib.request.Request(
        RAW_URL.format(date=date_iso), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — abstain, never fake
        log.warning("weibo-hotsearch %s fetch failed: %s", date_iso, exc)
        return None


def parse_day(raw: str) -> list[dict] | None:
    """One day's archive JSON -> [{title, rank, pinned}] or None.

    The archive is a JSON array of {url, title}; the day's union of every
    hourly snapshot, so one title appears once regardless of dwell time.
    Rank comes from the ``band_rank`` query parameter (best rank at capture);
    the editorially pinned state slot carries ``Refer=new_time`` and no rank.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        url = item.get("url") or ""
        m = _BAND_RANK.search(url)
        out.append({
            "title": title,
            "rank": int(m.group(1)) if m else None,
            "pinned": "Refer=new_time" in url,
        })
    return out or None


def collect_range(dates: list[str], fetch=_get_raw) -> dict[str, list[dict]]:
    """Fetch + parse a list of ISO dates. Missing/unparseable days are absent
    from the result — absence is reported, never imputed."""
    out: dict[str, list[dict]] = {}
    for i, d in enumerate(dates):
        if i:
            time.sleep(SPACING_S)
        raw = fetch(d)
        if raw is None:
            continue
        parsed = parse_day(raw)
        if parsed:
            out[d] = parsed
    return out


# ── the join: deletion stream × allowed attention ──────────────────────────────

def term_presence(term: str, days: dict[str, list[dict]]) -> dict:
    """Presence of one (Chinese, substring-matched) term across the window.

    Substring match on the raw title, same convention as the DDTI's zh term
    extraction — hot-search titles are short and unsegmented, and translation
    or tokenisation would destroy the very coinages we look for.
    """
    days_present, appearances, best_rank, samples = [], 0, None, []
    for date in sorted(days):
        hit = False
        for e in days[date]:
            if term in e["title"]:
                appearances += 1
                hit = True
                r = e["rank"]
                if r is not None and (best_rank is None or r < best_rank):
                    best_rank = r
                if len(samples) < 3:   # evidence: the exact board titles matched
                    samples.append({"date": date, "title": e["title"], "rank": r})
        if hit:
            days_present.append(date)
    return {"term": term, "days_present": days_present,
            "appearances": appearances, "best_rank": best_rank,
            "samples": samples}


def join_ddti(ddti_terms: list[dict], days: dict[str, list[dict]]) -> list[dict]:
    """Cross each DDTI term with the hot-search record and label the regime.

    ddti_terms: [{"term": str, "threat": float, ...}] — the published DDTI
    ranking (threat = censor attention share, already normalised upstream).

    Regime labels:
      contained_visible    — under deletion pressure AND trending: the censor
                             prunes what it cannot hide.
      suppressed_invisible — under deletion pressure, absent from the board:
                             deletion plus attention denial. The strong signal.

    attention_ratio = term threat share ÷ term share of permitted attention
    (appearance share over the window). Published with both parts; None when
    the term never trended (division by the absent — reported as absence, and
    that absence IS the suppressed_invisible label, not a numeric).
    """
    if not days:
        return []
    total_entries = sum(len(v) for v in days.values()) or 1
    out = []
    for t in ddti_terms:
        term = (t.get("term") or "").strip()
        if not term:
            continue
        pres = term_presence(term, days)
        threat = t.get("threat")
        share = pres["appearances"] / total_entries
        regime = "contained_visible" if pres["appearances"] else "suppressed_invisible"
        ratio = None
        if pres["appearances"] and isinstance(threat, (int, float)) and threat > 0:
            ratio = round(threat / share, 2)
        out.append({
            **pres,
            "threat": threat,
            "attention_share": round(share, 6),
            "attention_ratio": ratio,
            "regime": regime,
        })
    return out


def pinned_series(days: dict[str, list[dict]]) -> list[dict]:
    """The state's chosen headline per day (置顶 top slot), where captured."""
    out = []
    for date in sorted(days):
        pins = [e["title"] for e in days[date] if e["pinned"]]
        if pins:
            out.append({"date": date, "pinned": pins})
    return out
