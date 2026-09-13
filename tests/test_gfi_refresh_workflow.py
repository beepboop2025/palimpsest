from pathlib import Path
import hashlib
import json

from core import eval_registry as registry
from core import gfi_protocol
from scripts import preregister_gfi_v2


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "gfi-refresh.yml"


def test_shipping_instrument_has_an_exact_preregistration():
    # This reads the committed production instrument and registry, not a fixture.
    # Even transport-only edits change the exact classifier commitment.
    assert preregister_gfi_v2.main(["--check"]) == 0


def test_unregistered_classifier_bytes_fail_the_shipping_check(monkeypatch):
    protocol = preregister_gfi_v2.gfr.build_gfi_protocol()
    changed = {field: protocol[field] for field in gfi_protocol.CORE_FIELDS}
    changed["classifier_sha256"] = "0" * 64
    monkeypatch.setattr(
        preregister_gfi_v2.gfr, "build_gfi_protocol",
        lambda: gfi_protocol.seal_protocol(changed),
    )
    paths = [ROOT / "readings" / name for name in (
        "eval-registry.jsonl", "eval-registry-latest.json",
        "gfi-evaluation-protocol-v2.json",
    )]
    before = [path.read_bytes() for path in paths]

    assert preregister_gfi_v2.main(["--check"]) == 1
    assert [path.read_bytes() for path in paths] == before


def test_registry_preserves_recovered_history_and_its_summary():
    path = ROOT / "readings" / "eval-registry.jsonl"
    historical_prefix = b"".join(path.read_bytes().splitlines(keepends=True)[:548])
    # Immutable history recovered from source 499d485a; later append is allowed.
    assert hashlib.sha256(historical_prefix).hexdigest() == (
        "de8e8e6638cb116f52b5234c69579aa624300a1163ef0ff44589ff60ec2cab18"
    )
    entries = registry.read_ledger(path)
    assert registry.verify(entries) == (True, [])
    published = json.loads((ROOT / "readings/eval-registry-latest.json").read_text())
    assert published == registry.summary_document(entries)


def test_exact_protocol_is_public_before_any_paid_model_call():
    text = WORKFLOW.read_text(encoding="utf-8")
    preregister = text.index("python -m scripts.preregister_gfi_v2")
    prereg_commit = text.index("eval: preregister GFI v2 protocol", preregister)
    prereg_push = text.index("python scripts/push_data_commit.py --base-locked", prereg_commit)
    collect = text.index("run: python scripts/generative_firewall_reading.py")

    assert preregister < prereg_commit < prereg_push < collect
    assert "'readings/gfi-evaluation-protocol-v2*.json'" in text[preregister:collect]
    assert text.index("git add 'readings/gfi-evaluation-protocol-v2*.json'") < text.index(
        "python scripts/verify_public_surface.py", preregister,
    )


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

    assert "python -m pip install --quiet --require-hashes" in text
    assert "-r .github/osint-china-ci-requirements.txt" in text
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
        "readings/china-situation-latest.json",
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
