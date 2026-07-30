"""Direct tests for processors.ddti_index.extract_terms — the text→terms transform.

Every other DDTI test hands compute_selectivity_novelty a pre-made `terms` list, so the
one function that actually decides WHICH words the published index ranks was never
exercised. It is the whole input side of the DDTI: if it silently stops finding the
censored term inside a headline, the index keeps publishing a confident ranking of
nothing in particular. These tests pin its four extraction sources against real
behaviour, so a later edit to the span regex or the gazetteer loader fails loudly here.

Standard-library only, offline, no fixtures — matching the sealed-signal suites.

    PYTHONPATH=. python3 -m pytest tests/test_extract_terms.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from processors.ddti_index import extract_terms, load_censorship_terms  # noqa: E402

EMPTY_LEXICON: dict = {}  # the censorship-only build passes no domain lexicon


def _terms(title="", text="", tags=None):
    return extract_terms(title, text, tags or [], EMPTY_LEXICON)


# ── (1) bracketed / quoted spans in the title are the censored term itself ──────────────
# CDT wraps the sensitive vocabulary in CJK brackets or double quotes; those spans ARE the
# term, so each opening delimiter must still pair with its closing one.
SPAN_CASES = [
    ("CJK book brackets", "CDT: 《敏感词汇》 removed", "敏感词汇"),
    ("CJK corner brackets", "CDT: 「敏感词汇」 removed", "敏感词汇"),
    ("CJK white corner brackets", "CDT: 『敏感词汇』 removed", "敏感词汇"),
    ("curly double quotes", "CDT: “sensitive phrase” pulled", "sensitive phrase"),
    ("straight double quotes", 'CDT: "sensitive phrase" pulled', "sensitive phrase"),
]


def test_bracketed_span_in_title_is_extracted():
    for label, title, expected in SPAN_CASES:
        got = _terms(title=title)
        assert expected in got, f"{label}: {expected!r} not extracted from {title!r} (got {got})"


def test_span_is_only_read_from_the_title_not_the_body():
    # Deliberate: the headline names the censored term; a quoted span deep in the body is
    # ordinary reported speech, not the deletion trigger.
    assert "quoted in body" not in _terms(title="plain headline", text='he said "quoted in body"')


# ── (2) length bounds: a 1-char span and an over-long span are both dropped ─────────────
# Two different mechanisms, both load-bearing:
#   len 1  — the span regex requires 2+ chars, and the post-strip `1 < len(m)` guard catches
#            the case where padding made it match ("《 a 》" captures " a ", strips to "a");
#   len>60 — the regex caps the span at 60 chars, so an unterminated-looking run of text
#            never becomes a "term". Both keep punctuation noise out of a published ranking.
LENGTH_CASES = [
    ("padded single char is dropped by the post-strip guard", "《 a 》", "a", False),
    ("two chars is the shortest kept span", "《ab》", "ab", True),
    ("exactly 60 chars is kept", "《" + "x" * 60 + "》", "x" * 60, True),
    ("61 chars exceeds the span cap", "《" + "x" * 61 + "》", "x" * 61, False),
]


def test_span_length_bounds():
    for label, title, term, want_present in LENGTH_CASES:
        got = _terms(title=title)
        assert (term in got) is want_present, f"{label}: present={term in got}, want={want_present}"


# ── (3) canonical English entities match case-insensitively anywhere in the blob ────────
# Substring + casefold, and the match is over title AND body, so a term named only in the
# article text is still counted. The canonical spelling is what gets stored, never the
# casing the source happened to use — otherwise "xinjiang" and "Xinjiang" would rank as
# two separate terms and split the attention score for the same target.
GAZETTEER_CASES = [
    ("lowercase in body", "", "reports out of xinjiang province", "Xinjiang"),
    ("uppercase in body", "", "REPORTS OUT OF XINJIANG", "Xinjiang"),
    ("multi-word, mixed case, in title", "Unrest in hong KONG", "", "Hong Kong"),
    ("embedded in a longer word boundary-free run", "", "the tiananmen anniversary", "Tiananmen"),
]


def test_english_gazetteer_matches_case_insensitively():
    for label, title, text, expected in GAZETTEER_CASES:
        got = _terms(title=title, text=text)
        assert expected in got, f"{label}: {expected!r} not found (got {got})"


# ── (4) the loaded Chinese gazetteer fires on the blob ──────────────────────────────────
def test_censorship_gazetteer_is_actually_loaded():
    # The loader swallows its own exceptions and returns an empty tuple on failure, so a
    # broken config path would silently reduce extraction to the English list alone. Assert
    # the gazetteer is non-empty FIRST, so that failure is named rather than showing up as a
    # confusing miss below.
    assert load_censorship_terms(), "censorship gazetteer loaded empty — extraction is degraded"


def test_chinese_euphemism_from_the_gazetteer_is_found():
    terms = load_censorship_terms()
    assert "坦克人" in terms, "expected the June Fourth euphemism in the loaded gazetteer"
    got = _terms(title="平常的标题", text="据说有人提到坦克人这件事")
    assert "坦克人" in got, f"gazetteer euphemism not extracted from the body (got {got})"


# ── (5) CDT tags: real ones kept, CMS taxonomy noise dropped ────────────────────────────
TAG_CASES = [
    ("a real CDT tag is kept", "Real Term", True),
    ("generic CMS section is dropped", "Politics", False),
    ("stop-list match is case-insensitive", "CDT HIGHLIGHTS", False),
    ("whitespace-only tag is dropped", "   ", False),
]


def test_tags_are_filtered_against_the_taxonomy_stop_list():
    for label, tag, want_present in TAG_CASES:
        got = _terms(title="headline", tags=[tag])
        assert (tag.strip() in got) is want_present, f"{label}: got {got}"


# ── (6) output shape: sorted and de-duplicated across sources ───────────────────────────
def test_output_is_sorted_and_deduplicated():
    # The same term reachable from two sources at once (a quoted span that is also a tag)
    # must appear once, or it would double-count that term's censor attention.
    got = _terms(title="CDT: 《敏感词汇》 removed", tags=["敏感词汇"])
    assert got.count("敏感词汇") == 1
    assert got == sorted(got)


def test_empty_input_yields_no_terms():
    assert _terms() == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"=== extract_terms: {passed}/{len(fns)} passed ===")
    sys.exit(0 if passed == len(fns) else 1)
