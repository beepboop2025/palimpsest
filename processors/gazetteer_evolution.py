"""Self-evolving censorship gazetteer — candidate discovery from deletion evidence.

The vocabulary of Chinese censorship is adversarial and fast-moving: as soon as a
term is filtered, netizens coin a homophone, a number, a date-pun, or an image-code
to carry the same meaning (六四 → 8964 → 五月三十五日 → 八平方 → VIIV …). A gazetteer
authored once goes stale in weeks, and the most interesting terms are exactly the ones
nobody has catalogued yet.

This module closes that loop *from observed deletions* rather than by asking a language
model to invent sensitive words. It mines the deletion stream for terms that
  (1) recur across multiple independent deletions, and
  (2) co-occur with already-known censorship vocabulary,
and surfaces them as ranked **candidates for human ratification.**

Why discovery-from-evidence, and why human-in-the-loop:

  * Evidence-based: a candidate is proposed only because real public posts carrying it
    were observed being deleted alongside known-sensitive content. We are reading the
    censor's behavior, not speculating.
  * Human-ratified: the engine NEVER edits `config/zh_censorship_gazetteer.json` itself.
    It writes a proposal ledger; a human authors the final entry. SAFETY.md requires the
    gazetteer be authored directly and never delegated to a Beijing-aligned model — this
    keeps that property intact while still beating manual curation on speed.

The scoring core is pure, deterministic, and standard-library only, so the discovery
logic is fully unit-testable offline. An optional LLM gloss step (to draft an English
analyst note for a candidate) is gated OFF by default and, if ever enabled, must not be
routed to a PRC-aligned model — see `propose_glosses()`.
"""

import json
import logging
import math
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_GAZETTEER_PATH = Path(__file__).resolve().parent.parent / "config" / "zh_censorship_gazetteer.json"

# Promotion thresholds (documented tuning points).
MIN_EVIDENCE = 3          # distinct deletions a candidate must appear in to be eligible
PROMOTE_SCORE = 0.55      # association-weighted score above which we propose ratification
MIN_TERM_LEN = 2          # ignore single characters (too noisy in CJK)
MAX_TERM_LEN = 20         # ignore long spans (likely whole phrases, not a coinage)

# Generic tokens that recur in deletions but carry no censorship signal on their own.
# Kept small and auditable rather than a full stopword model.
_GENERIC = {
    "中国", "政府", "网络", "微博", "微信", "视频", "照片", "今天", "我们", "他们",
    "the", "and", "for", "that", "with", "china", "chinese", "video", "photo", "news",
}

# Candidate surface forms: runs of CJK characters, and alphanumeric tokens (the latter
# catches numeric/date-pun coinages like 8964 and roman-numeral evasions like VIIV).
# Chinese is written without spaces and the stdlib ships no word segmenter, so a maximal
# CJK run is sliced into character n-grams (below) — the standard segmenter-free way to
# surface a short coinage (散步, 润, 白纸) buried inside a longer span (今晚去散步).
_CJK_RUN = re.compile(r"[一-鿿]+")
# Maximal alnum run, anchored on alnum-only lookarounds rather than \b: a Chinese
# character is itself a Unicode word char, so \b never fires at a CJK↔digit seam and
# would miss coinages glued to Chinese (纪念8964 → 8964).
_ALNUM = re.compile(r"(?<![0-9A-Za-z])[0-9A-Za-z]{3,12}(?![0-9A-Za-z])")
_CJK_NGRAM_SIZES = (2, 3)  # most censorship coinages are 2-3 characters


@dataclass
class Candidate:
    """A proposed new gazetteer term, with the evidence that justified proposing it."""
    term: str
    total_support: int = 0          # distinct deletions containing the term
    sens_support: int = 0           # of those, how many also carried a known term
    association: float = 0.0        # sens_support / total_support
    score: float = 0.0              # association-weighted recurrence score
    state: str = "watch"            # "watch" | "propose"
    first_seen: str = ""
    last_seen: str = ""
    evidence: list = field(default_factory=list)  # up to a few sample {title,url}

    def to_dict(self) -> dict:
        return asdict(self)


def load_known_terms() -> set:
    """Flatten the current gazetteer into a set of known zh terms (empty on miss)."""
    try:
        data = json.loads(_GAZETTEER_PATH.read_text(encoding="utf-8"))
        terms = set()
        for cat in data.get("categories", {}).values():
            for e in cat:
                if e.get("zh"):
                    terms.add(e["zh"])
        return terms
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[gazetteer-evolution] could not load known terms: {e}")
        return set()


def candidate_tokens(text: str) -> set:
    """Extract candidate coinages from one post's text: CJK n-grams + alnum tokens.

    Deterministic and CJK-safe (no word-boundary anchoring on Chinese). Each maximal CJK
    run is sliced into 2- and 3-character n-grams so a short euphemism is recovered from
    inside a longer span. Returns a set, so a term counts once per post regardless of how
    often it repeats within that post. The n-gram blow-up is harmless precisely because
    the downstream filter is strict: a token only becomes a candidate if it RECURS across
    independent deletions AND travels with known-sensitive content (see mine_candidates).
    """
    text = text or ""
    toks = set()
    for run in _CJK_RUN.findall(text):
        if len(run) <= max(_CJK_NGRAM_SIZES):
            toks.add(run)
        for n in _CJK_NGRAM_SIZES:
            for i in range(len(run) - n + 1):
                toks.add(run[i:i + n])
    toks |= {m.lower() for m in _ALNUM.findall(text)}
    return {t for t in toks if MIN_TERM_LEN <= len(t) <= MAX_TERM_LEN and t not in _GENERIC}


def mine_candidates(observations: list[dict],
                    known_terms: set,
                    *,
                    min_evidence: int = MIN_EVIDENCE,
                    promote_score: float = PROMOTE_SCORE) -> list[Candidate]:
    """Discover candidate new gazetteer terms from deletion observations.

    observations: [{"text": str, "title": str, "url": str,
                    "detected_at": datetime|None}], each one an observed deletion.

    Method:
      * For each observation, mark whether it carries any KNOWN sensitive term
        (sensitive context) and extract its candidate tokens.
      * For each unknown candidate token, accumulate total support and the subset of
        support that occurred in a sensitive context.
      * association = sens_support / total_support  (how reliably the candidate travels
        with known-sensitive content — the core discriminator).
      * score = association · log1p(sens_support)  (reward strong association AND
        repeated independent evidence; a one-off co-occurrence stays low).
      * A candidate is proposed for ratification ("propose") when it clears both the
        evidence floor and the score threshold; otherwise it stays on the "watch" list.

    Returns candidates sorted by score (descending). Pure / offline / deterministic.
    """
    agg: dict[str, Candidate] = {}

    for obs in observations:
        text = f"{obs.get('title', '')} {obs.get('text', '')}".strip()
        toks = candidate_tokens(text)
        # A token already in the gazetteer is "known", not a candidate.
        unknown = {t for t in toks if t not in known_terms}
        sensitive_context = any(k in text for k in known_terms)
        ts = obs.get("detected_at")
        stamp = ts.isoformat() if isinstance(ts, datetime) else ""

        for t in unknown:
            c = agg.get(t)
            if c is None:
                c = agg[t] = Candidate(term=t, first_seen=stamp or "")
            c.total_support += 1
            if sensitive_context:
                c.sens_support += 1
            if stamp:
                c.last_seen = stamp
            if len(c.evidence) < 3 and obs.get("title"):
                c.evidence.append({"title": obs["title"][:140], "url": obs.get("url", "")})

    candidates = []
    for c in agg.values():
        if c.total_support <= 0:
            continue
        c.association = round(c.sens_support / c.total_support, 4)
        c.score = round(c.association * math.log1p(c.sens_support), 4)
        eligible = c.sens_support >= min_evidence and c.score >= promote_score
        c.state = "propose" if eligible else "watch"
        candidates.append(c)

    candidates.sort(key=lambda x: x.score, reverse=True)
    return candidates


# ── evasion-phenomenon taxonomy (CSM-MTBench, Zhao et al. 2026) ────────────────────────
# The MT benchmark shows these classes fail machine translation differently — which is
# also why this project matches euphemisms in Chinese DIRECTLY (extract_terms substring-
# matches zh) and never translates zh→en first: translation destroys the coinage before
# it can be detected. Tagging a candidate by phenomenon lets a curator triage faster.
_PHENOMENON_BY_CATEGORY = {
    "june4_tiananmen": "numeronym", "leadership_xi": "homophone", "censorship_meta": "homophone",
}


def classify_phenomenon(term: str, category: str = "") -> str:
    """Tag a term numeronym / homophone / affective / lexical. Seed categories carry a
    curated default; evolved terms fall back to a heuristic (digits→numeronym,
    emoji→affective, else lexical)."""
    if any(ch.isdigit() for ch in term):
        return "numeronym"
    if category in _PHENOMENON_BY_CATEGORY:
        return _PHENOMENON_BY_CATEGORY[category]
    if any(ord(ch) > 0x1F000 for ch in term):
        return "affective"
    return "lexical"


def slang_recall(found_terms, truth_terms) -> dict:
    """Validation seam (CSM-MTBench): score discovered/known terms against a labeled slang
    set (e.g. the benchmark's source-side inventory) to tune MIN_EVIDENCE / PROMOTE_SCORE.
    Pure set math — stdlib only."""
    found, truth = set(found_terms), set(truth_terms)
    hit = found & truth
    return {"recall": round(len(hit) / len(truth), 3) if truth else 0.0,
            "hits": sorted(hit), "missed": sorted(truth - found),
            "n_truth": len(truth), "n_found": len(found)}


def build_proposal_ledger(candidates: list[Candidate]) -> dict:
    """Render a human-review ledger: the proposals a curator should ratify, plus the
    watch list, with full provenance. This is the ONLY output that touches the
    gazetteer's lifecycle — and it is advisory. A human authors the actual entry."""
    proposals = [c.to_dict() for c in candidates if c.state == "propose"]
    watch = [c.to_dict() for c in candidates if c.state == "watch"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": "advisory-only; gazetteer entries are authored by a human reviewer, "
                  "never written automatically (see SAFETY.md)",
        "n_proposals": len(proposals),
        "n_watch": len(watch),
        "proposals": proposals,
        "watch": watch[:50],
    }


def propose_glosses(candidates: list[Candidate], llm_fn=None) -> list[dict]:
    """OPTIONAL: draft an English analyst gloss for each proposed candidate.

    Gated OFF by default: with no `llm_fn` supplied this returns the proposals with an
    empty gloss, and the deterministic pipeline above is unaffected. If a curator wires
    in an `llm_fn(prompt) -> str`, it MUST NOT be a PRC-aligned model — such a model
    will quietly refuse or omit the most sensitive coinages, defeating the purpose
    (this asymmetric risk is the reason the gazetteer is human-authored). The LLM only
    ever drafts a *gloss* for a human to confirm; it never decides what is sensitive.
    """
    out = []
    for c in candidates:
        if c.state != "propose":
            continue
        gloss = ""
        if llm_fn is not None:
            try:
                gloss = llm_fn(
                    "You are assisting a human-rights analyst cataloguing Chinese "
                    "internet-censorship euphemisms. In one short English line, gloss "
                    f"the likely meaning of the term {c.term!r}, observed being deleted "
                    "alongside known-sensitive posts. If unsure, say 'uncertain'."
                ).strip()[:200]
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"[gazetteer-evolution] gloss failed for {c.term!r}: {e}")
        out.append({**c.to_dict(), "draft_gloss": gloss, "ratified": False})
    return out


if __name__ == "__main__":  # tiny offline demo
    # Known gazetteer terms; 散步 ("taking a walk" = protest euphemism) is deliberately
    # NOT yet known — the engine should rediscover it from the deletion evidence.
    known = {"白纸", "8964", "躺平"}
    # Mined text is recovered post CONTENT (the detector strips deletion-notice chrome),
    # so there is no boilerplate to confound the signal — only what people actually wrote.
    obs = [
        {"title": "今晚去散步声援白纸的朋友们", "text": "今晚去散步声援白纸的朋友们", "url": "u1"},
        {"title": "明天广场散步现场会有很多人白纸", "text": "明天广场散步现场会有很多人白纸", "url": "u2"},
        {"title": "大家一起散步纪念8964", "text": "大家一起散步纪念8964", "url": "u3"},
        {"title": "周末散步看花拍照", "text": "周末散步看花拍照", "url": "u4"},
        {"title": "躺平内卷大学生找不到工作", "text": "躺平内卷大学生找不到工作", "url": "u5"},
    ]
    ledger = build_proposal_ledger(mine_candidates(obs, known, min_evidence=2, promote_score=0.3))
    print(f"proposals: {ledger['n_proposals']}   watch: {ledger['n_watch']}\n")
    for c in ledger["proposals"]:
        print(f"PROPOSE  {c['term']:<8} score={c['score']:<6} "
              f"assoc={c['association']} evidence={c['sens_support']}/{c['total_support']}")
