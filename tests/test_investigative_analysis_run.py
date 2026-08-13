from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.analytical_pieces import validate_draft_set, validate_packet_set
from core.investigative_candidates import canonical_json_bytes, validate_candidates
from core.wire_claim_audits import (
    canonical_json_bytes as wire_canonical_json_bytes,
    validate_wire_claim_audits,
)
from ops.investigative_analysis_runner import (
    DERIVED_LATEST,
    WIRE_STATUS_NAME,
    WIRE_STATUS_SCHEMA,
    snapshot_inputs,
)


ROOT = Path(__file__).resolve().parents[1]


def _execute_real_cascade(
    *, frozen: Path, work: Path, private: Path, commit: str, decision_clock: str
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.investigative_analysis_run",
            "--frozen-dir",
            str(frozen),
            "--readings-dir",
            str(work),
            "--private-dir",
            str(private),
            "--input-commit",
            commit,
            "--decision-clock",
            decision_clock,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_real_offline_cascade_is_complete_and_byte_replayable(tmp_path: Path) -> None:
    """Exercise every real analysis driver over one immutable input cohort."""

    frozen = tmp_path / "frozen"
    wire = tmp_path / "wire"
    wire.mkdir()
    for name in ("newswire-latest.json", "newswire-versions.jsonl"):
        (wire / name).write_bytes((ROOT / "readings" / name).read_bytes())
    latest_raw = (wire / "newswire-latest.json").read_bytes()
    latest = json.loads(latest_raw)
    completed = datetime.now(timezone.utc).replace(microsecond=0)
    (wire / WIRE_STATUS_NAME).write_text(
        json.dumps(
            {
                "schema_version": WIRE_STATUS_SCHEMA,
                "attempted_at": (completed - timedelta(seconds=1))
                .isoformat()
                .replace("+00:00", "Z"),
                "completed_at": completed.isoformat().replace("+00:00", "Z"),
                "status": "success",
                "fresh_sources": 1,
                "output_generated_at": latest["generated_at"],
                "output_sha256": hashlib.sha256(latest_raw).hexdigest(),
                "failure_class": None,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _trigger, _lineage, manifest, decision_clock = snapshot_inputs(
        readings_dir=ROOT / "readings",
        newswire_dir=wire,
        staging_readings=frozen,
    )
    assert all(row["path"].startswith("readings/") for row in manifest)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    editions: list[dict[str, bytes]] = []
    for number in (1, 2):
        work = tmp_path / f"work-{number}"
        private = tmp_path / f"private-{number}"
        work.mkdir()
        private.mkdir()
        _execute_real_cascade(
            frozen=frozen,
            work=work,
            private=private,
            commit=commit,
            decision_clock=decision_clock,
        )
        candidate_raw = (private / "candidates-latest.json").read_bytes()
        candidate = json.loads(candidate_raw)
        validate_candidates(candidate)
        assert candidate_raw == canonical_json_bytes(candidate)
        packet_raw = (private / "analytical-packets-latest.json").read_bytes()
        draft_raw = (private / "analytical-drafts-latest.json").read_bytes()
        audit_raw = (private / "wire-claim-audits-latest.json").read_bytes()
        packets = json.loads(packet_raw)
        drafts = json.loads(draft_raw)
        audits = json.loads(audit_raw)
        validate_packet_set(packets)
        validate_draft_set(packets, drafts)
        assert packet_raw == canonical_json_bytes(packets)
        assert draft_raw == canonical_json_bytes(drafts)
        validate_wire_claim_audits(audits)
        assert audit_raw == wire_canonical_json_bytes(audits)

        run_manifest = json.loads(
            (work / "analysis-run-manifest.json").read_text(encoding="utf-8")
        )
        assert run_manifest["decision_clock"] == decision_clock
        assert run_manifest["network_policy"] == "docker-network-none"
        assert [row["path"] for row in run_manifest["outputs"]] == [
            f"readings/{name}" for name in DERIVED_LATEST
        ]
        artifacts = {
            name: (work / name).read_bytes()
            for name in (*DERIVED_LATEST, "analysis-run-manifest.json")
        }
        artifacts["candidates-latest.json"] = candidate_raw
        artifacts["analytical-packets-latest.json"] = packet_raw
        artifacts["analytical-drafts-latest.json"] = draft_raw
        artifacts["wire-claim-audits-latest.json"] = audit_raw
        editions.append(artifacts)

    assert editions[0] == editions[1]
    assert len(
        {hashlib.sha256(payload).hexdigest() for payload in editions[0].values()}
    ) == len(editions[0])
