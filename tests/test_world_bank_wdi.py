"""Fail-closed contracts for the licensed World Bank China-history bootstrap."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import scripts.china_econ_wdi_pull as wdi_pull

from collectors.world_bank_wdi import (
    WDIError,
    WDIRegistry,
    build_url,
    fetch_bytes,
    load_registry,
    parse_response,
)
from core.econ_ledger import append_vintages, load_snapshot
from scripts.china_econ_wdi_pull import DEFAULT_LEDGER, main as pull_main


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "china_econ_wdi_series.json"
COLLECTED_AT = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
SMALL_INDICATORS = (
    "AG.PRD.CREL.MT",
    "IS.SHP.GOOD.TU",
    "FM.LBL.BMNY.ZG",
)


def _small_registry(*indicator_ids: str) -> WDIRegistry:
    selected = indicator_ids or SMALL_INDICATORS
    full = load_registry(REGISTRY)
    return WDIRegistry(
        dataset=dict(full.dataset),
        bindings={indicator_id: full.bindings[indicator_id] for indicator_id in selected},
    )


def _small_registry_path(tmp_path: Path, *indicator_ids: str) -> Path:
    selected = set(indicator_ids or SMALL_INDICATORS)
    document = json.loads(REGISTRY.read_text(encoding="utf-8"))
    document["series"] = [
        row for row in document["series"] if row["indicator_id"] in selected
    ]
    path = tmp_path / f"registry-{len(selected)}.json"
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    return path


def _row(
    indicator_id: str,
    year: int,
    value: float | int | None,
    *,
    title: str | None = None,
    footnote: str = "",
) -> dict:
    titles = {
        "AG.PRD.CREL.MT": "Cereal production (metric tons)",
        "IS.SHP.GOOD.TU": "Container port traffic (TEU: 20 foot equivalent units)",
        "FM.LBL.BMNY.ZG": "Broad money growth (annual %)",
    }
    return {
        "indicator": {"id": indicator_id, "value": title or titles[indicator_id]},
        "country": {"id": "CN", "value": "China"},
        "countryiso3code": "CHN",
        "date": str(year),
        "value": value,
        "unit": "",
        "scale": "",
        "obs_status": "",
        "decimal": 0,
        "footnote": footnote,
    }


def _default_rows() -> list[dict]:
    return [
        _row(
            "AG.PRD.CREL.MT",
            2024,
            652_290_000,
            footnote="Series reviewed after the latest agricultural census.",
        ),
        _row("IS.SHP.GOOD.TU", 2023, 310_000_000),
        _row("FM.LBL.BMNY.ZG", 2024, None),
    ]


def _response(*, rows: list[dict] | None = None, **metadata_changes: object) -> bytes:
    rows = deepcopy(_default_rows() if rows is None else rows)
    metadata = {
        "page": 1,
        "pages": 1,
        "per_page": 20_000,
        "total": len(rows),
        "sourceid": None,
        "lastupdated": "2026-07-13",
    }
    metadata.update(metadata_changes)
    return json.dumps([metadata, rows], separators=(",", ":")).encode()


def _parse(
    raw: bytes | None = None,
    *,
    registry: WDIRegistry | None = None,
    start_year: int = 2023,
    end_year: int = 2024,
    collected_at: datetime = COLLECTED_AT,
):
    scoped_registry = registry or _small_registry()
    return parse_response(
        raw or _response(),
        registry=scoped_registry,
        evidence_url=build_url(
            scoped_registry,
            start_year=start_year,
            end_year=end_year,
        ),
        start_year=start_year,
        end_year=end_year,
        collected_at=collected_at,
    )


def test_registry_is_broad_licensed_and_bounded_to_one_request():
    registry = load_registry(REGISTRY)

    assert 50 <= len(registry.bindings) <= 60
    assert registry.dataset["license"] == "CC-BY-4.0"
    assert registry.dataset["redistribution_status"] == "allowed"
    assert registry.dataset["per_indicator_upstream_metadata_status"] == "residual_gate"
    assert {binding.domain for binding in registry.bindings.values()} >= {
        "activity",
        "agriculture",
        "commodities",
        "credit",
        "investment",
        "labor",
        "logistics",
        "trade",
    }
    assert all(binding.market_channels for binding in registry.bindings.values())


def test_request_is_keyless_host_pinned_and_explicit_about_shape():
    registry = load_registry(REGISTRY)
    url = build_url(registry, start_year=1960, end_year=2026)
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https" and parsed.hostname == "api.worldbank.org"
    assert parsed.path.startswith("/v2/country/CHN/indicator/")
    assert len(parsed.path.rsplit("/", 1)[1].split(";")) == len(registry.bindings)
    assert query == {
        "source": ["2"],
        "date": ["1960:2026"],
        "format": ["json"],
        "per_page": ["20000"],
        "footnote": ["y"],
    }


def test_fetch_uses_hardened_fixed_host_transport_without_redirects():
    registry = _small_registry()
    url = build_url(registry, start_year=2024, end_year=2024)
    calls: list[tuple[str, dict]] = []

    def fake_fetcher(request_url: str, **kwargs: object) -> bytes:
        calls.append((request_url, kwargs))
        return b"exact-response-bytes"

    assert fetch_bytes(url, retries=0, fetcher=fake_fetcher) == b"exact-response-bytes"
    assert calls == [
        (
            url,
            {
                "max_bytes": 16 * 1024 * 1024,
                "timeout": 45.0,
                "max_redirects": 0,
                "headers": {
                    "User-Agent": (
                        "palimpsest.info observatory (World Bank WDI China aggregate "
                        "ingest; contact desk@palimpsest.info)"
                    )
                },
            },
        )
    ]


def test_parser_preserves_history_provenance_three_clocks_and_null_availability():
    parsed = _parse()

    assert len(parsed.observations) == 2
    assert parsed.source_rows == 3 and parsed.null_rows == 1
    assert parsed.represented_indicators == tuple(sorted(SMALL_INDICATORS))
    assert parsed.populated_indicators == tuple(sorted(SMALL_INDICATORS[:2]))
    cereal = next(row for row in parsed.observations if "cereal_production" in row.series_id)
    assert cereal.value == 652_290_000
    assert cereal.period_start.isoformat() == "2024-01-01"
    assert cereal.period_end.isoformat() == "2024-12-31"
    assert cereal.released_at.isoformat() == "2026-07-13T23:59:59+00:00"
    assert cereal.collected_at == COLLECTED_AT
    assert cereal.raw_sha256 != parsed.raw_sha256
    assert cereal.metadata == {
        "family": "wdi_officially_recognized_sources",
        "source_series_id": "AG.PRD.CREL.MT",
        "source_document_version": "2026-07-13",
        "parser_version": "world-bank-wdi-json.v1",
        "release_time_semantics": "dataset_lastupdated_upper_bound",
        "aggregation_window": "calendar_year",
    }
    provenance = {row.indicator_id: row for row in parsed.indicator_provenance}
    assert provenance["AG.PRD.CREL.MT"].source_title == (
        "Cereal production (metric tons)"
    )
    assert provenance["AG.PRD.CREL.MT"].reviewed_name == "Cereal production"
    availability = {
        (row.indicator_id, row.year): row for row in parsed.availability
    }
    assert availability[("AG.PRD.CREL.MT", 2024)].footnote.startswith("Series reviewed")
    assert availability[("FM.LBL.BMNY.ZG", 2024)].available is False


def test_parser_never_backdates_a_dataset_update_seen_early():
    early_collection = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
    parsed = _parse(collected_at=early_collection)
    assert {row.released_at for row in parsed.observations} == {early_collection}


def test_parser_binds_exact_url_year_scope_and_complete_indicator_representation():
    registry = _small_registry()
    expected_url = build_url(registry, start_year=2023, end_year=2024)
    with pytest.raises(WDIError, match="exactly match"):
        parse_response(
            _response(),
            registry=registry,
            evidence_url=expected_url.replace("footnote=y", "footnote=n"),
            start_year=2023,
            end_year=2024,
            collected_at=COLLECTED_AT,
        )

    outside = _default_rows()
    outside[0]["date"] = "2025"
    with pytest.raises(WDIError, match="outside the requested year range"):
        _parse(_response(rows=outside))

    with pytest.raises(WDIError, match="omits configured indicators"):
        _parse(_response(rows=_default_rows()[:-1]))

    all_null = [_row(indicator, 2024, None) for indicator in SMALL_INDICATORS]
    parsed = _parse(_response(rows=all_null), start_year=2024)
    assert parsed.observations == ()
    assert parsed.null_rows == len(SMALL_INDICATORS)
    assert parsed.represented_indicators == tuple(sorted(SMALL_INDICATORS))


def test_parser_rejects_future_dataset_lastupdated():
    with pytest.raises(WDIError, match="lastupdated is in the future"):
        _parse(_response(lastupdated="2026-08-25"))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda document: document[0].update(pages=2), "incomplete"),
        (lambda document: document[0].update(total=99), "total"),
        (lambda document: document[1][0].update(countryiso3code="USA"), "not China"),
        (lambda document: document[1][0]["indicator"].update(id="UNKNOWN.X"), "unrequested"),
        (lambda document: document[1].append(deepcopy(document[1][0])), "duplicate"),
        (lambda document: document[1][0].update(date="2024Q1"), "annual period"),
        (lambda document: document[1][0].update(value="652290000"), "numeric or null"),
    ],
)
def test_parser_refuses_partial_foreign_duplicate_or_coercive_data(mutation, message):
    document = json.loads(_response())
    mutation(document)
    if message == "duplicate":
        document[0]["total"] += 1
    raw = json.dumps(document, separators=(",", ":")).encode()

    with pytest.raises(WDIError, match=message):
        _parse(raw)


def test_row_fingerprint_ignores_batch_encoding_order_and_prevents_vintage_spam(
    tmp_path: Path,
):
    registry = _small_registry()
    raw_one = _response()
    document = json.loads(raw_one)
    document[1].reverse()
    document[1][1].pop("scale")
    raw_two = json.dumps(document, ensure_ascii=False, indent=2).encode()
    first = _parse(raw_one, registry=registry)
    second = _parse(
        raw_two,
        registry=registry,
        collected_at=COLLECTED_AT + timedelta(minutes=5),
    )

    assert first.raw_sha256 != second.raw_sha256
    first_hashes = {row.series_id: row.raw_sha256 for row in first.observations}
    second_hashes = {row.series_id: row.raw_sha256 for row in second.observations}
    assert first_hashes == second_hashes
    ledger = tmp_path / "observations.jsonl"
    assert len(append_vintages(ledger, first.observations)) == 2
    assert append_vintages(ledger, second.observations) == []
    assert load_snapshot(ledger).records == 2


def test_pull_receipt_splits_response_and_ledger_coverage_and_is_idempotent(
    tmp_path: Path,
    capsys,
):
    registry = _small_registry_path(tmp_path)
    response = tmp_path / "wdi.json"
    ledger = tmp_path / "review" / "observations.jsonl"
    latest = tmp_path / "review" / "latest.json"
    response.write_bytes(_response())
    args = [
        "--registry",
        str(registry),
        "--input",
        str(response),
        "--ledger",
        str(ledger),
        "--latest",
        str(latest),
        "--start-year",
        "2023",
        "--end-year",
        "2024",
    ]

    assert pull_main(args) == 0
    first_snapshot = load_snapshot(ledger)
    first_receipt = json.loads(latest.read_text(encoding="utf-8"))
    assert first_snapshot.records == 2
    assert first_receipt["schema_version"] == "palimpsest-china-econ-wdi-run.v3"
    assert first_receipt["context_only"] is True
    assert first_receipt["scoring_allowed"] is False
    assert first_receipt["publication_state"] == "review_only"
    assert first_receipt["revision_lineage"] == {
        "mode": "local_review_append_only",
        "durable_cross_run": False,
        "ledger_path": ledger.name,
    }
    assert first_receipt["batch_raw_sha256"] == hashlib.sha256(_response()).hexdigest()
    assert first_receipt["ledger_before"] == {
        "sha256": hashlib.sha256(b"").hexdigest(),
        "bytes": 0,
        "records": 0,
    }
    assert first_receipt["ledger_after"] == {
        "sha256": first_snapshot.byte_sha256,
        "bytes": first_snapshot.byte_size,
        "records": 2,
    }
    assert first_receipt["response_coverage"]["configured_indicators"] == 3
    assert first_receipt["response_coverage"]["represented_indicators"] == 3
    assert first_receipt["response_coverage"]["populated_indicators"] == 2
    assert first_receipt["response_coverage"]["null_only_indicators"] == 1
    assert first_receipt["ledger_coverage"]["records"] == 2
    assert first_receipt["availability"]["records"] == 3
    assert first_receipt["indicator_provenance"]["records"] == 3
    assert first_receipt["indicator_provenance"]["upstream_attribution_state"] == (
        "residual_gate"
    )

    assert pull_main(args) == 0
    second_snapshot = load_snapshot(ledger)
    second_receipt = json.loads(latest.read_text(encoding="utf-8"))
    assert second_snapshot.records == 2
    assert second_receipt["appended_observations"] == 0
    assert second_receipt["ledger_before"] == second_receipt["ledger_after"]
    assert second_receipt["ledger_after"]["sha256"] == first_snapshot.byte_sha256
    assert DEFAULT_LEDGER.parts[-3:] == (
        "data",
        "review",
        "china-econ-wdi-observations.jsonl",
    )
    assert "appended=0" in capsys.readouterr().out


def test_pull_records_null_withdrawal_without_claiming_old_value_is_current(
    tmp_path: Path,
):
    registry = _small_registry_path(tmp_path, "AG.PRD.CREL.MT")
    response = tmp_path / "wdi.json"
    ledger = tmp_path / "review" / "observations.jsonl"
    latest = tmp_path / "review" / "latest.json"
    args = [
        "--registry",
        str(registry),
        "--input",
        str(response),
        "--ledger",
        str(ledger),
        "--latest",
        str(latest),
        "--start-year",
        "2024",
        "--end-year",
        "2024",
    ]
    response.write_bytes(
        _response(rows=[_row("AG.PRD.CREL.MT", 2024, 1.0)], sourceid="2")
    )
    assert pull_main(args) == 0
    response.write_bytes(
        _response(
            rows=[
                _row(
                    "AG.PRD.CREL.MT",
                    2024,
                    None,
                    footnote="Value unavailable in this response.",
                )
            ],
            sourceid="2",
        )
    )
    assert pull_main(args) == 0

    receipt = json.loads(latest.read_text(encoding="utf-8"))
    assert load_snapshot(ledger).records == 1
    assert receipt["response_coverage"]["populated_observations"] == 0
    assert receipt["response_coverage"]["null_rows"] == 1
    assert receipt["ledger_coverage"]["records"] == 1
    assert receipt["availability"]["entries"] == [
        {
            "indicator_id": "AG.PRD.CREL.MT",
            "year": 2024,
            "available": False,
            "footnote": "Value unavailable in this response.",
        }
    ]
    assert receipt["availability"]["withdrawal_state"] == (
        "residual_gate_no_append_only_withdrawal_ledger"
    )


def test_explicit_public_context_mode_requires_policy_and_exact_readings_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    registry = _small_registry_path(tmp_path)
    response = tmp_path / "wdi.json"
    response.write_bytes(_response())
    ledger = tmp_path / "readings" / "china-econ-wdi-observations.jsonl"
    latest = tmp_path / "readings" / "china-econ-wdi-latest.json"
    monkeypatch.setattr(wdi_pull, "DEFAULT_REGISTRY", registry)
    monkeypatch.setattr(wdi_pull, "PUBLIC_LEDGER", ledger)
    monkeypatch.setattr(wdi_pull, "PUBLIC_LATEST", latest)

    def fake_fetch(url):
        assert "date=2023%3A2024" in url
        return response.read_bytes()

    monkeypatch.setattr(wdi_pull, "fetch_bytes", fake_fetch)
    args = [
        "--registry",
        str(registry),
        "--ledger",
        str(ledger),
        "--latest",
        str(latest),
        "--start-year",
        "2023",
        "--end-year",
        "2024",
        "--public-context-only",
    ]

    assert wdi_pull.main(args) == 0
    receipt = json.loads(latest.read_text(encoding="utf-8"))
    assert receipt["publication_state"] == "public_context_only"
    assert receipt["context_only"] is True
    assert receipt["scoring_allowed"] is False
    assert receipt["revision_lineage"] == {
        "mode": "git_tracked_append_only",
        "durable_cross_run": True,
        "ledger_path": "readings/china-econ-wdi-observations.jsonl",
    }

    wrong = tmp_path / "wrong.jsonl"
    assert wdi_pull.main([*args[:-1], "--ledger", str(wrong), args[-1]]) == 2
    assert not wrong.exists()

    alternate_registry = tmp_path / "alternate-registry.json"
    alternate_registry.write_bytes(registry.read_bytes())
    alternate_policy = tmp_path / "alternate-policy.json"
    alternate_policy.write_bytes(wdi_pull.DEFAULT_POLICY.read_bytes())
    original_ledger = ledger.read_bytes()
    original_latest = latest.read_bytes()
    assert (
        wdi_pull.main(
            [*args[:-1], "--input", str(response), args[-1]]
        )
        == 2
    )
    assert ledger.read_bytes() == original_ledger
    assert latest.read_bytes() == original_latest
    for override in (
        ["--registry", str(alternate_registry)],
        ["--policy", str(alternate_policy)],
    ):
        assert wdi_pull.main([*args[:-1], *override, args[-1]]) == 2
        assert ledger.read_bytes() == original_ledger
        assert latest.read_bytes() == original_latest


def test_pull_samples_collection_clock_only_after_exact_fetch_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    registry = _small_registry_path(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    latest = tmp_path / "latest.json"
    state = {"fetched": False, "calls": 0}

    class AfterFetchClock:
        @classmethod
        def now(cls, tz=None):
            state["calls"] += 1
            if state["calls"] == 1:
                return datetime(2026, 8, 24, 9, 59, tzinfo=UTC)
            assert state["fetched"] is True
            return datetime(2026, 8, 24, 10, 5, tzinfo=UTC)

    def delayed_fetch(_url):
        state["fetched"] = True
        return _response()

    monkeypatch.setattr(wdi_pull, "datetime", AfterFetchClock)
    monkeypatch.setattr(wdi_pull, "fetch_bytes", delayed_fetch)
    assert wdi_pull.main(
        [
            "--registry",
            str(registry),
            "--ledger",
            str(ledger),
            "--latest",
            str(latest),
            "--start-year",
            "2023",
            "--end-year",
            "2024",
        ]
    ) == 0

    receipt = json.loads(latest.read_bytes())
    snapshot = load_snapshot(ledger)
    assert receipt["generated_at"] == "2026-08-24T10:05:00Z"
    assert {row.collected_at for row in snapshot.observations} == {
        datetime(2026, 8, 24, 10, 5, tzinfo=UTC)
    }


def test_public_pull_rechecks_rights_after_fetch_and_refuses_expiry_crossing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    registry = _small_registry_path(tmp_path)
    policy = tmp_path / "policy.json"
    policy_document = json.loads(wdi_pull.DEFAULT_POLICY.read_bytes())
    for row in policy_document["sources"]:
        if row["source_id"] == "world_bank_wdi":
            row["expires_at"] = "2026-08-24T10:00:01Z"
    policy.write_text(
        json.dumps(
            policy_document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    ledger = tmp_path / "readings" / "china-econ-wdi-observations.jsonl"
    latest = tmp_path / "readings" / "china-econ-wdi-latest.json"
    moments = iter(
        [
            datetime(2026, 8, 24, 9, 59, 59, tzinfo=UTC),
            datetime(2026, 8, 24, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 24, 10, 0, 2, tzinfo=UTC),
        ]
    )

    class ExpiryClock:
        @classmethod
        def now(cls, tz=None):
            return next(moments)

    monkeypatch.setattr(wdi_pull, "datetime", ExpiryClock)
    monkeypatch.setattr(wdi_pull, "DEFAULT_REGISTRY", registry)
    monkeypatch.setattr(wdi_pull, "DEFAULT_POLICY", policy)
    monkeypatch.setattr(wdi_pull, "PUBLIC_LEDGER", ledger)
    monkeypatch.setattr(wdi_pull, "PUBLIC_LATEST", latest)
    monkeypatch.setattr(wdi_pull, "fetch_bytes", lambda _url: _response())

    assert wdi_pull.main(
        [
            "--registry",
            str(registry),
            "--policy",
            str(policy),
            "--ledger",
            str(ledger),
            "--latest",
            str(latest),
            "--start-year",
            "2023",
            "--end-year",
            "2024",
            "--public-context-only",
        ]
    ) == 2
    assert not ledger.exists()
    assert not latest.exists()


def test_public_context_mode_blocks_previously_numeric_null_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    registry = _small_registry_path(tmp_path)
    ledger = tmp_path / "readings" / "china-econ-wdi-observations.jsonl"
    latest = tmp_path / "readings" / "china-econ-wdi-latest.json"
    monkeypatch.setattr(wdi_pull, "DEFAULT_REGISTRY", registry)
    monkeypatch.setattr(wdi_pull, "PUBLIC_LEDGER", ledger)
    monkeypatch.setattr(wdi_pull, "PUBLIC_LATEST", latest)
    current_raw = {"payload": _response()}

    monkeypatch.setattr(wdi_pull, "fetch_bytes", lambda _url: current_raw["payload"])
    args = [
        "--registry",
        str(registry),
        "--ledger",
        str(ledger),
        "--latest",
        str(latest),
        "--start-year",
        "2023",
        "--end-year",
        "2024",
        "--public-context-only",
    ]
    assert wdi_pull.main(args) == 0
    original_ledger = ledger.read_bytes()
    original_latest = latest.read_bytes()

    current_raw["payload"] = _response(
        rows=[
            _row("AG.PRD.CREL.MT", 2024, None),
            _row("IS.SHP.GOOD.TU", 2023, 310_000_000),
            _row("FM.LBL.BMNY.ZG", 2024, None),
        ]
    )
    assert wdi_pull.main(args) == 2
    assert ledger.read_bytes() == original_ledger
    assert latest.read_bytes() == original_latest


def test_public_context_mode_authenticates_prior_receipt_with_prior_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    previous_registry = _small_registry_path(tmp_path, "AG.PRD.CREL.MT")
    current_registry = tmp_path / "registry-2-current.json"
    current_registry.write_bytes(
        _small_registry_path(
            tmp_path,
            "AG.PRD.CREL.MT",
            "IS.SHP.GOOD.TU",
        ).read_bytes()
    )
    ledger = tmp_path / "readings" / "china-econ-wdi-observations.jsonl"
    latest = tmp_path / "readings" / "china-econ-wdi-latest.json"
    current_raw = {
        "payload": _response(
            rows=[_row("AG.PRD.CREL.MT", 2024, 652_290_000)],
            sourceid="2",
        )
    }
    monkeypatch.setattr(wdi_pull, "DEFAULT_REGISTRY", previous_registry)
    monkeypatch.setattr(wdi_pull, "PUBLIC_LEDGER", ledger)
    monkeypatch.setattr(wdi_pull, "PUBLIC_LATEST", latest)
    monkeypatch.setattr(wdi_pull, "fetch_bytes", lambda _url: current_raw["payload"])
    common = [
        "--ledger",
        str(ledger),
        "--latest",
        str(latest),
        "--start-year",
        "2024",
        "--end-year",
        "2024",
        "--public-context-only",
    ]
    assert wdi_pull.main(["--registry", str(previous_registry), *common]) == 0
    original_ledger = ledger.read_bytes()

    monkeypatch.setattr(wdi_pull, "DEFAULT_REGISTRY", current_registry)
    current_raw["payload"] = _response(
        rows=[
            _row("AG.PRD.CREL.MT", 2024, 652_290_000),
            _row("IS.SHP.GOOD.TU", 2024, None),
        ]
    )
    assert wdi_pull.main(
        [
            "--registry",
            str(current_registry),
            "--prior-registry",
            str(previous_registry),
            *common,
        ]
    ) == 0
    receipt = json.loads(latest.read_bytes())
    assert ledger.read_bytes().startswith(original_ledger)
    assert receipt["response_coverage"]["configured_indicators"] == 2
    assert receipt["response_coverage"]["represented_indicators"] == 2
    assert receipt["appended_observations"] == 1
    assert receipt["ledger_before"]["bytes"] == len(original_ledger)


def test_published_wdi_ledger_and_receipt_are_exact_attributed_context_only():
    snapshot = load_snapshot(wdi_pull.PUBLIC_LEDGER)
    receipt = json.loads(wdi_pull.PUBLIC_LATEST.read_text(encoding="utf-8"))

    assert snapshot.records > 2_000
    assert receipt["schema_version"] == "palimpsest-china-econ-wdi-run.v3"
    assert receipt["ledger_after"] == {
        "sha256": snapshot.byte_sha256,
        "bytes": snapshot.byte_size,
        "records": snapshot.records,
    }
    assert receipt["source_id"] == "world_bank_wdi"
    assert receipt["license"] == "CC-BY-4.0"
    assert receipt["redistribution_status"] == "allowed"
    assert receipt["publication_state"] == "public_context_only"
    assert receipt["context_only"] is True
    assert receipt["scoring_allowed"] is False
    assert receipt["revision_lineage"] == {
        "mode": "git_tracked_append_only",
        "durable_cross_run": True,
        "ledger_path": "readings/china-econ-wdi-observations.jsonl",
    }
    assert receipt["response_coverage"]["represented_indicators"] == 54
    assert receipt["response_coverage"]["populated_indicators"] >= 40
    assert receipt["availability"]["withdrawal_state"] == (
        "residual_gate_no_append_only_withdrawal_ledger"
    )
    assert {row.source_id for row in snapshot.observations} == {"world_bank_wdi"}
    assert all(row.geography == "CN" for row in snapshot.observations)


@pytest.mark.parametrize("symlinked", [False, True])
def test_pull_refuses_ledger_latest_collision_without_mutation(
    tmp_path: Path,
    symlinked: bool,
):
    registry = _small_registry_path(tmp_path)
    response = tmp_path / "wdi.json"
    response.write_bytes(_response())
    target = tmp_path / "collision.json"
    sentinel = b"do-not-touch\n"
    target.write_bytes(sentinel)
    latest = target
    if symlinked:
        latest = tmp_path / "latest-link.json"
        latest.symlink_to(target)
    result = pull_main(
        [
            "--registry",
            str(registry),
            "--input",
            str(response),
            "--ledger",
            str(target),
            "--latest",
            str(latest),
            "--start-year",
            "2023",
            "--end-year",
            "2024",
        ]
    )
    assert result == 2
    assert target.read_bytes() == sentinel


def test_pull_refuses_output_input_collision_without_mutation(tmp_path: Path):
    registry = _small_registry_path(tmp_path)
    response = tmp_path / "wdi.json"
    original = _response()
    response.write_bytes(original)
    result = pull_main(
        [
            "--registry",
            str(registry),
            "--input",
            str(response),
            "--ledger",
            str(response),
            "--latest",
            str(tmp_path / "latest.json"),
        ]
    )
    assert result == 2
    assert response.read_bytes() == original
    assert not (tmp_path / "latest.json").exists()


def test_strict_json_refuses_duplicate_keys_and_non_finite_numbers():
    registry = _small_registry()
    evidence_url = build_url(registry, start_year=2024, end_year=2024)
    duplicate = (
        b'[{"page":1,"page":1,"pages":1,"per_page":20000,"total":0,'
        b'"sourceid":"2","lastupdated":"2026-07-13"},[]]'
    )
    non_finite = _response().replace(b"652290000", b"NaN", 1)

    with pytest.raises(WDIError, match="duplicate key"):
        parse_response(
            duplicate,
            registry=registry,
            evidence_url=evidence_url,
            start_year=2024,
            end_year=2024,
            collected_at=COLLECTED_AT,
        )
    with pytest.raises(WDIError, match="non-finite"):
        _parse(non_finite)
