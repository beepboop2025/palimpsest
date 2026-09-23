"""Exercise the real controller functions and cached peer builder on tiny inputs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from core.peer_context import build_peer_document

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "ops/measurement/palimpsest-measurement-refresh"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value) + "\n")


@pytest.fixture
def harness(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    state = tmp_path / "state"
    (state / "jobs").mkdir(parents=True)
    checkout = tmp_path / "checkout"
    (checkout / "readings").mkdir(parents=True)
    (checkout / "scripts").mkdir()
    for package in ("core", "collectors", "processors"):
        (checkout / package).symlink_to(ROOT / package, target_is_directory=True)
    for name in ("peer_context_pull.py", "peer_context_rank_pull.py"):
        shutil.copy(ROOT / "scripts" / name, checkout / "scripts" / name)
    # Any accidental extra probe makes the real native cached builder fail.
    with (checkout / "scripts/peer_context_pull.py").open("a") as handle:
        handle.write("\ndef _http_fetch(*args):\n    raise AssertionError('unexpected network probe')\n")
    gfw = host / "ooni-gfw-latest.json"
    write_json(gfw, {"generated_at": "2026-09-23T19:12:37Z", "top_blocked": [
        {"domain": "www.ft.com", "measurement_count": 22,
         "completed_measurement_count": 13, "anomaly_count": 13}
    ]})
    old_digest = digest(gfw)
    peer = build_peer_document(urls=["https://www.ft.com/a"], greatfire=None,
                               gfw_path=gfw, warehouse=tmp_path / "no-warehouse")
    assert peer["n_ooni"] == 1
    write_json(host / "peer-context-latest.json", peer)
    write_json(host / "newswire-latest.json", {"items": [{"url": "https://www.ft.com/a"}]})
    history = b'{"generated_at":"2026-09-23T19:13:00Z","n_ooni":1}\n'
    (host / "peer-context-history.jsonl").write_bytes(history)
    (host / "ooni-peer-context-history.jsonl").write_bytes(b'{"n_hits":1}\n')
    # Retained historical companion is not silently rewritten as a zero reading.
    write_json(host / "ooni-peer-context-latest.json", {"schema_version": "palimpsest-ooni-peer-context/v1", "n_hits": 1, "hosts": peer["ooni"]["hosts"]})
    stamp = str(int(time.time()))
    for name in ("peer-context", "peer-context-rank"):
        (state / f"jobs/{name}.success").write_text(stamp + "\n")
    (state / "jobs/peer-context.input-sha256").write_text(old_digest + "\n")
    (state / "jobs/peer-context-rank.input-sha256").write_text(digest(host / "peer-context-latest.json") + "\n")
    source = RUNNER.read_text()
    functions = source[source.index("public_outputs_for_job() {"):source.index("\noverlay_last_good\n\n# Fast")]
    # The implementation under test retains all real hash/cadence/promotion
    # functions. Only repository cloning and outer command bounds are fixtures.
    preamble = "set -Eeuo pipefail\n" + functions + r'''
log() { printf '%s\n' "$*" >&2; }
overlay_last_good() {
  "$PYTHON_BIN" - "$HOST_READINGS" "$checkout/readings" <<'PYCOPY'
from pathlib import Path
import shutil, sys
for path in Path(sys.argv[1]).iterdir():
    shutil.copy2(path, Path(sys.argv[2]) / path.name)
PYCOPY
}
overlay_private_state() { :; }
promote_private_state() { :; }
timeout() { shift 3; "$@"; }
'''
    if shutil.which("flock"):
        preamble += 'flock() { printf "%s\\n" "$*" >>"$LOCK_LOG"; command flock "$@"; }\n'
    else:
        preamble += 'flock() { printf "%s\\n" "$*" >>"$LOCK_LOG"; }\n'
    env = {k: v for k, v in os.environ.items() if not k.startswith("PALIMPSEST_")}
    env.update(STATE_ROOT=str(state), HOST_READINGS=str(host), checkout=str(checkout),
               work_root=str(tmp_path), DATA_LOCK_FILE=str(tmp_path / "data.lock"),
               PYTHON_BIN=sys.executable, MEASUREMENT_SCOPE="core", JOB_TIMEOUT="12m",
               SILENCE_INDEX_TIMEOUT="18m", GFI_TIMEOUT="100m", succeeded="0", failed="0",
               changed="0", LOCK_LOG=str(tmp_path / "locks"), PYTHONDONTWRITEBYTECODE="1")

    class Harness:
        def run(self, commands, **overrides):
            result = subprocess.run(["bash", "-c", preamble + commands],
                                    env={**env, **overrides}, text=True, capture_output=True)
            assert result.returncode == 0, result.stderr
            return result

        def remove_ft(self):
            write_json(gfw, {"generated_at": "2026-09-23T21:14:05Z", "top_blocked": [
                {"domain": "unrelated.example", "measurement_count": 3,
                 "completed_measurement_count": 3, "anomaly_count": 1}
            ]})

    result = Harness()
    result.host, result.state, result.checkout = host, state, checkout
    result.gfw, result.old_digest, result.history, result.stamp = gfw, old_digest, history, stamp
    return result


PEER = 'run_job peer-context 360 "$PYTHON_BIN" -c "raise AssertionError(\'scheduled probe must not run\')"\n'
RANK = 'run_job peer-context-rank 360 "$PYTHON_BIN" -m scripts.peer_context_rank_pull\n'


def test_changed_gfw_removes_ft_honestly_preserves_history_and_source(harness):
    harness.remove_ft()
    gfw_raw = harness.gfw.read_bytes()
    old_companion = (harness.host / "ooni-peer-context-latest.json").read_bytes()
    old_ooni_history = (harness.host / "ooni-peer-context-history.jsonl").read_bytes()
    result = harness.run(PEER)
    peer = json.loads((harness.host / "peer-context-latest.json").read_bytes())
    assert peer["n_ooni"] == 0
    assert all(row["status"] == "miss" for row in peer["ooni"]["hosts"])
    assert harness.gfw.read_bytes() == gfw_raw  # No redating or source rewrite.
    assert (harness.host / "peer-context-history.jsonl").read_bytes().startswith(harness.history)
    assert (harness.host / "ooni-peer-context-history.jsonl").read_bytes() == old_ooni_history
    assert (harness.host / "ooni-peer-context-latest.json").read_bytes() == old_companion
    assert (harness.state / "jobs/peer-context.input-sha256").read_text().strip() == digest(harness.gfw)
    assert (harness.state / "jobs/peer-context.success").read_text().strip() == harness.stamp
    assert "refreshed peer-context" in result.stderr


def test_unchanged_dependency_skips_both_jobs_without_writes(harness):
    before = {p.name: p.read_bytes() for p in harness.host.iterdir()}
    result = harness.run(PEER + RANK)
    assert "skip peer-context; cadence not due" in result.stderr
    assert "skip peer-context-rank; cadence not due" in result.stderr
    assert {p.name: p.read_bytes() for p in harness.host.iterdir()} == before


def test_failed_derivation_does_not_acknowledge_and_next_cycle_retries(harness):
    harness.remove_ft()
    before = {p.name: p.read_bytes() for p in harness.host.iterdir()}
    failed = harness.run(PEER + RANK, PALIMPSEST_HALT="1")
    assert "peer-context failed; retained last-good files" in failed.stderr
    assert "current OONI dependency is not admitted" in failed.stderr
    assert {p.name: p.read_bytes() for p in harness.host.iterdir()} == before
    assert (harness.state / "jobs/peer-context.input-sha256").read_text().strip() == harness.old_digest
    harness.run(PEER)
    assert (harness.state / "jobs/peer-context.input-sha256").read_text().strip() == digest(harness.gfw)


def test_partial_output_promotion_keeps_dependency_pending_and_retries(harness):
    harness.remove_ft()
    wrapper = r'''
eval "$(declare -f promote_public_file | sed '1s/promote_public_file/original_promote_public_file/')"
promotions=0
promote_public_file() {
  promotions=$((promotions + 1))
  [[ "$(cat "$STATE_ROOT/jobs/peer-context.input-sha256")" == "$OLD_DIGEST" ]] || return 99
  if (( promotions == 2 )); then return 1; fi
  original_promote_public_file "$@"
}
'''
    result = harness.run(wrapper + PEER + RANK, OLD_DIGEST=harness.old_digest)
    assert "promotion failed; input admission remains pending" in result.stderr
    assert "current OONI dependency is not admitted" in result.stderr
    # The first existing per-file promotion completed, but no group admission
    # or successful cadence is recorded for that partial outcome.
    assert json.loads((harness.host / "peer-context-latest.json").read_bytes())["n_ooni"] == 0
    assert (harness.state / "jobs/peer-context.input-sha256").read_text().strip() == harness.old_digest
    assert (harness.state / "jobs/peer-context.success").read_text().strip() == harness.stamp
    harness.run(PEER + RANK)
    assert (harness.state / "jobs/peer-context.input-sha256").read_text().strip() == digest(harness.gfw)
    assert (harness.state / "jobs/peer-context-rank.input-sha256").read_text().strip() == digest(harness.host / "peer-context-latest.json")


def test_changed_source_during_capture_never_derives_or_promotes(harness):
    harness.remove_ft()
    before_peer = (harness.host / "peer-context-latest.json").read_bytes()
    wrapper = r'''
eval "$(declare -f overlay_last_good | sed '1s/overlay_last_good/original_overlay_last_good/')"
overlay_last_good() {
  printf '{"top_blocked":[],"generated_at":"2026-09-23T22:00:00Z"}\n' >"$HOST_READINGS/ooni-gfw-latest.json"
  original_overlay_last_good
}
'''
    result = harness.run(wrapper + PEER)
    assert "dependency changed during capture" in result.stderr
    assert (harness.host / "peer-context-latest.json").read_bytes() == before_peer
    assert (harness.state / "jobs/peer-context.input-sha256").read_text().strip() == harness.old_digest


@pytest.mark.parametrize("raced_root", ["$HOST_READINGS", "$checkout/readings"])
def test_promotion_refuses_a_changed_source_before_any_output_write(harness, raced_root):
    harness.remove_ft()
    before_peer = (harness.host / "peer-context-latest.json").read_bytes()
    wrapper = r'''
eval "$(declare -f promote_changed_readings | sed '1s/promote_changed_readings/original_promote_changed_readings/')"
promote_changed_readings() {
  printf '{"top_blocked":[],"generated_at":"2026-09-23T22:00:00Z"}\n' >"RACED_ROOT/ooni-gfw-latest.json"
  original_promote_changed_readings "$@"
}
'''.replace("RACED_ROOT", raced_root)
    result = harness.run(wrapper + PEER)
    assert "dependency changed before promotion" in result.stderr
    assert (harness.host / "peer-context-latest.json").read_bytes() == before_peer
    assert (harness.state / "jobs/peer-context.input-sha256").read_text().strip() == harness.old_digest


def test_rank_is_refreshed_after_changed_peer_despite_recent_success(harness):
    harness.remove_ft()
    result = harness.run(PEER + RANK)
    assert result.stderr.index("refreshed peer-context;") < result.stderr.index("refreshed peer-context-rank;")
    rank = json.loads((harness.host / "peer-context-rank-latest.json").read_bytes())
    assert rank["publication_policy"]["human_review_required"] is True
    assert rank["publication_policy"]["automatic_publication"] == "prohibited"
    assert rank["publication_policy"]["generative_model"] == "prohibited"
    assert (harness.state / "jobs/peer-context-rank.input-sha256").read_text().strip() == digest(harness.host / "peer-context-latest.json")


def test_consistent_admitted_pair_passes_unchanged_snapshot_coverage_guard(harness):
    harness.remove_ft()
    harness.run(PEER)
    before = json.loads((harness.host / "peer-context-latest.json").read_bytes())
    # Repeat the publisher's native snapshot rebuild on a fresh immutable copy.
    harness.run('overlay_last_good\ncd "$checkout"\nPYTHONPATH="$checkout" "$PYTHON_BIN" -c "from scripts.peer_context_pull import main; main(probe_weiboscope=False)"\n')
    rebuilt = json.loads((harness.checkout / "readings/peer-context-latest.json").read_bytes())
    retained = {row["host"]: row for row in before["ooni"]["hosts"] if row["status"] == "live"}
    rebuilt_hosts = {row["host"]: row for row in rebuilt["ooni"]["hosts"]}
    assert all(rebuilt_hosts.get(host, {}).get("status") == "live" for host in retained)
    assert rebuilt["n_ooni"] == before["n_ooni"] == 0


@pytest.mark.parametrize("invalid", ["missing", "symlink", "malformed", "array", "bad-inventory"])
def test_missing_or_invalid_dependency_never_acknowledges(harness, invalid):
    if invalid in {"missing", "symlink"}:
        harness.gfw.rename(harness.gfw.with_suffix(".original"))
        if invalid == "symlink":
            harness.gfw.symlink_to(harness.gfw.with_suffix(".original"))
    else:
        harness.gfw.write_text({"malformed": "{", "array": "[]", "bad-inventory": '{"top_blocked":{}}'}[invalid])
    result = harness.run(PEER)
    assert "dependency is unavailable" in result.stderr
    assert (harness.state / "jobs/peer-context.input-sha256").read_text().strip() == harness.old_digest


def test_dependency_refresh_does_not_defer_normal_six_hour_probe(harness):
    harness.remove_ft()
    harness.run(PEER)
    cadence = harness.state / "jobs/peer-context.success"
    assert cadence.read_text().strip() == harness.stamp
    cadence.write_text(str(int(time.time()) - 6 * 3600 - 1) + "\n")
    # The ordinary caller command must now run even with an admitted digest.
    # Its probe is represented by a local marker, with no network access.
    scheduled = '''run_job peer-context 360 "$PYTHON_BIN" -c 'from pathlib import Path; from scripts.peer_context_pull import main; Path("normal-probe-ran").write_text("yes"); main(probe_weiboscope=False)'\n'''
    result = harness.run(scheduled)
    assert "refreshed peer-context" in result.stderr
    assert (harness.checkout / "normal-probe-ran").read_text() == "yes"
    assert int(cadence.read_text()) >= int(harness.stamp)


def test_source_collection_precedes_derivation_without_cadence_change():
    source = RUNNER.read_text()
    assert source.index("run_job ooni-gfw 90") < source.index("run_job peer-context 360") < source.index("run_job peer-context-rank 360")
    assert source.count("run_job ooni-gfw 90") == 1
