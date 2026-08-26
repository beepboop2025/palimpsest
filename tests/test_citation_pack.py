"""Citation helpers name the dataset, the file, and the accessed date."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

import scripts.build_citation_pack as build_citations
from collectors.bri_world_bank_wdi import (
    WDIRegistry,
    build_url,
    load_registry,
    parse_response,
)
from core.citation_pack import (
    CitationError,
    cite_bri_wdi_observation,
    cite_dataset,
    cite_signal_day,
)


CATALOG = {
    "datasets": [
        {
            "id": "ddti",
            "name": "Domestic Discourse Tightening Index",
            "latest": "readings/ddti-latest.json",
            "history": "readings/ddti-history.jsonl",
            "landing_page": "dashboards/ddti_observatory.html",
            "method": "docs/METHODOLOGY.md",
        }
    ]
}
ROOT = Path(__file__).resolve().parents[1]
WDI_REGISTRY = ROOT / "config" / "bri_wdi_series.json"
WDI_FIXTURE = ROOT / "tests" / "fixtures" / "bri_world_bank_wdi_valid.json"
WDI_RETRIEVED_AT = datetime(2026, 8, 26, 10, 30, tzinfo=UTC)


def _wdi_bundle() -> dict:
    registry = load_registry(WDI_REGISTRY)
    indicator_ids = ("IS.SHP.GOOD.TU", "NY.GDP.MKTP.KD.ZG")
    scoped = WDIRegistry(
        dataset=dict(registry.dataset),
        countries=dict(registry.countries),
        bindings={indicator_id: registry.bindings[indicator_id] for indicator_id in indicator_ids},
        raw_sha256=registry.raw_sha256,
    )
    return parse_response(
        WDI_FIXTURE.read_bytes(),
        registry=scoped,
        evidence_url=build_url(scoped, start_year=2023, end_year=2024),
        start_year=2023,
        end_year=2024,
        retrieved_at=WDI_RETRIEVED_AT,
    ).to_dict()


def _wdi_row(bundle: dict, *, state: str, country: str) -> dict:
    return next(
        row
        for row in bundle["observations"]
        if row["evidence_state"] == state and row["country_code"] == country
    )


def test_dataset_citation_contains_url_and_date() -> None:
    pack = cite_dataset(CATALOG, "ddti", accessed="2026-08-22")
    assert "2026-08-22" in pack["apa"]
    assert "palimpsest.info/readings/ddti-latest.json" in pack["url"]
    assert "@misc{palimpsest-ddti" in pack["bibtex"]
    assert "—" not in pack["bibtex"]


def test_unknown_dataset_fails() -> None:
    with pytest.raises(CitationError):
        cite_dataset(CATALOG, "not-a-signal")


def test_signal_day_abstains_when_history_lacks_the_day(tmp_path: Path) -> None:
    history = tmp_path / "ddti-history.jsonl"
    history.write_text(
        json.dumps({"generated_at": "2026-08-20T01:00:00Z", "n_terms": 1}) + "\n",
        encoding="utf-8",
    )
    pack = cite_signal_day(
        CATALOG, "ddti", "2026-08-22", history_path=history, accessed="2026-08-22"
    )
    assert pack["abstention"]["code"] == "day-not-in-history"


def test_signal_day_uses_the_matching_history_row(tmp_path: Path) -> None:
    history = tmp_path / "ddti-history.jsonl"
    history.write_text(
        json.dumps({"generated_at": "2026-08-22T09:42:00Z", "n_terms": 211}) + "\n",
        encoding="utf-8",
    )
    pack = cite_signal_day(
        CATALOG, "ddti", "2026-08-22", history_path=history, accessed="2026-08-22"
    )
    assert pack["abstention"] is None
    assert "2026-08-22T09:42:00Z" in pack["apa"]
    assert pack["history_row"]["n_terms"] == 211


def test_bri_wdi_observed_citation_binds_claim_clocks_rights_and_hashes() -> None:
    bundle = _wdi_bundle()
    row = _wdi_row(bundle, state="observed", country="PAK")
    pack = cite_bri_wdi_observation(
        bundle,
        row["observation_id"],
        accessed="2026-08-26",
    )

    assert pack["dataset"]["name"] == "World Development Indicators"
    assert pack["dataset"]["publisher"] == "World Bank"
    assert pack["source"] == {
        "source_id": "world_bank_wdi",
        "publisher": "World Bank",
        "evidence_url": row["evidence_url"],
    }
    assert pack["country"] == {"code": "PAK", "name": "Pakistan"}
    assert pack["indicator"] == {
        "indicator_id": row["indicator_id"],
        "series_id": row["series_id"],
    }
    assert pack["period"]["label"] in {"2023", "2024"}
    assert pack["unit"] == row["unit"]
    assert pack["evidence_state"] == "observed"
    assert pack["numeric_claim"] is True
    assert pack["value"] == row["value"]
    assert f"{row['value']} {row['unit']}" in pack["claim"]
    assert pack["clocks"] == {
        "dataset_generated_at": bundle["generated_at"],
        "source_dataset_last_updated": row["source_dataset_last_updated"],
        "source_release_upper_bound": row["source_release_upper_bound"],
        "retrieved_at": row["retrieved_at"],
    }
    assert pack["hashes"] == {
        "collection_id": bundle["collection_id"],
        "observation_id": row["observation_id"],
        "observations_sha256": bundle["observations_sha256"],
        "registry_sha256": bundle["registry_sha256"],
        "source_row_sha256": row["source_row_sha256"],
        "raw_response_sha256": row["raw_response_sha256"],
    }
    assert pack["rights"]["license"] == "CC-BY-4.0"
    assert pack["rights"]["attribution"] == (
        "World Bank, World Development Indicators"
    )
    assert pack["boundary"]["context_scope"] == "national_economic_context"
    assert pack["boundary"]["causality_boundary"] == (
        "not_evidence_of_bri_causality"
    )
    for required in (
        bundle["collection_id"],
        row["observation_id"],
        row["source_row_sha256"],
        row["raw_response_sha256"],
        "CC-BY-4.0",
        "not evidence of a BRI project",
    ):
        assert required in pack["bibtex"]


def test_bri_wdi_forecast_is_visible_and_never_emitted_as_observed() -> None:
    bundle = _wdi_bundle()
    row = _wdi_row(bundle, state="forecast", country="CHN")
    pack = cite_bri_wdi_observation(bundle, row["observation_id"])

    assert pack["evidence_state"] == "forecast"
    assert pack["numeric_claim"] is True
    assert pack["value"] == 5.0
    for text in (pack["claim"], pack["apa"], pack["bibtex"]):
        assert "forecast" in text.lower()
        assert "observed value" not in text.lower()


def test_bri_wdi_unavailable_row_emits_no_numeric_claim() -> None:
    bundle = _wdi_bundle()
    row = _wdi_row(bundle, state="unavailable", country="MMR")
    pack = cite_bri_wdi_observation(bundle, row["observation_id"])

    assert pack["evidence_state"] == "unavailable"
    assert pack["numeric_claim"] is False
    assert pack["value"] is None
    assert "no numeric claim" in pack["claim"]
    assert "no numeric claim" in pack["apa"]
    assert "no numeric claim" in pack["bibtex"]


def test_bri_wdi_citation_fails_closed_on_unknown_or_tampered_rows() -> None:
    bundle = _wdi_bundle()
    row = _wdi_row(bundle, state="observed", country="PAK")
    with pytest.raises(CitationError, match="unknown BRI WDI observation"):
        cite_bri_wdi_observation(bundle, "0" * 64)

    tampered = deepcopy(bundle)
    matching = next(
        item
        for item in tampered["observations"]
        if item["observation_id"] == row["observation_id"]
    )
    matching["value"] = 999.0
    with pytest.raises(CitationError, match="observations_sha256"):
        cite_bri_wdi_observation(tampered, row["observation_id"])


def test_cli_cites_one_bri_wdi_observation_by_authenticated_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _wdi_bundle()
    row = _wdi_row(bundle, state="forecast", country="CHN")
    bundle_path = tmp_path / "bri-economic-observations-latest.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    monkeypatch.setattr(build_citations, "BRI_WDI_BUNDLE", bundle_path)

    assert (
        build_citations.main(
            [
                "--observation-id",
                row["observation_id"],
                "--accessed",
                "2026-08-26",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert "World Bank source-marked forecast" in output.out
    assert bundle["collection_id"] in output.out
    assert row["source_row_sha256"] in output.out
    assert "2026-08-26" in output.out
    assert output.err == ""
