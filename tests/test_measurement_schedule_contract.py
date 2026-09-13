"""Runtime collection cadence and offline review-queue migration contracts."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import shutil
import socket
import subprocess

import pytest

from processors import board_alarm
from scripts import peer_context_rank_pull
from scripts import stage_pages_rights as rights

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "ops/measurement/palimpsest-measurement-refresh"
NETWORK = ("ooni-gfw", "in-path-interference", "ioda-outages")


def _jobs():
    return {match[0]: int(match[1]) for match in re.findall(
        r"^run_job ([a-z-]+) ([0-9]+) ", RUNNER.read_text(), re.M)}


def _function(name, next_name):
    source = RUNNER.read_text()
    return source[source.index(f"{name}() {{"):source.index(f"\n{next_name}() {{")]


def test_network_jobs_meet_catalog_target_on_the_hourly_timer():
    catalog = json.loads((ROOT / "config/public_data_catalog.json").read_text())
    rows = {row["id"]: row for row in catalog["datasets"]}
    timer = (ROOT / "ops/systemd/palimpsest-measurement-refresh.timer").read_text()
    assert "OnCalendar=hourly" in timer
    for name in NETWORK:
        # Quantize the successful-run guard to the actual timer, rather than
        # comparing a catalog promise to a guard the service cannot poll at.
        nominal_minutes = 60 * math.ceil(_jobs()[name] / 60)
        target = int(re.fullmatch(r"PT(\d+)H", rows[name]["cadence"])[1]) * 60
        assert nominal_minutes <= target
        assert 60 <= _jobs()[name] < target  # throttle floor and timing reserve


@pytest.mark.parametrize("age,expected", [(0, 1), (89 * 60, 1), (90 * 60, 0), (121 * 60, 0)])
def test_actual_due_guard_enforces_the_successful_run_interval(tmp_path, age, expected):
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs/ooni-gfw.success").write_text(str(1_000_000 - age))
    code = _function("job_due", "run_job") + '\ndate() { echo 1000000; }\njob_due ooni-gfw 90\n'
    result = subprocess.run(["bash", "-c", code], env={"STATE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert result.returncode == expected


def test_current_wall_clock_bound_is_separate_from_historical_rates():
    for signal, job in (("ooni_gfw", "ooni-gfw"), ("ioda_outages", "ioda-outages")):
        assert board_alarm.CADENCE_PER_DAY[signal] == 4.0
        bound = board_alarm.CURRENT_MAX_READINGS_PER_DAY[signal]
        assert bound == 24 * 60 / _jobs()[job]
        assert board_alarm._readings_to_days(signal, 160) == 10.0
    assert board_alarm._readings_to_days("unknown", 160) is None


def test_ranker_runs_after_warehouse_and_only_promotes_its_review_outputs():
    source = RUNNER.read_text()
    assert source.index("run_job peer-context 360") < source.index("run_job peer-context-rank 360")
    code = _function("public_outputs_for_job", "copy_last_good_public_file") + '\npublic_outputs_for_job peer-context-rank\n'
    result = subprocess.run(["bash", "-c", code], check=True, text=True, capture_output=True)
    assert result.stdout.splitlines() == [
        "readings/peer-context-rank-latest.json", "readings/peer-context-rank-history.jsonl"]
    # Both already-tracked paths enter the publisher's existing rights-checked
    # overlay; this migration adds no new public artifact class.
    tracked = subprocess.check_output(["git", "ls-files", "readings/peer-context-rank*"], cwd=ROOT, text=True)
    assert set(result.stdout.splitlines()).issubset(tracked.splitlines())


def test_offline_rank_refresh_preserves_warehouse_history_and_review_gate(tmp_path, monkeypatch):
    readings = tmp_path / "readings"
    readings.mkdir()
    fixtures = ROOT / "tests/fixtures/peer_context"
    for path in fixtures.glob("*.json*"):
        shutil.copy(path, readings / path.name)
    warehouse = readings / "peer-context-latest.json"
    warehouse.write_text('{"schema_version":"palimpsest-peer-context.v1","n_hosts":2}\n')
    history = readings / "peer-context-rank-history.jsonl"
    prefix = b'{"schema":"historical-test-receipt","generated_at":"2026-08-01T00:00:00Z"}\n'
    history.write_bytes(prefix)
    originals = {path.name: path.read_bytes() for path in readings.iterdir()}

    def no_network(*_args, **_kwargs):
        raise AssertionError("cached ranker attempted network access")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(peer_context_rank_pull.KillSwitch, "is_halted", lambda self: False)
    assert peer_context_rank_pull.main(["--root", str(tmp_path), "--now", "2026-09-13T21:00:00Z"]) == 0
    output = json.loads((readings / "peer-context-rank-latest.json").read_text())
    assert output["n_peer_series"] > 0
    assert output["publication_policy"]["human_review_required"] is True
    assert output["publication_policy"]["automatic_publication"] == "prohibited"
    assert output["publication_policy"]["generative_model"] == "prohibited"
    # The existing public review artifact must still pass the same per-source
    # rights scanner as every publisher edition; no blanket ranker exception.
    policy = rights.load_source_policy(ROOT / rights.POLICY_RELATIVE_PATH)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    allowed = frozenset(key for key, value in policy.decisions.items()
                        if rights._effective_decision(value, evaluated_at=now) == "allow")
    denied = frozenset(set(policy.decisions) - allowed)
    assert not rights._contains_denied_value(
        ROOT, ROOT / "readings/peer-context-rank-latest.json",
        (readings / "peer-context-rank-latest.json").read_bytes(),
        denied_source_ids=denied, allowed_source_ids=allowed,
        lineage_pattern=rights._lineage_pattern(denied),
        decoded_text=(readings / "peer-context-rank-latest.json").read_text())
    assert history.read_bytes().startswith(prefix)
    for name, raw in originals.items():
        if name != history.name:
            assert (readings / name).read_bytes() == raw
    first_history = history.read_bytes()
    assert peer_context_rank_pull.main(["--root", str(tmp_path), "--now", "2026-09-13T22:00:00Z"]) == 0
    assert history.read_bytes() == first_history  # identical analysis never duplicates history
