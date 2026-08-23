"""Offline tests for the authenticated private-runtime publication boundary."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import hmac
import json
from pathlib import Path
import stat

import pytest

from core.safe_fetch import FetchError, TooManyRedirects, safe_fetch_bytes
import scripts.import_nemesis_snapshot as importer


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "osint-china-refresh.yml"
NOW = 1_800_000_100.0
DATA_TIME = 1_800_000_000.0
KEY = b"publication-contract-test-key-32-bytes-minimum"
SCOPE = (
    "Observed records from enabled public-source collectors; not an estimate of total "
    "China coverage."
)


def _run(status="ok") -> dict:
    return {
        "id": 7,
        "started_at": DATA_TIME - 20,
        "finished_at": DATA_TIME - 10,
        "status": status,
        "healthy": status == "ok",
        "duration_s": 10.0,
        "collection_errors": 0 if status == "ok" else 1,
    }


def _sample() -> dict:
    return {
        "title": "Public evidence title",
        "url": "https://example.org/public-record",
        "source": "cdt",
        "matched_in": ["tag", "text"],
        "deletion_signal": "freeweibo_confirmed",
    }


def _snapshot() -> dict:
    return {
        "schema": importer.SCHEMA,
        "schema_version": importer.SCHEMA_VERSION,
        "source": importer.SOURCE,
        "method": importer.EXPECTED_METHOD,
        "method_version": importer.EXPECTED_METHOD_VERSION,
        "scope": SCOPE,
        "status": "ok",
        "methods": {
            "ddti": "attention-novelty-signal-weighted-v1",
            "economic": "observed-economic-topic-share-v2",
            "leads": "ddti-investigative-leads-v1",
        },
        "generated_at": NOW - 10,
        "data_timestamp": DATA_TIME,
        "timestamps": {
            "ddti_generated_at": DATA_TIME,
            "economic_generated_at": DATA_TIME,
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
                "age_seconds": (NOW - 10) - DATA_TIME,
                "stale_after_seconds": 1800,
            },
            "last_run": _run(),
            "last_completed_run": _run(),
            "last_successful_run_at": NOW - 15,
        },
        "coverage": {
            "completeness": "not_measured",
            "scope": SCOPE,
            "observed_source_count": 1,
            "observed_sources": [{
                "name": "cdt",
                "posts": 3,
                "first_published_at": DATA_TIME - 100,
                "latest_published_at": DATA_TIME,
                "latest_fetched_at": NOW - 20,
            }],
            "first_published_at": DATA_TIME - 100,
            "latest_published_at": DATA_TIME,
            "latest_fetched_at": NOW - 20,
            "last_successful_cycle": {
                "new": 1,
                "dupes": 0,
                "errors": 0,
                "attempted": 1,
                "succeeded": 1,
                "failed": 0,
                "posts_scored": 3,
                "sources": [{
                    "name": "cdt", "status": "ok", "new": 1, "dupes": 0,
                    "attempted": 1, "succeeded": 1, "failed": 0,
                }],
            },
        },
        "counts": {
            "posts": 3,
            "sources": 1,
            "topics": 1,
            "economic_articles": 1,
            "leads": 1,
        },
        "ddti": {
            "generated_at": DATA_TIME,
            "n_posts": 3,
            "n_posts_scored": 3,
            "n_terms": 1,
            "by_domain": {"ECONOMY": 1},
            "ranked": [{
                "term": "housing", "domain": "ECONOMY", "attention": 2.0,
                "novelty": 0.5, "threat": 3.5, "is_new": True, "total": 3,
                "recent": 1, "tag_observations": 1, "text_observations": 2,
                "direct_signal_observations": 1, "samples": [_sample()],
            }],
        },
        "economic": {
            "generated_at": DATA_TIME,
            "metric_name": "observed_economic_topic_share",
            "scope": "Share of observed scored articles matching declared economic terms.",
            "pct": 33,
            "n_econ_articles": 1,
            "ranked": [{"term": "housing", "weight": 1.5, "samples": [_sample()]}],
        },
        "leads": [{
            "term": "housing", "domain": "ECONOMY", "score": 3.0, "threat": 3.5,
            "novelty": 0.5, "n": 3, "is_new": True, "samples": [_sample()],
        }],
    }


def _wire(document: dict | None = None) -> bytes:
    return importer.serialize_snapshot(document or _snapshot())


def _tag(payload: bytes, key: bytes = KEY) -> bytes:
    digest = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return f"hmac-sha256={digest}\n".encode("ascii")


def _paired_fetch(payload: bytes, *, signature: bytes | None = None, calls=None):
    def fetch(url, **kwargs):
        if calls is not None:
            calls.append((url, kwargs))
        return signature if urlsplit_path(url).endswith(".hmac-sha256") else payload
    return fetch


def urlsplit_path(url: str) -> str:
    from urllib.parse import urlsplit
    return urlsplit(url).path


def test_unconfigured_import_is_a_noop_and_preserves_optional_absence(tmp_path, monkeypatch):
    output = tmp_path / "readings" / "nemesis-latest.json"
    monkeypatch.delenv(importer.URL_ENV, raising=False)
    monkeypatch.delenv(importer.HMAC_KEY_ENV, raising=False)

    assert importer.main(["--output", str(output)]) == 0
    assert not output.exists()


@pytest.mark.parametrize("url", [
    "http://example.org/public.json",
    "file:///tmp/public.json",
    "https://user:password@example.org/public.json",
    "https://example.org/public.json#fragment",
    "https://example.org/public.json?token=logged",
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
        importer.import_snapshot(url, hmac_key=KEY, fetcher=forbidden)
    assert called is False


def test_sidecar_suffix_is_applied_to_the_exact_query_free_path():
    assert importer.signature_url("https://example.org/public.json") == (
        "https://example.org/public.json.hmac-sha256")


def test_valid_snapshot_is_authenticated_bounded_and_atomically_public(tmp_path):
    output = tmp_path / "readings" / "nemesis-latest.json"
    payload = _wire()
    calls = []

    imported = importer.import_snapshot(
        "https://runtime.example/public.json",
        hmac_key=KEY,
        output=output,
        fetcher=_paired_fetch(payload, signature=_tag(payload), calls=calls),
        now=NOW,
    )

    assert imported == _snapshot()
    assert [urlsplit_path(call[0]) for call in calls] == [
        "/public.json", "/public.json.hmac-sha256"]
    assert all(call[1]["max_redirects"] == 0 for call in calls)
    assert calls[0][1]["max_bytes"] == importer.MAX_BYTES
    assert calls[1][1]["max_bytes"] == importer.MAX_SIGNATURE_BYTES
    assert json.loads(output.read_text(encoding="utf-8")) == _snapshot()
    assert output.read_bytes().endswith(b"\n")
    assert stat.S_IMODE(output.stat().st_mode) == 0o644


def test_pair_publication_mismatch_refetches_both_and_accepts_only_converged_pair(tmp_path):
    first = _wire()
    second_document = _snapshot()
    second_document["scope"] = second_document["coverage"]["scope"] = (
        SCOPE + " Updated public scope.")
    second = _wire(second_document)
    round_number = {"value": 0}
    calls = []

    def changing_pair(url, **_kwargs):
        calls.append(url)
        is_signature = urlsplit_path(url).endswith(".hmac-sha256")
        if not is_signature:
            round_number["value"] += 1
            return first if round_number["value"] == 1 else second
        return _tag(second)

    imported = importer.import_snapshot(
        "https://runtime.example/public.json",
        hmac_key=KEY,
        output=tmp_path / "out.json",
        fetcher=changing_pair,
        now=NOW,
    )
    assert imported["scope"].endswith("Updated public scope.")
    assert len(calls) == 4


def test_persistent_signature_mismatch_fails_after_three_pairs_without_publish(tmp_path):
    output = tmp_path / "out.json"
    calls = []
    payload = _wire()
    with pytest.raises(importer.SnapshotImportError, match="bounded pair refetch"):
        importer.import_snapshot(
            "https://runtime.example/public.json",
            hmac_key=KEY,
            output=output,
            fetcher=_paired_fetch(payload, signature=_tag(b"different"), calls=calls),
            now=NOW,
        )
    assert len(calls) == importer.PAIR_ATTEMPTS * 2
    assert not output.exists()


@pytest.mark.parametrize("signature", [
    b"", b"hmac-sha256=ABC\n", b"hmac-sha256=" + b"0" * 64,
    b"hmac-sha256=" + b"0" * 64 + b"\nextra",
])
def test_signature_sidecar_format_is_exact(signature, tmp_path):
    payload = _wire()
    with pytest.raises(importer.SnapshotImportError, match="sidecar is malformed"):
        importer.import_snapshot(
            "https://runtime.example/public.json", hmac_key=KEY,
            output=tmp_path / "out.json",
            fetcher=_paired_fetch(payload, signature=signature), now=NOW)


def test_short_or_missing_key_is_rejected_before_egress(tmp_path):
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        return b""

    with pytest.raises(importer.SnapshotImportError, match="at least 32 bytes"):
        importer.import_snapshot(
            "https://runtime.example/public.json", hmac_key="short",
            output=tmp_path / "out.json", fetcher=forbidden)
    assert called is False


def test_download_failures_do_not_leak_url_or_key(tmp_path):
    secret_url_part = "opaque-url-secret"
    secret_key = b"key-material-must-never-appear-123456"

    def unavailable(url, **_kwargs):
        raise FetchError("upstream mentioned " + url)

    with pytest.raises(importer.SnapshotImportError) as exc_info:
        importer.import_snapshot(
            f"https://runtime.example/{secret_url_part}/public.json",
            hmac_key=secret_key,
            output=tmp_path / "out.json",
            fetcher=unavailable,
        )
    rendered = str(exc_info.value)
    assert secret_url_part not in rendered
    assert secret_key.decode() not in rendered


def test_redirect_is_fail_loud_and_does_not_publish(tmp_path):
    output = tmp_path / "out.json"

    def redirecting(_url, **kwargs):
        assert kwargs["max_redirects"] == 0
        raise TooManyRedirects("redirect attempted")

    with pytest.raises(importer.SnapshotImportError, match="download failed"):
        importer.import_snapshot(
            "https://runtime.example/public.json", hmac_key=KEY,
            output=output, fetcher=redirecting)
    assert not output.exists()


def test_real_fetch_default_is_the_raw_bytes_seam():
    assert importer.import_snapshot.__kwdefaults__["fetcher"] is safe_fetch_bytes


@pytest.mark.parametrize("payload", [
    b"\xff\xfe{}",
    b'{"schema": NaN}',
    b'{"schema": "one", "schema": "two"}',
    b"[]",
    b"not json",
])
def test_parser_rejects_ambiguous_invalid_or_non_utf8_json(payload):
    with pytest.raises(importer.SnapshotImportError):
        importer._parse_document(payload)


def test_parser_rejects_replacement_decoded_text_from_an_injected_fetcher(tmp_path):
    payload = _wire()
    with pytest.raises(importer.SnapshotImportError, match="raw bytes"):
        importer.import_snapshot(
            "https://runtime.example/public.json", hmac_key=KEY,
            output=tmp_path / "out.json",
            fetcher=lambda url, **_kwargs: (
                _tag(payload).decode() if urlsplit_path(url).endswith(".hmac-sha256")
                else payload.decode()
            ),
            now=NOW,
        )


def _nested_exfiltration_cases():
    return [
        lambda d: d.update({"operator_secret": "never"}),
        lambda d: d["methods"].update({"private_method": "never"}),
        lambda d: d["timestamps"].update({"private_path": "/var/lib/runtime"}),
        lambda d: d["health"].update({"api_key": "never"}),
        lambda d: d["health"]["freshness"].update({"source_ip": "10.0.0.1"}),
        lambda d: d["health"]["last_run"].update({"error_detail": "token=never"}),
        lambda d: d["coverage"].update({"operator": "never"}),
        lambda d: d["coverage"]["observed_sources"][0].update({"source_ip": "10.0.0.1"}),
        lambda d: d["coverage"]["last_successful_cycle"].update({"raw": "never"}),
        lambda d: d["coverage"]["last_successful_cycle"]["sources"][0].update(
            {"exception": "password=never"}),
        lambda d: d["counts"].update({"alerts": 99}),
        lambda d: d["ddti"].update({"raw_posts": ["never"]}),
        lambda d: d["ddti"]["ranked"][0].update({"operator_secret": "never"}),
        lambda d: d["ddti"]["ranked"][0]["samples"][0].update({"source_ip": "10.0.0.1"}),
        lambda d: d["economic"].update({"internal_query": "never"}),
        lambda d: d["economic"]["ranked"][0].update({"raw": "never"}),
        lambda d: d["economic"]["ranked"][0]["samples"][0].update({"token": "never"}),
        lambda d: d["leads"][0].update({"analyst_note": "never"}),
        lambda d: d["leads"][0]["samples"][0].update({"command": "never"}),
    ]


@pytest.mark.parametrize("mutate", _nested_exfiltration_cases())
def test_unknown_field_at_every_nested_level_is_rejected_not_republished(mutate):
    document = _snapshot()
    mutate(document)
    with pytest.raises(importer.SnapshotImportError, match="fields do not match schema"):
        importer.validate_snapshot(document, now=NOW)


def test_private_literal_in_an_allowlisted_text_slot_is_still_rejected():
    document = _snapshot()
    document["ddti"]["ranked"][0]["samples"][0]["title"] = "token=do-not-publish"
    with pytest.raises(importer.SnapshotImportError, match="private literal"):
        importer.validate_snapshot(document, now=NOW)


def test_schema_health_counts_and_timestamps_are_semantically_strict():
    bad_documents = []
    for mutate in (
        lambda d: d.update({"schema_version": "2.0.0"}),
        lambda d: d.update({"source": "lookalike"}),
        lambda d: d["health"].update({"ready": False}),
        lambda d: d.update({"generated_at": NOW + importer.MAX_FUTURE_SKEW_SECONDS + 1}),
        lambda d: d.update({"data_timestamp": True}),
        lambda d: d["health"]["freshness"].update({"core_data_at": DATA_TIME - 1}),
        lambda d: d["counts"].update({"topics": 2}),
        lambda d: d["coverage"].update({"observed_source_count": 2}),
    ):
        document = _snapshot()
        mutate(document)
        bad_documents.append(document)
    for document in bad_documents:
        with pytest.raises(importer.SnapshotImportError):
            importer.validate_snapshot(document, now=NOW)


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


def test_signed_older_replay_cannot_replace_the_published_high_water_mark(tmp_path):
    output = tmp_path / "nemesis-latest.json"
    current = _snapshot()
    importer.write_atomic(current, output)
    previous_bytes = output.read_bytes()
    replay = deepcopy(current)
    replay["generated_at"] -= 1
    replay["data_timestamp"] -= 1
    replay["timestamps"]["ddti_generated_at"] -= 1
    replay["timestamps"]["economic_generated_at"] -= 1
    replay["health"]["freshness"]["core_data_at"] -= 1
    replay["ddti"]["generated_at"] -= 1
    replay["economic"]["generated_at"] -= 1
    replay["health"]["freshness"]["age_seconds"] = (
        replay["generated_at"] - replay["data_timestamp"])
    payload = _wire(replay)

    with pytest.raises(importer.SnapshotImportError, match="roll back"):
        importer.import_snapshot(
            "https://runtime.example/public.json", hmac_key=KEY, output=output,
            fetcher=_paired_fetch(payload, signature=_tag(payload)), now=NOW)
    assert output.read_bytes() == previous_bytes


def test_equal_generation_is_idempotent_only_for_identical_canonical_bytes(tmp_path):
    output = tmp_path / "nemesis-latest.json"
    current = _snapshot()
    importer.write_atomic(current, output)
    identical = _wire(current)
    importer.import_snapshot(
        "https://runtime.example/public.json", hmac_key=KEY, output=output,
        fetcher=_paired_fetch(identical, signature=_tag(identical)), now=NOW)

    equivocation = deepcopy(current)
    equivocation["economic"]["scope"] = "Different valid public scope at the same generation."
    payload = _wire(equivocation)
    with pytest.raises(importer.SnapshotImportError, match="equivocates"):
        importer.import_snapshot(
            "https://runtime.example/public.json", hmac_key=KEY, output=output,
            fetcher=_paired_fetch(payload, signature=_tag(payload)), now=NOW)


def test_newer_degraded_snapshot_may_advance_without_erasing_evidence_time(tmp_path):
    output = tmp_path / "nemesis-latest.json"
    current = _snapshot()
    importer.write_atomic(current, output)
    newer = deepcopy(current)
    newer["generated_at"] += 5
    newer["status"] = "degraded"
    newer["health"]["status"] = "degraded"
    newer["health"]["ready"] = False
    newer["health"]["reasons"] = ["last_run_error"]
    newer["health"]["freshness"]["age_seconds"] += 5
    payload = _wire(newer)

    imported = importer.import_snapshot(
        "https://runtime.example/public.json", hmac_key=KEY, output=output,
        fetcher=_paired_fetch(payload, signature=_tag(payload)), now=NOW)
    assert imported["status"] == "degraded"
    assert imported["data_timestamp"] == current["data_timestamp"]


def test_replayed_ok_snapshot_is_rejected_after_its_freshness_ceiling(tmp_path):
    payload = _wire()
    with pytest.raises(importer.SnapshotImportError, match="freshness ceiling"):
        importer.import_snapshot(
            "https://runtime.example/public.json", hmac_key=KEY,
            output=tmp_path / "out.json",
            fetcher=_paired_fetch(payload, signature=_tag(payload)),
            now=DATA_TIME + 1801,
        )


def test_workflow_scopes_secrets_pins_tools_and_rebuilds_after_each_race():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "persist-credentials: false" in text
    assert "pip install --quiet --require-hashes" in text
    assert "-r .github/osint-china-ci-requirements.txt" in text
    lock = (ROOT / ".github" / "osint-china-ci-requirements.txt").read_text(
        encoding="utf-8"
    )
    assert "pytest==9.1.1" in lock
    assert "env:\n      PYTHONPATH:" in text
    assert text.count("NEMESIS_SNAPSHOT_HMAC_KEY:") == 3
    assert text.count("PALIMPSEST_SCRUB_STRINGS:") == 3
    assert text.count("GITHUB_PUSH_TOKEN:") == 2
    assert text.count("python scripts/push_data_commit.py --base-locked") == 2
    assert text.count("python -m scripts.import_nemesis_snapshot") == 3
    assert text.count("python -m scripts.build_osint_china") == 3
    assert text.count("python scripts/seal_readings.py") == 3
    assert text.count("python scripts/verify_public_surface.py") == 3
    assert text.count("tests/test_import_nemesis_snapshot.py") == 3
    assert text.count("tests/test_osint_china_page.py") == 3
    assert text.count(
        'if [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]; then'
    ) == 2
    assert text.index("git rebase origin/main") < text.index(
        "python -m scripts.import_nemesis_snapshot")
    assert "if: steps.push_attempt.outputs.exit_code == '75'" in text
    race_import = text.rindex("python -m scripts.import_nemesis_snapshot")
    race_build = text.rindex("python -m scripts.build_osint_china")
    race_seal = text.rindex("python scripts/seal_readings.py")
    race_tests = text.rindex("tests/test_import_nemesis_snapshot.py")
    race_scrub = text.rindex("python scripts/verify_public_surface.py")
    final_push = text.rindex("python scripts/push_data_commit.py --base-locked")
    assert race_import < race_build < race_seal < race_tests < race_scrub < final_push
