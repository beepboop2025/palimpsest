"""Inside View runner — command DNS measurements from inside mainland China via
Globalping's volunteer probes and publish readings/inside-view-latest.json.

Vantage-insensitive on our side (the probing happens on Globalping's probes, not
ours), key-less, standard-library only. Follows the house pattern:
collect -> control gate -> honesty guard / abstain -> write-if-changed -> history.

The control gate is the part worth reading. Unlike the other pulls, this one can
fail in a way that looks like success: if the classifier breaks, every domain
reads "clean" and the observatory would report calm. So a round only produces a
block rate when a benign control came back clean AND a censored domain came back
forged in the same round.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from collectors.inside_view import (PANEL, RateLimited, _role, control_state,
                                    observe_panel, regional_divergence)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "inside-view-latest.json")
HIST = os.path.join(READINGS, "inside-view-history.jsonl")

# Bumped whenever the METHOD changes in a way a reader must see, even if the
# numbers do not move. Write-if-changed compares readings, so without this a
# methodology correction that leaves block_rate at 1.0 would never reach the
# published file — the site would keep advertising the superseded method and the
# superseded vantage list. v2: in-China probes pinned to datacenter ASNs, and a
# single-ASN result no longer counts as blocked.
METHOD_VERSION = 2

# Globalping allows 250 unauthenticated probe-credits per rolling hour. A full
# panel round costs len(PANEL) * (CN_PROBES + CONTROL_PROBES) = 49, so the
# 6-hourly cadence spends 196/day against a 250/hour ceiling and no pre-flight
# budget check is needed. If the budget is exhausted anyway, RateLimited is
# raised mid-panel and the round abstains whole rather than publishing the
# domains that happened to be measured before the wall.


def _band(rate: float) -> str:
    if rate >= 0.9:
        return "the panel is comprehensively blocked from inside"
    if rate >= 0.6:
        return "most of the panel is blocked from inside"
    if rate >= 0.3:
        return "part of the panel is blocked from inside"
    if rate > 0:
        return "a minority of the panel is blocked from inside"
    return "no panel domain read as forged from inside"


def main() -> None:
    now = datetime.now(timezone.utc)

    if not os.getenv("PALIMPSEST_LIVE"):
        print("inside-view: inert (set PALIMPSEST_LIVE=1) — abstaining")
        return

    try:
        observations = observe_panel()
    except RateLimited as e:
        # Not a measurement. A rate-limited round produces no probe results,
        # which would otherwise look exactly like every probe going silent.
        print(f"inside-view: Globalping rate limit hit ({e}) — abstaining, "
              "this is a budget failure and not an observation")
        return

    control = control_state(observations)

    # A DEGRADED round means the classifier itself is untrustworthy. Publishing
    # anything derived from it would be publishing a guess as a measurement.
    if control["state"] == "DEGRADED":
        print(f"inside-view: DEGRADED — {control['why']} — abstaining, not publishing")
        return

    # The headline rate is over the measurement set only. Boundary domains are an
    # open experiment and are reported beside it, so adding one cannot move a
    # number that has been tracked across previous readings.
    censored = [o for o in observations if _role(o) == "measurement"]
    answered = [o for o in censored
                if (o["n_forged"] or 0) + (o["n_clean"] or 0) > 0]

    if not answered:
        print("inside-view: no censored domain produced a classifiable answer "
              "from any CN vantage — abstaining")
        return

    blocked = [o for o in answered if (o["n_forged"] or 0) > 0]
    block_rate = round(len(blocked) / len(answered), 3)

    # BLIND is published, deliberately. "We could not see injection" is a real
    # coverage statement and belongs in the record; it is simply not the same
    # claim as "there was no censorship", so no block rate is derived from it.
    if control["state"] == "BLIND":
        block_rate = None
        reading = ("this vantage set could not see injection on any panel domain "
                   "this round — a coverage gap, not an all-clear")
    else:
        reading = _band(block_rate)

    # Regional divergence is an enrichment layer, not a precondition. Until it
    # is implemented the signal still publishes, and says plainly that the layer
    # is pending rather than silently omitting it.
    #
    # It runs over the boundary set as well as the measurement set, because the
    # measurement set is saturated by construction: five permanently-blocked
    # domains can only ever return UNIFORM_BLOCKED, so the layer had nothing to
    # find. Provincial filtering, the thing REGIONAL exists to name, can only
    # appear on a domain whose treatment actually varies.
    graded = [o for o in observations
              if _role(o) in ("measurement", "boundary")
              and (o["n_forged"] or 0) + (o["n_clean"] or 0) > 0]
    regional, regional_status = [], "reporting"
    for o in graded:
        try:
            regional.append({"domain": o["domain"], "role": _role(o),
                             **regional_divergence(o)})
        except NotImplementedError:
            regional_status = ("armed, not yet reporting — regional_divergence() "
                               "is unimplemented in collectors/inside_view.py")
            regional = []
            break

    # Boundary domains report beside the headline, never inside it. A domain that
    # the control arm could not pin down is named as such rather than counted.
    boundary = [{
        "domain": o["domain"],
        "n_forged": o["n_forged"],
        "n_clean": o["n_clean"],
        "geo_variable": o.get("geo_variable", False),
        "state": ("undetermined, the control arm disagreed with itself"
                  if o.get("geo_variable") else
                  "blocked from every answering vantage" if o["n_clean"] == 0 and o["n_forged"]
                  else "clean from every answering vantage" if o["n_forged"] == 0 and o["n_clean"]
                  else "split across vantages" if o["n_forged"] and o["n_clean"]
                  else "no classifiable answer this round"),
    } for o in observations if _role(o) == "boundary"]

    out = {
        "generated_at": now.isoformat(),
        "method_version": METHOD_VERSION,
        "source": "Globalping (api.globalping.io) volunteer probes inside mainland China",
        "scope": ("DNS answers received by probes INSIDE China for a fixed panel of "
                  "censored and benign domains, classified against a same-round "
                  "control arm outside China"),
        "method": ("a CN answer is forged when it shares no address with the control "
                   "arm's answer set; no forged-IP list is hardcoded, because the "
                   "injector pool rotates"),
        "vantage_limitation": (
            "in-China probes are pinned to datacenter ASNs (Tencent, Alibaba, Huawei "
            "Cloud). Globalping's CN pool also contains household connections, and a "
            "volunteer hosting a probe did not thereby consent to have their home "
            "connection emit DNS queries for censored domains. The cost is real: this "
            "measures filtering as experienced on Chinese CLOUD networks, which is not "
            "necessarily what a household sees"),
        "attribution_caveat": (
            "a forged answer proves on-path DNS interference, not necessarily national "
            "filtering; Tencent and Alibaba each run resolver interception, so a domain "
            "forged within a single ASN is reported as SINGLE_OPERATOR rather than as "
            "blocked"),
        "control": control,
        "block_rate": block_rate,
        "reading": reading,
        "n_censored_answered": len(answered),
        "n_censored_blocked": len(blocked),
        "panel_size": len(PANEL),
        "regional_status": regional_status,
        "regional": regional,
        "boundary": boundary,
        "boundary_note": (
            "domains whose treatment is reported to vary, measured beside the "
            "headline panel and deliberately excluded from the block rate. They "
            "are where a REGIONAL verdict can appear at all; the measurement set "
            "is long-blocked by design and saturates at uniform"),
        "domains": observations,
    }

    prev = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                prev = json.load(f)
        except (OSError, json.JSONDecodeError):
            prev = {}

    changed = (prev.get("block_rate") != out["block_rate"]
               or (prev.get("control") or {}).get("state") != control["state"]
               or prev.get("n_censored_blocked") != out["n_censored_blocked"]
               # A method correction must reach the published file even when the
               # numbers are identical, or the site keeps advertising a method
               # and a vantage list that no longer apply.
               or prev.get("method_version") != METHOD_VERSION)

    # "When did we last look" and "when did the answer last move" are different
    # questions, and a reader has to be able to tell them apart. Write-if-changed
    # answers only the second, so a finding that holds still — which is exactly
    # what sustained blocking looks like — stopped refreshing generated_at, and
    # the observatory ended up labelling its own healthy signal stale. Silence
    # from a censor and silence from a dead collector are not the same claim.
    # So every round that survives the control gate publishes its own
    # observation time, and last_changed_at carries the movement. The history
    # file stays gated on change, so the movement record never fills with
    # heartbeats and no false sense of movement is manufactured.
    out["last_changed_at"] = (
        out["generated_at"] if (changed or not prev)
        else (prev.get("last_changed_at") or prev.get("generated_at")))

    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    if changed or not prev:
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "generated_at": out["generated_at"],
                "control_state": control["state"],
                "block_rate": block_rate,
                "n_censored_answered": len(answered),
                "n_censored_blocked": len(blocked),
            }, ensure_ascii=False) + "\n")
        print(f"inside-view: {control['state']} — block_rate={block_rate} "
              f"({len(blocked)}/{len(answered)} censored domains forged)")
    else:
        print(f"inside-view: unchanged since {out['last_changed_at']} "
              f"({control['state']}, block_rate={block_rate}) — "
              f"republished with this round's observation time, history untouched")


if __name__ == "__main__":
    main()
