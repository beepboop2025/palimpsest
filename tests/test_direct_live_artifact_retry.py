"""Run the real shell fetcher against transient and permanent origin failures."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = ROOT / "ops/railway/palimpsest-railway-publish"


def _run(tmp_path: Path, scenario: str, budget: int = 600):
    source = PUBLISHER.read_text()
    deadlines = source[
        source.index("bounded_deadline_timeout() {") : source.index("sha256_file() {")
    ]
    fetcher = source[
        source.index("fetch_live_artifact_pair() {") : source.index(
            "validate_durable_release_manifest() {"
        )
    ]
    (tmp_path / "clock").write_text("1000")
    curl = tmp_path / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        + '''
import json, os, sys
from pathlib import Path
root = Path(os.environ["ARTIFACT_RETRY_ROOT"])
scenario = os.environ["ARTIFACT_RETRY_SCENARIO"]
args = sys.argv[1:]
url = args[-1]
events = root / "events.jsonl"
prior = [json.loads(row) for row in events.read_text().splitlines()] if events.exists() else []
attempt = sum(row["url"] == url for row in prior) + 1
with events.open("a") as stream:
    stream.write(json.dumps({"url": url, "args": args, "attempt": attempt}) + "\\n")
provider = "provider.invalid" in url
failed = provider and (scenario == "persistent" or attempt == 1 and scenario != "success")
http, code, body = "200", 0, b"verified artifact\\n"
if failed:
    http, body = "503", b"temporary upstream failure\\n"
    if scenario in {"timeout", "reset"}:
        http, code = "000", 28 if scenario == "timeout" else 56
        clock = root / "clock"
        clock.write_text(str(int(clock.read_text()) + int(args[args.index("--max-time") + 1])))
    elif scenario == "missing":
        http = "404"
    elif scenario == "denied":
        http = "403"
    elif scenario == "too_large":
        http, code = "200", 63
    elif scenario == "empty":
        http, body = "200", b""
Path(args[args.index("-o") + 1]).write_bytes(body)
print(http, end="")
raise SystemExit(code)
'''
    )
    curl.chmod(0o700)
    variables = {
        "ARTIFACT_RETRY_ROOT": str(tmp_path),
        "PROVIDER_ORIGIN": "https://provider.invalid",
        "PUBLIC_ORIGIN": "https://public.invalid",
        "ORIGIN_REQUEST_TIMEOUT_SECONDS": "15",
        "POST_MUTATION_POLL_SECONDS": "5",
        "mutation_proof_deadline_epoch": str(1000 + budget),
    }
    script = "\n".join(
        [
            "set -Eeuo pipefail",
            *(f"{key}={shlex.quote(value)}" for key, value in variables.items()),
            '''
log() { printf '%s\\n' "$*" >&2; }
date() { cat "$ARTIFACT_RETRY_ROOT/clock"; }
sleep() {
  local now
  now="$(cat "$ARTIFACT_RETRY_ROOT/clock")"
  printf '%s' "$((now + $1))" > "$ARTIFACT_RETRY_ROOT/clock"
}
stat() { wc -c < "$3"; }
''',
            deadlines,
            fetcher,
            'fetch_live_artifact_pair readings/github-refuge-latest.json '
            '"$ARTIFACT_RETRY_ROOT/provider" "$ARTIFACT_RETRY_ROOT/public" '
            'original-nonce 1000 10 application/json',
        ]
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "ARTIFACT_RETRY_ROOT": str(tmp_path),
            "ARTIFACT_RETRY_SCENARIO": scenario,
        },
        timeout=10,
        check=False,
    )
    events = [json.loads(row) for row in (tmp_path / "events.jsonl").read_text().splitlines()]
    return result, events


@pytest.mark.parametrize("scenario", ["unavailable", "timeout", "reset"])
def test_transient_origin_failure_retries_same_bounded_url(tmp_path, scenario):
    result, events = _run(tmp_path, scenario)
    assert result.returncode == 0, result.stderr
    assert len(events) == 3
    assert events[0]["url"] == events[1]["url"]
    assert events[0]["url"].endswith("?receipt=original-nonce")
    assert "public.invalid" in events[2]["url"]
    assert (tmp_path / "provider").read_bytes() == (tmp_path / "public").read_bytes() == b"verified artifact\n"
    for event in events:
        args = event["args"]
        assert args[args.index("--max-filesize") + 1] == "1000"
        assert args[args.index("--proto") + 1] == "=https"


@pytest.mark.parametrize("scenario", ["missing", "denied", "too_large", "empty"])
def test_permanent_or_invalid_response_is_never_retried(tmp_path, scenario):
    result, events = _run(tmp_path, scenario)
    assert result.returncode != 0
    assert len(events) == 1
    assert not (tmp_path / "public").exists()


def test_persistent_failure_has_three_attempt_ceiling(tmp_path):
    result, events = _run(tmp_path, "persistent")
    assert result.returncode != 0
    assert len(events) == 3
    assert int((tmp_path / "clock").read_text()) == 1010


def test_retry_cannot_extend_original_deadline_or_use_receipt_reserve(tmp_path):
    result, events = _run(tmp_path, "timeout", budget=20)
    assert result.returncode != 0
    assert len(events) == 1
    assert int((tmp_path / "clock").read_text()) == 1010
    assert "freshness deadline expired" in result.stderr
