"""NARRATIVE erasure runner — Baike redaction-diff, published as a sealed reading.

Drives collectors/baike_redaction.py over a curated set of contested entities and
publishes readings/baike-redaction-latest.json: the share of contested entries whose
state encyclopedia (Baidu Baike) has silently forked from the open record (Chinese
Wikipedia) — sensitive terms excised, sourcing collapsed to state media, or the entry
absent entirely. This is the narrative layer of the Information Erasure Observatory.

HONESTY / FAIL-LOUD (load-bearing):
  * Live Baike acquisition is disabled pending authorized access. This runner has no
    environment flag, proxy setting, or fallback that can turn it on. It preserves
    existing evidence and publishes nothing new when no authorized observation occurred.
  * When too few entities yield a COMPARABLE read (Baike reachable AND Wikipedia
    present), we ABSTAIN: we publish rewrite_index = null with status
    "insufficient_data" and the exact reason. We never emit a fabricated 0 or a
    misleading number from a one-sided read. A null result is a reportable result.
  * The rewrite_index is computed ONLY over comparable entities.

Vantage-insensitive, stdlib-only. Judgement is lexical and auditable (see the
collector). Any future authorized acquisition must retain the kill switch and rate
ceiling gates before it can be reviewed for activation.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from collectors.baike_redaction import BaikeRedactionWatch, Entity  # noqa: E402
from collectors.baike_redaction import ENCYCLOPEDIA_FORK  # noqa: E402

try:
    from core.governance import KillSwitch  # noqa: E402
except Exception:  # pragma: no cover - governance is always present, but stay fail-soft
    KillSwitch = None

READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "baike-redaction-latest.json")
HIST = os.path.join(READINGS, "baike-redaction-history.jsonl")

# Bumped when the METHOD changes in a way a reader must see, even if the numbers
# do not move. The reading itself is now rewritten every round that looked, so the
# method text can no longer be stranded on disk; what this still buys is the
# movement record — a methodology correction that leaves the values identical is a
# real change to what the number means, and it earns its own history row rather
# than passing as another quiet republication.
METHOD_VERSION = 2


# Minimum comparable entities before we trust an index (else abstain).
MIN_COMPARABLE = 4

DISABLED_REASON = "Baike collection is disabled pending authorized access; no new observation was made"

# Curated contested-entity canon: widely documented public censorship subjects only.
# domain = DDTI hint. (lemma_id left blank; a disambiguation landing abstains per-entity.)
ENTITIES = [
    Entity(zh_title="六四事件", domain="UNREST", wiki_title="六四事件"),
    Entity(zh_title="刘晓波", domain="RIGHTS"),
    Entity(zh_title="法轮功", domain="RIGHTS"),
    # wiki_title differs from zh_title here: zh-wikipedia has no article at 天安门母亲, so the
    # control was permanently absent and this entity could never become comparable.
    Entity(zh_title="天安门母亲", wiki_title="天安门母亲运动", domain="UNREST"),
    Entity(zh_title="维吾尔族", domain="RIGHTS", wiki_title="维吾尔族"),
    Entity(zh_title="新疆再教育营", domain="RIGHTS"),
    Entity(zh_title="白纸运动", domain="UNREST"),
    Entity(zh_title="李文亮", domain="DISASTER"),
    Entity(zh_title="709大抓捕", domain="RIGHTS"),
    Entity(zh_title="盲人维权律师陈光诚", domain="RIGHTS", wiki_title="陈光诚"),
]


def main() -> None:
    """Fail closed: this public runner never initiates Baike acquisition.

    `_collect` remains an offline-fixture seam so the analytical code is testable. It is
    intentionally not reached from this executable entry point.
    """
    now = datetime.now(timezone.utc)
    kill = KillSwitch() if KillSwitch else None
    if kill is not None:
        try:
            kill.require_live()
        except RuntimeError:
            print("baike-redaction: halted by governance — no observation made")
            _write_abstain(now, reason="Baike collection halted by governance; no new observation was made",
                           comparable=0, forks=0, results=[], observed=False,
                           collector_status="halted_by_governance")
            return
    print(f"baike-redaction: disabled — {DISABLED_REASON}")
    _write_abstain(now, reason=DISABLED_REASON, comparable=0, forks=0, results=[], observed=False,
                   collector_status="disabled_no_authorized_access")


def _collect(watch) -> tuple[int, int, list[dict], bool]:
    """Run injected offline fixtures and return comparable/fork counts plus evidence rows.

    This is deliberately separate from `main`: keeping the scoring seam testable must not
    create an executable live-acquisition path.
    """

    results = []
    comparable = 0
    forks = 0
    for e in ENTITIES:
        try:
            r = watch.observe(e)
        except RuntimeError:
            # A governance halt is terminal: do not turn it into ten more attempted reads.
            raise
        except Exception as ex:  # a transport error on one entity must not sink the run
            results.append({"entity": e.zh_title, "status": f"error:{type(ex).__name__}"})
            continue
        baike = r.get("baike", {})
        wiki = r.get("wiki", {})
        baike_int = baike.get("interstitial", "")
        wiki_ok = bool(wiki.get("present"))
        is_comparable = wiki_ok and baike_int not in (
            "fetch_failed", "disambiguation", "not_found_ambiguous")
        fork = next((d for d in r.get("divergences", [])
                     if getattr(d, "kind", None) == ENCYCLOPEDIA_FORK), None)
        if is_comparable:
            comparable += 1
            if fork is not None:
                forks += 1
        results.append({
            "entity": e.zh_title,
            "status": r.get("status"),
            "comparable": is_comparable,
            "baike_present": bool(baike.get("present")),
            "baike_interstitial": baike_int,
            "wiki_present": wiki_ok,
            "fork": None if fork is None else str(getattr(fork, "detail", ""))[:240],
        })

    observed = any(not str(r.get("status") or "").startswith("error:") for r in results)
    return comparable, forks, results, observed


def _publish_collected(now, watch) -> None:
    """Publish an injected/offline collection result; used only by offline tests."""
    comparable, forks, results, observed = _collect(watch)

    if comparable < MIN_COMPARABLE:
        # Report only the observed per-entity states. An abstain reason is publication
        # evidence, so it must not speculate about the source, network, client, or cause.
        by_reason = {}
        for r in results:
            # an entity that raised is appended as a short {entity, status} row with no
            # comparability fields, so every read here has to tolerate their absence
            if r.get("comparable"):
                continue
            status = str(r.get("status") or "")
            if status.startswith("error:"):
                k = status
            elif r.get("baike_interstitial"):
                k = r["baike_interstitial"]
            elif not r.get("wiki_present"):
                k = "wiki_missing"
            else:
                k = "unknown"
            by_reason[k] = by_reason.get(k, 0) + 1
        detail = ", ".join(f"{k}×{n}" for k, n in sorted(by_reason.items()))
        reason = (f"only {comparable}/{len(ENTITIES)} entities were comparable "
                  f"({detail or 'no per-entity detail'}); insufficient comparable evidence "
                  "to calculate a narrative-erasure index")
        print(f"baike-redaction: insufficient data — {reason}; abstaining")
        _write_abstain(now, reason=reason, comparable=comparable, forks=forks, results=results,
                       observed=observed)
        return

    rewrite_index = round(100.0 * forks / comparable, 1)
    _write(now, rewrite_index=rewrite_index, status="ok", reason=None,
           comparable=comparable, forks=forks, results=results, observed=observed)
    print(f"=== Baike redaction — rewrite_index {rewrite_index} "
          f"({forks}/{comparable} contested entries forked from the open record) ===")


def _base(now, *, rewrite_index, status, reason, comparable, forks, results) -> dict:
    return {
        "generated_at": now.isoformat(),
        "method_version": METHOD_VERSION,
        "source": "Baidu Baike (subject) vs Chinese Wikipedia (open-record control)",
        "scope": ("narrative erasure — contested encyclopedia entries silently forked from the "
                  "open record: sensitive terms excised, sourcing collapsed to state media, or absent"),
        "method": ("offline fixture analysis; live Baike collection disabled pending authorized "
                   "access; lexical, auditable judgement with no authenticated revision-history access"),
        "rewrite_index": rewrite_index,
        "index_definition": "share (%) of comparable contested entries showing an encyclopedia fork vs the open record",
        "status": status,
        "reason": reason,
        "n_entities": len(ENTITIES),
        "n_comparable": comparable,
        "n_forked": forks,
        "entities": results,
    }


def _write_status_only(now, *, collector_status: str, reason: str) -> None:
    """Publish pipeline health without changing the last observation's time or value."""
    if not os.path.exists(OUT):
        print("baike-redaction: no prior reading; status recorded only in the workflow log")
        return
    try:
        with open(OUT, encoding="utf-8") as f:
            out = json.load(f)
    except (ValueError, OSError):
        print("baike-redaction: prior reading unreadable; status not published")
        return
    if not isinstance(out, dict):
        print("baike-redaction: prior reading is not an object; status not published")
        return
    out["pipeline_checked_at"] = now.isoformat()
    out["collector_status"] = collector_status
    out["collector_reason"] = reason
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("baike-redaction: operational status updated; observation timestamp unchanged")


def _write(now, *, observed: bool = True, collector_status: str | None = None, **kw) -> None:
    # No evidence was acquired: retain the prior sealed observation time and value, but
    # publish a separate operational heartbeat so readers see the current disabled/error
    # state instead of an obsolete acquisition explanation.
    if not observed:
        _write_status_only(
            now,
            collector_status=collector_status or "error_no_observation",
            reason=kw.get("reason") or "No observation was acquired",
        )
        return
    out = _base(now, **kw)
    out["pipeline_checked_at"] = out["generated_at"]
    out["collector_status"] = "observed"
    out["collector_reason"] = None
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
        except (ValueError, OSError):
            prev = {}
    # method_version is part of the comparison so a methodology correction reaches
    # the published file even when every value is identical — otherwise the site
    # keeps asserting a method, and an abstain reason, that no longer apply.
    changed = (prev.get("rewrite_index") != out["rewrite_index"]
               or prev.get("status") != out["status"]
               or prev.get("n_comparable") != out["n_comparable"]
               or prev.get("method_version") != METHOD_VERSION)

    # "When did we last look" and "when did the answer last move" are different
    # questions, and a reader has to be able to tell them apart. Write-if-changed
    # answers only the second, so a finding that holds still — a stable rewrite
    # index, or an abstain that keeps abstaining for the same reason — stopped
    # refreshing generated_at, and the observatory ended up labelling its own
    # healthy signal stale. A state encyclopedia that keeps its entries forked and
    # a collector that died are not the same claim. So every round that actually
    # went and looked publishes its own observation time, and last_changed_at
    # carries the movement. The history file stays gated on change, so the
    # movement record never fills with heartbeats.
    out["last_changed_at"] = (
        out["generated_at"] if (changed or not prev)
        else (prev.get("last_changed_at") or prev.get("generated_at")))

    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    if changed or not prev:
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps({"generated_at": out["generated_at"], "rewrite_index": out["rewrite_index"],
                                "status": out["status"], "n_comparable": out["n_comparable"],
                                "n_forked": out["n_forked"]}, ensure_ascii=False) + "\n")
    else:
        print(f"baike-redaction: unchanged since {out['last_changed_at']} "
              f"(status={out['status']}, rewrite_index={out['rewrite_index']}) — "
              f"republished with this round's observation time, history untouched")


def _write_abstain(now, *, reason, comparable, forks, results, observed: bool = True,
                   collector_status: str | None = None) -> None:
    # An abstain is this collector's reading, not the absence of one: rewrite_index
    # stays null and the reason travels with it. So a repeated abstain gets the same
    # heartbeat as a repeated number — the null is republished, never a fabricated value.
    _write(now, rewrite_index=None, status="insufficient_data", reason=reason,
           comparable=comparable, forks=forks, results=results, observed=observed,
           collector_status=collector_status)


if __name__ == "__main__":
    main()
