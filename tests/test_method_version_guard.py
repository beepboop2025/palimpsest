"""Guard test: a methodology correction must be able to reach the published file.

Signal drivers used to use write-if-changed — compare the new reading against the
one on disk, skip the write when the values match. It looked like the right way to
avoid a commit every six hours that says nothing.

It had two failure modes, and both were quiet.

The first: when a METHOD changes but the numbers do not, the comparison sees no
difference and skips the write, so the site keeps serving old method text, old
caveats, an old vantage list. Not hypothetical. `inside_view` was corrected to stop
drawing household probes for censored queries and to require two ASNs before a
blocking verdict; both left `block_rate` at 1.0, the guard said "unchanged", and the
site kept advertising the very vantage list the fix existed to remove. That reached
the file only once METHOD_VERSION joined the comparison.

The second killed the approach. A reading that is never rewritten cannot say "I
looked and nothing changed". index.html renders generated_at as
"stale · last measured", so a finding that held still — which is what sustained
censorship looks like — read on the public site as a collector that had died. The
fleet therefore moved to publishing every round that survives its control gate,
with last_changed_at carrying the movement and history files still gated on change.

What this file now pins: every publishing driver declares a METHOD_VERSION and
emits it into the reading, no driver reintroduces the conditional write, and the
detector that decides which shape a driver has still works. If a conditional writer
ever comes back, the method_version comparison requirement applies to it again.

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


_FIXTURE_CONDITIONAL = '''
def main():
    out = {"generated_at": now}
    if changed or not prev:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f)
'''

_FIXTURE_UNCONDITIONAL = '''
def main():
    out = {"generated_at": now}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f)
'''


def test_the_detector_still_tells_the_two_shapes_apart():
    """This used to be asserted against the live drivers: at least one of each
    kind had to exist, so neither branch of the classification could quietly
    empty out and make the assertions above vacuous.

    That premise died when the fleet moved to unconditional writes. Every driver
    is now the same shape, so sampling the codebase can no longer prove the
    detector works — it would only prove they all look alike, which is exactly
    what a broken detector also reports. Pin it to fixtures instead, and the
    guard keeps its teeth no matter what the fleet happens to look like.
    """
    assert _writes_conditionally(_FIXTURE_CONDITIONAL), (
        "the detector no longer recognises a write nested inside an if — the "
        "conditional-writer assertions above would silently pass for everything")
    assert not _writes_conditionally(_FIXTURE_UNCONDITIONAL), (
        "the detector calls a plain function-body write conditional — it would "
        "demand a method_version comparison that has nothing to guard")


def test_no_driver_writes_its_reading_conditionally():
    """The invariant the fleet now holds, and the reason the class above is empty.

    A driver that only rewrote its reading when the values moved could not say
    "I looked and nothing changed". index.html renders generated_at as
    "stale · last measured", so a finding that held still — sustained blocking,
    a door that stays shut — read on the public site as a collector that had
    died. Silence from a censor and silence from a dead collector are not the
    same claim.

    So every driver now publishes each round that survives its control gate, and
    carries last_changed_at for the movement. History files stay gated on change;
    a heartbeat is not an event. If a new driver reintroduces the old shape this
    fails, and the method_version assertion above starts applying to it again.
    """
    offenders = [p.name for p, t in _drivers() if _writes_conditionally(t)]
    assert not offenders, (
        "driver(s) rewrite their reading only when the values move, so a round "
        "that observed no change cannot be distinguished from a round that never "
        f"ran, and the site will call them stale: {offenders}")
