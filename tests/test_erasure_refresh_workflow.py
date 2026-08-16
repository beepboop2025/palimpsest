"""Publication invariants for the long-running eval/erasure refresh.

The model sweep takes long enough that other readings workflows can push repeatedly
while it is running. A swallowed rebase failure once discarded six fresh evals, and a
later run exhausted its one-shot race recovery when two publishers won in succession.
These tests keep publication failure loud and preserve the expensive model result while
shared ledgers are rebuilt on the current public head before every retry.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "erasure-refresh.yml"


def test_eval_refresh_is_race_safe_and_never_swallows_a_rebase_failure():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "git pull --rebase origin main || true" not in text
    assert "if git rebase origin/main; then" in text
    assert "git rebase --abort || true" in text
    assert "git switch --detach origin/main" in text
    assert text.count("python scripts/push_data_commit.py --base-locked") == 2
    assert "continue-on-error: true" in text
    assert "for attempt in 1 2 3 4; do" in text
    assert "FATAL: erasure publication exhausted four verified rebuild retries" in text
    assert "sleep $((attempt * 5))" in text
    assert "--force" not in text


def test_a_race_rebuilds_shared_seals_without_requerying_paid_models():
    text = WORKFLOW.read_text(encoding="utf-8")

    # The paid observation is retained from the candidate. Only deterministic
    # composites, seals, verification, anchoring, and scrubbing may be rerun.
    assert text.count("run: python -m scripts.refusal_drift_pull") == 1
    assert text.count("python3 scripts/seal_readings.py || rc=$?") == 3
    assert text.count("python -m scripts.anchor_roots") == 2
    assert text.count("python -m scripts.verify_eval_registry") == 3
    assert text.count("python scripts/verify_public_surface.py") == 3
    assert text.count("\n          PALIMPSEST_WAYBACK_ACCESS_KEY:") == 2
    assert text.count("\n          PALIMPSEST_WAYBACK_SECRET_KEY:") == 2

    race = text.index("Recollect, reverify, and retry after a push race")
    refresh = text.index("git fetch origin main", race)
    reset = text.index("git switch --detach origin/main", refresh)
    restore = text.index('git checkout "$measurement_commit"', reset)
    aggregate = text.index("python -m scripts.erasure_pull", restore)
    rebuild = text.index("python3 scripts/seal_readings.py || rc=$?", aggregate)
    verify = text.index("python -m scripts.verify_eval_registry", rebuild)
    anchor = text.index("python -m scripts.anchor_roots", verify)
    scrub = text.index("python scripts/verify_public_surface.py", anchor)
    final_push = text.rindex("python scripts/push_data_commit.py --base-locked")
    assert (
        race
        < refresh
        < reset
        < restore
        < aggregate
        < rebuild
        < verify
        < anchor
        < scrub
        < final_push
    )


def test_each_retry_discards_stale_derived_bytes_before_rebuilding():
    text = WORKFLOW.read_text(encoding="utf-8")
    race = text.index("Recollect, reverify, and retry after a push race")
    preserve_start = text.index("# Carry forward only this run's measured/model artifacts", race)
    preserve_end = text.index("            done", preserve_start)
    preserved = text[preserve_start:preserve_end]

    for derived_path in (
        "readings/erasure-observatory-latest.json",
        "readings/erasure-observatory-history.jsonl",
        "readings/erasure-ledger.jsonl",
        "readings/readings-ledger.jsonl",
        "readings/anchors.jsonl",
        "readings/anchors-latest.json",
        "readings/anchors/",
    ):
        assert derived_path not in preserved

    retry = text[race:]
    loop = retry.index("for attempt in 1 2 3 4; do")
    refresh = retry.index("git fetch origin main", loop)
    reset = retry.index("git switch --detach origin/main", refresh)
    rebuild = retry.index("python -m scripts.erasure_pull", reset)
    seal = retry.index("python3 scripts/seal_readings.py || rc=$?", rebuild)
    verify_ledger = retry.index("python -m scripts.verify_ledger", seal)
    verify_eval = retry.index("python -m scripts.verify_eval_registry", verify_ledger)
    verify_transcripts = retry.index(
        "python -m scripts.verify_refusal_transcripts", verify_eval
    )
    verify_seal = retry.index("python3 scripts/seal_readings.py --check", verify_transcripts)
    anchor = retry.index("python -m scripts.anchor_roots", verify_seal)
    scrub = retry.index("python scripts/verify_public_surface.py", anchor)
    commit = retry.index('git commit -C "$measurement_commit"', scrub)
    push = retry.index(
        "if python scripts/push_data_commit.py --base-locked; then", commit
    )
    assert loop < refresh < reset < rebuild < seal < verify_ledger
    assert verify_ledger < verify_eval < verify_transcripts < verify_seal
    assert verify_seal < anchor < scrub < commit < push


def test_workflow_bounds_provider_runtime_and_stages_every_eval_artifact():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "timeout-minutes: 150" in text
    assert "timeout-minutes: 110" in text
    assert 'cron: "8 */6 * * *"' in text
    race = text.index("Recollect, reverify, and retry after a push race")
    staging_start = text.index(
        "for p in readings/erasure-observatory-latest.json", race
    )
    staging_end = text.index("            done", staging_start)
    retry_staging = text[staging_start:staging_end]
    for path in (
        "readings/eval-registry.jsonl",
        "readings/eval-registry-latest.json",
        "readings/eval-assurance-latest.json",
        "readings/eval-journal-latest.json",
        "evals/",
        "readings/refusal-drift-latest.json",
        "readings/refusal-drift-history.jsonl",
        "readings/refusal-drift-transcripts.json",
        "readings/refusal-drift-churn.jsonl",
        "readings/readings-ledger.jsonl",
        "readings/anchors.jsonl",
        "readings/anchors-latest.json",
    ):
        assert path in retry_staging, path


def test_eval_assurance_is_rebuilt_after_every_chain_reconciliation():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert text.count("python -m scripts.build_eval_assurance") >= 6
    assert "python -m scripts.build_eval_assurance --check" in text
    assert "python -m scripts.verify_gfi_transcripts" in text
    assert "readings/eval-assurance-latest.json" in text
    assert text.count("python -m scripts.build_eval_journal") >= 6
    assert "python -m scripts.build_eval_journal --check" in text
    assert "readings/eval-journal-latest.json" in text
    assert text.count("python -m scripts.build_eval_findings") >= 6
    assert "python -m scripts.build_eval_findings --check" in text
    assert "readings/eval-articles-latest.json" in text
    assert "journal/" in text


def test_eval_article_scenarios_gate_reconciliation_and_every_race_retry():
    text = WORKFLOW.read_text(encoding="utf-8")
    command = "python -m pytest -q"

    assert "Install the hash-pinned eval publication test environment" in text
    assert ".github/osint-china-ci-requirements.txt" in text
    assert text.count(command) == 2
    for test_path in (
        "tests/test_eval_assurance.py",
        "tests/test_eval_journal.py",
        "tests/test_eval_articles.py",
        "tests/test_eval_journal_renderer.py",
    ):
        assert text.count(test_path) == 2, test_path

    reconcile = text.index("Verify the reconciled eval and readings chains")
    reconcile_check = text.index("python -m scripts.build_eval_findings --check", reconcile)
    reconcile_tests = text.index(command, reconcile_check)
    reconcile_anchor = text.index("Anchor only the reconciled roots", reconcile_tests)
    assert reconcile < reconcile_check < reconcile_tests < reconcile_anchor

    retry = text.index("Recollect, reverify, and retry after a push race")
    retry_check = text.index("python -m scripts.build_eval_findings --check", retry)
    retry_tests = text.index(command, retry_check)
    retry_anchor = text.index("python -m scripts.anchor_roots", retry_tests)
    assert retry < retry_check < retry_tests < retry_anchor
