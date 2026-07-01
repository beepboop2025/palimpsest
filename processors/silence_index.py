"""Silence Index — the censorship that leaves no deletion to count.

> The cleanest censorship leaves a *hole where coverage should be*, not a scar.

`processors/ddti_index.py` counts what the censor *touches* (deletions). But the
Cyberspace Administration of China's directive apparatus (the CDT "Minitrue" / 真理部
archive) more often tells outlets *not to report* a topic at all — pre-emptive silence —
and editors/users pre-suppress (the chilling effect). The result is a topic that is
globally enormous yet domestically *flat*: Peng Shuai (彭帅 / 张高丽, near-total blackout,
Nov 2021), the Tangshan assault, the Aug-2023 youth-unemployment-statistics suspension.
There is no 404 to count; there is an absence.

This processor operationalises `gdelt_cross_signal.py`'s BLACKOUT / CONTAINMENT reading
into a per-topic Silence Index. It does NOT re-derive GDELT scoring — it *reuses*
`collectors.gdelt_cross_signal` (`normalize_global`, `cross_signal`, `enrich_terms`) as the
single source of truth for the blackout-vs-containment label.

THE HARD PROBLEM (built in, not bolted on): do not false-flag normal local-interest
variation. A US school-board fight or an EPL transfer is loud in GDELT's Anglophone corpus
and legitimately quiet in China — that is *disinterest*, not suppression. Two guard rails,
both mandatory:

  1. China-nexus gate. A topic with no China nexus returns label="out_of_scope", score 0 —
     never silence.
  2. Baseline-aware decoupling (THE false-positive killer). The signal is a *change in the
     coupling ratio*, not a one-shot global≫domestic level. A topic China never covered has
     a low coupling baseline, so a low domestic volume is *expected* and scores ~0; a topic
     China DID cover, now gone, scores high:
        expected_domestic = coupling_baseline * global_norm
        decoupling        = max(0, expected_domestic - domestic_norm)
     NON-BYPASSABLE corroboration requirement: if a deployer wires a domestic proxy but NOT a
     coupling_baseline_fn, the naive `global - domestic` fallback would flag every china-nexus
     topic that is loud abroad and quiet at home — a false blackout on ordinary local
     disinterest. So a reading with NEITHER a coupling baseline NOR a lexicon/gazetteer
     corroboration ABSTAINS instead of scoring. Guard rail #2 cannot be silently switched off.

FAIL LOUD. A number we cannot stand behind is shown *suppressed*, never faked: if the
domestic input is unreachable (None) or the topic is not loud anywhere (global below floor),
the reading ABSTAINS (score None) and is NOT emitted as an observation — it is shown
suppressed, not as a false zero.

THE TWO LINES (held): (1) PUBLIC / PERMITTED READS ONLY — GDELT aggregates already-published
world news; the domestic-volume input is INJECTED from a permitted outside-the-wall public
source (Weibo hot-search absence, Baidu coverage counts, WeiboScope/GreatFire datasets, CDT
presence) and is never a person-dependent in-country read. (2) NO model is the analyst — every
decision here is arithmetic + transparent lexical gazetteer matching, auditable from the text.

Standard-library only in the scoring core; the thin `SilenceIndexProcessor` shell does the I/O.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from core.base_processor import BaseProcessor

# Reuse the GDELT cross-signal math rather than re-deriving it: one source of truth for the
# blackout/containment label and for normalising global volume.
from collectors import gdelt_cross_signal
from collectors.gdelt_cross_signal import cross_signal
# Reuse the censorship gazetteer loader (china-nexus + lexicon corroboration) exactly as the
# rest of the pipeline does.
from processors.ddti_index import load_censorship_terms

logger = logging.getLogger(__name__)

# ── Tunable scoring parameters (documented knobs, mirroring ddti_index) ────────────────
FLOOR = 0.1            # global_norm below this -> not loud anywhere -> abstain (never silence)
LEXICON_WEIGHT = 0.5   # corroboration multiplier; same spirit as ddti_index.NOVELTY_WEIGHT
PRESENCE_EPS = 0.05    # domestic_norm at/below this reads as "absent" -> blackout (not containment)
COUPLED_EPS = 0.05     # silence_score at/below this means domestic tracks global -> "coupled"
HIGH_SCORE = 0.5       # severity boundary for emitted observations
TOP_N = 25
ALERT_SILENCE_THRESHOLD = 0.6  # push blackouts above this to the alert stream

# Labels this module assigns (a topic-level reading is exactly one of these).
OUT_OF_SCOPE = "out_of_scope"  # no china nexus -> not our jurisdiction, score 0
ABSTAIN = "abstain"            # missing/unreachable input -> score None, shown suppressed
BLACKOUT = "blackout"          # loud abroad, domestically absent, china nexus
CONTAINMENT = "containment"    # loud abroad AND domestically present but heavily decoupled
COUPLED = "coupled"            # domestic tracks global -> no silence -> score ~0

# Only these two are real silence signals that flow downstream as observations.
EMIT_LABELS = (BLACKOUT, CONTAINMENT)

# Terms whose domestic disappearance is a WITHHOLDING of *data* (a statistical series stops
# being published), not a deletion of *posts*. Carried as an honest-scope note so an analyst
# never reads a withholding as a normal post deletion. Mirrors config/validation_events.json's
# youth_unemployment_2023 boundary note.
WITHHOLDING_TERMS = {"青年失业率", "youth unemployment", "youth unemployment rate"}

# A small, human-authored English sensitivity lexicon for topics that arrive in English
# headline form (the zh gazetteer covers Chinese). Substring, case-insensitive, auditable.
_EN_LEXICON = (
    "tiananmen", "peng shuai", "white paper", "a4 protest", "blank paper",
    "xinjiang", "uyghur", "tangshan", "li wenliang", "sitong bridge",
    "zhang gaoli", "urumqi fire", "chained woman",
)
# Plain China-nexus markers: enough to bring a topic into scope, but NOT a sensitivity
# corroboration on their own (china_nexus=True, lexicon_hit=False).
_CN_NEXUS_MARKERS = (
    "中国", "china", "chinese", "beijing", "北京", "prc", "ccp", "cpc",
    "shanghai", "上海", "weibo", "微博", "xi jinping",
)


def china_nexus_and_lexicon(topic: str) -> tuple:
    """(china_nexus, lexicon_hit) for a topic, by transparent gazetteer/marker matching.

    Substring (never \\b — CJK-safe). A gazetteer/sensitivity-lexicon hit implies a china
    nexus AND corroboration; a bare nexus marker brings the topic into scope without
    corroboration. No model decides sensitivity — the match ships as evidence.
    """
    if not topic:
        return (False, False)
    low = topic.lower()
    for zh in load_censorship_terms():           # Chinese sensitivity gazetteer
        if zh and zh in topic:
            return (True, True)
    for ent in _EN_LEXICON:                       # English sensitivity lexicon
        if ent in low:
            return (True, True)
    for marker in _CN_NEXUS_MARKERS:              # bare china nexus, no corroboration
        if marker in topic or marker in low:
            return (True, False)
    return (False, False)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


# ── scoring core (pure, offline, testable; NEVER raises) ───────────────────────────────

def silence_score(
    topic: str,
    *,
    global_norm,                  # 0..1 from gdelt_cross_signal.normalize_global(), or None = abstain
    domestic_norm,                # 0..1 normalized domestic volume, or None = abstain
    china_nexus: bool,            # gate: False -> out_of_scope
    coupling_baseline=None,       # this topic's historical domestic/global coupling ratio, or None
    lexicon_hit: bool = False,    # gazetteer/directive-vocab corroboration
    floor: float = FLOOR,         # global_norm below this -> not loud anywhere -> abstain
) -> dict:
    """Return one silence reading. NEVER raises.

    Shape:
      {"topic", "label", "silence_score" (0..1 or None), "global_norm", "domestic_norm",
       "decoupling", "coupling_baseline", "china_nexus", "lexicon_hit", "abstained"}

    Gate order (priority matters):
      out_of_scope  <- china_nexus False (returned, score 0.0, not silence)
      abstain       <- global_norm is None (GDELT unknown) OR domestic_norm is None
                       (domestic unreachable) OR global_norm < floor (not loud anywhere)
                       OR (coupling_baseline is None AND not lexicon_hit) — the non-bypassable
                       corroboration guard: no baseline and no gazetteer hit => never a silence
      then a real reading:
        decoupling = max(0, coupling_baseline*global_norm - domestic_norm)  [baseline-aware]
                     or max(0, global_norm - domestic_norm)                 [no baseline]
        silence_score = clamp01(decoupling * (1 + LEXICON_WEIGHT*lexicon_hit))
        label: cross_signal() decides blackout vs containment (one source of truth);
               a score at/below COUPLED_EPS is relabelled "coupled" (no real silence).
    """
    base = {
        "topic": topic,
        "global_norm": global_norm,
        "domestic_norm": domestic_norm,
        "coupling_baseline": coupling_baseline,
        "china_nexus": bool(china_nexus),
        "lexicon_hit": bool(lexicon_hit),
    }

    # 1) China-nexus gate — a topic with no China nexus is never silence.
    if not china_nexus:
        return {**base, "label": OUT_OF_SCOPE, "silence_score": 0.0,
                "decoupling": 0.0, "abstained": False}

    # 2) Abstain bands — fail loud, never a false zero.
    if global_norm is None:
        return {**base, "label": ABSTAIN, "silence_score": None,
                "decoupling": None, "abstained": True}
    if domestic_norm is None:
        return {**base, "label": ABSTAIN, "silence_score": None,
                "decoupling": None, "abstained": True}
    if global_norm < floor:
        return {**base, "label": ABSTAIN, "silence_score": None,
                "decoupling": None, "abstained": True}

    # 2b) Corroboration guard — the false-positive killer, made NON-BYPASSABLE.
    # Guard rail #2 (baseline-aware decoupling) only kills false blackouts when a coupling
    # baseline exists. If a deployer wires a domestic proxy but forgets coupling_baseline_fn,
    # the naive `global - domestic` branch would flag ANY china-nexus topic that is loud abroad
    # and quiet on the domestic proxy as a blackout — including ordinary local-disinterest
    # topics (a bare "china" marker, no gazetteer hit). So when NEITHER a coupling baseline NOR
    # a lexicon/gazetteer corroboration is present, we ABSTAIN rather than fabricate a silence.
    if coupling_baseline is None and not lexicon_hit:
        return {**base, "label": ABSTAIN, "silence_score": None,
                "decoupling": None, "abstained": True}

    g = _clamp01(float(global_norm))
    d = _clamp01(float(domestic_norm))

    # 3) Decoupling — baseline-aware when a coupling history exists (the false-positive killer).
    # When no baseline exists we are here only because lexicon corroboration is present (guard
    # 2b), so the naive `global - domestic` decoupling is backed by a transparent gazetteer hit.
    if coupling_baseline is not None:
        expected_domestic = _clamp01(float(coupling_baseline)) * g
        decoupling = max(0.0, expected_domestic - d)
    else:
        decoupling = max(0.0, g - d)

    score = _clamp01(decoupling * (1.0 + LEXICON_WEIGHT * (1.0 if lexicon_hit else 0.0)))

    # 4) Label — defer blackout/containment to cross_signal (saturation=1.0 makes our already
    # normalized global pass through unchanged). domestic_present flips the label there.
    domestic_present = d > PRESENCE_EPS
    cs = cross_signal(d, domestic_present, g, saturation=1.0)
    if score <= COUPLED_EPS:
        label = COUPLED                       # domestic tracks global -> no silence
    elif cs["label"] == "blackout":
        label = BLACKOUT
    else:                                     # cross_signal said containment
        label = CONTAINMENT

    return {**base, "label": label,
            "silence_score": round(score, 4),
            "decoupling": round(decoupling, 4),
            "abstained": False}


def rank_silence(readings: list) -> list:
    """Sort readings by silence_score (desc); abstentions sort LAST and stay flagged.

    Abstentions (score None) are never silently ordered as zeros — they go to the bottom
    with abstained=True intact, so an analyst sees them as suppressed, not as quiet topics.
    """
    def key(r):
        ab = bool(r.get("abstained"))
        sc = r.get("silence_score")
        return (1 if ab else 0, -(sc if isinstance(sc, (int, float)) else 0.0))
    return sorted(readings, key=key)


def _silence_severity(label: str, score, lexicon_hit: bool) -> str:
    """Small, defensible severity map. blackout+lexicon_hit -> high; bare decoupling -> medium."""
    if score is None:
        return "low"
    if label == BLACKOUT and lexicon_hit:
        return "high"
    if score >= HIGH_SCORE and lexicon_hit:
        return "high"
    if score > 0:
        return "medium"
    return "low"


def silence_to_observation(reading: dict, now: datetime) -> dict:
    """Map a silence reading onto the DDTI observation schema consumed by
    processors.ddti_index.compute_selectivity_novelty and processors.gazetteer_evolution.

    A pre-emptive silence / blackout on a topic IS a censor-attention event, just on the
    *absence* surface rather than the deletion surface — so it slots straight into the same
    selectivity/novelty index (same logic as undertext/generative_firewall adding a surface).
    Only blackout/containment are emitted; abstain/out_of_scope/coupled are shown suppressed.
    """
    label = reading.get("label")
    topic = reading.get("topic") or ""
    score = reading.get("silence_score")
    deletion_signal = label if label in EMIT_LABELS else "preemptive_silence"
    text = (f"silence reading: global_norm={reading.get('global_norm')} "
            f"domestic_norm={reading.get('domestic_norm')} "
            f"decoupling={reading.get('decoupling')} "
            f"coupling_baseline={reading.get('coupling_baseline')} "
            f"lexicon_hit={reading.get('lexicon_hit')}")
    obs = {
        "terms": [topic] if topic else [],
        "detected_at": now,
        "title": f"[silence:{label}] {topic}",
        "text": text,
        "url": "",
        "source": "silence_index",
        "deletion_signal": deletion_signal,
        "severity": _silence_severity(label, score, bool(reading.get("lexicon_hit"))),
    }
    if topic in WITHHOLDING_TERMS:
        # Honest scope: a data hole, not a post deletion. Carried so it is never read as a
        # normal deletion downstream (mirrors validation_events.json's youth_unemployment note).
        obs["note"] = "withholding: a published data series stopped, not a post deletion"
    return obs


def emit_observations(readings: list, now: datetime) -> list:
    """The subset of readings that are real silence signals, in DDTI schema. Abstentions and
    out-of-scope/coupled readings are intentionally excluded (shown suppressed upstream)."""
    return [silence_to_observation(r, now)
            for r in readings
            if r.get("label") in EMIT_LABELS and not r.get("abstained")]


# ── thin governance-gated shell ───────────────────────────────────────────────────────

class SilenceIndexProcessor(BaseProcessor):
    """Aggregate processor: latest DDTI terms × GDELT × injected domestic volume -> Silence Index.

    Inert by default: with no `domestic_volume_fn` the domestic input is unreachable, so EVERY
    topic abstains (the correct fail-loud default — never a fabricated silence). A deployer wires
    `domestic_volume_fn` to a permitted outside-the-wall public source (Weibo hot-search absence,
    Baidu coverage counts, WeiboScope/GreatFire/CDT presence). `enrich_fn` (GDELT) is injectable
    for offline testing; both outbound paths are governance-gated.
    """

    name = "silence_index"

    def __init__(self, config: dict = None, *, domestic_volume_fn=None, enrich_fn=None,
                 coupling_baseline_fn=None, kill_switch=None, rate_ceiling=None):
        super().__init__(config)
        self._domestic_volume_fn = domestic_volume_fn          # term -> float|None (None => abstain)
        self._enrich_fn = enrich_fn or gdelt_cross_signal.enrich_terms
        self._coupling_baseline_fn = coupling_baseline_fn      # term -> float|None
        self._kill = kill_switch
        self._rate = rate_ceiling
        self.floor = (config or {}).get("floor", FLOOR)
        self.top_n = (config or {}).get("top_n", TOP_N)

    def process_one(self, article: dict) -> dict:
        return {"status": "use_run"}  # aggregate processor — see run()

    # ── pure, offline-testable build step ──────────────────────────────────────────────
    def _domestic_norm(self, term: str):
        """Injected, governance-gated domestic-volume read. None => abstain (fail soft)."""
        if self._domestic_volume_fn is None:
            return None
        if self._kill is not None:
            self._kill.require_live()      # raises if halted — fail safe
        if self._rate is not None:
            self._rate.acquire()           # polite by construction
        try:
            v = self._domestic_volume_fn(term)
        except Exception as e:
            logger.info("[Silence] domestic volume read failed for %r: %s", term, type(e).__name__)
            return None
        if v is None:
            return None
        return _clamp01(float(v))

    def build_readings(self, term_dicts: list, *, floor: float = None) -> list:
        """Take ranked domestic terms ([{"term","attention","recent_count","china_nexus"?,
        "lexicon_hit"?}, ...]), attach a GDELT cross-signal via the injected enrich_fn, attach
        the injected domestic volume, score, and return readings ranked by silence (abstain last).

        Governance-gated before the outbound GDELT batch. Fails soft per term.
        """
        floor = self.floor if floor is None else floor
        if self._kill is not None:
            self._kill.require_live()
        if self._rate is not None:
            self._rate.acquire()
        try:
            cross_rows = self._enrich_fn(term_dicts)
        except Exception as e:
            logger.warning("[Silence] GDELT enrich failed: %s", e)
            cross_rows = [{**t, "global_norm": None, "abstained": True} for t in term_dicts]

        # index the caller's china_nexus/lexicon hints by term (so a CDT/probe-sourced flag wins)
        hints = {t.get("term"): t for t in term_dicts}
        readings = []
        for row in cross_rows:
            term = row.get("term")
            hint = hints.get(term, {})
            cn_flag = hint.get("china_nexus")
            lex_flag = hint.get("lexicon_hit")
            auto_cn, auto_lex = china_nexus_and_lexicon(term)
            china_nexus = auto_cn if cn_flag is None else bool(cn_flag)
            lexicon_hit = auto_lex if lex_flag is None else bool(lex_flag)
            domestic = self._domestic_norm(term)
            baseline = None
            if self._coupling_baseline_fn is not None:
                try:
                    baseline = self._coupling_baseline_fn(term)
                except Exception:
                    baseline = None
            readings.append(silence_score(
                term,
                global_norm=row.get("global_norm"),
                domestic_norm=domestic,
                china_nexus=china_nexus,
                coupling_baseline=baseline,
                lexicon_hit=lexicon_hit,
                floor=floor,
            ))
        return rank_silence(readings)

    def _latest_ddti_terms(self) -> list:
        """Best-effort: ranked domestic terms from the latest DDTI index in Redis. Empty on miss."""
        try:
            import redis
            r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
            raw = r.get("ddti:index:latest")
            r.close()
            if not raw:
                return []
            ranked = json.loads(raw).get("ranked", [])
            return [{"term": t["term"], "attention": t.get("attention", 1.0),
                     "recent_count": t.get("recent_count", 1)} for t in ranked]
        except Exception as e:
            logger.warning("[Silence] could not read ddti:index:latest: %s", e)
            return []

    def run(self) -> dict:
        now = datetime.now(timezone.utc)
        try:
            term_dicts = self._latest_ddti_terms()
            if not term_dicts:
                logger.info("[Silence] no DDTI terms available; nothing to score")
                return {"status": "no_work", "topics": 0}
            readings = self.build_readings(term_dicts)
            observations = emit_observations(readings, now)
            index = {
                "generated_at": now.isoformat(),
                "scope": "preemptive_silence / blackout (absence surface; baseline-aware decoupling)",
                "floor": self.floor,
                "lexicon_weight": LEXICON_WEIGHT,
                "n_topics": len(readings),
                "n_emitted": len(observations),
                "n_abstained": sum(1 for r in readings if r.get("abstained")),
                "readings": readings[:self.top_n],
            }
            self._publish(index, readings)
            self._writeback(observations)
            logger.info("[Silence] %d topics, %d silence signals, %d abstained",
                        index["n_topics"], index["n_emitted"], index["n_abstained"])
            return {"status": "success", "topics": index["n_topics"],
                    "emitted": index["n_emitted"], "abstained": index["n_abstained"]}
        except Exception as e:
            logger.error("[Silence] run failed: %s", e)
            return {"status": "error", "error": str(e)}

    def _publish(self, index: dict, readings: list):
        """Publish silence:index:latest + push high blackouts to alerts:silence. Best-effort."""
        try:
            import redis
            r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
            r.set("silence:index:latest", json.dumps(index, ensure_ascii=False), ex=7200)
            for rd in readings:
                sc = rd.get("silence_score")
                if rd.get("label") == BLACKOUT and isinstance(sc, (int, float)) and sc >= ALERT_SILENCE_THRESHOLD:
                    r.lpush("alerts:silence", json.dumps({
                        "topic": rd["topic"], "label": rd["label"], "silence_score": sc,
                        "lexicon_hit": rd.get("lexicon_hit"), "at": index["generated_at"],
                    }, ensure_ascii=False))
            r.ltrim("alerts:silence", 0, 199)
            r.close()
        except Exception as e:
            logger.warning("[Silence] Redis publish failed: %s", e)

    def _writeback(self, observations: list):
        """Write emitted silence observations back as category='silence_signal' Articles so they
        re-enter the DDTI selectivity/novelty loop. Best-effort; never blocks the index."""
        if not observations:
            return
        try:
            from api.database import SessionLocal
            from storage.models import Article
            db = SessionLocal()
            try:
                for obs in observations:
                    db.merge(Article(
                        source=self.name,
                        source_type="silence",
                        url=obs.get("url", ""),
                        title=obs.get("title", "")[:280],
                        author=obs.get("source", self.name),
                        published_at=obs.get("detected_at"),
                        collected_at=obs.get("detected_at"),
                        full_text=obs.get("text", ""),
                        category="silence_signal",
                    ))
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning("[Silence] Article write-back failed: %s", e)


if __name__ == "__main__":  # offline demo: no network — an injected GDELT + domestic table
    now = datetime.now(timezone.utc)

    # A fake GDELT enrich: returns the cross-signal rows rank_cross_signals would produce.
    def fake_enrich(term_dicts):
        table = {"彭帅": 0.92, "premier league transfer": 0.88, "李文亮": 0.80}
        return [{"term": t["term"], "global_norm": table.get(t["term"]),
                 "abstained": table.get(t["term"]) is None} for t in term_dicts]

    # A fake domestic-volume source (outside-the-wall public proxy). None => abstain.
    domestic = {"彭帅": 0.02, "premier league transfer": 0.05, "李文亮": None}
    baselines = {"彭帅": 0.8, "premier league transfer": 0.9, "李文亮": 0.7}

    proc = SilenceIndexProcessor(
        domestic_volume_fn=lambda t: domestic.get(t),
        enrich_fn=fake_enrich,
        coupling_baseline_fn=lambda t: baselines.get(t),
    )
    terms = [{"term": "彭帅", "attention": 1.0, "recent_count": 1},
             {"term": "premier league transfer", "attention": 1.0, "recent_count": 1},
             {"term": "李文亮", "attention": 1.0, "recent_count": 1}]
    for rd in proc.build_readings(terms):
        print(f"  {rd['topic'][:26]:<26} {rd['label']:<13} "
              f"score={rd['silence_score']} abstained={rd['abstained']}")
    print("emitted observations:")
    for obs in emit_observations(proc.build_readings(terms), now):
        print(f"  {obs['title']}  -> deletion_signal={obs['deletion_signal']} sev={obs['severity']}")
