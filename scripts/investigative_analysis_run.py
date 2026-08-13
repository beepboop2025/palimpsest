#!/usr/bin/env python3
"""Run one egress-disabled, immutable-snapshot investigative analysis edition."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from core.analytical_pieces import build_packet_set, build_template_draft_set
from core.investigative_candidates import (
    atomic_write,
    build_candidates,
    canonical_json_bytes,
)
from core.wire_claim_audits import (
    DELIVERY_POLICY as WIRE_DELIVERY_POLICY,
    build_wire_claim_audits,
    canonical_json_bytes as wire_canonical_json_bytes,
)


DERIVED_LATEST = (
    "vantage-fusion-latest.json",
    "event-flags-latest.json",
    "coverage-guard-latest.json",
    "board-alarm-latest.json",
    "cross-layer-latest.json",
    "forecast-ledger-latest.json",
    "china-economic-pulse-latest.json",
    "osint-china-latest.json",
    "investigations-latest.json",
)
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(path: Path, document: dict) -> None:
    atomic_write(path, canonical_json_bytes(document))


def _file_version(path: Path) -> tuple[int, int, int, str] | None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        _sha256(path),
    )


def _require_fresh_output(
    path: Path, previous: tuple | None, *, decision_text: str
) -> None:
    current = _file_version(path)
    if current is None or current == previous:
        raise RuntimeError(f"analysis step did not refresh {path.name}")
    try:
        document = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"analysis step wrote invalid JSON: {path.name}") from exc
    try:
        output_clock = _parse_clock(document.get("generated_at", ""))
        decision_clock = _parse_clock(decision_text)
    except (AttributeError, TypeError, ValueError):
        output_clock = None
        decision_clock = None
    if not isinstance(document, dict) or output_clock != decision_clock:
        raise RuntimeError(
            f"analysis step did not bind {path.name} to the frozen decision clock"
        )


def _materialize_frozen_inputs(frozen_dir: Path, readings_dir: Path) -> None:
    """Copy the read-only evidence snapshot into a disposable work directory."""

    if not frozen_dir.is_dir() or not readings_dir.is_dir():
        raise RuntimeError("frozen input and readings work directories must exist")
    if any(readings_dir.iterdir()):
        raise RuntimeError("readings work directory must begin empty")
    rows = sorted(frozen_dir.iterdir(), key=lambda path: path.name)
    if not rows or len(rows) > 256:
        raise RuntimeError("frozen input inventory is outside 1..256 files")
    for source in rows:
        descriptor = os.open(
            source,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > 64 * 1024 * 1024
            ):
                raise RuntimeError(f"frozen input is not a bounded file: {source.name}")
            chunks: list[bytes] = []
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) != metadata.st_size or os.read(descriptor, 1):
                raise RuntimeError(f"frozen input changed while copied: {source.name}")
        finally:
            os.close(descriptor)
        atomic_write(readings_dir / source.name, raw, mode=0o640)


def _parse_clock(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("decision_clock must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("decision_clock must include a timezone")
    return parsed.astimezone(timezone.utc)


def run(
    *,
    frozen_dir: Path,
    readings_dir: Path,
    private_dir: Path,
    input_commit: str,
    decision_clock: datetime,
) -> dict:
    """Execute the fixed dependency order against one staged readings directory."""

    if not _COMMIT.fullmatch(input_commit):
        raise ValueError("input_commit must be a full lowercase Git object ID")
    if readings_dir.resolve() == private_dir.resolve():
        raise ValueError("readings and private output directories must be distinct")
    if decision_clock.tzinfo is None or decision_clock.utcoffset() is None:
        raise ValueError("decision_clock must be timezone-aware")
    decision_clock = decision_clock.astimezone(timezone.utc)
    decision_text = decision_clock.isoformat().replace("+00:00", "Z")
    if frozen_dir.resolve() in {readings_dir.resolve(), private_dir.resolve()}:
        raise ValueError("frozen, readings, and private directories must be distinct")
    _materialize_frozen_inputs(frozen_dir, readings_dir)

    # Bind the legacy drivers to this disposable workspace explicitly. This
    # makes the same cascade testable outside Docker while production still
    # supplies /app/readings inside the network-disabled container.
    from scripts import (
        board_alarm_pull,
        conformal_events_pull,
        coverage_guard_pull,
        cross_layer_pull,
        forecast_ledger_pull,
        vantage_fusion_pull,
    )

    modules = (
        board_alarm_pull,
        conformal_events_pull,
        coverage_guard_pull,
        cross_layer_pull,
        forecast_ledger_pull,
        vantage_fusion_pull,
    )
    for module in modules:
        module.READINGS = str(readings_dir)
        module.OUT = str(readings_dir / Path(module.OUT).name)
        module.HIST = str(readings_dir / Path(module.HIST).name)

    steps: list[tuple[str, object]] = [
        ("vantage_fusion", vantage_fusion_pull),
        ("event_flags", conformal_events_pull),
        ("coverage_guard", coverage_guard_pull),
        ("board_alarm", board_alarm_pull),
        ("cross_layer", cross_layer_pull),
        ("forecast_ledger", forecast_ledger_pull),
    ]
    completed: list[str] = []
    for name, module in steps:
        output = Path(module.OUT)
        previous = _file_version(output)
        options = {"now": decision_clock}
        if name == "forecast_ledger":
            options["append_unchanged"] = False
        result = module.main(**options)
        if (
            name == "vantage_fusion"
            and isinstance(result, dict)
            and not result.get("ok")
        ):
            # The public legacy driver intentionally preserves last-good on an
            # abstention. Inside a frozen run that would masquerade as fresh, so
            # write the explicit abstention into staging only.
            result = dict(result)
            result["generated_at"] = decision_text
            result["status"] = "abstain"
            atomic_write(output, canonical_json_bytes(result), mode=0o640)
        _require_fresh_output(output, previous, decision_text=decision_text)
        completed.append(name)

    from scripts import build_economic_pulse, build_investigations, build_osint_china

    code_root = Path(__file__).resolve().parents[1]

    output = readings_dir / "china-economic-pulse-latest.json"
    previous = _file_version(output)
    code = build_economic_pulse.main(
        [
            "--readings-dir",
            str(readings_dir),
            "--registry",
            str(code_root / "config/china_econ_sources.json"),
            "--output",
            str(readings_dir / "china-economic-pulse-latest.json"),
            "--as-of",
            decision_text,
        ]
    )
    if code:
        raise RuntimeError(f"economic pulse exited with status {code}")
    _require_fresh_output(output, previous, decision_text=decision_text)
    completed.append("economic_pulse")

    output = readings_dir / "osint-china-latest.json"
    previous = _file_version(output)
    build_osint_china.main(
        [
            "--readings-dir",
            str(readings_dir),
            "--output",
            str(readings_dir / "osint-china-latest.json"),
            "--input-commit",
            input_commit,
            "--now",
            decision_text,
        ]
    )
    _require_fresh_output(output, previous, decision_text=decision_text)
    completed.append("osint_china")

    output = readings_dir / "investigations-latest.json"
    previous = _file_version(output)
    code = build_investigations.main(
        [
            "--readings-dir",
            str(readings_dir),
            "--config",
            str(code_root / "config/investigations.json"),
            "--output",
            str(readings_dir / "investigations-latest.json"),
            "--as-of",
            decision_text,
        ]
    )
    if code:
        raise RuntimeError(f"investigations builder exited with status {code}")
    _require_fresh_output(output, previous, decision_text=decision_text)
    completed.append("investigations")

    document = build_candidates(readings_dir, decision_clock=decision_clock)
    # This is a staging mount, not the durable private ledger. The host validates
    # the entire run first and only then publishes this edition transactionally.
    atomic_write(private_dir / "candidates-latest.json", canonical_json_bytes(document))
    completed.append("candidate_edition")

    # Models never receive the source lake or choose their own evidence.  This
    # compact, content-addressed projection is the only supported drafting input.
    packets = build_packet_set(document)
    drafts = build_template_draft_set(packets)
    atomic_write(
        private_dir / "analytical-packets-latest.json",
        canonical_json_bytes(packets),
    )
    atomic_write(
        private_dir / "analytical-drafts-latest.json",
        canonical_json_bytes(drafts),
    )
    completed.extend(("analytical_packets", "analytical_template_drafts"))

    # The Wire audit is a separate delivery-safe projection. It covers every
    # accepted feed event, but only entries passing its deterministic interest
    # and evidence gates are eligible for an automated brief.
    wire_audits = build_wire_claim_audits(
        readings_dir,
        decision_clock=decision_clock,
    )
    atomic_write(
        private_dir / "wire-claim-audits-latest.json",
        wire_canonical_json_bytes(wire_audits),
    )
    completed.append("wire_claim_audits")

    outputs = []
    for name in DERIVED_LATEST:
        path = readings_dir / name
        if not path.is_file():
            raise RuntimeError(f"analysis step did not produce {name}")
        outputs.append(
            {
                "path": f"readings/{name}",
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    manifest = {
        "schema_version": "palimpsest-investigative-analysis-run.v3",
        "completed_at": decision_text,
        "input_commit": input_commit,
        "decision_clock": decision_text,
        "network_policy": "docker-network-none",
        "publication_policy": "private-review-only",
        "steps": completed,
        "candidate_edition_id": document["edition_id"],
        "candidate_input_fingerprint": document["input_fingerprint"],
        "candidate_count": document["n_candidates"],
        "analytical_packet_edition_id": packets["edition_id"],
        "analytical_packet_count": packets["n_packets"],
        "analytical_draft_edition_id": drafts["edition_id"],
        "analytical_draft_count": drafts["n_drafts"],
        "wire_claim_audit_edition_id": wire_audits["edition_id"],
        "wire_claim_audit_count": wire_audits["n_audits"],
        "wire_claim_audit_brief_eligible_count": sum(
            audit["brief_eligible"] for audit in wire_audits["audits"]
        ),
        "wire_delivery_policy": WIRE_DELIVERY_POLICY,
        "outputs": outputs,
    }
    _write_manifest(readings_dir / "analysis-run-manifest.json", manifest)
    return manifest


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-dir", type=Path, default=Path("/app/frozen"))
    parser.add_argument("--readings-dir", type=Path, default=Path("/app/readings"))
    parser.add_argument("--private-dir", type=Path, default=Path("/app/private"))
    parser.add_argument(
        "--input-commit",
        default=os.getenv("PALIMPSEST_INPUT_COMMIT", ""),
    )
    parser.add_argument("--decision-clock", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    manifest = run(
        frozen_dir=args.frozen_dir,
        readings_dir=args.readings_dir,
        private_dir=args.private_dir,
        input_commit=args.input_commit,
        decision_clock=_parse_clock(args.decision_clock),
    )
    print(
        "investigative analysis -> "
        f"{manifest['candidate_edition_id']} · "
        f"{manifest['candidate_count']} staged candidates · "
        f"{manifest['analytical_draft_count']} private working drafts · "
        f"{manifest['wire_claim_audit_brief_eligible_count']} Wire briefs eligible"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
