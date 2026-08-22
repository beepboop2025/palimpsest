"""Offline contract tests for the fixed-origin BLEEDTHROUGH publication relay."""

from __future__ import annotations

from copy import deepcopy
import inspect
import json
from pathlib import Path
import time

import pytest

import scripts.import_bleedthrough_snapshot as importer


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "osint-china-refresh.yml"
CADDY = ROOT / "ops" / "caddy" / "palimpsest-bleedthrough.caddy"
NOW = 1_800_000_000.0
GENERATED = "2027-01-15T07:58:20Z"


def _snapshot() -> dict:
    transports = {
        "direct": {"ran": True, "targets": 3},
        "open_resolver": {"ran": False, "targets": None},
    }
    return {
        "generated_at": GENERATED,
        "last_changed_at": GENERATED,
        "method_version": importer.METHOD_VERSION,
        "signal": importer.SIGNAL,
        "title": importer.TITLE,
        "scope": importer.SCOPE,
        "method": importer._method(transports),
        "probe_domain": importer.PROBE_DOMAIN,
        "vantages_probed": 3,
        "vantages_injecting": 3,
        "distinct_pools": 1,
        "distinct_pools_basis": importer.DISTINCT_POOLS_BASIS,
        "max_process_count": 2,
        "process_count_semantics": importer.PROCESS_COUNT_SEMANTICS,
        "pool_sampling_suspected": False,
        "provenance": {
            "vantage_count": 1,
            "vantage_kind": importer.VANTAGE_KIND,
            "vantage_country": importer.VANTAGE_COUNTRY,
            "flow_id_policy": importer.FLOW_ID_POLICY,
            "burst": 24,
            "rate_per_sec": 5.0,
            "wait_s": 1.2,
            "queries_attempted": 72,
            "transports": transports,
            "code_version": "a" * 40,
            "authorization": {"live_opt_in": True, "fixed_box_opt_in": True},
            "caveat": importer.CAVEAT,
        },
        "events": [],
    }


def _wire(document: dict | None = None) -> bytes:
    return json.dumps(document or _snapshot(), ensure_ascii=False).encode("utf-8")


def _fetch(payload: bytes, calls: list | None = None):
    def fetch(url, **kwargs):
        if calls is not None:
            calls.append((url, kwargs))
        return payload

    return fetch


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _import(tmp_path: Path, document: dict | None = None, **kwargs):
    output = tmp_path / "bleedthrough-latest.json"
    history = tmp_path / "bleedthrough-history.jsonl"
    result = importer.import_snapshot(
        output=output,
        history=history,
        fetcher=_fetch(_wire(document)),
        now=NOW,
        **kwargs,
    )
    return result, output, history


def test_origin_is_a_code_constant_and_fetch_is_bounded_without_redirects(tmp_path):
    calls = []
    output = tmp_path / "latest.json"
    history = tmp_path / "history.jsonl"
    assert importer.LATEST_URL == (
        "https://api.seiche.info/palimpsest/bleedthrough/bleedthrough-latest.json"
    )

    importer.import_snapshot(
        output=output,
        history=history,
        fetcher=_fetch(_wire(), calls),
        now=NOW,
    )

    assert list(inspect.signature(importer.import_snapshot).parameters) == [
        "output",
        "history",
        "fetcher",
        "now",
        "allow_empty_bootstrap_404",
    ]
    assert calls == [
        (
            importer.LATEST_URL,
            {
                "max_bytes": 256 * 1024,
                "timeout": 15.0,
                "max_redirects": 0,
                "headers": {
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            },
        )
    ]
    source = (ROOT / "scripts" / "import_bleedthrough_snapshot.py").read_text(
        encoding="utf-8"
    )
    assert "BLEEDTHROUGH_SNAPSHOT_URL" not in source
    assert "--url" not in source


def test_explicit_bootstrap_flag_allows_only_the_initial_exact_404(tmp_path):
    output = tmp_path / "latest.json"
    history = tmp_path / "history.jsonl"

    def not_published(_url, **_kwargs):
        raise importer.FetchError("http status 404")

    result = importer.import_snapshot(
        output=output,
        history=history,
        fetcher=not_published,
        now=NOW,
        allow_empty_bootstrap_404=True,
    )

    assert result is None
    assert not output.exists()
    assert not history.exists()

    with pytest.raises(importer.BleedthroughImportError, match="download failed"):
        importer.import_snapshot(
            output=output,
            history=history,
            fetcher=not_published,
            now=NOW,
        )


@pytest.mark.parametrize("existing_name", ["latest.json", "history.jsonl"])
def test_bootstrap_404_is_fatal_after_either_local_artifact_exists(
    tmp_path, existing_name
):
    output = tmp_path / "latest.json"
    history = tmp_path / "history.jsonl"
    (tmp_path / existing_name).write_bytes(b"prior-publication-state\n")

    def disappeared(_url, **_kwargs):
        raise importer.FetchError("http status 404")

    with pytest.raises(importer.BleedthroughImportError, match="download failed"):
        importer.import_snapshot(
            output=output,
            history=history,
            fetcher=disappeared,
            now=NOW,
            allow_empty_bootstrap_404=True,
        )


def test_bootstrap_flag_never_excuses_other_fetch_or_content_failures(tmp_path):
    output = tmp_path / "latest.json"
    history = tmp_path / "history.jsonl"

    def unavailable(_url, **_kwargs):
        raise importer.FetchError("http status 503")

    with pytest.raises(importer.BleedthroughImportError, match="download failed"):
        importer.import_snapshot(
            output=output,
            history=history,
            fetcher=unavailable,
            now=NOW,
            allow_empty_bootstrap_404=True,
        )
    with pytest.raises(importer.BleedthroughImportError, match="valid bounded JSON"):
        importer.import_snapshot(
            output=output,
            history=history,
            fetcher=_fetch(b"not-json"),
            now=NOW,
            allow_empty_bootstrap_404=True,
        )

    importer.import_snapshot(
        output=output,
        history=history,
        fetcher=_fetch(_wire()),
        now=NOW,
        allow_empty_bootstrap_404=True,
    )
    equivocation = _snapshot()
    equivocation["max_process_count"] += 1
    with pytest.raises(importer.BleedthroughImportError, match="equivocated"):
        importer.import_snapshot(
            output=output,
            history=history,
            fetcher=_fetch(_wire(equivocation)),
            now=NOW,
            allow_empty_bootstrap_404=True,
        )


def test_cli_bootstrap_flag_reports_pending_without_writing(
    monkeypatch, tmp_path, capsys
):
    calls = []

    def bootstrap(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(importer, "import_snapshot", bootstrap)
    result = importer.main(
        [
            "--output",
            str(tmp_path / "latest.json"),
            "--history",
            str(tmp_path / "history.jsonl"),
            "--allow-empty-bootstrap-404",
        ]
    )

    assert result == 0
    assert calls[0]["allow_empty_bootstrap_404"] is True
    assert "bootstrap pending" in capsys.readouterr().out


def test_hard_deadline_stops_a_trickling_or_stalled_fetcher(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "TIMEOUT_SECONDS", 0.02)

    def stalled(_url, **_kwargs):
        time.sleep(1.0)
        return _wire()

    started = time.monotonic()
    with pytest.raises(importer.BleedthroughImportError, match="download failed"):
        importer.import_snapshot(
            output=tmp_path / "latest.json",
            history=tmp_path / "history.jsonl",
            fetcher=stalled,
            now=NOW,
        )
    assert time.monotonic() - started < 0.5


def test_valid_document_is_reconstructed_and_atomically_published(tmp_path):
    document, output, history = _import(tmp_path)

    assert _load(output) == document
    assert output.read_bytes().endswith(b"\n")
    assert history.read_bytes().endswith(b"\n")
    assert _history(history) == [
        importer._history_row(document, timestamp=document["last_changed_at"])
    ]
    assert output.stat().st_mode & 0o777 == 0o644
    assert history.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update({"operator_contact": "desk@example.org"}),
        lambda d: d["provenance"].update({"hostname": "probe-1.internal"}),
        lambda d: d["provenance"].update({"code_version": "a" * 12}),
        lambda d: d["provenance"]["authorization"].update({"fixed_box_opt_in": False}),
        lambda d: d["provenance"].update({"vantage_country": "US"}),
        lambda d: d.update({"method_version": True}),
    ],
)
def test_schema_and_provenance_are_closed_and_exact(tmp_path, mutation):
    document = _snapshot()
    mutation(document)

    with pytest.raises(importer.BleedthroughImportError):
        _import(tmp_path, document)
    assert not (tmp_path / "bleedthrough-latest.json").exists()


@pytest.mark.parametrize(
    ("vantage", "detail"),
    [
        ("203.0.113.7@CN-HA/AS4837", "forged-IP pool rotated"),
        ("probe-01.example.net", "forged-IP pool rotated"),
        ("CN-HA/AS4837", "contact analyst@example.org"),
        ("CN-HA/AS4837", "/home/palimpsest/private-targets.json"),
        ("CN-HA/AS4837", "+49 30 1234 5678"),
    ],
)
def test_event_boundary_rejects_ip_host_person_and_contact_leakage(
    tmp_path, vantage, detail
):
    document = _snapshot()
    document["events"] = [
        {
            "kind": "pool_rotation",
            "vantage": vantage,
            "detail": detail,
            "severity": "low",
        }
    ]

    with pytest.raises(importer.BleedthroughImportError):
        _import(tmp_path, document)


def test_coarse_event_and_allowlisted_semantics_are_accepted(tmp_path):
    document = _snapshot()
    document["events"] = [
        {
            "kind": "regional_firewall_candidate",
            "vantage": "CN-HA/AS4837",
            "detail": "regional forged-IP pool diverged from the shared baseline",
            "severity": "high",
        }
    ]
    document["distinct_pools"] = 2

    imported, _output, _history_path = _import(tmp_path, document)
    assert imported["events"] == document["events"]


def test_repeated_target_events_collapse_at_the_public_boundary(tmp_path):
    document = _snapshot()
    event = {
        "kind": "pool_rotation",
        "vantage": "CN-HA/AS4837",
        "detail": "forged-IP pool rotated",
        "severity": "low",
    }
    document["events"] = [dict(event) for _ in range(210)]

    imported, _output, history_path = _import(tmp_path, document)

    assert imported["events"] == [event]
    history = [json.loads(line) for line in history_path.read_text().splitlines()]
    assert history[-1]["n_events"] == 1


def test_sampled_pools_cannot_publish_a_regional_claim(tmp_path):
    document = _snapshot()
    document["events"] = [
        {
            "kind": "regional_firewall_candidate",
            "vantage": "CN-HA/AS4837",
            "detail": "regional forged-IP pool diverged from the shared baseline",
            "severity": "high",
        }
    ]
    document["pool_sampling_suspected"] = True

    with pytest.raises(importer.BleedthroughImportError, match="cannot support"):
        _import(tmp_path, document)


def test_regional_claim_requires_at_least_two_distinct_pools(tmp_path):
    document = _snapshot()
    document["events"] = [
        {
            "kind": "regional_firewall_candidate",
            "vantage": "CN-HA/AS4837",
            "detail": "regional forged-IP pool diverged from the shared baseline",
            "severity": "high",
        }
    ]

    with pytest.raises(importer.BleedthroughImportError, match="at least two"):
        _import(tmp_path, document)


def test_regional_claim_cannot_fall_back_to_the_national_bucket(tmp_path):
    document = _snapshot()
    document["distinct_pools"] = 2
    document["events"] = [
        {
            "kind": "regional_firewall_candidate",
            "vantage": "CN",
            "detail": "regional forged-IP pool diverged from the shared baseline",
            "severity": "high",
        }
    ]

    with pytest.raises(importer.BleedthroughImportError, match="subnational"):
        _import(tmp_path, document)


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b'{"generated_at":"a","generated_at":"b"}',
        b'{"generated_at":NaN}',
        b"[]",
        b"x" * (256 * 1024 + 1),
    ],
)
def test_malformed_nonfinite_duplicate_and_oversized_payloads_are_rejected(
    tmp_path, payload
):
    with pytest.raises(importer.BleedthroughImportError):
        importer.import_snapshot(
            output=tmp_path / "latest.json",
            history=tmp_path / "history.jsonl",
            fetcher=_fetch(payload),
            now=NOW,
        )


def test_clock_order_future_and_rollback_are_rejected_without_touching_last_good(
    tmp_path,
):
    _first, output, history = _import(tmp_path)
    last_latest = output.read_bytes()
    last_history = history.read_bytes()

    future = _snapshot()
    future["generated_at"] = future["last_changed_at"] = "2099-01-01T00:00:00Z"
    with pytest.raises(importer.BleedthroughImportError, match="accepted clock"):
        _import(tmp_path, future)

    reversed_clock = _snapshot()
    reversed_clock["last_changed_at"] = "2027-01-15T07:59:20Z"
    with pytest.raises(importer.BleedthroughImportError, match="after generated_at"):
        _import(tmp_path, reversed_clock)

    rollback = _snapshot()
    rollback["generated_at"] = rollback["last_changed_at"] = "2027-01-15T07:57:20Z"
    with pytest.raises(importer.BleedthroughImportError, match="roll back"):
        _import(tmp_path, rollback)

    assert output.read_bytes() == last_latest
    assert history.read_bytes() == last_history


def test_heartbeat_advances_latest_without_polluting_semantic_history(tmp_path):
    first, output, history = _import(tmp_path)
    heartbeat = deepcopy(first)
    heartbeat["generated_at"] = "2027-01-15T07:59:20Z"

    imported, _output, _history_path = _import(tmp_path, heartbeat)

    assert _load(output) == imported
    assert imported["last_changed_at"] == first["last_changed_at"]
    assert len(_history(history)) == 1


def test_heartbeat_cannot_move_last_changed_without_evidence(tmp_path):
    first, _output, history = _import(tmp_path)
    dishonest = deepcopy(first)
    dishonest["generated_at"] = dishonest["last_changed_at"] = "2027-01-15T07:59:20Z"

    with pytest.raises(importer.BleedthroughImportError, match="without an observed"):
        _import(tmp_path, dishonest)
    assert len(_history(history)) == 1


def test_semantic_change_requires_and_appends_a_new_change_timestamp(tmp_path):
    _first, _output, history = _import(tmp_path)
    changed = _snapshot()
    changed["generated_at"] = changed["last_changed_at"] = "2027-01-15T07:59:20Z"
    changed["max_process_count"] = 3

    imported, _output, _history_path = _import(tmp_path, changed)

    rows = _history(history)
    assert len(rows) == 2
    assert rows[-1] == importer._history_row(
        imported, timestamp=imported["generated_at"]
    )

    dishonest = deepcopy(changed)
    dishonest["generated_at"] = "2027-01-15T08:00:20Z"
    dishonest["max_process_count"] = 4
    with pytest.raises(importer.BleedthroughImportError, match="without moving"):
        _import(tmp_path, dishonest)
    assert len(_history(history)) == 2


def test_new_semantics_cannot_claim_a_change_before_the_last_good_observation(tmp_path):
    first, _output, history = _import(tmp_path)
    previous = deepcopy(first)
    previous["generated_at"] = "2027-01-15T07:59:00Z"
    previous, _output, _history_path = _import(tmp_path, previous)
    backdated = deepcopy(previous)
    backdated["generated_at"] = "2027-01-15T07:59:20Z"
    backdated["last_changed_at"] = "2027-01-15T07:58:40Z"
    backdated["max_process_count"] = 3

    with pytest.raises(importer.BleedthroughImportError, match="predates"):
        _import(tmp_path, backdated)
    assert len(_history(history)) == 1


def test_method_upgrade_retains_the_supported_history_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "METHOD_VERSION", 2)
    _first, output, history = _import(tmp_path)
    monkeypatch.setattr(importer, "METHOD_VERSION", 3)
    upgraded = _snapshot()
    upgraded["generated_at"] = upgraded["last_changed_at"] = "2027-01-15T07:59:20Z"

    imported, _output, _history_path = _import(tmp_path, upgraded)

    assert _load(output) == imported
    assert [row["method_version"] for row in _history(history)] == [2, 3]


def test_stored_v2_latest_is_accepted_during_a_v3_publish(tmp_path):
    legacy = _snapshot()
    legacy["method_version"] = 2
    legacy["method"] = importer._method(
        legacy["provenance"]["transports"], method_version=2
    )
    output = tmp_path / "bleedthrough-latest.json"
    history = tmp_path / "bleedthrough-history.jsonl"
    output.write_text(json.dumps(legacy), encoding="utf-8")
    history.write_text(
        json.dumps(
            importer._history_row(legacy, timestamp=legacy["last_changed_at"]),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    upgraded = _snapshot()
    upgraded["generated_at"] = upgraded["last_changed_at"] = (
        "2027-01-15T07:59:20Z"
    )
    imported = importer.import_snapshot(
        output=output,
        history=history,
        fetcher=_fetch(_wire(upgraded)),
        now=NOW,
    )

    assert "Direct receive windows overlap" not in legacy["method"]
    assert imported["method_version"] == 3
    assert [row["method_version"] for row in _history(history)] == [2, 3]
    with pytest.raises(importer.BleedthroughImportError, match="unsupported"):
        importer.validate_document(legacy, now=NOW, require_current_method=True)


def test_history_write_failure_preserves_both_last_good_files(tmp_path, monkeypatch):
    _first, output, history = _import(tmp_path)
    old_latest = output.read_bytes()
    old_history = history.read_bytes()
    changed = _snapshot()
    changed["generated_at"] = changed["last_changed_at"] = "2027-01-15T07:59:20Z"
    changed["max_process_count"] = 3
    real_replace = importer.os.replace

    def fail_history(source, destination):
        if Path(destination) == history.resolve():
            raise OSError("injected history replacement failure")
        return real_replace(source, destination)

    monkeypatch.setattr(importer.os, "replace", fail_history)
    with pytest.raises(OSError, match="history replacement"):
        _import(tmp_path, changed)

    assert output.read_bytes() == old_latest
    assert history.read_bytes() == old_history
    assert not list(tmp_path.glob(".*.tmp"))


def test_latest_write_failure_keeps_last_good_and_retry_reuses_history_row(
    tmp_path, monkeypatch
):
    _first, output, history = _import(tmp_path)
    old_latest = output.read_bytes()
    changed = _snapshot()
    changed["generated_at"] = changed["last_changed_at"] = "2027-01-15T07:59:20Z"
    changed["max_process_count"] = 3
    real_replace = importer.os.replace

    def fail_latest(source, destination):
        if Path(destination) == output.resolve():
            raise OSError("injected latest replacement failure")
        return real_replace(source, destination)

    monkeypatch.setattr(importer.os, "replace", fail_latest)
    with pytest.raises(OSError, match="latest replacement"):
        _import(tmp_path, changed)

    assert output.read_bytes() == old_latest
    assert len(_history(history)) == 2

    monkeypatch.setattr(importer.os, "replace", real_replace)
    imported, _output, _history_path = _import(tmp_path, changed)
    assert _load(output) == imported
    assert len(_history(history)) == 2, (
        "retry must not duplicate the durable history row"
    )


def test_recovery_accepts_a_later_heartbeat_after_history_landed(tmp_path, monkeypatch):
    _first, output, history = _import(tmp_path)
    changed = _snapshot()
    changed["generated_at"] = changed["last_changed_at"] = "2027-01-15T07:59:00Z"
    changed["max_process_count"] = 3
    real_replace = importer.os.replace

    def fail_latest(source, destination):
        if Path(destination) == output.resolve():
            raise OSError("injected latest replacement failure")
        return real_replace(source, destination)

    monkeypatch.setattr(importer.os, "replace", fail_latest)
    with pytest.raises(OSError, match="latest replacement"):
        _import(tmp_path, changed)
    assert len(_history(history)) == 2

    # The producer completed another identical round before the publication retry. Its
    # observation clock moves, but the already-durable history row remains the proof of when
    # the semantic transition occurred.
    heartbeat = deepcopy(changed)
    heartbeat["generated_at"] = "2027-01-15T07:59:20Z"
    monkeypatch.setattr(importer.os, "replace", real_replace)

    imported, _output, _history_path = _import(tmp_path, heartbeat)

    assert _load(output) == imported
    assert imported["last_changed_at"] == changed["last_changed_at"]
    assert len(_history(history)) == 2


@pytest.mark.parametrize("revert", [False, True])
def test_recovery_appends_a_later_change_after_an_unpublished_durable_row(
    tmp_path, monkeypatch, revert
):
    original, output, history = _import(tmp_path)
    intermediate = deepcopy(original)
    intermediate["generated_at"] = intermediate["last_changed_at"] = (
        "2027-01-15T07:59:00Z"
    )
    intermediate["max_process_count"] = 3
    real_replace = importer.os.replace

    def fail_latest(source, destination):
        if Path(destination) == output.resolve():
            raise OSError("injected latest replacement failure")
        return real_replace(source, destination)

    monkeypatch.setattr(importer.os, "replace", fail_latest)
    with pytest.raises(OSError, match="latest replacement"):
        _import(tmp_path, intermediate)
    monkeypatch.setattr(importer.os, "replace", real_replace)

    later = deepcopy(original if revert else intermediate)
    later["generated_at"] = later["last_changed_at"] = "2027-01-15T07:59:20Z"
    if not revert:
        later["max_process_count"] = 4

    imported, _output, _history_path = _import(tmp_path, later)

    assert _load(output) == imported
    assert len(_history(history)) == 3
    assert _history(history)[-1] == importer._history_row(
        imported, timestamp=imported["last_changed_at"]
    )


def test_impossible_non_tail_history_row_blocks_publication(tmp_path):
    document, output, history = _import(tmp_path)
    valid_tail = _history(history)[0]
    impossible = deepcopy(valid_tail)
    impossible["generated_at"] = "2027-01-15T07:57:20Z"
    impossible["vantages_injecting"] = impossible["vantages_probed"] + 1
    history.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in (impossible, valid_tail))
        + "\n",
        encoding="utf-8",
    )
    heartbeat = deepcopy(document)
    heartbeat["generated_at"] = "2027-01-15T07:59:20Z"

    with pytest.raises(
        importer.BleedthroughImportError, match="injecting targets exceed"
    ):
        _import(tmp_path, heartbeat)
    assert _load(output) == document


def test_torn_or_oversized_existing_history_blocks_publication(tmp_path):
    output = tmp_path / "latest.json"
    history = tmp_path / "history.jsonl"
    history.write_bytes(b'{"torn":true}')

    with pytest.raises(importer.BleedthroughImportError, match="torn"):
        importer.import_snapshot(
            output=output,
            history=history,
            fetcher=_fetch(_wire()),
            now=NOW,
        )
    assert not output.exists()


def test_workflow_imports_tests_and_stages_the_artifacts_in_every_race_path():
    text = WORKFLOW.read_text(encoding="utf-8")
    boundaries = (
        (
            "- name: Import the pinned BLEEDTHROUGH public aggregate",
            "- name: Re-import external aggregates after a pre-publication ledger change",
            None,
        ),
        (
            "- name: Re-import external aggregates after a pre-publication ledger change",
            "- name: Attempt the verified push",
            "if: steps.prepublish_sync.outputs.rebuild == 'true'",
        ),
        (
            "- name: Re-import external aggregates after a push race",
            "- name: Push the race-safe rebuilt commit",
            "if: steps.push_attempt.outcome == 'failure'",
        ),
    )
    for start_marker, end_marker, condition in boundaries:
        branch = text[
            text.index(start_marker) : text.index(end_marker, text.index(start_marker))
        ]
        assert branch.count("python -m scripts.import_bleedthrough_snapshot") == 1
        assert branch.count(
            "python -m scripts.import_bleedthrough_snapshot --allow-empty-bootstrap-404"
        ) == 1
        assert branch.count("python -m scripts.build_osint_china") == 1
        assert branch.count("tests/test_import_bleedthrough_snapshot.py") == 1
        assert branch.count("readings/bleedthrough-latest.json") == 1
        assert branch.count("readings/bleedthrough-history.jsonl") == 1
        assert branch.index(
            "python -m scripts.import_bleedthrough_snapshot"
        ) < branch.index("python -m scripts.build_osint_china")
        assert branch.index("python -m scripts.build_osint_china") < branch.index(
            "tests/test_import_bleedthrough_snapshot.py"
        )
        if condition is not None:
            assert branch.count(condition) >= 4


def test_caddy_contract_exposes_only_the_two_exact_no_store_files():
    text = CADDY.read_text(encoding="utf-8")
    assert text.count("/palimpsest/bleedthrough/bleedthrough-latest.json") == 1
    assert text.count("/palimpsest/bleedthrough/bleedthrough-history.jsonl") == 1
    assert "/palimpsest/bleedthrough/*" not in text
    assert "root * /var/lib/palimpsest/readings" in text
    assert 'header Cache-Control "no-store, no-transform"' in text
    assert "file_server browse" not in text


def test_publication_contract_graduates_bleedthrough_from_pending_to_scheduled():
    text = (ROOT / "tests" / "test_publication_contract.py").read_text(encoding="utf-8")
    pending = text[text.index("PENDING =") : text.index("OPTIONAL_EXTERNAL =")]
    scheduled = text[
        text.index("SCHEDULED_PUBLICATIONS =") : text.index(
            "def test_no_contract_entry"
        )
    ]
    assert '"bleedthrough"' not in pending
    assert '"bleedthrough"' in scheduled
