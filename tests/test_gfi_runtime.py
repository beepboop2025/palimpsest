"""Offline contracts for bounded GFI execution and race-safe admission."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import subprocess

import pytest

from core import eval_registry
from scripts import generative_firewall_reading as gfr
from scripts import verify_gfi_promotion as promotion

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "ops/measurement/palimpsest-measurement-refresh"


def function(name, next_name):
    source = RUNNER.read_text()
    return source[source.index(name + "() {"):source.index("\n" + next_name + "() {")]


@pytest.mark.parametrize("scope,job,expected", [
    ("core", "generative-firewall", ""),
    ("regional-research", "generative-firewall", ""),
    ("generative-firewall", "ooni-gfw", ""),
    ("generative-firewall", "regional-research", ""),
    ("generative-firewall", "generative-firewall", "due:generative-firewall\n"),
])
def test_scope_filters_execute_before_any_paid_or_other_work(scope, job, expected):
    source = RUNNER.read_text()
    start = source.index("run_job() {")
    end = source.index("\noverlay_last_good\n", start)
    script = source[start:end] + '\njob_due() { echo "due:$1"; return 1; }\nlog() { :; }\nrun_job "$1" 0 true\n'
    result = subprocess.run(["bash", "-c", script, "test", job],
                            env={"MEASUREMENT_SCOPE":scope}, text=True, capture_output=True, check=True)
    assert result.stdout == expected


def test_gfi_timer_and_timeout_cover_the_existing_retry_budget():
    service = (ROOT / "ops/systemd/palimpsest-gfi-refresh.service").read_text()
    timer = (ROOT / "ops/systemd/palimpsest-gfi-refresh.timer").read_text()
    runner = RUNNER.read_text()
    assert 'readonly GFI_TIMEOUT=100m' in runner
    assert 'timeout_limit="$GFI_TIMEOUT"' in runner
    assert "TimeoutStartSec=110m" in service
    assert "OnCalendar=*-*-* 08:00:00 UTC" in timer
    assert "Restart=" not in service
    assert "PALIMPSEST_MEASUREMENT_SCOPE=generative-firewall" in service
    assert "PALIMPSEST_MEASUREMENT_STATE_ROOT=/var/lib/palimpsest/gfi-refresh" in service
    assert "RequiresMountsFor=/opt/palimpsest-gfi/source /var/lib/palimpsest/gfi-refresh" in service
    assert "run_job generative-firewall 0 " in runner
    assert 660 * 51 / 6 < 100 * 60 < 110 * 60


def test_progress_does_not_change_the_660_calls_or_expose_content(monkeypatch, capsys):
    calls = []
    def fake_fetch(key, model, prompt):
        calls.append((model, prompt))
        return None if model.startswith("qwen/") else "private-response-canary"
    monkeypatch.setattr(gfr, "fetch_one", fake_fetch)
    rounds = gfr.run_panel("private-key-canary", gfr.build_probes(), k=5)
    assert len(rounds) == 5
    assert len(calls) == 660 and set(Counter(model for model, _ in calls).values()) == {220}
    assert set(Counter(calls).values()) == {5}
    output = capsys.readouterr().out
    assert "private-response-canary" not in output and "private-key-canary" not in output
    assert not any(prompt in output for _, prompt in calls)
    records = [json.loads(line.removeprefix("GFI progress ")) for line in output.splitlines() if line.startswith("GFI progress ")]
    assert [row["completed"] for row in records] == list(range(30, 661, 30))
    assert records[-1]["expected"] == 660
    assert set(records[-1]["completed_by_model"].values()) == {220}
    assert records[-1]["abstained_by_model"]["qwen/qwen-2.5-7b-instruct"] == 220


def make_chain(path):
    return eval_registry.preregister(path, ["a"], suite="runtime-fixture")


def test_registry_prefix_preserves_concurrent_history_and_rejects_forks(tmp_path):
    host, candidate = tmp_path / "host", tmp_path / "candidate"
    prereg = make_chain(host)
    candidate.write_bytes(host.read_bytes())
    eval_registry.submit_run(candidate, probe_set_hash=prereg["probe_set_hash"], model="model-a", responses={"a":"first"}, metrics={})
    before = host.read_bytes(), candidate.read_bytes()
    promotion.check_registry_prefix(host, candidate)
    assert (host.read_bytes(), candidate.read_bytes()) == before
    eval_registry.submit_run(host, probe_set_hash=prereg["probe_set_hash"], model="model-b", responses={"a":"concurrent"}, metrics={})
    advanced = host.read_bytes(), candidate.read_bytes()
    with pytest.raises(ValueError, match="advanced or diverged"):
        promotion.check_registry_prefix(host, candidate)
    assert (host.read_bytes(), candidate.read_bytes()) == advanced


def test_invalid_registry_cannot_be_admitted_even_as_a_byte_prefix(tmp_path):
    host, candidate = tmp_path / "host", tmp_path / "candidate"
    make_chain(host)
    damaged = host.read_text().replace('"seq":0', '"seq":4').replace('"seq": 0', '"seq": 4')
    assert damaged != host.read_text()
    host.write_text(damaged)
    candidate.write_bytes(host.read_bytes())
    with pytest.raises(ValueError, match="chain is invalid"):
        promotion.check_registry_prefix(host, candidate)


def test_failed_preflight_stops_before_any_public_file_mutation_even_in_substitution(tmp_path):
    lock = tmp_path / "data.lock"
    lock.write_bytes(b"")
    marker = tmp_path / "marker"
    marker.touch()
    # Mimic the runner's command-substitution context, where bash normally
    # disables errexit inside the called function. An explicit return is vital.
    script = function("promote_changed_readings", "job_due") + '''
flock() { :; }
public_outputs_for_job() { echo readings/latest.json; }
promote_public_file() { echo forbidden-mutation > "$MUTATION"; }
promoted="$(promote_changed_readings "$MARKER" generative-firewall)"
exit "$?"
'''
    mutation = tmp_path / "mutation"
    result = subprocess.run(["bash", "-c", script], env={
        "DATA_LOCK_FILE":str(lock), "checkout":str(tmp_path), "PYTHON_BIN":"/usr/bin/false",
        "HOST_READINGS":str(tmp_path), "MUTATION":str(mutation), "MARKER":str(marker),
    }, capture_output=True, text=True)
    assert result.returncode == 1
    assert not mutation.exists()


def test_gfi_outputs_never_include_the_shared_readings_ledger():
    script = function("public_outputs_for_job", "copy_last_good_public_file") + '\npublic_outputs_for_job generative-firewall\n'
    result = subprocess.run(["bash", "-c", script], check=True, text=True, capture_output=True)
    assert "readings/readings-ledger.jsonl" not in result.stdout.splitlines()


@pytest.mark.parametrize("scope,failed,changed,expected", [
    ("generative-firewall", 1, 0, 1),
    ("generative-firewall", 0, 0, 1),
    ("generative-firewall", 0, 6, 0),
    ("core", 1, 0, 0),
])
def test_dedicated_failure_is_visible_without_changing_core_partial_success(scope, failed, changed, expected):
    source = RUNNER.read_text()
    start = source.index('if [[ "$MEASUREMENT_SCOPE" == generative-firewall ]] &&')
    result = subprocess.run(["bash", "-c", source[start:]], env={
        "MEASUREMENT_SCOPE":scope, "failed":str(failed), "changed":str(changed),
    })
    assert result.returncode == expected


@pytest.mark.parametrize("failure", ["open", "flock", "copy"])
def test_lock_and_copy_errors_cannot_fall_through_to_promotion(tmp_path, failure):
    readings = tmp_path / "readings"
    readings.mkdir()
    (readings / "latest.json").write_bytes(b"candidate")
    marker = tmp_path / "marker"
    marker.touch()
    import os
    os.utime(readings / "latest.json", (2_000_000_000, 2_000_000_000))
    mutation = tmp_path / "mutation"
    script = function("promote_changed_readings", "job_due") + '''
flock() { test "$FAILURE" != flock; }
public_outputs_for_job() { echo readings/latest.json; }
promote_public_file() { test "$FAILURE" != copy || return 1; echo forbidden > "$MUTATION"; }
promoted="$(promote_changed_readings "$MARKER" peer-context)"
exit "$?"
'''
    result = subprocess.run(["bash", "-c", script], env={
        "DATA_LOCK_FILE":str(tmp_path if failure == "open" else tmp_path / "lock"),
        "checkout":str(tmp_path), "PYTHON_BIN":"/usr/bin/true", "FAILURE":failure,
        "HOST_READINGS":str(tmp_path), "MUTATION":str(mutation), "MARKER":str(marker),
    }, capture_output=True, text=True)
    assert result.returncode == 1
    assert not mutation.exists()


def test_history_bootstrap_keeps_newer_source_and_refuses_divergence(tmp_path):
    source, host = tmp_path / "source", tmp_path / "host"
    (source / "readings").mkdir(parents=True)
    host.mkdir()
    first = b'{"date":"2026-08-11","gfi":12}\n'
    second = b'{"date":"2026-08-23","gfi":15}\n'
    path = source / "readings/history.jsonl"
    path.write_bytes(first + second)
    (host / "history.jsonl").write_bytes(first)
    baseline = tmp_path / "before.jsonl"
    promotion.prepare_history(source, host, baseline)
    assert path.read_bytes() == baseline.read_bytes() == first + second
    (host / "history.jsonl").write_bytes(first.replace(b'12', b'13'))
    with pytest.raises(ValueError, match="diverge"):
        promotion.prepare_history(source, host, baseline)
    assert path.read_bytes() == baseline.read_bytes() == first + second


def test_history_promotion_cannot_drop_a_reviewed_older_source_row(tmp_path):
    source, host = tmp_path / "source", tmp_path / "host"
    (source / "readings").mkdir(parents=True)
    host.mkdir()
    baseline = tmp_path / "before.jsonl"
    baseline.write_text('{"date":"2026-08-23","gfi":15}\n')
    (source / "readings/history.jsonl").write_text('{"date":"2026-09-13","gfi":20}\n')
    (source / "readings/latest.json").write_text('{"summary":{"date":"2026-09-13"}}')
    with pytest.raises(ValueError, match="lost or rewritten"):
        promotion.check_history(source, host, baseline)
