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

Idempotent by the house convention: if neither root moved since the last
anchor record, nothing is anchored and nothing grows. A broken chain is never
anchored — verification failure aborts with exit 1, because anchoring a bad
root would launder it.

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
ANCHOR_LOG = os.path.join(READINGS, "anchors.jsonl")
ANCHOR_LATEST = os.path.join(READINGS, "anchors-latest.json")
ANCHOR_DIR = os.path.join(READINGS, "anchors")

SITE = os.environ.get("PALIMPSEST_SITE", "https://palimpsest.info")
WAYBACK_TARGETS = (
    f"{SITE}/readings/eval-registry.jsonl",
    f"{SITE}/readings/erasure-ledger.jsonl",
)
UA = "palimpsest-anchor/1.0 (+https://palimpsest.info)"


def current_roots() -> dict:
    """Verify both chains and return their roots. Refuses broken chains."""
    reg_entries = reg.read_ledger(REGISTRY)
    led_entries = led.read_ledger(ERASURE)
    reg_ok, reg_problems = reg.verify(reg_entries)
    led_ok, led_problems = led.verify(led_entries)
    if not (reg_ok and led_ok):
        for p in reg_problems + led_problems:
            print(f"BROKEN: {p}")
        raise SystemExit(1)
    return {
        "registry_root": led.merkle_root(reg_entries),
        "registry_head": reg_entries[-1]["entry_hash"] if reg_entries else led.GENESIS_PREV,
        "registry_entries": len(reg_entries),
        "erasure_root": led.merkle_root(led_entries),
        "erasure_head": led_entries[-1]["entry_hash"] if led_entries else led.GENESIS_PREV,
        "erasure_entries": len(led_entries),
    }


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
    body = "".join(f"{k} {roots[k]}\n" for k in sorted(roots)) + f"anchored_at {ts}\n"
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
    if prev and all(prev.get("roots", {}).get(k) == roots[k]
                    for k in ("registry_root", "erasure_root")):
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
        "wayback_ok": ok_wayback,
        "wayback_snapshots": [w.get("snapshot") for w in record["wayback"] if w["ok"]],
        "ots": record["ots"].get("proof") if record["ots"]["ok"] else None,
        "ots_status": "stamped" if record["ots"]["ok"]
                      else record["ots"].get("reason", "failed"),
    }
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1)

    print(f"anchored     : registry {roots['registry_root'][:16]}… / erasure {roots['erasure_root'][:16]}…")
    print(f"wayback      : {ok_wayback}/{len(WAYBACK_TARGETS)} snapshots")
    print(f"opentimestamps: {'stamped -> ' + record['ots']['proof'] if record['ots']['ok'] else record['ots'].get('reason')}")
    return record


if __name__ == "__main__":
    anchor(dry_run="--dry-run" in sys.argv[1:])
