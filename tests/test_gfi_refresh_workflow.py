from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "gfi-refresh.yml"


def test_exact_protocol_is_public_before_any_paid_model_call():
    text = WORKFLOW.read_text(encoding="utf-8")
    preregister = text.index("python -m scripts.preregister_gfi_v2")
    prereg_commit = text.index("eval: preregister GFI v2 protocol", preregister)
    prereg_push = text.index("python scripts/push_data_commit.py --base-locked", prereg_commit)
    collect = text.index("run: python scripts/generative_firewall_reading.py")

    assert preregister < prereg_commit < prereg_push < collect
    assert "readings/gfi-evaluation-protocol-v2.json" in text[preregister:collect]


def test_gfi_publication_carries_full_evidence_and_machine_assurance():
    text = WORKFLOW.read_text(encoding="utf-8")
    for required in (
        "python -m scripts.verify_gfi_transcripts",
        "python -m scripts.build_eval_assurance --check",
        "python -m scripts.build_eval_journal --check",
        "readings/gfi-transcripts-latest.json",
        "readings/eval-registry.jsonl",
        "readings/eval-registry-latest.json",
        "readings/eval-assurance-latest.json",
        "readings/eval-journal-latest.json",
        "evals/",
    ):
        assert required in text


def test_push_race_reseals_responses_without_requerying_models():
    text = WORKFLOW.read_text(encoding="utf-8")
    race = text.index("Reseal the measured bytes after a publication race")
    retry = text[race:]

    assert "python -m scripts.ingest_gfi_v2" in retry
    assert "generative_firewall_reading.py" not in retry
    assert "for attempt in 1 2 3 4; do" in retry
    assert "FATAL: GFI publication exhausted four verified reseal retries" in retry
    assert "--force" not in text
