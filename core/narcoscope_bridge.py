"""Strict admission boundary for NarcoScope's public China aggregate.

The producer artifact is intentionally static JSON, not a trusted plugin.  This
module validates the topic-specific payload shapes that the producer's generic
JSON Schema leaves open, checks arithmetic and attribution invariants, and
binds the admitted bytes to an append-only supersession receipt.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
COMMONS = ROOT / "integrations" / "intelligence-commons"
DEFAULT_ARTIFACT_PATH = COMMONS / "narcoscope-palimpsest-v1.json"
DEFAULT_RECEIPT_PATH = COMMONS / "narcoscope-pin-v1.json"
CANONICAL_URL = (
    "https://drug-price-observatory.vercel.app/data/"
    "narcoscope-palimpsest-v1.json"
)
SCHEMA_VERSION = "narcoscope.palimpsest.china-aggregate.v1"
ARTIFACT_ID = "narcoscope.china.official-coverage"
RECEIPT_SCHEMA = "palimpsest-partner-pin/v1"
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 10_000
MAX_HISTORY = 128

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_ALLOWED_SOURCE_HOSTS = frozenset({
    "www.unodc.org",
    "www.incb.org",
    "sanctionslist.ofac.treas.gov",
    "trade.cites.org",
})
_TOP_FIELDS = frozenset({
    "$schema", "schemaVersion", "artifactId", "dataAsOf", "geography",
    "disclosure", "datasets", "exclusions", "limitations",
})
_DATASET_FIELDS = frozenset({
    "datasetId", "topic", "sourceStatus", "measurement", "temporalCoverage",
    "provenance", "data", "limitations",
})
_MEASUREMENT_FIELDS = frozenset({"status", "valueType", "method", "unit", "grain"})
_COVERAGE_FIELDS = frozenset({"kind", "fromYear", "toYear", "snapshotDate"})
_PROVENANCE_FIELDS = frozenset({
    "publisher", "title", "url", "sourceEdition", "localDataDate", "input",
})
_PRECURSOR_PROVENANCE_FIELDS = _PROVENANCE_FIELDS | frozenset({
    "documentSha256", "retrievedAt",
})
_INPUT_FIELDS = frozenset({"path", "sha256"})
_SOURCE_LOCATOR_FIELDS = frozenset({"pdfPage", "printedPage", "paragraph"})
_AGGREGATION_GROUPS = frozenset({
    "mdma_precursor_substance_mass",
    "meth_pre_precursor_substance_mass",
    "pseudoephedrine_preparation_gross_mass",
    "potassium_permanganate_substance_mass",
    "pseudoephedrine_preparation_mass",
})
_DISCLOSURE = {
    "level": "public_aggregate",
    "sourcePolicy": "official_only",
    "subjectEntityDisclosure": "none",
    "exactAddressDisclosure": "none",
    "identifierDisclosure": "none",
    "illustrativeDataIncluded": False,
    "runtimeCoupling": "none_static_artifact",
}
_DATASETS = {
    "retailDrugPrices": (
        "retail_drug_prices", "drug_market_prices", "src/data/prices.ts"
    ),
    "drugSeizures": (
        "drug_seizures", "drug_seizures", "src/data/seizures.json"
    ),
    "precursorCorridorIncidents": (
        "precursor_corridor_incidents", "precursor_flows", "src/data/flows.ts"
    ),
    "ofacDesignations": (
        "ofac_designations", "official_designations", "src/data/designations.json"
    ),
    "wildlifeConfiscations": (
        "wildlife_confiscations", "wildlife_confiscations",
        "src/data/wildlifeSeizures.json",
    ),
}
_RECEIPT_FIELDS = frozenset({
    "schema", "producer", "source_url", "artifact_id", "current", "superseded",
})
_PIN_FIELDS = frozenset({"data_as_of", "sha256", "admitted_at"})
_SUPERSEDED_FIELDS = frozenset({
    "data_as_of", "sha256", "admitted_at", "superseded_at",
})
_SENSITIVE_KEYS = frozenset({
    "name", "alias", "aliases", "entitynumber", "address", "addresses",
    "identity", "identities", "wallet", "wallets", "message", "messages",
    "phone", "email", "handle", "ioc", "iocs",
})


class NarcoScopeBridgeError(ValueError):
    """A candidate artifact or its pin receipt failed closed."""


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NarcoScopeBridgeError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_loads(raw: bytes, *, label: str = "NarcoScope artifact") -> dict[str, Any]:
    """Parse bounded UTF-8 JSON while rejecting duplicate/non-finite values."""

    if len(raw) > MAX_BYTES:
        raise NarcoScopeBridgeError(f"{label} exceeds {MAX_BYTES} bytes")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                NarcoScopeBridgeError(f"non-finite JSON number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NarcoScopeBridgeError(f"{label} is not strict UTF-8 JSON") from exc
    if type(value) is not dict:
        raise NarcoScopeBridgeError(f"{label} must be an object")
    return value


def _fields(value: Any, expected: frozenset[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != expected:
        actual = set(value) if type(value) is dict else set()
        raise NarcoScopeBridgeError(
            f"{path} fields differ (missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)})"
        )
    return value


def _text(value: Any, path: str, *, maximum: int = 4096) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise NarcoScopeBridgeError(f"{path} is not bounded non-empty text")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise NarcoScopeBridgeError(f"{path} contains unsafe Unicode")
    return value


def _text_list(value: Any, path: str, *, maximum: int = 64) -> list[str]:
    if type(value) is not list or not 1 <= len(value) <= maximum:
        raise NarcoScopeBridgeError(f"{path} must be a bounded non-empty array")
    result = [_text(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if len(result) != len(set(result)):
        raise NarcoScopeBridgeError(f"{path} contains duplicate statements")
    return result


def _day(value: Any, path: str, *, nullable: bool = False) -> date | None:
    if nullable and value is None:
        return None
    if type(value) is not str or not _DATE_RE.fullmatch(value):
        raise NarcoScopeBridgeError(f"{path} is not an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise NarcoScopeBridgeError(f"{path} is not a real date") from exc


def _instant(value: Any, path: str) -> datetime:
    if type(value) is not str or not _TIME_RE.fullmatch(value):
        raise NarcoScopeBridgeError(f"{path} is not canonical UTC time")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise NarcoScopeBridgeError(f"{path} is not a real UTC time") from exc


def _count(value: Any, path: str, *, positive: bool = False) -> int:
    lower = 1 if positive else 0
    if type(value) is not int or not lower <= value <= 9_007_199_254_740_991:
        raise NarcoScopeBridgeError(f"{path} is not a safe integer")
    return value


def _decimal(value: Any, path: str, *, positive: bool = False) -> Decimal:
    if type(value) not in {int, float} or isinstance(value, bool):
        raise NarcoScopeBridgeError(f"{path} is not a JSON number")
    if isinstance(value, float) and not math.isfinite(value):
        raise NarcoScopeBridgeError(f"{path} is not finite")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise NarcoScopeBridgeError(f"{path} is not decimal-compatible") from exc
    if result < 0 or (positive and result <= 0):
        raise NarcoScopeBridgeError(f"{path} is outside the nonnegative domain")
    return result


def _source_url(value: Any, path: str) -> str:
    url = _text(value, path, maximum=2048)
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if (
        parsed.scheme != "https"
        or host not in _ALLOWED_SOURCE_HOSTS
        or address is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.fragment
    ):
        raise NarcoScopeBridgeError(f"{path} is outside the reviewed HTTPS allowlist")
    return url


def _scan_public_aggregate(value: Any, path: str = "artifact") -> None:
    if type(value) is dict:
        for key, item in value.items():
            if key.casefold() in _SENSITIVE_KEYS:
                raise NarcoScopeBridgeError(f"sensitive field is forbidden at {path}.{key}")
            _scan_public_aggregate(item, f"{path}.{key}")
    elif type(value) is list:
        for index, item in enumerate(value):
            _scan_public_aggregate(item, f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise NarcoScopeBridgeError(f"non-finite number at {path}")


def _year(value: Any, path: str) -> int:
    if type(value) is not int or not 1900 <= value <= 2200:
        raise NarcoScopeBridgeError(f"{path} is not a bounded year")
    return value


def _source_locator(value: Any, path: str) -> tuple[int, int, int]:
    locator = _fields(value, _SOURCE_LOCATOR_FIELDS, path)
    return tuple(
        _count(locator[field], f"{path}.{field}", positive=True)
        for field in ("pdfPage", "printedPage", "paragraph")
    )


def _validate_retail(data: Any, coverage: Mapping[str, Any]) -> None:
    row = _fields(data, frozenset({"recordCount", "observations"}), "retail.data")
    observations = row["observations"]
    if type(observations) is not list or len(observations) > MAX_ROWS:
        raise NarcoScopeBridgeError("retail observations are not bounded")
    if _count(row["recordCount"], "retail.recordCount") != len(observations):
        raise NarcoScopeBridgeError("retail recordCount does not match observations")
    identities: set[tuple[str, int]] = set()
    for index, item in enumerate(observations):
        item = _fields(
            item,
            frozenset({"drug", "year", "priceUsdPerGram", "purityPct"}),
            f"retail.observations[{index}]",
        )
        identity = (
            _text(item["drug"], f"retail.observations[{index}].drug", maximum=80),
            _year(item["year"], f"retail.observations[{index}].year"),
        )
        if identity in identities:
            raise NarcoScopeBridgeError("retail observations contain a duplicate grain")
        identities.add(identity)
        _decimal(item["priceUsdPerGram"], "retail.priceUsdPerGram", positive=True)
        if item["purityPct"] is not None:
            purity = _decimal(item["purityPct"], "retail.purityPct")
            if purity > 100:
                raise NarcoScopeBridgeError("retail purityPct exceeds 100")
        if not coverage["fromYear"] <= identity[1] <= coverage["toYear"]:
            raise NarcoScopeBridgeError("retail observation is outside temporal coverage")


def _drug_group_rows(
    value: Any, path: str
) -> tuple[int, Decimal, dict[str, tuple[int, Decimal]]]:
    if type(value) is not list or len(value) > MAX_ROWS:
        raise NarcoScopeBridgeError(f"{path} is not a bounded array")
    groups: dict[str, tuple[int, Decimal]] = {}
    total_count = 0
    total_quantity = Decimal(0)
    for index, item in enumerate(value):
        item = _fields(
            item,
            frozenset({"drugGroup", "sourceRowCount", "quantityKg"}),
            f"{path}[{index}]",
        )
        group = _text(item["drugGroup"], f"{path}[{index}].drugGroup", maximum=160)
        if group in groups:
            raise NarcoScopeBridgeError(f"{path} contains duplicate drugGroup")
        count = _count(item["sourceRowCount"], f"{path}[{index}].sourceRowCount")
        quantity = _decimal(item["quantityKg"], f"{path}[{index}].quantityKg")
        groups[group] = (count, quantity)
        total_count += count
        total_quantity += quantity
    return total_count, total_quantity, groups


def _validate_seizures(data: Any, coverage: Mapping[str, Any]) -> None:
    row = _fields(
        data,
        frozenset({"sourceRowCount", "quantityKg", "byYear", "byDrugGroup"}),
        "seizures.data",
    )
    expected_count = _count(row["sourceRowCount"], "seizures.sourceRowCount")
    expected_quantity = _decimal(row["quantityKg"], "seizures.quantityKg")
    years = row["byYear"]
    if type(years) is not list or not years or len(years) > 301:
        raise NarcoScopeBridgeError("seizures.byYear is not a bounded non-empty array")
    seen_years: set[int] = set()
    aggregate: dict[str, tuple[int, Decimal]] = {}
    total_count = 0
    total_quantity = Decimal(0)
    for index, item in enumerate(years):
        item = _fields(
            item,
            frozenset({"year", "sourceRowCount", "quantityKg", "byDrugGroup"}),
            f"seizures.byYear[{index}]",
        )
        year = _year(item["year"], f"seizures.byYear[{index}].year")
        if year in seen_years or not coverage["fromYear"] <= year <= coverage["toYear"]:
            raise NarcoScopeBridgeError("seizure year is duplicate or outside coverage")
        seen_years.add(year)
        year_count = _count(item["sourceRowCount"], "seizures.year.sourceRowCount")
        year_quantity = _decimal(item["quantityKg"], "seizures.year.quantityKg")
        child_count, child_quantity, child_groups = _drug_group_rows(
            item["byDrugGroup"], f"seizures.byYear[{index}].byDrugGroup"
        )
        if (year_count, year_quantity) != (child_count, child_quantity):
            raise NarcoScopeBridgeError("seizure year totals do not match drug groups")
        total_count += year_count
        total_quantity += year_quantity
        for group, (count, quantity) in child_groups.items():
            prior_count, prior_quantity = aggregate.get(group, (0, Decimal(0)))
            aggregate[group] = (prior_count + count, prior_quantity + quantity)
    if (total_count, total_quantity) != (expected_count, expected_quantity):
        raise NarcoScopeBridgeError("seizure headline totals do not match year rows")
    group_count, group_quantity, groups = _drug_group_rows(
        row["byDrugGroup"], "seizures.byDrugGroup"
    )
    if (group_count, group_quantity) != (expected_count, expected_quantity):
        raise NarcoScopeBridgeError("seizure headline totals do not match group rows")
    if groups != aggregate:
        raise NarcoScopeBridgeError("seizure group totals do not reconcile across years")


def _validate_precursors(data: Any, coverage: Mapping[str, Any]) -> None:
    row = _fields(
        data,
        frozenset({
            "includedQuantitativeRecordCount", "includedContextRecordCount",
            "quantityAggregation", "corridors", "contextRecords",
        }),
        "precursors.data",
    )
    corridors = row["corridors"]
    if type(corridors) is not list or len(corridors) > MAX_ROWS:
        raise NarcoScopeBridgeError("precursor corridors are not bounded")
    if _count(
        row["includedQuantitativeRecordCount"],
        "precursors.includedQuantitativeRecordCount",
    ) != len(corridors):
        raise NarcoScopeBridgeError("precursor quantitative count does not match corridors")
    exact_count = 0
    non_exact_count = 0
    eligible_count = 0
    eligible_total = Decimal(0)
    eligible_groups: set[str] = set()
    identities: set[tuple[Any, ...]] = set()
    for index, item in enumerate(corridors):
        item = _fields(
            item,
            frozenset({
                "originAttribution", "reportedOrigin", "transit", "destination",
                "seizureLocation", "year", "precursor", "quantityKg",
                "quantityRelation", "quantityBasis", "recordKind",
                "aggregationEligibility", "aggregationGroup", "incidentCount",
                "sourceLocator",
            }),
            f"precursors.corridors[{index}]",
        )
        attribution = item["originAttribution"]
        origin = item["reportedOrigin"]
        if not (
            (attribution == "china_only" and origin == "China")
            or (
                attribution == "joint_origin_includes_china"
                and origin == "China / India"
            )
        ):
            raise NarcoScopeBridgeError(
                "precursor origin attribution may not allocate a joint origin to China"
            )
        year = _year(item["year"], f"precursors.corridors[{index}].year")
        if not coverage["fromYear"] <= year <= coverage["toYear"]:
            raise NarcoScopeBridgeError("precursor corridor is outside temporal coverage")
        destination = _text(item["destination"], "precursors.destination", maximum=120)
        transit = item["transit"]
        if transit is not None:
            transit = _text(transit, "precursors.transit", maximum=120)
        seizure_location = item["seizureLocation"]
        if seizure_location is not None:
            seizure_location = _text(
                seizure_location, "precursors.seizureLocation", maximum=120
            )
        precursor = _text(item["precursor"], "precursors.precursor", maximum=120)
        quantity = _decimal(item["quantityKg"], "precursors.quantityKg", positive=True)
        relation = item["quantityRelation"]
        if type(relation) is not str or relation not in {
            "exact", "approx", "less_than", "greater_than",
        }:
            raise NarcoScopeBridgeError("precursor quantity relation is invalid")
        _text(item["quantityBasis"], "precursors.quantityBasis")
        record_kind = item["recordKind"]
        if type(record_kind) is not str or record_kind not in {
            "single_incident", "multi_incident_aggregate", "annual_aggregate",
            "derived_subtotal",
        }:
            raise NarcoScopeBridgeError("precursor record kind is invalid")
        eligibility = item["aggregationEligibility"]
        if type(eligibility) is not str or eligibility not in {
            "eligible", "ineligible_non_exact", "ineligible_derived",
            "ineligible_incompatible_basis",
        }:
            raise NarcoScopeBridgeError("precursor aggregation eligibility is invalid")
        aggregation_group = item["aggregationGroup"]
        if aggregation_group is not None and (
            type(aggregation_group) is not str
            or aggregation_group not in _AGGREGATION_GROUPS
        ):
            raise NarcoScopeBridgeError("precursor aggregation group is invalid")
        if relation != "exact" and eligibility != "ineligible_non_exact":
            raise NarcoScopeBridgeError(
                "non-exact precursor quantity must be aggregation-ineligible"
            )
        if relation == "exact" and eligibility == "ineligible_non_exact":
            raise NarcoScopeBridgeError(
                "exact precursor quantity has non-exact aggregation eligibility"
            )
        if record_kind == "derived_subtotal":
            if eligibility != "ineligible_derived" or aggregation_group is not None:
                raise NarcoScopeBridgeError(
                    "derived precursor subtotal must remain aggregation-ineligible"
                )
        elif eligibility == "ineligible_derived":
            raise NarcoScopeBridgeError(
                "non-derived precursor row has derived aggregation eligibility"
            )
        if eligibility == "eligible":
            if (
                relation != "exact"
                or record_kind == "derived_subtotal"
                or aggregation_group is None
            ):
                raise NarcoScopeBridgeError(
                    "aggregation-eligible precursor row is not exact and comparable"
                )
            eligible_count += 1
            eligible_total += quantity
            eligible_groups.add(aggregation_group)
        elif (
            eligibility == "ineligible_incompatible_basis"
            and aggregation_group is not None
        ):
            raise NarcoScopeBridgeError(
                "incompatible precursor basis may not claim an aggregation group"
            )
        incident_count = item["incidentCount"]
        if incident_count is not None:
            incident_count = _count(
                incident_count, "precursors.incidentCount", positive=True
            )
        locator = _source_locator(
            item["sourceLocator"], f"precursors.corridors[{index}].sourceLocator"
        )
        identity = (
            attribution, origin, transit, destination, seizure_location, year,
            precursor, quantity, relation, record_kind, incident_count, locator,
        )
        if identity in identities:
            raise NarcoScopeBridgeError("precursor corridors contain a duplicate record")
        identities.add(identity)
        if relation == "exact":
            exact_count += 1
        else:
            non_exact_count += 1

    context_records = row["contextRecords"]
    if type(context_records) is not list or len(context_records) > MAX_ROWS:
        raise NarcoScopeBridgeError("precursor context records are not bounded")
    if _count(
        row["includedContextRecordCount"], "precursors.includedContextRecordCount"
    ) != len(context_records):
        raise NarcoScopeBridgeError("precursor context count does not match records")
    context_ids: set[str] = set()
    for index, item in enumerate(context_records):
        item = _fields(
            item,
            frozenset({
                "contextId", "precursor", "origins", "destinations", "year",
                "recordKind", "allocationStatus", "operationReportedSeizureCount",
                "countScope", "summary", "sourceLocator",
            }),
            f"precursors.contextRecords[{index}]",
        )
        context_id = _text(
            item["contextId"], f"precursors.contextRecords[{index}].contextId",
            maximum=160,
        )
        if context_id in context_ids:
            raise NarcoScopeBridgeError("precursor context contains a duplicate id")
        context_ids.add(context_id)
        _text(item["precursor"], "precursors.context.precursor", maximum=120)
        _text_list(item["origins"], "precursors.context.origins")
        _text_list(item["destinations"], "precursors.context.destinations")
        year = _year(item["year"], f"precursors.contextRecords[{index}].year")
        if not coverage["fromYear"] <= year <= coverage["toYear"]:
            raise NarcoScopeBridgeError("precursor context is outside temporal coverage")
        if (
            item["recordKind"] != "qualitative_context"
            or item["allocationStatus"]
            != "not_reported_by_origin_destination_pair"
            or item["countScope"] != "four_reporting_countries_operation_total"
        ):
            raise NarcoScopeBridgeError("precursor context lost its non-bilateral scope")
        _count(
            item["operationReportedSeizureCount"],
            "precursors.context.operationReportedSeizureCount",
            positive=True,
        )
        _text(item["summary"], "precursors.context.summary")
        _source_locator(
            item["sourceLocator"],
            f"precursors.contextRecords[{index}].sourceLocator",
        )

    aggregation = _fields(
        row["quantityAggregation"],
        frozenset({
            "status", "exactRecordCount", "nonExactRecordCount",
            "eligibleRecordCount", "excludedRecordCount", "aggregationGroup",
            "summedQuantityKg",
        }),
        "precursors.quantityAggregation",
    )
    excluded_count = len(corridors) - eligible_count
    reported_counts = (
        _count(aggregation["exactRecordCount"], "precursors.exactRecordCount"),
        _count(
            aggregation["nonExactRecordCount"], "precursors.nonExactRecordCount"
        ),
        _count(
            aggregation["eligibleRecordCount"], "precursors.eligibleRecordCount"
        ),
        _count(
            aggregation["excludedRecordCount"], "precursors.excludedRecordCount"
        ),
    )
    if reported_counts != (
        exact_count, non_exact_count, eligible_count, excluded_count
    ):
        raise NarcoScopeBridgeError(
            "precursor aggregation counts do not match row qualifications"
        )
    aggregate_group = aggregation["aggregationGroup"]
    if aggregate_group is not None and (
        type(aggregate_group) is not str
        or aggregate_group not in _AGGREGATION_GROUPS
    ):
        raise NarcoScopeBridgeError("precursor aggregate group is invalid")

    if not corridors:
        expected_status = "not_computed_no_records"
    elif not eligible_count:
        expected_status = (
            "not_computed_non_exact_inputs"
            if non_exact_count
            else "not_computed_ineligible_exact_inputs"
        )
    elif len(eligible_groups) != 1:
        expected_status = "not_computed_mixed_aggregation_groups"
    else:
        expected_status = "computed_exact_only"

    if aggregation["status"] != expected_status:
        raise NarcoScopeBridgeError(
            "precursor aggregation status does not match eligible rows"
        )
    if expected_status == "computed_exact_only":
        only_group = next(iter(eligible_groups))
        if aggregate_group != only_group:
            raise NarcoScopeBridgeError(
                "precursor aggregate group does not match eligible rows"
            )
        if _decimal(
            aggregation["summedQuantityKg"], "precursors.summedQuantityKg"
        ) != eligible_total:
            raise NarcoScopeBridgeError(
                "eligible precursor quantity total is inconsistent"
            )
    elif aggregate_group is not None or aggregation["summedQuantityKg"] is not None:
        raise NarcoScopeBridgeError(
            "ineligible precursor values may not enter a quantity total"
        )


def _validate_ofac(data: Any, coverage: Mapping[str, Any]) -> None:
    del coverage
    row = _fields(
        data,
        frozenset({
            "recordCount", "narcoticsSpecificProgramRecordCount", "tcoOnlyRecordCount",
            "byEntityType", "byProgram", "multiCountryRecordCount",
        }),
        "ofac.data",
    )
    total = _count(row["recordCount"], "ofac.recordCount")
    narcotics_count = _count(
        row["narcoticsSpecificProgramRecordCount"],
        "ofac.narcoticsSpecificProgramRecordCount",
    )
    tco_only_count = _count(row["tcoOnlyRecordCount"], "ofac.tcoOnlyRecordCount")
    if narcotics_count + tco_only_count != total:
        raise NarcoScopeBridgeError("OFAC authority classes do not partition recordCount")
    types = row["byEntityType"]
    if type(types) is not list or len(types) != 2:
        raise NarcoScopeBridgeError("ofac entity types must contain the two aggregate classes")
    by_type: dict[str, int] = {}
    for index, item in enumerate(types):
        item = _fields(item, frozenset({"entityType", "count"}), f"ofac.byEntityType[{index}]")
        entity_type = item["entityType"]
        if entity_type not in {"individual", "organization"} or entity_type in by_type:
            raise NarcoScopeBridgeError("ofac entity type is unknown or duplicate")
        by_type[entity_type] = _count(item["count"], "ofac.byEntityType.count")
    if sum(by_type.values()) != total:
        raise NarcoScopeBridgeError("ofac entity type counts do not match recordCount")
    programs = row["byProgram"]
    if type(programs) is not list or len(programs) > 64:
        raise NarcoScopeBridgeError("ofac byProgram is not bounded")
    seen_programs: set[str] = set()
    for index, item in enumerate(programs):
        item = _fields(item, frozenset({"program", "label", "count"}), f"ofac.byProgram[{index}]")
        program = _text(item["program"], "ofac.program", maximum=80)
        if program in seen_programs:
            raise NarcoScopeBridgeError("ofac programs contain a duplicate")
        seen_programs.add(program)
        _text(item["label"], "ofac.label", maximum=160)
        if _count(item["count"], "ofac.program.count") > total:
            raise NarcoScopeBridgeError("ofac program count exceeds recordCount")
    if _count(row["multiCountryRecordCount"], "ofac.multiCountryRecordCount") > total:
        raise NarcoScopeBridgeError("ofac multi-country count exceeds recordCount")


def _validate_wildlife(data: Any, coverage: Mapping[str, Any]) -> None:
    del coverage
    row = _fields(
        data,
        frozenset({"datasetRecordCount", "exporterOfRecord", "importerOfRecord"}),
        "wildlife.data",
    )
    total = _count(row["datasetRecordCount"], "wildlife.datasetRecordCount")
    for role in ("exporterOfRecord", "importerOfRecord"):
        item = _fields(
            row[role], frozenset({"recordCount", "rankInRetainedTable"}),
            f"wildlife.{role}",
        )
        if _count(item["recordCount"], f"wildlife.{role}.recordCount") > total:
            raise NarcoScopeBridgeError(f"wildlife {role} count exceeds dataset count")
        _count(item["rankInRetainedTable"], f"wildlife.{role}.rank", positive=True)


_PAYLOAD_VALIDATORS = {
    "retailDrugPrices": _validate_retail,
    "drugSeizures": _validate_seizures,
    "precursorCorridorIncidents": _validate_precursors,
    "ofacDesignations": _validate_ofac,
    "wildlifeConfiscations": _validate_wildlife,
}


def validate_artifact(document: Mapping[str, Any]) -> None:
    """Validate structure, arithmetic, provenance and aggregate-only safety."""

    _fields(document, _TOP_FIELDS, "artifact")
    if document["$schema"] != "./narcoscope-palimpsest-v1.schema.json":
        raise NarcoScopeBridgeError("unexpected NarcoScope $schema")
    if document["schemaVersion"] != SCHEMA_VERSION or document["artifactId"] != ARTIFACT_ID:
        raise NarcoScopeBridgeError("unsupported NarcoScope artifact identity")
    data_as_of = _day(document["dataAsOf"], "artifact.dataAsOf")
    if document["geography"] != {"country": "China", "iso2": "CN", "iso3": "CHN"}:
        raise NarcoScopeBridgeError("NarcoScope geography is not the reviewed China scope")
    if document["disclosure"] != _DISCLOSURE:
        raise NarcoScopeBridgeError("NarcoScope disclosure boundary changed")
    datasets = document["datasets"]
    if type(datasets) is not dict or set(datasets) != set(_DATASETS):
        raise NarcoScopeBridgeError("NarcoScope dataset set changed")
    latest_source_day: date | None = None
    for key, (dataset_id, topic, input_path) in _DATASETS.items():
        dataset = _fields(datasets[key], _DATASET_FIELDS, f"datasets.{key}")
        if (
            dataset["datasetId"] != dataset_id
            or dataset["topic"] != topic
            or dataset["sourceStatus"] != "official"
        ):
            raise NarcoScopeBridgeError(f"datasets.{key} identity/status changed")
        measurement = _fields(
            dataset["measurement"], _MEASUREMENT_FIELDS, f"datasets.{key}.measurement"
        )
        if measurement["status"] not in {"official_reported", "official_action_record"}:
            raise NarcoScopeBridgeError(f"datasets.{key} measurement status is invalid")
        if measurement["valueType"] not in {
            "statistical_measurement", "administrative_measurement", "administrative_action"
        }:
            raise NarcoScopeBridgeError(f"datasets.{key} measurement type is invalid")
        for field in ("method", "unit", "grain"):
            _text(measurement[field], f"datasets.{key}.measurement.{field}")
        coverage = _fields(
            dataset["temporalCoverage"], _COVERAGE_FIELDS,
            f"datasets.{key}.temporalCoverage",
        )
        if coverage["kind"] == "year_range":
            start = _year(coverage["fromYear"], f"datasets.{key}.fromYear")
            end = _year(coverage["toYear"], f"datasets.{key}.toYear")
            if start > end or coverage["snapshotDate"] is not None:
                raise NarcoScopeBridgeError(f"datasets.{key} year coverage is inconsistent")
        elif coverage["kind"] == "snapshot":
            if coverage["fromYear"] is not None or coverage["toYear"] is not None:
                raise NarcoScopeBridgeError(f"datasets.{key} snapshot has year bounds")
            snapshot = _day(coverage["snapshotDate"], f"datasets.{key}.snapshotDate")
            latest_source_day = max(filter(None, (latest_source_day, snapshot)))
        else:
            raise NarcoScopeBridgeError(f"datasets.{key} coverage kind is invalid")
        provenance_fields = (
            _PRECURSOR_PROVENANCE_FIELDS
            if key == "precursorCorridorIncidents"
            else _PROVENANCE_FIELDS
        )
        provenance = _fields(
            dataset["provenance"], provenance_fields, f"datasets.{key}.provenance"
        )
        for field in ("publisher", "title", "sourceEdition"):
            _text(provenance[field], f"datasets.{key}.provenance.{field}")
        _source_url(provenance["url"], f"datasets.{key}.provenance.url")
        if key == "precursorCorridorIncidents":
            if not _SHA_RE.fullmatch(provenance["documentSha256"]):
                raise NarcoScopeBridgeError("precursor document SHA-256 is invalid")
            _instant(provenance["retrievedAt"], "precursor.provenance.retrievedAt")
        local_day = _day(
            provenance["localDataDate"], f"datasets.{key}.provenance.localDataDate",
            nullable=True,
        )
        if local_day is not None:
            latest_source_day = max(filter(None, (latest_source_day, local_day)))
        source_input = _fields(
            provenance["input"], _INPUT_FIELDS, f"datasets.{key}.provenance.input"
        )
        if source_input["path"] != input_path:
            raise NarcoScopeBridgeError(f"datasets.{key} input path changed")
        if type(source_input["sha256"]) is not str or not _SHA_RE.fullmatch(source_input["sha256"]):
            raise NarcoScopeBridgeError(f"datasets.{key} input SHA-256 is invalid")
        _text_list(dataset["limitations"], f"datasets.{key}.limitations")
        _PAYLOAD_VALIDATORS[key](dataset["data"], coverage)
    if latest_source_day is not None and data_as_of < latest_source_day:
        raise NarcoScopeBridgeError("dataAsOf precedes a retained source/snapshot date")
    exclusions = document["exclusions"]
    if type(exclusions) is not list or not 1 <= len(exclusions) <= 64:
        raise NarcoScopeBridgeError("exclusions must be a bounded non-empty array")
    seen_components: set[str] = set()
    for index, item in enumerate(exclusions):
        item = _fields(
            item, frozenset({"component", "classification", "reason"}),
            f"exclusions[{index}]",
        )
        component = _text(item["component"], f"exclusions[{index}].component", maximum=120)
        if not _SAFE_ID_RE.fullmatch(component) or component in seen_components:
            raise NarcoScopeBridgeError("exclusion component is unsafe or duplicate")
        seen_components.add(component)
        if item["classification"] not in {
            "illustrative", "illustrative_or_constructed", "unreviewed_leads",
            "privacy_minimized",
        }:
            raise NarcoScopeBridgeError("exclusion classification is invalid")
        _text(item["reason"], f"exclusions[{index}].reason")
    _text_list(document["limitations"], "artifact.limitations")
    _scan_public_aggregate(document)


def artifact_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_artifact(path: Path = DEFAULT_ARTIFACT_PATH) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise NarcoScopeBridgeError(f"cannot read NarcoScope artifact: {path}") from exc
    document = strict_json_loads(raw)
    validate_artifact(document)
    return document, raw


def validate_receipt(receipt: Mapping[str, Any], *, artifact: bytes | None = None) -> None:
    _fields(receipt, _RECEIPT_FIELDS, "receipt")
    if (
        receipt["schema"] != RECEIPT_SCHEMA
        or receipt["producer"] != "narcoscope"
        or receipt["source_url"] != CANONICAL_URL
        or receipt["artifact_id"] != ARTIFACT_ID
    ):
        raise NarcoScopeBridgeError("NarcoScope pin identity changed")
    current = _fields(receipt["current"], _PIN_FIELDS, "receipt.current")
    current_day = _day(current["data_as_of"], "receipt.current.data_as_of")
    current_time = _instant(current["admitted_at"], "receipt.current.admitted_at")
    if type(current["sha256"]) is not str or not _SHA_RE.fullmatch(current["sha256"]):
        raise NarcoScopeBridgeError("receipt.current SHA-256 is invalid")
    if artifact is not None:
        document = strict_json_loads(artifact)
        validate_artifact(document)
        if artifact_sha256(artifact) != current["sha256"]:
            raise NarcoScopeBridgeError("pin receipt does not match admitted artifact bytes")
        if document["dataAsOf"] != current["data_as_of"]:
            raise NarcoScopeBridgeError("pin receipt data_as_of does not match artifact")
    superseded = receipt["superseded"]
    if type(superseded) is not list or len(superseded) > MAX_HISTORY:
        raise NarcoScopeBridgeError("receipt supersession history is not bounded")
    seen_hashes = {current["sha256"]}
    last_day: date | None = None
    last_superseded: datetime | None = None
    for index, item in enumerate(superseded):
        item = _fields(item, _SUPERSEDED_FIELDS, f"receipt.superseded[{index}]")
        item_day = _day(item["data_as_of"], f"receipt.superseded[{index}].data_as_of")
        admitted = _instant(item["admitted_at"], f"receipt.superseded[{index}].admitted_at")
        superseded_at = _instant(
            item["superseded_at"], f"receipt.superseded[{index}].superseded_at"
        )
        digest = item["sha256"]
        if type(digest) is not str or not _SHA_RE.fullmatch(digest) or digest in seen_hashes:
            raise NarcoScopeBridgeError("receipt supersession SHA-256 is invalid or duplicate")
        if admitted > superseded_at or superseded_at > current_time:
            raise NarcoScopeBridgeError("receipt supersession times are out of order")
        if last_day is not None and item_day < last_day:
            raise NarcoScopeBridgeError("receipt supersession data dates regress")
        if last_superseded is not None and superseded_at < last_superseded:
            raise NarcoScopeBridgeError("receipt supersession clocks regress")
        seen_hashes.add(digest)
        last_day = item_day
        last_superseded = superseded_at
    if last_day is not None and current_day < last_day:
        raise NarcoScopeBridgeError("current pin data date regresses behind history")


def load_receipt(path: Path = DEFAULT_RECEIPT_PATH, *, artifact: bytes | None = None) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise NarcoScopeBridgeError(f"cannot read NarcoScope pin receipt: {path}") from exc
    receipt = strict_json_loads(raw, label="NarcoScope pin receipt")
    validate_receipt(receipt, artifact=artifact)
    return receipt


def admission_receipt(
    candidate: Mapping[str, Any],
    candidate_bytes: bytes,
    *,
    admitted_at: datetime,
    previous_receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Create a monotonic receipt, recording prior bytes as superseded."""

    if admitted_at.tzinfo is None or admitted_at.utcoffset() is None:
        raise NarcoScopeBridgeError("admitted_at must be timezone-aware")
    validate_artifact(candidate)
    normalized_time = admitted_at.astimezone(timezone.utc).replace(microsecond=0)
    timestamp = normalized_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = artifact_sha256(candidate_bytes)
    superseded: list[dict[str, Any]] = []
    if previous_receipt is not None:
        validate_receipt(previous_receipt)
        previous = previous_receipt["current"]
        previous_admitted = _instant(
            previous["admitted_at"], "previous.admitted_at"
        )
        if normalized_time < previous_admitted:
            raise NarcoScopeBridgeError(
                "candidate admission clock regresses behind current pin"
            )
        if _day(candidate["dataAsOf"], "candidate.dataAsOf") < _day(
            previous["data_as_of"], "previous.data_as_of"
        ):
            raise NarcoScopeBridgeError("candidate dataAsOf regresses behind current pin")
        superseded = [dict(item) for item in previous_receipt["superseded"]]
        if previous["sha256"] != digest:
            superseded.append({
                "data_as_of": previous["data_as_of"],
                "sha256": previous["sha256"],
                "admitted_at": previous["admitted_at"],
                "superseded_at": timestamp,
            })
        elif candidate["dataAsOf"] != previous["data_as_of"]:
            raise NarcoScopeBridgeError("identical bytes cannot declare a different dataAsOf")
        else:
            # An idempotent check is not a new admission. Preserve the original
            # clock and receipt bytes instead of manufacturing revision churn.
            return dict(previous_receipt)
    if len(superseded) > MAX_HISTORY:
        raise NarcoScopeBridgeError("pin history is full; archive before admitting another revision")
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "producer": "narcoscope",
        "source_url": CANONICAL_URL,
        "artifact_id": ARTIFACT_ID,
        "current": {
            "data_as_of": candidate["dataAsOf"],
            "sha256": digest,
            "admitted_at": timestamp,
        },
        "superseded": superseded,
    }
    validate_receipt(receipt, artifact=candidate_bytes)
    return receipt


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NarcoScopeBridgeError("value is not finite canonical JSON") from exc


__all__ = [
    "ARTIFACT_ID", "CANONICAL_URL", "DEFAULT_ARTIFACT_PATH",
    "DEFAULT_RECEIPT_PATH", "NarcoScopeBridgeError", "admission_receipt",
    "artifact_sha256", "canonical_json_bytes", "load_artifact", "load_receipt",
    "strict_json_loads", "validate_artifact", "validate_receipt",
]
