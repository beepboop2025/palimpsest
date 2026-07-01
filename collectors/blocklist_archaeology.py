"""BLOCKLIST ARCHAEOLOGY — reading the keyword lists the censor ships inside its own clients.

> A term that is *newly present* in version N+1 of a client blocklist is a dated censorship
> directive — the cleanest NOVELTY signal in the whole system, because the censor has
> labelled the term *for us*. No inference: presence == "this platform was ordered (or chose)
> to filter this." The only judgement we add is lexical normalization and diffing.

This is the **inverse** of the rest of Palimpsest. UNDERTEXT and the passive legs (CDT,
FreeWeibo) *infer* sensitivity from deletions. A client-embedded blocklist is the censor's
**ground-truth trigger list**, pre-labelled by the platform. We consume **already-published**
client-extracted artifacts (Citizen Lab's open `chat-censorship` corpus and friends), parse
three real on-disk formats, diff across versions, and emit each newly-added term as a NOVELTY
observation into the *same* DDTI / gazetteer-evolution pipeline the deletion legs feed.

A blocklist is simply a new SURFACE in the tensor — observation = f(query × geo × cohort ×
surface × time). So rather than re-derive content addressing or the DDTI schema we reuse
them: the emitted dict mirrors `undertext.divergence_to_observation` field-for-field (so
`gazetteer_evolution.mine_candidates` and `ddti_index.compute_selectivity_novelty` consume it
unchanged), and `undertext.content_key` gives each parsed list a replayable audit fingerprint.

THE TWO LINES (held; see SAFETY.md, docs/ETHICS.md):

  Line 1 — PUBLIC / PERMITTED READS ONLY; watch the censor, never the censored. The module
    NEVER acquires a client binary or a live keyword-list URL. Acquisition is out-of-tree and
    INJECTED (`load_fn`, default a local-fixtures reader — there is no network default). Real
    data is Citizen Lab's already-published plaintext lists. No DRM cracking: LINE's encrypted
    `cbw.dat` (Base64 → AES-CBC, static key in the binary; Citizen Lab, *Asia Chats: LINE*,
    2014) is handled ONLY via an injectable `decryptor` seam (default None, inert) — ciphertext
    with no decryptor raises `BlocklistEncryptedError` (loud), never an in-module crack attempt.

  Line 2 — no Beijing-aligned model is the analyst. Severity / category / phenomenon are
    lexical and rule-based, computed against the human-authored gazetteer and the published
    `categories_keyword_censorship.csv` code book — auditable from the text alone. A newly-added
    term is proposed into the gazetteer only through the existing human-ratification ledger
    (`gazetteer_evolution.build_proposal_ledger`); this module never writes the gazetteer.

  Fail loud, not silent. Decode fallbacks (legacy GBK/GB2312, Citizen Lab's oldest leaked 2004
    Tencent QQ blacklist), encrypted-blob skips, and sampled-list low confidence are all
    surfaced explicitly — warnings, caveat flags, recorded skips — never papered over.

Standard-library only. The parse + version-diff core is pure and fully unit-testable offline.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Reuse, do not duplicate. The blocklist is a new SURFACE in the same observation tensor, so we
# borrow content addressing (replayable audit fingerprints) and mirror the DDTI adapter schema
# instead of re-deriving either. The emitted dict matches divergence_to_observation field-for-
# field; the downstream consumers below ingest it unchanged.
from collectors.undertext import content_key  # noqa: E402
from processors.gazetteer_evolution import (  # noqa: E402
    build_proposal_ledger,
    classify_phenomenon,
    load_known_terms,
    mine_candidates,
)
from processors.ddti_index import compute_selectivity_novelty  # noqa: E402

logger = logging.getLogger(__name__)

# ── completeness (the metadata that gates novelty confidence) ──────────────────────────────
# EXHAUSTIVE = reverse-engineered client-side list (YY, LINE, TOM-Skype, Sina UC, Sina Show,
# 9158, GuaGua) — the full trigger set for a period, safe to diff for "newly added".
# SAMPLED = server-side / probe-derived (WeChat, QQMail, Apple, Bing) — non-exhaustive, so a
# term "appearing" between two snapshots may be sampling coverage, not a real addition.
# (Citizen Lab, *One App, Two Systems*, 2016.)
EXHAUSTIVE = "exhaustive"
SAMPLED = "sampled"

# Deletion-signal vocabulary this surface adds (mapped onto the DDTI schema downstream).
BLOCKLIST_ADD = "blocklist_add"        # a NEW directive — the highest-confidence novelty input
BLOCKLIST_REMOVE = "blocklist_remove"  # a relaxation/fold — NEVER counted as novelty

SURFACE = "blocklist"

# WeChat censors keyword COMBINATIONS (logical AND): a message is blocked only if ALL components
# are present. Represented as multiple components joined on one of these delimiters. Splitting a
# combo into independent OR-terms massively over-claims what is censored (Citizen Lab WeChat FAQ
# / *Censored Contagion*, 2020), so combination semantics are preserved end to end.
COMBO_DELIMS = (";", "；", "\t", "+")
_COMBO_SPLIT = re.compile("[" + re.escape("".join(COMBO_DELIMS)) + "]")
_COMBO_JOIN = " + "  # canonical readable AND-join for the combo's surface form

_WS = re.compile(r"\s+")
_ASCII_DIGITS = set("0123456789")
# A well-formed roman numeral (canonical additive/subtractive form), lowercased. Used to catch
# roman-numeral date evasions (iv→4, ix→9, mcmlxxxix→1989) WITHOUT firing on benign latin words
# that merely reuse the letters i/v/x/l/c/d/m (mild, civil, dim, lid, mimic, civic, vivid) — none
# of which are valid roman numerals. Purely lexical / auditable; no model (Line 2).
_ROMAN_NUMERAL_RE = re.compile(r"^m{0,4}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")


def _is_roman_numeral(s: str) -> bool:
    """True only for a non-empty, well-formed canonical roman numeral (lowercased input)."""
    return bool(s) and _ROMAN_NUMERAL_RE.fullmatch(s) is not None

# Tiny, auditable script tables. Distinguishing Simplified from Traditional needs a character
# table, not a Unicode block (both live in CJK Unified Ideographs); we keep only the handful of
# distinctive characters the published corpora actually exercise. We tag `script` but NEVER fold
# one into the other — which script the censor listed (武汉 vs 武漢) is itself intelligence.
_TRAD_DISTINCT = set("發漢國華灣亂蘭愛廣東衛壓鎮變態勢黨爆運動員")
_SIMP_DISTINCT = set("发汉国华湾乱兰爱广东卫压镇变态势党运动员")

# High-salience content categories (lexical match against the published code book / gazetteer
# category strings). Auditable, no model.
_HIGH_CATS = (
    "june", "tiananmen", "leadership", "leader", "unrest", "protest", "dissident",
    "dissent", "political", "falun", "independence", "massacre", "rights", "xinjiang",
)
# Pure-latin tokens that carry no censorship signal on their own.
_LATIN_STOPWORDS = {"the", "and", "for", "www", "http", "https", "com", "null", "test"}


class BlocklistEncryptedError(Exception):
    """Raised when a parse is asked for an encrypted artifact with no injected decryptor.

    We FAIL LOUD rather than attempt to decrypt: cracking a client's DRM (e.g. pulling LINE's
    static AES key out of the binary) is out of scope and over the line. The caller owns the
    key and its legality; the parser is inert by default."""


# ── data model ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Keyword:
    """One normalized trigger term (or AND-combination) extracted from a blocklist artifact."""
    term: str                       # normalized surface form (see normalize_term); the diff key
    components: tuple = ()          # non-empty for AND-combination rules (WeChat); else ()
    script: str = ""               # "simplified" | "traditional" | "latin" | "numeric" | "mixed"
    category: str = ""             # from categories_keyword_censorship.csv, if present
    raw: str = ""                  # original line, for audit
    is_combination: bool = False
    date_added: str = ""           # per-keyword Date Added (Harmonized-Histories CSV); else ""


@dataclass
class BlocklistArtifact:
    """A published, client-extracted blocklist snapshot. ACQUISITION is the caller's job and is
    out of tree — this object is *handed* the bytes; it never fetches a binary or a live URL."""
    app: str                        # "yy" | "line" | "tom-skype" | "sina-uc" | "wechat" | ...
    version: str                    # client version OR list version OR observation-date string
    observed_at: datetime           # AWARE; the artifact's stated observe/add date (see §velocity)
    source_ref: str                 # published artifact path/URL (provenance, NOT a CN endpoint)
    completeness: str = EXHAUSTIVE  # EXHAUSTIVE | SAMPLED — gates novelty confidence
    encoding: str = ""             # filled by decode_bytes; "" until decoded


@dataclass
class ParsedBlocklist:
    artifact: BlocklistArtifact
    keywords: dict                  # normalized term -> Keyword (set semantics, order-free)
    decode_warning: str = ""       # non-empty whenever a non-UTF-8 fallback codec was used

    def fingerprint(self) -> str:
        """Replayable content address of the term SET (order/dedup/encoding invariant). A diff
        you cannot replay is not a finding; two encodings of the same list share this fp."""
        return content_key(*sorted(self.keywords))


@dataclass
class BlocklistDiff:
    old: ParsedBlocklist
    new: ParsedBlocklist
    added: list = field(default_factory=list)    # list[Keyword] present in new, absent in old
    removed: list = field(default_factory=list)  # list[Keyword] present in old, absent in new


# ── decoding (fail loud on any non-UTF-8 fallback) ──────────────────────────────────────────

def decode_bytes(raw, hint: str = None) -> tuple:
    """Decode artifact bytes to text. Returns (text, encoding_used, warning).

    Order: optional `hint` → UTF-8 (BOM stripped) → GB18030 → latin-1. A NON-EMPTY warning is
    set whenever the final codec is anything other than clean UTF-8 — the oldest Chinese
    artifacts (the leaked 2004 Tencent QQ blacklist, old Sina lists) are GBK/GB2312, and a
    silent mis-decode yields mojibake "terms" that would poison the gazetteer. latin-1 always
    succeeds, so it is the guaranteed loud last resort, never a crash."""
    if isinstance(raw, str):
        return raw, "unicode", ""
    if not raw:
        return "", "utf-8", ""
    body = raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw  # strip UTF-8 BOM
    order = []
    if hint:
        order.append(hint)
    for enc in ("utf-8", "gb18030", "latin-1"):
        if enc not in order:
            order.append(enc)
    for enc in order:
        try:
            text = body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        norm = "utf-8" if enc.lower().replace("_", "-") in ("utf-8", "u8") else enc.lower()
        if norm == "utf-8":
            return text, "utf-8", ""
        if norm == "latin-1":
            return text, "latin-1", ("FELL BACK to latin-1 after utf-8/gb18030 failed — output "
                                     "is probably mojibake; manual verification required")
        return text, norm, (f"decoded with '{norm}' (utf-8 was not used) — legacy/lossy codec, "
                            "verify the terms before trusting them")
    # latin-1 cannot raise, so this is unreachable in practice; keep a loud safety net.
    return body.decode("latin-1", "replace"), "latin-1", "latin-1 replacement decode (lossy)"


# ── format detection ────────────────────────────────────────────────────────────────────────

_CSV_COLS = {"keyword", "term", "date_added", "date_removed", "language", "category", "word"}
_B64_RE = re.compile(rb"^[A-Za-z0-9+/=\s]+$")


def _looks_encrypted(raw: bytes) -> bool:
    """Heuristic for LINE's `cbw.dat`-style blob: a Base64 body whose decoded bytes are NOT
    decodable text (i.e. ciphertext, not a list). We never decrypt — we only recognise it so we
    can fail loud rather than emit a garbage 'list'."""
    if not isinstance(raw, bytes) or len(raw) < 32:
        return False
    stripped = raw.strip()
    if not _B64_RE.match(stripped) or b" " in stripped:
        return False
    import base64
    try:
        decoded = base64.b64decode(stripped, validate=True)
    except Exception:
        return False
    if len(decoded) < 16:
        return False
    for enc in ("utf-8", "gb18030"):
        try:
            decoded.decode(enc)
            return False  # decodes to text -> it is a base64-wrapped LIST, not ciphertext
        except (UnicodeDecodeError, LookupError):
            continue
    # Entropy guard against a false positive: a single-line, all-latin token can itself be valid
    # base64 whose decode is not utf-8/gb18030 yet is still mostly printable. Real cbw.dat-style
    # ciphertext is ~uniform random, so its printable fraction hovers near 37% (95/256); anything
    # this readable is not a ciphertext blob, so we refuse to mislabel it 'encrypted'.
    printable = sum(1 for b in decoded if 0x20 <= b <= 0x7E or b in (0x09, 0x0A, 0x0D))
    if printable / len(decoded) >= 0.85:
        return False
    return True


def detect_format(raw) -> str:
    """Sniff the on-disk format: "plaintext" | "csv" | "combination" | "encrypted".

    "combination" is informational — the line parser handles per-line AND-combos in either the
    plaintext or combination path, so a combo line in a "plaintext" file is still parsed as a
    combo (never split into OR-terms)."""
    if isinstance(raw, bytes) and _looks_encrypted(raw):
        return "encrypted"
    text, _, _ = decode_bytes(raw) if isinstance(raw, (bytes, bytearray)) else (raw, "", "")
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "plaintext"
    header = lines[0].lstrip("﻿").strip()
    if "," in header:
        cols = {c.strip().strip('"').lower() for c in header.split(",")}
        if cols & _CSV_COLS:
            return "csv"
    if any(len(_COMBO_SPLIT.split(ln)) > 1 for ln in lines):
        return "combination"
    return "plaintext"


# ── normalization & classification (deterministic, lexical, auditable) ──────────────────────

def normalize_term(s: str) -> str:
    """NFKC, strip, collapse internal whitespace, lowercase the ASCII run ONLY (preserve CJK).
    Comment (`#…`) and blank lines normalize to "". Deterministic. Does NOT fold Simplified ↔
    Traditional (that fold destroys signal — the censor lists 武汉 and 武漢 separately)."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.strip()
    if not s or s.startswith("#"):
        return ""
    s = _WS.sub(" ", s)
    return "".join(ch.lower() if "A" <= ch <= "Z" else ch for ch in s)


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return 0x3400 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x2FA1F


def classify_script(term: str) -> str:
    """simplified | traditional | latin | numeric | mixed — heuristic, auditable. A token that
    mixes scripts/digits (天安门1989, 爆發sars疫情, p4病毒实验室) is "mixed" and is ONE keyword
    (never split on the digit boundary)."""
    cjk = latin = digit = trad = simp = False
    for ch in term:
        if ch in _ASCII_DIGITS:
            digit = True
        elif ch.isascii() and ch.isalpha():
            latin = True
        elif _is_cjk(ch):
            cjk = True
            if ch in _TRAD_DISTINCT:
                trad = True
            if ch in _SIMP_DISTINCT:
                simp = True
    if cjk and (latin or digit):
        return "mixed"
    if latin and digit:
        return "mixed"
    if cjk:
        return "traditional" if (trad and not simp) else "simplified"
    if latin:
        return "latin"
    if digit:
        return "numeric"
    return "mixed"


def parse_combination(line: str, delims=COMBO_DELIMS) -> tuple:
    """Split an AND-rule line into its normalized component tuple. A line with <2 components is
    a single keyword, not a combination."""
    split_re = _COMBO_SPLIT if tuple(delims) == COMBO_DELIMS else \
        re.compile("[" + re.escape("".join(delims)) + "]")
    comps = tuple(normalize_term(p) for p in split_re.split(line))
    return tuple(c for c in comps if c)


def _is_only_wildcard(term: str) -> bool:
    """A token that is only wildcard/pattern punctuation (e.g. a lone `*`) — not a literal CJK
    keyword. Recorded as a pattern, not surfaced as a term (pitfall: `*` as fake keyword)."""
    return bool(term) and set(term) <= {"*", "＊", "?", "."}


def _make_keyword(term: str, raw: str, category_map: dict, date_added: str = "") -> Keyword:
    return Keyword(term=term, script=classify_script(term),
                   category=(category_map or {}).get(term, ""), raw=raw,
                   is_combination=False, date_added=date_added)


def _make_combination(comps: tuple, raw: str, category_map: dict, date_added: str = "") -> Keyword:
    joined = _COMBO_JOIN.join(comps)
    return Keyword(term=joined, components=comps, script=classify_script("".join(comps)),
                   category=(category_map or {}).get(joined, ""), raw=raw,
                   is_combination=True, date_added=date_added)


# ── parsing ─────────────────────────────────────────────────────────────────────────────────

def _parse_lines(text: str, category_map: dict) -> dict:
    """Newline-delimited plaintext / combination list -> {normterm: Keyword}. Per-line combo
    detection means a combo line is preserved as a combination even in a 'plaintext' file."""
    kws: dict = {}
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        comps = parse_combination(raw_line)
        if len(comps) >= 2:
            kw = _make_combination(comps, raw_line.rstrip("\r\n"), category_map)
        else:
            term = normalize_term(raw_line)
            if not term or _is_only_wildcard(term):
                continue
            kw = _make_keyword(term, raw_line.rstrip("\r\n"), category_map)
        kws[kw.term] = kw  # set semantics: dedup by normalized key (last wins)
    return kws


def _parse_csv(text: str, category_map: dict) -> dict:
    """CSV-with-metadata list (Harmonized-Histories style: keyword, date_added, date_removed,
    language, category) -> {normterm: Keyword}. Per-keyword `date_added` is carried so a dated
    list flows the directive date through to each observation's detected_at."""
    kws: dict = {}
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return kws
    header = [c.strip().lstrip("﻿").lower() for c in rows[0]]
    has_header = bool({"keyword", "term", "word"} & set(header))
    if has_header:
        def col(*names):
            for n in names:
                if n in header:
                    return header.index(n)
            return -1
        i_kw = col("keyword", "term", "word")
        i_date = col("date_added")
        i_cat = col("category")
        data = rows[1:]
    else:
        i_kw, i_date, i_cat, data = 0, -1, -1, rows

    for row in data:
        if not row or i_kw >= len(row):
            continue
        raw_cell = row[i_kw]
        if not raw_cell.strip() or raw_cell.strip().startswith("#"):
            continue
        date_added = row[i_date].strip() if 0 <= i_date < len(row) else ""
        cat_cell = row[i_cat].strip() if 0 <= i_cat < len(row) else ""
        comps = parse_combination(raw_cell)
        if len(comps) >= 2:
            kw = _make_combination(comps, raw_cell, category_map, date_added)
        else:
            term = normalize_term(raw_cell)
            if not term or _is_only_wildcard(term):
                continue
            kw = _make_keyword(term, raw_cell, category_map, date_added)
        if cat_cell:  # explicit CSV category beats the code-book lookup
            kw = Keyword(term=kw.term, components=kw.components, script=kw.script,
                         category=cat_cell, raw=kw.raw, is_combination=kw.is_combination,
                         date_added=kw.date_added)
        kws[kw.term] = kw
    return kws


def parse_blocklist(raw, artifact: BlocklistArtifact, *, fmt: str = None,
                    decryptor=None, category_map: dict = None) -> ParsedBlocklist:
    """Decode → detect format → build the Keyword set. Pure given its inputs.

    If `fmt == "encrypted"` and `decryptor is None`: raise `BlocklistEncryptedError` (we do not
    crack DRM). If a `decryptor` is supplied it is called on the blob first (caller owns the key
    and its legality) and the decrypted bytes are parsed as an ordinary list."""
    if fmt is None:
        fmt = detect_format(raw)

    if fmt == "encrypted":
        if decryptor is None:
            raise BlocklistEncryptedError(
                f"{artifact.app} {artifact.version}: encrypted blob and no decryptor injected — "
                "refusing to crack client DRM (Line 1). Supply already-decrypted published bytes "
                "or an explicit `decryptor`.")
        raw = decryptor(raw)
        fmt = detect_format(raw)  # parse whatever the decryptor handed back as a normal list

    text, encoding, warning = decode_bytes(raw, hint=(artifact.encoding or None))
    artifact.encoding = encoding

    if fmt == "csv":
        keywords = _parse_csv(text, category_map or {})
    else:  # plaintext / combination share the per-line parser
        keywords = _parse_lines(text, category_map or {})

    return ParsedBlocklist(artifact=artifact, keywords=keywords, decode_warning=warning)


# ── diffing (set difference on normalized term keys) ───────────────────────────────────────

def diff_versions(old: ParsedBlocklist, new: ParsedBlocklist) -> BlocklistDiff:
    """Newly-added / removed terms as a SET DIFFERENCE on normalized term keys. Order-, dedup-,
    and encoding-invariant: re-ordering or re-encoding a list must produce an EMPTY diff, never
    hundreds of phantom adds (pitfall: diffing raw bytes or line order). Only ever diff the same
    app/channel, sorted by observed_at — a region fork is not a time-diff."""
    old_keys, new_keys = set(old.keywords), set(new.keywords)
    added = sorted((new.keywords[k] for k in new_keys - old_keys), key=lambda kw: kw.term)
    removed = sorted((old.keywords[k] for k in old_keys - new_keys), key=lambda kw: kw.term)
    return BlocklistDiff(old=old, new=new, added=added, removed=removed)


# ── severity (lexical only — Line 2; no model ever judges sensitivity) ──────────────────────

def severity_of(kw: Keyword, known_terms=frozenset(), category_map: dict = None) -> str:
    """Lexical, auditable severity. AND-combinations and numeronym/date-pun/roman-numeral
    coinages are the censor's high-salience evasions; a known high-salience category or a term
    that carries a known-sensitive substring is high; a pure-latin stopword is low; else medium.
    Reuses gazetteer_evolution.classify_phenomenon for the phenomenon tag (no model)."""
    phenom = classify_phenomenon(kw.term, kw.category)
    if kw.is_combination:
        return "high"
    if phenom == "numeronym":  # contains a digit -> date-pun / numeronym coinage
        return "high"
    ascii_run = "".join(ch for ch in kw.term.lower() if ch.isascii() and ch.isalpha())
    # A well-formed roman numeral is a date/number evasion (iv→4, ix→9, mcmlxxxix→1989); a latin
    # word that merely uses those letters (mild, civil, mimic) is NOT valid roman and stays out of
    # 'high'. Note: concatenated pseudo-numerals like "VIIV" (6-4) reach 'high' via the numeronym /
    # known-term paths in their real june-4 context, not through this strict-form check.
    if len(ascii_run) >= 2 and _is_roman_numeral(ascii_run):
        return "high"
    cat = (kw.category or (category_map or {}).get(kw.term, "")).lower()
    if cat and any(h in cat for h in _HIGH_CATS):
        return "high"
    if any(k and k in kw.term and k != kw.term for k in known_terms):  # carries a known coinage
        return "high"
    if kw.script == "latin" and kw.term in _LATIN_STOPWORDS:
        return "low"
    return "medium"


# ── emission into the DDTI / gazetteer-evolution pipeline ───────────────────────────────────

def _to_aware(value) -> datetime:
    """Coerce a datetime or a 'YYYY-MM-DD'(/'YYYY-MM-DDTHH:MM:SS') string to an aware UTC dt."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        s = value.strip().replace("/", "-")
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return datetime.now(timezone.utc)


def term_to_observation(kw: Keyword, artifact: BlocklistArtifact, *, signal: str = BLOCKLIST_ADD,
                        known_terms=frozenset(), category_map: dict = None) -> dict:
    """Map one keyword onto the DDTI observation schema — mirrors undertext.divergence_to_
    observation field-for-field, so mine_candidates / compute_selectivity_novelty consume it
    unchanged. For a combination: `terms = [*components, joined_form]` and `text = joined_form`
    (the combo is one finding; components are surfaced so the gazetteer can mine each).

    `detected_at` is the per-keyword Date Added when the artifact carried one (dated CSV), else
    the artifact's observe date. Note (velocity caveat): an out-of-China Date Added is Citizen
    Lab's OBSERVATION date — an upper bound on the true directive timestamp, never the directive
    itself. We surface it as `detected_at`; we do NOT fabricate a latency from it."""
    date_part = artifact.observed_at.date().isoformat()
    detected_at = _to_aware(kw.date_added) if kw.date_added else _to_aware(artifact.observed_at)
    short = "add" if signal == BLOCKLIST_ADD else "remove"
    if kw.is_combination:
        terms = [*kw.components, kw.term]
        text = kw.term
    else:
        terms = [kw.term]
        text = kw.term
    return {
        "terms": terms,
        "detected_at": detected_at,
        "title": f"[blocklist:{short}] {kw.term} ({artifact.app} {artifact.version})",
        "text": text,
        "url": artifact.source_ref,
        "source": f"{SURFACE}:{artifact.app}@{date_part}",
        "deletion_signal": signal,
        "severity": severity_of(kw, known_terms, category_map),
    }


def novelty_inputs(observations) -> list:
    """Filter an observation stream down to what may safely enter the novelty/attention index
    (`ddti_index.compute_selectivity_novelty`) and the gazetteer candidate miner
    (`gazetteer_evolution.mine_candidates`): ADDITIONS only.

    THIS is the boundary that makes the `blocklist_remove` stamp load-bearing. Neither consumer
    inspects `deletion_signal`, so a removal that reached them would be scored as a brand-new,
    maximum-novelty censor-attention event (hist_count==0 → novelty=1.0, full attention) — the
    exact INVERSE of a relaxation, and a number the project cannot stand behind. Dropping
    `blocklist_remove` observations here keeps that fake signal out of the index. Additions and
    any non-blocklist observations (no `deletion_signal`, or a deletion signal from another
    surface) pass through unchanged. The collector's `run()` routes every novelty consumer
    through this filter; any direct caller of the two consumers MUST do the same."""
    return [o for o in observations if o.get("deletion_signal") != BLOCKLIST_REMOVE]


def emit_novelty_observations(diff: BlocklistDiff, *, emit_removals: bool = False,
                              known_terms=frozenset(), category_map: dict = None) -> list:
    """One NOVELTY observation per ADDED keyword (`deletion_signal="blocklist_add"`).

    A removal is the INVERSE of novelty — a relaxation or a fold into a broader rule (the LINE
    147 Bo-Xilai removals tracked his political rehabilitation), not a new directive. Removals
    are therefore emitted only when `emit_removals=True`, each stamped
    `deletion_signal="blocklist_remove"`.

    IMPORTANT — that stamp is a LABEL, not a downstream guarantee. The two consumers
    (`compute_selectivity_novelty` and `mine_candidates`) do NOT read `deletion_signal`; a
    removal left in a stream handed to them is counted as a brand-new term (hist_count==0 →
    novelty=1.0, full attention) — i.e. a relaxation reported as a maximum-novelty censor-
    attention event, the inverse of its meaning and a number the project cannot stand behind.
    So the `emit_removals=True` output is an EVENT record, NOT direct novelty-index input: it
    MUST be passed through `novelty_inputs()` (which drops the removal-stamped observations)
    before it reaches either consumer. The collector's `run()` does this for you.

    For a SAMPLED (non-exhaustive) artifact a "new" term may be sampling coverage rather than a
    real addition, so severity is forced to "low" and a `_caveat="sampled-nonexhaustive"` flag
    is attached for the index to suppress confidence."""
    new_art = diff.new.artifact
    sampled = new_art.completeness == SAMPLED
    out = []

    def _emit(kw, signal):
        obs = term_to_observation(kw, new_art, signal=signal,
                                  known_terms=known_terms, category_map=category_map)
        if sampled:
            obs["severity"] = "low"
            obs["_caveat"] = "sampled-nonexhaustive"
        out.append(obs)

    for kw in diff.added:
        _emit(kw, BLOCKLIST_ADD)
    if emit_removals:
        for kw in diff.removed:
            _emit(kw, BLOCKLIST_REMOVE)
    return out


# ── collector (governance-gated, I/O injected; BaseCollector pattern, no network default) ───

def _default_local_load(artifact: BlocklistArtifact) -> bytes:
    """Default load_fn: read the artifact bytes from a LOCAL path. No network: a non-local
    `source_ref` (e.g. an http(s) URL) is refused, so the inert/no-network property holds by
    construction. Acquisition of real artifacts is out-of-tree and injected by the deployment."""
    ref = artifact.source_ref or ""
    if ref.split("://", 1)[0].lower() in ("http", "https", "ftp"):
        raise RuntimeError(
            f"refusing to fetch a remote artifact ({ref!r}) — acquisition is out-of-tree and "
            "must be injected via load_fn (Line 1; no network default).")
    with open(ref, "rb") as f:
        return f.read()


class BlocklistArchaeologyCollector:
    """Thin orchestrator over the pure core (BaseCollector *pattern*: injected I/O, governance-
    gated, fail-soft). It does NOT inherit the httpx/async BaseCollector — like UNDERTEXT's
    vantage points it is stdlib-only and standalone. Fed a list of `BlocklistArtifact` whose
    bytes arrive through an injected `load_fn` (default a local-fixtures reader; no network
    default). For each consecutive (old, new) artifact pair of the SAME app, sorted by
    observed_at, it diffs and emits novelty observations, then feeds the existing
    gazetteer_evolution.mine_candidates and ddti_index.compute_selectivity_novelty unchanged."""

    name = "blocklist_archaeology"
    source_type = "file"

    def __init__(self, artifacts, *, load_fn=None, category_map: dict = None,
                 known_terms=None, decryptor=None, emit_removals: bool = False,
                 kill_switch=None, rate_ceiling=None):
        self.artifacts = list(artifacts or [])
        self._load = load_fn or _default_local_load
        self.category_map = category_map or {}
        self._known_terms = set(known_terms) if known_terms is not None else None
        self._decryptor = decryptor
        self.emit_removals = emit_removals
        self._kill = kill_switch
        self._rate = rate_ceiling
        self.warnings: list = []   # decode fallbacks surfaced loud
        self.skipped: list = []    # encrypted/failed artifacts recorded, never silently dropped

    def known_terms(self) -> set:
        if self._known_terms is None:
            self._known_terms = load_known_terms()
        return self._known_terms

    def _parse_one(self, artifact: BlocklistArtifact):
        # Governance: consult the optional kill switch + rate ceiling before any load, so the
        # haltable/polite property is enforced even when an injected load_fn IS a live fetcher.
        if self._kill is not None:
            self._kill.require_live()
        if self._rate is not None:
            self._rate.acquire()
        raw = self._load(artifact)
        parsed = parse_blocklist(raw, artifact, decryptor=self._decryptor,
                                 category_map=self.category_map)
        if parsed.decode_warning:
            self.warnings.append({"app": artifact.app, "version": artifact.version,
                                  "warning": parsed.decode_warning})
        return parsed

    def parse_all(self) -> dict:
        """Parse every artifact, grouped by app and sorted by observed_at. Encrypted-without-
        decryptor (and any parse failure) is recorded in `self.skipped` and skipped — a blocked
        source abstains, it never injects an empty/garbage list (fail-soft, no false zero)."""
        by_app: dict = {}
        for art in self.artifacts:
            try:
                parsed = self._parse_one(art)
            except BlocklistEncryptedError as e:
                self.skipped.append({"app": art.app, "version": art.version,
                                     "reason": "encrypted-no-decryptor", "detail": str(e)})
                logger.warning("[blocklist] skipped encrypted artifact %s %s", art.app, art.version)
                continue
            except Exception as e:  # fail-soft: a bad artifact abstains, never a false zero
                self.skipped.append({"app": art.app, "version": art.version,
                                     "reason": "parse-failed", "detail": str(e)})
                logger.warning("[blocklist] skipped artifact %s %s (%s)",
                               art.app, art.version, type(e).__name__)
                continue
            by_app.setdefault(art.app, []).append(parsed)
        for parsed_list in by_app.values():
            parsed_list.sort(key=lambda p: p.artifact.observed_at)
        return by_app

    def collect(self) -> list:
        """Diff consecutive same-app versions and emit the NOVELTY observation stream."""
        observations: list = []
        known = self.known_terms()
        for parsed_list in self.parse_all().values():
            for old, new in zip(parsed_list, parsed_list[1:]):
                diff = diff_versions(old, new)
                observations.extend(emit_novelty_observations(
                    diff, emit_removals=self.emit_removals,
                    known_terms=known, category_map=self.category_map))
        return observations

    def run(self) -> dict:
        """End-to-end: observations → gazetteer proposal ledger + DDTI selectivity/novelty index.
        Fail-soft: returns a status dict, never raises into a scheduler."""
        try:
            observations = self.collect()
            known = self.known_terms()
            # Removals are recorded as EVENTS in `observations`, but they must never enter the
            # novelty/attention index or the candidate miner (neither reads deletion_signal, so
            # a removal would be miscounted as a max-novelty new term). novelty_inputs() is the
            # boundary that makes the blocklist_remove stamp load-bearing.
            novelty = novelty_inputs(observations)
            ledger = build_proposal_ledger(mine_candidates(novelty, known))
            index = compute_selectivity_novelty(novelty, datetime.now(timezone.utc))
            logger.info("[blocklist] %d novelty obs, %d proposals, %d terms indexed",
                        len(observations), ledger["n_proposals"], index["n_terms"])
            return {
                "status": "success",
                "n_observations": len(observations),
                "observations": observations,
                "ledger": ledger,
                "index": index,
                "decode_warnings": self.warnings,
                "skipped": self.skipped,
            }
        except Exception as e:  # pragma: no cover - defensive
            logger.error("[blocklist] run failed: %s", e)
            return {"status": "error", "error": str(e),
                    "decode_warnings": self.warnings, "skipped": self.skipped}


if __name__ == "__main__":  # offline demo: a YY COVID add falls out as a novelty observation
    def _art(ver, date, completeness=EXHAUSTIVE):
        return BlocklistArtifact(app="yy", version=ver,
                                 observed_at=datetime(*date, tzinfo=timezone.utc),
                                 source_ref=f"citizenlab/chat-censorship/yy/{ver}.txt",
                                 completeness=completeness)

    # Real, dated facts (Citizen Lab, *Censored Contagion*, 2020, Table 1): the 2019-12-31 YY
    # build adds the first batch of Wuhan-pneumonia terms. Acquisition is out-of-tree; here the
    # bytes are inline fixtures so the demo needs no network.
    v_before = "武汉\n上海\n北京\n".encode("utf-8")
    v_after = "武汉\n上海\n北京\n武汉不明肺炎\n武汉海鲜市场\n沙士变异\np4病毒实验室\n".encode("utf-8")

    a0 = parse_blocklist(v_before, _art("2019-12-30", (2019, 12, 30)))
    a1 = parse_blocklist(v_after, _art("2019-12-31", (2019, 12, 31)))
    diff = diff_versions(a0, a1)
    print(f"added={len(diff.added)} removed={len(diff.removed)} fp0={a0.fingerprint()[:8]}")
    for obs in emit_novelty_observations(diff):
        print(f"  {obs['deletion_signal']:14} sev={obs['severity']:6} {obs['title']}")
    # encrypted blob with no decryptor fails loud, never cracks:
    import base64 as _b64
    blob = _b64.b64encode(bytes(range(256)) * 2)
    try:
        parse_blocklist(blob, _art("cbw", (2014, 1, 1)), fmt="encrypted")
    except BlocklistEncryptedError as e:
        print("  encrypted -> refused (loud):", str(e)[:60], "...")
