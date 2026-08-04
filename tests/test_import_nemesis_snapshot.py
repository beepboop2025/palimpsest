"""Offline tests for the optional Nemesis-to-Palimpsest publication boundary."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import stat

import pytest

from core.safe_fetch import TooManyRedirects
import scripts.import_nemesis_snapshot as importer


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "osint-china-refresh.yml"
NOW = 1_800_000_100.0
DATA_TIME = 1_800_000_000.0


def _snapshot() -> dict:
    return {
        "schema": importer.SCHEMA,
        "schema_version": importer.SCHEMA_VERSION,
        "source": importer.SOURCE,
        "method": "DDTI attention-novelty and censorship-derived economic stress",
        "method_version": "nemesis-public-v1",
        "scope": "Observed records from enabled public-source collectors; not total coverage.",
        "status": "ok",
        "methods": {"ddti": "attention-v1"},
        "generated_at": NOW - 10,
        "data_timestamp": DATA_TIME,
        "timestamps": {
            "ddti_generated_at": DATA_TIME,
            "economic_generated_at": DATA_TIME,
            "threats_generated_at": None,
            "latest_fetched_at": NOW - 20,
            "latest_published_at": DATA_TIME,
            "last_successful_run_at": NOW - 15,
            "data_updated_at": NOW - 15,
        },
        "health": {
            "status": "ok",
            "live": True,
            "ready": True,
            "stale": False,
            "posts": 3,
            "reasons": [],
            "freshness": {
                "status": "fresh",
                "core_data_at": DATA_TIME,
                "age_seconds": NOW - DATA_TIME,
                "stale_after_seconds": 1800,
            },
            "last_run": {"status": "ok"},
            "last_successful_run_at": NOW - 15,
        },
        "coverage": {"completeness": "not_measured", "observed_sources": []},
        "n_alerts": 2,
        "counts": {
            "posts": 3,
            "sources": 1,
            "topics": 2,
            "economic_articles": 1,
            "leads": 1,
            "alerts": 2,
            "alerts_returned": 2,
            "threat_signatures": 0,
        },
        "ddti": {"ranked": []},
        "economic": {"ranked": []},
        "leads": [],
        "alerts": [],
        "threats": None,
    }


def test_unconfigured_import_is_a_noop_and_preserves_optional_absence(tmp_path, monkeypatch):
    output = tmp_path / "readings" / "nemesis-latest.json"
    monkeypatch.delenv(importer.URL_ENV, raising=False)

    assert importer.main(["--output", str(output)]) == 0
    assert not output.exists()


@pytest.mark.parametrize("url", [
    "http://example.org/public.json",
    "file:///tmp/public.json",
    "https://user:password@example.org/public.json",
    "https://example.org/public.json#fragment",
    "https://example.org/public json",
    "//example.org/public.json",
])
def test_only_credential_free_absolute_https_urls_are_accepted(url):
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid URL must be rejected before egress")

    with pytest.raises(importer.SnapshotImportError):
        importer.import_snapshot(url, fetcher=forbidden)
    assert called is False


def test_valid_snapshot_is_bounded_no_redirect_and_atomically_public(tmp_path):
    output = tmp_path / "readings" / "nemesis-latest.json"
    calls = []

    def fake_fetch(url, **kwargs):
        calls.append((url, kwargs))
        return json.dumps(_snapshot())

    imported = importer.import_snapshot(
        "https://nemesis.example/public.json?opaque=allowed",
        output=output,
        fetcher=fake_fetch,
        now=NOW,
    )

    assert imported == _snapshot()
    assert calls[0][0].startswith("https://")
    assert calls[0][1]["max_bytes"] == importer.MAX_BYTES
    assert calls[0][1]["max_redirects"] == 0, "all redirects, including downgrade, are refused"
    assert calls[0][1]["timeout"] == importer.TIMEOUT_SECONDS
    assert json.loads(output.read_text(encoding="utf-8")) == _snapshot()
    assert output.read_bytes().endswith(b"\n")
    assert stat.S_IMODE(output.stat().st_mode) == 0o644


def test_redirect_is_a_fail_loud_import_error_and_does_not_publish(tmp_path):
    output = tmp_path / "nemesis-latest.json"

    def redirecting(_url, **kwargs):
        assert kwargs["max_redirects"] == 0
        raise TooManyRedirects("HTTPS endpoint attempted a redirect")

    with pytest.raises(importer.SnapshotImportError, match="download failed"):
        importer.import_snapshot(
            "https://nemesis.example/public.json", output=output, fetcher=redirecting)
    assert not output.exists()


def test_configured_unavailable_source_returns_failure_not_optional_success(monkeypatch, capsys):
    def unavailable(*_args, **_kwargs):
        raise importer.SnapshotImportError("simulated unavailable endpoint")

    monkeypatch.setattr(importer, "import_snapshot", unavailable)
    assert importer.main(["--url", "https://nemesis.example/public.json"]) == 1
    assert "import failed" in capsys.readouterr().err


def test_schema_source_status_health_counts_and_timestamps_are_strict():
    bad_documents = []

    wrong_schema = _snapshot()
    wrong_schema["schema_version"] = "2.0.0"
    bad_documents.append(wrong_schema)

    wrong_source = _snapshot()
    wrong_source["source"] = "Nemesis-lookalike"
    bad_documents.append(wrong_source)

    unknown_status = _snapshot()
    unknown_status["status"] = "operational"
    unknown_status["health"]["status"] = "operational"
    bad_documents.append(unknown_status)

    contradictory_health = _snapshot()
    contradictory_health["health"]["ready"] = False
    bad_documents.append(contradictory_health)

    future_dated = _snapshot()
    future_dated["generated_at"] = NOW + importer.MAX_FUTURE_SKEW_SECONDS + 1
    bad_documents.append(future_dated)

    boolean_timestamp = _snapshot()
    boolean_timestamp["data_timestamp"] = True
    bad_documents.append(boolean_timestamp)

    timestamp_disagreement = _snapshot()
    timestamp_disagreement["health"]["freshness"]["core_data_at"] = DATA_TIME - 1
    bad_documents.append(timestamp_disagreement)

    wrong_counts = _snapshot()
    wrong_counts["counts"]["alerts"] = 3
    bad_documents.append(wrong_counts)

    unknown_field = _snapshot()
    unknown_field["operator_secret"] = "must not cross"
    bad_documents.append(unknown_field)

    for document in bad_documents:
        with pytest.raises(importer.SnapshotImportError):
            importer.validate_snapshot(document, now=NOW)


@pytest.mark.parametrize("payload", [
    b"\xff\xfe{}",
    b'{"schema": NaN}',
    b'{"schema": "one", "schema": "two"}',
    b"[]",
    b"not json",
])
def test_parser_rejects_ambiguous_or_invalid_json(payload):
    with pytest.raises(importer.SnapshotImportError):
        importer._parse_document(payload)


def test_parser_enforces_its_own_byte_cap_even_with_an_injected_fetcher(tmp_path):
    with pytest.raises(importer.SnapshotImportError, match="exceeds"):
        importer.import_snapshot(
            "https://nemesis.example/public.json",
            output=tmp_path / "out.json",
            fetcher=lambda *_args, **_kwargs: b"x" * (importer.MAX_BYTES + 1),
        )


def test_failed_atomic_replace_preserves_previous_snapshot_and_cleans_temp(tmp_path, monkeypatch):
    output = tmp_path / "nemesis-latest.json"
    sentinel = b'{"previous":true}\n'
    output.write_bytes(sentinel)

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(importer.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        importer.write_atomic(_snapshot(), output)
    assert output.read_bytes() == sentinel
    assert not list(tmp_path.glob(".nemesis-latest.json.*.tmp"))


def test_workflow_syncs_then_imports_and_revalidates_the_post_rebase_tree():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "NEMESIS_SNAPSHOT_URL: ${{ vars.NEMESIS_SNAPSHOT_URL }}" in text
    assert text.count("python -m scripts.import_nemesis_snapshot") == 2
    assert text.count("python -m scripts.build_osint_china") == 2
    assert text.count("python scripts/seal_readings.py") == 2
    assert text.count("tests/test_import_nemesis_snapshot.py") == 2
    assert text.count("tests/test_egress_policy.py") == 2
    assert text.count("tests/test_safe_fetch.py") == 2
    assert text.count("tests/test_seal_readings.py") == 2
    assert text.count("python scripts/verify_public_surface.py") == 2
    assert text.count("git add readings/readings-ledger.jsonl") == 2

    first_sync = text.index("git rebase origin/main")
    first_import = text.index("python -m scripts.import_nemesis_snapshot")
    first_build = text.index("python -m scripts.build_osint_china")
    first_seal = text.index("python scripts/seal_readings.py")
    first_tests = text.index("tests/test_import_nemesis_snapshot.py")
    first_surface = text.index("python scripts/verify_public_surface.py")
    commit = text.index("git commit -m")
    pull_rebase = text.index("git pull --rebase origin main")
    second_import = text.index("python -m scripts.import_nemesis_snapshot", first_import + 1)
    second_build = text.index("python -m scripts.build_osint_china", first_build + 1)
    second_seal = text.index("python scripts/seal_readings.py", first_seal + 1)
    second_tests = text.index("tests/test_import_nemesis_snapshot.py", first_tests + 1)
    second_surface = text.index("python scripts/verify_public_surface.py", first_surface + 1)
    final_stage = text.rindex("git add readings/osint-china-latest.json")
    amend = text.index("git commit --amend --no-edit")
    push = text.index("git push origin HEAD:main")

    assert first_sync < first_import < first_build < first_seal < first_tests < first_surface < commit
    assert commit < pull_rebase < second_import < second_build < second_seal < second_tests < second_surface
    assert second_surface < final_stage < amend < push
