from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from scripts import stage_pages_rights


ROOT = Path(__file__).resolve().parents[1]
RIGHTS_CLOCK = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)
PUBLICATION_SHA = "1" * 40
DENIED_VALUE_KEYS = {
    *stage_pages_rights.DIRECT_VALUE_KEYS,
    *stage_pages_rights.VALUE_FIELDS,
}
DIRECT_DENIED_BYTES = re.compile(
    rb'"(?:fdr001|fdr007|fdr014|fr001|fr007|fr014|'
    rb'shibor_(?:on|1w|2w|1m|3m|6m|9m|1y)|usdcny_parity)"\s*:\s*"?[+-]?\d',
    re.IGNORECASE,
)
MAPPING_DENIED_BYTES = re.compile(
    rb'"(?:cfets_benchmarks|chinamoney)"\s*:\s*"?[+-]?\d',
    re.IGNORECASE,
)
SCHEMA = json.loads(
    (ROOT / "protocol" / "restricted-publication-v1.schema.json").read_text(
        encoding="utf-8"
    )
)
RECEIPT_SCHEMA = json.loads(
    (ROOT / "protocol" / "pages-rights-release-receipt-v2.schema.json").read_text(
        encoding="utf-8"
    )
)
ENDPOINT_SCHEMA = json.loads(
    (ROOT / "protocol" / "restricted-publication-endpoint-v1.schema.json").read_text(
        encoding="utf-8"
    )
)
FRESHNESS_ATTESTATION_SCHEMA = json.loads(
    (ROOT / "protocol" / "publication-freshness-attestation-v1.schema.json").read_text(
        encoding="utf-8"
    )
)


def _stage(
    root: Path,
    *,
    evaluated_at: datetime,
    admission_at: datetime | None = None,
    publication_sha: str = PUBLICATION_SHA,
) -> dict:
    return stage_pages_rights.stage_pages_tree(
        root,
        publication_sha=publication_sha,
        evaluated_at=evaluated_at,
        admission_at=admission_at or evaluated_at,
    )


def _verify(
    root: Path,
    *,
    evaluated_at: datetime,
    admission_at: datetime | None = None,
    publication_sha: str = PUBLICATION_SHA,
) -> dict:
    return stage_pages_rights.verify_staged_tree(
        root,
        publication_sha=publication_sha,
        evaluated_at=evaluated_at,
        admission_at=admission_at or evaluated_at,
    )


def _materialize_git_archive_universe(destination: Path) -> None:
    """Hard-link every tracked file, independently of the gate's selector."""

    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    relative_paths = [
        Path(raw.decode("utf-8")) for raw in completed.stdout.split(b"\0") if raw
    ]
    assert len(relative_paths) > 35_000
    for relative in relative_paths:
        source = ROOT / relative
        assert source.is_file() and not source.is_symlink()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)


def _write_minimal_denied_tree(destination: Path) -> None:
    policy = destination / stage_pages_rights.POLICY_RELATIVE_PATH
    policy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / stage_pages_rights.POLICY_RELATIVE_PATH, policy)
    ledger = destination / "readings" / "china-econ-observations.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps(
            {
                "source_id": "cfets_benchmarks",
                "series_id": "cn.cfets.synthetic",
                "value": 987654.321,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_pre_quarantine_sources(destination)


def _compact_canonical_sha256(document: dict) -> str:
    payload = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_pre_quarantine_sources(destination: Path) -> None:
    readings = destination / "readings"
    readings.mkdir(parents=True, exist_ok=True)
    for relative in (
        stage_pages_rights.NEWSWIRE_RELATIVE_PATH,
        stage_pages_rights.CHINA_SITUATION_RELATIVE_PATH,
    ):
        (destination / relative).write_bytes((ROOT / relative).read_bytes())


def _validator() -> Draft202012Validator:
    Draft202012Validator.check_schema(SCHEMA)
    return Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def _endpoint_validator() -> Draft202012Validator:
    Draft202012Validator.check_schema(ENDPOINT_SCHEMA)
    return Draft202012Validator(ENDPOINT_SCHEMA, format_checker=FormatChecker())


def _freshness_attestation_validator() -> Draft202012Validator:
    Draft202012Validator.check_schema(FRESHNESS_ATTESTATION_SCHEMA)
    return Draft202012Validator(
        FRESHNESS_ATTESTATION_SCHEMA, format_checker=FormatChecker()
    )


def _receipt_validator() -> Draft202012Validator:
    Draft202012Validator.check_schema(RECEIPT_SCHEMA)
    return Draft202012Validator(RECEIPT_SCHEMA, format_checker=FormatChecker())


def _decision(status: dict, source_id: str) -> dict:
    return next(
        row for row in status["source_decisions"] if row["source_id"] == source_id
    )


def _json_documents(path: Path) -> list[object]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return [json.loads(text)]


def _mentions_denied_lineage(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in {"cfets_benchmarks", "chinamoney"}
            or _mentions_denied_lineage(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_mentions_denied_lineage(child) for child in value)
    return isinstance(value, str) and (
        value.lower() in {"cfets_benchmarks", "chinamoney"}
        or value.lower().startswith("cn.cfets.")
    )


def _contains_independent_denied_derivative(value: object) -> bool:
    if not _mentions_denied_lineage(value):
        return False

    def walk(child: object) -> bool:
        if isinstance(child, dict):
            for key, nested in child.items():
                if key in DENIED_VALUE_KEYS and nested is not None:
                    return True
                if str(key).lower() in {"cfets_benchmarks", "chinamoney"} and type(
                    nested
                ) in {int, float}:
                    return True
                if walk(nested):
                    return True
        elif isinstance(child, list):
            return any(walk(nested) for nested in child)
        return False

    return walk(value)


def _assert_independent_archive_is_clean(
    root: Path, *, denied_sentinels: set[bytes]
) -> None:
    failures = []
    for path in sorted(
        candidate for candidate in root.rglob("*") if candidate.is_file()
    ):
        raw = path.read_bytes()
        if any(sentinel in raw for sentinel in denied_sentinels):
            failures.append(f"sentinel:{path.relative_to(root)}")
        if DIRECT_DENIED_BYTES.search(raw) or MAPPING_DENIED_BYTES.search(raw):
            failures.append(f"raw-value:{path.relative_to(root)}")
        if path.suffix in {".json", ".jsonl"} and any(
            token in raw.lower()
            for token in (b"cfets", b"chinamoney", b"shibor", b"cn.cfets.")
        ):
            for document in _json_documents(path):
                if _contains_independent_denied_derivative(document):
                    failures.append(f"derivative:{path.relative_to(root)}")
                    break
        elif path.suffix == ".html":
            text = raw.decode("utf-8").lower()
            if ("cfets" in text or "chinamoney" in text or "shibor" in text) and (
                "metric-card__value" in text or "cn-num" in text
            ):
                failures.append(f"html-value:{path.relative_to(root)}")
    assert failures == []


def test_exact_git_archive_universe_is_recursively_quarantined(tmp_path: Path):
    _materialize_git_archive_universe(tmp_path)
    ledger_path = tmp_path / "readings/china-econ-observations.jsonl"
    first_observation = json.loads(
        ledger_path.read_text(encoding="utf-8").splitlines()[0]
    )
    denied_sentinels = {
        first_observation["observation_id"].encode(),
        first_observation["raw_sha256"].encode(),
    }
    before = stage_pages_rights.find_denied_value_paths(
        tmp_path, evaluated_at=RIGHTS_CLOCK
    )
    assert {
        "readings/china-econ-observations.jsonl",
        "readings/china-economic-pulse-latest.json",
        "readings/china-econ-forecast-latest.json",
        "readings/china-index-latest.json",
        "readings/cny-fix-gap-latest.json",
        "readings/osint-china-latest.json",
        "readings/reading-analysis-latest.json",
        "readings/data-darkness-latest.json",
        "readings/data-darkness-history.jsonl",
        "readings/evidence-mesh-latest.json",
        "readings/machine-investigations-latest.json",
        "news/analysis/china-economic-evidence-readiness/report.json",
        "china/index.html",
        "news/economy/index.html",
    }.issubset(before)

    status = _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)
    verified = _verify(tmp_path, evaluated_at=RIGHTS_CLOCK)

    assert verified == status
    assert (
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)
        == []
    )
    _assert_independent_archive_is_clean(tmp_path, denied_sentinels=denied_sentinels)
    assert status["status"] == "restricted"
    assert status["availability"] == "unavailable"
    assert status["publication_allowed"] is False
    assert status["rights_evaluated_at"] == "2026-08-26T00:00:00Z"
    assert status["publication_sha"] == PUBLICATION_SHA
    assert status["counts"] == {
        "input_records": 2259,
        "allowed_records": 0,
        "restricted_records": 2259,
        "published_records": 0,
        "quarantined_artifacts": len(status["quarantined_paths"]),
    }
    assert set(before).issubset(status["quarantined_paths"])
    assert {
        "china/index.html",
        "china/sources/index.html",
        "news/economy/index.html",
        "readings/index.html",
    }.issubset(status["quarantined_paths"])
    assert "readings/catalog.json" in status["quarantined_paths"]
    assert ".well-known/ai-catalog.json" not in status["quarantined_paths"]
    assert any("co-located" in limitation for limitation in status["limitations"])
    _validator().validate(status)

    for relative in status["quarantined_paths"]:
        path = tmp_path / relative
        if path.suffix == ".html":
            text = path.read_text(encoding="utf-8")
            assert 'data-palimpsest-publication-status="restricted"' in text
            assert "Values unavailable: publication restricted" in text
        elif path.suffix in {".json", ".jsonl"}:
            text = path.read_text(encoding="utf-8")
            documents = (
                [json.loads(line) for line in text.splitlines() if line.strip()]
                if path.suffix == ".jsonl"
                else [json.loads(text)]
            )
            assert len(documents) == 1
            assert documents[0]["schema_version"] == (
                "palimpsest-restricted-publication-endpoint.v1"
            )
            _endpoint_validator().validate(documents[0])
            assert documents[0]["master_status"] == {
                "bytes": len(
                    (tmp_path / stage_pages_rights.STATUS_RELATIVE_PATH).read_bytes()
                ),
                "path": "/readings/china-publication-rights-latest.json",
                "sha256": hashlib.sha256(
                    (tmp_path / stage_pages_rights.STATUS_RELATIVE_PATH).read_bytes()
                ).hexdigest(),
            }
            assert len(text.encode("utf-8")) < 16_384
        else:
            text = path.read_text(encoding="utf-8")
            assert text.startswith("Palimpsest publication status: restricted\n")
            assert "Published records: 0" in text


def test_denied_cfets_and_allowed_empty_wdi_remain_distinct(tmp_path: Path):
    _write_minimal_denied_tree(tmp_path)
    status = _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)

    cfets = _decision(status, "cfets_benchmarks")
    assert cfets["decision"] == "deny"
    assert cfets["configured_decision"] == "deny"
    assert cfets["availability"] == "restricted"
    assert cfets["values_allowed"] is False
    assert cfets["seiche_export_allowed"] is False
    assert cfets["input_records"] == 1
    assert cfets["published_records"] == 0

    wdi = _decision(status, "world_bank_wdi")
    assert wdi["decision"] == "allow"
    assert wdi["configured_decision"] == "allow"
    assert wdi["availability"] == "unavailable"
    assert wdi["values_allowed"] is True
    assert wdi["seiche_export_allowed"] is True
    assert wdi["input_records"] == 0
    assert wdi["published_records"] == 0


def test_restricted_status_never_infers_zero_calm_or_a_carrier(tmp_path: Path):
    _write_minimal_denied_tree(tmp_path)
    status = _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)
    encoded = json.dumps(status, sort_keys=True).lower()

    assert "unavailable or restricted evidence is not zero, calm, healthy" in encoded
    assert "evidence carrier" in encoded
    assert "observation" not in status
    assert "value" not in status
    assert "direction" not in status
    assert "composite" not in status
    assert "authority" not in status
    assert status["counts"]["published_records"] == 0


def test_pre_quarantine_freshness_attestation_is_compact_and_lineage_bound(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    newswire_path = tmp_path / stage_pages_rights.NEWSWIRE_RELATIVE_PATH
    situation_path = tmp_path / stage_pages_rights.CHINA_SITUATION_RELATIVE_PATH
    original_newswire = json.loads(newswire_path.read_text(encoding="utf-8"))
    original_situation = json.loads(situation_path.read_text(encoding="utf-8"))

    status = _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)
    attestation_path = tmp_path / stage_pages_rights.FRESHNESS_ATTESTATION_RELATIVE_PATH
    attestation_raw = attestation_path.read_bytes()
    attestation = json.loads(attestation_raw)
    rights_raw = (tmp_path / stage_pages_rights.STATUS_RELATIVE_PATH).read_bytes()

    _freshness_attestation_validator().validate(attestation)
    assert len(attestation_raw) < 4096
    assert set(attestation) == {
        "schema_version",
        "publication_sha",
        "attested_at",
        "mode",
        "publication_allowed",
        "artifacts",
        "rights_status",
        "limitations",
    }
    assert attestation["publication_sha"] == PUBLICATION_SHA
    assert attestation["attested_at"] == "2026-08-26T00:00:00Z"
    assert attestation["mode"] == "rights-suppressed"
    assert attestation["publication_allowed"] is False
    assert attestation["artifacts"] == {
        "newswire": {
            "path": "readings/newswire-latest.json",
            "schema_version": "palimpsest-newswire.v1",
            "generated_at": original_newswire["generated_at"],
            "canonical_sha256": _compact_canonical_sha256(original_newswire),
        },
        "china_situation": {
            "path": "readings/china-situation-latest.json",
            "schema_version": "palimpsest-china-situation.v1",
            "generated_at": original_situation["generated_at"],
            "canonical_sha256": _compact_canonical_sha256(original_situation),
            "inputs": {
                "newswire_generated_at": original_newswire["generated_at"],
                "newswire_canonical_sha256": _compact_canonical_sha256(
                    original_newswire
                ),
            },
        },
    }
    assert attestation["rights_status"] == {
        "path": "readings/china-publication-rights-latest.json",
        "sha256": hashlib.sha256(rights_raw).hexdigest(),
        "bytes": len(rights_raw),
    }
    encoded = json.dumps(attestation, sort_keys=True)
    for forbidden_key in (
        "events",
        "situations",
        "source_id",
        "observations",
        "record_count",
        "title",
        "url",
        "value",
    ):
        assert f'"{forbidden_key}":' not in encoded
    assert "readings/china-situation-latest.json" in status["quarantined_paths"]
    assert "readings/china-situation-latest.json" in stage_pages_rights.ALWAYS_RESTRICT
    assert json.loads(newswire_path.read_text())["schema_version"] == (
        "palimpsest-restricted-publication-endpoint.v1"
    )
    assert json.loads(situation_path.read_text())["schema_version"] == (
        "palimpsest-restricted-publication-endpoint.v1"
    )
    assert _verify(tmp_path, evaluated_at=RIGHTS_CLOCK) == status


def test_release_receipt_v2_rejects_attested_identity_tamper_during_check(
    tmp_path: Path,
    capsys,
):
    _write_minimal_denied_tree(tmp_path)
    status = _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)
    receipt_path = tmp_path.parent / f"{tmp_path.name}-rights-receipt.json"
    receipt = stage_pages_rights.write_release_receipt(
        receipt_path,
        root=tmp_path,
        status=status,
        publication_sha=PUBLICATION_SHA,
        evaluated_at=RIGHTS_CLOCK,
        admission_at=RIGHTS_CLOCK,
    )
    _receipt_validator().validate(receipt)
    attestation_path = tmp_path / stage_pages_rights.FRESHNESS_ATTESTATION_RELATIVE_PATH
    attestation_raw = attestation_path.read_bytes()
    assert receipt["schema_version"] == "palimpsest.pages-rights-release-receipt.v2"
    assert receipt["freshness_attestation"] == {
        "path": "readings/publication-freshness-attestation-latest.json",
        "sha256": hashlib.sha256(attestation_raw).hexdigest(),
        "bytes": len(attestation_raw),
    }

    forged = json.loads(attestation_raw)
    forged["artifacts"]["newswire"]["generated_at"] = "2026-08-25T23:00:00Z"
    forged["artifacts"]["newswire"]["canonical_sha256"] = "0" * 64
    forged["artifacts"]["china_situation"]["inputs"] = {
        "newswire_generated_at": "2026-08-25T23:00:00Z",
        "newswire_canonical_sha256": "0" * 64,
    }
    attestation_path.write_bytes(stage_pages_rights._canonical_json(forged))
    _freshness_attestation_validator().validate(forged)
    assert _verify(tmp_path, evaluated_at=RIGHTS_CLOCK) == status

    result = stage_pages_rights.main(
        [
            "--root",
            str(tmp_path),
            "--publication-sha",
            PUBLICATION_SHA,
            "--evaluated-at",
            "2026-08-26T00:00:00Z",
            "--admission-at",
            "2026-08-26T00:00:00Z",
            "--receipt",
            str(receipt_path),
            "--check",
        ]
    )
    assert result == 2
    assert "Pages rights release receipt has drifted" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("identity", "forged_path"),
    [
        ("status", "readings/other-status.json"),
        ("policy", "config/other-policy.json"),
        ("freshness_attestation", "readings/other-attestation.json"),
    ],
)
def test_release_receipt_v2_pins_every_artifact_path(
    tmp_path: Path,
    identity: str,
    forged_path: str,
):
    _write_minimal_denied_tree(tmp_path)
    status = _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)
    receipt = stage_pages_rights.build_release_receipt(
        root=tmp_path,
        status=status,
        publication_sha=PUBLICATION_SHA,
        evaluated_at=RIGHTS_CLOCK,
        admission_at=RIGHTS_CLOCK,
    )
    receipt[identity]["path"] = forged_path

    errors = sorted(
        _receipt_validator().iter_errors(receipt), key=lambda error: list(error.path)
    )

    assert errors


def test_freshness_attestation_refuses_mismatched_situation_lineage(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    situation_path = tmp_path / stage_pages_rights.CHINA_SITUATION_RELATIVE_PATH
    situation = json.loads(situation_path.read_text(encoding="utf-8"))
    situation["inputs"]["newswire_sha256"] = "0" * 64
    situation_path.write_text(
        json.dumps(situation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="does not bind the exact newswire input",
    ):
        _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)


@pytest.mark.parametrize(
    ("relative", "required_field"),
    [
        (stage_pages_rights.NEWSWIRE_RELATIVE_PATH, "scope"),
        (stage_pages_rights.CHINA_SITUATION_RELATIVE_PATH, "coverage"),
    ],
)
def test_freshness_attestation_requires_complete_source_contracts(
    tmp_path: Path,
    relative: Path,
    required_field: str,
):
    _write_minimal_denied_tree(tmp_path)
    source_path = tmp_path / relative
    document = json.loads(source_path.read_text(encoding="utf-8"))
    del document[required_field]
    source_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="fails its source contract",
    ):
        _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)


@pytest.mark.parametrize(
    "forged_member", ['"duplicate":1,"duplicate":2,', '"nan":NaN,']
)
def test_freshness_attestation_rejects_non_strict_source_json(
    tmp_path: Path,
    forged_member: str,
):
    _write_minimal_denied_tree(tmp_path)
    newswire_path = tmp_path / stage_pages_rights.NEWSWIRE_RELATIVE_PATH
    original = newswire_path.read_text(encoding="utf-8")
    newswire_path.write_text(
        "{" + forged_member + original.lstrip()[1:],
        encoding="utf-8",
    )

    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="invalid public JSON artifact",
    ):
        _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)


def test_sanitized_osint_is_always_quarantined_by_designation(tmp_path: Path):
    _write_minimal_denied_tree(tmp_path)
    osint_path = tmp_path / "readings" / "osint-china-latest.json"
    osint_path.write_text(
        json.dumps(
            {
                "schema_version": "osint-china.v1",
                "generated_at": "2026-08-25T23:59:59Z",
                "input_commit": "b" * 40,
                "signals": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    status = _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)
    stub = json.loads(osint_path.read_text(encoding="utf-8"))

    assert "readings/osint-china-latest.json" in stage_pages_rights.ALWAYS_RESTRICT
    assert "readings/osint-china-latest.json" in status["quarantined_paths"]
    assert stub["schema_version"] == ("palimpsest-restricted-publication-endpoint.v1")
    assert stub["artifact"] == {
        "path": "readings/osint-china-latest.json",
        "media_type": "application/json",
    }


def test_future_denied_values_and_derivatives_are_detected_then_removed(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    derivative = tmp_path / "readings" / "new-derived-surface.json"
    derivative.write_text(
        json.dumps(
            {
                "instrument_id": "cny-fix-gap",
                "field": "gap_pct",
                "current_value": -987654.321,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert stage_pages_rights.find_denied_value_paths(
        tmp_path, evaluated_at=RIGHTS_CLOCK
    ) == [
        "readings/china-econ-observations.jsonl",
        "readings/china-situation-latest.json",
        "readings/new-derived-surface.json",
    ]
    status = _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)

    assert (
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)
        == []
    )
    assert status["counts"]["input_records"] == 1
    assert status["counts"]["restricted_records"] == 1
    assert status["counts"]["published_records"] == 0
    assert "987654.321" not in derivative.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("relative", "payload"),
    [
        (
            "archive/rows.csv",
            b"source_id,series_id,value\r\ncfets_benchmarks,cn.cfets.synthetic,0\r\n",
        ),
        (
            "archive/rows.tsv",
            b"source_id\tseries_id\tcurrent_value\n"
            b"cfets_benchmarks\tcn.cfets.synthetic\t1e-9\n",
        ),
        (
            "archive/rows.data",
            b'source_id;score\r\ncfets_benchmarks;"987654.321"\r\n',
        ),
        (
            "archive/quoted",
            '\ufeff"source_id","previous_value"\r\n"cfets_benchmarks","0"\r\n'.encode(
                "utf-8"
            ),
        ),
        (
            "archive/direct.csv",
            b'field,value\nfdr007,"987654.321"\n',
        ),
    ],
)
def test_delimited_derivatives_are_detected_by_content(
    tmp_path: Path, relative: str, payload: bytes
):
    _write_minimal_denied_tree(tmp_path)
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

    assert relative in stage_pages_rights.find_denied_value_paths(
        tmp_path, evaluated_at=RIGHTS_CLOCK
    )


def test_large_single_line_html_never_enters_csv_materialization(
    monkeypatch,
):
    payload = (
        "<!doctype html><html data-source_id='documentation' "
        "data-value='metadata'>" + "x" * (4 * 1024 * 1024) + "</html>"
    )

    def unexpected_dict_reader(*args, **kwargs):
        raise AssertionError("large non-delimited HTML reached csv.DictReader")

    monkeypatch.setattr(stage_pages_rights.csv, "DictReader", unexpected_dict_reader)
    started = time.monotonic()
    assert stage_pages_rights._delimited_documents(payload) == []
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("suffix", ["", ".data", ".png"])
@pytest.mark.parametrize("kind", ["json", "csv"])
def test_novel_or_mislabelled_text_derivatives_are_not_skipped(
    tmp_path: Path, suffix: str, kind: str
):
    _write_minimal_denied_tree(tmp_path)
    path = tmp_path / "archive" / f"derivative{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "json":
        path.write_text(
            json.dumps(
                {
                    "source_id": "cfets_benchmarks",
                    "series_id": "cn.cfets.synthetic",
                    "value": 987654.321,
                }
            ),
            encoding="utf-8",
        )
    else:
        path.write_text(
            "source_id,series_id,value\n"
            "cfets_benchmarks,cn.cfets.synthetic,987654.321\n",
            encoding="utf-8",
        )

    relative = path.relative_to(tmp_path).as_posix()
    assert relative in stage_pages_rights.find_denied_value_paths(
        tmp_path, evaluated_at=RIGHTS_CLOCK
    )


@pytest.mark.parametrize("encoding", ["base64", "percent", "html-entity"])
def test_common_text_encodings_cannot_conceal_a_derivative(
    tmp_path: Path, encoding: str
):
    _write_minimal_denied_tree(tmp_path)
    denied = json.dumps(
        {
            "source_id": "cfets_benchmarks",
            "series_id": "cn.cfets.synthetic",
            "value": 987654.321,
        },
        separators=(",", ":"),
    )
    if encoding == "base64":
        payload = json.dumps({"payload": base64.b64encode(denied.encode()).decode()})
    elif encoding == "percent":
        payload = quote(denied, safe="")
    else:
        payload = (
            denied.replace("&", "&amp;").replace('"', "&quot;").replace(":", "&#58;")
        )
    path = tmp_path / "archive" / f"encoded-{encoding}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")

    assert path.relative_to(tmp_path).as_posix() in (
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)
    )


def test_encoded_token_and_expansion_caps_fail_closed(tmp_path: Path, monkeypatch):
    _write_minimal_denied_tree(tmp_path)
    token = base64.b64encode(b'{"source_id":"world_bank_wdi","value":1}')
    many = tmp_path / "archive" / "many-encoded.txt"
    many.parent.mkdir(parents=True, exist_ok=True)
    many.write_bytes(b"\n".join(b'"payload":"' + token + b'"' for _ in range(3)))
    monkeypatch.setattr(stage_pages_rights, "MAX_ENCODED_TOKENS", 2)
    with pytest.raises(stage_pages_rights.PagesRightsError, match="token scan cap"):
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)

    many.unlink()
    monkeypatch.setattr(stage_pages_rights, "MAX_ENCODED_TOKENS", 4096)
    oversized = tmp_path / "archive" / "oversized-encoded.txt"
    oversized.write_bytes(b'"payload":"' + base64.b64encode(b"x" * 65) + b'"')
    monkeypatch.setattr(stage_pages_rights, "MAX_DECODED_BYTES", 64)
    with pytest.raises(stage_pages_rights.PagesRightsError, match="expansion cap"):
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)


def _zip_payload(name: str, payload: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, payload)
    return output.getvalue()


def _mark_zip_encrypted(raw: bytes) -> bytes:
    changed = bytearray(raw)
    local = changed.find(b"PK\x03\x04")
    central = changed.find(b"PK\x01\x02")
    assert local >= 0 and central >= 0
    changed[local + 6 : local + 8] = (
        int.from_bytes(changed[local + 6 : local + 8], "little") | 1
    ).to_bytes(2, "little")
    changed[central + 8 : central + 10] = (
        int.from_bytes(changed[central + 8 : central + 10], "little") | 1
    ).to_bytes(2, "little")
    return bytes(changed)


def test_nested_and_unsafe_containers_fail_closed(tmp_path: Path, monkeypatch):
    _write_minimal_denied_tree(tmp_path)
    denied = json.dumps({"source_id": "cfets_benchmarks", "value": 987654.321}).encode()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    (archive_dir / "denied.json.gz").write_bytes(gzip.compress(denied))
    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="encoded container contains a denied derivative",
    ):
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)
    (archive_dir / "denied.json.gz").unlink()

    (archive_dir / "denied.zip").write_bytes(_zip_payload("nested/data.json", denied))
    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="encoded container contains a denied derivative",
    ):
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)
    (archive_dir / "denied.zip").unlink()

    (archive_dir / "traversal.zip").write_bytes(_zip_payload("../data.json", denied))
    with pytest.raises(stage_pages_rights.PagesRightsError, match="unsafe member"):
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)
    (archive_dir / "traversal.zip").unlink()

    encrypted = _mark_zip_encrypted(_zip_payload("data.json", denied))
    (archive_dir / "encrypted.zip").write_bytes(encrypted)
    with pytest.raises(stage_pages_rights.PagesRightsError, match="unsafe member"):
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)
    (archive_dir / "encrypted.zip").unlink()

    monkeypatch.setattr(stage_pages_rights, "MAX_DECODED_BYTES", 1024)
    (archive_dir / "expansion.gz").write_bytes(gzip.compress(b"x" * 2048))
    with pytest.raises(stage_pages_rights.PagesRightsError, match="expansion cap"):
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        (
            "utf16.data",
            json.dumps({"source_id": "cfets_benchmarks", "value": 987654.321}).encode(
                "utf-16"
            ),
        ),
        (
            "nul.data",
            b"c\x00f\x00e\x00t\x00s\x00 value 987654.321",
        ),
        (
            "sqlite.db",
            b"SQLite format 3\x00cfets_benchmarks value 987654.321",
        ),
        (
            "derivative.parquet",
            b"PAR1\x00cfets_benchmarks value 987654.321\x00PAR1",
        ),
    ],
)
def test_utf16_nul_and_opaque_derivatives_never_silently_pass(
    tmp_path: Path, name: str, payload: bytes
):
    _write_minimal_denied_tree(tmp_path)
    path = tmp_path / "archive" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    if name == "utf16.data":
        assert path.relative_to(tmp_path).as_posix() in (
            stage_pages_rights.find_denied_value_paths(
                tmp_path, evaluated_at=RIGHTS_CLOCK
            )
        )
    else:
        with pytest.raises(
            stage_pages_rights.PagesRightsError,
            match="opaque public artifact lacks exact path-and-digest review",
        ):
            stage_pages_rights.find_denied_value_paths(
                tmp_path, evaluated_at=RIGHTS_CLOCK
            )


def test_xlsx_style_container_with_derivative_is_refused(tmp_path: Path):
    _write_minimal_denied_tree(tmp_path)
    denied = json.dumps({"source_id": "cfets_benchmarks", "value": 987654.321}).encode()
    path = tmp_path / "archive" / "derivative.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_zip_payload("xl/sharedStrings.xml", denied))
    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="encoded container contains a denied derivative",
    ):
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)


def test_exact_reviewed_binary_is_allowed_and_digest_drift_is_refused(tmp_path: Path):
    _write_minimal_denied_tree(tmp_path)
    binary = tmp_path / "assets" / "reviewed.png"
    binary.parent.mkdir(parents=True, exist_ok=True)
    raw = b"\x89PNG\r\n\x1a\n\x00\xff\x00reviewed-image"
    binary.write_bytes(raw)
    allowlist = {
        "schema_version": "palimpsest.pages-public-binary-allowlist.v1",
        "files": [
            {
                "path": "assets/reviewed.png",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
        ],
    }
    allowlist_path = tmp_path / stage_pages_rights.BINARY_ALLOWLIST_RELATIVE_PATH
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    allowlist_path.write_bytes(stage_pages_rights._canonical_json(allowlist))

    assert stage_pages_rights.find_denied_value_paths(
        tmp_path, evaluated_at=RIGHTS_CLOCK
    ) == [
        "readings/china-econ-observations.jsonl",
        "readings/china-situation-latest.json",
    ]
    binary.write_bytes(raw + b"drift")
    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="opaque public artifact lacks exact path-and-digest review",
    ):
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)


def test_markers_unknown_sources_and_transitive_lineage_cannot_bypass_gate(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    fixtures = {
        "china/forged-restricted.json": {
            "schema_version": "palimpsest-restricted-publication.v1",
            "source_id": "cfets_benchmarks",
            "value": 987654.321,
        },
        "china/unreviewed-source.json": {
            "source_id": "unreviewed_vendor",
            "series_id": "cn.unreviewed.synthetic",
            "value": 987654.321,
        },
        "news/analysis/evidence/derived.json": {
            "upstream_groups": ["cfets_benchmarks"],
            "value": "warming_up",
        },
        "readings/data-darkness-synthetic.json": {
            "darkness_index": 0.0,
            "days_since": {"cfets_benchmarks": 0},
        },
    }
    for relative, document in fixtures.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    forged_html = tmp_path / "china/forged-restricted.html"
    forged_html.write_text(
        '<html data-palimpsest-publication-status="restricted">'
        '<p>cfets_benchmarks</p><span class="metric-card__value">987654.321</span>'
        "</html>",
        encoding="utf-8",
    )

    before = set(
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)
    )
    assert set(fixtures) | {"china/forged-restricted.html"} <= before

    status = _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)
    assert set(fixtures) | {"china/forged-restricted.html"} <= set(
        status["quarantined_paths"]
    )
    _verify(tmp_path, evaluated_at=RIGHTS_CLOCK)
    assert all(
        "987654.321" not in (tmp_path / relative).read_text(encoding="utf-8")
        for relative in set(fixtures) | {"china/forged-restricted.html"}
    )


def test_policy_clock_expires_allow_decisions_and_rejects_stale_status(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    wdi = tmp_path / "readings/china-econ-wdi-observations.jsonl"
    wdi.write_text(
        json.dumps(
            {
                "source_id": "world_bank_wdi",
                "series_id": "cn.wdi.synthetic",
                "value": 987654.321,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    before_expiry = datetime(2027, 8, 23, 23, 59, 59, tzinfo=UTC)
    at_expiry = datetime(2027, 8, 24, 0, 0, tzinfo=UTC)

    assert "readings/china-econ-wdi-observations.jsonl" not in (
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=before_expiry)
    )
    _stage(tmp_path, evaluated_at=before_expiry)
    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="changed between edition and admission clocks",
    ):
        _verify(
            tmp_path,
            evaluated_at=before_expiry,
            admission_at=at_expiry,
        )

    expired_root = tmp_path / "expired"
    _write_minimal_denied_tree(expired_root)
    expired_wdi = expired_root / "readings/china-econ-wdi-observations.jsonl"
    expired_wdi.write_text(wdi.read_text(encoding="utf-8"), encoding="utf-8")
    expired = _stage(expired_root, evaluated_at=at_expiry)
    decision = _decision(expired, "world_bank_wdi")
    assert decision["decision"] == "expired"
    assert decision["configured_decision"] == "allow"
    assert decision["availability"] == "restricted"
    assert decision["values_allowed"] is False
    assert decision["seiche_export_allowed"] is False
    assert expired["rights_evaluated_at"] == "2027-08-24T00:00:00Z"
    assert "readings/china-econ-wdi-observations.jsonl" in expired["quarantined_paths"]


def test_exact_status_and_policy_receipt_are_verified_not_self_attested(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)
    endpoint = tmp_path / "readings/china-econ-observations.jsonl"
    forged = json.loads(endpoint.read_text(encoding="utf-8"))
    forged["artifact"]["path"] = "readings/not-the-endpoint.jsonl"
    endpoint.write_text(json.dumps(forged) + "\n", encoding="utf-8")

    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="not exact",
    ):
        _verify(tmp_path, evaluated_at=RIGHTS_CLOCK)

    _write_pre_quarantine_sources(tmp_path)
    _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)
    master = tmp_path / stage_pages_rights.STATUS_RELATIVE_PATH
    forged_master = json.loads(master.read_text(encoding="utf-8"))
    forged_master["policy"]["sha256"] = "0" * 64
    master.write_text(
        json.dumps(forged_master, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="exact policy-derived stub",
    ):
        _verify(tmp_path, evaluated_at=RIGHTS_CLOCK)

    _write_pre_quarantine_sources(tmp_path)
    _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)
    forged_master = json.loads(master.read_text(encoding="utf-8"))
    forged_master["rights_evaluated_at"] = "2026-08-27T00:00:00Z"
    master.write_text(
        json.dumps(forged_master, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="evaluation clock is in the future",
    ):
        _verify(tmp_path, evaluated_at=RIGHTS_CLOCK)


def test_compact_endpoint_stub_is_bounded_and_master_digest_bound(tmp_path: Path):
    _write_minimal_denied_tree(tmp_path)
    _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)
    master_path = tmp_path / stage_pages_rights.STATUS_RELATIVE_PATH
    master_raw = master_path.read_bytes()
    endpoint = tmp_path / "readings/china-econ-observations.jsonl"
    endpoint_raw = endpoint.read_bytes()
    document = json.loads(endpoint_raw)

    _endpoint_validator().validate(document)
    assert len(endpoint_raw) < 16_384
    assert "quarantined_paths" not in document
    assert "source_decisions" not in document
    assert document["master_status"] == {
        "path": "/readings/china-publication-rights-latest.json",
        "sha256": hashlib.sha256(master_raw).hexdigest(),
        "bytes": len(master_raw),
    }
    assert "987654.321" not in endpoint_raw.decode("utf-8")

    document["master_status"]["sha256"] = "0" * 64
    endpoint.write_bytes(stage_pages_rights._canonical_json(document, jsonl=True))
    with pytest.raises(stage_pages_rights.PagesRightsError, match="not exact"):
        _verify(tmp_path, evaluated_at=RIGHTS_CLOCK)


def test_stage_bytes_and_release_receipt_are_deterministic(tmp_path: Path):
    roots = [tmp_path / "first", tmp_path / "second"]
    receipts = []
    inventories = []
    for index, root in enumerate(roots):
        _write_minimal_denied_tree(root)
        status = _stage(root, evaluated_at=RIGHTS_CLOCK)
        receipt_path = tmp_path / f"receipt-{index}.json"
        stage_pages_rights.write_release_receipt(
            receipt_path,
            root=root,
            status=status,
            publication_sha=PUBLICATION_SHA,
            evaluated_at=RIGHTS_CLOCK,
            admission_at=RIGHTS_CLOCK,
        )
        receipts.append(receipt_path.read_bytes())
        inventories.append(
            {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
        )

    assert inventories[0] == inventories[1]
    assert receipts[0] == receipts[1]


def test_ephemeral_stage_skips_fsync_but_receipt_remains_durable(
    tmp_path: Path, monkeypatch
):
    _write_minimal_denied_tree(tmp_path)
    fsync_calls = []
    monkeypatch.setattr(
        stage_pages_rights.os,
        "fsync",
        lambda descriptor: fsync_calls.append(descriptor),
    )
    status = _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)
    assert fsync_calls == []

    receipt_path = tmp_path.parent / f"{tmp_path.name}-rights-receipt.json"
    stage_pages_rights.write_release_receipt(
        receipt_path,
        root=tmp_path,
        status=status,
        publication_sha=PUBLICATION_SHA,
        evaluated_at=RIGHTS_CLOCK,
        admission_at=RIGHTS_CLOCK,
    )
    assert len(fsync_calls) == 1


def test_pages_workflow_stages_and_verifies_rights_before_upload():
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8"
    )
    stage_call = 'python3 -m scripts.stage_pages_rights "${rights_args[@]}"'
    check_call = stage_call + " --check"
    upload = "actions/upload-pages-artifact@"
    sentinel_scan = "Pages artifact retained denied CFETS source bytes"

    assert workflow.count(stage_call) == 2
    assert (
        workflow.index(stage_call) < workflow.index(check_call) < workflow.index(upload)
    )
    assert '--publication-sha "$PUBLICATION_SHA"' in workflow
    assert '--evaluated-at "$rights_edition_at"' in workflow
    assert '--admission-at "$rights_admission_at"' in workflow
    assert '--receipt "$rights_receipt"' in workflow
    assert "pages-rights-release-receipt.json" in workflow
    assert "Upload the Pages rights release receipt" in workflow
    assert "verify-live-rights-closure:" in workflow
    assert '--expected-publication-sha "$PUBLICATION_SHA"' in workflow
    assert "git archive --format=tar" in workflow
    assert "denied-ledger sentinel set is empty" in workflow
    assert "command -v rg >/dev/null" in workflow
    assert sentinel_scan in workflow
    assert (
        workflow.index(check_call)
        < workflow.index(sentinel_scan)
        < workflow.index(upload)
    )


def test_public_discovery_describes_restriction_instead_of_denied_values():
    catalog = json.loads(
        (ROOT / ".well-known/ai-catalog.json").read_text(encoding="utf-8")
    )
    entry = next(
        item
        for item in catalog["entries"]
        if item["identifier"]
        == "urn:air:palimpsest.info:dataset:china-economic-observations"
    )
    assert entry["metadata"]["access"] == "metadata-only-restricted"
    assert entry["metadata"]["manifest"].endswith(
        "/readings/china-publication-rights-latest.json"
    )
    assert "no observations or derivatives" in entry["description"].lower()
    assert "fdr007 observation" not in json.dumps(entry).lower()
    openapi = next(
        item
        for item in catalog["entries"]
        if item["identifier"] == "urn:air:palimpsest.info:openapi:public-readings"
    )
    index = next(
        item
        for item in catalog["entries"]
        if item["identifier"]
        == "urn:air:palimpsest.info:dataset:china-observatory-index"
    )
    mcp = next(
        item
        for item in catalog["entries"]
        if item["identifier"] == "urn:air:palimpsest.info:mcp:evidence-observatory"
    )
    assert "download economic observation ledger" not in openapi["capabilities"]
    assert index["metadata"]["access"] == "metadata-only-restricted"
    mcp_description = mcp["description"].lower()
    assert "native fail-closed" in mcp_description
    assert "explicitly unavailable" in mcp_description
    assert "separate deployment" not in mcp_description
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8").lower()
    assert "metadata-only restricted stubs" in llms
    assert "tool availability does not" in llms
    assert "grant value-publication rights" in llms
    assert "lineage-filtered rebuild" in llms
