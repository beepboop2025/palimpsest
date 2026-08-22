"""One-command reproduce: verify the sealed chains and rebuild derived scorecards.

Stdlib plus the repo. No install, no key, no network. Exit 0 means every required
check passed. Optional notebooks are reported but do not fail the command if the
live readings they need are absent.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

REQUIRED = (
    ("erasure ledger", [sys.executable, str(ROOT / "scripts" / "verify_ledger.py")]),
    (
        "eval registry",
        [sys.executable, str(ROOT / "scripts" / "verify_eval_registry.py")],
    ),
    (
        "eval assurance",
        [sys.executable, "-m", "scripts.build_eval_assurance", "--check"],
    ),
    (
        "evidence capsule",
        [
            sys.executable,
            str(ROOT / "scripts" / "evidence_capsule.py"),
            "verify",
            str(ROOT / "protocol" / "test-vectors" / "palimpsest-erasure-v1.json"),
        ],
    ),
    (
        "weekly situation",
        [sys.executable, "-m", "scripts.weekly_situation_pull", "--check"],
    ),
    (
        "gazetteer phylogeny",
        [sys.executable, "-m", "scripts.gazetteer_phylogeny_pull", "--check"],
    ),
    (
        "collector health",
        [sys.executable, "-m", "scripts.collector_health_pull", "--check"],
    ),
)

OPTIONAL = (
    (
        "refusal transcripts",
        [sys.executable, str(ROOT / "scripts" / "verify_refusal_transcripts.py")],
    ),
    (
        "eval findings",
        [sys.executable, "-m", "scripts.build_eval_findings", "--check"],
    ),
    (
        "recompute DDTI threat identity",
        [sys.executable, str(ROOT / "notebooks" / "recompute_ddti.py"), "--check"],
    ),
    (
        "recompute forecast WIS",
        [sys.executable, str(ROOT / "notebooks" / "recompute_forecast_wis.py"), "--check"],
    ),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--optional",
        action="store_true",
        help="also run optional transcript and notebook checks",
    )
    args = parser.parse_args(argv)
    failed = 0
    print(f"Palimpsest reproduce-all · {ROOT}")
    for name, command in REQUIRED:
        failed += _run(name, command, required=True)
    if args.optional:
        for name, command in OPTIONAL:
            failed += _run(name, command, required=False)
    if failed:
        print(f"STATUS: {failed} required check(s) failed")
        return 1
    print("STATUS: INTACT: required sealed chains and derived scorecards match")
    print("Cite: python3 -m scripts.build_citation_pack --dataset ddti")
    print("Challenge a number: https://palimpsest.info/challenge.html")
    return 0


def _run(name: str, command: list[str], *, required: bool) -> int:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )
    body = (result.stdout or "") + (result.stderr or "")
    tail = " ".join(body.strip().splitlines()[-2:]) if body.strip() else "(no output)"
    if result.returncode == 0:
        print(f"  PASS  {name} · {tail[:160]}")
        return 0
    label = "FAIL" if required else "SKIP"
    print(f"  {label}  {name} · exit {result.returncode} · {tail[:200]}")
    return 1 if required else 0


if __name__ == "__main__":
    raise SystemExit(main())
