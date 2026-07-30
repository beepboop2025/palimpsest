"""Claim support — the arithmetic of "do we have enough to say this at all?".

Palimpsest's recurring failure mode is not a crash. It is publishing a number the
measurement cannot carry. The commit log is full of the same lesson relearned per
collector: zero deletions over zero posts is not a measurement of zero deletions;
a dropped connection is not a takedown; a mandatory filing footer is not a
censorship verdict; a per-target sample of a rotating pool is not a provincial
firewall. Each was fixed where it was found, and the next collector reinvented it.

Two rules keep showing up, so they live here once, with one definition and one
test, rather than as a fresh constant in every collector that needs them.

  quorum      — a claim needs N *independent* backers, not N observations. Two
                readings from one network are one network agreeing with itself.
  sampled     — when distinct observations track the number of observations, you
                are sampling a larger space, not enumerating it, and set-equality
                between samples means nothing.

Standard library only, no I/O, fully offline-testable, so a reviewer can check the
honesty machinery without running the pipeline.
"""

from __future__ import annotations


def distinct_backers(items, key) -> int:
    """How many *independent* sources back this observation set.

    `key` maps an item to the thing that makes it independent — an ASN, a region,
    a vantage. Counting rows instead of backers is the mistake this exists to stop:
    twenty probes inside one datacenter are one datacenter, and a claim resting on
    them has a support of 1, not 20.
    """
    return len({key(i) for i in items if key(i) not in (None, "")})


def has_quorum(items, key, *, minimum: int) -> bool:
    """Is there enough independent support to make the claim at all?

    Deliberately a hard gate rather than a confidence weight: the downstream
    surfaces publish severities and coloured bands, and a low-confidence claim
    rendered in red is indistinguishable from a high-confidence one. Below quorum
    the honest output is nothing, not a hedge.
    """
    if minimum < 1:
        raise ValueError("quorum minimum must be at least 1")
    return distinct_backers(items, key) >= minimum


def looks_sampled(distinct: int, total: int, *, ratio: float = 0.5) -> bool:
    """Are these observations a SAMPLE of a larger space rather than an enumeration?

    The test is whether distinct values track the number of observations. If almost
    every observation is unique, the space is bigger than what was looked at, and any
    comparison that treats two observation sets as comparable objects — set equality,
    a content hash, "these two disagree, therefore they differ" — is measuring the
    sample size instead of the world.

    This is not hypothetical. BLEEDTHROUGH's first live round hashed the forged IPs
    each target drew from the GFW's rotating pool and compared those hashes across
    targets. 204 injecting targets produced 204 distinct hashes, so 203 of them
    "diverged" from the modal one and the board was one commit away from announcing
    203 autonomous provincial firewalls. A direct measurement then drew 47 distinct
    forged addresses from 40 queries, still climbing: the pool was never enumerable
    at that burst, so no two targets could have agreed. See docs/INTEGRITY.md.

    `ratio` is the fraction of observations that must be distinct before we call it
    sampled. The default of 0.5 is deliberately loose — it fires only when the
    evidence is unmistakable, because a false "we are sampling" abstains from a real
    finding, and abstaining loudly is recoverable while publishing a phantom is not.
    """
    if total <= 0:
        return False
    if not 0.0 < ratio <= 1.0:
        raise ValueError("ratio must be in (0, 1]")
    return distinct >= ratio * total


def support_note(distinct: int, minimum: int, unit: str = "source") -> str:
    """One human-readable clause explaining why a claim was or was not made, for the
    payload. A suppressed claim that says nothing about why reads as no finding."""
    plural = unit if distinct == 1 else unit + "s"
    verdict = "sufficient" if distinct >= minimum else "below quorum"
    return f"{distinct} independent {plural} ({verdict}; {minimum} required)"
