"""Weibo hot-search runner — pull the community archive, join it against the
live DDTI ranking, and publish the allowed-attention signal.

Mirrors the stock-connect pull pattern: collect -> honesty-guard/abstain ->
write latest + append the dated history. The archive repo holds the long
record; our history file keeps only the per-day JOIN summary (the derived
signal), not a mirror of the archive.

The join uses only CJK-bearing DDTI terms: hot-search titles are Chinese, and
matching an English CDT topic label against them would manufacture absence.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

from collectors.weibo_hotsearch import (
    collect_range, join_ddti, pinned_series, term_presence,
    withdrawal_candidates)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "weibo-hotsearch-latest.json")
HIST = os.path.join(READINGS, "weibo-hotsearch-history.jsonl")

# Bumped when the METHOD changes in a way a reader must see. This driver rewrites
# the reading unconditionally, so it cannot hide a method change behind an
# unchanged number. Carried as provenance: a reader diffing two history rows
# needs to know whether the method moved underneath them.
#   1: any gazetteer substring hit on a board title counted as a breakthrough.
#   2: hits are sense-gated (2026-08-01). A term whose everyday sense dominates
#      the board counts only when its title context does not read as that
#      everyday sense, and every gated-out hit ships in the reading as
#      sense_filtered rather than being dropped in silence. See _SENSE_RULES in
#      collectors/weibo_hotsearch.py for the rule and its stated recall cost.
#      All four breakthroughs in the last v1 reading were everyday-sense
#      artifacts, so a fall in the count across this boundary is the method
#      moving and not the board getting cleaner. The breakthrough series closes
#      here rather than being re-baselined.
METHOD_VERSION = 2

DDTI = os.path.join(READINGS, "ddti-latest.json")
GAZETTEER = os.path.join(ROOT, "config", "zh_censorship_gazetteer.json")

WINDOW_DAYS = 7
_HAS_CJK = re.compile(r"[㐀-鿿]")


def _load_ddti_terms() -> list[dict]:
    try:
        with open(DDTI, encoding="utf-8") as f:
            ranked = json.load(f).get("ranked") or []
    except (OSError, json.JSONDecodeError):
        return []
    return [t for t in ranked if _HAS_CJK.search(t.get("term") or "")]


def _load_gazetteer_terms() -> list[dict]:
    """[{term, category}] for every zh gazetteer entry (2+ chars — single
    characters substring-match half the board and would manufacture hits)."""
    try:
        with open(GAZETTEER, encoding="utf-8") as f:
            cats = json.load(f).get("categories") or {}
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for cat, entries in cats.items():
        for e in entries:
            zh = (e.get("zh") or "").strip()
            if len(zh) >= 2:
                out.append({"term": zh, "category": cat})
    return out


def _observation_records(now, joined, breakthroughs, days, withdrawals=None) -> list[dict]:
    """Project join/breakthrough/withdrawal rows onto the shared China observation schema."""

    from core.china_observation import enrich_observation, serialize_observation

    records = []
    for row in list(joined) + list(breakthroughs):
        term = row.get("term") or ""
        if not term:
            continue
        regime = row.get("regime") or (
            "gazetteer_breakthrough" if row.get("appearances") else "unclassified"
        )
        titles = []
        for sample in row.get("samples") or []:
            if isinstance(sample, dict) and sample.get("title"):
                titles.append(str(sample["title"]))
        text = "\n".join(titles + [term]) if titles else term
        raw = {
            "terms": [term],
            "title": titles[0] if titles else f"[weibo-hotsearch:{regime}] {term}",
            "text": text,
            "url": "",
            "source": "weibo-hotsearch",
            "regime": regime,
        }
        records.append(serialize_observation(enrich_observation(
            raw,
            text=text,
            provenance={
                "collector": "weibo-hotsearch",
                "method": "justjavac weibo-trending-hot-search archive join",
                "vantage": "outside-china-public-archive",
                "schema_version": "palimpsest-china-observation.v1",
                "method_version": METHOD_VERSION,
                "board_days": len(days),
            },
        )))
    for row in withdrawals or []:
        if not isinstance(row, dict):
            continue
        title = row.get("title") or ""
        terms = [str(t) for t in (row.get("matched_terms") or []) if t]
        if not title and not terms:
            continue
        text = title or "; ".join(terms)
        raw = {
            "terms": terms,
            "title": title or f"[weibo-hotsearch:withdrawal_watch] {terms[0]}",
            "text": text,
            "url": "",
            "source": "weibo-hotsearch",
            "regime": "withdrawal_watch",
        }
        records.append(serialize_observation(enrich_observation(
            raw,
            text=text,
            provenance={
                "collector": "weibo-hotsearch",
                "method": "justjavac weibo-trending-hot-search withdrawal watch",
                "vantage": "outside-china-public-archive",
                "schema_version": "palimpsest-china-observation.v1",
                "method_version": METHOD_VERSION,
                "board_days": len(days),
            },
        )))
    return records[:256]


def main() -> None:
    now = datetime.now(timezone.utc)
    dates = [(now - timedelta(days=i)).strftime("%Y-%m-%d")
             for i in range(WINDOW_DAYS, -1, -1)]
    days = collect_range(dates)

    # Honesty guard: no day parsed (archive down / format change) -> abstain.
    if not days:
        print("weibo hot-search archive returned nothing parseable — abstaining")
        return

    ddti_terms = _load_ddti_terms()
    joined = join_ddti(ddti_terms, days)
    suppressed = [j for j in joined if j["regime"] == "suppressed_invisible"]
    contained = [j for j in joined if j["regime"] == "contained_visible"]

    # Gazetteer breakthrough: a KNOWN-censored term appearing on the curated
    # board is the anomaly worth flagging (an anniversary the apparatus let
    # through, a sensitivity boundary moving, or an event too big to hold).
    # Absence of a gazetteer term with no live deletion pressure is expected
    # background and deliberately NOT labeled a regime.
    breakthroughs = []
    for g in _load_gazetteer_terms():
        pres = term_presence(g["term"], days)
        if pres["appearances"]:
            breakthroughs.append({**pres, "category": g["category"]})
    breakthroughs.sort(key=lambda b: b["appearances"], reverse=True)
    withdrawal_watch = withdrawal_candidates(
        days,
        sensitive_terms={g["term"] for g in _load_gazetteer_terms()}
        | {t["term"] for t in ddti_terms},
    )

    latest = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method_version": METHOD_VERSION,
        "source": ("Sina Weibo hot-search board via the MIT-licensed archive "
                   "github.com/justjavac/weibo-trending-hot-search "
                   "(hourly captures, per-day union; keyless raw fetch)"),
        "window_days": sorted(days),
        "board_entries": sum(len(v) for v in days.values()),
        "ddti_terms_joined": len(joined),
        "regimes": {
            "suppressed_invisible": len(suppressed),
            "contained_visible": len(contained),
        },
        "join": joined,
        "gazetteer_breakthroughs": breakthroughs,
        "withdrawal_watch": withdrawal_watch,
        "observation_records": _observation_records(
            now, joined, breakthroughs, days,
            withdrawals=withdrawal_watch.get("candidates"),
        ),
        "pinned_headlines": pinned_series(days),
        "method_note": (
            "The hot-search board is itself a censored surface (curated top "
            "slot, on-command withdrawals), so it is read here as the record "
            "of PERMITTED attention, never as neutral volume. A DDTI term "
            "under deletion pressure that also trends is CONTAINED_VISIBLE "
            "(pruned, not hidden); one absent from the board while hot in the "
            "deletion stream is SUPPRESSED_INVISIBLE (deleted AND denied "
            "attention) — the content-layer sibling of the Silence Index. "
            "attention_ratio is threat share over permitted-attention share, "
            "published with both parts."
        ),
    }
    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1, sort_keys=True)

    hist_row = {
        "date": max(days),
        "generated_at": latest["generated_at"],
        "board_entries": latest["board_entries"],
        "ddti_terms_joined": len(joined),
        "suppressed_invisible": len(suppressed),
        "contained_visible": len(contained),
        "suppressed_terms": [j["term"] for j in suppressed][:40],
        "gazetteer_breakthroughs": [b["term"] for b in breakthroughs][:40],
    }
    with open(HIST, "a", encoding="utf-8") as f:
        f.write(json.dumps(hist_row, ensure_ascii=False, sort_keys=True) + "\n")

    print(f"weibo-hotsearch: {len(days)} days, {latest['board_entries']} board "
          f"entries, {len(joined)} DDTI terms joined "
          f"({len(suppressed)} suppressed_invisible / {len(contained)} contained_visible)")


if __name__ == "__main__":
    main()
