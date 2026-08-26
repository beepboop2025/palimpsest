#!/usr/bin/env python3
"""Check the tracked tree before it is served.

This is a GitHub Pages repository, so PUBLISHING IS THE DEFAULT: every tracked file is
served at palimpsest.info whether or not anyone meant it to be. This is the grep that runs
over `git ls-files`, which is that set, and it checks two kinds of rule.

  SHAPE RULES, in this file, safe to publish because they name shapes rather than people:
  an internal annotation with a handle in it, a local machine hostname, an email address on
  a domain this project does not own.

  RULE LIST, never in this file, because a public repository that lists the strings it must
  not contain contains them. The list is read from the env var PALIMPSEST_SCRUB_STRINGS (one
  string per line) or, locally, from an untracked `.scrub-strings.local` at this repo's root.
  A match is reported by rule number and never by content, so a failing log does not leak
  what it caught.

WHAT BLOCKS AND WHAT ONLY WARNS. This runs immediately before a push on workflows that
publish, so a check that exits non-zero for a heuristic reason stops the observatory from
refreshing, and a gate that stops routine work is a gate someone deletes. Only two things
exit non-zero: a rule-list match anywhere, and a shape match in an authored file. A shape
match in a collected corpus (a scraped page, a model transcript, a vendor fixture) is
printed as a warning and exits 0, because a third party's address inside a corpus is the
subject matter rather than a leak.

If no rule list is configured the shape rules still run and the report says so loudly, in
place of the half of the check that did not run. --require-rules turns that into an exit
code, for a manual run where the operator wants to know.

    python3 scripts/verify_public_surface.py
    python3 scripts/verify_public_surface.py --require-rules

Exit 0 when nothing blocking was found, 1 otherwise.
"""
from __future__ import annotations

import bisect
import collections
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_STRINGS = os.path.join(ROOT, ".scrub-strings.local")
STRINGS_ENV = "PALIMPSEST_SCRUB_STRINGS"

# Files whose bytes are not prose or code. Reading them wastes time and their content is
# checked at the source that generates them.
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".woff",
                 ".woff2", ".ttf", ".otf", ".zip", ".gz", ".ots")

# This file spells out the shapes it forbids, so the SHAPE rules would match it. Only those
# are waived, and only for this one path: the rule list and the address rule run over it
# like any other published file.
SELF = "scripts/verify_public_surface.py"

# Paths whose bytes come from a third-party feed rather than from an author here, either
# collected raw or rendered straight out of a collected corpus. A shape found in one of these
# is reported and does not block, because the alternative is a scraped corpus that can never
# be refreshed until someone hand-lists every address in it.
#
# china-brief.html, demo/ and dashboards/ carry China Digital Times article titles and URLs,
# rebuilt on a schedule by china-brief-refresh.yml and ddti-refresh.yml. Those workflows run
# this check in the step before `git push`, so a shape arriving in one scraped title would
# stop the refresh rather than report on it.
COLLECTED_PREFIXES = ("readings/", "data/", "demo/", "dashboards/", "china-brief.html")

# Email domains this project owns or that are unambiguously not a person's. Anything else is
# treated as a personal address until someone says otherwise, which is the right default for
# a pseudonymous repository.
ALLOWED_EMAIL_DOMAINS = ("palimpsest.info", "users.noreply.github.com", "example.com",
                         "example.org", "localhost")

# Subject-matter addresses: published BY the thing being measured, not by us. Named one at a
# time on purpose, so the allow-list cannot quietly become a domain wildcard.
ALLOWED_EMAILS = (
    "jubao@eastmoney.com",          # Guba's own report-a-post address, in a scraped fixture
)

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# A systemd template instance is operational identity, not a mailbox. Keep this
# exemption deliberately narrower than a generic ``*.service`` suffix: only the
# numeric broker instances created by the Palimpsest release controller qualify.
SYSTEMD_BROKER_INSTANCE = re.compile(
    r"palimpsest-investigative-broker@[0-9]+-[0-9]+-10001\.service"
)

WHITESPACE = re.compile(r"\s+")

PATTERNS = [
    # Assembled so the pattern does not contain the literal it forbids and match itself.
    (re.compile(r"\b(?:TODO|FIXME|HACK|XXX)" + re.escape("(")),
     "internal annotation with a handle in it, served live"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.local\b"),
     "a local machine hostname, which carries a username"),
]

Finding = collections.namedtuple("Finding", "path line why blocking")


def render(finding: Finding) -> str:
    return f"{finding.path}:{finding.line}: {finding.why}"


def is_collected(path: str) -> bool:
    """True for a path whose bytes this project reads from the world rather than writes."""
    return (path.startswith(COLLECTED_PREFIXES)
            or path.startswith("fixtures/") or "/fixtures/" in path)


def identity_rules() -> tuple:
    """The strings that must never appear, and where they came from.

    Env first so CI can carry them as a secret; the untracked local file second so a laptop
    run checks the same things without one. Blank lines and #-comments are ignored, and the
    rules are returned in file order because that is how a failure refers to them.
    """
    raw = os.environ.get(STRINGS_ENV)
    if raw:
        source = f"${STRINGS_ENV}"
    elif os.path.exists(LOCAL_STRINGS):
        raw = open(LOCAL_STRINGS, encoding="utf-8").read()
        source = os.path.basename(LOCAL_STRINGS)
    else:
        return (), None
    rules = [line.strip() for line in raw.splitlines()
             if line.strip() and not line.strip().startswith("#")]
    return tuple(rules), source


def tracked_files() -> list:
    """What Pages will serve: everything tracked, plus everything untracked that is not
    ignored, because a new file is published the moment it is committed, and the run that
    should catch it is the one before that."""
    out = subprocess.run(
        ["git", "-C", ROOT, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=True).stdout
    return sorted({p for p in out.split("\0") if p})


def flattened(text: str) -> tuple:
    """The text with each run of whitespace collapsed to one space, and the bookkeeping that
    turns an offset in it back into a line number.

    A rule of two or three words is the ordinary case, and any formatter that wraps prose can
    put a newline in the middle of one. Matching line by line looks for the words in a place
    they need not be. Returns (flat, spans) where spans pairs a flat offset with the offset
    in the original text that it came from.
    """
    parts, spans = [], []
    flat_len, pos = 0, 0
    for m in WHITESPACE.finditer(text):
        chunk = text[pos:m.start()]
        if chunk:
            parts.append(chunk)
            spans.append((flat_len, pos))
            flat_len += len(chunk)
        parts.append(" ")
        spans.append((flat_len, m.start()))
        flat_len += 1
        pos = m.end()
    tail = text[pos:]
    if tail:
        parts.append(tail)
        spans.append((flat_len, pos))
    return "".join(parts), spans


def line_at(text: str, spans: list, flat_index: int) -> int:
    """The line the match STARTS on, which is the line someone has to open."""
    i = bisect.bisect_right(spans, (flat_index, len(text) + 1)) - 1
    if i < 0:
        return 1
    flat_start, orig_start = spans[i]
    return text.count("\n", 0, orig_start + (flat_index - flat_start)) + 1


def _findings_in(path: str, text: str, rules: tuple, patterns: bool = True) -> list:
    blocks = not is_collected(path)
    problems = []
    for n, line in enumerate(text.splitlines(), 1):
        if patterns:
            for pattern, why in PATTERNS:
                if pattern.search(line):
                    problems.append(Finding(path, n, why, blocks))
        for address_match in EMAIL.finditer(line):
            address = address_match.group(0)
            if SYSTEMD_BROKER_INSTANCE.fullmatch(address):
                continue
            if address in ALLOWED_EMAILS:
                continue
            if any(address.lower().endswith("@" + d) for d in ALLOWED_EMAIL_DOMAINS):
                continue
            problems.append(Finding(
                path, n, "an email address on a domain this project does not own", blocks))
    if rules:
        flat, spans = flattened(text)
        haystack = flat.lower()
        for i, rule in enumerate(rules, 1):
            needle = WHITESPACE.sub(" ", rule.strip()).lower()
            if not needle:
                continue
            at = haystack.find(needle)
            while at != -1:
                # The rule is NOT echoed. A CI log is as public as the site.
                problems.append(Finding(path, line_at(text, spans, at),
                                        f"identity rule #{i} matched", True))
                at = haystack.find(needle, at + 1)
    return problems


def check(paths=None, rules=()) -> list:
    problems = []
    for path in (tracked_files() if paths is None else paths):
        if path.lower().endswith(SKIP_SUFFIXES):
            continue
        full = os.path.join(ROOT, path)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            problems.append(Finding(path, 0, f"could not be read ({e.__class__.__name__}), "
                                             f"so it was not checked", False))
            continue
        except UnicodeDecodeError:
            # Publishable text this checker cannot decode is not a pass. A string it would
            # have caught survives any encoding it cannot read.
            problems.append(Finding(path, 0, "is not UTF-8 text and was not checked. Add "
                                             "its suffix to SKIP_SUFFIXES if it is "
                                             "genuinely binary", False))
            continue
        problems.extend(_findings_in(path, text, rules, patterns=path != SELF))
    return problems


def main() -> int:
    require = "--require-rules" in sys.argv[1:]
    rules, source = identity_rules()
    problems = check(rules=rules)
    if source:
        print(f"public-surface: {len(rules)} rule(s) loaded from {source}")
    else:
        print(f"public-surface: WARNING, no rule list is configured. The shape rules ran; "
              f"the rule list did NOT. Set {STRINGS_ENV} or write "
              f"{os.path.basename(LOCAL_STRINGS)} (untracked) to arm it.", file=sys.stderr)
        if require:
            print("REFUSING: --require-rules was passed and no rule list is configured.",
                  file=sys.stderr)
            return 1
    for p in problems:
        if not p.blocking:
            print(f"public-surface: WARNING, not blocking: {render(p)}", file=sys.stderr)
    blocking = [p for p in problems if p.blocking]
    if blocking:
        print("REFUSING: the tracked tree is published in full, and it carries:",
              file=sys.stderr)
        for p in blocking:
            print(f"  - {render(p)}", file=sys.stderr)
        return 1
    print(f"public-surface: {len(tracked_files())} tracked files, nothing to redact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
