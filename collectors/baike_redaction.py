"""BAIKE REDACTION-DIFF — censorship tomography of a state encyclopedia.

> Baidu Baike is a state-moderated encyclopedia that silently rewrites contested
> entries and has deliberately removed the public's ability to view its own edit
> history. The act of redaction is hidden. We reconstruct it from outside the wall:
> content-address the entry over time, diff it against the open record (Chinese
> Wikipedia), and treat the divergence as the payload. The censor erased the
> history; we keep it.

This is UNDERTEXT pointed at an encyclopedia. It changes ONE coordinate of the vantage
tensor — the ``surface`` becomes an encyclopedia — and reuses everything else: the same
``Observation`` schema, the same ``DivergenceDetector`` + ``JsonBaselineStore``, the same
``divergence_to_observation`` adapter into the DDTI selectivity/novelty index. Two things
are measured on this surface:

  (i) **ENCYCLOPEDIA_FORK** — Baike vs Chinese Wikipedia on the *same* contested entity.
      The two prose bodies ALWAYS differ (different register/language), so comparing
      ``content_fp`` would flag every pair — the ``PLATFORM_FORK`` trap. The payload is a
      diff of DERIVED FACETS: terms the open record carries that Baike omits
      (``wiki_only_sensitive``), a reference list collapsed to state media
      (``sourcing_monoculture``), the years of biography the open record has that Baike
      lacks (``bio_gap``), and the hardest case — total absence (``absent_on_baike``).

  (ii) **STATE_REWRITE** — Baike rewriting its OWN history over time. We feed the Baike
      observation to a ``DivergenceDetector`` (``DELETION`` on present→absent, ``MUTATION``
      on a content-fp change) and then run the transparent, lexical ``state_rewrite_signal``
      over the two snapshots: sensitive-term excision, biographical truncation, dated-
      paragraph deletion-with-padding, sourcing collapse, euphemism substitution, role/title
      removal. ≥2 markers relabels the MUTATION as a STATE_REWRITE — the act of rewriting
      history, reconstructed because Baike removed the public diff.

WHY THIS STAYS ON THE TWO LINES (held, like UNDERTEXT and the Generative Firewall):

  * PUBLIC / PERMITTED READS ONLY. Baike and Wikipedia are public encyclopedias; we issue
    plain anonymous GETs from OUTSIDE-the-wall infrastructure (the optional ``PALIMPSEST_PROXY``
    egress seam, since the GFW blocks Wikipedia). We deliberately do NOT authenticate into
    Baike's restricted 历史版本 (revision history) even though a high-level account could —
    that needs credentials, is not a public read, and breaks Line 1. Reconstructing the diff
    from our own snapshots is both the safe and the correct method. No person inside China is
    ever asked to act.
  * NO Beijing-aligned model is ever the analyst. Baike is the SUBJECT under observation.
    Every judgement — what is sensitive, what counts as a state rewrite, fork vs normal edit —
    is made by the transparent lexical/structural rules below, auditable from the text alone
    and shipped as the finding's own evidence (``reasons[]``, the exact ``wiki_only_sensitive``
    delta, the replayable fingerprints). No LLM decides sensitivity here.
  * FAIL LOUD, NOT SILENT. Inert without a backend (never a false zero). A blocked/failed
    fetch is ``present=False`` with a ``fetch_failed`` marker kept DISTINCT from a real
    deletion (a timeout must never masquerade as a scrub). never-created / deleted / locked /
    disambiguation / fetch-failed are five different states. From outside the wall the
    redaction *moment* is unobservable, so velocity is poll-bounded and shown suppressed.

Standard-library only. The two fetchers are INJECTABLE; the defaults are INERT — they
perform NO network I/O unless live mode is explicitly enabled (``PALIMPSEST_LIVE=1`` or a
configured ``PALIMPSEST_PROXY``), so a bare ``BaikeRedactionWatch().observe(entity)``
reaches out to nothing. A disabled/blocked fetch is fail-soft (present=False), never a
false zero.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass

# Reuse UNDERTEXT's tensor + divergence machinery rather than re-deriving it: an encyclopedia
# surface is just another point in observation = f(query × geo × cohort × surface × time).
from collectors.undertext import (  # noqa: E402
    Vantage, Probe, Observation, Divergence, DivergenceDetector, JsonBaselineStore,
    content_key, normalize_body, items_fingerprint_text, extract_items,
    divergence_to_observation, _default_fetch,
    DELETION, MUTATION,
)

logger = logging.getLogger(__name__)

# New divergence kinds this surface adds to the deletion-signal vocabulary. They map onto the
# same DDTI observation schema downstream via divergence_to_observation (kind -> deletion_signal).
ENCYCLOPEDIA_FORK = "encyclopedia_fork"   # Baike hides facts the open record (Wikipedia) carries
STATE_REWRITE = "state_rewrite"           # a Baike self-MUTATION the classifier flags as managed

# state_rewrite_signal labels (the interpretation of a MUTATION; never a substitute for the fp).
NORMAL_EDIT = "NORMAL_EDIT"
STATE_REWRITE_SUSPECTED = "STATE_REWRITE_SUSPECTED"

# Both vantages are GLOBAL + anon: we only ever read the publicly-served surface. The SUBJECT
# is the censored encyclopedia; the CONTROL is the open record read from outside the wall.
BAIKE_VANTAGE = Vantage(geo="GLOBAL", cohort="anon-web", surface="baidu-baike")
WIKI_VANTAGE = Vantage(geo="GLOBAL", cohort="anon-web", surface="zh-wikipedia")


# ── the contested entity (one row of the work list) ────────────────────────────────────────

@dataclass(frozen=True)
class Entity:
    """A contested entity to watch on both encyclopedias.

    ``lemma_id`` PINS the Baike lemma so ``/item/{title}/{lemma_id}`` cannot drift onto a
    多义词 (disambiguation) page or a same-named subject — the #1 Baike pitfall. ``wiki_title``
    defaults to ``zh_title`` (Simplified, to match Baike); ``domain`` is the DDTI domain hint.
    """
    zh_title: str             # Baike + zh-wiki title (Simplified)
    lemma_id: str = ""        # PIN the Baike lemma to avoid disambiguation drift
    wiki_title: str = ""      # default = zh_title
    domain: str = ""          # LEADERSHIP / UNREST / DISASTER / RIGHTS  (DDTI domain hint)
    aliases: tuple = ()       # extra surface forms for matching

    def probe(self) -> Probe:
        return Probe(query=self.zh_title, lang="zh", domain=self.domain)


# ── lexicons & signatures (all lexical, all auditable — they ship as the evidence) ──────────
# Authored directly, never delegated to any model (ETHICS.md / SAFETY.md). Public, widely
# documented censorship vocabulary only — these identify the *vocabulary of censorship*, not
# any person. Substring-matched (CJK-safe; no \b).

SENSITIVE_TERMS = (
    "六四", "天安门事件", "天安门", "八九", "法轮功", "白纸", "A4", "清零", "维权", "软禁",
    "失踪", "双开", "落马", "被查", "接受调查", "开除党籍", "贪腐", "情妇", "婚外", "叛逃",
    "颠覆国家政权", "寻衅滋事", "自焚", "镇压", "屠杀",
)

# Disgrace / purge verbs — the biographical-truncation tell on officials.
DISGRACE_VERBS = (
    "落马", "双开", "调查", "判处", "受贿", "免职", "撤职", "开除党籍", "被查", "接受调查",
)

# Euphemism / harmonization cues (the "scriptio inferior"). A euphemism appearing in Baike
# where the plain fact was, or where the open record names the event, is a substitution signal.
EUPHEMISMS = (
    "河蟹", "和谐", "被旅游", "被自杀", "移居", "赴境外", "身体原因", "蛤蟆", "蛤",
)

# State-media domains. A contested entry whose ENTIRE reference list is state media, when the
# open record cites independent/foreign press, is a managed entry (sourcing monoculture).
STATE_MEDIA = frozenset({
    "xinhuanet.com", "news.cn", "people.com.cn", "cctv.com", "cctv.com.cn", "gov.cn",
    "ccdi.gov.cn", "chinanews.com.cn", "qstheory.cn", "china.com.cn",
})

# Infobox keys / lede title words used by the role/title-removal rule (purged-official pattern).
_ROLE_KEYS = ("现任", "职务", "现任职务", "职位", "任职")
_TITLE_WORDS = ("外交部长", "部长", "书记", "省长", "市长", "主任", "总理", "委员", "常委", "主席")

# Absence / interstitial signatures. Each means a DIFFERENT thing — never collapse them.
_SIG_NOT_CREATED = ("百科尚未收录词条", "词条暂未创建", "该词条尚未创建")
_SIG_DELETED = ("该词条已被删除", "词条已被删除")
_SIG_LOCKED = ("该词条已被锁定",)
_SIG_DISAMBIG = ("polysemant", "lemmaWgt-subLemmaList", "多义词")

# Baike main-content selectors drift across redesigns — extract by these BUT fall back to a
# whole-body normalize_body when they miss; never hinge on one class surviving (pitfall #7).
_SEL_PARA = {"tag": "div", "class": "para"}
_SEL_SUMMARY = {"tag": "div", "class": "lemma-summary"}
_SEL_IB_NAME = {"tag": "dt", "class": "basicInfo-name"}
_SEL_IB_VALUE = {"tag": "dd", "class": "basicInfo-value"}

_HREF = re.compile(r'href=["\']?(https?://[^"\'>\s]+)', re.IGNORECASE)
_YEAR = re.compile(r"(1[89]\d{2}|20\d{2})")
_MIN_PRESENT_LEN = 80  # below this the visible body is empty/blocked → present=False


def _domain(url: str) -> str:
    """Registrable-ish host of an absolute URL, ``www.`` and port stripped. "" if not http(s)."""
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""
    if not netloc:
        return ""
    netloc = netloc.split("@")[-1].split(":")[0]
    return netloc[4:] if netloc.startswith("www.") else netloc


def is_state_media(domain: str) -> bool:
    """True if ``domain`` is (a subdomain of) a state-media outlet. Auditable allowlist."""
    return any(domain == d or domain.endswith("." + d) for d in STATE_MEDIA)


def _ref_domains(html: str) -> list:
    """External citation domains in the page (Baike-internal hosts dropped). The sourcing tell."""
    out = []
    for u in _HREF.findall(html or ""):
        d = _domain(u)
        if d and not d.endswith("baidu.com"):
            out.append(d)
    return sorted(set(out))


def _latest_year(text: str) -> int:
    """Latest plausible 4-digit year (1850–2100) in ``text``; None if none. A biographical-
    recency anchor. The CALLER controls what text is scanned: on the selector path that is
    clean joined visible text (years survive); on the ``normalize_body`` DOM-churn fallback the
    3+-digit runs are already collapsed to ``#``, so this yields None — recency is SUPPRESSED
    rather than read off contaminated chrome, so ``bio_truncation`` / ``bio_gap`` simply do not
    fire on a churned page (fail-soft: never a false positive from a number we cannot stand
    behind)."""
    years = [int(y) for y in _YEAR.findall(text or "") if 1850 <= int(y) <= 2100]
    return max(years) if years else None


# ── extraction (inert, deterministic, fail-soft — never raises) ─────────────────────────────

def extract_baike(html: str) -> dict:
    """Parse a Baike entry page into facets, by visible text with a normalize_body fallback.

    Returns ``{present, interstitial, summary, paragraphs[], infobox{}, ref_domains[],
    latest_year, text, excerpt}``. ``present=False`` carries an ``interstitial`` naming the
    matched signature ("not_created"/"deleted"/"locked"/"disambiguation"), so an entry that
    never existed is distinguished from one that was deleted. Same no-raise contract as
    ``extract_items``: a selector miss degrades to the whole-body path, never a crash.
    """
    out = {"present": False, "interstitial": "", "summary": "", "paragraphs": [],
           "infobox": {}, "ref_domains": [], "latest_year": None, "text": "", "excerpt": ""}
    html = html or ""

    # Absence / interstitial first — each state is different and only present→deleted is a scrub.
    if any(s in html for s in _SIG_NOT_CREATED):
        out["interstitial"] = "not_created"
        return out
    if any(s in html for s in _SIG_DELETED):
        out["interstitial"] = "deleted"
        return out
    if any(s in html for s in _SIG_LOCKED):
        out["interstitial"] = "locked"
        return out
    if any(s in html for s in _SIG_DISAMBIG):
        # A 多义词 landing means we are NOT on the entity (lemma_id was omitted). Flag, never
        # fingerprint it as the subject (pitfall #4).
        out["interstitial"] = "disambiguation"
        return out

    paragraphs = extract_items(html, _SEL_PARA)
    summary_items = extract_items(html, _SEL_SUMMARY)
    summary = " ".join(summary_items).strip()
    names = extract_items(html, _SEL_IB_NAME)
    values = extract_items(html, _SEL_IB_VALUE)
    infobox = {k: v for k, v in zip(names, values) if k}
    refs = _ref_domains(html)

    if paragraphs or summary:
        text = " ".join([summary] + paragraphs + list(infobox.values())).strip()
        present = True
    else:
        # Selectors missed (DOM churn) → whole-body fallback. Chrome is collapsed by
        # normalize_body so a sidebar/ad change cannot fabricate substance. Note: that same
        # collapse erases 4-digit years, so latest_year (below) is None on this path — recency
        # is deliberately suppressed on a churned page rather than guessed off chrome (fail-soft).
        text = normalize_body(html)
        present = len(text) >= _MIN_PRESENT_LEN

    out.update(present=present, summary=summary, paragraphs=paragraphs, infobox=infobox,
               ref_domains=refs, latest_year=_latest_year(text), text=text,
               excerpt=text[:200])
    return out


def extract_wiki(data) -> dict:
    """Parse the Wikipedia extracts (and optional parse/externallinks) JSON into the CONTROL
    facets ``{present, plaintext, latest_year, ref_domains[]}``. Accepts a dict or a JSON str.

    The control is the OPEN RECORD: it carries a complete public revision history and
    independent sourcing, which is the whole point of the contrast. Tolerant of the
    ``missing`` page marker and of either an extracts-API or a parse-API shape. Never raises.
    """
    out = {"present": False, "interstitial": "", "plaintext": "", "latest_year": None,
           "ref_domains": []}
    if isinstance(data, str):
        import json
        try:
            data = json.loads(data)
        except Exception:
            out["interstitial"] = "parse_error"
            return out
    if not isinstance(data, dict):
        out["interstitial"] = "parse_error"
        return out

    pages = ((data.get("query") or {}).get("pages") or {})
    extract = ""
    for page in pages.values():
        if "missing" in page:
            continue
        ex = page.get("extract") or ""
        if ex:
            extract = ex
            break

    refs = []
    for u in ((data.get("parse") or {}).get("externallinks") or []):
        d = _domain(u)
        if d:
            refs.append(d)
    for page in pages.values():
        for el in page.get("extlinks") or []:
            u = el.get("*") if isinstance(el, dict) else el
            d = _domain(u or "")
            if d:
                refs.append(d)

    out.update(present=bool(extract), plaintext=extract, latest_year=_latest_year(extract),
               ref_domains=sorted(set(refs)))
    return out


def fingerprint(extract: dict) -> str:
    """Content address of an entry: prefer the paragraph-SET fingerprint (so a chrome/ad/sidebar
    change cannot flip the fp and fake a MUTATION — pitfall #2/#3); fall back to the whole
    normalized body when there are no paragraphs."""
    paras = extract.get("paragraphs") or []
    if paras:
        return content_key(items_fingerprint_text(paras))
    return content_key(normalize_body(extract.get("text", "")))


# ── detection (i): the FORK — Baike vs the open record (derived-facet delta) ─────────────────

def _fork_facets(baike_ex: dict, wiki_ex: dict) -> dict:
    """The derived redaction delta. NOT content_fp equality (the two ALWAYS differ — the
    PLATFORM_FORK trap); the same derive_features→narrative_divergence pattern."""
    baike_text = baike_ex.get("text", "")
    wiki_text = wiki_ex.get("plaintext", "")
    wiki_only = sorted(t for t in SENSITIVE_TERMS if t in wiki_text and t not in baike_text)
    b_refs = baike_ex.get("ref_domains") or []
    w_refs = wiki_ex.get("ref_domains") or []
    monoculture = (bool(b_refs) and all(is_state_media(d) for d in b_refs)
                   and any(not is_state_media(d) for d in w_refs))
    by, wy = baike_ex.get("latest_year"), wiki_ex.get("latest_year")
    bio_gap = (wy - by) if (by and wy and wy > by) else 0
    absent = (not baike_ex.get("present")) and wiki_ex.get("present")
    return {"wiki_only_sensitive": wiki_only, "sourcing_monoculture": monoculture,
            "bio_gap": bio_gap, "absent_on_baike": bool(absent)}


def encyclopedia_fork(entity: Entity, baike_ex: dict, wiki_ex: dict,
                      baike_obs: Observation, wiki_obs: Observation):
    """Flag a Baike-vs-Wikipedia fork from the derived-facet delta, or None.

    Abstains when the comparison cannot be trusted: a ``fetch_failed`` or ``disambiguation``
    Baike state, or a control (Wikipedia) that is absent/failed — a blocked Wikipedia read is
    ``fetch_failed``, never misread as "Wikipedia also lacks it" (Line: fail loud). Fires when
    the entity is totally absent on Baike, when sourcing has collapsed to a state-media
    monoculture, when the open record carries ≥2 sensitive terms Baike omits, or ≥1 such term
    with a real biographical gap. ``detail`` carries the exact delta so the fork self-evidences.
    """
    if baike_ex.get("interstitial") in ("fetch_failed", "disambiguation"):
        return None
    if not wiki_ex.get("present"):
        return None

    f = _fork_facets(baike_ex, wiki_ex)
    wiki_only = f["wiki_only_sensitive"]
    trigger = (f["absent_on_baike"] or f["sourcing_monoculture"]
               or len(wiki_only) >= 2 or (len(wiki_only) >= 1 and f["bio_gap"] >= 1))
    if not trigger:
        return None

    facets = []
    if f["absent_on_baike"]:
        facets.append(f"absent_on_baike:{baike_ex.get('interstitial') or 'absent'}")
    if wiki_only:
        facets.append("wiki_only_sensitive:" + ",".join(wiki_only))
    if f["sourcing_monoculture"]:
        facets.append("sourcing_monoculture")
    if f["bio_gap"] >= 1:
        facets.append(f"bio_gap:{f['bio_gap']}y")

    # The entity title and any wiki-only sensitive terms become candidate gazetteer terms.
    baike_obs.features["redaction_terms"] = sorted(set([entity.zh_title] + wiki_only))
    baike_obs.features["fork_facets"] = f
    # a = the open record (control), b = the censored subject (so the DDTI source is baidu-baike).
    return Divergence(ENCYCLOPEDIA_FORK, baike_obs.probe, wiki_obs, baike_obs,
                      detail=" ; ".join(facets))


# ── detection (ii): the MUTATION classifier — a state rewrite vs a normal edit ───────────────

def _role_removed(prev: dict, cur: dict) -> bool:
    """True if a current-title/role disappeared: an infobox role key lost its value, or a lede
    title clause vanished (the purged-official pattern)."""
    p_ib, c_ib = prev.get("infobox") or {}, cur.get("infobox") or {}
    for k in _ROLE_KEYS:
        if p_ib.get(k) and not c_ib.get(k):
            return True
    p_sum, c_sum = prev.get("summary", ""), cur.get("summary", "")
    for w in _TITLE_WORDS:
        if w in p_sum and w not in c_sum:
            return True
    return False


def _survives_as_superset(para: str, others) -> bool:
    """True if ``para`` survives almost whole inside some OTHER paragraph — an ADDITIVE
    rewording (the paragraph merely grew), not a deletion.

    The paragraph-SET diff sees a reworded paragraph as removal+addition, which would fake a
    ``dated_paragraph_deletion`` on a benign edit (e.g. the 江泽民/蛤-meme control, where a
    dated line gains a clause). We use difflib character-containment to recognise the survivor:
    an additive edit leaves ≥90% of the old text inside a new one, so it is NOT counted; a
    SANITIZING rewrite (specifics replaced by a euphemism — content is LOST) has low containment
    and is still counted. Stdlib difflib only; auditable from the two texts."""
    if not para:
        return False
    for c in others:
        if c == para:
            continue
        sm = difflib.SequenceMatcher(None, para, c, autojunk=False)
        matched = sum(b.size for b in sm.get_matching_blocks())
        if matched / len(para) >= 0.9:
            return True
    return False


def state_rewrite_signal(prev: dict, cur: dict):
    """Classify a Baike self-MUTATION as ``NORMAL_EDIT`` or ``STATE_REWRITE_SUSPECTED``.

    Pure lexical/structural rules over the two extracted snapshots; every reason is a string an
    auditor can re-derive from the texts (it ships as the finding's evidence). Score by reason
    count; ≥2 ⇒ SUSPECTED. A normal edit grows, advances its latest date, keeps/broadens
    sourcing, and excises no sensitive fact — so it accumulates 0–1 reasons and does not fire.

    Returns ``(label, reasons[])``. The label is the INTERPRETATION of a MUTATION; the
    replayable fingerprint is the fact.
    """
    reasons = []
    prev_text, cur_text = prev.get("text", ""), cur.get("text", "")
    prev_paras = set(prev.get("paragraphs") or [])
    cur_paras = set(cur.get("paragraphs") or [])

    # 1. Sensitive-term excision — a contested fact present in prev is gone from cur.
    excised = [t for t in SENSITIVE_TERMS if t in prev_text and t not in cur_text]
    if excised:
        reasons.append("sensitive_excision:" + ",".join(excised))

    # 2. Biographical truncation — the latest dated year rolled BACK (history shortened at the
    #    end), the opposite of a normal edit that adds recent events.
    py, cy = prev.get("latest_year"), cur.get("latest_year")
    if py and cy and cy < py:
        reasons.append(f"bio_truncation:{py}->{cy}")

    # 3. Net deletion of dated/sensitive paragraphs WITH the page padded back to length — the
    #    "delete the section, pad the page" move that length heuristics miss; the paragraph-SET
    #    diff catches it.
    removed = prev_paras - cur_paras
    removed_dated = [p for p in removed
                     if (_YEAR.search(p) or any(t in p for t in SENSITIVE_TERMS + DISGRACE_VERBS))
                     and not _survives_as_superset(p, cur_paras)]
    padded = len(cur_text) >= 0.9 * len(prev_text)
    if removed_dated and padded:
        reasons.append(f"dated_paragraph_deletion:{len(removed_dated)}")

    # 4. Sourcing collapse — cur references are a strict all-state-media subset where prev had
    #    broader (independent/foreign) sourcing.
    prev_refs = set(prev.get("ref_domains") or [])
    cur_refs = set(cur.get("ref_domains") or [])
    if (cur_refs and all(is_state_media(d) for d in cur_refs)
            and prev_refs and not all(is_state_media(d) for d in prev_refs)):
        reasons.append("sourcing_collapse")

    # 6. Role/title removal (computed before 5 so euphemism substitution can corroborate it).
    role_gone = _role_removed(prev, cur)

    # 5. Euphemism substitution — a euphemism newly introduced in cur, alongside an excision /
    #    role removal / dated-paragraph deletion (i.e. it stands in for something removed). A
    #    euphemism stable across both rounds (e.g. a meme) is NOT a substitution and won't fire.
    new_euph = [e for e in EUPHEMISMS if e in cur_text and e not in prev_text]
    if new_euph and (excised or role_gone or removed_dated):
        reasons.append("euphemism_substitution:" + ",".join(new_euph))

    if role_gone:
        reasons.append("role_removal")

    label = STATE_REWRITE_SUSPECTED if len(reasons) >= 2 else NORMAL_EDIT
    return label, reasons


# ── the collector (mirrors WebVantagePoint / ModelVantagePoint) ─────────────────────────────

def _baike_url(entity: Entity) -> str:
    base = "https://baike.baidu.com/item/" + urllib.parse.quote(entity.zh_title)
    return f"{base}/{entity.lemma_id}" if entity.lemma_id else base


def _wiki_url(entity: Entity) -> str:
    title = entity.wiki_title or entity.zh_title
    return ("https://zh.wikipedia.org/w/api.php?action=query&format=json&prop=extracts"
            "&explaintext=1&redirects=1&variant=zh-hans&titles="
            + urllib.parse.quote(title))


def _fetch_failed(url: str, kind: str) -> dict:
    """A blocked/failed read → present=False with a fetch_failed marker DISTINCT from a real
    deletion. A timeout must never masquerade as a scrub."""
    if kind == "baike":
        return {"present": False, "interstitial": "fetch_failed", "summary": "",
                "paragraphs": [], "infobox": {}, "ref_domains": [], "latest_year": None,
                "text": "", "excerpt": "", "url": url}
    return {"present": False, "interstitial": "fetch_failed", "plaintext": "",
            "latest_year": None, "ref_domains": [], "url": url}


def _live_enabled(proxy) -> bool:
    """Is real outbound network I/O explicitly opted into? True only when a proxy egress is
    configured or ``PALIMPSEST_LIVE`` is truthy. Default (neither) is INERT."""
    if proxy:
        return True
    return os.environ.get("PALIMPSEST_LIVE", "").strip().lower() in ("1", "true", "yes", "on")


def _governed_default_fetch(url: str, proxy: str = None) -> str:
    """The INERT-by-default network seam used when no fetch is injected.

    Unlike a raw ``_default_fetch``, this refuses to touch the network unless live mode is
    EXPLICITLY enabled (``PALIMPSEST_LIVE=1`` or a configured ``PALIMPSEST_PROXY``). When it is
    not, it raises ``URLError`` WITHOUT opening a socket, which the collector catches as
    ``fetch_failed`` (present=False) — so a bare ``BaikeRedactionWatch().observe(entity)``, with
    no injected fetch and no governance, performs no real GET at all (inert, never a false
    zero). Governance gating (kill switch / rate ceiling) still runs first when provided."""
    if not _live_enabled(proxy):
        raise urllib.error.URLError(
            "inert: live network disabled (set PALIMPSEST_LIVE=1 or configure PALIMPSEST_PROXY)")
    return _default_fetch(url, proxy=proxy)


class BaikeRedactionWatch:
    """Watches a contested entity on Baike (subject) and Wikipedia (control), emitting
    ENCYCLOPEDIA_FORK and STATE_REWRITE / MUTATION / DELETION divergences.

    Governance-gated exactly like ``WebVantagePoint``: before every outbound read it consults
    the optional kill switch (``require_live()`` — raises if halted) and rate ceiling
    (``acquire()`` — polite by construction). ``baike_fetch`` / ``wiki_fetch`` are injectable;
    the defaults are INERT and refuse real network I/O unless live mode is EXPLICITLY enabled
    (``PALIMPSEST_LIVE=1`` or a configured ``PALIMPSEST_PROXY`` egress seam — the GFW blocks
    Wikipedia), going through stdlib urllib only then. A bare ``BaikeRedactionWatch().observe(
    entity)`` performs no network I/O and returns no divergences — inert, never a false zero.

    Time-divergence baselines persist via the injected ``store`` (e.g. ``JsonBaselineStore``);
    the full prior EXTRACTION (needed for the lexical rewrite classifier) is cached in memory,
    so across a fresh process a MUTATION still fires from the stored fingerprint even when the
    prior text is unavailable to relabel it STATE_REWRITE.
    """

    def __init__(self, *, baike_fetch=None, wiki_fetch=None, proxy=None, store=None,
                 kill_switch=None, rate_ceiling=None):
        self.proxy = proxy if proxy is not None else os.environ.get("PALIMPSEST_PROXY")
        self._baike_fetch = baike_fetch or (lambda url: _governed_default_fetch(url, proxy=self.proxy))
        self._wiki_fetch = wiki_fetch or (lambda url: _governed_default_fetch(url, proxy=self.proxy))
        self._detector = DivergenceDetector(store=store)
        self._kill = kill_switch
        self._rate = rate_ceiling
        self._last_extract: dict[str, dict] = {}  # baike observation_key -> prior extraction

    def _guard(self) -> None:
        if self._kill is not None:
            self._kill.require_live()   # raises if halted — fail safe
        if self._rate is not None:
            self._rate.acquire()        # polite by construction

    def fetch_baike(self, entity: Entity) -> dict:
        url = _baike_url(entity)
        self._guard()
        try:
            html = self._baike_fetch(url)
        except urllib.error.HTTPError as e:
            if getattr(e, "code", None) == 404:
                ex = extract_baike("")          # nothing to parse
                ex["interstitial"] = "deleted"  # a 404 is a real absence, not a fetch error
                ex["url"] = url
                return ex
            return _fetch_failed(url, "baike")
        except (urllib.error.URLError, OSError):
            return _fetch_failed(url, "baike")
        ex = extract_baike(html)
        ex["url"] = url
        return ex

    def fetch_wiki(self, entity: Entity) -> dict:
        url = _wiki_url(entity)
        self._guard()
        try:
            data = self._wiki_fetch(url)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            return _fetch_failed(url, "wiki")
        ex = extract_wiki(data)
        ex["url"] = url
        return ex

    def observe(self, entity: Entity, *, observed_at: float = None) -> dict:
        """One round on one entity. Returns a result dict with both extractions and any
        divergences. Fetches consult the kill switch + rate ceiling and propagate a halt."""
        ts = time.time() if observed_at is None else observed_at
        probe = entity.probe()

        baike_ex = self.fetch_baike(entity)
        wiki_ex = self.fetch_wiki(entity)

        baike_obs = Observation(
            probe, BAIKE_VANTAGE,
            present=bool(baike_ex.get("present")),
            content_fp=fingerprint(baike_ex) if baike_ex.get("present") else "",
            observed_at=ts, raw_excerpt=baike_ex.get("excerpt", "")[:200],
            features={"interstitial": baike_ex.get("interstitial", "")},
        )
        wiki_obs = Observation(
            probe, WIKI_VANTAGE,
            present=bool(wiki_ex.get("present")),
            content_fp=content_key(normalize_body(wiki_ex.get("plaintext", "")))
            if wiki_ex.get("present") else "",
            observed_at=ts, raw_excerpt=wiki_ex.get("plaintext", "")[:200],
        )

        result = {"entity": entity.zh_title, "observed_at": ts, "status": "ok",
                  "baike": baike_ex, "wiki": wiki_ex,
                  "baike_obs": baike_obs, "wiki_obs": wiki_obs, "divergences": []}

        # Detection (i): the cross-surface fork (single round; never stored).
        fork = encyclopedia_fork(entity, baike_ex, wiki_ex, baike_obs, wiki_obs)
        if fork is not None:
            result["divergences"].append(fork)

        # Distinct non-comparable Baike states: a fetch failure or a disambiguation landing must
        # NOT be fed to the time-detector (a timeout is not a scrub; a 多义词 page is not the
        # entity). Surface the state and leave the stored baseline untouched.
        interstitial = baike_ex.get("interstitial", "")
        if interstitial == "fetch_failed":
            result["status"] = "baike_fetch_failed"
            return result
        if interstitial == "disambiguation":
            result["status"] = "disambiguation_flagged"
            return result

        # Detection (ii): time-divergence on the Baike surface (DELETION / MUTATION).
        key = baike_obs.observation_key()
        prev_ex = self._last_extract.get(key)
        div = self._detector.observe(baike_obs)
        if div is not None:
            baike_obs.features["latency_bounded_by_poll"] = True  # velocity honesty (see below)
            if div.kind == MUTATION and prev_ex is not None and baike_ex.get("present"):
                label, reasons = state_rewrite_signal(prev_ex, baike_ex)
                result["state_rewrite_label"] = label
                result["state_rewrite_reasons"] = reasons
                if label == STATE_REWRITE_SUSPECTED:
                    div.kind = STATE_REWRITE
                    excised = [t for t in SENSITIVE_TERMS
                               if t in prev_ex.get("text", "") and t not in baike_ex.get("text", "")]
                    baike_obs.features["redaction_terms"] = sorted(set([entity.zh_title] + excised))
                    div.detail = "STATE_REWRITE_SUSPECTED: " + "; ".join(reasons)
                else:
                    div.detail = "NORMAL_EDIT: " + ("; ".join(reasons) if reasons
                                                    else "no rewrite markers")
            elif div.kind == DELETION:
                div.detail = f"present->absent ({interstitial or 'absent'})"
            result["divergences"].append(div)

        # Update the in-memory extraction baseline only on a trustworthy read (not on a
        # fetch failure — handled by the early return above — so a transient blip preserves the
        # last-known text for the next real comparison).
        self._last_extract[key] = baike_ex
        return result


# ── DDTI integration ────────────────────────────────────────────────────────────────────────

def redaction_to_ddti(div: Divergence) -> dict:
    """Map a redaction Divergence onto the DDTI observation schema, enriching the base
    ``divergence_to_observation`` output.

    ``divergence_to_observation`` is reused UNCHANGED for the core mapping (kind →
    ``deletion_signal``, ``detected_at``, ``severity``). This wrapper additionally folds the
    ``redaction_terms`` (entity title + any ``wiki_only_sensitive`` / excised terms) into
    ``terms`` so they become candidate gazetteer entries, tags the source as
    ``baike-redaction:<vantage>``, and attaches the velocity-honesty note: from outside the
    wall the redaction moment is unobservable, so latency is poll-bounded and shown suppressed
    ("redaction within [t_prev, t_now]"), never as a precise censor-action latency.
    """
    obs = divergence_to_observation(div)  # reused unchanged
    feats = getattr(div.b, "features", {}) or {}
    extra = feats.get("redaction_terms") or []
    if extra:
        obs["terms"] = sorted(set(obs["terms"]) | set(extra))
    obs["source"] = f"baike-redaction:{div.b.vantage.tag()}"
    if div.detail:
        obs["detail"] = div.detail
    if feats.get("latency_bounded_by_poll"):
        obs["latency_bounded_by_poll"] = True
        obs["velocity_note"] = ("redaction within [t_prev, t_now]; "
                                "latency <= poll interval (suppressed; unobservable from outside)")
    return obs


if __name__ == "__main__":  # offline demo: a fork and a self-rewrite, no network
    def seq(*responses):
        it = iter(responses)
        return lambda url: next(it)

    # (i) ENCYCLOPEDIA_FORK — entity absent on Baike, present on the open record.
    sitong = Entity("四通桥事件", lemma_id="0", domain="UNREST")
    baike_absent = "<html><body>抱歉，百科尚未收录词条「四通桥事件」。</body></html>"
    wiki_present = ('{"query":{"pages":{"1":{"title":"四通桥事件",'
                    '"extract":"2022年10月，北京四通桥出现抗议横幅，彭立发被拘留。"}}}}')
    w = BaikeRedactionWatch(baike_fetch=seq(baike_absent), wiki_fetch=seq(wiki_present))
    for d in w.observe(sitong, observed_at=1000.0)["divergences"]:
        o = redaction_to_ddti(d)
        print(f"  {d.kind:17} {d.detail}")
        print(f"    -> DDTI: terms={o['terms']} signal={o['deletion_signal']} src={o['source']}")

    # (ii) STATE_REWRITE — an official's bio truncated, title removed, sourcing collapsed.
    qin = Entity("秦刚", lemma_id="123", domain="LEADERSHIP")
    r1 = ('<html><body><div class="lemma-summary">秦刚，中华人民共和国外交部部长。</div>'
          '<dt class="basicInfo-name">职务</dt><dd class="basicInfo-value">外交部长</dd>'
          '<div class="para">2022年12月，秦刚出任外交部长。</div>'
          '<div class="para">2023年6月，秦刚会见外国政要。</div>'
          '<a href="https://www.reuters.com/x">ref</a>'
          '<a href="https://www.people.com.cn/y">ref</a></body></html>')
    r2 = ('<html><body><div class="lemma-summary">秦刚，中国政治人物。</div>'
          '<div class="para">2022年12月，秦刚曾任职务。</div>'
          '<div class="para">秦刚长期从事外交工作，为国家作出贡献。</div>'
          '<a href="https://www.xinhuanet.com/x">ref</a>'
          '<a href="https://www.people.com.cn/y">ref</a></body></html>')
    wiki_qin = ('{"query":{"pages":{"2":{"title":"秦刚",'
                '"extract":"2023年秦刚被免职，外界关注其失踪。"}}}}')
    w2 = BaikeRedactionWatch(baike_fetch=seq(r1, r2), wiki_fetch=seq(wiki_qin, wiki_qin))
    w2.observe(qin, observed_at=1000.0)                       # round 1: baseline
    res = w2.observe(qin, observed_at=1000.0 + 86400)         # round 2: the rewrite
    print("  state_rewrite reasons:", res.get("state_rewrite_reasons"))
    for d in res["divergences"]:
        o = redaction_to_ddti(d)
        print(f"  {d.kind:17} {d.detail}")
        print(f"    -> DDTI: terms={o['terms']} signal={o['deletion_signal']} "
              f"velocity={o.get('velocity_note', 'n/a')}")
