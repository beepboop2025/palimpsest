"""Offline security and evidence-contract tests for the UCDP bulk adapter."""

from __future__ import annotations

import csv
import io
import json
import stat
import zipfile
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from collectors.ucdp_bulk import (
    ACTOR_HEADER,
    ARMED_CONFLICT_HEADER,
    COUNTRY_YEAR_HEADER,
    REQUIRED_CITATIONS,
    RIGHTS_DECISION_STATUS,
    RIGHTS_PAGE_URL,
    REVIEW_LOCK_SCHEMA_VERSION,
    REVIEW_POLICY_VERSION,
    UCDPRightsSnapshotReceipt,
    UCDPAcquisitionReceipt,
    UCDPBulkError,
    build_bundle,
    extract_member,
    fetch_archive,
    load_registry,
    load_review_lock,
    parse_review_lock,
    receipt_for,
    verify_acquisition_receipt,
    _csv_rows,
    _parse_actor_registry,
    _parse_conflicts,
    _parse_country_years,
    _transport_policy_sha256,
)
from core.safe_fetch import SafeFetchResponse
from core.ucdp_aggregate import (
    MAX_ACTOR_IDS_PER_SIDE,
    TRUST_MODEL,
    UCDPAggregateError,
    assert_public_safe,
    canonical_public_bytes,
    canonical_json_bytes,
    sha256_bytes,
)
from processors.bri_observatory import load_registry as load_bri_registry
from scripts.ucdp_bulk_pull import main as pull_main

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "ucdp_aggregate.json"
BRI_REGISTRY = ROOT / "config" / "bri_observatory.json"
SCHEMA = ROOT / "protocol" / "ucdp-aggregate-v1.schema.json"
REVIEW_LOCK_SCHEMA = (
    ROOT / "protocol" / "ucdp-reviewed-acquisition-lock-v1.schema.json"
)
REVIEW_LOCK = ROOT / "config" / "ucdp_acquisition_lock.json"
RETRIEVED_AT = datetime(2026, 8, 26, 18, 30, tzinfo=UTC)
LAST_MODIFIED = datetime(2026, 6, 8, 20, 19, 1, tzinfo=UTC)
RIGHTS_OBSERVED_AT = datetime(2026, 8, 26, 18, 31, tzinfo=UTC)
RIGHTS_REVIEWED_AT = datetime(2026, 8, 26, 18, 35, tzinfo=UTC)
PUBLICATION_AT = datetime(2026, 8, 26, 18, 40, tzinfo=UTC)
CURRENT_AT = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
RIGHTS_VALID_UNTIL = datetime(2026, 9, 25, 18, 35, tzinfo=UTC)
RIGHTS_SNAPSHOT = b"<html><body>UCDP 26.1 CC BY 4.0 citation index fixture</body></html>\n"


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
                "cumulative_total_deaths_in_orgvio_low": "0",
                "cumulative_total_deaths_in_orgvio_best": "0",
                "cumulative_total_deaths_in_orgvio_high": "0",
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
                        "cumulative_total_deaths_in_orgvio_low": "3",
                        "cumulative_total_deaths_in_orgvio_best": "5",
                        "cumulative_total_deaths_in_orgvio_high": "7",
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


def _review_material(registry, archives, receipts):
    rights_receipt = UCDPRightsSnapshotReceipt(
        snapshot_sha256=sha256_bytes(RIGHTS_SNAPSHOT),
        snapshot_bytes=len(RIGHTS_SNAPSHOT),
        observed_at=RIGHTS_OBSERVED_AT,
    )
    decision = {
        "status": RIGHTS_DECISION_STATUS,
        "decision": "allow_with_attribution",
        "scope": "annual_aggregate_context_only",
        "rights_page_url": RIGHTS_PAGE_URL,
        "rights_page_snapshot_sha256": sha256_bytes(RIGHTS_SNAPSHOT),
        "rights_page_snapshot_bytes": len(RIGHTS_SNAPSHOT),
        "rights_page_snapshot_receipt_sha256": sha256_bytes(
            canonical_json_bytes(rights_receipt.to_dict())
        ),
        "observed_at": RIGHTS_OBSERVED_AT.isoformat().replace("+00:00", "Z"),
        "reviewed_at": RIGHTS_REVIEWED_AT.isoformat().replace("+00:00", "Z"),
        "valid_until": RIGHTS_VALID_UNTIL.isoformat().replace("+00:00", "Z"),
        "license": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "Uppsala Conflict Data Program (UCDP), version 26.1",
        "reviewed_by": "palimpsest-publication-rights-review",
        "policy_version": REVIEW_POLICY_VERSION,
        "citations": [dict(row) for row in REQUIRED_CITATIONS],
    }
    decision["decision_id"] = sha256_bytes(canonical_json_bytes(decision))
    pins = []
    for input_id in sorted(registry.inputs):
        member = extract_member(archives[input_id], registry.inputs[input_id])
        receipt = receipts[input_id]
        pins.append(
            {
                "input_id": input_id,
                "archive_sha256": sha256_bytes(archives[input_id]),
                "archive_bytes": len(archives[input_id]),
                "member_sha256": member.sha256,
                "member_bytes": len(member.raw),
                "receipt_sha256": sha256_bytes(
                    canonical_json_bytes(receipt.to_dict())
                ),
                "transport_policy_sha256": _transport_policy_sha256(receipt),
            }
        )
    members = {
        input_id: extract_member(archives[input_id], registry.inputs[input_id]).raw
        for input_id in registry.inputs
    }
    rows = {
        input_id: _csv_rows(member, spec=registry.inputs[input_id])
        for input_id, member in members.items()
    }
    actor_ids, actor_ids_sha256 = _parse_actor_registry(rows["actor_registry"])
    conflicts = _parse_conflicts(
        rows["armed_conflict"],
        registry=registry,
        actor_ids=actor_ids,
        armed_receipt=receipts["armed_conflict"],
        actor_receipt=receipts["actor_registry"],
    )
    country_years = _parse_country_years(
        rows["organized_country_year"],
        registry=registry,
        receipt=receipts["organized_country_year"],
    )
    document = {
        "schema_version": REVIEW_LOCK_SCHEMA_VERSION,
        "status": "approved",
        "dataset_version": "26.1",
        "trust_model": TRUST_MODEL,
        "policy": {
            "maximum_future_skew_seconds": 300,
            "maximum_cross_input_retrieval_skew_seconds": 900,
            "maximum_evidence_age_days": 550,
        },
        "rights_decision": decision,
        "expected_public_aggregate": {
            "actor_registry_id_count": len(actor_ids),
            "actor_registry_ids_sha256": actor_ids_sha256,
            "conflict_years_sha256": sha256_bytes(
                canonical_json_bytes([row.to_dict() for row in conflicts])
            ),
            "country_years_sha256": sha256_bytes(
                canonical_json_bytes([row.to_dict() for row in country_years])
            ),
        },
        "inputs": pins,
    }
    raw = canonical_json_bytes(document)
    return parse_review_lock(raw), raw, RIGHTS_SNAPSHOT, rights_receipt


def _bundle():
    registry, archives, receipts = _evidence()
    return _build(registry, archives, receipts)


def _build(
    registry,
    archives,
    receipts,
    *,
    review_lock=None,
    rights_snapshot: bytes | None = None,
    rights_receipt: UCDPRightsSnapshotReceipt | None = None,
    publication_at: datetime = PUBLICATION_AT,
    current_at: datetime = CURRENT_AT,
):
    if review_lock is None or rights_snapshot is None or rights_receipt is None:
        generated_lock, _raw, generated_snapshot, generated_rights_receipt = (
            _review_material(registry, archives, receipts)
        )
        review_lock = review_lock or generated_lock
        rights_snapshot = rights_snapshot or generated_snapshot
        rights_receipt = rights_receipt or generated_rights_receipt
    return build_bundle(
        registry,
        archives=archives,
        receipts=receipts,
        review_lock=review_lock,
        rights_snapshot=rights_snapshot,
        rights_snapshot_receipt=rights_receipt,
        publication_at=publication_at,
        current_at=current_at,
    )


def _reviewed_evidence():
    registry, archives, receipts = _evidence()
    review_lock, _raw, snapshot, rights_receipt = _review_material(
        registry,
        archives,
        receipts,
    )
    return (
        registry,
        archives,
        receipts,
        review_lock,
        snapshot,
        rights_receipt,
    )


def _replace_country_year_archive(registry, archives, receipts, rows):
    updated_archives = dict(archives)
    updated_receipts = dict(receipts)
    spec = registry.inputs["organized_country_year"]
    updated_archives["organized_country_year"] = _zip(
        spec.member_name,
        _csv_member(COUNTRY_YEAR_HEADER, rows, encoding="utf-8-sig"),
    )
    updated_receipts["organized_country_year"] = receipt_for(
        updated_archives["organized_country_year"],
        spec=spec,
        http_last_modified=LAST_MODIFIED,
        retrieved_at=RETRIEVED_AT,
        maximum_source_age_days=registry.source["maximum_source_age_days"],
    )
    return updated_archives, updated_receipts


def test_registry_pins_rights_version_encodings_and_approved_release_status() -> None:
    registry = load_registry(REGISTRY)
    assert registry.source["dataset_version"] == "26.1"
    assert registry.source["license"] == "CC-BY-4.0"
    assert registry.source["redistribution_status"] == "review_required"
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
    review_lock = load_review_lock(REVIEW_LOCK)
    assert review_lock.status == "approved"
    assert review_lock.raw_sha256 == (
        "5975f2bbf1617a06a0c63b9843500082d2a3d2c866314d57ef53719332807fb2"
    )
    assert review_lock.rights_decision is not None
    assert review_lock.rights_decision.decision_id == (
        "e1d3e80d03ddb8b0983dabf4e9107cdc0cdbadb95b57bef41aa5d597db7ad66e"
    )
    assert tuple(pin.input_id for pin in review_lock.inputs) == (
        "actor_registry",
        "armed_conflict",
        "organized_country_year",
    )
    assert review_lock.expected_public_aggregate is not None
    assert review_lock.expected_public_aggregate.to_dict() == {
        "actor_registry_id_count": 1928,
        "actor_registry_ids_sha256": (
            "e818085eb8dc15595ccc391da8c53612afd3acc58118e71e2baa2373bf22947d"
        ),
        "conflict_years_sha256": (
            "d69c8879343f7433e05a10af123f02462d75463a23a9cf68348a6ccd06906063"
        ),
        "country_years_sha256": (
            "2305243bd886cb26eb0f565041b8e788dc607c21a1dd4ab3077e4126fbc2e9bb"
        ),
    }

    bri = load_bri_registry(BRI_REGISTRY)
    ucdp = next(row for row in bri["sources"] if row["source_id"] == "ucdp_events")
    assert ucdp["rights_status"] == "attribution"
    assert ucdp["implementation"] == "live"
    assert "never tactical coordinates or person-level dossiers" in ucdp["notes"]

    assert tuple(row["citation_id"] for row in REQUIRED_CITATIONS) == (
        "davies-pettersson-oberg-2026",
        "gleditsch-et-al-2002",
        "sundberg-melander-2013",
    )


def test_pending_and_approved_review_locks_match_the_closed_schema() -> None:
    schema = json.loads(REVIEW_LOCK_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(
        {
            "schema_version": REVIEW_LOCK_SCHEMA_VERSION,
            "status": "review_required",
            "dataset_version": "26.1",
            "trust_model": TRUST_MODEL,
            "policy": {
                "maximum_future_skew_seconds": 300,
                "maximum_cross_input_retrieval_skew_seconds": 900,
                "maximum_evidence_age_days": 550,
            },
            "rights_decision": None,
            "expected_public_aggregate": None,
            "inputs": [],
        }
    )
    validator.validate(json.loads(REVIEW_LOCK.read_text(encoding="utf-8")))

    registry, archives, receipts = _evidence()
    _lock, approved_raw, _snapshot, _rights_receipt = _review_material(
        registry,
        archives,
        receipts,
    )
    validator.validate(json.loads(approved_raw))


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

    assert document["generated_at"] == "2026-08-26T18:40:00Z"
    assert document["latest_retrieved_at"] == "2026-08-26T18:30:00Z"
    assert document["source"]["trust_model"] == TRUST_MODEL
    assert document["source"]["review_lock_sha256"] == document[
        "review_lock_sha256"
    ]
    assert document["source"]["rights_valid_until"] == "2026-09-25T18:35:00Z"
    assert [row["citation_id"] for row in document["source"]["citations"]] == [
        row["citation_id"] for row in REQUIRED_CITATIONS
    ]
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
        _build(registry, archives, receipts)

    village_registry, village_archives, village_receipts = _evidence(
        myanmar_territory="Tiny Village"
    )
    with pytest.raises(UCDPBulkError, match="unreviewed Myanmar territory"):
        _build(village_registry, village_archives, village_receipts)

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
        _build(normal_registry, normal_archives, wrong_url_receipts)
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
    with pytest.raises(UCDPAggregateError, match="prohibited public field"):
        assert_public_safe({"side_a": "PRIVATE actor name"})


def test_review_lock_not_self_issued_receipt_anchors_exact_evidence() -> None:
    (
        registry,
        archives,
        receipts,
        review_lock,
        snapshot,
        rights_receipt,
    ) = _reviewed_evidence()
    rows = _country_year_rows()
    target = next(
        row
        for row in rows
        if row["country"] == "Pakistan" and row["year"] == "2025"
    )
    for suffix in ("low", "best", "high"):
        target[f"sb_total_deaths_{suffix}"] = "0"
        target[f"ns_total_deaths_{suffix}"] = "0"
        target[f"os_total_deaths_{suffix}"] = "0"
        target[f"cumulative_total_deaths_in_orgvio_{suffix}"] = "0"
    tampered_archives, self_issued_receipts = _replace_country_year_archive(
        registry,
        archives,
        receipts,
        rows,
    )
    # The new receipt is internally valid, but the reviewed Git lock still pins
    # the original archive/member/receipt bytes and therefore refuses it.
    verify_acquisition_receipt(
        canonical_json_bytes(
            self_issued_receipts["organized_country_year"].to_dict()
        ),
        archive=tampered_archives["organized_country_year"],
        spec=registry.inputs["organized_country_year"],
        maximum_source_age_days=550,
    )
    with pytest.raises(UCDPBulkError, match="reviewed acquisition lock"):
        _build(
            registry,
            tampered_archives,
            self_issued_receipts,
            review_lock=review_lock,
            rights_snapshot=snapshot,
            rights_receipt=rights_receipt,
        )


def test_cumulative_totals_must_match_category_sums() -> None:
    registry, archives, receipts = _evidence()
    rows = _country_year_rows()
    target = next(
        row
        for row in rows
        if row["country"] == "Pakistan" and row["year"] == "2025"
    )
    target["cumulative_total_deaths_in_orgvio_best"] = "6"
    changed_archives, changed_receipts = _replace_country_year_archive(
        registry,
        archives,
        receipts,
        rows,
    )
    with pytest.raises(UCDPBulkError, match="cumulative total"):
        _build(registry, changed_archives, changed_receipts)


def test_publication_clocks_rights_expiry_and_cross_input_skew_fail_closed() -> None:
    registry, archives, receipts = _evidence()

    skewed = dict(receipts)
    skewed["armed_conflict"] = replace(
        skewed["armed_conflict"],
        retrieved_at=RETRIEVED_AT - timedelta(minutes=16),
    )
    with pytest.raises(UCDPBulkError, match="retrieval clocks"):
        _build(registry, archives, skewed)

    future = dict(receipts)
    future["armed_conflict"] = replace(
        future["armed_conflict"],
        retrieved_at=PUBLICATION_AT + timedelta(minutes=6),
    )
    with pytest.raises(
        UCDPBulkError,
        match="retrieval clocks|future|rights observation",
    ):
        _build(registry, archives, future)

    with pytest.raises(UCDPBulkError, match="rights decision has expired"):
        _build(
            registry,
            archives,
            receipts,
            current_at=RIGHTS_VALID_UNTIL + timedelta(seconds=1),
        )
    with pytest.raises(UCDPBulkError, match="precedes the rights review"):
        _build(
            registry,
            archives,
            receipts,
            publication_at=RIGHTS_REVIEWED_AT - timedelta(seconds=1),
        )


def test_rights_snapshot_and_review_decision_are_exact_and_expiring() -> None:
    (
        registry,
        archives,
        receipts,
        review_lock,
        snapshot,
        rights_receipt,
    ) = _reviewed_evidence()
    with pytest.raises(UCDPBulkError, match="rights-page snapshot"):
        _build(
            registry,
            archives,
            receipts,
            review_lock=review_lock,
            rights_snapshot=snapshot + b"tampered",
            rights_receipt=rights_receipt,
        )

    lock_value = json.loads(
        canonical_json_bytes(
            {
                "schema_version": review_lock.schema_version,
                "status": review_lock.status,
                "dataset_version": review_lock.dataset_version,
                "trust_model": review_lock.trust_model,
                "policy": review_lock.policy.to_dict(),
                "rights_decision": review_lock.rights_decision.to_dict(),
                "expected_public_aggregate": (
                    review_lock.expected_public_aggregate.to_dict()
                ),
                "inputs": [row.to_dict() for row in review_lock.inputs],
            }
        )
    )
    lock_value["rights_decision"]["valid_until"] = "2027-01-01T00:00:00Z"
    with pytest.raises(UCDPBulkError, match="decision_id"):
        parse_review_lock(canonical_json_bytes(lock_value))


def test_typed_public_boundary_and_actor_id_cap_are_runtime_enforced(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    with pytest.raises(TypeError, match="frozen SourceRecord"):
        replace(bundle, source={"side_a": "PRIVATE actor name"})
    with pytest.raises(UCDPAggregateError, match="64-ID cap"):
        replace(
            bundle.conflict_years[0],
            side_a_actor_ids=tuple(range(1, MAX_ACTOR_IDS_PER_SIDE + 2)),
        )

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    schema["$defs"]["source"]["additionalProperties"] = True
    weakened_schema = tmp_path / "weakened.schema.json"
    weakened_schema.write_text(json.dumps(schema), encoding="utf-8")
    with pytest.raises(UCDPAggregateError, match="not recursively closed"):
        canonical_public_bytes(
            bundle,
            schema_path=weakened_schema,
            forbidden_values=(),
        )


def test_duplicate_last_modified_headers_are_rejected() -> None:
    registry, archives, _receipts = _evidence()
    spec = registry.inputs["armed_conflict"]

    def duplicate_fetcher(url: str, **_kwargs: object) -> SafeFetchResponse:
        return SafeFetchResponse(
            status=200,
            headers={"Last-Modified": "Mon, 08 Jun 2026 20:19:01 GMT"},
            body=archives["armed_conflict"],
            url=url,
            header_fields=(
                ("Last-Modified", "Mon, 08 Jun 2026 20:19:01 GMT"),
                ("last-modified", "Tue, 09 Jun 2026 20:19:01 GMT"),
            ),
        )

    with pytest.raises(UCDPBulkError, match="duplicated Last-Modified"):
        fetch_archive(
            spec,
            maximum_source_age_days=550,
            clock=lambda: RETRIEVED_AT,
            retries=0,
            fetcher=duplicate_fetcher,
        )


def test_offline_cli_replay_is_exact_and_never_needs_network(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry, archives, receipts = _evidence()
    review_lock, review_lock_raw, snapshot, rights_receipt = _review_material(
        registry, archives, receipts
    )
    evidence_dir = tmp_path / "private-evidence"
    evidence_dir.mkdir(mode=0o700)
    assert stat.S_IMODE(evidence_dir.stat().st_mode) & 0o077 == 0
    for input_id in registry.inputs:
        (evidence_dir / f"{input_id}.zip").write_bytes(archives[input_id])
        (evidence_dir / f"{input_id}.receipt.json").write_bytes(
            canonical_json_bytes(receipts[input_id].to_dict())
        )
    (evidence_dir / "rights-page.snapshot.html").write_bytes(snapshot)
    (evidence_dir / "rights-page.receipt.json").write_bytes(
        canonical_json_bytes(rights_receipt.to_dict())
    )
    review_lock_path = tmp_path / "review-lock.json"
    review_lock_path.write_bytes(review_lock_raw)
    monkeypatch.setattr(
        "scripts.ucdp_bulk_pull.DEFAULT_REVIEW_LOCK",
        review_lock_path,
    )
    output = tmp_path / "ucdp-aggregate.json"
    command = [
        "build",
        "--input-dir",
        str(evidence_dir),
        "--publication-at",
        PUBLICATION_AT.isoformat().replace("+00:00", "Z"),
        "--output",
        str(output),
    ]
    assert pull_main(command) == 0
    first = output.read_bytes()
    assert pull_main(command) == 0
    assert output.read_bytes() == first == canonical_json_bytes(_bundle().to_dict())
    assert pull_main(["archive-check", "--input-dir", str(evidence_dir)]) == 0
    assert pull_main(["check"]) == 0
    assert review_lock.status == "approved"


def test_cli_refuses_a_caller_selected_self_issued_review_lock(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as refused:
        pull_main(
            [
                "build",
                "--review-lock",
                str(tmp_path / "self-issued.json"),
                "--input-dir",
                str(tmp_path),
                "--publication-at",
                "2026-08-27T00:00:00Z",
                "--output",
                str(tmp_path / "public.json"),
            ]
        )
    assert refused.value.code == 2
