"""The publication contract: every reading must say when, from what, and out of what.

Palimpsest's promise is "nothing is published without evidence". `core/governance.py`
makes the *safety* rules executable; `core/claim_support.py` makes the *claim* arithmetic
executable. This file does the same for the publication boundary, which is the last place
a number can go wrong before a reader sees it.

The contract is three questions every published reading must answer:

  WHEN         a timestamp, so a stale feed is visible as stale
  FROM WHAT    provenance -- the source, method, or verification command behind it
  OUT OF WHAT  a denominator, so a numerator is never quotable on its own

The third is the one this project keeps relearning. "Zero deletions" means nothing
without the posts watched; a delisted-app count means nothing without the panel size;
203 diverging pools meant nothing without the 204 targets they were drawn from.

WHY THIS IS AN INVENTORY AND NOT A HEURISTIC. An earlier attempt guessed denominators
from field-name substrings and was wrong in both directions: it missed `citation` and
`verify_cmd` as provenance, and it demanded a denominator from `china-econ`, which
publishes benchmark levels and has no population to count. A test that cries wolf gets
suppressed, and a suppressed test protects nothing. So each signal *declares* its fields,
the declaration is checked against the actual file, and a signal with no denominator must
say why in writing. That is the same discipline as `_ALLOWED` in test_egress_policy.py.

THE RATCHET. A new reading that is not registered here fails the first test. That is the
point: it cannot reach the board until someone has answered all three questions for it.
"""

import glob
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")


def _d(timestamp, provenance, denominator=None, *, reason=None):
    return {"timestamp": timestamp, "provenance": provenance,
            "denominator": denominator, "reason": reason}


# signal -> what it declares. `denominator=None` REQUIRES a written reason.
CONTRACT = {
    # ── measurements with a population ────────────────────────────────────────
    "app-storefront":       _d("generated_at", ["source", "scope"], "n_tracked"),
    "baike-redaction":      _d("generated_at", ["source", "method"], "n_comparable"),
    "cross-layer":          _d("generated_at", ["method"], "n_pairs_tested"),
    "ddti":                 _d("generated_at", ["citation"], "n_observations"),
    "forecast-ledger":      _d("generated_at", ["method"], "n_signals_scored"),
    "gdelt":                _d("generated_at", ["source", "scope"], "n_terms"),
    "github-refuge":        _d("generated_at", ["source", "scope"], "n_watched"),
    "inside-view":          _d("generated_at", ["source", "method"], "panel_size"),
    "net4people":           _d("generated_at", ["source", "method"], "n_recent"),
    "ooni-gfw":             _d("generated_at", ["source", "method"], "n_measurements"),
    "refusal-drift":        _d("generated_at", ["method", "verify_cmd"], "n_probes"),
    "wayback":              _d("generated_at", ["source", "scope"], "n_watched"),
    "weibo-hotsearch":      _d("generated_at", ["source", "method_note"], "board_entries"),
    "censored-planet":      _d("generated_at", ["source", "method"], "n_events"),
    "eval-registry":        _d("generated_at", ["registry", "verify_cmd"], "runs"),
    "blocklist":            _d("generated_at", ["source", "attribution"], "n_versions"),
    # registered before its first round lands — see PENDING below
    "bleedthrough":         _d("generated_at", ["method", "scope", "provenance"],
                               "vantages_probed"),
    "data-darkness":        _d("generated_at", ["source", "method_note"],
                               "n_series_watched"),
    "silence-index":        _d("generated_at", ["source", "method_note"],
                               "n_topics_considered"),
    "believability":        _d("generated_at", ["source", "method_note"],
                               "n_components_required"),

    # ── readings with no population to count, each with its reason ────────────
    "china-econ": _d(
        "generated_at", ["source"],
        reason="publishes official CFETS benchmark LEVELS, not a count over a sample; "
               "there is no population to be a denominator of."),
    "stock-connect": _d(
        "generated_at", ["source"],
        reason="publishes the HKEX daily flow print, a single official value per day; "
               "a denominator would be invented, not measured."),
    "cny-fix-gap": _d(
        "generated_at", ["source", "method_note"],
        reason="publishes one derived daily divergence between two official prices "
               "(the PBOC fix and an independent reference rate); there is no sample "
               "and no population to be a denominator of."),
    "circumvention-demand": _d(
        "generated_at", ["source", "method_note"],
        reason="publishes Tor bridge-user levels from the Tor metrics series; the value "
               "is an estimated count of users, not a fraction of a sample we drew."),
    "ioda-outages": _d(
        "generated_at", ["source", "method_note"],
        reason="reports discrete outage events from three external instruments; the "
               "instrument count is published as instruments_firing, and there is no "
               "population of non-events to divide by."),
    "anchors": _d(
        "ts", ["registry_root", "erasure_root"],
        reason="anchoring infrastructure rather than a measurement: it records which "
               "roots were witnessed where, and witnesses are enumerated, not sampled."),

    # ── roll-ups over other signals: their denominator is the layer set ───────
    "erasure-observatory": _d(
        "generated_at", ["thesis", "index_scale"],
        reason="a roll-up across erasure layers; its inputs are the layer readings "
               "themselves, enumerated in layers_contributing rather than sampled."),
    "board-alarm": _d(
        "generated_at", ["method", "board_guarantee"],
        reason="merges the per-signal e-detectors; the signals it merged are enumerated "
               "in the signals container, and it draws no sample of its own."),
    "event-flags": _d(
        "generated_at", ["method", "guarantee"],
        reason="per-signal conformal detector; each signal is compared against its own "
               "history, so the population is that signal's history, not a cross-section."),
    "coverage-guard": _d(
        "generated_at", ["method"],
        reason="audits the other signals' coverage; the audited set is enumerated in the "
               "signals container rather than sampled from a larger population."),
    "vantage-fusion": _d(
        "generated_at", ["method"],
        reason="fuses the network vantages that reported; the contributing and excluded "
               "vantages are both enumerated, so a ratio would double-count them."),
    "osint-china": _d(
        "generated_at", ["source", "method", "scope"], "n_signals_total"),
    "nemesis": _d(
        "generated_at", ["source", "method", "scope"],
        reason="an allowlisted operational snapshot whose observed sources and component "
               "counts are explicitly enumerated in coverage and counts; it is not one "
               "ratio drawn from a larger sampled population."),
    "in-path-interference": _d(
        "generated_at", ["source", "method"],
        reason="indices are measurement-weighted means over OONI's published aggregate; "
               "the per-test denominators live inside the tests container, and a single "
               "top-level count would misrepresent a weighted mean."),
    "apple-censorship": _d(
        "generated_at", ["source", "scope"],
        reason="unavailable_pct is computed against the same catalogue measured in peer "
               "storefronts; the comparison set is enumerated in the country container "
               "rather than drawn as a sample."),
}


def _readings():
    return sorted(glob.glob(os.path.join(READINGS, "*-latest.json")))


def _name(path):
    return os.path.basename(path).replace("-latest.json", "")


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_every_published_reading_is_registered():
    """A new feed cannot reach the board without answering the three questions."""
    unregistered = [_name(p) for p in _readings() if _name(p) not in CONTRACT]
    assert not unregistered, (
        "these readings publish without a declared publication contract. Add each to "
        "CONTRACT in this test with its timestamp field, its provenance field(s), and "
        "either its denominator or an honest written reason it has none:\n  "
        + "\n  ".join(unregistered))


# Registered, but not publishing yet: a signal that is built and whose contract is agreed
# before its first reading lands. Keeping these here means the first live round cannot break
# the build, and — more usefully — means the three questions were answered while the code was
# being written rather than retrofitted once a number was already on the board.
PENDING = {
    # BLEEDTHROUGH measures the GFW injector fleet from outside China. It is deliberately
    # unpublished until a multi-vantage round exists; see docs/BLEEDTHROUGH.md and the worked
    # case in docs/INTEGRITY.md.
    "bleedthrough",
}

# External public products whose contract is agreed here but whose presence is deliberately
# deployment-dependent. Unlike PENDING, these do not "graduate": Nemesis remains optional so a
# repository with no NEMESIS_SNAPSHOT_URL has an honest missing layer rather than a fake zero.
OPTIONAL_EXTERNAL = {
    "nemesis",
}


def test_no_contract_entry_describes_a_reading_that_does_not_exist():
    """Keeps the inventory honest as signals are retired, without punishing signals whose
    contract was agreed before their first round."""
    present = {_name(p) for p in _readings()}
    stale = sorted(set(CONTRACT) - present - PENDING - OPTIONAL_EXTERNAL)
    assert not stale, (
        f"CONTRACT describes readings that no longer exist: {stale}. Retire the entry, or "
        "move it to PENDING if the signal is built but not publishing yet.")


def test_pending_signals_are_registered_and_do_not_linger_silently():
    """A pending signal must still declare its contract, and must leave PENDING once it
    publishes, so PENDING cannot become a permanent parking space."""
    unregistered = sorted(PENDING - set(CONTRACT))
    assert not unregistered, f"PENDING lists signals with no declared contract: {unregistered}"
    published = {_name(p) for p in _readings()}
    graduated = sorted(PENDING & published)
    assert not graduated, (
        f"these signals now publish and must be removed from PENDING: {graduated}")


def test_optional_external_signals_are_pre_registered_not_silently_required():
    """An optional deployment feed still agrees its contract before its first import."""
    unregistered = sorted(OPTIONAL_EXTERNAL - set(CONTRACT))
    assert not unregistered, f"optional external signals have no contract: {unregistered}"
    assert not (OPTIONAL_EXTERNAL & PENDING), (
        "deployment-dependent signals must not also use one-time PENDING graduation semantics")


@pytest.mark.parametrize("path", _readings(), ids=_name)
def test_reading_declares_when_it_was_measured(path):
    d, c = _load(path), CONTRACT[_name(path)]
    assert c["timestamp"] in d, (
        f"{_name(path)} declares timestamp '{c['timestamp']}', which is not in the file")
    assert d[c["timestamp"]], f"{_name(path)} has an empty timestamp"


@pytest.mark.parametrize("path", _readings(), ids=_name)
def test_reading_declares_where_it_came_from(path):
    d, c = _load(path), CONTRACT[_name(path)]
    missing = [f for f in c["provenance"] if f not in d or d[f] in (None, "", [], {})]
    assert not missing, (
        f"{_name(path)} declares provenance {c['provenance']} but these are absent or "
        f"empty: {missing}. A reader cannot check a number whose origin is not stated.")


@pytest.mark.parametrize("path", _readings(), ids=_name)
def test_a_count_is_never_published_without_its_denominator(path):
    d, c = _load(path), CONTRACT[_name(path)]
    if c["denominator"] is None:
        assert c["reason"] and len(c["reason"]) > 40, (
            f"{_name(path)} declares no denominator, so it must say why in writing. "
            "An omission and a considered exemption look identical without one.")
        return
    assert c["denominator"] in d, (
        f"{_name(path)} declares denominator '{c['denominator']}', which is not in the file")
    v = d[c["denominator"]]
    assert isinstance(v, int), (
        f"{_name(path)} denominator '{c['denominator']}' is {type(v).__name__}, not an int")
    assert v >= 0, f"{_name(path)} denominator '{c['denominator']}' is negative: {v}"
