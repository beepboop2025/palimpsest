from __future__ import annotations

import base64
import csv
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
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from core import newsroom
from scripts import build_newsroom, share_cards, stage_pages_rights


ROOT = Path(__file__).resolve().parents[1]
RIGHTS_CLOCK = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
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
    (ROOT / "protocol" / "pages-rights-release-receipt-v3.schema.json").read_text(
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
REGIONAL_CAPTURED_INDEX_SCHEMA = json.loads(
    (ROOT / "protocol" / "regional-captured-index-v1.schema.json").read_text(
        encoding="utf-8"
    )
)
REGIONAL_DATA_DUMP_SCHEMA = json.loads(
    (ROOT / "protocol" / "regional-data-dump-v1.schema.json").read_text(
        encoding="utf-8"
    )
)
REGIONAL_EDITORIAL_SCHEMA = json.loads(
    (ROOT / "protocol" / "regional-editorial-evidence-v1.schema.json").read_text(
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


def _embedded_content_sha256(document: dict) -> str:
    payload = {key: value for key, value in document.items() if key != "content_sha256"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def test_regional_archives_validate_and_keep_article_bodies_outside_publication() -> (
    None
):
    for schema in (
        REGIONAL_CAPTURED_INDEX_SCHEMA,
        REGIONAL_DATA_DUMP_SCHEMA,
        REGIONAL_EDITORIAL_SCHEMA,
    ):
        Draft202012Validator.check_schema(schema)

    registry = Registry().with_resource(
        REGIONAL_CAPTURED_INDEX_SCHEMA["$id"],
        Resource.from_contents(REGIONAL_CAPTURED_INDEX_SCHEMA),
    )
    captured_validator = Draft202012Validator(
        REGIONAL_CAPTURED_INDEX_SCHEMA,
        format_checker=FormatChecker(),
    )
    dump_validator = Draft202012Validator(
        REGIONAL_DATA_DUMP_SCHEMA,
        registry=registry,
        format_checker=FormatChecker(),
    )
    assert (
        REGIONAL_CAPTURED_INDEX_SCHEMA["$defs"]["source"]["properties"]["feed_url"][
            "format"
        ]
        == "iri"
    )
    assert (
        REGIONAL_CAPTURED_INDEX_SCHEMA["$defs"]["publisher_link"]["properties"]["url"][
            "format"
        ]
        == "iri"
    )
    Draft202012Validator(
        {"type": "string", "format": "iri"},
        format_checker=FormatChecker(),
    ).validate("https://www.dw.com/zh/国际新闻/a-1")

    for region in ("", "gwadar", "balochistan", "myanmar"):
        directory = ROOT / "belt-and-road" / region / "data"
        captured = json.loads(
            (directory / "captured-index.json").read_text(encoding="utf-8")
        )
        regional = json.loads(
            (directory / "regional-data.json").read_text(encoding="utf-8")
        )
        captured_validator.validate(captured)
        dump_validator.validate(regional)

        assert regional["captured_news"] == captured
        assert captured["content_sha256"] == _embedded_content_sha256(captured)
        assert regional["content_sha256"] == _embedded_content_sha256(regional)
        assert captured["counts"]["unique_events"] == len(captured["events"])
        assert captured["counts"]["event_versions"] == sum(
            event["version_count"] for event in captured["events"]
        )
        assert (
            captured["counts"]["current_events"]
            + captured["counts"]["historical_events"]
            == captured["counts"]["unique_events"]
        )
        assert (
            captured["counts"]["event_pages_available"]
            <= captured["counts"]["unique_events"]
        )
        assert captured["counts"]["events_with_english_translation"] == sum(
            event["english_translation"] is not None for event in captured["events"]
        )
        assert captured["counts"]["sources"] == len(captured["sources"])
        source_counts = {
            source["source_id"]: source["captured_event_count"]
            for source in captured["sources"]
        }
        assert source_counts == {
            source_id: sum(
                source_id in event["source_ids"] for event in captured["events"]
            )
            for source_id in source_counts
        }
        for event in captured["events"]:
            assert len(event["version_ids"]) == event["version_count"]
            assert len(event["source_ids"]) == len(event["source_names"])
            assert set(event["source_ids"]) <= set(source_counts)
            assert captured["region"] in event["region_tags"]
        translation_input = captured["inputs"]["chinese_translation_sidecar"]
        assert translation_input is not None
        assert (
            translation_input["newswire_ledger_sha256"]
            == captured["inputs"]["newswire_versions_sha256"]
        )
        assert (
            translation_input["newswire_ledger_rows"]
            == captured["capture_universe"]["event_versions"]
        )
        assert translation_input["missing_records"] == 0
        assert captured["rights"] == {
            "publication_mode": "metadata-link-only",
            "article_bodies_included": False,
            "publisher_copyright_retained": True,
            "source_policy": "config/news_sources.json",
        }
        assert regional["publication_boundary"]["article_body_fetching"] == (
            "prohibited"
        )
        assert regional["publication_boundary"]["person_or_tactical_records"] == (
            "excluded"
        )
        assert captured["classification"]["csv_formula_neutralization"] == (
            "CSV text cells whose first non-whitespace character is =, +, -, or @ "
            "receive a leading apostrophe; JSON and JSONL preserve the exact "
            "captured text."
        )

        jsonl_rows = [
            json.loads(line)
            for line in (directory / "captured-index.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        with (directory / "captured-index.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            csv_rows = list(csv.DictReader(handle))
        assert len(jsonl_rows) == len(csv_rows) == captured["counts"]["unique_events"]
        assert {row["event_id"] for row in jsonl_rows} == {
            event["event_id"] for event in captured["events"]
        }
        assert {row["event_id"] for row in csv_rows} == {
            event["event_id"] for event in captured["events"]
        }
        for jsonl_row, event in zip(jsonl_rows, captured["events"], strict=True):
            assert jsonl_row == {
                "schema_version": captured["schema_version"],
                "region": captured["region"],
                **event,
            }
        assert all(
            not value.lstrip().startswith(("=", "+", "-", "@"))
            for row in csv_rows
            for value in row.values()
            if value
        )
        assert all(
            row["schema_version"] == captured["schema_version"] for row in jsonl_rows
        )
        assert all(row["region"] == captured["region"] for row in jsonl_rows)
        assert all(
            source["rights_policy"] == "metadata-link-only"
            for source in captured["sources"]
        )

    editorial = json.loads(
        (ROOT / "config" / "regional_editorials.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(
        REGIONAL_EDITORIAL_SCHEMA,
        format_checker=FormatChecker(),
    ).validate(editorial)
    evidence_ids = {row["evidence_id"] for row in editorial["evidence"]}
    assert len(evidence_ids) == len(editorial["evidence"])
    for region, article in editorial["editorials"].items():
        for section in article["sections"]:
            assert set(section["evidence_ids"]) <= evidence_ids
            assert all(
                region
                in next(
                    row["regions"]
                    for row in editorial["evidence"]
                    if row["evidence_id"] == evidence_id
                )
                for evidence_id in section["evidence_ids"]
            )


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
    assert status["rights_evaluated_at"] == "2026-08-31T00:00:00Z"
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
            if relative == "china/index.html":
                assert "Public evidence remains online" in text
                assert 'href="/news/china/situation/"' in text
                assert 'href="/readings/ddti-latest.json"' in text
                assert 'href="/belt-and-road/"' in text
                assert 'href="/belt-and-road/balochistan/analysis/"' in text
            else:
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
    assert attestation["attested_at"] == "2026-08-31T00:00:00Z"
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


def test_release_receipt_v3_rejects_attested_identity_tamper_during_check(
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
    assert receipt["schema_version"] == "palimpsest.pages-rights-release-receipt.v3"
    assert receipt["public_tree"]["schema_version"] == (
        "palimpsest.pages-rights-public-tree-proof.v1"
    )
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
            "2026-08-31T00:00:00Z",
            "--admission-at",
            "2026-08-31T00:00:00Z",
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
def test_release_receipt_v3_pins_every_artifact_path(
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


def test_freshness_attestation_rejects_future_situation_clock(tmp_path: Path):
    _write_minimal_denied_tree(tmp_path)
    situation_path = tmp_path / stage_pages_rights.CHINA_SITUATION_RELATIVE_PATH
    situation = json.loads(situation_path.read_text(encoding="utf-8"))
    situation["generated_at"] = "2026-09-01T00:00:00Z"
    situation_path.write_text(
        json.dumps(situation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="China situation generated_at exceeds the rights evaluation clock",
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


def test_rights_derived_signal_closure_is_explicit_and_exact():
    assert stage_pages_rights.DERIVED_INSTRUMENTS == {
        "board-alarm",
        "event-flags",
        "coverage-guard",
        "forecast-ledger",
        "cross-layer",
        "china-econ",
        "cny-fix-gap",
        "data-darkness",
    }


def test_mixed_newsroom_and_analysis_derivatives_are_quarantined(tmp_path: Path):
    _write_minimal_denied_tree(tmp_path)
    fixtures = {
        "news/feed.json": {
            "version": "https://jsonfeed.org/version/1.1",
            "items": [
                {
                    "id": "measurement-cny-fix-gap",
                    "title": "CNY fix gap is -0.9298%",
                    "content_text": "Result: the current gap is -0.9298%.",
                    "tags": ["palimpsest-measurement", "cny-fix-gap"],
                    "_palimpsest": {"kind": "instrument_measurement"},
                }
            ],
        },
        "news/wire/event-example/analysis.json": {
            "schema_version": "palimpsest-event-analysis.v2",
            "collector_context": [
                {
                    "signal_id": "china-econ",
                    "status": "live",
                    "metric": {"label": "families", "value": 3},
                }
            ],
        },
        "news/cny-fix-gap/analysis.json": {
            "schema_version": "palimpsest-instrument-analysis.v1",
            "signal_id": "cny-fix-gap",
            "key_numbers": [{"label": "gap", "value": "-0.9298%"}],
        },
        "archive/collector-row.json": {
            "kind": "collector-row",
            "signal_ids": ["data-darkness"],
            "metric": {"value": 0.25},
        },
    }
    for relative, document in fixtures.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")

    html_path = tmp_path / "news/index.html"
    html_path.write_text(
        '<article class="nw-card"><a href="/news/china-money-market-benchmarks/">'
        'China money-market benchmarks</a><p class="nw-card__metric">'
        "<strong>3 families</strong> current official benchmarks</p></article>",
        encoding="utf-8",
    )
    rss_path = tmp_path / "news/instruments/feed.xml"
    rss_path.parent.mkdir(parents=True, exist_ok=True)
    rss_path.write_text(
        "<rss><channel><item><title>[Palimpsest measurement] "
        "Board e-value 1.18</title><link>https://palimpsest.info/news/board-alarm/"
        "</link><description>Item type: Palimpsest measurement. Result: 1.18."
        "</description><source>board-alarm</source></item></channel></rss>",
        encoding="utf-8",
    )

    expected = set(fixtures) | {"news/index.html", "news/instruments/feed.xml"}
    before = set(
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)
    )
    assert expected <= before

    status = _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)

    assert expected <= set(status["quarantined_paths"])
    _verify(tmp_path, evaluated_at=RIGHTS_CLOCK)
    assert (
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)
        == []
    )


def test_topic_tags_and_safe_measurements_do_not_cross_contaminate(tmp_path: Path):
    _write_minimal_denied_tree(tmp_path)
    safe_feed = tmp_path / "news/safe-feed.json"
    safe_feed.parent.mkdir(parents=True, exist_ok=True)
    safe_feed.write_text(
        json.dumps(
            {
                "version": "https://jsonfeed.org/version/1.1",
                "items": [
                    {
                        "id": "publisher-report",
                        "content_text": "Publisher reports growth of 5%.",
                        "tags": ["china-econ"],
                        "_palimpsest": {"kind": "source_report"},
                    },
                    {
                        "id": "safe-measurement",
                        "content_text": "Result: 7 deletions.",
                        "tags": ["palimpsest-measurement", "ddti"],
                        "_palimpsest": {"kind": "instrument_measurement"},
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    safe_rss = tmp_path / "news/safe-feed.xml"
    safe_rss.write_text(
        "<rss><channel><item><title>Publisher reports 5% growth</title>"
        "<category>source-report</category><category>china-econ</category>"
        "</item></channel></rss>",
        encoding="utf-8",
    )
    safe_html = tmp_path / "news/safe.html"
    safe_html.write_text(
        '<article data-topic="china-econ"><a href="/news/ddti/">DDTI</a>'
        '<p class="nw-card__metric"><strong>7</strong> deletions</p></article>',
        encoding="utf-8",
    )
    mixed_availability_html = tmp_path / "news/safe-availability-and-metric.html"
    availability_card = stage_pages_rights._expected_newsroom_availability_card(
        "board-alarm",
        publication_at=RIGHTS_CLOCK.isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
    )
    mixed_availability_html.write_text(
        '<section class="nw-section">'
        + availability_card
        + '<article><a href="/news/ddti/">DDTI</a>'
        '<p class="nw-card__metric"><strong>7</strong> deletions</p></article>'
        + "</section>",
        encoding="utf-8",
    )
    safe_analysis = tmp_path / "news/safe-analysis.json"
    safe_analysis.write_text(
        json.dumps(
            {
                "declared_links": {"signal_ids": ["china-econ"]},
                "collector_context": [{"signal_id": "ddti", "metric": {"value": 7}}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    violations = set(
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)
    )

    assert {
        "news/safe-feed.json",
        "news/safe-feed.xml",
        "news/safe.html",
        "news/safe-availability-and-metric.html",
        "news/safe-analysis.json",
    }.isdisjoint(violations)


def test_availability_labels_cannot_hide_a_derived_value(tmp_path: Path):
    _write_minimal_denied_tree(tmp_path)
    rights_url = stage_pages_rights.PUBLIC_RIGHTS_EVIDENCE_URL
    fixtures = {
        "news/availability-feed.json": json.dumps(
            {
                "version": "https://jsonfeed.org/version/1.1",
                "items": [
                    {
                        "id": "unsafe-availability",
                        "url": "https://palimpsest.info/news/board-alarm/",
                        "external_url": rights_url,
                        "title": "[Palimpsest availability] Board alarm unavailable",
                        "summary": "Palimpsest availability notice. Current value: 3.2",
                        "content_text": (
                            "ITEM TYPE: PALIMPSEST AVAILABILITY\n\n"
                            "Availability: Current value: 3.2"
                        ),
                        "date_published": "2026-08-31T00:00:00Z",
                        "date_modified": "2026-08-31T00:00:00Z",
                        "tags": [
                            "palimpsest-availability",
                            "The Board",
                            "board-alarm",
                            "degraded",
                        ],
                        "attachments": [
                            {
                                "url": rights_url,
                                "mime_type": "application/json",
                                "title": "china-publication-rights-latest.json",
                            }
                        ],
                        "metric": {"value": 3.2},
                        "_palimpsest": {
                            "kind": "instrument_availability",
                            "publication_disposition": (
                                "rights-restricted-availability-v1"
                            ),
                            "revision_id": "storyv-" + "1" * 24,
                            "signal_id": "board-alarm",
                            "value_state": "withheld",
                            "verification_status": "public_value_unavailable",
                        },
                    }
                ],
            }
        )
        + "\n",
        "news/availability.html": (
            '<article class="nw-card" data-claim-type="availability" '
            'data-palimpsest-kind="instrument-availability" '
            'data-signal-id="board-alarm" '
            'data-publication-disposition="rights-restricted-availability-v1">'
            '<a href="/news/board-alarm/">Board alarm</a>'
            '<p class="nw-card__availability"><strong>Current value 3.2</strong>'
            '<span>Not zero; no current result is published.</span></p></article>'
        ),
        "news/availability.xml": (
            '<rss xmlns:palimpsest="https://palimpsest.info/ns/publication/1.0">'
            '<channel><item><title>[Palimpsest availability] Board alarm</title>'
            '<link>https://palimpsest.info/news/board-alarm/</link>'
            '<description>Item type: Palimpsest availability. Availability: '
            'Current value: 3.2</description>'
            f'<source url="{rights_url}">board-alarm</source>'
            '<palimpsest:kind>instrument_availability</palimpsest:kind>'
            '<palimpsest:signal>board-alarm</palimpsest:signal>'
            '<palimpsest:publicationDisposition>'
            'rights-restricted-availability-v1'
            '</palimpsest:publicationDisposition>'
            '<palimpsest:valueState>withheld</palimpsest:valueState>'
            '</item></channel></rss>'
        ),
    }
    for relative, payload in fixtures.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    violations = set(
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
    )

    assert set(fixtures) <= violations


def _canonical_availability_rss(signal_id: str, publication_at: str) -> str:
    identity = stage_pages_rights.DERIVED_AVAILABILITY_IDENTITIES[signal_id]
    story = stage_pages_rights._expected_availability_story(
        signal_id,
        publication_at=publication_at,
    )
    published = datetime.fromisoformat(publication_at.replace("Z", "+00:00"))
    description = (
        "Item type: Palimpsest availability. Availability: "
        + stage_pages_rights._availability_claim(signal_id)
        + " Limit: Current finding withheld: public value publication is restricted "
        + "Availability is not a zero, a normal reading, or evidence of direction. "
        + "Evidence: "
        + stage_pages_rights.PUBLIC_RIGHTS_EVIDENCE_URL
    )
    return f'''<rss xmlns:palimpsest="{stage_pages_rights.PALIMPSEST_RSS_NAMESPACE}"><channel><item>
<title>[Palimpsest availability] {story["headline"]}</title>
<link>{story["url"]}</link>
<guid isPermaLink="false">{story["id"]}:{story["claim_fingerprint"]}</guid>
<pubDate>{format_datetime(published, usegmt=True)}</pubDate>
<description>{description}</description>
<category>{identity["section"]}</category>
<source url="{stage_pages_rights.PUBLIC_RIGHTS_EVIDENCE_URL}">{signal_id}</source>
<palimpsest:kind>instrument_availability</palimpsest:kind>
<palimpsest:signal>{signal_id}</palimpsest:signal>
<palimpsest:publicationDisposition>rights-restricted-availability-v1</palimpsest:publicationDisposition>
<palimpsest:valueState>withheld</palimpsest:valueState>
</item></channel></rss>'''


def test_canonical_availability_prose_is_closed_across_public_formats(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    publication_at = RIGHTS_CLOCK.isoformat(timespec="seconds").replace("+00:00", "Z")
    injected = "FDR007 latest reading 987654.321"

    story = stage_pages_rights._expected_availability_story(
        "board-alarm",
        publication_at=publication_at,
    )
    story["claims"][0]["statement"] = (
        "No current finding is published for Board alarm; "
        + injected
        + " because public value publication is restricted by the active source policy."
    )
    claim_core = {
        "claim_type": "availability",
        "metric": story["metric"],
        "signal_id": story["signal_id"],
        "statement": story["claims"][0]["statement"],
        "status": story["status"],
    }
    story["claim_fingerprint"] = "sha256:" + hashlib.sha256(
        stage_pages_rights._contract_json_bytes(claim_core)
    ).hexdigest()

    analysis = stage_pages_rights._expected_availability_analysis(
        "board-alarm",
        generated_at=publication_at,
    )
    analysis["brief"]["does_not_show"]["sentences"][0]["text"] = injected
    analysis_seed = {
        key: value for key, value in analysis.items() if key != "analysis_id"
    }
    analysis["analysis_id"] = stage_pages_rights._contract_id(
        "instrumentv", analysis_seed, 24
    )

    feed_item = stage_pages_rights._expected_json_feed_availability(
        "board-alarm",
        publication_at=publication_at,
    )
    feed_item["summary"] += " " + injected
    card = stage_pages_rights._expected_newsroom_availability_card(
        "board-alarm",
        publication_at=publication_at,
    ).replace("</article>", f"<p>{injected}</p></article>")
    rss = _canonical_availability_rss("board-alarm", publication_at).replace(
        "Availability: No current finding",
        f"Availability: {injected}. No current finding",
    )
    fixtures = {
        "news/board-alarm/story.json": json.dumps(story) + "\n",
        "news/board-alarm/analysis.json": json.dumps(analysis) + "\n",
        "news/availability-feed.json": json.dumps(feed_item) + "\n",
        "news/availability-card.html": card,
        "news/availability-feed.xml": rss,
    }
    for relative, payload in fixtures.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    violations = set(
        stage_pages_rights.find_denied_value_paths(
            tmp_path,
            evaluated_at=RIGHTS_CLOCK,
        )
    )

    assert set(fixtures) <= violations


def test_availability_prose_cannot_escape_into_representation_siblings(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    publication_at = RIGHTS_CLOCK.isoformat(timespec="seconds").replace("+00:00", "Z")
    injected = "FDR007 latest reading 987654.321"
    feed_item = stage_pages_rights._expected_json_feed_availability(
        "board-alarm",
        publication_at=publication_at,
    )
    assert not stage_pages_rights._is_derived_availability_story(feed_item)
    assert stage_pages_rights._is_safe_derived_json_feed_availability(feed_item)
    card = stage_pages_rights._expected_newsroom_availability_card(
        "board-alarm",
        publication_at=publication_at,
    )
    rss = _canonical_availability_rss("board-alarm", publication_at)
    safe_feed = tmp_path / "news/availability-canonical.json"
    safe_feed.parent.mkdir(parents=True, exist_ok=True)
    safe_feed.write_text(json.dumps(feed_item) + "\n", encoding="utf-8")
    fixtures = {
        "news/availability-sibling.json": json.dumps(
            {"item": feed_item, "footer": injected}
        )
        + "\n",
        "news/availability-sibling.html": card + f"<p>{injected}</p>",
        "news/availability-sibling.xml": rss.replace(
            "</channel>",
            f"<description>{injected}</description></channel>",
        ),
    }
    for relative, payload in fixtures.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    violations = set(
        stage_pages_rights.find_denied_value_paths(
            tmp_path,
            evaluated_at=RIGHTS_CLOCK,
        )
    )

    assert set(fixtures) <= violations
    assert "news/availability-canonical.json" not in violations


def test_availability_machine_markers_trigger_strict_validation_as_a_union(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    publication_at = RIGHTS_CLOCK.isoformat(timespec="seconds").replace("+00:00", "Z")
    canonical = stage_pages_rights._expected_json_feed_availability(
        "board-alarm",
        publication_at=publication_at,
    )
    fixtures: dict[str, dict] = {}
    missing_tag = json.loads(json.dumps(canonical))
    missing_tag["tags"].remove("board-alarm")
    missing_tag["metric"] = {"value": 3.2}
    fixtures["news/availability-missing-signal-tag.json"] = missing_tag
    misspelled_kind = json.loads(json.dumps(canonical))
    misspelled_kind["_palimpsest"]["kind"] = "instrument_availabilty"
    misspelled_kind["metric"] = {"value": 3.2}
    fixtures["news/availability-misspelled-kind.json"] = misspelled_kind
    missing_kind = json.loads(json.dumps(canonical))
    del missing_kind["_palimpsest"]["kind"]
    missing_kind["metric"] = {"value": 3.2}
    fixtures["news/availability-missing-kind.json"] = missing_kind
    for relative, document in fixtures.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")

    violations = set(
        stage_pages_rights.find_denied_value_paths(
            tmp_path,
            evaluated_at=RIGHTS_CLOCK,
        )
    )

    assert set(fixtures) <= violations


def test_availability_signal_identity_is_cross_bound_in_every_format(tmp_path: Path):
    _write_minimal_denied_tree(tmp_path)
    publication_at = RIGHTS_CLOCK.isoformat(timespec="seconds").replace("+00:00", "Z")
    story = stage_pages_rights._expected_availability_story(
        "board-alarm",
        publication_at=publication_at,
    )
    story["slug"] = "china-money-market-benchmarks"
    story["url"] = "https://palimpsest.info/news/china-money-market-benchmarks/"
    feed_item = stage_pages_rights._expected_json_feed_availability(
        "board-alarm",
        publication_at=publication_at,
    )
    feed_item["url"] = "https://palimpsest.info/news/china-money-market-benchmarks/"
    card = stage_pages_rights._expected_newsroom_availability_card(
        "board-alarm",
        publication_at=publication_at,
    ).replace("/news/board-alarm/", "/news/china-money-market-benchmarks/")
    rss = _canonical_availability_rss("board-alarm", publication_at).replace(
        "<palimpsest:signal>board-alarm</palimpsest:signal>",
        "<palimpsest:signal>china-econ</palimpsest:signal>",
    )
    fixtures = {
        "news/mismatched-story.json": json.dumps(story) + "\n",
        "news/mismatched-feed.json": json.dumps(feed_item) + "\n",
        "news/mismatched-card.html": card,
        "news/mismatched-feed.xml": rss,
    }
    for relative, payload in fixtures.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    violations = set(
        stage_pages_rights.find_denied_value_paths(
            tmp_path,
            evaluated_at=RIGHTS_CLOCK,
        )
    )

    assert set(fixtures) <= violations


def test_nonrights_no_result_availability_survives_every_public_format(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    publication_at = RIGHTS_CLOCK.isoformat(timespec="seconds").replace("+00:00", "Z")
    evidence_url = "https://palimpsest.info/readings/ddti-latest.json"
    story = {
        "id": "palimpsest-news:ddti",
        "slug": "ddti",
        "url": "https://palimpsest.info/news/ddti/",
        "signal_id": "ddti",
        "headline": "Deletion-directive term index: no current finding",
        "status": "stale",
        "published_at": publication_at,
        "modified_at": publication_at,
        "metric": {
            "label": None,
            "value": None,
            "unit": None,
            "denominator": {"label": None, "value": None},
        },
        "claims": [
            {
                "type": "availability",
                "statement": (
                    "No current finding is published for the deletion-directive "
                    "term index because the source status is stale."
                ),
            }
        ],
        "evidence": {
            "url": evidence_url,
            "input": {
                "filename": "ddti-latest.json",
                "sha256": "a" * 64,
                "bytes": 172274,
            },
            "source_timestamp": "2026-08-24T04:07:05Z",
        },
    }
    feed_item = {
        "id": "palimpsest-news:ddti:storyv-safe",
        "url": story["url"],
        "external_url": evidence_url,
        "title": "[Palimpsest availability] " + story["headline"],
        "summary": "Palimpsest availability notice. Source status is stale.",
        "content_text": (
            "ITEM TYPE: PALIMPSEST AVAILABILITY\n\n"
            "Availability: No current finding is published because the source "
            "status is stale."
        ),
        "date_published": publication_at,
        "date_modified": publication_at,
        "tags": ["palimpsest-availability", "ddti", "stale"],
        "attachments": [
            {
                "url": evidence_url,
                "mime_type": "application/json",
                "title": "ddti-latest.json",
            }
        ],
        "_palimpsest": {
            "kind": "instrument_availability",
            "signal_id": "ddti",
            "publication_disposition": "source-stale-availability-v1",
            "value_state": "unavailable",
            "verification_status": "source_stale",
        },
    }
    analysis = {
        "schema_version": "palimpsest-instrument-analysis.v1",
        "analysis_id": "instrumentv-safe-ddti",
        "signal_id": "ddti",
        "story_url": story["url"],
        "reading_url": evidence_url,
        "generated_at": publication_at,
        "disposition": "availability-brief",
        "status": "stale",
        "position": "No current finding is published while the source is stale.",
    }
    html_card = f'''<article class="nw-card" data-status="stale" data-claim-type="availability" data-palimpsest-kind="instrument-availability" data-signal-id="ddti">
  <h3><a class="nw-card__link" href="/news/ddti/">{story["headline"]}</a></h3>
  <p class="nw-card__availability"><strong>No current finding</strong><span>The source status is stale.</span></p>
  <time datetime="{publication_at}">{publication_at}</time>
</article>'''
    rss = f'''<rss xmlns:palimpsest="{stage_pages_rights.PALIMPSEST_RSS_NAMESPACE}"><channel><item>
<title>[Palimpsest availability] {story["headline"]}</title>
<link>{story["url"]}</link>
<guid isPermaLink="false">palimpsest-news:ddti:storyv-safe</guid>
<pubDate>{format_datetime(RIGHTS_CLOCK, usegmt=True)}</pubDate>
<description>Item type: Palimpsest availability. Availability: No current finding is published because the source status is stale.</description>
<category>watch</category>
<source url="{evidence_url}">ddti</source>
<palimpsest:kind>instrument_availability</palimpsest:kind>
<palimpsest:signal>ddti</palimpsest:signal>
<palimpsest:publicationDisposition>source-stale-availability-v1</palimpsest:publicationDisposition>
<palimpsest:valueState>unavailable</palimpsest:valueState>
</item></channel></rss>'''
    fixtures = {
        "news/ddti/story.json": json.dumps(story) + "\n",
        "news/ddti/analysis.json": json.dumps(analysis) + "\n",
        "news/ddti/feed-item.json": json.dumps(feed_item) + "\n",
        "news/ddti/index.html": html_card,
        "news/ddti/feed.xml": rss,
    }
    for relative, payload in fixtures.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    violations = set(
        stage_pages_rights.find_denied_value_paths(
            tmp_path,
            evaluated_at=RIGHTS_CLOCK,
        )
    )

    assert set(fixtures).isdisjoint(violations)


def test_unicode_escaped_json_lineage_is_structurally_denied(tmp_path: Path):
    _write_minimal_denied_tree(tmp_path)
    fixtures = {
        "archive/escaped-source.json": (
            '{"source_id":"\\u0063fets_benchmarks","value":987654.321}\n'
        ),
        "archive/escaped-signal.json": (
            '{"signal_id":"board\\u002dalarm","value":987654.321}\n'
        ),
    }
    for relative, payload in fixtures.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    violations = set(
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
    )

    assert set(fixtures) <= violations


def test_mixed_case_html_and_rss_markers_are_not_prefilter_bypasses(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    html_path = tmp_path / "news/mixed-case.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(
        '<article><a href="/news/board-alarm/">Board</a>'
        '<p class="NW-CARD__METRIC"><strong>987654.321</strong></p></article>',
        encoding="utf-8",
    )
    rss_path = tmp_path / "news/mixed-case.xml"
    rss_path.write_text(
        "<rss><channel><item><title>[PALIMPSEST MEASUREMENT] Board</title>"
        "<link>https://palimpsest.info/news/board-alarm/</link>"
        "<description>ITEM TYPE: PALIMPSEST MEASUREMENT. Result: 987654.321"
        "</description><source>board-alarm</source></item></channel></rss>",
        encoding="utf-8",
    )

    violations = set(
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
    )

    assert {"news/mixed-case.html", "news/mixed-case.xml"} <= violations


def test_datapackage_sizes_are_reconciled_after_quarantine_and_verified(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    safe_path = tmp_path / "public/safe.json"
    safe_path.parent.mkdir(parents=True, exist_ok=True)
    safe_path.write_text('{"safe":true}\n', encoding="utf-8")
    package_path = tmp_path / stage_pages_rights.DATAPACKAGE_RELATIVE_PATH
    package_path.write_text(
        json.dumps(
            {
                "profile": "data-package",
                "resources": [
                    {
                        "name": "restricted-observations",
                        "path": "readings/china-econ-observations.jsonl",
                        "bytes": 1,
                    },
                    {"name": "safe", "path": "public/safe.json"},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)
    package = json.loads(package_path.read_text(encoding="utf-8"))
    resources = {row["name"]: row for row in package["resources"]}

    assert (
        resources["restricted-observations"]["bytes"]
        == (tmp_path / "readings/china-econ-observations.jsonl").stat().st_size
    )
    assert resources["safe"]["bytes"] == safe_path.stat().st_size
    _verify(tmp_path, evaluated_at=RIGHTS_CLOCK)

    safe_path.write_text('{"safe":true} \n', encoding="utf-8")
    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="datapackage resource byte size drifted: public/safe.json",
    ):
        _verify(tmp_path, evaluated_at=RIGHTS_CLOCK)


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


def _write_china_analysis_availability_feeds(
    root: Path, *, generated_at: str
) -> None:
    json_path = root / stage_pages_rights.CHINA_ANALYSIS_JSON_FEED_RELATIVE_PATH
    rss_path = root / stage_pages_rights.CHINA_ANALYSIS_RSS_FEED_RELATIVE_PATH
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_bytes(
        stage_pages_rights._expected_china_analysis_availability_json_bytes(
            generated_at
        )
    )
    rss_path.write_bytes(
        stage_pages_rights._expected_china_analysis_availability_rss(generated_at)
    )


def test_china_analysis_denied_feeds_are_exact_same_clock_availability_only(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    generated_at = RIGHTS_CLOCK.isoformat(timespec="seconds").replace("+00:00", "Z")
    _write_china_analysis_availability_feeds(tmp_path, generated_at=generated_at)
    newsroom_path = tmp_path / "readings/newsroom-latest.json"
    newsroom_path.write_text(
        json.dumps(
            {
                "schema_version": "palimpsest-news.v1",
                "generated_at": generated_at,
            }
        ),
        encoding="utf-8",
    )

    violations = set(
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
    )
    assert {
        stage_pages_rights.CHINA_ANALYSIS_JSON_FEED_RELATIVE_PATH.as_posix(),
        stage_pages_rights.CHINA_ANALYSIS_RSS_FEED_RELATIVE_PATH.as_posix(),
    }.isdisjoint(violations)

    document = json.loads(
        (tmp_path / stage_pages_rights.CHINA_ANALYSIS_JSON_FEED_RELATIVE_PATH).read_text(
            encoding="utf-8"
        )
    )
    document["items"][0]["summary"] += " Prior finding: 987654.321."
    (tmp_path / stage_pages_rights.CHINA_ANALYSIS_JSON_FEED_RELATIVE_PATH).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    violations = set(
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
    )
    assert {
        stage_pages_rights.CHINA_ANALYSIS_JSON_FEED_RELATIVE_PATH.as_posix(),
        stage_pages_rights.CHINA_ANALYSIS_RSS_FEED_RELATIVE_PATH.as_posix(),
    } <= violations


def test_china_analysis_denied_feed_clock_is_bound_to_current_newsroom(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    generated_at = RIGHTS_CLOCK.isoformat(timespec="seconds").replace("+00:00", "Z")
    _write_china_analysis_availability_feeds(tmp_path, generated_at=generated_at)
    newsroom_path = tmp_path / "readings/newsroom-latest.json"
    newsroom_path.write_text(
        json.dumps(
            {
                "schema_version": "palimpsest-news.v1",
                "generated_at": "2026-08-31T00:00:01Z",
            }
        ),
        encoding="utf-8",
    )

    violations = set(
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
    )
    assert {
        stage_pages_rights.CHINA_ANALYSIS_JSON_FEED_RELATIVE_PATH.as_posix(),
        stage_pages_rights.CHINA_ANALYSIS_RSS_FEED_RELATIVE_PATH.as_posix(),
    } <= violations


def test_restricted_availability_cannot_hide_a_numeric_mapping_sibling(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    publication_at = RIGHTS_CLOCK.isoformat(timespec="seconds").replace("+00:00", "Z")
    availability = stage_pages_rights._expected_json_feed_availability(
        "board-alarm", publication_at=publication_at
    )
    safe = tmp_path / "archive/safe-wrapper.json"
    unsafe = tmp_path / "archive/numeric-sibling.json"
    safe.parent.mkdir(parents=True, exist_ok=True)
    safe.write_text(
        json.dumps({"availability": availability, "note": "No result."}) + "\n",
        encoding="utf-8",
    )
    unsafe.write_text(
        json.dumps({"availability": availability, "latest": 987654.321}) + "\n",
        encoding="utf-8",
    )

    violations = set(
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
    )
    assert "archive/safe-wrapper.json" not in violations
    assert "archive/numeric-sibling.json" in violations


def test_restricted_availability_in_array_marks_its_parent_key_subtree(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    publication_at = RIGHTS_CLOCK.isoformat(timespec="seconds").replace("+00:00", "Z")
    availability = stage_pages_rights._expected_json_feed_availability(
        "board-alarm", publication_at=publication_at
    )
    path = tmp_path / "archive/array-numeric-sibling.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"items": [availability], "leak": 1.18}) + "\n",
        encoding="utf-8",
    )

    assert path.relative_to(tmp_path).as_posix() in (
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
    )


def test_restricted_availability_in_nested_arrays_marks_outer_child_subtree(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    publication_at = RIGHTS_CLOCK.isoformat(timespec="seconds").replace("+00:00", "Z")
    availability = stage_pages_rights._expected_json_feed_availability(
        "board-alarm", publication_at=publication_at
    )
    path = tmp_path / "archive/nested-array-numeric-sibling.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"items": [[[availability]]], "leak": 1.18}) + "\n",
        encoding="utf-8",
    )

    assert path.relative_to(tmp_path).as_posix() in (
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
    )


def test_restricted_availability_does_not_bleed_across_array_records(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    publication_at = RIGHTS_CLOCK.isoformat(timespec="seconds").replace("+00:00", "Z")
    availability = stage_pages_rights._expected_json_feed_availability(
        "board-alarm", publication_at=publication_at
    )
    path = tmp_path / "archive/independent-array-records.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "rows": [
                    {"notice": availability},
                    {"unrelated_numeric_metadata": 1.18},
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert path.relative_to(tmp_path).as_posix() not in (
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
    )


def test_rendered_newsroom_aggregate_keeps_closed_numeric_metadata(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    projected = build_newsroom._rights_safe_newsroom_feed(
        newsroom.build_news_feed()
    )
    safe_path = tmp_path / "archive/projected-newsroom.json"
    unsafe_path = tmp_path / "archive/projected-newsroom-with-leak.json"
    safe_path.parent.mkdir(parents=True, exist_ok=True)
    safe_path.write_text(json.dumps(projected) + "\n", encoding="utf-8")
    compromised = json.loads(json.dumps(projected))
    compromised["leak"] = 1.18
    unsafe_path.write_text(json.dumps(compromised) + "\n", encoding="utf-8")

    violations = set(
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
    )
    assert safe_path.relative_to(tmp_path).as_posix() not in violations
    assert unsafe_path.relative_to(tmp_path).as_posix() in violations


def test_renamed_and_recursively_encoded_raw_newswire_shapes_are_denied(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    structural_wire = {
        "format": stage_pages_rights.NEWSWIRE_SCHEMA,
        "generated_at": "2026-08-31T00:00:00Z",
        "source_registry": "https://palimpsest.info/config/news_sources.json",
        "source_registry_sha256": "0" * 64,
        "window": {},
        "scope": "raw",
        "method": "raw",
        "mutation_semantics": "raw",
        "coverage": {},
        "n_items": 0,
        "n_events": 0,
        "items": [],
        "events": [],
    }
    renamed = tmp_path / "archive/wire.snapshot"
    renamed.parent.mkdir(parents=True, exist_ok=True)
    renamed.write_text(json.dumps(structural_wire), encoding="utf-8")

    schema_wire = json.dumps(
        {**structural_wire, "schema_version": stage_pages_rights.NEWSWIRE_SCHEMA},
        separators=(",", ":"),
    ).encode()
    encoded = tmp_path / "archive/innocent-metadata.txt"
    encoded.write_text(
        json.dumps(
            {"harmless_note": base64.b64encode(base64.b64encode(schema_wire)).decode()}
        ),
        encoding="utf-8",
    )

    violations = set(
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
    )
    assert {"archive/wire.snapshot", "archive/innocent-metadata.txt"} <= violations


def test_arbitrary_base64_keys_are_bounded_without_treating_hashes_as_payloads(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    denied = json.dumps(
        {"source_id": "cfets_benchmarks", "value": 987654.321},
        separators=(",", ":"),
    ).encode()
    nested = base64.b64encode(base64.b64encode(denied)).decode()
    denied_path = tmp_path / "archive/arbitrary-key.json"
    denied_path.parent.mkdir(parents=True, exist_ok=True)
    denied_path.write_text(json.dumps({"caption": nested}), encoding="utf-8")

    hashes_path = tmp_path / "archive/hashes.json"
    hashes_path.write_text(
        json.dumps(
            {
                f"digest_{position}": hashlib.sha256(str(position).encode()).hexdigest()
                for position in range(stage_pages_rights.MAX_ENCODED_TOKENS + 1)
            }
        ),
        encoding="utf-8",
    )

    violations = set(
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
    )
    assert "archive/arbitrary-key.json" in violations
    assert "archive/hashes.json" not in violations


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


def test_mixed_public_url_encodings_do_not_exhaust_decode_depth(tmp_path: Path):
    _write_minimal_denied_tree(tmp_path)
    path = tmp_path / "archive" / "mixed-encoded-metadata.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<a href="/evidence?source_id=cfets_benchmarks&amp;amp;path=%252Fmetadata">'
        "Metadata only; values unavailable."
        "</a>",
        encoding="utf-8",
    )

    assert path.relative_to(tmp_path).as_posix() not in (
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)
    )


def test_four_nested_encoding_layers_still_fail_closed(tmp_path: Path):
    _write_minimal_denied_tree(tmp_path)
    payload = json.dumps(
        {"source_id": "cfets_benchmarks", "value": 987654.321},
        separators=(",", ":"),
    )
    for _ in range(4):
        payload = quote(payload, safe="")
    path = tmp_path / "archive" / "too-deep-percent.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(stage_pages_rights.PagesRightsError, match="decode depth"):
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)


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


def test_generated_share_cards_require_reproducible_specs_not_png_allowlisting(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    spec = {
        "schema_version": share_cards.SPEC_VERSION,
        "kind": "instrument-reading",
        "kicker": "Command desk / evidence reading",
        "title": "Current evidence remains explicitly bounded",
        "status": "live",
        "status_label": "Current evidence",
        "metric": {"value": "84.4%", "label": "empirical coverage"},
        "as_of": "2026-08-30T07:32:12Z",
        "source": "forecast-ledger-latest.json",
        "receipt": "SHA256 92dd686d31a4e373",
        "target_url": "https://palimpsest.info/news/forecast-ledger/",
    }
    card = share_cards.render_card(spec)
    card_path = tmp_path / card.path
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_bytes(card.png)
    manifest_path = tmp_path / share_cards.MANIFEST_PATH
    manifest_path.write_bytes(share_cards.manifest_bytes([card]))

    assert stage_pages_rights.find_denied_value_paths(
        tmp_path, evaluated_at=RIGHTS_CLOCK
    ) == [
        "readings/china-econ-observations.jsonl",
        "readings/china-situation-latest.json",
    ]

    card_path.write_bytes(card.png + b"drift")
    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="does not reproduce from its spec",
    ):
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)

    card_path.write_bytes(card.png)
    manifest_path.unlink()
    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="generated share card lacks a reproducing manifest row",
    ):
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)


def test_share_card_rights_are_scanned_per_spec_without_cross_row_token_bleed(
    tmp_path: Path,
) -> None:
    _write_minimal_denied_tree(tmp_path)
    safe = share_cards.render_card(
        {
            "schema_version": share_cards.SPEC_VERSION,
            "kind": "instrument-reading",
            "kicker": "Command desk / evidence reading",
            "title": "Current evidence remains explicitly bounded",
            "status": "live",
            "status_label": "Current evidence",
            "metric": {"value": "84.4%", "label": "empirical coverage"},
            "as_of": "2026-08-30T07:32:12Z",
            "source": "forecast-ledger-latest.json",
            "receipt": "SHA256 92dd686d31a4e373",
            "target_url": "https://palimpsest.info/news/forecast-ledger/",
        }
    )
    restricted = share_cards.render_card(
        {
            "schema_version": share_cards.SPEC_VERSION,
            "kind": "instrument-reading",
            "kicker": "China economic evidence",
            "title": "Restricted upstream values remain unavailable",
            "status": "restricted",
            "status_label": "Publication restricted",
            "metric": None,
            "as_of": "2026-08-30T07:32:12Z",
            "source": "cfets_benchmarks",
            "receipt": None,
            "target_url": "https://palimpsest.info/news/economy/",
        }
    )
    for card in (safe, restricted):
        destination = tmp_path / card.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(card.png)
    manifest_path = tmp_path / share_cards.MANIFEST_PATH
    manifest_raw = share_cards.manifest_bytes([safe, restricted])
    manifest_path.write_bytes(manifest_raw)

    assert share_cards.MANIFEST_PATH.as_posix() not in (
        stage_pages_rights.find_denied_value_paths(tmp_path, evaluated_at=RIGHTS_CLOCK)
    )
    status = _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)
    assert share_cards.MANIFEST_PATH.as_posix() not in status["quarantined_paths"]
    assert manifest_path.read_bytes() == manifest_raw
    assert len(share_cards.parse_manifest(manifest_raw)) == 2
    assert _verify(tmp_path, evaluated_at=RIGHTS_CLOCK) == status


def test_denied_share_card_fails_before_mutating_the_staged_tree(
    tmp_path: Path,
) -> None:
    _write_minimal_denied_tree(tmp_path)
    denied = share_cards.render_card(
        {
            "schema_version": share_cards.SPEC_VERSION,
            "kind": "instrument-reading",
            "kicker": "China economic evidence",
            "title": "A value that must not be published",
            "status": "live",
            "status_label": "Current evidence",
            "metric": {"value": "987654.321", "label": "restricted benchmark"},
            "as_of": "2026-08-30T07:32:12Z",
            "source": "cfets_benchmarks",
            "receipt": "SHA256 denied000000000",
            "target_url": "https://palimpsest.info/news/economy/",
        }
    )
    card_path = tmp_path / denied.path
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_bytes(denied.png)
    manifest_path = tmp_path / share_cards.MANIFEST_PATH
    manifest_raw = share_cards.manifest_bytes([denied])
    manifest_path.write_bytes(manifest_raw)
    ledger_path = tmp_path / "readings" / "china-econ-observations.jsonl"
    ledger_raw = ledger_path.read_bytes()

    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="generated share card contains a denied value",
    ):
        _stage(tmp_path, evaluated_at=RIGHTS_CLOCK)

    assert manifest_path.read_bytes() == manifest_raw
    assert card_path.read_bytes() == denied.png
    assert ledger_path.read_bytes() == ledger_raw
    assert not (tmp_path / stage_pages_rights.STATUS_RELATIVE_PATH).exists()


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
    forged_master["rights_evaluated_at"] = "2026-09-01T00:00:00Z"
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
