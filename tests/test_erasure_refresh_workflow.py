"""Publication invariants for the long-running eval/erasure refresh.

The model sweep takes long enough that another readings workflow will normally push
while it is running. A swallowed rebase failure left Actions on a detached rebase
HEAD; six consecutive runs then measured and verified fresh evals but discarded them
at ``git push``. These tests keep publication failure loud and preserve the expensive
model result while shared ledgers are rebuilt on the current public head.
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
    assert text.count("git push origin HEAD:main") == 2
    assert "continue-on-error: true" in text


def test_a_race_rebuilds_shared_seals_without_requerying_paid_models():
    text = WORKFLOW.read_text(encoding="utf-8")

    # The paid observation is retained from the candidate. Only deterministic
    # composites, seals, verification, anchoring, and scrubbing may be rerun.
    assert text.count("run: python -m scripts.refusal_drift_pull") == 1
    assert text.count("python3 scripts/seal_readings.py || rc=$?") == 3
    assert text.count("python -m scripts.anchor_roots") == 2
    assert text.count("python -m scripts.verify_eval_registry") == 3
    assert text.count("python scripts/verify_public_surface.py") == 3

    race = text.index("Synchronize the measured result after a push race")
    restore = text.index("git restore --source=origin/main", race)
    rebuild = text.index("python3 scripts/seal_readings.py || rc=$?", restore)
    verify = text.index("python -m scripts.verify_eval_registry", rebuild)
    anchor = text.index("python -m scripts.anchor_roots", verify)
    scrub = text.index("python scripts/verify_public_surface.py", anchor)
    final_push = text.rindex("git push origin HEAD:main")
    assert race < restore < rebuild < verify < anchor < scrub < final_push


def test_workflow_bounds_provider_runtime_and_stages_every_eval_artifact():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "timeout-minutes: 150" in text
    assert "timeout-minutes: 110" in text
    for path in (
        "readings/eval-registry.jsonl",
        "readings/eval-registry-latest.json",
        "readings/refusal-drift-latest.json",
        "readings/refusal-drift-history.jsonl",
        "readings/refusal-drift-transcripts.json",
        "readings/refusal-drift-churn.jsonl",
        "readings/readings-ledger.jsonl",
        "readings/anchors.jsonl",
        "readings/anchors-latest.json",
    ):
        assert text.count(path) >= 3, path
