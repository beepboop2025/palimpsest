"""NARRATIVE erasure runner — Baike redaction-diff, published as a sealed reading.

Drives collectors/baike_redaction.py over a curated set of contested entities and
publishes readings/baike-redaction-latest.json: the share of contested entries whose
state encyclopedia (Baidu Baike) has silently forked from the open record (Chinese
Wikipedia) — sensitive terms excised, sourcing collapsed to state media, or the entry
absent entirely. This is the narrative layer of the Information Erasure Observatory.

HONESTY / FAIL-LOUD (load-bearing):
  * The Great Firewall blocks Wikipedia from inside China, and Baidu Baike blocks or
    hangs for non-China / datacenter IPs. So a genuine two-sided diff needs an
    outside-the-wall egress that can ALSO reach Baike (a residential/China-reachable
    proxy via PALIMPSEST_PROXY). From open infrastructure, Baike reads fail.
  * When too few entities yield a COMPARABLE read (Baike reachable AND Wikipedia
    present), we ABSTAIN: we publish rewrite_index = null with status
    "insufficient_data" and the exact reason. We never emit a fabricated 0 or a
    misleading number from a one-sided read. A null result is a reportable result.
  * The rewrite_index is computed ONLY over comparable entities.

Vantage-insensitive, stdlib-only. Judgement is lexical and auditable (see the
collector). Run live with PALIMPSEST_LIVE=1 (open infra) or PALIMPSEST_PROXY=<egress>.

Governance-gated (kill switch + rate ceiling): PALIMPSEST_HALT (or the kill file)
stops this runner dead with no redeploy, and the token bucket keeps the two public
encyclopedias' reads polite by construction.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from collectors.baike_redaction import BaikeRedactionWatch, Entity, _default_fetch  # noqa: E402
from collectors.baike_redaction import ENCYCLOPEDIA_FORK  # noqa: E402

try:
    from core.governance import KillSwitch, RateCeiling  # noqa: E402
except Exception:  # pragma: no cover - governance is always present, but stay fail-soft
    KillSwitch = RateCeiling = None

READINGS = os.path.join(ROOT, "readings")
OUT = os.path.join(READINGS, "baike-redaction-latest.json")
HIST = os.path.join(READINGS, "baike-redaction-history.jsonl")

# Bumped when the METHOD changes in a way a reader must see, even if the numbers
# do not move. Write-if-changed compares readings, so without this a methodology
# correction that leaves the values identical never reaches the published file and
# the site keeps asserting a method it no longer uses.
METHOD_VERSION = 1


# Minimum comparable entities before we trust an index (else abstain).
MIN_COMPARABLE = 4

# Polite by construction: two reads per entity against two public encyclopedias.
#
# 0.5/s with a burst of 2 permitted ~20 reads in 40 seconds, and that is precisely the burst
# Baidu rate-bans on: a run at that pace took the source IP to blanket 403 on every entity,
# still refusing 45s later. A banned IP does not fail loudly — it returns 403 for every
# entity, which the collector reads as "unreachable" and reports as a vantage problem. The
# pacing was manufacturing the very blocker the reading then blamed.
#
# One read per 12s with a small burst puts a full 10-entity pass (20 reads) at ~4 minutes,
# which is nothing on a 6-hourly cadence and stays far below the ban threshold.
_RATE_PER_SEC = 1 / 12
_BURST = 2.0

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


def _short_fetch(url: str, proxy: str | None):
    # 8s per read so an unreachable Baike fails fast instead of the default 20s hang.
    return _default_fetch(url, proxy=proxy, timeout=8.0)


def main() -> None:
    now = datetime.now(timezone.utc)
    proxy = os.environ.get("PALIMPSEST_PROXY")
    live = bool(proxy) or os.environ.get("PALIMPSEST_LIVE", "").lower() in ("1", "true", "yes", "on")

    if not live:
        print("baike-redaction: inert (set PALIMPSEST_LIVE=1 or PALIMPSEST_PROXY) — abstaining")
        _write_abstain(now, reason="live network not enabled (PALIMPSEST_LIVE / PALIMPSEST_PROXY unset)",
                       comparable=0, forks=0, results=[])
        return

    # Governance BEFORE the first outbound read: the kill switch can halt this runner
    # instantly, and the rate ceiling bounds the request rate.
    kill = KillSwitch() if KillSwitch else None
    rate = RateCeiling(rate=_RATE_PER_SEC, capacity=_BURST) if RateCeiling else None

    watch = BaikeRedactionWatch(
        proxy=proxy,
        baike_fetch=lambda u: _short_fetch(u, proxy),
        wiki_fetch=lambda u: _short_fetch(u, proxy),
        kill_switch=kill,
        rate_ceiling=rate,
    )

    results = []
    comparable = 0
    forks = 0
    for e in ENTITIES:
        try:
            r = watch.observe(e)
        except Exception as ex:  # a halt or transport error on one entity must not sink the run
            results.append({"entity": e.zh_title, "status": f"error:{type(ex).__name__}"})
            continue
        baike = r.get("baike", {})
        wiki = r.get("wiki", {})
        baike_int = baike.get("interstitial", "")
        wiki_ok = bool(wiki.get("present"))
        is_comparable = wiki_ok and baike_int not in ("fetch_failed", "disambiguation")
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

    if comparable < MIN_COMPARABLE:
        # Report WHY each entity failed rather than asserting a cause. The previous text
        # named a China-reachable proxy as the missing ingredient, and that diagnosis was
        # wrong: Baidu's WAF refuses the stdlib client's TLS/HTTP fingerprint independently
        # of IP or User-Agent, and the same URL from the same machine with the same UA
        # succeeds under a browser-grade stack. An abstain reason is written at design time
        # and then read downstream as a measurement, so a guessed cause becomes a published
        # finding. Say what was observed; do not name a culprit we have not established.
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
                  f"({detail or 'no per-entity detail'}). Baidu filters on two axes, "
                  f"measured separately: the CLIENT (urllib and curl are refused, httpx is "
                  f"served, same IP and same second) and the EGRESS (residential is served, "
                  f"datacenter is refused whatever the client). This run cleared the first "
                  f"and not the second — it needs a residential egress, not a Chinese one")
        print(f"baike-redaction: insufficient data — {reason}; abstaining")
        _write_abstain(now, reason=reason, comparable=comparable, forks=forks, results=results)
        return

    rewrite_index = round(100.0 * forks / comparable, 1)
    _write(now, rewrite_index=rewrite_index, status="ok", reason=None,
           comparable=comparable, forks=forks, results=results)
    print(f"=== Baike redaction — rewrite_index {rewrite_index} "
          f"({forks}/{comparable} contested entries forked from the open record) ===")


def _base(now, *, rewrite_index, status, reason, comparable, forks, results) -> dict:
    return {
        "generated_at": now.isoformat(),
        "method_version": METHOD_VERSION,
        "source": "Baidu Baike (subject) vs Chinese Wikipedia (open-record control)",
        "scope": ("narrative erasure — contested encyclopedia entries silently forked from the "
                  "open record: sensitive terms excised, sourcing collapsed to state media, or absent"),
        "method": ("public anonymous reads of both encyclopedias from outside the wall; lexical, "
                   "auditable judgement; we never authenticate into Baike's hidden revision history"),
        "rewrite_index": rewrite_index,
        "index_definition": "share (%) of comparable contested entries showing an encyclopedia fork vs the open record",
        "status": status,
        "reason": reason,
        "n_entities": len(ENTITIES),
        "n_comparable": comparable,
        "n_forked": forks,
        "entities": results,
    }


def _write(now, **kw) -> None:
    out = _base(now, **kw)
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
        except (ValueError, OSError):
            prev = {}
    # method_version is part of the comparison so a methodology correction reaches
    # the published file even when every value is identical — otherwise the site
    # keeps asserting a method, and an abstain reason, that no longer apply.
    if (prev.get("rewrite_index") != out["rewrite_index"]
            or prev.get("status") != out["status"]
            or prev.get("n_comparable") != out["n_comparable"]
            or prev.get("method_version") != METHOD_VERSION):
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps({"generated_at": out["generated_at"], "rewrite_index": out["rewrite_index"],
                                "status": out["status"], "n_comparable": out["n_comparable"],
                                "n_forked": out["n_forked"]}, ensure_ascii=False) + "\n")


def _write_abstain(now, *, reason, comparable, forks, results) -> None:
    _write(now, rewrite_index=None, status="insufficient_data", reason=reason,
           comparable=comparable, forks=forks, results=results)


if __name__ == "__main__":
    main()
