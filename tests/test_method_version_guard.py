"""Guard test: a methodology correction must be able to reach the published file.

Every signal driver here uses write-if-changed — it compares the new reading against
the one on disk and skips the write when the values match. That is right for
avoiding a commit every six hours that says nothing.

It has one failure mode, and it is quiet. When a METHOD changes but the numbers do
not, the guard sees no difference and skips the write, so the site keeps serving the
old reading: old method text, old caveats, old vantage list. The published file then
describes a method that is no longer the one that produced it.

This is not hypothetical. `inside_view` was corrected to stop drawing household
probes for censored queries and to require two ASNs before a blocking verdict. Both
changes left `block_rate` at 1.0, the guard said "unchanged", and the site kept
advertising the superseded method and the very vantage list the fix existed to
remove. The fix reached the file only after METHOD_VERSION joined the comparison.

So: any driver that guards its write must carry a METHOD_VERSION and must consult it
in that guard. Drivers that rewrite unconditionally cannot have this bug, and are
exempt from the comparison requirement — but still declare the constant, because a
reader diffing two history rows needs to know whether the method moved underneath
them.

Standard-library only; this reads source text and never imports the drivers.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# A driver is anything that publishes a readings/*-latest.json.
_PUBLISHES = re.compile(r'OUT\s*=\s*os\.path\.join\(\s*READINGS\s*,\s*["\'][^"\']*latest\.json')

# Whether the reading is written CONDITIONALLY, detected structurally rather than by
# guessing at text. `open(OUT, "w")` sitting at the function-body indent (4 spaces)
# is an unconditional rewrite; deeper than that means it lives inside an `if`.
#
# This distinction cost a wrong patch to learn. Several drivers reference `previous`
# and compare a projection of it, which looks exactly like a write guard — but the
# comparison controls only whether a HISTORY row is appended, while the reading
# itself is rewritten every run. Adding a method_version clause to those guards would
# have appended a spurious history row asserting a state change that never happened.
# Text proxies could not tell the two apart; indentation can.
# [ \t]* not \s*: \s matches newlines, so a blank line above the call would let the
# match start one line early and report every indent as one deeper than it is.
_WRITES_READING = re.compile(r'^([ \t]*)with open\(OUT, ["\']w["\']', re.M)


def _writes_conditionally(text: str) -> bool:
    m = _WRITES_READING.search(text)
    return bool(m) and len(m.group(1)) > 4


def _drivers():
    for p in sorted(SCRIPTS.glob("*_pull.py")):
        text = p.read_text(encoding="utf-8")
        if _PUBLISHES.search(text):
            yield p, text


def test_there_are_drivers_to_check():
    """If the discovery regex silently stops matching, every assertion below turns
    vacuous and the guard quietly protects nothing."""
    assert len(list(_drivers())) >= 8


def test_every_publishing_driver_declares_a_method_version():
    missing = [p.name for p, t in _drivers() if "METHOD_VERSION" not in t]
    assert not missing, (
        "driver(s) publish a reading without declaring METHOD_VERSION: "
        f"{missing}. A methodology change must be expressible to a reader."
    )


def test_every_conditional_writer_consults_the_method_version():
    """The load-bearing assertion. A driver that writes its reading only when
    something changed must include METHOD_VERSION in that comparison, or a
    method-only fix never ships."""
    offenders = []
    for p, t in _drivers():
        if not _writes_conditionally(t):
            continue
        # the guard must read the PREVIOUS reading's method_version, whatever the
        # local name for it is
        if not re.search(r'(?:prev|previous)\.get\(\s*["\']method_version["\']', t):
            offenders.append(p.name)
    assert not offenders, (
        "driver(s) guard their write but never compare method_version, so a "
        f"methodology correction that leaves the numbers unchanged would never reach "
        f"the published file: {offenders}"
    )


def test_every_publishing_driver_emits_the_method_version_into_the_reading():
    """Comparing against a field the reading never carries would make the guard
    always fire, which is a different bug wearing the same clothes."""
    # Two legitimate shapes: a key in a dict literal, or an assignment onto a
    # reading built by an imported function.
    _EMITS = re.compile(r'"method_version":\s*METHOD_VERSION'
                        r'|\[["\']method_version["\']\]\s*=\s*METHOD_VERSION')
    missing = [p.name for p, t in _drivers() if not _EMITS.search(t)]
    assert not missing, (
        f"driver(s) never write method_version into the reading: {missing}"
    )


def test_at_least_one_driver_of_each_kind_exists():
    """If either branch of the classification empties out, the assertions above
    stop testing what they claim to."""
    kinds = [_writes_conditionally(t) for _, t in _drivers()]
    assert any(kinds), "no driver writes its reading conditionally — check the detector"
    assert not all(kinds), "no driver writes unconditionally — check the detector"
