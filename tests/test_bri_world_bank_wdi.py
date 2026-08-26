"""Offline, fail-closed tests for the multi-country BRI WDI adapter."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import scripts.bri_wdi_pull as bri_pull
from collectors.bri_world_bank_wdi import (
    BRIWDIError,
    MAX_RESPONSE_BYTES,
    WDIRegistry,
    build_url,
    collect,
    fetch_bytes,
    load_registry,
    parse_response,
)
from core.bri_observation import canonical_json_bytes, sha256_bytes
from scripts.bri_wdi_pull import main as pull_main


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "bri_wdi_series.json"
FIXTURE = ROOT / "tests" / "fixtures" / "bri_world_bank_wdi_valid.json"
SCHEMA = ROOT / "protocol" / "bri-economic-observations-v1.schema.json"
RETRIEVED_AT = datetime(2026, 8, 26, 10, 30, tzinfo=UTC)
INDICATORS = ("IS.SHP.GOOD.TU", "NY.GDP.MKTP.KD.ZG")


def _scoped_registry() -> WDIRegistry:
    registry = load_registry(REGISTRY)
    return WDIRegistry(
        dataset=dict(registry.dataset),
        countries=dict(registry.countries),
        bindings={key: registry.bindings[key] for key in INDICATORS},
        raw_sha256=registry.raw_sha256,
    )


def _scoped_registry_path(tmp_path: Path) -> Path:
    document = json.loads(REGISTRY.read_text(encoding="utf-8"))
    document["series"] = [
        row for row in document["series"] if row["indicator_id"] in INDICATORS
    ]
    path = tmp_path / "bri-wdi-series.json"
    path.write_bytes(canonical_json_bytes(document))
    return path


def _parse(raw: bytes | None = None, *, retrieved_at: datetime = RETRIEVED_AT):
    registry = _scoped_registry()
    return parse_response(
        FIXTURE.read_bytes() if raw is None else raw,
        registry=registry,
        evidence_url=build_url(registry, start_year=2023, end_year=2024),
        start_year=2023,
        end_year=2024,
        retrieved_at=retrieved_at,
    )


def _mutated_response(mutation) -> bytes:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    mutation(document)
    return json.dumps(document, separators=(",", ":")).encode("utf-8")


def test_registry_is_reviewed_bounded_and_exactly_three_countries():
    registry = load_registry(REGISTRY)
    assert sorted(registry.countries) == ["CHN", "MMR", "PAK"]
    assert 15 <= len(registry.bindings) <= 24
    assert registry.dataset["license"] == "CC-BY-4.0"
    assert registry.dataset["redistribution_status"] == "allowed_with_attribution"
    assert registry.dataset["context_scope"] == "national_economic_context"
    assert registry.dataset["causality_boundary"] == ("not_evidence_of_bri_causality")
    assert {row.topic for row in registry.bindings.values()} >= {
        "macro",
        "trade",
        "finance",
        "labor",
        "energy",
        "environment",
        "logistics",
    }
    assert registry.raw_sha256 == hashlib.sha256(REGISTRY.read_bytes()).hexdigest()
    with pytest.raises(TypeError):
        registry.dataset["license"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        registry.bindings["NEW"] = next(iter(registry.bindings.values()))  # type: ignore[index]


def test_request_is_keyless_canonical_and_row_bounded():
    registry = load_registry(REGISTRY)
    url = build_url(registry, start_year=1960, end_year=2026)
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https" and parsed.hostname == "api.worldbank.org"
    assert parsed.path.startswith("/v2/country/CHN;MMR;PAK/indicator/")
    assert len(parsed.path.rsplit("/", 1)[1].split(";")) == len(registry.bindings)
    assert query == {
        "source": ["2"],
        "date": ["1960:2026"],
        "format": ["json"],
        "per_page": ["20000"],
        "footnote": ["y"],
    }
    with pytest.raises(BRIWDIError, match="100 annual periods"):
        build_url(registry, start_year=1960, end_year=2060)


def test_fetch_uses_hardened_tls_host_without_redirects():
    registry = _scoped_registry()
    url = build_url(registry, start_year=2023, end_year=2024)
    calls: list[tuple[str, dict]] = []

    def fake_fetcher(request_url: str, **kwargs: object) -> bytes:
        calls.append((request_url, kwargs))
        return b"exact-response"

    assert fetch_bytes(url, retries=0, fetcher=fake_fetcher) == b"exact-response"
    assert calls == [
        (
            url,
            {
                "max_bytes": MAX_RESPONSE_BYTES,
                "timeout": 45.0,
                "max_redirects": 0,
                "headers": {
                    "User-Agent": (
                        "palimpsest.info BRI observatory (World Bank WDI national "
                        "context; contact desk@palimpsest.info)"
                    )
                },
            },
        )
    ]
    with pytest.raises(BRIWDIError, match="reviewed World Bank"):
        fetch_bytes("https://example.test/data", retries=0, fetcher=fake_fetcher)
    with pytest.raises(BRIWDIError, match="three-country"):
        fetch_bytes(
            "https://api.worldbank.org/v2/country/USA/indicator/SP.POP.TOTL?format=json",
            retries=0,
            fetcher=fake_fetcher,
        )


def test_parser_preserves_all_clocks_raw_receipt_and_explicit_nulls():
    raw = FIXTURE.read_bytes()
    parsed = _parse(raw)
    receipt = parsed.request_receipt

    assert len(parsed.observations) == receipt.source_rows == 12
    assert receipt.observed_rows == 8
    assert receipt.unavailable_rows == 4
    assert receipt.raw_response_sha256 == hashlib.sha256(raw).hexdigest()
    assert receipt.source_release_upper_bound.isoformat() == (
        "2026-07-13T23:59:59+00:00"
    )
    assert receipt.retrieved_at == RETRIEVED_AT
    assert {row.country_code for row in parsed.observations} == {
        "CHN",
        "MMR",
        "PAK",
    }
    assert {row.raw_response_sha256 for row in parsed.observations} == {
        receipt.raw_response_sha256
    }
    assert {row.request_id for row in parsed.observations} == {receipt.request_id}
    assert all(
        row.context_scope == "national_economic_context" for row in parsed.observations
    )
    assert all(
        row.causality_boundary == "not_evidence_of_bri_causality"
        for row in parsed.observations
    )

    unavailable = [
        row for row in parsed.observations if row.evidence_state == "unavailable"
    ]
    assert len(unavailable) == 4
    assert all(row.value is None for row in unavailable)
    assert all(row.unavailability_reason == "source_value_null" for row in unavailable)
    assert not any(row.value == 0 for row in unavailable)
    assert len({row.source_row_sha256 for row in parsed.observations}) == 12


def test_lastupdated_is_only_an_upper_bound_when_retrieved_same_day():
    retrieved = datetime(2026, 7, 13, 8, 15, tzinfo=UTC)
    parsed = _parse(retrieved_at=retrieved)
    assert parsed.request_receipt.source_release_upper_bound == retrieved
    assert {row.source_release_upper_bound for row in parsed.observations} == {
        retrieved
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda doc: doc[0].update(pages=2), "incomplete"),
        (lambda doc: doc[0].update(lastupdated="2026-08-27"), "future"),
        (lambda doc: doc[1][0].update(unexpected=True), "fields changed"),
        (
            lambda doc: doc[1][0]["indicator"].update(value="GDP growth"),
            "title changed",
        ),
        (
            lambda doc: doc[1][0]["country"].update(value="PR China"),
            "country descriptor changed",
        ),
        (lambda doc: doc[1][0].update(value="5.0"), "numeric or null"),
        (lambda doc: doc[1].pop(), "matrix is incomplete"),
        (lambda doc: doc[1].__setitem__(-1, deepcopy(doc[1][0])), "duplicate"),
    ],
)
def test_parser_fails_closed_on_schema_scope_or_matrix_drift(mutation, message):
    raw = _mutated_response(mutation)
    document = json.loads(raw)
    document[0]["total"] = len(document[1])
    raw = json.dumps(document, separators=(",", ":")).encode("utf-8")
    with pytest.raises(BRIWDIError, match=message):
        _parse(raw)


def test_parser_binds_exact_request_scope_and_explicit_retrieval_clock():
    registry = _scoped_registry()
    expected_url = build_url(registry, start_year=2023, end_year=2024)
    with pytest.raises(BRIWDIError, match="exactly match"):
        parse_response(
            FIXTURE.read_bytes(),
            registry=registry,
            evidence_url=expected_url.replace("footnote=y", "footnote=n"),
            start_year=2023,
            end_year=2024,
            retrieved_at=RETRIEVED_AT,
        )
    with pytest.raises(BRIWDIError, match="timezone-aware"):
        parse_response(
            FIXTURE.read_bytes(),
            registry=registry,
            evidence_url=expected_url,
            start_year=2023,
            end_year=2024,
            retrieved_at=datetime(2026, 8, 26, 10, 30),
        )


def test_collect_samples_required_clock_only_after_fetch_returns():
    state = {"fetched": False, "clock_calls": 0}

    def fake_fetch(_url: str) -> bytes:
        state["fetched"] = True
        return FIXTURE.read_bytes()

    def clock() -> datetime:
        assert state["fetched"] is True
        state["clock_calls"] += 1
        return RETRIEVED_AT

    parsed = collect(
        _scoped_registry(),
        start_year=2023,
        end_year=2024,
        clock=clock,
        fetch=fake_fetch,
    )
    assert state["clock_calls"] == 1
    assert parsed.request_receipt.retrieved_at == RETRIEVED_AT

    with pytest.raises(BRIWDIError, match="clock must be callable"):
        collect(
            _scoped_registry(),
            start_year=2023,
            end_year=2024,
            clock=RETRIEVED_AT,  # type: ignore[arg-type]
            fetch=fake_fetch,
        )


def test_bundle_is_schema_valid_deterministic_and_self_authenticating():
    first = _parse().to_dict()
    second = _parse().to_dict()
    assert canonical_json_bytes(first) == canonical_json_bytes(second)

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(first)

    payload = dict(first)
    collection_id = payload.pop("collection_id")
    assert collection_id == sha256_bytes(canonical_json_bytes(payload))
    assert first["observations_sha256"] == sha256_bytes(
        canonical_json_bytes(first["observations"])
    )
    assert first["context_policy"]["actor_inference"] == "prohibited"
    assert first["context_policy"]["project_attribution"] == "prohibited"
    assert first["context_policy"]["tactical_data"] == "prohibited"


def test_cli_checks_and_builds_offline_without_implicit_network(tmp_path, capsys):
    registry = _scoped_registry_path(tmp_path)
    output = tmp_path / "review" / "bri-wdi.json"
    shared = [
        "--registry",
        str(registry),
        "--start-year",
        "2023",
        "--end-year",
        "2024",
        "--input",
        str(FIXTURE),
        "--retrieved-at",
        "2026-08-26T10:30:00Z",
    ]

    assert pull_main(["check", *shared]) == 0
    assert "response valid rows=12" in capsys.readouterr().out
    assert pull_main(["build", *shared, "--output", str(output)]) == 0
    first_bytes = output.read_bytes()
    assert pull_main(["build", *shared, "--output", str(output)]) == 0
    assert output.read_bytes() == first_bytes
    assert json.loads(first_bytes)["coverage"] == {
        "start_year": 2023,
        "end_year": 2024,
        "countries": 3,
        "indicators": 2,
        "source_rows": 12,
        "observed_rows": 8,
        "unavailable_rows": 4,
    }


def test_cli_requires_explicit_source_clock_and_noncolliding_output(tmp_path):
    registry = _scoped_registry_path(tmp_path)
    base = [
        "--registry",
        str(registry),
        "--start-year",
        "2023",
        "--end-year",
        "2024",
    ]
    assert pull_main(["check", *base]) == 0
    assert pull_main(["build", *base, "--output", str(tmp_path / "out.json")]) == 2
    assert pull_main(["check", *base, "--input", str(FIXTURE)]) == 2
    assert (
        pull_main(
            [
                "build",
                *base,
                "--input",
                str(FIXTURE),
                "--retrieved-at",
                "2026-08-26T10:30:00Z",
                "--output",
                str(registry),
            ]
        )
        == 2
    )


def test_cli_live_fetch_requires_and_retains_exact_raw_bytes(
    tmp_path, monkeypatch, capsys
):
    registry = _scoped_registry_path(tmp_path)
    raw_output = tmp_path / "controlled" / "wdi-response.json"
    calls: list[str] = []

    def fake_fetch(url: str) -> bytes:
        calls.append(url)
        return FIXTURE.read_bytes()

    monkeypatch.setattr(bri_pull, "fetch_bytes", fake_fetch)
    base = [
        "--registry",
        str(registry),
        "--start-year",
        "2023",
        "--end-year",
        "2024",
        "--fetch",
    ]

    assert pull_main(["check", *base]) == 2
    assert calls == []
    assert pull_main(["check", *base, "--raw-output", str(raw_output)]) == 0
    assert len(calls) == 1
    assert raw_output.read_bytes() == FIXTURE.read_bytes()
    assert f"raw_output={raw_output}" in capsys.readouterr().out

    rejected_raw = _mutated_response(lambda document: document[0].update(extra=True))
    rejected_output = tmp_path / "controlled" / "rejected-response.json"

    def invalid_fetch(url: str) -> bytes:
        calls.append(url)
        return rejected_raw

    monkeypatch.setattr(bri_pull, "fetch_bytes", invalid_fetch)
    assert pull_main(["check", *base, "--raw-output", str(rejected_output)]) == 2
    assert rejected_output.read_bytes() == rejected_raw
    assert len(calls) == 2

    assert (
        pull_main(
            [
                "build",
                *base,
                "--raw-output",
                str(raw_output),
                "--output",
                str(raw_output),
            ]
        )
        == 2
    )
    assert len(calls) == 2
