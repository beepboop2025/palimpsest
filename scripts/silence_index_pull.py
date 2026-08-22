"""Silence Index runner — score pre-emptive silence over the live DDTI terms
and publish readings/silence-index-latest.json + append the dated history.

processors/silence_index.py has been built and tested since it landed; this
driver is the missing wiring that makes it a live signal. The inputs are the
repo's own committed readings, so the run is auditable end to end:

  topics    readings/ddti-latest.json (the ranked deletion-pressure terms)
  global    GDELT DOC API via the processor's default enrich_fn (keyless;
            collectors/gdelt_cross_signal.py, already in the egress inventory)
  domestic  readings/weibo-hotsearch-latest.json join rows — the permitted
            outside-the-wall proxy the processor's docstring names. A DDTI
            term the weibo round did not evaluate returns None (abstain),
            NEVER 0.0: not-measured is not the same as absent.

V1 wires NO coupling_baseline_fn. The processor's non-bypassable guard then
scores only topics with gazetteer/lexicon corroboration and abstains on the
rest — mostly-abstaining early rounds are the designed cold start, not a
defect. The history this driver accrues (global_norm and domestic_norm per
scored topic per round) is exactly the data a future baseline needs; adding
coupling_baseline_fn is a METHOD_VERSION bump.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

# Bound at import time: the publish tests stub `datetime` with a clock that
# only fakes now(), and timestamp PARSING must keep working under that stub.
_fromisoformat = datetime.fromisoformat

from processors.silence_index import SilenceIndexProcessor, emit_observations

try:
    from core.governance import KillSwitch, RateCeiling
except Exception:  # pragma: no cover - governance is always present, but stay fail-soft
    KillSwitch = RateCeiling = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "silence-index-latest.json")
HIST = os.path.join(READINGS, "silence-index-history.jsonl")
DDTI = os.path.join(READINGS, "ddti-latest.json")
WEIBO = os.path.join(READINGS, "weibo-hotsearch-latest.json")

# Bumped when the METHOD changes in a way a reader must see (wiring a coupling
# baseline is the already-known next bump). The reading is rewritten
# unconditionally, so a method change can never hide behind an unchanged number.
METHOD_VERSION = 1

TOP_N = 25
# A domestic proxy older than this is a dead input, not a quiet one: every
# topic must abstain rather than read a stale board as current absence.
WEIBO_MAX_AGE_H = 36.0

_RATE_PER_SEC = 1 / 12
_BURST = 2.0


def _load(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _ddti_terms() -> list[dict]:
    d = _load(DDTI)
    ranked = (d or {}).get("ranked") or []
    # china_nexus=True by PROVENANCE: every DDTI term is drawn from the
    # censor's own deletion stream (the CDT archive), so the China nexus is
    # established by where the term came from. The processor's auto-gazetteer
    # gate exists for arbitrary inputs and misreads CDT's English topic
    # labels ("Economics", "activists") as having no nexus — verified live
    # 2026-08-01, when all 25 DDTI topics came back out_of_scope.
    return [
        {"term": t["term"], "attention": t.get("attention", 1.0),
         "recent_count": t.get("recent_count", 1), "china_nexus": True}
        for t in ranked[:TOP_N] if t.get("term")
    ]


def _domestic_map(now: datetime) -> dict | None:
    """{term: days-on-board fraction} from the weibo join, or None if the
    proxy is missing or stale (=> every topic abstains, the honest default)."""
    w = _load(WEIBO)
    if not w:
        return None
    try:
        t = _fromisoformat(str(w.get("generated_at", "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    if now - t > timedelta(hours=WEIBO_MAX_AGE_H):
        return None
    raw_window = w.get("window_days", 7)
    if isinstance(raw_window, list):
        window = len(raw_window)
    elif isinstance(raw_window, int) and not isinstance(raw_window, bool):
        window = raw_window
    else:
        return None
    if window <= 0:
        return None
    out = {}
    for row in w.get("join") or []:
        term = row.get("term")
        days_present = row.get("days_present")
        if isinstance(term, str) and isinstance(days_present, list):
            out[term] = min(1.0, len(days_present) / max(1, window))
    return out


def main() -> None:
    now = datetime.now(timezone.utc)

    terms = _ddti_terms()
    if not terms:
        print("no DDTI terms available — abstaining, not publishing")
        return

    domestic = _domestic_map(now)

    def domestic_volume_fn(term: str):
        # None means "this round could not measure the domestic side for this
        # term" — the processor abstains. Only a term the weibo join actually
        # evaluated gets a number.
        if domestic is None:
            return None
        return domestic.get(term)

    kill = KillSwitch() if KillSwitch else None
    rate = RateCeiling(rate=_RATE_PER_SEC, capacity=_BURST) if RateCeiling else None
    proc = SilenceIndexProcessor(
        domestic_volume_fn=domestic_volume_fn,
        kill_switch=kill,
        rate_ceiling=rate,
    )
    readings = proc.build_readings(terms)

    # "scored" means a real silence reading was produced. out_of_scope rows
    # are neither scored nor abstentions — they are jurisdiction statements.
    scored = [r for r in readings
              if r.get("label") in ("blackout", "containment", "coupled")]
    blackouts = [r for r in scored if r.get("label") == "blackout"]
    containment = [r for r in scored if r.get("label") == "containment"]
    n_out_of_scope = sum(1 for r in readings if r.get("label") == "out_of_scope")

    # A blind round (nothing scored, everything abstained) writes NULL
    # blackout/containment counts, not zeros: the conformal detector reads
    # n_blackout straight from these rows and skips non-numeric values, so a
    # null is "the instrument could not see" while a 0 would be "looked and
    # found calm" — the one lie this signal must never tell.
    hist_row = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_topics_considered": len(terms),
        "n_scored": len(scored),
        "n_abstained": sum(1 for r in readings if r.get("abstained")),
        "n_out_of_scope": n_out_of_scope,
        "n_blackout": len(blackouts) if scored else None,
        "n_containment": len(containment) if scored else None,
        "scored": [
            {"topic": r["topic"], "label": r["label"],
             "silence_score": r["silence_score"],
             "global_norm": r["global_norm"], "domestic_norm": r["domestic_norm"]}
            for r in scored
        ],
    }
    os.makedirs(READINGS, exist_ok=True)
    with open(HIST, "a", encoding="utf-8") as f:
        f.write(json.dumps(hist_row, ensure_ascii=False, sort_keys=True) + "\n")

    latest = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method_version": METHOD_VERSION,
        "source": (
            "DDTI ranked terms (readings/ddti-latest.json) x GDELT DOC API "
            "global volume x weibo hot-search join as the permitted domestic "
            "proxy (readings/weibo-hotsearch-latest.json)"
        ),
        "method_note": (
            "pre-emptive silence: topics loud abroad and gone at home, the "
            "censorship that leaves no deletion to count. Guard rails are the "
            "processor's, not this runner's: no China nexus -> out_of_scope; "
            "missing global or domestic input -> abstain; and with no coupling "
            "baseline wired (v1), a topic without gazetteer corroboration "
            "abstains rather than scores — a topic China never covered being "
            "quiet at home is disinterest, not suppression. An abstention is "
            "published as an abstention, never as a zero. DDTI terms carry "
            "china_nexus by provenance: they are drawn from the censor's own "
            "deletion stream, so this runner sets the nexus flag at source "
            "and the processor's per-term gazetteer gate is deliberately "
            "bypassed for them (it still guards any non-DDTI deployment). "
            "A blind round (nothing scored) records null blackout counts in "
            "both the latest reading and the history, never zeros"
        ),
        "n_topics_considered": len(terms),
        "n_scored": len(scored),
        "n_abstained": sum(1 for r in readings if r.get("abstained")),
        "n_out_of_scope": n_out_of_scope,
        "n_blackout": len(blackouts) if scored else None,
        "n_containment": len(containment) if scored else None,
        "domestic_proxy_live": domestic is not None,
        "top": [
            {k: r.get(k) for k in
             ("topic", "label", "silence_score", "decoupling", "global_norm",
              "domestic_norm", "lexicon_hit", "abstained")}
            for r in readings[:TOP_N]
        ],
        "observations": emit_observations(readings, now),
        "note": (
            "high n_blackout = more topics loud abroad while absent from the "
            "domestic board; the Peng Shuai / youth-unemployment shape, watched "
            "continuously"
        ),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1, sort_keys=True)

    print(f"silence-index: {len(terms)} topics, {len(scored)} scored, "
          f"{len(blackouts)} blackout, {len(containment)} containment, "
          f"{latest['n_abstained']} abstained"
          + ("" if domestic is not None else " (domestic proxy dark — all abstained)"))


if __name__ == "__main__":
    main()
