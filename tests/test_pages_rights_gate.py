from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from scripts import stage_pages_rights


ROOT = Path(__file__).resolve().parents[1]
RIGHTS_CLOCK = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)
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


def _materialize_git_archive_universe(destination: Path) -> None:
    """Hard-link every tracked file, independently of the gate's selector."""

    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    relative_paths = [
        Path(raw.decode("utf-8"))
        for raw in completed.stdout.split(b"\0")
        if raw
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


def _validator() -> Draft202012Validator:
    Draft202012Validator.check_schema(SCHEMA)
    return Draft202012Validator(SCHEMA, format_checker=FormatChecker())


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
                if (
                    str(key).lower() in {"cfets_benchmarks", "chinamoney"}
                    and type(nested) in {int, float}
                ):
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
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
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
            if (
                ("cfets" in text or "chinamoney" in text or "shibor" in text)
                and ("metric-card__value" in text or "cn-num" in text)
            ):
                failures.append(f"html-value:{path.relative_to(root)}")
    assert failures == []


def test_exact_git_archive_universe_is_recursively_quarantined(tmp_path: Path):
    _materialize_git_archive_universe(tmp_path)
    ledger_path = tmp_path / "readings/china-econ-observations.jsonl"
    first_observation = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
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

    status = stage_pages_rights.stage_pages_tree(
        tmp_path, evaluated_at=RIGHTS_CLOCK
    )
    verified = stage_pages_rights.verify_staged_tree(
        tmp_path, evaluated_at=RIGHTS_CLOCK
    )

    assert verified == status
    assert (
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
        == []
    )
    _assert_independent_archive_is_clean(
        tmp_path, denied_sentinels=denied_sentinels
    )
    assert status["status"] == "restricted"
    assert status["availability"] == "unavailable"
    assert status["publication_allowed"] is False
    assert status["rights_evaluated_at"] == "2026-08-26T00:00:00Z"
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
    assert "readings/catalog.json" not in status["quarantined_paths"]
    assert ".well-known/ai-catalog.json" not in status["quarantined_paths"]
    assert any(
        "co-located" in limitation for limitation in status["limitations"]
    )
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
                "palimpsest-restricted-publication.v1"
            )
            _validator().validate(documents[0])
        else:
            text = path.read_text(encoding="utf-8")
            assert text.startswith("Palimpsest publication status: restricted\n")
            assert "Published records: 0" in text


def test_denied_cfets_and_allowed_empty_wdi_remain_distinct(tmp_path: Path):
    _write_minimal_denied_tree(tmp_path)
    status = stage_pages_rights.stage_pages_tree(
        tmp_path, evaluated_at=RIGHTS_CLOCK
    )

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
    status = stage_pages_rights.stage_pages_tree(
        tmp_path, evaluated_at=RIGHTS_CLOCK
    )
    encoded = json.dumps(status, sort_keys=True).lower()

    assert "unavailable or restricted evidence is not zero, calm, healthy" in encoded
    assert "evidence carrier" in encoded
    assert "observation" not in status
    assert "value" not in status
    assert "direction" not in status
    assert "composite" not in status
    assert "authority" not in status
    assert status["counts"]["published_records"] == 0


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
        "readings/new-derived-surface.json",
    ]
    status = stage_pages_rights.stage_pages_tree(
        tmp_path, evaluated_at=RIGHTS_CLOCK
    )

    assert (
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
        == []
    )
    assert status["counts"]["input_records"] == 1
    assert status["counts"]["restricted_records"] == 1
    assert status["counts"]["published_records"] == 0
    assert "987654.321" not in derivative.read_text(encoding="utf-8")


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
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )
    )
    assert set(fixtures) | {"china/forged-restricted.html"} <= before

    status = stage_pages_rights.stage_pages_tree(
        tmp_path, evaluated_at=RIGHTS_CLOCK
    )
    assert set(fixtures) | {"china/forged-restricted.html"} <= set(
        status["quarantined_paths"]
    )
    stage_pages_rights.verify_staged_tree(
        tmp_path, evaluated_at=RIGHTS_CLOCK
    )
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
        stage_pages_rights.find_denied_value_paths(
            tmp_path, evaluated_at=before_expiry
        )
    )
    stage_pages_rights.stage_pages_tree(tmp_path, evaluated_at=before_expiry)
    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="stale across a policy review or expiry clock",
    ):
        stage_pages_rights.verify_staged_tree(
            tmp_path, evaluated_at=at_expiry
        )

    expired_root = tmp_path / "expired"
    _write_minimal_denied_tree(expired_root)
    expired_wdi = expired_root / "readings/china-econ-wdi-observations.jsonl"
    expired_wdi.write_text(wdi.read_text(encoding="utf-8"), encoding="utf-8")
    expired = stage_pages_rights.stage_pages_tree(
        expired_root, evaluated_at=at_expiry
    )
    decision = _decision(expired, "world_bank_wdi")
    assert decision["decision"] == "expired"
    assert decision["configured_decision"] == "allow"
    assert decision["availability"] == "restricted"
    assert decision["values_allowed"] is False
    assert decision["seiche_export_allowed"] is False
    assert expired["rights_evaluated_at"] == "2027-08-24T00:00:00Z"
    assert "readings/china-econ-wdi-observations.jsonl" in expired[
        "quarantined_paths"
    ]


def test_exact_status_and_policy_receipt_are_verified_not_self_attested(
    tmp_path: Path,
):
    _write_minimal_denied_tree(tmp_path)
    stage_pages_rights.stage_pages_tree(tmp_path, evaluated_at=RIGHTS_CLOCK)
    endpoint = tmp_path / "readings/china-econ-observations.jsonl"
    forged = json.loads(endpoint.read_text(encoding="utf-8"))
    forged["artifact"]["path"] = "readings/not-the-endpoint.jsonl"
    endpoint.write_text(json.dumps(forged) + "\n", encoding="utf-8")

    with pytest.raises(
        stage_pages_rights.PagesRightsError,
        match="exact policy-derived stub",
    ):
        stage_pages_rights.verify_staged_tree(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )

    stage_pages_rights.stage_pages_tree(tmp_path, evaluated_at=RIGHTS_CLOCK)
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
        stage_pages_rights.verify_staged_tree(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )

    stage_pages_rights.stage_pages_tree(tmp_path, evaluated_at=RIGHTS_CLOCK)
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
        stage_pages_rights.verify_staged_tree(
            tmp_path, evaluated_at=RIGHTS_CLOCK
        )


def test_pages_workflow_stages_and_verifies_rights_before_upload():
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8"
    )
    stage_call = (
        'python3 -m scripts.stage_pages_rights --root "$RUNNER_TEMP/pages-root"'
    )
    check_call = stage_call + " --check"
    upload = "actions/upload-pages-artifact@"
    sentinel_scan = "Pages artifact retained denied CFETS source bytes"

    assert workflow.count(stage_call) == 2
    assert workflow.index(stage_call) < workflow.index(check_call) < workflow.index(upload)
    assert "git archive --format=tar" in workflow
    assert "denied-ledger sentinel set is empty" in workflow
    assert "command -v rg >/dev/null" in workflow
    assert sentinel_scan in workflow
    assert workflow.index(check_call) < workflow.index(sentinel_scan) < workflow.index(upload)


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
        if item["identifier"]
        == "urn:air:palimpsest.info:openapi:public-readings"
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
        if item["identifier"]
        == "urn:air:palimpsest.info:mcp:evidence-observatory"
    )
    assert "download economic observation ledger" not in openapi["capabilities"]
    assert index["metadata"]["access"] == "metadata-only-restricted"
    assert "separate deployment" in mcp["description"]
    llms = (ROOT / "llms.txt").read_text(encoding="utf-8").lower()
    assert "metadata-only restricted stubs" in llms
    assert "tool availability does not" in llms
    assert "grant value-publication rights" in llms
    assert "lineage-filtered rebuild" in llms
