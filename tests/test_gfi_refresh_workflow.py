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
        "python -m scripts.build_eval_findings --check",
        "readings/gfi-transcripts-latest.json",
        "readings/eval-registry.jsonl",
        "readings/eval-registry-latest.json",
        "readings/eval-assurance-latest.json",
        "readings/eval-journal-latest.json",
        "readings/eval-articles-latest.json",
        "evals/",
        "journal/",
    ):
        assert required in text


def test_every_gfi_publication_path_runs_the_universal_semantic_contract():
    text = WORKFLOW.read_text(encoding="utf-8")
    candidate = text[text.index("Create the measured candidate"):]
    candidate = candidate[:candidate.index("Attempt the base-locked publication")]
    retry = text[text.index("Reseal the measured bytes after a publication race"):]

    assert "python -m pip install --quiet pytest" in text
    for contract in (
        "tests/test_osint_china.py",
        "tests/test_evidence_mesh.py",
        "tests/test_data_catalog.py",
        "tests/test_seal_readings.py",
        "tests/test_publication_contract.py",
        "tests/test_eval_assurance.py",
        "tests/test_eval_journal.py",
        "tests/test_eval_articles.py",
        "tests/test_eval_journal_renderer.py",
    ):
        assert text.count(contract) == 2

    for output in (
        "readings/osint-china-latest.json",
        "readings/investigations-latest.json",
        "readings/corroboration-latest.json",
        "readings/network-rounds-latest.json",
        "readings/editorial-readiness-latest.json",
        "readings/evidence-mesh-latest.json",
        "readings/machine-investigations-latest.json",
        "readings/newsroom-latest.json",
        "readings/readings-ledger.jsonl",
        "readings/catalog.json",
        "readings/eval-articles-latest.json",
        "readings/board-alarm-analysis.json",
        "journal/",
    ):
        assert output in candidate
        assert output in retry


def test_push_race_reseals_responses_without_requerying_models():
    text = WORKFLOW.read_text(encoding="utf-8")
    race = text.index("Reseal the measured bytes after a publication race")
    retry = text[race:]

    assert "python -m scripts.ingest_gfi_v2" in retry
    assert "generative_firewall_reading.py" not in retry
    assert "for attempt in 1 2 3 4; do" in retry
    assert "git reset --hard origin/main" in retry
    assert "git clean -fd" in retry
    assert text.count("git add -A -- readings news evals journal datapackage.json") == 2
    assert "FATAL: GFI publication exhausted four verified reseal retries" in retry
    assert "--force" not in text
