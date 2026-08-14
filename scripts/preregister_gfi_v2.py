"""Publish the exact GFI v2 protocol before any model is queried.

The scheduled workflow commits and pushes the files produced here as a separate
publication event.  Only after that push succeeds does it run the paid model panel.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import eval_registry as reg  # noqa: E402
from core import gfi_protocol as gfi_proto  # noqa: E402
from core.sealed_ledger import atomic_replace_bytes  # noqa: E402
from scripts import generative_firewall_reading as gfr  # noqa: E402


def _document(protocol: dict, entry: dict) -> dict:
    return {
        **protocol,
        "registration": {
            "registry": "readings/eval-registry.jsonl",
            "seq": entry["seq"],
            "ts": entry["ts"],
            "entry_hash": entry["entry_hash"],
        },
        "collection_guard": (
            "scripts/generative_firewall_reading.py refuses to query a model unless this "
            "protocol and its matching registry entry are already present"
        ),
        "verify_cmd": "python -m scripts.verify_gfi_transcripts",
    }


def _encoded(document: dict) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    protocol = gfr.build_gfi_protocol()
    entries = reg.read_ledger(gfr.EVAL_REGISTRY)
    ok, problems = reg.verify(entries)
    if not ok:
        print("BROKEN: " + "; ".join(problems))
        return 1
    entry = next(
        (
            item
            for item in entries
            if item.get("kind") == reg.PREREGISTRATION
            and item.get("probe_set_hash") == protocol["probe_commitment"]
            and item.get("suite") == gfi_proto.SUITE
        ),
        None,
    )
    if args.check:
        if entry is None:
            print("MISSING: current GFI v2 protocol is not preregistered")
            return 1
        expected = _encoded(_document(protocol, entry))
        try:
            current = Path(gfr.GFI_PROTOCOL).read_bytes()
        except OSError:
            print("MISSING: readings/gfi-evaluation-protocol-v2.json")
            return 1
        if current != expected:
            print("STALE: published GFI v2 protocol differs from the shipping instrument")
            return 1
        print(
            f"INTACT: GFI v2 protocol {protocol['probe_commitment'][:16]}… was "
            f"preregistered at seq {entry['seq']}"
        )
        return 0

    if entry is None:
        entry = reg.preregister(
            gfr.EVAL_REGISTRY,
            [
                f"{arm_id}\t{arm['prompt_sha256']}\t{protocol['evaluation_protocol_sha256']}"
                for arm_id, arm in sorted(protocol["arms"].items())
            ],
            suite=gfi_proto.SUITE,
            note=(
                "GFI v2 exact-prompt protocol: panel, cohorts, k, method and classifier "
                "bytes are bound through evaluation_protocol_sha256"
            ),
        )
        print(
            f"registered GFI v2 protocol {protocol['probe_commitment'][:16]}… at "
            f"seq {entry['seq']}"
        )
    else:
        print(f"GFI v2 protocol already registered at seq {entry['seq']}")
    atomic_replace_bytes(gfr.GFI_PROTOCOL, _encoded(_document(protocol, entry)))
    reg.refresh_summary(gfr.EVAL_REGISTRY, gfr.EVAL_REGISTRY_SUMMARY)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
