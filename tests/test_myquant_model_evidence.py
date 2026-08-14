from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import eval_registry as reg
from core import myquant_model_evidence as evidence
from scripts import import_myquant_model_evidence as import_cli
from scripts import seal_readings


BASE = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _preregistration(*, evaluation: str = "evaluation-1", issued: datetime | None = None) -> dict:
    return {
        "schema": evidence.PREREGISTRATION_SCHEMA,
        "kind": evidence.PREREGISTRATION_KIND,
        "evaluation_id": _sha(evaluation),
        "issued_at": _utc(issued or (BASE - timedelta(minutes=5))),
        "model_artifact_sha256": _sha("candidate-model"),
        "probe_set_sha256": _sha("sealed-probe-set"),
        "probe_count": 43,
        "evaluation_protocol_sha256": _sha("evaluation-protocol"),
        "authority": dict(evidence.AUTHORITY),
    }


def _run(
    preregistration_sha256: str,
    *,
    evaluation: str = "evaluation-1",
    run: str = "run-1",
    result: str = "private-result-artifact-1",
    started: datetime | None = None,
    completed: datetime | None = None,
) -> dict:
    return {
        "schema": evidence.RUN_SCHEMA,
        "kind": evidence.RUN_KIND,
        "evaluation_id": _sha(evaluation),
        "run_id": _sha(run),
        "preregistration_receipt_sha256": preregistration_sha256,
        "started_at": _utc(started or (BASE + timedelta(minutes=1))),
        "completed_at": _utc(completed or (BASE + timedelta(minutes=2))),
        "model_artifact_sha256": _sha("candidate-model"),
        "probe_set_sha256": _sha("sealed-probe-set"),
        "evaluation_protocol_sha256": _sha("evaluation-protocol"),
        "result_artifact_sha256": _sha(result),
        "authority": dict(evidence.AUTHORITY),
    }


def _receipt_sha256(receipt: dict) -> str:
    return hashlib.sha256(evidence.canonical_json_bytes(receipt)).hexdigest()


def _write_envelope(
    path: Path,
    receipt: dict,
    *,
    claimed: str | None = None,
    envelope_extra: dict | None = None,
) -> Path:
    envelope = {
        "schema": evidence.ENVELOPE_SCHEMA,
        "receipt_sha256": claimed or _receipt_sha256(receipt),
        "receipt": receipt,
    }
    envelope.update(envelope_extra or {})
    path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    return path


def _locations(tmp_path: Path) -> tuple[Path, Path, Path]:
    readings = tmp_path / "readings"
    readings.mkdir(exist_ok=True)
    return (
        readings / "eval-registry.jsonl",
        readings / "myquant-model-evidence" / "sha256",
        readings / "myquant-model-evidence-latest.json",
    )


def _import(
    tmp_path: Path,
    receipt: dict,
    *,
    now: datetime,
    name: str,
) -> tuple[evidence.ImportResult, tuple[Path, Path, Path], Path]:
    locations = _locations(tmp_path)
    envelope = _write_envelope(tmp_path / name, receipt)
    result = evidence.import_envelope(
        envelope,
        registry_path=locations[0],
        store_dir=locations[1],
        latest_path=locations[2],
        now=now,
    )
    return result, locations, envelope


def _stored(store: Path, digest: str) -> Path:
    return store / digest[:2] / f"{digest}.json"


def test_preregistration_is_content_addressed_projected_and_exactly_idempotent(
    tmp_path: Path,
) -> None:
    receipt = _preregistration()
    first, (registry, store, latest), envelope = _import(
        tmp_path, receipt, now=BASE, name="prereg.json"
    )
    registry_before = registry.read_bytes()
    latest_before = latest.read_bytes()

    assert first.changed is True
    assert _stored(store, first.receipt_sha256).read_bytes() == evidence.canonical_json_bytes(
        receipt
    )
    entry = reg.read_ledger(str(registry))[0]
    assert entry["preregistration_receipt_sha256"] == first.receipt_sha256
    assert entry["probe_set_hash"] == receipt["probe_set_sha256"]
    assert entry["preregistration_issued_at"] == receipt["issued_at"]
    assert reg.verify([entry]) == (True, [])

    reading = json.loads(latest.read_text(encoding="utf-8"))
    assert reading["schema"] == evidence.LATEST_SCHEMA
    assert reading["authority"] == evidence.AUTHORITY
    assert reading["latest_receipt_sha256"]["eval_preregistration"] == first.receipt_sha256
    assert "path" not in json.dumps(reading).lower()

    replay = evidence.import_envelope(
        envelope,
        registry_path=registry,
        store_dir=store,
        latest_path=latest,
        now=BASE + timedelta(minutes=10),
    )
    assert replay == evidence.ImportResult(
        evidence.PREREGISTRATION_KIND, first.receipt_sha256, 0, False
    )
    assert registry.read_bytes() == registry_before
    assert latest.read_bytes() == latest_before


def test_run_commits_the_exact_result_receipt_not_the_private_result_artifact(
    tmp_path: Path,
) -> None:
    preregistration = _preregistration()
    prereg, _, _ = _import(
        tmp_path, preregistration, now=BASE, name="prereg.json"
    )
    run_receipt = _run(prereg.receipt_sha256)
    result, (registry, store, latest), envelope = _import(
        tmp_path,
        run_receipt,
        now=BASE + timedelta(minutes=3),
        name="run.json",
    )

    entries = reg.read_ledger(str(registry))
    run_entry = entries[-1]
    assert run_entry["responses_hash"] == result.receipt_sha256
    assert run_entry["result_receipt_sha256"] == result.receipt_sha256
    assert run_entry["responses_hash"] != run_receipt["result_artifact_sha256"]
    assert run_entry["result_artifact_sha256"] == run_receipt["result_artifact_sha256"]
    assert "training" not in json.dumps(run_entry).lower()
    assert reg.verify(entries) == (True, [])
    assert evidence.verify_publication(
        registry_path=registry, store_dir=store, latest_path=latest, now=BASE + timedelta(minutes=3)
    ) == (True, [])
    assert _stored(store, result.receipt_sha256).read_bytes() == evidence.canonical_json_bytes(
        run_receipt
    )
    reading = json.loads(latest.read_text(encoding="utf-8"))
    assert reading["latest_receipt_sha256"]["eval_run"] == result.receipt_sha256

    before = registry.read_bytes(), latest.read_bytes()
    replay = evidence.import_envelope(
        envelope,
        registry_path=registry,
        store_dir=store,
        latest_path=latest,
        now=BASE + timedelta(hours=1),
    )
    assert replay.changed is False
    assert (registry.read_bytes(), latest.read_bytes()) == before


def test_run_without_preregistration_fails_before_writing_any_public_state(
    tmp_path: Path,
) -> None:
    registry, store, latest = _locations(tmp_path)
    receipt = _run(_sha("missing-preregistration"))
    envelope = _write_envelope(tmp_path / "run.json", receipt)

    with pytest.raises(evidence.EvidenceImportError, match="not present earlier"):
        evidence.import_envelope(
            envelope,
            registry_path=registry,
            store_dir=store,
            latest_path=latest,
            now=BASE + timedelta(minutes=3),
        )

    assert not registry.exists()
    assert not latest.exists()
    assert not store.exists()


def test_retroactive_preregistration_is_rejected_against_local_registry_time(
    tmp_path: Path,
) -> None:
    prereg, (registry, store, latest), _ = _import(
        tmp_path, _preregistration(), now=BASE, name="prereg.json"
    )
    late = _run(
        prereg.receipt_sha256,
        started=BASE - timedelta(seconds=1),
        completed=BASE + timedelta(minutes=1),
    )
    envelope = _write_envelope(tmp_path / "late.json", late)
    before = registry.read_bytes(), latest.read_bytes()

    with pytest.raises(evidence.EvidenceImportError, match="late preregistration"):
        evidence.import_envelope(
            envelope,
            registry_path=registry,
            store_dir=store,
            latest_path=latest,
            now=BASE + timedelta(minutes=2),
        )

    assert (registry.read_bytes(), latest.read_bytes()) == before
    assert not _stored(store, _receipt_sha256(late)).exists()


def test_run_must_match_the_exact_preregistration_commitments(tmp_path: Path) -> None:
    prereg, (registry, store, latest), _ = _import(
        tmp_path, _preregistration(), now=BASE, name="prereg.json"
    )
    run = _run(prereg.receipt_sha256)
    run["probe_set_sha256"] = _sha("different-probe-set")
    envelope = _write_envelope(tmp_path / "mismatch.json", run)

    with pytest.raises(evidence.EvidenceImportError, match="probe_set_sha256"):
        evidence.import_envelope(
            envelope,
            registry_path=registry,
            store_dir=store,
            latest_path=latest,
            now=BASE + timedelta(minutes=3),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt", "private prompt text"),
        ("labels", ["accept", "reject"]),
        ("reviewer_email", "person@example.com"),
        ("weights", [0.1, 0.2]),
        ("api_secret", "secret-value"),
        ("source_url", "https://private.invalid/result"),
        ("private_path", "/var/lib/myquant/result.json"),
        ("provider", {"model": "internal-model"}),
        ("training_cut_sha256", "0" * 64),
    ],
)
def test_receipt_forbids_private_or_authority_bearing_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    receipt = _preregistration()
    receipt[field] = value
    envelope = _write_envelope(tmp_path / f"{field}.json", receipt)

    with pytest.raises(evidence.EvidenceImportError, match="forbidden/unknown"):
        evidence.load_envelope(envelope, now=BASE)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_artifact_sha256", "https://private.invalid/model"),
        ("probe_set_sha256", "/var/lib/myquant/probes.json"),
        ("evaluation_protocol_sha256", "sha256:" + "a" * 64),
    ],
)
def test_allowed_content_fields_accept_only_bare_lowercase_digests(
    tmp_path: Path, field: str, value: str
) -> None:
    receipt = _preregistration()
    receipt[field] = value
    envelope = _write_envelope(tmp_path / f"bad-{field}.json", receipt)
    with pytest.raises(evidence.EvidenceImportError, match="lowercase 64-character"):
        evidence.load_envelope(envelope, now=BASE)


def test_weakened_or_extended_authority_is_rejected(tmp_path: Path) -> None:
    for index, authority in enumerate(
        (
            {**evidence.AUTHORITY, "grants_deployment": True},
            {**evidence.AUTHORITY, "informational_only": True},
            {key: False for key in evidence.AUTHORITY if key != "grants_training"},
            {key: 0 for key in evidence.AUTHORITY},
        )
    ):
        receipt = _preregistration(evaluation=f"evaluation-{index}")
        receipt["authority"] = authority
        envelope = _write_envelope(tmp_path / f"authority-{index}.json", receipt)
        with pytest.raises(evidence.EvidenceImportError, match="exact all-false"):
            evidence.load_envelope(envelope, now=BASE)


def test_projection_comparison_is_type_exact_for_governance_booleans(
    tmp_path: Path,
) -> None:
    _, (registry, store, latest), _ = _import(
        tmp_path, _preregistration(), now=BASE, name="prereg.json"
    )
    reading = json.loads(latest.read_text(encoding="utf-8"))
    assert reading["public_witness_verified"] is False
    reading["public_witness_verified"] = 0
    latest.write_text(json.dumps(reading), encoding="utf-8")

    ok, problems = evidence.verify_publication(
        registry_path=registry,
        store_dir=store,
        latest_path=latest,
        now=BASE,
    )
    assert not ok
    assert any("does not match" in problem for problem in problems)


@pytest.mark.parametrize(
    "mutation",
    [
        {"envelope_schema": "palimpsest.myquant-model-evidence-envelope.v2"},
        {"receipt_schema": "palimpsest.myquant-eval-preregistration.v2"},
        {"kind": "training_cut"},
    ],
)
def test_unknown_schema_or_kind_fails_closed(tmp_path: Path, mutation: dict) -> None:
    receipt = _preregistration()
    if "receipt_schema" in mutation:
        receipt["schema"] = mutation["receipt_schema"]
    if "kind" in mutation:
        receipt["kind"] = mutation["kind"]
    envelope = {
        "schema": mutation.get("envelope_schema", evidence.ENVELOPE_SCHEMA),
        "receipt_sha256": _receipt_sha256(receipt),
        "receipt": receipt,
    }
    path = tmp_path / "unknown.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(evidence.EvidenceImportError, match="unknown"):
        evidence.load_envelope(path, now=BASE)


def test_hash_mismatch_duplicate_json_key_and_symlink_input_are_rejected(
    tmp_path: Path,
) -> None:
    receipt = _preregistration()
    mismatch = _write_envelope(tmp_path / "mismatch.json", receipt, claimed="0" * 64)
    with pytest.raises(evidence.EvidenceImportError, match="hash mismatch"):
        evidence.load_envelope(mismatch, now=BASE)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema":"%s","schema":"%s","receipt_sha256":"%s","receipt":{}}'
        % (evidence.ENVELOPE_SCHEMA, evidence.ENVELOPE_SCHEMA, "0" * 64),
        encoding="utf-8",
    )
    with pytest.raises(evidence.EvidenceImportError, match="duplicate JSON key"):
        evidence.load_envelope(duplicate, now=BASE)

    link = tmp_path / "envelope-link.json"
    link.symlink_to(mismatch)
    with pytest.raises(evidence.EvidenceImportError, match="regular file"):
        evidence.load_envelope(link, now=BASE)


@pytest.mark.parametrize("numeric_value", ["1e999", "9" * 5000])
def test_nonfinite_or_oversized_numbers_are_cleanly_refused(
    monkeypatch, tmp_path: Path, capsys, numeric_value: str
) -> None:
    path = tmp_path / "numeric-envelope.json"
    path.write_text(
        '{"schema":"%s","receipt_sha256":"%s",'
        '"receipt":{"probe_count":%s}}'
        % (evidence.ENVELOPE_SCHEMA, "0" * 64, numeric_value),
        encoding="utf-8",
    )

    with pytest.raises(evidence.EvidenceImportError):
        evidence.load_envelope(path, now=BASE)
    registry_path, store, latest = _locations(tmp_path)
    monkeypatch.setattr(
        import_cli,
        "import_envelope",
        lambda envelope: evidence.import_envelope(
            envelope,
            registry_path=registry_path,
            store_dir=store,
            latest_path=latest,
        ),
    )
    assert import_cli.main([str(path)]) == 2
    assert "REFUSING" in capsys.readouterr().err


def test_conflicting_preregistration_and_second_result_are_rejected(tmp_path: Path) -> None:
    prereg_receipt = _preregistration()
    prereg, (registry, store, latest), _ = _import(
        tmp_path, prereg_receipt, now=BASE, name="prereg.json"
    )

    conflicting = _preregistration()
    conflicting["probe_set_sha256"] = _sha("replacement-probe-set")
    conflict_envelope = _write_envelope(tmp_path / "conflict.json", conflicting)
    with pytest.raises(evidence.EvidenceImportError, match="evaluation_id"):
        evidence.import_envelope(
            conflict_envelope,
            registry_path=registry,
            store_dir=store,
            latest_path=latest,
            now=BASE + timedelta(seconds=30),
        )

    _import(
        tmp_path,
        _run(prereg.receipt_sha256),
        now=BASE + timedelta(minutes=3),
        name="run.json",
    )
    second = _run(
        prereg.receipt_sha256,
        run="different-run",
        result="different-result",
        started=BASE + timedelta(minutes=4),
        completed=BASE + timedelta(minutes=5),
    )
    second_envelope = _write_envelope(tmp_path / "second.json", second)
    with pytest.raises(evidence.EvidenceImportError, match="evaluation_id"):
        evidence.import_envelope(
            second_envelope,
            registry_path=registry,
            store_dir=store,
            latest_path=latest,
            now=BASE + timedelta(minutes=6),
        )


def test_result_artifact_replay_under_another_evaluation_is_rejected(tmp_path: Path) -> None:
    first_prereg, locations, _ = _import(
        tmp_path, _preregistration(), now=BASE, name="prereg-1.json"
    )
    _import(
        tmp_path,
        _run(first_prereg.receipt_sha256),
        now=BASE + timedelta(minutes=3),
        name="run-1.json",
    )
    second_receipt = _preregistration(
        evaluation="evaluation-2", issued=BASE + timedelta(minutes=3)
    )
    second_prereg, _, _ = _import(
        tmp_path,
        second_receipt,
        now=BASE + timedelta(minutes=4),
        name="prereg-2.json",
    )
    replay = _run(
        second_prereg.receipt_sha256,
        evaluation="evaluation-2",
        run="run-2",
        result="private-result-artifact-1",
        started=BASE + timedelta(minutes=5),
        completed=BASE + timedelta(minutes=6),
    )
    envelope = _write_envelope(tmp_path / "replay.json", replay)
    with pytest.raises(evidence.EvidenceImportError, match="result_artifact_sha256"):
        evidence.import_envelope(
            envelope,
            registry_path=locations[0],
            store_dir=locations[1],
            latest_path=locations[2],
            now=BASE + timedelta(minutes=7),
        )


def test_broken_registry_or_inconsistent_latest_is_never_laundered(tmp_path: Path) -> None:
    first, (registry, store, latest), _ = _import(
        tmp_path, _preregistration(), now=BASE, name="prereg.json"
    )
    next_receipt = _preregistration(
        evaluation="evaluation-2", issued=BASE + timedelta(minutes=1)
    )
    next_envelope = _write_envelope(tmp_path / "next.json", next_receipt)

    reading = json.loads(latest.read_text(encoding="utf-8"))
    reading["latest_receipt_sha256"]["eval_preregistration"] = None
    latest.write_text(json.dumps(reading), encoding="utf-8")
    with pytest.raises(evidence.EvidenceImportError, match="does not match"):
        evidence.import_envelope(
            next_envelope,
            registry_path=registry,
            store_dir=store,
            latest_path=latest,
            now=BASE + timedelta(minutes=2),
        )
    assert not _stored(store, _receipt_sha256(next_receipt)).exists()

    # Restore latest, then break the hash chain itself.
    exact = json.loads(latest.read_text(encoding="utf-8"))
    exact["latest_receipt_sha256"]["eval_preregistration"] = first.receipt_sha256
    latest.write_text(json.dumps(exact), encoding="utf-8")
    entries = reg.read_ledger(str(registry))
    entries[0]["probe_set_hash"] = "0" * 64
    registry.write_text(json.dumps(entries[0]) + "\n", encoding="utf-8")
    with pytest.raises(evidence.EvidenceImportError, match="broken eval registry"):
        evidence.import_envelope(
            next_envelope,
            registry_path=registry,
            store_dir=store,
            latest_path=latest,
            now=BASE + timedelta(minutes=2),
        )


def test_exact_replay_repairs_only_a_missing_latest_projection(tmp_path: Path) -> None:
    receipt = _preregistration()
    first, (registry, store, latest), envelope = _import(
        tmp_path, receipt, now=BASE, name="prereg.json"
    )
    latest.unlink()
    ok, problems = evidence.verify_publication(
        registry_path=registry,
        store_dir=store,
        latest_path=latest,
        now=BASE + timedelta(minutes=5),
    )
    assert not ok and "without the latest" in problems[0]

    repaired = evidence.import_envelope(
        envelope,
        registry_path=registry,
        store_dir=store,
        latest_path=latest,
        now=BASE + timedelta(minutes=5),
    )
    assert repaired.changed is True
    assert repaired.receipt_sha256 == first.receipt_sha256
    assert latest.is_file()
    assert len(reg.read_ledger(str(registry))) == 1


def test_publication_verifier_rejects_a_mutated_content_address(tmp_path: Path) -> None:
    first, (registry, store, latest), _ = _import(
        tmp_path, _preregistration(), now=BASE, name="prereg.json"
    )
    _stored(store, first.receipt_sha256).write_bytes(b"{}")

    ok, problems = evidence.verify_publication(
        registry_path=registry,
        store_dir=store,
        latest_path=latest,
        now=BASE + timedelta(minutes=1),
    )
    assert not ok
    assert "no longer matches its address" in problems[0]


def test_exact_replay_repairs_only_the_verified_predecessor_projections(
    tmp_path: Path,
) -> None:
    prereg, (registry, store, latest), _ = _import(
        tmp_path, _preregistration(), now=BASE, name="prereg.json"
    )
    registry_latest = registry.with_name("eval-registry-latest.json")
    previous_latest = latest.read_bytes()
    previous_registry_latest = registry_latest.read_bytes()
    run_receipt = _run(prereg.receipt_sha256)
    result, _, envelope = _import(
        tmp_path,
        run_receipt,
        now=BASE + timedelta(minutes=3),
        name="run.json",
    )
    current_latest = latest.read_bytes()
    current_registry_latest = registry_latest.read_bytes()

    # Simulate a crash after the append but before either derived projection was
    # replaced. These exact predecessor bytes are the only stale state we repair.
    latest.write_bytes(previous_latest)
    registry_latest.write_bytes(previous_registry_latest)
    replay = evidence.import_envelope(
        envelope,
        registry_path=registry,
        store_dir=store,
        latest_path=latest,
        now=BASE + timedelta(minutes=4),
    )

    assert replay == evidence.ImportResult(
        evidence.RUN_KIND, result.receipt_sha256, 1, True
    )
    assert latest.read_bytes() == current_latest
    assert registry_latest.read_bytes() == current_registry_latest
    assert len(reg.read_ledger(str(registry))) == 2


def test_old_replay_cannot_use_its_predecessor_to_repair_a_newer_tail(
    tmp_path: Path,
) -> None:
    prereg, (registry, store, latest), _ = _import(
        tmp_path, _preregistration(), now=BASE, name="prereg-1.json"
    )
    registry_latest = registry.with_name("eval-registry-latest.json")
    prereg_latest = latest.read_bytes()
    prereg_registry_latest = registry_latest.read_bytes()
    run_result, _, run_envelope = _import(
        tmp_path,
        _run(prereg.receipt_sha256),
        now=BASE + timedelta(minutes=3),
        name="run-1.json",
    )
    _import(
        tmp_path,
        _preregistration(
            evaluation="evaluation-2", issued=BASE + timedelta(minutes=3)
        ),
        now=BASE + timedelta(minutes=4),
        name="prereg-2.json",
    )

    # These are the exact predecessor projections for seq=1, but seq=1 is no
    # longer the tail.  Replaying it must not bless this older snapshot.
    latest.write_bytes(prereg_latest)
    registry_latest.write_bytes(prereg_registry_latest)
    before = registry.read_bytes(), latest.read_bytes(), registry_latest.read_bytes()
    with pytest.raises(evidence.EvidenceImportError, match="exact predecessor"):
        evidence.import_envelope(
            run_envelope,
            registry_path=registry,
            store_dir=store,
            latest_path=latest,
            now=BASE + timedelta(minutes=5),
        )
    assert run_result.registry_seq == 1
    assert (registry.read_bytes(), latest.read_bytes(), registry_latest.read_bytes()) == before


def test_managed_output_symlinks_fail_closed_without_touching_targets(
    tmp_path: Path,
) -> None:
    registry, store, latest = _locations(tmp_path)
    external_store = tmp_path / "external-store"
    external_store.mkdir()
    store.parent.mkdir(parents=True)
    store.symlink_to(external_store, target_is_directory=True)
    envelope = _write_envelope(tmp_path / "prereg.json", _preregistration())

    with pytest.raises(evidence.EvidenceImportError, match="symlink"):
        evidence.import_envelope(
            envelope,
            registry_path=registry,
            store_dir=store,
            latest_path=latest,
            now=BASE,
        )
    assert list(external_store.iterdir()) == []
    assert not registry.exists()


def test_production_import_samples_registry_time_at_the_locked_append(
    monkeypatch, tmp_path: Path
) -> None:
    validation_time = BASE
    append_time = BASE + timedelta(minutes=1)

    class ValidationClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return validation_time

    class AppendClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return append_time

    monkeypatch.setattr(evidence, "datetime", ValidationClock)
    monkeypatch.setattr(reg, "datetime", AppendClock)
    registry, store, latest = _locations(tmp_path)
    envelope = _write_envelope(tmp_path / "prereg.json", _preregistration())
    evidence.import_envelope(
        envelope,
        registry_path=registry,
        store_dir=store,
        latest_path=latest,
    )

    assert reg.read_ledger(str(registry))[0]["ts"] == append_time.isoformat()


def test_publication_verifier_uses_the_supplied_registry_snapshot(
    monkeypatch, tmp_path: Path
) -> None:
    _, (registry, store, latest), _ = _import(
        tmp_path, _preregistration(), now=BASE, name="prereg.json"
    )
    entries = reg.read_ledger(str(registry))

    def unexpected_reread(path):
        raise AssertionError(f"unexpected registry reread: {path}")

    monkeypatch.setattr(evidence, "_verified_registry", unexpected_reread)
    assert evidence.verify_publication(
        registry_path=registry,
        store_dir=store,
        latest_path=latest,
        now=BASE,
        registry_entries=entries,
        _lock_held=True,
    ) == (True, [])


def test_eval_registry_verifier_enforces_receipt_commitment_and_temporal_order(
    tmp_path: Path,
) -> None:
    prereg, (registry, _, _), _ = _import(
        tmp_path, _preregistration(), now=BASE, name="prereg.json"
    )
    _import(
        tmp_path,
        _run(prereg.receipt_sha256),
        now=BASE + timedelta(minutes=3),
        name="run.json",
    )
    entries = reg.read_ledger(str(registry))

    wrong_commitment = json.loads(json.dumps(entries))
    wrong_commitment[1]["responses_hash"] = wrong_commitment[1][
        "result_artifact_sha256"
    ]
    core = {key: value for key, value in wrong_commitment[1].items() if key != "entry_hash"}
    wrong_commitment[1]["entry_hash"] = reg._entry_hash(core)  # noqa: SLF001
    ok, problems = reg.verify(wrong_commitment)
    assert not ok
    assert any("exact result receipt" in problem for problem in problems)

    late = json.loads(json.dumps(entries))
    late[1]["run_started_at"] = _utc(BASE)
    core = {key: value for key, value in late[1].items() if key != "entry_hash"}
    late[1]["entry_hash"] = reg._entry_hash(core)  # noqa: SLF001
    ok, problems = reg.verify(late)
    assert not ok
    assert any("strictly before run start" in problem for problem in problems)

    hidden_field = json.loads(json.dumps(entries))
    hidden_field[1]["labels_sha256"] = _sha("forbidden-label-map")
    core = {key: value for key, value in hidden_field[1].items() if key != "entry_hash"}
    hidden_field[1]["entry_hash"] = reg._entry_hash(core)  # noqa: SLF001
    ok, problems = reg.verify(hidden_field)
    assert not ok
    assert any("closed schema" in problem for problem in problems)


def test_latest_reading_is_discovered_and_sealed(monkeypatch, tmp_path: Path) -> None:
    _, (_, _, latest), _ = _import(
        tmp_path, _preregistration(), now=BASE, name="prereg.json"
    )
    readings = latest.parent
    ledger = tmp_path / "readings-ledger.jsonl"
    monkeypatch.setattr(seal_readings, "READINGS", str(readings))
    monkeypatch.setattr(seal_readings, "LEDGER", str(ledger))
    monkeypatch.setattr(seal_readings, "ROOT", str(tmp_path))

    assert dict(seal_readings.discover())["myquant-model-evidence"] == str(latest)
    sealed, unchanged, problems = seal_readings.seal_all()
    assert (sealed, unchanged, problems) == (2, 0, [])
    sources = {seal["source"] for seal in reg.read_ledger(str(ledger))}
    assert sources == {"eval-registry", "myquant-model-evidence"}
