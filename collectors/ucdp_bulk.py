"""Rights-gated UCDP 26.1 bulk acquisition and aggregate-only parser.

Only three reviewed, versioned archives are accepted. Exact ZIP bytes remain
private evidence. Public output is reduced to annual conflict identifiers and
country-year uncertainty bounds before it crosses the contract boundary.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time as time_module
import urllib.parse
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from core.governance import KillSwitch, RateCeiling
from core.safe_fetch import FetchError, SafeFetchResponse, safe_fetch_response
from core.ucdp_aggregate import (
    CountryYearAggregate,
    ConflictYearAggregate,
    UCDPAggregateBundle,
    UCDPAggregateError,
    UncertaintyBounds,
    canonical_json_bytes,
    parse_timestamp,
    sha256_bytes,
)

REGISTRY_SCHEMA_VERSION = "palimpsest.ucdp-bulk-registry.v1"
ACQUISITION_SCHEMA_VERSION = "palimpsest.ucdp-bulk-acquisition-receipt.v1"
DATASET_VERSION = "26.1"
UCDP_HOST = "ucdp.uu.se"
MAX_REGISTRY_BYTES = 128 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
MAX_SOURCE_ROWS = 100_000
MAX_ZIP_COMPRESSION_RATIO = 100
USER_AGENT = (
    "palimpsest.info UCDP annual aggregate collector "
    "(historical non-tactical research; contact desk@palimpsest.info)"
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTOR_IDS = re.compile(r"^\d+(?:,\s*\d+)*$")

ARMED_CONFLICT_HEADER = (
    "conflict_id",
    "location",
    "side_a",
    "side_a_id",
    "side_a_2nd",
    "side_b",
    "side_b_id",
    "side_b_2nd",
    "incompatibility",
    "territory_name",
    "year",
    "intensity_level",
    "cumulative_intensity",
    "type_of_conflict",
    "start_date",
    "start_prec",
    "start_date2",
    "start_prec2",
    "ep_end",
    "ep_end_date",
    "ep_end_prec",
    "gwno_a",
    "gwno_a_2nd",
    "gwno_b",
    "gwno_b_2nd",
    "gwno_loc",
    "region",
    "version",
)

ACTOR_HEADER = (
    "ActorId",
    "NameData",
    "NameOrig",
    "NameOrigFull",
    "NameOrigFullEng",
    "NameChange",
    "NewName",
    "NewNameFullMotherTongue",
    "NewNameFullEng",
    "Org",
    "ConflictId",
    "DyadId",
    "PrimaryParty",
    "OSID",
    "OSCoalition",
    "OSCoalitionID",
    "NSID",
    "NSCoalition",
    "NSCoalitionID",
    "Splinter",
    "NamePrev",
    "ActorIdPrev",
    "SplitTemp",
    "NameSplitTemp",
    "ActorIdSplitTemp",
    "Alliance",
    "NameAlliance",
    "ActorIdAlliance",
    "JoinGroup",
    "GroupName",
    "ActorIdGroup",
    "Location",
    "GWNOLoc",
    "Region",
    "Version",
)

COUNTRY_YEAR_HEADER = (
    "country",
    "country_id",
    "year",
    "region",
    "govt_name",
    "sb_exist",
    "sb_dyad_count",
    "sb_dyad_ids",
    "sb_dyad_names",
    "sb_deaths_parties",
    "sb_deaths_civilians",
    "sb_deaths_unknown",
    "sb_total_deaths_best",
    "sb_total_deaths_high",
    "sb_total_deaths_low",
    "sb_intrastate_exist",
    "sb_intrastate_dyad_count",
    "sb_intrastate_dyad_ids",
    "sb_intrastate_dyad_names",
    "sb_intrastate_govt_inv_incomp",
    "sb_intrastate_deaths_parties",
    "sb_intrastate_deaths_civilians",
    "sb_intrastate_deaths_unknown",
    "sb_intrastate_deaths_best",
    "sb_intrastate_deaths_high",
    "sb_intrastate_deaths_low",
    "sb_interstate_exist",
    "sb_interstate_dyad_count",
    "sb_interstate_dyad_ids",
    "sb_interstate_dyad_names",
    "sb_interstate_govt_inv_incomp",
    "sb_interstate_deaths_parties",
    "sb_interstate_deaths_civilians",
    "sb_interstate_deaths_unknown",
    "sb_interstate_deaths_best",
    "sb_interstate_deaths_high",
    "sb_interstate_deaths_low",
    "ns_exist",
    "ns_dyad_count",
    "ns_dyad_ids",
    "ns_dyad_names",
    "ns_deaths_parties",
    "ns_deaths_civilians",
    "ns_deaths_unknown",
    "ns_total_deaths_best",
    "ns_total_deaths_high",
    "ns_total_deaths_low",
    "os_exist",
    "os_dyad_count",
    "os_dyad_ids",
    "os_dyad_names",
    "os_govt_inv",
    "os_govt_killings_best",
    "os_govt_killings_high",
    "os_govt_killings_low",
    "os_any_govt_inv",
    "os_any_govt_killings_best",
    "os_any_govt_killings_high",
    "os_any_govt_killings_low",
    "os_nsgroup_inv",
    "os_nsgroup_killings_best",
    "os_nsgroup_killings_high",
    "os_nsgroup_killings_low",
    "os_killings_unknown",
    "os_total_deaths_best",
    "os_total_deaths_high",
    "os_total_deaths_low",
    "cumulative_total_deaths_parties_in_orgvio",
    "cumulative_total_deaths_civilians_in_orgvio",
    "cumulative_total_deaths_unknown_in_orgvio",
    "cumulative_total_deaths_in_orgvio_best",
    "cumulative_total_deaths_in_orgvio_high",
    "cumulative_total_deaths_in_orgvio_low",
    "Version",
)

EXPECTED_HEADERS = {
    "armed_conflict": ARMED_CONFLICT_HEADER,
    "actor_registry": ACTOR_HEADER,
    "organized_country_year": COUNTRY_YEAR_HEADER,
}


class UCDPBulkError(UCDPAggregateError):
    """Registry, transport, archive, or CSV input was refused."""


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _utc(value: object, *, label: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{label} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise UCDPBulkError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _text(
    value: object,
    *,
    label: str,
    maximum_bytes: int = 4096,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or (not allow_empty and not value.strip()):
        raise UCDPBulkError(f"{label} must be bounded text")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise UCDPBulkError(f"{label} exceeds {maximum_bytes} UTF-8 bytes")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise UCDPBulkError(f"{label} contains control characters")
    return value


def _integer(
    value: object,
    *,
    label: str,
    minimum: int = 0,
    maximum: int = 2_147_483_647,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise UCDPBulkError(
            f"{label} must be an integer from {minimum} through {maximum}"
        )
    return value


def _source_integer(
    value: object,
    *,
    label: str,
    minimum: int = 0,
    maximum: int = 2_147_483_647,
) -> int:
    if type(value) is not str or not value.isascii() or not value.isdecimal():
        raise UCDPBulkError(f"{label} must be a source integer")
    return _integer(int(value), label=label, minimum=minimum, maximum=maximum)


def _digest(value: object, *, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise UCDPBulkError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _https_url(value: object, *, label: str, exact: str | None = None) -> str:
    url = _text(value, label=label, maximum_bytes=16 * 1024)
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UCDPBulkError(f"{label} is not a valid URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise UCDPBulkError(f"{label} must be a credential-free HTTPS URL")
    if exact is not None and url != exact:
        raise UCDPBulkError(f"{label} changed from the reviewed URL")
    return url


def _strict_json_loads(raw: bytes, *, label: str, maximum_bytes: int) -> Any:
    if type(raw) is not bytes or not raw or len(raw) > maximum_bytes:
        raise UCDPBulkError(f"{label} is empty or exceeds {maximum_bytes} bytes")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise UCDPBulkError(f"{label} is not strict UTF-8") from exc

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise UCDPBulkError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise UCDPBulkError(f"{label} contains non-finite JSON number {value}")

    try:
        return json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise UCDPBulkError(f"{label} is not valid JSON") from exc


@dataclass(frozen=True, slots=True)
class UCDPInput:
    input_id: str
    url: str
    member_name: str
    encoding: str
    maximum_archive_bytes: int
    maximum_member_bytes: int

    def __post_init__(self) -> None:
        if self.input_id not in EXPECTED_HEADERS:
            raise UCDPBulkError("input_id is outside the reviewed UCDP set")
        _https_url(self.url, label=f"{self.input_id} URL")
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.hostname != UCDP_HOST or parsed.query:
            raise UCDPBulkError("UCDP input must use the fixed keyless download host")
        _text(self.member_name, label=f"{self.input_id} member_name", maximum_bytes=256)
        if "/" in self.member_name or "\\" in self.member_name:
            raise UCDPBulkError("ZIP member_name must be a flat filename")
        expected_encoding = (
            "latin-1" if self.input_id == "actor_registry" else "utf-8-sig"
        )
        if self.encoding != expected_encoding:
            raise UCDPBulkError(f"{self.input_id} encoding must be {expected_encoding}")
        _integer(
            self.maximum_archive_bytes,
            label="maximum_archive_bytes",
            minimum=1,
            maximum=16 * 1024 * 1024,
        )
        _integer(
            self.maximum_member_bytes,
            label="maximum_member_bytes",
            minimum=1,
            maximum=32 * 1024 * 1024,
        )


@dataclass(frozen=True, slots=True)
class CountryYearBinding:
    country_code: str
    source_country: str
    source_country_id: int


@dataclass(frozen=True, slots=True)
class UCDPRegistry:
    source: Mapping[str, object]
    scope: Mapping[str, object]
    country_year_bindings: Mapping[str, CountryYearBinding]
    inputs: Mapping[str, UCDPInput]
    raw_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, Mapping) or not isinstance(self.scope, Mapping):
            raise TypeError("registry source and scope must be mappings")
        if not isinstance(self.inputs, Mapping) or set(self.inputs) != set(
            EXPECTED_HEADERS
        ):
            raise UCDPBulkError("registry input coverage is incomplete")
        if not isinstance(self.country_year_bindings, Mapping) or set(
            self.country_year_bindings
        ) != {"PAK", "MMR"}:
            raise UCDPBulkError("country-year bindings are incomplete")
        _digest(self.raw_sha256, label="registry raw_sha256")
        object.__setattr__(self, "source", MappingProxyType(dict(self.source)))
        object.__setattr__(self, "scope", MappingProxyType(dict(self.scope)))
        object.__setattr__(
            self,
            "inputs",
            MappingProxyType(dict(self.inputs)),
        )
        object.__setattr__(
            self,
            "country_year_bindings",
            MappingProxyType(dict(self.country_year_bindings)),
        )


def load_registry(path: str | Path) -> UCDPRegistry:
    raw = Path(path).read_bytes()
    value = _strict_json_loads(
        raw,
        label="UCDP registry",
        maximum_bytes=MAX_REGISTRY_BYTES,
    )
    if type(value) is not dict or set(value) != {
        "schema_version",
        "source",
        "scope",
        "inputs",
    }:
        raise UCDPBulkError("UCDP registry fields changed")
    if value["schema_version"] != REGISTRY_SCHEMA_VERSION:
        raise UCDPBulkError(f"registry must use {REGISTRY_SCHEMA_VERSION}")

    source = value["source"]
    source_fields = {
        "source_id",
        "name",
        "publisher",
        "dataset_version",
        "catalog_url",
        "license",
        "license_url",
        "rights_evidence_url",
        "redistribution_status",
        "attribution",
        "citation_url",
        "source_period_start_year",
        "source_period_end_year",
        "release_cadence",
        "maximum_source_age_days",
    }
    if type(source) is not dict or set(source) != source_fields:
        raise UCDPBulkError("registry source fields changed")
    expected_source = {
        "source_id": "ucdp_bulk_26_1",
        "name": "Uppsala Conflict Data Program annual bulk datasets",
        "publisher": "Uppsala Conflict Data Program, Uppsala University",
        "dataset_version": DATASET_VERSION,
        "catalog_url": "https://ucdp.uu.se/downloads/",
        "license": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "rights_evidence_url": "https://ucdp.uu.se/downloads/",
        "redistribution_status": "allowed_with_attribution",
        "attribution": "Uppsala Conflict Data Program (UCDP), version 26.1",
        "citation_url": "https://doi.org/10.1093/jopres/xjag046",
        "source_period_start_year": 1946,
        "source_period_end_year": 2025,
        "release_cadence": "annual",
        "maximum_source_age_days": 550,
    }
    if source != expected_source:
        raise UCDPBulkError("registry source rights, clocks, or attribution changed")
    for key in (
        "catalog_url",
        "license_url",
        "rights_evidence_url",
        "citation_url",
    ):
        _https_url(source[key], label=f"source.{key}")

    scope = value["scope"]
    scope_fields = {
        "geographies",
        "pakistan_location",
        "balochistan_territory",
        "myanmar_location_token",
        "myanmar_territory_allowlist",
        "country_year_bindings",
    }
    if type(scope) is not dict or set(scope) != scope_fields:
        raise UCDPBulkError("registry scope fields changed")
    if scope["geographies"] != ["PAK-BAL", "MMR"]:
        raise UCDPBulkError("registry geographies changed")
    fixed_scope = {
        "pakistan_location": "Pakistan",
        "balochistan_territory": "Balochistan",
        "myanmar_location_token": "Myanmar (Burma)",
    }
    for key, expected in fixed_scope.items():
        if scope[key] != expected:
            raise UCDPBulkError(f"registry {key} changed")
    expected_myanmar_territories = [
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
    if scope["myanmar_territory_allowlist"] != expected_myanmar_territories:
        raise UCDPBulkError("registry Myanmar territory allowlist changed")
    binding_rows = scope["country_year_bindings"]
    if type(binding_rows) is not list or len(binding_rows) != 2:
        raise UCDPBulkError("registry requires two country-year bindings")
    expected_bindings = {
        "PAK": ("Pakistan", 770),
        "MMR": ("Myanmar (Burma)", 775),
    }
    bindings: dict[str, CountryYearBinding] = {}
    for position, row in enumerate(binding_rows, 1):
        if type(row) is not dict or set(row) != {
            "country_code",
            "source_country",
            "source_country_id",
        }:
            raise UCDPBulkError(f"country-year binding {position} fields changed")
        country_code = row["country_code"]
        if country_code not in expected_bindings or country_code in bindings:
            raise UCDPBulkError(f"country-year binding {position} is invalid")
        source_country, source_country_id = expected_bindings[country_code]
        if (
            row["source_country"] != source_country
            or row["source_country_id"] != source_country_id
        ):
            raise UCDPBulkError(f"country-year binding {country_code} changed")
        bindings[country_code] = CountryYearBinding(
            country_code=country_code,
            source_country=source_country,
            source_country_id=source_country_id,
        )

    input_rows = value["inputs"]
    if type(input_rows) is not list or len(input_rows) != 3:
        raise UCDPBulkError("registry requires exactly three inputs")
    expected_inputs = {
        "armed_conflict": (
            "https://ucdp.uu.se/downloads/ucdpprio/ucdp-prio-acd-261-csv.zip",
            "UcdpPrioConflict_v26_1.csv",
            "utf-8-sig",
            1_048_576,
            2_097_152,
        ),
        "actor_registry": (
            "https://ucdp.uu.se/downloads/actor/ucdp-actor-261-csv.zip",
            "Actor_v26_1.csv",
            "latin-1",
            1_048_576,
            2_097_152,
        ),
        "organized_country_year": (
            "https://ucdp.uu.se/downloads/organizedviolencecy/organizedviolencecy-261-csv.zip",
            "OrganizedViolenceCYDataSet26_1.csv",
            "utf-8-sig",
            4_194_304,
            8_388_608,
        ),
    }
    inputs: dict[str, UCDPInput] = {}
    for position, row in enumerate(input_rows, 1):
        if type(row) is not dict or set(row) != {
            "input_id",
            "url",
            "member_name",
            "encoding",
            "maximum_archive_bytes",
            "maximum_member_bytes",
        }:
            raise UCDPBulkError(f"input {position} fields changed")
        input_id = row["input_id"]
        if input_id not in expected_inputs or input_id in inputs:
            raise UCDPBulkError(f"input {position} is invalid or duplicated")
        expected = expected_inputs[input_id]
        actual = (
            row["url"],
            row["member_name"],
            row["encoding"],
            row["maximum_archive_bytes"],
            row["maximum_member_bytes"],
        )
        if actual != expected:
            raise UCDPBulkError(f"input {input_id} changed from reviewed bounds")
        inputs[input_id] = UCDPInput(**row)
    return UCDPRegistry(
        source=dict(source),
        scope={
            key: value for key, value in scope.items() if key != "country_year_bindings"
        },
        country_year_bindings=bindings,
        inputs=inputs,
        raw_sha256=sha256_bytes(raw),
    )


@dataclass(frozen=True, slots=True)
class ExtractedMember:
    raw: bytes
    sha256: str


def extract_member(archive: bytes, spec: UCDPInput) -> ExtractedMember:
    """Extract one exact flat ZIP member through decompressed-byte guards."""

    if (
        type(archive) is not bytes
        or not archive
        or len(archive) > spec.maximum_archive_bytes
    ):
        raise UCDPBulkError(
            f"{spec.input_id} archive is empty or exceeds its byte bound"
        )
    try:
        with zipfile.ZipFile(io.BytesIO(archive), "r") as bundle:
            members = bundle.infolist()
            if len(members) != 1:
                raise UCDPBulkError(
                    f"{spec.input_id} archive must contain exactly one member"
                )
            info = members[0]
            if info.filename != spec.member_name or info.is_dir():
                raise UCDPBulkError(f"{spec.input_id} ZIP member changed")
            if info.flag_bits & 0x1:
                raise UCDPBulkError(f"{spec.input_id} ZIP member must not be encrypted")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise UCDPBulkError(f"{spec.input_id} ZIP compression is unsupported")
            if not 0 < info.file_size <= spec.maximum_member_bytes:
                raise UCDPBulkError(f"{spec.input_id} member exceeds its byte bound")
            if info.compress_size <= 0:
                raise UCDPBulkError(
                    f"{spec.input_id} member has invalid compressed size"
                )
            if info.file_size > info.compress_size * MAX_ZIP_COMPRESSION_RATIO:
                raise UCDPBulkError(f"{spec.input_id} ZIP compression ratio is unsafe")
            with bundle.open(info, "r") as handle:
                raw = handle.read(spec.maximum_member_bytes + 1)
            if len(raw) != info.file_size or len(raw) > spec.maximum_member_bytes:
                raise UCDPBulkError(f"{spec.input_id} member length changed")
            if bundle.testzip() is not None:
                raise UCDPBulkError(f"{spec.input_id} ZIP CRC validation failed")
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise UCDPBulkError(f"{spec.input_id} is not a valid bounded ZIP") from exc
    return ExtractedMember(raw=raw, sha256=sha256_bytes(raw))


@dataclass(frozen=True, slots=True)
class UCDPAcquisitionReceipt:
    input_id: str
    source_url: str
    archive_sha256: str
    archive_bytes: int
    member_name: str
    member_sha256: str
    member_bytes: int
    http_last_modified: datetime
    retrieved_at: datetime
    maximum_archive_bytes: int
    maximum_member_bytes: int
    maximum_source_age_days: int
    schema_version: str = ACQUISITION_SCHEMA_VERSION
    dataset_version: str = DATASET_VERSION
    request_method: str = "GET"
    request_user_agent: str = USER_AGENT
    redirect_policy: str = "disabled"
    tls_verification: str = "required"

    def __post_init__(self) -> None:
        if self.schema_version != ACQUISITION_SCHEMA_VERSION:
            raise UCDPBulkError("acquisition schema_version changed")
        if self.input_id not in EXPECTED_HEADERS:
            raise UCDPBulkError("acquisition input_id is outside scope")
        _https_url(self.source_url, label="acquisition source_url")
        _digest(self.archive_sha256, label="archive_sha256")
        _digest(self.member_sha256, label="member_sha256")
        _integer(
            self.archive_bytes,
            label="archive_bytes",
            minimum=1,
            maximum=self.maximum_archive_bytes,
        )
        _integer(
            self.member_bytes,
            label="member_bytes",
            minimum=1,
            maximum=self.maximum_member_bytes,
        )
        _text(self.member_name, label="member_name", maximum_bytes=256)
        modified = _utc(self.http_last_modified, label="http_last_modified")
        retrieved = _utc(self.retrieved_at, label="retrieved_at")
        if modified > retrieved:
            raise UCDPBulkError("HTTP Last-Modified is later than retrieval")
        if retrieved - modified > timedelta(days=self.maximum_source_age_days):
            raise UCDPBulkError("UCDP archive is stale under the annual source policy")
        if self.dataset_version != DATASET_VERSION:
            raise UCDPBulkError("acquisition dataset_version changed")
        fixed = {
            "request_method": (self.request_method, "GET"),
            "request_user_agent": (self.request_user_agent, USER_AGENT),
            "redirect_policy": (self.redirect_policy, "disabled"),
            "tls_verification": (self.tls_verification, "required"),
        }
        for label, (actual, expected) in fixed.items():
            if actual != expected:
                raise UCDPBulkError(f"acquisition {label} must be {expected!r}")
        object.__setattr__(self, "http_last_modified", modified)
        object.__setattr__(self, "retrieved_at", retrieved)

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "input_id": self.input_id,
            "dataset_version": self.dataset_version,
            "source_url": self.source_url,
            "request_method": self.request_method,
            "request_user_agent": self.request_user_agent,
            "redirect_policy": self.redirect_policy,
            "tls_verification": self.tls_verification,
            "maximum_archive_bytes": self.maximum_archive_bytes,
            "maximum_member_bytes": self.maximum_member_bytes,
            "maximum_source_age_days": self.maximum_source_age_days,
            "archive_sha256": self.archive_sha256,
            "archive_bytes": self.archive_bytes,
            "member_name": self.member_name,
            "member_sha256": self.member_sha256,
            "member_bytes": self.member_bytes,
            "http_last_modified": _timestamp(self.http_last_modified),
            "retrieved_at": _timestamp(self.retrieved_at),
        }

    @property
    def acquisition_id(self) -> str:
        return sha256_bytes(canonical_json_bytes(self._payload()))

    def to_dict(self) -> dict[str, object]:
        payload = self._payload()
        payload["acquisition_id"] = self.acquisition_id
        return payload

    @classmethod
    def from_bytes(cls, raw: bytes) -> "UCDPAcquisitionReceipt":
        value = _strict_json_loads(
            raw,
            label="UCDP acquisition receipt",
            maximum_bytes=MAX_RECEIPT_BYTES,
        )
        expected = {
            "schema_version",
            "acquisition_id",
            "input_id",
            "dataset_version",
            "source_url",
            "request_method",
            "request_user_agent",
            "redirect_policy",
            "tls_verification",
            "maximum_archive_bytes",
            "maximum_member_bytes",
            "maximum_source_age_days",
            "archive_sha256",
            "archive_bytes",
            "member_name",
            "member_sha256",
            "member_bytes",
            "http_last_modified",
            "retrieved_at",
        }
        if type(value) is not dict or set(value) != expected:
            raise UCDPBulkError("acquisition receipt fields changed")
        if canonical_json_bytes(value) != raw:
            raise UCDPBulkError("acquisition receipt must use canonical JSON bytes")
        acquisition_id = value.pop("acquisition_id")
        _digest(acquisition_id, label="acquisition_id")
        value["http_last_modified"] = parse_timestamp(
            value["http_last_modified"], label="http_last_modified"
        )
        value["retrieved_at"] = parse_timestamp(
            value["retrieved_at"], label="retrieved_at"
        )
        receipt = cls(**value)
        if receipt.acquisition_id != acquisition_id:
            raise UCDPBulkError("acquisition_id does not authenticate the receipt")
        return receipt


def receipt_for(
    archive: bytes,
    *,
    spec: UCDPInput,
    http_last_modified: datetime,
    retrieved_at: datetime,
    maximum_source_age_days: int,
) -> UCDPAcquisitionReceipt:
    member = extract_member(archive, spec)
    return UCDPAcquisitionReceipt(
        input_id=spec.input_id,
        source_url=spec.url,
        archive_sha256=sha256_bytes(archive),
        archive_bytes=len(archive),
        member_name=spec.member_name,
        member_sha256=member.sha256,
        member_bytes=len(member.raw),
        http_last_modified=http_last_modified,
        retrieved_at=retrieved_at,
        maximum_archive_bytes=spec.maximum_archive_bytes,
        maximum_member_bytes=spec.maximum_member_bytes,
        maximum_source_age_days=maximum_source_age_days,
    )


def verify_acquisition_receipt(
    raw_receipt: bytes,
    *,
    archive: bytes,
    spec: UCDPInput,
    maximum_source_age_days: int,
) -> UCDPAcquisitionReceipt:
    receipt = UCDPAcquisitionReceipt.from_bytes(raw_receipt)
    if (
        receipt.input_id != spec.input_id
        or receipt.source_url != spec.url
        or receipt.member_name != spec.member_name
        or receipt.maximum_archive_bytes != spec.maximum_archive_bytes
        or receipt.maximum_member_bytes != spec.maximum_member_bytes
        or receipt.maximum_source_age_days != maximum_source_age_days
    ):
        raise UCDPBulkError("acquisition receipt does not match the reviewed input")
    member = extract_member(archive, spec)
    if receipt.archive_bytes != len(archive) or receipt.archive_sha256 != sha256_bytes(
        archive
    ):
        raise UCDPBulkError("acquisition receipt does not match exact ZIP bytes")
    if (
        receipt.member_bytes != len(member.raw)
        or receipt.member_sha256 != member.sha256
    ):
        raise UCDPBulkError("acquisition receipt does not match exact member bytes")
    return receipt


def _header(headers: Mapping[str, str], name: str) -> str | None:
    matches = [
        value for key, value in headers.items() if key.casefold() == name.casefold()
    ]
    if len(matches) > 1:
        raise UCDPBulkError(f"response contains duplicate {name} headers")
    return matches[0] if matches else None


def _http_last_modified(
    headers: Mapping[str, str], *, retrieved_at: datetime
) -> datetime:
    value = _header(headers, "Last-Modified")
    if value is None:
        raise UCDPBulkError("UCDP response omitted Last-Modified")
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise UCDPBulkError("UCDP Last-Modified header is invalid") from exc
    modified = _utc(parsed, label="HTTP Last-Modified")
    if modified > retrieved_at:
        raise UCDPBulkError("UCDP Last-Modified is later than retrieval")
    return modified


def _url_policy(expected: str) -> Callable[[str], None]:
    def validate(candidate: str) -> None:
        if candidate != expected:
            raise FetchError("UCDP request URL changed from the reviewed versioned URL")

    return validate


@dataclass(frozen=True, slots=True)
class FetchedArchive:
    archive: bytes
    receipt: UCDPAcquisitionReceipt


def fetch_archive(
    spec: UCDPInput,
    *,
    maximum_source_age_days: int,
    clock: Callable[[], datetime],
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
    timeout: float = 45.0,
    retries: int = 2,
    fetcher: Callable[..., SafeFetchResponse] = safe_fetch_response,
) -> FetchedArchive:
    """Fetch one versioned ZIP with TLS, host, redirect, time, and byte bounds."""

    if not callable(clock):
        raise UCDPBulkError("retrieval clock must be callable")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise UCDPBulkError("retries must be a non-negative integer")
    kill = kill_switch or KillSwitch()
    ceiling = rate_ceiling or RateCeiling(rate=0.2, capacity=1.0)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        kill.require_live()
        ceiling.acquire()
        try:
            response = fetcher(
                spec.url,
                max_bytes=spec.maximum_archive_bytes,
                timeout=timeout,
                max_redirects=0,
                headers={"User-Agent": USER_AGENT, "Accept": "application/zip"},
                url_policy=_url_policy(spec.url),
            )
            if not isinstance(response, SafeFetchResponse):
                raise UCDPBulkError("fetcher must return SafeFetchResponse")
            if response.status != 200 or response.url != spec.url:
                raise UCDPBulkError(
                    "UCDP fetch did not return the exact reviewed 200 URL"
                )
            archive = response.body
            if (
                type(archive) is not bytes
                or not archive
                or len(archive) > spec.maximum_archive_bytes
            ):
                raise UCDPBulkError("UCDP fetch returned invalid bounded bytes")
            retrieved_at = _utc(clock(), label="retrieval clock")
            modified = _http_last_modified(response.headers, retrieved_at=retrieved_at)
            receipt = receipt_for(
                archive,
                spec=spec,
                http_last_modified=modified,
                retrieved_at=retrieved_at,
                maximum_source_age_days=maximum_source_age_days,
            )
            return FetchedArchive(archive=archive, receipt=receipt)
        except (FetchError, OSError, UCDPBulkError, zipfile.BadZipFile) as exc:
            last_error = exc
            if attempt < retries:
                time_module.sleep(float(attempt + 1))
    raise UCDPBulkError(
        f"{spec.input_id} fetch failed after {retries + 1} attempts: {last_error}"
    )


def _csv_rows(member: bytes, *, spec: UCDPInput) -> list[dict[str, str]]:
    try:
        text = member.decode(spec.encoding, "strict")
    except UnicodeDecodeError as exc:
        raise UCDPBulkError(
            f"{spec.input_id} member is not strict {spec.encoding}"
        ) from exc
    if "\x00" in text:
        raise UCDPBulkError(f"{spec.input_id} member contains NUL")
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    if tuple(reader.fieldnames or ()) != EXPECTED_HEADERS[spec.input_id]:
        raise UCDPBulkError(f"{spec.input_id} CSV header changed")
    rows: list[dict[str, str]] = []
    blank_trailer_started = False
    try:
        for position, row in enumerate(reader, 1):
            if position > MAX_SOURCE_ROWS:
                raise UCDPBulkError(f"{spec.input_id} exceeds {MAX_SOURCE_ROWS} rows")
            if None in row or set(row) != set(EXPECTED_HEADERS[spec.input_id]):
                raise UCDPBulkError(f"{spec.input_id} row {position} changed shape")
            if any(type(value) is not str for value in row.values()):
                raise UCDPBulkError(f"{spec.input_id} row {position} contains null")
            if not any(row.values()):
                blank_trailer_started = True
                continue
            if blank_trailer_started:
                raise UCDPBulkError(
                    f"{spec.input_id} contains data after an empty trailer row"
                )
            rows.append(dict(row))
    except csv.Error as exc:
        raise UCDPBulkError(f"{spec.input_id} CSV is malformed") from exc
    if not rows:
        raise UCDPBulkError(f"{spec.input_id} CSV is empty")
    return rows


def _actor_id_list(value: object, *, label: str) -> tuple[int, ...]:
    if type(value) is not str or not _ACTOR_IDS.fullmatch(value):
        raise UCDPBulkError(f"{label} is not a source actor-ID list")
    ids = tuple(
        sorted(
            {
                _source_integer(item.strip(), label=label, minimum=1)
                for item in value.split(",")
            }
        )
    )
    if not ids:
        raise UCDPBulkError(f"{label} cannot be empty")
    return ids


def _row_sha256(row: Mapping[str, str]) -> str:
    return sha256_bytes(canonical_json_bytes(dict(row)))


def _parse_actor_registry(
    rows: list[dict[str, str]],
) -> tuple[frozenset[int], str]:
    actor_ids: set[int] = set()
    for position, row in enumerate(rows, 1):
        if row["Version"] != DATASET_VERSION:
            raise UCDPBulkError(f"actor row {position} version changed")
        actor_id = _source_integer(
            row["ActorId"], label=f"actor row {position} ActorId", minimum=1
        )
        if actor_id in actor_ids:
            raise UCDPBulkError(f"actor ID {actor_id} is duplicated")
        actor_ids.add(actor_id)
    ordered = sorted(actor_ids)
    return frozenset(actor_ids), sha256_bytes(canonical_json_bytes(ordered))


def _location_tokens(value: str) -> frozenset[str]:
    tokens = [
        _text(part.strip(), label="location token", maximum_bytes=128)
        for part in value.split(",")
    ]
    return frozenset(tokens)


def _parse_conflicts(
    rows: list[dict[str, str]],
    *,
    registry: UCDPRegistry,
    actor_ids: frozenset[int],
    armed_receipt: UCDPAcquisitionReceipt,
    actor_receipt: UCDPAcquisitionReceipt,
) -> tuple[ConflictYearAggregate, ...]:
    records: list[ConflictYearAggregate] = []
    seen: set[tuple[str, int, int]] = set()
    maximum_year = 0
    for position, row in enumerate(rows, 1):
        if row["version"] != DATASET_VERSION:
            raise UCDPBulkError(f"armed-conflict row {position} version changed")
        year = _source_integer(
            row["year"], label=f"armed-conflict row {position} year", minimum=1946
        )
        maximum_year = max(maximum_year, year)
        geography: str | None = None
        country_code: str | None = None
        if (
            row["location"] == registry.scope["pakistan_location"]
            and row["territory_name"] == registry.scope["balochistan_territory"]
        ):
            geography = "PAK-BAL"
            country_code = "PAK"
        elif registry.scope["myanmar_location_token"] in _location_tokens(
            row["location"]
        ):
            geography = "MMR"
            country_code = "MMR"
        if geography is None or country_code is None:
            continue
        conflict_id = _source_integer(
            row["conflict_id"],
            label=f"armed-conflict row {position} conflict_id",
            minimum=1,
        )
        side_a = _actor_id_list(
            row["side_a_id"], label=f"armed-conflict row {position} side_a_id"
        )
        side_b = _actor_id_list(
            row["side_b_id"], label=f"armed-conflict row {position} side_b_id"
        )
        missing = (set(side_a) | set(side_b)) - actor_ids
        if missing:
            raise UCDPBulkError(
                f"armed-conflict row {position} references unknown actor IDs"
            )
        key = (geography, conflict_id, year)
        if key in seen:
            raise UCDPBulkError(f"duplicate target conflict-year {key}")
        seen.add(key)
        territory = row["territory_name"] or None
        if (
            geography == "MMR"
            and territory is not None
            and territory not in registry.scope["myanmar_territory_allowlist"]
        ):
            raise UCDPBulkError(
                f"armed-conflict row {position} has an unreviewed Myanmar territory"
            )
        records.append(
            ConflictYearAggregate(
                geography_code=geography,
                country_code=country_code,
                territory_name=territory,
                year=year,
                conflict_id=conflict_id,
                side_a_actor_ids=side_a,
                side_b_actor_ids=side_b,
                source_row_sha256=_row_sha256(row),
                armed_conflict_acquisition_id=armed_receipt.acquisition_id,
                actor_registry_acquisition_id=actor_receipt.acquisition_id,
            )
        )
    expected_end = registry.source["source_period_end_year"]
    if maximum_year != expected_end:
        raise UCDPBulkError("armed-conflict source period end changed")
    if {record.geography_code for record in records} != {"PAK-BAL", "MMR"}:
        raise UCDPBulkError("target conflict coverage is incomplete")
    return tuple(
        sorted(
            records,
            key=lambda row: (
                row.geography_code,
                row.year,
                row.conflict_id,
                row.record_id,
            ),
        )
    )


def _bounds(
    row: Mapping[str, str],
    *,
    prefix: str,
    position: int,
) -> UncertaintyBounds:
    return UncertaintyBounds(
        low=_source_integer(
            row[f"{prefix}_low"],
            label=f"country-year row {position} {prefix}_low",
        ),
        best=_source_integer(
            row[f"{prefix}_best"],
            label=f"country-year row {position} {prefix}_best",
        ),
        high=_source_integer(
            row[f"{prefix}_high"],
            label=f"country-year row {position} {prefix}_high",
        ),
    )


def _parse_country_years(
    rows: list[dict[str, str]],
    *,
    registry: UCDPRegistry,
    receipt: UCDPAcquisitionReceipt,
) -> tuple[CountryYearAggregate, ...]:
    source_to_binding = {
        binding.source_country: binding
        for binding in registry.country_year_bindings.values()
    }
    records: list[CountryYearAggregate] = []
    seen: set[tuple[str, int]] = set()
    maximum_year = 0
    for position, row in enumerate(rows, 1):
        if row["Version"] != DATASET_VERSION:
            raise UCDPBulkError(f"country-year row {position} version changed")
        year = _source_integer(
            row["year"], label=f"country-year row {position} year", minimum=1989
        )
        maximum_year = max(maximum_year, year)
        binding = source_to_binding.get(row["country"])
        if binding is None:
            continue
        source_country_id = _source_integer(
            row["country_id"],
            label=f"country-year row {position} country_id",
            minimum=1,
        )
        if source_country_id != binding.source_country_id:
            raise UCDPBulkError(f"country-year row {position} country identity changed")
        state_based = _bounds(row, prefix="sb_total_deaths", position=position)
        non_state = _bounds(row, prefix="ns_total_deaths", position=position)
        one_sided = _bounds(row, prefix="os_total_deaths", position=position)
        total = UncertaintyBounds(
            low=state_based.low + non_state.low + one_sided.low,
            best=state_based.best + non_state.best + one_sided.best,
            high=state_based.high + non_state.high + one_sided.high,
        )
        key = (binding.country_code, year)
        if key in seen:
            raise UCDPBulkError(f"country-year identity {key} is duplicated")
        seen.add(key)
        records.append(
            CountryYearAggregate(
                country_code=binding.country_code,
                year=year,
                state_based=state_based,
                non_state=non_state,
                one_sided=one_sided,
                total=total,
                source_row_sha256=_row_sha256(row),
                country_year_acquisition_id=receipt.acquisition_id,
            )
        )
    expected_end = registry.source["source_period_end_year"]
    if maximum_year != expected_end:
        raise UCDPBulkError("country-year source period end changed")
    expected = {
        (country_code, year)
        for country_code in registry.country_year_bindings
        for year in range(1989, expected_end + 1)
    }
    if seen != expected:
        missing = sorted(expected - seen)
        raise UCDPBulkError(f"country-year target matrix is incomplete: {missing[:3]}")
    return tuple(sorted(records, key=lambda row: (row.country_code, row.year)))


def build_bundle(
    registry: UCDPRegistry,
    *,
    archives: Mapping[str, bytes],
    receipts: Mapping[str, UCDPAcquisitionReceipt],
) -> UCDPAggregateBundle:
    """Authenticate and reduce three exact UCDP archives to the public contract."""

    if set(archives) != set(registry.inputs) or set(receipts) != set(registry.inputs):
        raise UCDPBulkError("archives and receipts must cover all reviewed inputs")
    members: dict[str, bytes] = {}
    for input_id, spec in registry.inputs.items():
        archive = archives[input_id]
        receipt = receipts[input_id]
        if not isinstance(receipt, UCDPAcquisitionReceipt):
            raise TypeError("receipts must contain UCDPAcquisitionReceipt")
        member = extract_member(archive, spec)
        if (
            receipt.input_id != input_id
            or receipt.source_url != spec.url
            or receipt.member_name != spec.member_name
            or receipt.maximum_archive_bytes != spec.maximum_archive_bytes
            or receipt.maximum_member_bytes != spec.maximum_member_bytes
            or receipt.maximum_source_age_days
            != registry.source["maximum_source_age_days"]
            or receipt.archive_sha256 != sha256_bytes(archive)
            or receipt.archive_bytes != len(archive)
            or receipt.member_sha256 != member.sha256
            or receipt.member_bytes != len(member.raw)
        ):
            raise UCDPBulkError(f"{input_id} archive is not bound to its receipt")
        members[input_id] = member.raw
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
    ordered_receipts = tuple(
        receipts[input_id].to_dict()
        for input_id in (
            "armed_conflict",
            "actor_registry",
            "organized_country_year",
        )
    )
    generated_at = max(receipt.retrieved_at for receipt in receipts.values())
    source = {
        key: registry.source[key]
        for key in (
            "source_id",
            "name",
            "publisher",
            "dataset_version",
            "catalog_url",
            "license",
            "license_url",
            "rights_evidence_url",
            "redistribution_status",
            "attribution",
            "citation_url",
            "source_period_start_year",
            "source_period_end_year",
            "release_cadence",
        )
    }
    return UCDPAggregateBundle(
        generated_at=generated_at,
        registry_sha256=registry.raw_sha256,
        source=source,
        acquisition_receipts=ordered_receipts,
        actor_registry_ids_sha256=actor_ids_sha256,
        actor_registry_id_count=len(actor_ids),
        conflict_years=conflicts,
        country_years=country_years,
    )


__all__ = [
    "ACQUISITION_SCHEMA_VERSION",
    "ACTOR_HEADER",
    "ARMED_CONFLICT_HEADER",
    "COUNTRY_YEAR_HEADER",
    "DATASET_VERSION",
    "FetchedArchive",
    "MAX_RECEIPT_BYTES",
    "MAX_REGISTRY_BYTES",
    "REGISTRY_SCHEMA_VERSION",
    "UCDPAcquisitionReceipt",
    "UCDPBulkError",
    "UCDPInput",
    "UCDPRegistry",
    "USER_AGENT",
    "build_bundle",
    "extract_member",
    "fetch_archive",
    "load_registry",
    "receipt_for",
    "verify_acquisition_receipt",
]
