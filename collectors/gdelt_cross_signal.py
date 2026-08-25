"""GDELT cross-signal — "censored at home, loud abroad".

A single deletion stream tells you what the censor is *touching*. It cannot, on its
own, tell you whether a topic is genuinely consequential or merely a domestic
sensitivity. Triangulating against the open global media record closes that gap.

GDELT (the Global Database of Events, Language, and Tone) indexes worldwide news in
real time and exposes a free, key-less JSON API. We query it for the *global* volume
of coverage on a term and compare that to the term's *domestic* censorship
attention. Two readings fall out:

  CONTAINMENT  — the topic is loud in the world's press AND heavily censored at home.
                 The state is actively suppressing a story the rest of the world is
                 already reporting. This is the high-confidence signal.

  BLACKOUT     — the topic is loud abroad but conspicuously ABSENT at home. Suppression
                 by silence rather than by visible deletion: the cleanest censorship
                 leaves no deletion to count, only a hole where coverage should be.

This module is deliberately standard-library only (shared safe transport + json), so it runs and is
testable with no third-party dependency. The scoring core is pure and offline; only
`fetch_global_volume()` touches the network, and it fails soft (returns None) so a
blocked or rate-limited GDELT never corrupts the index — it just abstains.

OSINT posture: GDELT aggregates already-published news. Nothing here collects, stores,
or reasons about any private individual; it measures coverage volume of a *topic*.
"""

import json
import logging
import math
import re
from collections.abc import Callable
import urllib.parse

from core.safe_fetch import FetchError, safe_fetch_bytes

logger = logging.getLogger(__name__)

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
USER_AGENT = "Palimpsest/0.2 (open-source censorship research; GDELT cross-signal)"

# GDELT "timelinevol" returns a normalized volume-intensity series (roughly a percent
# of all monitored coverage). We treat ~5% as a saturating "very loud globally" anchor;
# above it, a topic is already a major world story. Tunable; documented as a knob.
GLOBAL_VOLUME_SATURATION = 5.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TERM_CHARS = 256
MAX_TERMS_PER_RUN = 64
MAX_TIMELINE_POINTS = 2_048
_TIMESPAN = re.compile(r"(?:[1-9]|[1-9][0-9]|[12][0-9]{2}|3[0-5][0-9]|360)(?:min|h|d|w|m)\Z")


# ── scoring core (pure, offline, testable) ───────────────────────────────────

def normalize_global(volume_intensity: float,
                     saturation: float = GLOBAL_VOLUME_SATURATION) -> float:
    """Map a GDELT volume-intensity (percent-of-coverage) to [0,1].

    Saturating-linear: 0 at no coverage, 1 once the topic reaches `saturation`% of
    monitored global coverage. Saturating (rather than raw) keeps one mega-story from
    dwarfing every other live signal in the index.
    """
    if (
        isinstance(volume_intensity, bool)
        or not isinstance(volume_intensity, (int, float))
        or not math.isfinite(float(volume_intensity))
        or volume_intensity <= 0
    ):
        return 0.0
    if (
        isinstance(saturation, bool)
        or not isinstance(saturation, (int, float))
        or not math.isfinite(float(saturation))
        or saturation <= 0
    ):
        return 0.0
    return min(1.0, float(volume_intensity) / float(saturation))


def cross_signal(domestic_attention: float,
                 domestic_present: bool,
                 global_volume_intensity: float,
                 *,
                 saturation: float = GLOBAL_VOLUME_SATURATION) -> dict:
    """Combine domestic censorship attention with global coverage into one reading.

    Args:
        domestic_attention: the DDTI attention score for the term at home — how much
            *censorship* the term is drawing (deletions / flags), recency-weighted.
            Higher = the apparatus is working harder on it.
        domestic_present: whether the term appears at all in reachable domestic public
            discourse this window. False ⇒ candidate BLACKOUT (suppression by absence).
        global_volume_intensity: GDELT volume-intensity for the term (percent-ish), or
            None if GDELT was unreachable (the caller should then skip cross-scoring).

    Returns a dict with the normalized global score, a label, and a `cross_score` in
    [0,1] that is high only when global salience and domestic suppression coincide.

    Design choice (documented tuning point): containment multiplies normalized global
    salience by a bounded domestic-attention factor, so a term must be BOTH globally
    real AND domestically suppressed to score high — neither alone suffices. Blackout
    is scored from global salience alone (there is no domestic attention to multiply,
    because the topic has been driven to silence), but flagged distinctly so an analyst
    can weigh "loud-and-deleted" against "loud-and-missing" differently.
    """
    g = normalize_global(global_volume_intensity, saturation)
    # bounded domestic factor: saturating so a single loud term can't run away
    dom = domestic_attention / (1.0 + domestic_attention) if domestic_attention > 0 else 0.0

    if not domestic_present and g > 0:
        label = "blackout"
        cross_score = g                      # suppression by absence: global salience only
    elif domestic_present and g > 0:
        label = "containment"
        cross_score = g * dom                # loud abroad AND actively censored at home
    else:
        label = "domestic_only"              # GDELT sees nothing notable abroad
        cross_score = 0.0

    return {
        "label": label,
        "cross_score": round(cross_score, 4),
        "global_norm": round(g, 4),
        "global_volume_intensity": global_volume_intensity,
        "domestic_attention": round(domestic_attention, 4),
        "domestic_present": domestic_present,
    }


def rank_cross_signals(terms: list[dict],
                       saturation: float = GLOBAL_VOLUME_SATURATION) -> list[dict]:
    """Score and rank a batch of terms.

    Each input term: {"term": str, "domestic_attention": float,
                      "domestic_present": bool, "global_volume_intensity": float|None}.
    Terms whose GDELT lookup failed (global_volume_intensity is None) are scored as
    abstentions (cross_score 0, label "unknown") rather than dropped, so the absence of
    a global reading is visible rather than silently treated as zero coverage.
    """
    out = []
    for t in terms:
        gv = t.get("global_volume_intensity")
        if gv is None:
            out.append({**t, "label": "unknown", "cross_score": 0.0,
                        "global_norm": None, "abstained": True})
            continue
        scored = cross_signal(
            t.get("domestic_attention", 0.0),
            bool(t.get("domestic_present", True)),
            gv,
            saturation=saturation,
        )
        out.append({"term": t["term"], **scored})
    out.sort(key=lambda r: (r["cross_score"], r.get("global_norm") or 0.0), reverse=True)
    return out


# ── network (fails soft; the only impure function here) ──────────────────────

def _reject_constant(_value: str):
    raise ValueError("non-finite JSON number")


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _bounded_term(term: object) -> str | None:
    if type(term) is not str:
        return None
    candidate = term.strip()
    if (
        not candidate
        or len(candidate) > MAX_TERM_CHARS
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in candidate)
    ):
        return None
    return candidate


def fetch_global_volume(
    term: str,
    timespan: str = "1w",
    timeout: float = 20.0,
    *,
    fetcher: Callable[..., bytes] = safe_fetch_bytes,
):
    """Return GDELT mean volume-intensity for `term` over `timespan`, or None on any
    failure. Key-less GDELT DOC 2.0 `timelinevol` endpoint.

    Returns None (never raises) on network error, rate-limit, bad JSON, or empty
    series — so a flaky GDELT degrades the cross-signal to "unknown/abstain" rather
    than poisoning the index with a false zero.
    """
    query_term = _bounded_term(term)
    if query_term is None or type(timespan) is not str or not _TIMESPAN.fullmatch(timespan):
        return None
    params = {
        "query": f'"{query_term}"',
        "mode": "timelinevol",
        "format": "json",
        "timespan": timespan,
    }
    url = f"{GDELT_DOC_API}?{urllib.parse.urlencode(params)}"

    def exact_url(candidate: str) -> None:
        if candidate != url:
            raise FetchError("GDELT request URL changed")

    try:
        raw = fetcher(
            url,
            timeout=timeout,
            max_bytes=MAX_RESPONSE_BYTES,
            max_redirects=0,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            url_policy=exact_url,
        )
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FetchError("GDELT response exceeded its byte budget")
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except Exception as exc:  # noqa: BLE001 — hostile upstream means abstention
        logger.debug("[GDELT] lookup failed (%s)", type(exc).__name__)
        return None
    if not isinstance(data, dict):
        return None
    series = data.get("timeline") or []
    if not isinstance(series, list) or len(series) != 1 or not isinstance(series[0], dict):
        return None
    points = series[0].get("data") or []
    if not isinstance(points, list) or not 1 <= len(points) <= MAX_TIMELINE_POINTS:
        return None
    vals = []
    for point in points:
        if not isinstance(point, dict):
            return None
        value = point.get("value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= float(value) <= 100
        ):
            return None
        vals.append(float(value))
    if not vals:
        return None
    return sum(vals) / len(vals)


def enrich_terms(domestic_terms: list[dict], timespan: str = "1w") -> list[dict]:
    """Convenience: take ranked domestic terms (from the DDTI index) and attach a live
    GDELT cross-signal to each. Network-bound; each lookup fails soft independently.

    domestic_terms: [{"term", "attention", "is_new"/..., "recent_count": int}, ...]
    """
    if not isinstance(domestic_terms, list) or len(domestic_terms) > MAX_TERMS_PER_RUN:
        return []
    enriched = []
    for t in domestic_terms:
        if not isinstance(t, dict):
            continue
        term = _bounded_term(t.get("term"))
        if term is None:
            continue
        gv = fetch_global_volume(term, timespan=timespan)
        enriched.append({
            "term": term,
            "domestic_attention": float(t.get("attention", 0.0)),
            "domestic_present": (t.get("recent_count", 1) or 0) > 0,
            "global_volume_intensity": gv,
        })
    return rank_cross_signals(enriched)


if __name__ == "__main__":  # tiny manual smoke test (hits the network)
    import sys
    terms = sys.argv[1:] or ["Tiananmen", "youth unemployment", "white paper protest"]
    rows = enrich_terms([{"term": t, "attention": 1.0, "recent_count": 1} for t in terms])
    for r in rows:
        print(f"{r['term'][:28]:<28} {r['label']:<13} "
              f"cross={r['cross_score']:.3f} global={r.get('global_norm')}")
