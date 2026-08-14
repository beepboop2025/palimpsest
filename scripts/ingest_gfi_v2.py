"""Reseal an already-collected GFI v2 transcript on the current public registry head.

This is the race-recovery path: it never queries a model.  It carries the expensive
measured bytes forward, rebuilds only their registry attestations, and then lets the
normal verifiers prove the result before publication.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import generative_firewall_reading as gfr  # noqa: E402


def main() -> int:
    try:
        protocol = json.loads(Path(gfr.GFI_PROTOCOL).read_text(encoding="utf-8"))
        transcripts = json.loads(Path(gfr.GFI_TRANSCRIPTS).read_text(encoding="utf-8"))
        reading = json.loads(Path(gfr.LATEST).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"GFI v2 ingest refused: {exc}")
        return 1
    current = gfr.build_gfi_protocol(k=protocol.get("samples_per_cell"))
    try:
        gfr.require_gfi_preregistration(current)
    except RuntimeError as exc:
        print(f"GFI v2 ingest refused: {exc}")
        return 1
    for field in ("probe_commitment", "evaluation_protocol_sha256"):
        if transcripts.get(field) != protocol.get(field):
            print(f"GFI v2 ingest refused: transcript {field} differs from protocol")
            return 1
    summary = reading.get("summary")
    responses = transcripts.get("responses")
    if not isinstance(summary, dict) or not isinstance(responses, dict):
        print("GFI v2 ingest refused: reading summary or responses are malformed")
        return 1
    try:
        appended = gfr._seal_gfi_v2(
            protocol, responses, summary, datetime.now(timezone.utc)
        )
    except (KeyError, TypeError, ValueError) as exc:
        print(f"GFI v2 ingest refused: {exc}")
        return 1
    print(f"GFI v2 ingest sealed {appended} new model run(s) without requerying")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
