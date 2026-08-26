"""Offline security and evidence-contract tests for the UCDP bulk adapter."""

from __future__ import annotations

import csv
import io
import json
import stat
import zipfile
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from collectors.ucdp_bulk import (
    ACTOR_HEADER,
    ARMED_CONFLICT_HEADER,
    COUNTRY_YEAR_HEADER,
    UCDPAcquisitionReceipt,
    UCDPBulkError,
    build_bundle,
    extract_member,
    fetch_archive,
    load_registry,
    receipt_for,
    verify_acquisition_receipt,
)
from core.safe_fetch import SafeFetchResponse
from core.ucdp_aggregate import (
    UCDPAggregateError,
    assert_public_safe,
    canonical_json_bytes,
    sha256_bytes,
)
from processors.bri_observatory import load_registry as load_bri_registry
from scripts.ucdp_bulk_pull import main as pull_main

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "ucdp_aggregate.json"
BRI_REGISTRY = ROOT / "config" / "bri_observatory.json"
SCHEMA = ROOT / "protocol" / "ucdp-aggregate-v1.schema.json"
RETRIEVED_AT = datetime(2026, 8, 26, 18, 30, tzinfo=UTC)
LAST_MODIFIED = datetime(2026, 6, 8, 20, 19, 1, tzinfo=UTC)


def _csv_member(
    header: tuple[str, ...],
    rows: list[dict[str, str]],
    *,
    encoding: str,
) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    for supplied in rows:
        row = dict.fromkeys(header, "")
        row.update(supplied)
        writer.writerow(row)
    return output.getvalue().encode(encoding)


def _zip(member_name: str, member: bytes, *, extra_member: bool = False) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        info = zipfile.ZipInfo(member_name, date_time=(2026, 6, 8, 20, 19, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        bundle.writestr(info, member)
        if extra_member:
            bundle.writestr("unexpected.txt", b"not reviewed")
    return output.getvalue()


def _actor_rows() -> list[dict[str, str]]:
    return [
        {
            "ActorId": str(actor_id),
            "NameData": private_name,
            "Org": org,
            "Version": "26.1",
        }
        for actor_id, private_name, org in (
            (142, "PRIVATE Pakistan state label", "1"),
            (287, "PRIVATE distinct Baloch actor label", "3"),
            (144, "PRIVATE Myanmar state label", "1"),
            (890, "PRIVATE distinct Myanmar actor label", "3"),
            (999, "PRIVATE excluded Punjab actor label", "3"),
        )
    ] + [{}]


def _conflict_rows(
    *, unknown_actor: bool = False, myanmar_territory: str = "Karen"
) -> list[dict[str, str]]:
    return [
        {
            "conflict_id": "325",
            "location": "Pakistan",
            "side_a": "private state name",
            "side_a_id": "142",
            "side_b": "private armed name",
            "side_b_id": "12345" if unknown_actor else "287",
            "territory_name": "Balochistan",
            "year": "2025",
            "version": "26.1",
        },
        {
            "conflict_id": "221",
            "location": "Myanmar (Burma)",
            "side_a": "private state name",
            "side_a_id": "144",
            "side_b": "private armed name",
            "side_b_id": "890",
            "territory_name": myanmar_territory,
            "year": "2025",
            "version": "26.1",
        },
        {
            "conflict_id": "999",
            "location": "Pakistan",
            "side_a": "private excluded state name",
            "side_a_id": "142",
            "side_b": "private excluded actor name",
            "side_b_id": "999",
            "territory_name": "Punjab",
            "year": "2025",
            "version": "26.1",
        },
    ]


def _country_year_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for country, country_id in (("Pakistan", "770"), ("Myanmar (Burma)", "775")):
        for year in range(1989, 2026):
            values = {
                "sb_total_deaths_low": "0",
                "sb_total_deaths_best": "0",
                "sb_total_deaths_high": "0",
                "ns_total_deaths_low": "0",
                "ns_total_deaths_best": "0",
                "ns_total_deaths_high": "0",
                "os_total_deaths_low": "0",
                "os_total_deaths_best": "0",
                "os_total_deaths_high": "0",
            }
            if country == "Pakistan" and year == 2025:
                values.update(
                    {
                        "sb_total_deaths_low": "2",
                        "sb_total_deaths_best": "3",
                        "sb_total_deaths_high": "4",
                        "ns_total_deaths_low": "1",
                        "ns_total_deaths_best": "1",
                        "ns_total_deaths_high": "2",
                        "os_total_deaths_low": "0",
                        "os_total_deaths_best": "1",
                        "os_total_deaths_high": "1",
                    }
                )
            rows.append(
                {
                    "country": country,
                    "country_id": country_id,
                    "year": str(year),
                    "region": "Asia",
                    "govt_name": "PRIVATE government label",
                    "Version": "26.1",
                    **values,
                }
            )
    return rows


def _archives(
    registry,
    *,
    unknown_actor: bool = False,
    myanmar_territory: str = "Karen",
):
    members = {
        "armed_conflict": _csv_member(
            ARMED_CONFLICT_HEADER,
            _conflict_rows(
                unknown_actor=unknown_actor,
                myanmar_territory=myanmar_territory,
            ),
            encoding="utf-8-sig",
        ),
        "actor_registry": _csv_member(
            ACTOR_HEADER,
            _actor_rows(),
            encoding="latin-1",
        ),
        "organized_country_year": _csv_member(
            COUNTRY_YEAR_HEADER,
            _country_year_rows(),
            encoding="utf-8-sig",
        ),
    }
    return {
        input_id: _zip(registry.inputs[input_id].member_name, member)
        for input_id, member in members.items()
    }


def _evidence(*, unknown_actor: bool = False, myanmar_territory: str = "Karen"):
    registry = load_registry(REGISTRY)
    archives = _archives(
        registry,
        unknown_actor=unknown_actor,
        myanmar_territory=myanmar_territory,
    )
    receipts = {
        input_id: receipt_for(
            archive,
            spec=registry.inputs[input_id],
            http_last_modified=LAST_MODIFIED,
            retrieved_at=RETRIEVED_AT,
            maximum_source_age_days=registry.source["maximum_source_age_days"],
        )
        for input_id, archive in archives.items()
    }
    return registry, archives, receipts


def _bundle():
    registry, archives, receipts = _evidence()
    return build_bundle(registry, archives=archives, receipts=receipts)


def test_registry_pins_rights_version_encodings_and_adapter_ready_status() -> None:
    registry = load_registry(REGISTRY)
    assert registry.source["dataset_version"] == "26.1"
    assert registry.source["license"] == "CC-BY-4.0"
    assert registry.source["redistribution_status"] == "allowed_with_attribution"
    assert registry.source["maximum_source_age_days"] == 550
    assert registry.inputs["actor_registry"].encoding == "latin-1"
    assert registry.inputs["armed_conflict"].encoding == "utf-8-sig"
    assert registry.inputs["organized_country_year"].encoding == "utf-8-sig"
    assert registry.scope["myanmar_territory_allowlist"] == [
        "Arakan",
        "Common Border",
        "Kachin",
        "Karen",
        "Karenni",
        "Kokang",
        "Lahu",
        "Mon",
        "Nagaland",
        "Shan",
        "Wa",
    ]
    assert registry.raw_sha256 == sha256_bytes(REGISTRY.read_bytes())

    bri = load_bri_registry(BRI_REGISTRY)
    ucdp = next(row for row in bri["sources"] if row["source_id"] == "ucdp_events")
    assert ucdp["rights_status"] == "attribution"
    assert ucdp["implementation"] == "adapter_ready"
    assert "never tactical coordinates or person-level dossiers" in ucdp["notes"]

    implementation = (
        (ROOT / "collectors" / "ucdp_bulk.py").read_text(encoding="utf-8")
        + (ROOT / "scripts" / "ucdp_bulk_pull.py").read_text(encoding="utf-8")
    ).casefold()
    assert "api token" not in implementation
    assert "authorization" not in implementation


def test_fetch_uses_response_aware_safe_transport_and_source_clock() -> None:
    registry, archives, _receipts = _evidence()
    spec = registry.inputs["armed_conflict"]
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_fetcher(url: str, **kwargs: object) -> SafeFetchResponse:
        calls.append((url, kwargs))
        kwargs["url_policy"](url)  # type: ignore[index,operator]
        return SafeFetchResponse(
            status=200,
            headers={"Last-Modified": "Mon, 08 Jun 2026 20:19:01 GMT"},
            body=archives["armed_conflict"],
            url=url,
        )

    fetched = fetch_archive(
        spec,
        maximum_source_age_days=550,
        clock=lambda: RETRIEVED_AT,
        retries=0,
        fetcher=fake_fetcher,
    )
    assert fetched.receipt.http_last_modified == LAST_MODIFIED
    assert fetched.receipt.retrieved_at == RETRIEVED_AT
    assert fetched.receipt.archive_sha256 == sha256_bytes(fetched.archive)
    assert calls == [
        (
            spec.url,
            {
                "max_bytes": spec.maximum_archive_bytes,
                "timeout": 45.0,
                "max_redirects": 0,
                "headers": {
                    "User-Agent": (
                        "palimpsest.info UCDP annual aggregate collector "
                        "(historical non-tactical research; contact desk@palimpsest.info)"
                    ),
                    "Accept": "application/zip",
                },
                "url_policy": calls[0][1]["url_policy"],
            },
        )
    ]


def test_bundle_is_schema_valid_deterministic_and_aggregate_only() -> None:
    first = _bundle()
    second = _bundle()
    first_bytes = canonical_json_bytes(first.to_dict())
    assert first_bytes == canonical_json_bytes(second.to_dict())
    document = json.loads(first_bytes)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)

    assert document["generated_at"] == "2026-08-26T18:30:00Z"
    assert document["coverage"] == {
        "start_year": 1989,
        "end_year": 2025,
        "conflict_year_records": 2,
        "country_year_records": 74,
        "actor_registry_id_count": 5,
    }
    conflicts = document["conflict_years"]
    assert [(row["geography_code"], row["territory_name"]) for row in conflicts] == [
        ("MMR", "Karen"),
        ("PAK-BAL", "Balochistan"),
    ]
    assert {tuple(row["side_b_actor_ids"]) for row in conflicts} == {(287,), (890,)}
    assert all(row["territory_name"] != "Punjab" for row in conflicts)

    pakistan_2025 = next(
        row
        for row in document["country_years"]
        if row["country_code"] == "PAK" and row["year"] == 2025
    )
    assert pakistan_2025["total"] == {"low": 3, "best": 5, "high": 7}
    assert pakistan_2025["total_derivation"] == "sum_of_ucdp_category_bounds"

    serialized = first_bytes.decode("utf-8")
    assert "PRIVATE" not in serialized
    assert "Punjab" not in serialized
    assert "latitude" not in serialized
    assert "longitude" not in serialized
    assert "person_name" not in serialized
    assert "drug_actor" in serialized
    assert '"drug_actor_inference":"prohibited"' in serialized
    public_records = canonical_json_bytes(
        {"conflict_years": conflicts, "country_years": document["country_years"]}
    ).decode("utf-8")
    assert "village" not in public_records.casefold()
    assert "narrative" not in public_records.casefold()


def test_movement_lanes_never_flatten_civic_political_armed_or_state_roles() -> None:
    lanes = {row["lane_id"]: row for row in _bundle().to_dict()["movement_lanes"]}
    assert set(lanes) == {
        "civic_society",
        "electoral_political",
        "armed_conflict_organizations",
        "state_authorities",
        "human_rights_documentation",
    }
    assert lanes["civic_society"]["evidence_state"] == "unavailable"
    assert lanes["electoral_political"]["ucdp_mapping"] == "none"
    assert lanes["human_rights_documentation"]["ucdp_mapping"] == "none"
    assert lanes["armed_conflict_organizations"]["ucdp_mapping"] == (
        "distinct_side_b_actor_ids_by_conflict_year"
    )
    assert (
        "not one unified movement" in lanes["armed_conflict_organizations"]["boundary"]
    )


def test_unknown_actor_schema_drift_stale_or_unauthenticated_bytes_fail_closed() -> (
    None
):
    registry, archives, receipts = _evidence(unknown_actor=True)
    with pytest.raises(UCDPBulkError, match="unknown actor IDs"):
        build_bundle(registry, archives=archives, receipts=receipts)

    village_registry, village_archives, village_receipts = _evidence(
        myanmar_territory="Tiny Village"
    )
    with pytest.raises(UCDPBulkError, match="unreviewed Myanmar territory"):
        build_bundle(
            village_registry,
            archives=village_archives,
            receipts=village_receipts,
        )

    normal_registry, normal_archives, normal_receipts = _evidence()
    armed = normal_registry.inputs["armed_conflict"]
    bad_archive = _zip(
        armed.member_name,
        extract_member(normal_archives["armed_conflict"], armed).raw,
        extra_member=True,
    )
    with pytest.raises(UCDPBulkError, match="exactly one member"):
        extract_member(bad_archive, armed)

    raw_receipt = canonical_json_bytes(normal_receipts["armed_conflict"].to_dict())
    tampered = bytearray(normal_archives["armed_conflict"])
    tampered[-1] ^= 1
    with pytest.raises(UCDPBulkError):
        verify_acquisition_receipt(
            raw_receipt,
            archive=bytes(tampered),
            spec=armed,
            maximum_source_age_days=550,
        )
    with pytest.raises(UCDPBulkError, match="canonical JSON"):
        UCDPAcquisitionReceipt.from_bytes(
            json.dumps(normal_receipts["armed_conflict"].to_dict()).encode("utf-8")
        )
    wrong_url_receipts = dict(normal_receipts)
    wrong_url_receipts["armed_conflict"] = replace(
        normal_receipts["armed_conflict"],
        source_url=normal_registry.inputs["actor_registry"].url,
    )
    with pytest.raises(UCDPBulkError, match="not bound to its receipt"):
        build_bundle(
            normal_registry,
            archives=normal_archives,
            receipts=wrong_url_receipts,
        )
    with pytest.raises(UCDPBulkError, match="stale"):
        receipt_for(
            normal_archives["armed_conflict"],
            spec=armed,
            http_last_modified=LAST_MODIFIED,
            retrieved_at=datetime(2028, 6, 8, tzinfo=UTC),
            maximum_source_age_days=550,
        )


def test_public_scrub_and_schema_reject_tactical_or_person_fields() -> None:
    document = _bundle().to_dict()
    poisoned = deepcopy(document)
    poisoned["conflict_years"][0]["coordinates"] = [25.0, 66.0]
    with pytest.raises(UCDPAggregateError, match="prohibited public field"):
        assert_public_safe(poisoned)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(poisoned)


def test_offline_cli_replay_is_exact_and_never_needs_network(tmp_path: Path) -> None:
    registry, archives, receipts = _evidence()
    evidence_dir = tmp_path / "private-evidence"
    evidence_dir.mkdir(mode=0o700)
    assert stat.S_IMODE(evidence_dir.stat().st_mode) & 0o077 == 0
    for input_id in registry.inputs:
        (evidence_dir / f"{input_id}.zip").write_bytes(archives[input_id])
        (evidence_dir / f"{input_id}.receipt.json").write_bytes(
            canonical_json_bytes(receipts[input_id].to_dict())
        )
    output = tmp_path / "ucdp-aggregate.json"
    command = [
        "build",
        "--input-dir",
        str(evidence_dir),
        "--output",
        str(output),
    ]
    assert pull_main(command) == 0
    first = output.read_bytes()
    assert pull_main(command) == 0
    assert output.read_bytes() == first == canonical_json_bytes(_bundle().to_dict())
    assert pull_main(["check"]) == 0
