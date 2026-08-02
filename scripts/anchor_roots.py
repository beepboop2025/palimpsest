"""Anchor the sealed chains OUTSIDE our own infrastructure.

A hash chain proves internal consistency, but a chain the operator serves is
only tamper-evident to someone who already holds an old copy. This script
closes that gap by depositing each new Merkle root with parties we do not
control, so rewriting history would require defeating them too:

  1. Internet Archive — a Wayback Machine snapshot of the published chain
     files. A dated, third-party copy of the exact bytes, held by a
     library. Pure stdlib.
  2. OpenTimestamps — the roots are stamped into Bitcoin via the standard
     `ots` client when it is installed (CI installs it; local runs skip
     loudly). The resulting .ots files are committed and verify with the
     standard client against the Bitcoin blockchain, not against us.

Idempotent by the house convention: if none of the three roots moved since the
last anchor record, nothing is anchored and nothing grows. All three are
compared, because a root that is published but never re-anchored goes stale
against the chain it claims to fingerprint.

A broken chain is never anchored, because anchoring a bad root would launder
it, but the three chains do not fail together:

  * eval-registry and erasure-ledger are the observatory's own attestations,
    written by this workflow. A break in either aborts with exit 1.
  * readings-ledger sweeps 31 files written by 30 other workflows, so its
    corruption surface is somebody else's truncated JSON far more often than
    it is our tampering. A break there is printed loudly and its root is
    WITHHELD from the record, while the other two chains still reach Wayback
    and Bitcoin. Fail-closed here would let one bad line in a file we do not
    write keep the established chains off the blockchain, and, since the
    anchor step runs before the commit step, out of the repository entirely.

Every attempt (success or failure) is recorded in readings/anchors.jsonl and
summarized in readings/anchors-latest.json for the site. An anchoring failure
is a visible gap in the log, never a fabricated success.

    python3 scripts/anchor_roots.py            # anchor if roots moved
    python3 scripts/anchor_roots.py --dry-run  # show what would be anchored
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import eval_registry as reg  # noqa: E402
from core import sealed_ledger as led  # noqa: E402

READINGS = os.path.join(ROOT, "readings")
REGISTRY = os.path.join(READINGS, "eval-registry.jsonl")
ERASURE = os.path.join(READINGS, "erasure-ledger.jsonl")
# Every published reading, sealed by scripts/seal_readings.py. Anchored here so
# the readings record reaches Bitcoin on the same footing as the other two
# chains; a seal nobody anchors is only a promise we made to ourselves.
READINGS_LEDGER = os.path.join(READINGS, "readings-ledger.jsonl")
ANCHOR_LOG = os.path.join(READINGS, "anchors.jsonl")
ANCHOR_LATEST = os.path.join(READINGS, "anchors-latest.json")
ANCHOR_DIR = os.path.join(READINGS, "anchors")

SITE = os.environ.get("PALIMPSEST_SITE", "https://palimpsest.info")
WAYBACK_TARGETS = (
    f"{SITE}/readings/eval-registry.jsonl",
    f"{SITE}/readings/erasure-ledger.jsonl",
    f"{SITE}/readings/readings-ledger.jsonl",
)
UA = "palimpsest-anchor/1.0 (+https://palimpsest.info)"


def current_roots() -> dict:
    """Verify the three chains and return the roots we are entitled to anchor.

    Our own two attestations fail closed; the readings sweep fails open with
    its root withheld. See the module docstring for why the coupling is
    deliberately asymmetric.
    """
    reg_entries = reg.read_ledger(REGISTRY)
    led_entries = led.read_ledger(ERASURE)
    reg_ok, reg_problems = reg.verify(reg_entries)
    led_ok, led_problems = led.verify(led_entries)
    if not (reg_ok and led_ok):
        for p in reg_problems + led_problems:
            print(f"BROKEN: {p}")
        raise SystemExit(1)
    roots = {
        "registry_root": led.merkle_root(reg_entries),
        "registry_head": reg_entries[-1]["entry_hash"] if reg_entries else led.GENESIS_PREV,
        "registry_entries": len(reg_entries),
        "erasure_root": led.merkle_root(led_entries),
        "erasure_head": led_entries[-1]["entry_hash"] if led_entries else led.GENESIS_PREV,
        "erasure_entries": len(led_entries),
    }
    try:
        rdg_entries = led.read_ledger(READINGS_LEDGER)
        rdg_ok, rdg_problems = led.verify(rdg_entries)
    except (OSError, ValueError) as exc:  # a half-written line is unparseable
        rdg_entries, rdg_ok, rdg_problems = [], False, [f"unreadable: {exc}"]
    if rdg_ok:
        roots["readings_root"] = led.merkle_root(rdg_entries)
        roots["readings_head"] = (rdg_entries[-1]["entry_hash"] if rdg_entries
                                  else led.GENESIS_PREV)
        roots["readings_entries"] = len(rdg_entries)
    else:
        for p in rdg_problems:
            print(f"BROKEN readings chain: {p}")
        roots["readings_root"] = None
        roots["readings_problems"] = rdg_problems
    return roots


def last_anchor(path: str = ANCHOR_LOG) -> dict | None:
    if not os.path.exists(path):
        return None
    last = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                last = json.loads(line)
    return last


def wayback_save(url: str, opener=urllib.request.urlopen, timeout: int = 90) -> dict:
    """Ask the Internet Archive to snapshot one URL. Returns the snapshot
    reference on success, or the failure reason — never raises."""
    req = urllib.request.Request(f"https://web.archive.org/save/{url}",
                                 headers={"User-Agent": UA})
    try:
        with opener(req, timeout=timeout) as resp:
            return {"target": url, "ok": True, "snapshot": resp.geturl(),
                    "http": getattr(resp, "status", None)}
    except Exception as exc:  # noqa: BLE001 — anchoring must degrade loudly, not crash
        return {"target": url, "ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def ots_stamp(roots: dict, ts: str, run=subprocess.run) -> dict:
    """Write the roots to a canonical text file and stamp it into Bitcoin with
    the standard OpenTimestamps client, if installed. The .ots proof commits to
    the repo and verifies with `ots verify` against Bitcoin, not against us."""
    if shutil.which("ots") is None:
        return {"ok": False, "skipped": True,
                "reason": "ots client not installed (pip install opentimestamps-client)"}
    os.makedirs(ANCHOR_DIR, exist_ok=True)
    stamp_name = f"roots-{ts.replace(':', '').replace('-', '').split('.')[0]}Z.txt"
    stamp_path = os.path.join(ANCHOR_DIR, stamp_name)
    # Only values we actually verified go into the stamp. A withheld root is
    # absent rather than stamped as the string "None", because Bitcoin should
    # commit to what we can stand behind and nothing else; the break itself is
    # recorded in the anchor log beside it.
    body = "".join(f"{k} {roots[k]}\n" for k in sorted(roots)
                   if isinstance(roots[k], (str, int))) + f"anchored_at {ts}\n"
    with open(stamp_path, "w", encoding="utf-8") as f:
        f.write(body)
    try:
        proc = run(["ots", "stamp", stamp_path], capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and os.path.exists(stamp_path + ".ots"):
            return {"ok": True, "file": f"readings/anchors/{stamp_name}",
                    "proof": f"readings/anchors/{stamp_name}.ots"}
        return {"ok": False, "reason": (proc.stderr or proc.stdout or "ots stamp failed").strip()[:400]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def anchor(*, dry_run: bool = False, opener=urllib.request.urlopen,
           run=subprocess.run, log_path: str = ANCHOR_LOG,
           latest_path: str = ANCHOR_LATEST) -> dict | None:
    roots = current_roots()
    prev = last_anchor(log_path)
    # Every root we publish is compared. Leaving readings_root out meant a
    # refresh where the erasure inputs and the eval registry both sat still,
    # while the other readings moved, anchored nothing and kept republishing a
    # readings_root that no longer fingerprinted readings-ledger.jsonl, for as
    # many consecutive quiet rounds as it took.
    if prev and all(prev.get("roots", {}).get(k) == roots.get(k)
                    for k in ("registry_root", "erasure_root", "readings_root")):
        print("roots unchanged since last anchor — nothing to do")
        return None
    ts = datetime.now(timezone.utc).isoformat()
    if dry_run:
        print(json.dumps({"would_anchor": roots}, indent=2))
        return None

    record = {
        "ts": ts,
        "roots": roots,
        "wayback": [wayback_save(u, opener=opener) for u in WAYBACK_TARGETS],
        "ots": ots_stamp(roots, ts, run=run),
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    ok_wayback = sum(1 for w in record["wayback"] if w["ok"])
    latest = {
        "ts": ts,
        "registry_root": roots["registry_root"],
        "erasure_root": roots["erasure_root"],
        "readings_root": roots["readings_root"],
        "readings_chain": "broken" if roots.get("readings_problems") else "verified",
        "readings_problems": roots.get("readings_problems", []),
        "wayback_ok": ok_wayback,
        "wayback_snapshots": [w.get("snapshot") for w in record["wayback"] if w["ok"]],
        "ots": record["ots"].get("proof") if record["ots"]["ok"] else None,
        "ots_status": "stamped" if record["ots"]["ok"]
                      else record["ots"].get("reason", "failed"),
    }
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1)

    readings_line = (f"readings {roots['readings_root'][:16]}… "
                     f"({roots['readings_entries']} entries)"
                     if roots["readings_root"]
                     else f"readings WITHHELD ({len(roots['readings_problems'])} breaks)")
    print(f"anchored     : registry {roots['registry_root'][:16]}… / "
          f"erasure {roots['erasure_root'][:16]}… / " + readings_line)
    print(f"wayback      : {ok_wayback}/{len(WAYBACK_TARGETS)} snapshots")
    print(f"opentimestamps: {'stamped -> ' + record['ots']['proof'] if record['ots']['ok'] else record['ots'].get('reason')}")
    return record


if __name__ == "__main__":
    anchor(dry_run="--dry-run" in sys.argv[1:])
