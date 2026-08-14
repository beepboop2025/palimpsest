"""Offline contracts for the first review-gated primary economic adapters."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from collectors import mot_transport, nbs_housing, nea_electricity, spb_parcels
from core.econ_ledger import load_observations
from core.evidence_documents import EvidenceDocumentStore
from core.primary_documents import (
    canonical_json_bytes,
    collect_primary_documents,
    load_primary_source_registry,
)
from processors.china_econ_primary import (
    DEFAULT_ALIAS_PATH,
    DEFAULT_SERIES_PATH,
    PrimaryEconomicAdapterError,
    PrimaryEconomicRegistryError,
    load_series_registry,
    load_source_aliases,
    observations_from_captured_document,
)
from scripts.china_econ_primary_ingest import DEFAULT_LEDGER_PATH, main as ingest_main


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "china_econ_primary"
PRIMARY_CONFIG = ROOT / "config" / "primary_document_sources.json"
UTC = timezone.utc


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _captured(
    tmp_path: Path,
    *,
    primary_source_id: str,
    raw: bytes,
    evidence_url: str,
    publication_time: str | None,
):
    collected_at = "2026-08-12T10:00:00Z"
    store = EvidenceDocumentStore(
        tmp_path / f"store-{primary_source_id}",
        acceptance_clock=lambda request: request["metadata"]["collected_at"],
    )
    stored = store.ingest(
        raw,
        {
            "source": {"id": primary_source_id, "canonical_url": evidence_url},
            "media_type": "text/html",
            "language": "zh",
            "event_time": publication_time,
            "publication_time": publication_time,
            "knowledge_time": publication_time or collected_at,
            "collected_at": collected_at,
            "collection": {"run_id": "primary-adapter-test", "parent_feed_sha256": None},
            "retention_class": "primary-source-permanent",
            "rights": {
                "training_use": "metadata_only",
                "license_or_terms_ref": "https://example.test/terms",
            },
        },
    )
    vintage = {
        "vintage_id": "documentv-1234567890abcdef12345678",
        "publication_time": publication_time,
        "first_retrieved_at": collected_at,
        "accepted_at": stored.accepted_at,
        "content_sha256": stored.content_sha256,
        "manifest_sha256": stored.manifest_sha256,
        "byte_size": stored.byte_size,
    }
    document = {
        "source_id": primary_source_id,
        "capture_scope": "release_document",
        "media_type": "text/html",
        "original_url": evidence_url,
    }
    return document, vintage, stored.manifest


def test_reviewed_aliases_are_explicit_and_series_registry_covers_the_tranche():
    aliases = load_source_aliases()
    series = load_series_registry(aliases=aliases)

    assert {
        primary: alias.economic_source_id
        for primary, alias in aliases.aliases.items()
    } == {
        "mot-transport": "mot_transport",
        "spb-parcels": "spb_parcels",
        "nea-electricity": "nea_electricity",
        "nbs-70-city-housing": "nbs_70_city_housing",
    }
    assert len(series.series) == 20
    assert len(series.geography_groups["nbs_70_cities"]) == 70
    assert all(alias.parser_version.endswith(".v1") for alias in aliases.aliases.values())


def test_alias_registry_rejects_naive_hyphen_replacement(tmp_path: Path):
    document = json.loads(DEFAULT_ALIAS_PATH.read_text(encoding="utf-8"))
    row = next(
        item for item in document["aliases"] if item["primary_source_id"] == "nbs-70-city-housing"
    )
    row["economic_source_id"] = "nbs_70_city_housing_replaced"
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PrimaryEconomicRegistryError, match="broadens"):
        load_source_aliases(path)


def test_reviewed_tranche_remains_quarantined_and_unscheduled():
    source_registry = json.loads(
        (ROOT / "config" / "china_econ_sources.json").read_text(encoding="utf-8")
    )
    implementations = {
        row["source_id"]: row["implementation"]
        for row in source_registry["sources"]
        if row["source_id"]
        in {"mot_transport", "spb_parcels", "nea_electricity", "nbs_70_city_housing"}
    }
    assert set(implementations.values()) == {"adapter_ready"}
    assert DEFAULT_LEDGER_PATH == ROOT / "data" / "review" / "china-econ-primary-observations.jsonl"
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        assert "china_econ_primary_ingest" not in workflow.read_text(encoding="utf-8")


def test_cli_refuses_a_missing_private_store_without_a_traceback(tmp_path, capsys):
    result = ingest_main(["--store", str(tmp_path / "missing"), "--check"])

    assert result == 2
    assert "EvidenceDocument store does not exist" in capsys.readouterr().err


def test_pure_parsers_preserve_month_ytd_city_and_sector_semantics():
    mot = mot_transport.parse(_fixture("mot_valid.html"))
    spb = spb_parcels.parse(_fixture("spb_valid.html"))
    nea = nea_electricity.parse(_fixture("nea_valid.html"))
    nbs = nbs_housing.parse(_fixture("nbs_valid.html"))

    assert len(mot) == 4
    assert {(row["aggregation_window"], row["period_start"].month) for row in mot} == {
        ("month", 5),
        ("year_to_date", 1),
    }
    assert len(spb) == 8
    assert {row["source_unit"] for row in spb} == {"亿元", "亿件", "%"}
    assert len(nea) == 20
    assert {row["sector_key"] for row in nea} == {
        "all_electricity",
        "primary_industry",
        "secondary_industry",
        "tertiary_industry",
        "households",
    }
    household_month_yoy = next(
        row
        for row in nea
        if row["sector_key"] == "households"
        and row["series_key"] == "electricity_consumption_yoy_month"
    )
    assert household_month_yoy["value"] == -1.0
    assert len(nbs) == 8
    assert {row["geography_key"] for row in nbs} == {"北京", "上海"}
    assert {row["sector_key"] for row in nbs} == {"new_home", "resale_home"}
    assert {row["source_unit"] for row in nbs} == {"上月=100", "上年同月=100"}


@pytest.mark.parametrize(
    ("parser", "fixture", "message"),
    [
        (mot_transport.parse, "mot_shape_drift.html", "reviewed inline"),
        (nbs_housing.parse, "nbs_shape_drift.html", "headers changed"),
        (spb_parcels.parse, "spb_unit_drift.html", "units changed"),
        (nea_electricity.parse, "nea_range_failure.html", "reviewed range"),
    ],
)
def test_parsers_fail_closed_on_shape_unit_and_range_drift(parser, fixture, message):
    with pytest.raises(ValueError, match=message):
        parser(_fixture(fixture))


@pytest.mark.parametrize(
    "parser",
    [mot_transport.parse, spb_parcels.parse, nea_electricity.parse, nbs_housing.parse],
)
def test_every_parser_rejects_access_interstitials(parser):
    with pytest.raises(ValueError, match="interstitial"):
        parser(_fixture("interstitial.html"))


def test_nbs_responsive_copies_must_agree_exactly():
    text = _fixture("nbs_valid.html").decode("utf-8")
    prefix, marker, suffix = text.rpartition("101.5")
    assert marker
    drifted = (prefix + "101.6" + suffix).encode("utf-8")

    with pytest.raises(nbs_housing.NBSHousingParseError, match="copies disagree"):
        nbs_housing.parse(drifted)


def test_processor_binds_release_collection_content_manifest_and_parser(tmp_path: Path):
    raw = _fixture("nbs_valid.html")
    url = "https://www.stats.gov.cn/sj/zxfbhjd/202602/t20260213_1962617.html"
    document, vintage, manifest = _captured(
        tmp_path,
        primary_source_id="nbs-70-city-housing",
        raw=raw,
        evidence_url=url,
        publication_time="2026-02-13T09:30:00Z",
    )
    aliases = load_source_aliases()
    series = load_series_registry(aliases=aliases)

    observations = observations_from_captured_document(
        raw,
        document=document,
        vintage=vintage,
        manifest=manifest,
        aliases=aliases,
        series_registry=series,
    )

    assert len(observations) == 8
    assert {row.source_id for row in observations} == {"nbs_70_city_housing"}
    assert {row.geography for row in observations} == {
        "CN:city:beijing",
        "CN:city:shanghai",
    }
    assert {row.released_at for row in observations} == {
        datetime(2026, 2, 13, 9, 30, tzinfo=UTC)
    }
    assert {row.collected_at for row in observations} == {
        datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    }
    content_hash = hashlib.sha256(raw).hexdigest()
    assert all(row.raw_sha256 == content_hash for row in observations)
    assert all(row.metadata["source_document_sha256"] == content_hash for row in observations)
    assert all(
        row.metadata["source_manifest_sha256"] == vintage["manifest_sha256"]
        for row in observations
    )
    assert all(row.metadata["parser_version"] == nbs_housing.PARSER_VERSION for row in observations)
    assert all(row.firm_size == row.ownership == "all" for row in observations)


def test_processor_refuses_tampered_bytes_manifest_and_missing_release_time(tmp_path: Path):
    raw = _fixture("mot_valid.html")
    url = "https://xxgk.mot.gov.cn/jigou/zhghs/202606/t20260629_4208435.html"
    document, vintage, manifest = _captured(
        tmp_path,
        primary_source_id="mot-transport",
        raw=raw,
        evidence_url=url,
        publication_time="2026-06-29T00:00:00Z",
    )
    aliases = load_source_aliases()
    series = load_series_registry(aliases=aliases)

    with pytest.raises(PrimaryEconomicAdapterError, match="content SHA-256"):
        observations_from_captured_document(
            raw + b"\n",
            document=document,
            vintage=vintage,
            manifest=manifest,
            aliases=aliases,
            series_registry=series,
        )
    tampered = deepcopy(manifest)
    tampered["language"] = "en"
    with pytest.raises(PrimaryEconomicAdapterError, match="manifest hash"):
        observations_from_captured_document(
            raw,
            document=document,
            vintage=vintage,
            manifest=tampered,
            aliases=aliases,
            series_registry=series,
        )

    no_release_document, no_release_vintage, no_release_manifest = _captured(
        tmp_path,
        primary_source_id="mot-transport",
        raw=raw,
        evidence_url=url,
        publication_time=None,
    )
    with pytest.raises(PrimaryEconomicAdapterError, match="required; no time is inferred"):
        observations_from_captured_document(
            raw,
            document=no_release_document,
            vintage=no_release_vintage,
            manifest=no_release_manifest,
            aliases=aliases,
            series_registry=series,
        )


def test_processor_rejects_unreviewed_city_even_when_the_html_shape_is_valid(tmp_path: Path):
    raw = _fixture("nbs_valid.html").replace("上 海".encode(), "虚构城".encode())
    url = "https://www.stats.gov.cn/sj/zxfbhjd/202602/t20260213_1962617.html"
    document, vintage, manifest = _captured(
        tmp_path,
        primary_source_id="nbs-70-city-housing",
        raw=raw,
        evidence_url=url,
        publication_time="2026-02-13T09:30:00Z",
    )
    aliases = load_source_aliases()
    series = load_series_registry(aliases=aliases)

    with pytest.raises(PrimaryEconomicAdapterError, match="geography"):
        observations_from_captured_document(
            raw,
            document=document,
            vintage=vintage,
            manifest=manifest,
            aliases=aliases,
            series_registry=series,
        )


def _full_capture(tmp_path: Path):
    registry = load_primary_source_registry(PRIMARY_CONFIG)
    special = {
        "mot-transport": _fixture("mot_valid.html"),
        "spb-parcels": _fixture("spb_valid.html"),
        "nea-electricity": _fixture("nea_valid.html"),
        "nbs-70-city-housing": _fixture("nbs_valid.html"),
    }
    payloads = {}
    for source in registry.sources:
        if source.id in special:
            body = special[source.id]
        elif source.media_type == "application/pdf":
            body = f"%PDF-1.7\nfixture:{source.id}\n%%EOF\n".encode()
        elif source.media_type == "application/json":
            body = json.dumps({"source": source.id, "aggregate": True}).encode()
        else:
            body = (
                "<!doctype html><html><head><title>Primary source</title></head>"
                f"<body><p>{source.id}</p></body></html>"
            ).encode()
        payloads[source.url] = body

    def fetch(url, **_kwargs):
        return payloads[url]

    store_path = (tmp_path / "complete-store").resolve()
    store = EvidenceDocumentStore(
        store_path,
        acceptance_clock=lambda request: request["metadata"]["collected_at"],
    )
    index = collect_primary_documents(
        registry,
        fetch,
        store,
        now=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )
    index_path = tmp_path / "primary-documents.json"
    index_path.write_bytes(canonical_json_bytes(index))
    return index_path, store_path


def test_cli_check_dry_run_and_locked_append_are_non_networked_and_idempotent(
    tmp_path: Path,
    capsys,
):
    index_path, store_path = _full_capture(tmp_path)
    ledger = tmp_path / "observations.jsonl"
    common = [
        "--index",
        str(index_path),
        "--store",
        str(store_path),
        "--ledger",
        str(ledger),
        "--aliases",
        str(DEFAULT_ALIAS_PATH),
        "--series",
        str(DEFAULT_SERIES_PATH),
    ]

    assert ingest_main([*common, "--check"]) == 0
    assert "40 candidates" in capsys.readouterr().out
    assert not ledger.exists()
    assert ingest_main([*common, "--dry-run"]) == 0
    assert "logical_sha256=" in capsys.readouterr().out
    assert not ledger.exists()

    assert ingest_main(common) == 0
    assert "40 observation vintages appended" in capsys.readouterr().out
    rows = load_observations(ledger)
    assert len(rows) == 40
    assert {row.source_id for row in rows} == {
        "mot_transport",
        "spb_parcels",
        "nea_electricity",
        "nbs_70_city_housing",
    }
    before = ledger.read_bytes()
    assert ingest_main(common) == 0
    assert "0 observation vintages appended" in capsys.readouterr().out
    assert ledger.read_bytes() == before


def test_frozen_primary_fixtures_are_aggregate_only():
    forbidden = (b"email", b"phone", b"person_id", b"company_name", b"respondent")
    for path in FIXTURES.glob("*.html"):
        folded = path.read_bytes().lower()
        assert all(marker not in folded for marker in forbidden), path.name
