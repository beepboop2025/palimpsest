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


def withdrawal_candidates(days: dict[str, list[dict]], top_rank: int = 10,
                          sensitive_terms: set | None = None) -> dict:
    """One-day high-rank exits — the day-granularity read of 撤热搜 withdrawal.

    A topic that reaches the board's top ranks and vanishes after a single day
    is a candidate for on-command withdrawal. This is the deterministic,
    day-level version of trending-survival analysis: per-day archive files
    give residence in DAYS, not hours, so the honest unit here is "days on
    the board".

    MEASURED, not assumed: on this board a one-day top-10 exit is the NORM
    (live baseline ≈ 23% persistence — sports and entertainment churn daily),
    so a bare one-day exit is NOT evidence of withdrawal. The published shape
    is therefore: aggregate counts + the baseline persistence rate as the
    context statistic, and a NAMED candidate list only for exits whose title
    carries known-sensitive vocabulary (sensitive_terms) — the intersection
    where fast exit and sensitivity coincide.

    Right-censoring, handled not ignored: a topic that debuts on the LAST day
    of the window has had no chance to persist, so last-day debuts are
    excluded from both numerator and baseline — never counted as exits.
    """
    dates = sorted(days)
    if len(dates) < 3:
        return {"baseline_persist_rate": None, "candidates": [],
                "note": "window too short — warming up"}
    last = dates[-1]

    # first-seen date, days present, best rank per title
    seen: dict[str, dict] = {}
    for date in dates:
        for e in days[date]:
            t = e["title"]
            rec = seen.setdefault(t, {"first": date, "days": 0, "best_rank": None})
            rec["days"] += 1
            r = e["rank"]
            if r is not None and (rec["best_rank"] is None or r < rec["best_rank"]):
                rec["best_rank"] = r

    top = {t: r for t, r in seen.items()
           if r["best_rank"] is not None and r["best_rank"] <= top_rank
           and r["first"] != last}    # right-censored debuts excluded
    if not top:
        return {"baseline_persist_rate": None, "candidates": [],
                "note": "no uncensored top-rank topics in window"}

    persisted = sum(1 for r in top.values() if r["days"] >= 2)
    exits = sorted(
        ({"title": t, "best_rank": r["best_rank"], "date": r["first"]}
         for t, r in top.items() if r["days"] == 1),
        key=lambda c: c["best_rank"])
    sens = sensitive_terms or set()
    candidates = [
        {**c, "matched_terms": sorted(s for s in sens if s in c["title"])}
        for c in exits if any(s in c["title"] for s in sens)]
    return {
        "baseline_persist_rate": round(persisted / len(top), 4),
        "top_topics_considered": len(top),
        "one_day_exits": len(exits),
        "candidates": candidates,
    }


def pinned_series(days: dict[str, list[dict]]) -> list[dict]:
    """The state's chosen headline per day (置顶 top slot), where captured."""
    out = []
    for date in sorted(days):
        pins = [e["title"] for e in days[date] if e["pinned"]]
        if pins:
            out.append({"date": date, "pinned": pins})
    return out
