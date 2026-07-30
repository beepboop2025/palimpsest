"""Inside View — what a user *inside* China actually receives, measured from
in-China vantages we do not own.

Every other Palimpsest network signal looks at the wall from outside and infers
inward. Bleedthrough reads the injection that leaks out; OONI ingests what other
people's probes already uploaded. This one is different: it commands live
measurements **from inside mainland China**, on consented volunteer probes run
by the Globalping network (jsDelivr), and reports what those probes received.

Why that matters, concretely: injection is not uniform. A Ningbo probe and a
Guangzhou probe can be handed different forged addresses for the same domain in
the same minute, and provincial filtering has been documented repeatedly. From
outside the wall that asymmetry is invisible. From inside it is the measurement.

METHOD (and its honesty machinery)

For each domain we run two measurements in the same round:

  * the CN arm  — N probes inside mainland China
  * the control arm — probes in countries that do not filter DNS

A CN answer is FORGED when it shares no address with the control arm's answer
set. We never hardcode a forged-IP list: the GFW draws from a large, rotating
pool (a single target yielded ten distinct forged addresses in twenty probes),
so any fixed table is stale on arrival. Truth is established per-round by the
control arm, and forgery is defined relative to it.

The panel deliberately carries BOTH censored and benign domains. That pairing is
the point:

  * a benign domain must come back clean from inside China
  * a censored domain is expected to come back forged

If a benign domain reads as forged, our classifier is wrong (control arm broken,
CDN geo-split mistaken for injection) and the round is not trustworthy. If every
censored domain reads clean, we may be blind rather than looking at calm. Either
way the round abstains instead of publishing. A censorship observatory cannot
report "we saw nothing" unless it can also say "and we would have seen it."

VANTAGE: this collector is vantage-insensitive on OUR side. The probing happens
on Globalping's probes, not on our host, so it runs correctly from a GitHub
runner and never touches the box's egress.

Standard-library only (urllib + json), no key, no dependencies in CI.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

API = "https://api.globalping.io/v1/measurements"
USER_AGENT = ("palimpsest.info observatory (censorship research; "
              "contact desk@palimpsest.info)")

# Unauthenticated Globalping allows 250 probe-credits/hour. One probe = one
# credit, so a round costs len(PANEL) * (CN_PROBES + CONTROL_PROBES).
CN_PROBES = 5
CONTROL_PROBES = 2
CONTROL_COUNTRIES = ["DE", "NL"]

# The panel is half the method. `censored=True` domains are the measurement;
# `censored=False` domains are the negative control that proves the classifier
# is not simply calling everything forged.
#
# Choosing negative controls is subtler than it looks, and getting it wrong is
# the first bug this collector produced. A control must be GLOBALLY CONSISTENT,
# not merely uncensored: we classify by set-intersection against a control arm
# outside China, so any domain that legitimately resolves differently by region
# reads as forged. www.baidu.com was the original choice — obviously uncensored
# inside China, and exactly wrong, because China-hosted CDN domains are the most
# geo-split of all. It shares no address with a German control arm and tripped
# the gate on the first live round.
#
# The controls below resolve to fixed anycast constants (1.1.1.1, 8.8.8.8) that
# are the same address everywhere on earth, so they cannot geo-split.
PANEL = [
    {"domain": "torproject.org", "censored": True, "ddti": "CIRCUMVENTION"},
    {"domain": "rsf.org", "censored": True, "ddti": "INFORMATION"},
    {"domain": "www.hrw.org", "censored": True, "ddti": "INFORMATION"},
    {"domain": "zh.wikipedia.org", "censored": True, "ddti": "INFORMATION"},
    {"domain": "wikileaks.org", "censored": True, "ddti": "INFORMATION"},
    # Negative controls: globally constant answers, must read clean from inside.
    {"domain": "one.one.one.one", "censored": False, "ddti": None},
    {"domain": "dns.google", "censored": False, "ddti": None},
]


def _request(url: str, body: dict | None = None, timeout: float = 30.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class RateLimited(Exception):
    """Globalping returned 429. Raised rather than swallowed: a rate-limited
    round yields no probe results, which is indistinguishable from every probe
    staying silent, and silence is a measurement here. The caller must be able
    to tell 'we did not ask' from 'we asked and heard nothing'."""


def _create(domain: str, locations: list, limit: int) -> str | None:
    """Start one DNS measurement. Returns the measurement id, or None if
    Globalping refused for a reason that is not rate limiting (no matching
    probes, malformed panel). Raises RateLimited on 429."""
    body = {
        "type": "dns",
        "target": domain,
        "limit": limit,
        "locations": locations,
        "measurementOptions": {"query": {"type": "A"}, "protocol": "UDP", "port": 53},
    }
    try:
        return _request(API, body).get("id")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(f"429 on {domain}") from e
        log.warning("globalping refused %s (%s): %s", domain, e.code, e.reason)
    except Exception as e:                                    # noqa: BLE001
        log.warning("globalping create failed for %s: %s", domain, e)
    return None


def _collect(mid: str, *, poll: float = 3.0, tries: int = 20) -> list:
    """Poll one measurement to completion and return its per-probe results.
    An unfinished measurement returns [] so the caller treats it as no data
    rather than as a clean answer."""
    for _ in range(tries):
        try:
            doc = _request(f"{API}/{mid}")
        except Exception as e:                                # noqa: BLE001
            log.warning("globalping poll failed for %s: %s", mid, e)
            return []
        if doc.get("status") != "in-progress":
            return doc.get("results") or []
        time.sleep(poll)
    log.warning("measurement %s did not finish", mid)
    return []


def _answers(result: dict) -> set:
    """Extract the A-record addresses one probe received."""
    body = (result or {}).get("result") or {}
    out = set()
    for ans in body.get("answers") or []:
        if ans.get("type") == "A" and ans.get("value"):
            out.add(ans["value"])
    return out


def observe_domain(entry: dict, *, create=_create, collect=_collect) -> dict:
    """Measure one domain from inside China against a control arm.

    `create` and `collect` are injected so the whole classification path is
    testable offline with no network.
    """
    domain = entry["domain"]

    ctl_id = create(domain, [{"country": c} for c in CONTROL_COUNTRIES], CONTROL_PROBES)
    cn_id = create(domain, [{"country": "CN"}], CN_PROBES)

    control = [r for r in (collect(ctl_id) if ctl_id else [])]
    inside = [r for r in (collect(cn_id) if cn_id else [])]

    truth = set()
    for r in control:
        truth |= _answers(r)

    vantages, forged_n, clean_n, silent_n = [], 0, 0, 0
    for r in inside:
        probe = r.get("probe") or {}
        got = _answers(r)
        if not got:
            state = "silent"
            silent_n += 1
        elif truth and not (got & truth):
            state = "forged"
            forged_n += 1
        elif truth:
            state = "clean"
            clean_n += 1
        else:
            # No control truth: we cannot classify, and guessing here is exactly
            # the failure this collector exists to avoid.
            state = "unclassified"
        vantages.append({
            "city": probe.get("city"),
            "asn": probe.get("asn"),
            "network": probe.get("network"),
            "state": state,
            "answers": sorted(got),
        })

    answered = forged_n + clean_n
    return {
        "domain": domain,
        "expected_censored": entry["censored"],
        "ddti": entry.get("ddti"),
        "control_answers": sorted(truth),
        "control_probes": len(control),
        "n_vantages": len(inside),
        "n_forged": forged_n,
        "n_clean": clean_n,
        "n_silent": silent_n,
        "forged_fraction": round(forged_n / answered, 3) if answered else None,
        "vantages": vantages,
    }


def observe_panel(panel: list = None, *, create=_create, collect=_collect) -> list:
    return [observe_domain(e, create=create, collect=collect)
            for e in (panel if panel is not None else PANEL)]


# ── control gate ──────────────────────────────────────────────────────────────

def control_state(observations: list) -> dict:
    """Decide whether this round is trustworthy at all, BEFORE any reading is
    derived from it.

    Three states:
      SIGHTED  — at least one censored domain read forged, and every benign
                 control read clean. We can see injection and we are not
                 hallucinating it.
      BLIND    — controls behaved, but no censored domain read forged. Either
                 the panel has been de-listed or this vantage set cannot see
                 injection. Not the same as "no censorship", so we do not say it.
      DEGRADED — a benign control read forged, or too little answered to judge.
                 The classifier is untrustworthy this round.
    """
    censored = [o for o in observations if o["expected_censored"]]
    benign = [o for o in observations if not o["expected_censored"]]

    benign_forged = [o["domain"] for o in benign if (o["n_forged"] or 0) > 0]
    usable = [o for o in observations if (o["n_forged"] or 0) + (o["n_clean"] or 0) > 0]

    if benign_forged:
        return {"state": "DEGRADED",
                "why": f"benign control(s) read as forged: {', '.join(benign_forged)}; "
                       "the classifier or the control arm is wrong this round"}
    if not usable:
        return {"state": "DEGRADED",
                "why": "no domain produced a classifiable answer from any CN vantage"}

    sighted = [o["domain"] for o in censored if (o["n_forged"] or 0) > 0]
    if sighted:
        return {"state": "SIGHTED",
                "why": f"injection observed on {len(sighted)} censored domain(s): "
                       f"{', '.join(sighted)}"}
    return {"state": "BLIND",
            "why": "controls behaved but no censored domain read forged; this vantage "
                   "set cannot currently see injection (not evidence of no censorship)"}


# ── regional reading ──────────────────────────────────────────────────────────

def regional_divergence(observation: dict) -> dict:
    """Describe how the SAME domain was treated differently across in-China
    vantages, and say what that disagreement means.

    This is the part of the signal that only an inside vantage can produce, and
    it is a genuine methodological choice rather than a mechanical one. Given a
    single domain measured from several Chinese cities and ASNs, the vantages
    can disagree: some forged, some clean, some silent.

    TODO(mrinal): decide what disagreement MEANS and return it.

    `observation` is one dict from observe_domain(), so you have:
        observation["vantages"]  -> [{city, asn, network, state, answers}, ...]
        observation["n_forged"], ["n_clean"], ["n_silent"], ["forged_fraction"]

    Return a dict with at least:
        {"verdict": <str>, "detail": <str>}

    The trade-off to weigh, and there is no default that is right for every
    observatory:

      - "any forged means blocked" is the most sensitive reading. It catches
        regional filtering that a majority vote would erase. It also lets one
        flaky probe, or one CDN geo-split mistaken for injection, promote a
        domain to blocked.

      - "majority forged means blocked" is robust to a single bad probe, but it
        actively destroys the regional signal. Henan-style provincial filtering
        looks exactly like a minority of vantages disagreeing, and a majority
        vote reports that as "not blocked".

      - treating the DISAGREEMENT ITSELF as the finding keeps both, at the cost
        of a signal that is harder to reduce to one number for the board.

    Worth knowing: forged answers differ per vantage even when every vantage is
    blocked (each injector draws from a rotating pool), so "the answers
    disagree" is NOT the same as "the vantages disagree about whether it is
    blocked". We compare `state`, never `answers`.

    THE CHOICE MADE HERE: disagreement is its own verdict, not a vote.

    Both vote-based readings discard the only thing an in-China vantage buys us.
    "Any forged means blocked" lets a single flaky probe promote a domain;
    "majority forged means blocked" reports provincial filtering as no filtering,
    which is precisely backwards for an observatory whose subject is regional
    variation in control. So REGIONAL is named rather than resolved.

    The flaky-probe risk that "any forged" carries is handled two ways instead of
    by a majority rule: a verdict needs at least MIN_VANTAGES answering probes,
    and the split is published, so 1-of-5 reads as visibly weak evidence rather
    than silently becoming "blocked".
    """
    MIN_VANTAGES = 2

    forged = [v for v in observation["vantages"] if v["state"] == "forged"]
    clean = [v for v in observation["vantages"] if v["state"] == "clean"]
    answering = len(forged) + len(clean)

    def _where(vs):
        return ", ".join(sorted({f"{v['city'] or '?'} (AS{v['asn']})" for v in vs}))

    if answering < MIN_VANTAGES:
        return {"verdict": "INSUFFICIENT",
                "detail": f"only {answering} vantage(s) answered; regional variation "
                          f"cannot be judged below {MIN_VANTAGES}"}
    if forged and clean:
        return {"verdict": "REGIONAL",
                "detail": f"blocked from {len(forged)}/{answering} vantages "
                          f"[{_where(forged)}] but resolving correctly from "
                          f"[{_where(clean)}] — filtering is not uniform"}
    if forged:
        return {"verdict": "UNIFORM_BLOCKED",
                "detail": f"forged from all {answering} answering vantages [{_where(forged)}]"}
    return {"verdict": "UNIFORM_CLEAN",
            "detail": f"resolved correctly from all {answering} answering vantages "
                      f"[{_where(clean)}]"}
