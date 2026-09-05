"""Execute the publisher's status/proof/receipt path with a deterministic clock.

Body and rights validators have separate contracts in test_direct_railway_runtime;
this harness runs the unchanged upload, inventory, topology, origin-identity and
receipt sections, without rebuilding or publishing an edition.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = ROOT / "ops/railway/palimpsest-railway-publish"
DEPLOYMENT_ID = "55555555-5555-4555-8555-555555555555"
PROJECT_ID = "11111111-1111-4111-8111-111111111111"
ENVIRONMENT_ID = "22222222-2222-4222-8222-222222222222"
SERVICE_ID = "33333333-3333-4333-8333-333333333333"
IMAGE_DIGEST = "sha256:" + "d" * 64
MESSAGE = "palimpsest-hetzner-" + "a" * 12 + "-" + "b" * 12 + "-" + "c" * 32


def _run(tmp_path: Path, scenario: str) -> subprocess.CompletedProcess[str]:
    source = PUBLISHER.read_text()
    now = datetime.now(UTC).replace(microsecond=0)
    clock = now.isoformat().replace("+00:00", "Z")
    (tmp_path / "clock").write_text(str(int(now.timestamp())))
    (tmp_path / "clock-start").write_text(str(int(now.timestamp())))
    (tmp_path / "pending-candidate.json").write_text(MESSAGE)
    (tmp_path / "latest-success.json").write_text("predecessor\n")
    deployment = {
        "id": DEPLOYMENT_ID,
        "status": "SUCCESS",
        "createdAt": clock,
        "deploymentStopped": False,
        "instances": [{"status": "RUNNING"}],
        "meta": {
            "cliMessage": MESSAGE,
            "imageDigest": IMAGE_DIGEST,
            "reason": "deploy",
            "buildOnly": False,
            "volumeMounts": [],
            "serviceManifest": {
                "build": {
                    "builder": "DOCKERFILE",
                    "dockerfilePath": "ops/railway/Dockerfile.static",
                },
                "deploy": {
                    "cronSchedule": None,
                    "healthcheckPath": "/healthz",
                    "numReplicas": 1,
                    "requiredMountPath": None,
                },
            },
        },
    }
    inventory = [deployment]
    if scenario == "duplicate":
        inventory.append({**deployment, "id": "66666666-6666-4666-8666-666666666666"})
    if scenario == "failed":
        deployment["status"] = "FAILED"
    (tmp_path / "inventory.json").write_text(json.dumps(inventory))
    status = {
        "id": PROJECT_ID,
        "services": {
            "edges": [{"node": {"id": SERVICE_ID, "name": "palimpsest-publication"}}]
        },
        "environments": {
            "edges": [
                {
                    "node": {
                        "id": ENVIRONMENT_ID,
                        "canAccess": True,
                        "deletedAt": None,
                        "volumeInstances": {"edges": []},
                        "serviceInstances": {
                            "edges": [
                                {
                                    "node": {
                                        "environmentId": ENVIRONMENT_ID,
                                        "serviceId": SERVICE_ID,
                                        "serviceName": "palimpsest-publication",
                                        "source": None,
                                        "cronSchedule": None,
                                        "nextCronRunAt": None,
                                        "latestDeployment": deployment,
                                    }
                                }
                            ]
                        },
                    }
                }
            ]
        },
    }
    if scenario == "wrong_topology":
        status["id"] = SERVICE_ID
    (tmp_path / "status.json").write_text(json.dumps(status))
    railway = tmp_path / "railway"
    railway.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
from pathlib import Path
root = Path(os.environ["STATUS_RETRY_TEST_ROOT"])
scenario = os.environ["STATUS_RETRY_TEST_SCENARIO"]
kind = "list" if sys.argv[1:3] == ["deployment", "list"] else sys.argv[1]
events = root / "events.jsonl"
prior = [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
attempt = sum(row["kind"] == kind for row in prior) + 1
with events.open("a") as stream:
    stream.write(json.dumps({"kind": kind, "args": sys.argv[1:], "attempt": attempt}) + "\\n")
if kind == "up":
    print("{}")
    raise SystemExit(0)
timeout = (
    kind == "list" and (scenario == "persistent" or scenario == "list_once" and attempt == 1)
    or kind == "status" and scenario == "status_once" and attempt == 1
    or kind == "status" and scenario == "final_once" and attempt == 2
    or kind == "status" and scenario == "status_persistent"
    or kind == "status" and scenario == "final_persistent" and attempt >= 2
)
if kind == "list" and scenario == "empty":
    print("[]")
elif kind == "list" and scenario == "malformed":
    print("{invalid")
else:
    # Even plausible SUCCESS output from an exit-124 command must be discarded.
    print((root / ("inventory.json" if kind == "list" else "status.json")).read_text())
raise SystemExit(124 if timeout else 0)
"""
    )
    railway.chmod(0o700)
    timeout = tmp_path / "timeout"
    timeout.write_text(
        f"#!{sys.executable}\n"
        + """
import os, subprocess, sys
from pathlib import Path
seconds = int(sys.argv[3].removesuffix("s"))
result = subprocess.run(sys.argv[4:], check=False)
if result.returncode == 124:
    clock = Path(os.environ["STATUS_RETRY_TEST_ROOT"]) / "clock"
    clock.write_text(str(int(clock.read_text()) + seconds))
raise SystemExit(result.returncode)
"""
    )
    timeout.chmod(0o700)

    functions = source[
        source.index("bounded_deadline_timeout() {") : source.index("sha256_file() {")
    ]
    fsync = source[
        source.index("fsync_paths_and_directory() {") : source.index(
            "archive_predecessor_receipt() {"
        )
    ]
    upload_and_proof = source[
        source.index('upload_log="$STATE_ROOT/last-upload.json"') : source.index(
            'provider_manifest="$work_root/provider-railway-release.json"'
        )
    ]
    final_proof_and_receipt = source[
        source.index(
            'candidate_final_topology="$work_root/candidate-final-railway-status.json"'
        ) :
    ]
    # Populate unrelated receipt metadata without replacing the receipt writer.
    variables = {
        name: "1"
        for name in re.findall(r'--arg(?:json)? \w+ "\$(\w+)"', final_proof_and_receipt)
    }
    variables.update(
        {
            "STATE_ROOT": str(tmp_path),
            "work_root": str(tmp_path),
            "release": str(tmp_path),
            "checkout": str(ROOT),
            "PYTHON_BIN": sys.executable,
            "TIMEOUT_BIN": str(timeout),
            "RAILWAY_BIN": str(railway),
            "RAILWAY_NODE_OPTIONS": "--jitless",
            "railway_token_name": "STATUS_RETRY_TEST_TOKEN",
            "railway_token_value": "fixture-only",
            "PROJECT_ID": PROJECT_ID,
            "ENVIRONMENT_ID": ENVIRONMENT_ID,
            "SERVICE_ID": SERVICE_ID,
            "PROVIDER_ORIGIN": "https://provider.invalid",
            "PUBLIC_ORIGIN": "https://public.invalid",
            "message": MESSAGE,
            "release_sha": "a" * 40,
            "candidate_prepared_at": clock,
            "PENDING_CANDIDATE": str(tmp_path / "pending-candidate.json"),
            "SUCCESS_RECEIPT": str(tmp_path / "latest-success.json"),
            "candidate_journal_sha256": hashlib.sha256(MESSAGE.encode()).hexdigest(),
            "mutation_proof_deadline_epoch": str(int(now.timestamp()) + 600),
        }
    )
    constants = "\n".join(
        line
        for line in source.splitlines()
        if re.match(
            r"readonly (RAILWAY_UPLOAD_TIMEOUT_SECONDS|RAILWAY_COMMAND_TIMEOUT_SECONDS|ORIGIN_REQUEST_TIMEOUT_SECONDS|POST_MUTATION_POLL_SECONDS|RECEIPT_COMMIT_RESERVE_SECONDS)=",
            line,
        )
    )
    script = "\n".join(
        [
            "set -Eeuo pipefail",
            constants,
            *(f"{name}={shlex.quote(value)}" for name, value in variables.items()),
            """
log() { printf '%s\\n' "$*"; }
date() {
  if [[ "$*" == +%s ]]; then cat "$STATE_ROOT/clock"; else command date "$@"; fi
}
sleep() {
  local now
  now="$(cat "$STATE_ROOT/clock")"
  printf '%s' "$((now + $1))" > "$STATE_ROOT/clock"
}
sha256_file() { shasum -a 256 "$1" | awk '{print $1}'; }
origin_release_sha() {
  printf '%s\\n' "$1" >> "$STATE_ROOT/origin-proofs"
  printf '%s\\n' "$release_sha"
}
""",
            functions,
            fsync,
            upload_and_proof,
            final_proof_and_receipt,
        ]
    )
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "STATUS_RETRY_TEST_ROOT": str(tmp_path),
            "STATUS_RETRY_TEST_SCENARIO": scenario,
        },
        timeout=20,
        check=False,
    )


@pytest.mark.parametrize("scenario", ["list_once", "status_once", "final_once"])
def test_transient_status_timeout_keeps_one_submission_and_reaches_receipt(
    tmp_path: Path, scenario: str
) -> None:
    result = _run(tmp_path, scenario)
    assert result.returncode == 0, result.stderr
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    uploads = [row for row in events if row["kind"] == "up"]
    assert len(uploads) == 1
    assert uploads[0]["args"][-2:] == ["--message", MESSAGE]
    assert len(events) == 5  # One upload, inventory, two topology reads, one retry.
    assert "exit 124" in result.stderr
    assert (tmp_path / "origin-proofs").read_text().splitlines() == [
        "https://provider.invalid",
        "https://public.invalid",
    ]
    receipt = json.loads((tmp_path / "latest-success.json").read_text())
    assert receipt["railway"] == {"deployment_id": DEPLOYMENT_ID, "status": "SUCCESS"}
    assert receipt["candidate"]["message"] == MESSAGE
    assert not (tmp_path / "pending-candidate.json").exists()


@pytest.mark.parametrize(
    "scenario",
    [
        "persistent",
        "status_persistent",
        "final_persistent",
        "empty",
        "malformed",
        "duplicate",
        "failed",
        "wrong_topology",
    ],
)
def test_failed_status_proof_preserves_pending_and_predecessor(
    tmp_path: Path, scenario: str
) -> None:
    result = _run(tmp_path, scenario)
    assert result.returncode != 0
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert sum(row["kind"] == "up" for row in events) == 1
    assert (tmp_path / "pending-candidate.json").read_text() == MESSAGE
    assert (tmp_path / "latest-success.json").read_text() == "predecessor\n"
    assert (tmp_path / "origin-proofs").exists() == (scenario == "final_persistent")
    if scenario in {"persistent", "status_persistent", "final_persistent", "empty"}:
        # The clock reaches the original ten-minute deadline minus its unchanged
        # ten-second receipt reserve; retries cannot reset or extend that bound.
        started = int((tmp_path / "clock-start").read_text())
        assert int((tmp_path / "clock").read_text()) - started == 590
        assert "freshness deadline expired" in result.stderr
    if scenario == "persistent":
        assert sum(row["kind"] == "list" for row in events) == 24
