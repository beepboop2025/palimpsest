"""Private, structured Common Crawl evidence lake for reviewed financial institutions.

Common Crawl already holds monthly, outside-the-wall captures of public web pages.
This module turns reviewed URL Index exports into a local SQLite history without
contacting the original publisher hosts. The high-volume path is deliberately import
based: operators query the public Parquet URL Index with DuckDB or Athena, then this
code validates and ingests the resulting JSONL/CSV stream. The rate-limited CDX API
is reserved for a small exact-URL diagnostic path.

The public and training boundaries are conservative:

* only code-and-config allowlisted institutional hosts and exact aliases are accepted;
* every target routes explicitly to LiquiLens, Undertow, Seiche, and/or Palimpsest;
* full URLs remain in the private warehouse;
* feature exports contain aggregate metadata, never source bodies or URLs;
* a missing monthly capture is an archive coverage gap, never a deletion label;
* raw WARC records are fetched only by an explicit locator request and remain private;
* upstream rights are not inferred, so every initial target is metadata-only.

The analytical core is standard-library only. Network calls use ``core.safe_fetch``
and are injectable in tests.
"""

from __future__ import annotations

import csv
import fcntl
import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import statistics
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from core.governance import KillSwitch, RateCeiling
from core.safe_fetch import safe_fetch_bytes


UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "common_crawl_targets.json"
DEFAULT_INDEX_PLAN = ROOT / "config" / "common_crawl_index_plan.json"
DEFAULT_WAREHOUSE = ROOT / "data" / "common-crawl"
DEFAULT_DATABASE_NAME = "common-crawl.sqlite3"
LOCK_NAME = ".common-crawl.lock"

# The bulk export runs outside the application services, so its SQL must carry its
# own conservative resource envelope. DuckDB documents these settings as the
# controls for buffer-manager memory, worker concurrency, and bounded spill.
DUCKDB_MEMORY_LIMIT = "3GB"
DUCKDB_THREADS = 2
DUCKDB_MAX_TEMP_DIRECTORY_SIZE = "128GB"

DATABASE_SCHEMA_VERSION = 1
FEATURE_SCHEMA_VERSION = "palimpsest-common-crawl-features/v1"
SUMMARY_SCHEMA_VERSION = "palimpsest-common-crawl-summary/v1"
METHOD_VERSION = 1
MODEL_ID = "prequential-robust-mad/v1"

_CRAWL_RE = re.compile(r"^CC-MAIN-(\d{4})-(\d{2})$")
_TARGET_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_DIGEST_RE = re.compile(r"^[A-Z2-7]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9,_-]{0,128}$")
_MIME_RE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_TIMESTAMP14_RE = re.compile(r"^\d{14}$")
_TRUTHY = {"1", "true", "yes", "on"}

_SOURCE = {
    "collection_catalog_url": "https://index.commoncrawl.org/collinfo.json",
    "index_base_url": "https://index.commoncrawl.org/",
    "data_base_url": "https://data.commoncrawl.org/",
    "terms_url": "https://commoncrawl.org/terms-of-use",
}

# Config cannot silently widen collection to arbitrary hosts. A target addition requires a
# code review and a config change, and every source remains metadata-only. Canonical hosts
# are listed first; aliases cover publisher-owned bare/www or data subdomains without
# weakening the exact-host boundary.
_APPROVED_PRODUCTS = frozenset({"liquilens", "undertow", "seiche", "palimpsest"})
_APPROVED_TARGETS: dict[
    str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]
] = {
    # China policy and institutional evidence (Palimpsest plus product companions).
    "state-council": (
        ("www.gov.cn", "gov.cn"),
        ("government", "policy", "politics"),
        ("palimpsest",),
    ),
    "nbs": (
        ("www.stats.gov.cn", "stats.gov.cn"),
        ("economy", "government", "measurement"),
        ("palimpsest", "seiche"),
    ),
    "pbc": (
        ("www.pbc.gov.cn", "pbc.gov.cn"),
        ("economy", "funding", "government", "policy"),
        ("liquilens", "palimpsest", "seiche"),
    ),
    "safe": (
        ("www.safe.gov.cn", "safe.gov.cn"),
        ("economy", "foreign-exchange", "government", "policy"),
        ("palimpsest", "seiche", "undertow"),
    ),
    "ndrc": (
        ("www.ndrc.gov.cn", "ndrc.gov.cn"),
        ("economy", "government", "policy"),
        ("palimpsest",),
    ),
    "miit": (
        ("www.miit.gov.cn", "miit.gov.cn"),
        ("technology", "government", "policy"),
        ("palimpsest",),
    ),
    "cac": (
        ("www.cac.gov.cn", "cac.gov.cn"),
        ("censorship", "technology", "government", "policy"),
        ("palimpsest",),
    ),
    "csrc": (
        ("www.csrc.gov.cn", "csrc.gov.cn"),
        ("economy", "institutions", "markets", "policy"),
        ("liquilens", "palimpsest", "undertow"),
    ),
    "customs": (
        ("www.customs.gov.cn", "customs.gov.cn"),
        ("economy", "government", "measurement", "trade"),
        ("palimpsest", "seiche"),
    ),
    "mof": (
        ("www.mof.gov.cn", "mof.gov.cn"),
        ("economy", "fiscal", "government", "policy"),
        ("palimpsest", "seiche"),
    ),
    # United States funding, prudential, filing, and market evidence.
    "federal-reserve": (
        ("www.federalreserve.gov", "federalreserve.gov"),
        ("banking", "funding", "markets", "monetary-policy"),
        ("liquilens", "seiche", "undertow"),
    ),
    "new-york-fed": (
        ("www.newyorkfed.org", "newyorkfed.org", "markets.newyorkfed.org"),
        ("funding", "markets", "monetary-policy", "settlement"),
        ("seiche", "undertow"),
    ),
    "us-treasury": (
        ("home.treasury.gov", "treasury.gov", "www.treasury.gov"),
        ("fiscal", "funding", "government", "markets"),
        ("liquilens", "seiche", "undertow"),
    ),
    "us-fiscal-data": (
        ("fiscaldata.treasury.gov",),
        ("fiscal", "funding", "measurement"),
        ("seiche",),
    ),
    "sec": (
        ("www.sec.gov", "sec.gov", "data.sec.gov"),
        ("filings", "institutions", "markets", "securities"),
        ("liquilens", "undertow"),
    ),
    "fdic": (
        ("www.fdic.gov", "fdic.gov", "api.fdic.gov"),
        ("banking", "failures", "institutions", "prudential"),
        ("liquilens",),
    ),
    "occ": (
        ("www.occ.treas.gov", "occ.treas.gov", "www.occ.gov", "occ.gov"),
        ("banking", "institutions", "prudential"),
        ("liquilens",),
    ),
    "ffiec": (
        ("www.ffiec.gov", "ffiec.gov"),
        ("banking", "filings", "institutions", "prudential"),
        ("liquilens",),
    ),
    "ncua": (
        ("ncua.gov", "www.ncua.gov"),
        ("banking", "institutions", "prudential"),
        ("liquilens",),
    ),
    "office-financial-research": (
        ("www.financialresearch.gov", "financialresearch.gov"),
        ("funding", "institutions", "markets", "systemic-risk"),
        ("liquilens", "seiche", "undertow"),
    ),
    "cftc": (
        ("www.cftc.gov", "cftc.gov"),
        ("derivatives", "markets", "positioning"),
        ("undertow",),
    ),
    "finra": (
        ("www.finra.org", "finra.org"),
        ("institutions", "markets", "securities"),
        ("liquilens", "undertow"),
    ),
    # India institutional and market evidence.
    "rbi": (
        ("www.rbi.org.in", "rbi.org.in", "rbidocs.rbi.org.in"),
        ("banking", "funding", "monetary-policy", "prudential"),
        ("liquilens", "seiche"),
    ),
    "sebi": (
        ("www.sebi.gov.in", "sebi.gov.in"),
        ("institutions", "markets", "securities"),
        ("liquilens", "undertow"),
    ),
    "mca-india": (
        ("www.mca.gov.in", "mca.gov.in"),
        ("companies", "filings", "institutions"),
        ("liquilens",),
    ),
    # United Kingdom and Europe.
    "bank-of-england": (
        ("www.bankofengland.co.uk", "bankofengland.co.uk"),
        ("banking", "funding", "monetary-policy", "prudential"),
        ("liquilens", "seiche"),
    ),
    "fca": (
        ("www.fca.org.uk", "fca.org.uk"),
        ("institutions", "markets", "prudential"),
        ("liquilens", "undertow"),
    ),
    "ecb": (
        ("www.ecb.europa.eu", "ecb.europa.eu", "data-api.ecb.europa.eu"),
        ("banking", "funding", "monetary-policy", "settlement"),
        ("liquilens", "seiche"),
    ),
    "eba": (
        ("www.eba.europa.eu", "eba.europa.eu"),
        ("banking", "institutions", "prudential"),
        ("liquilens",),
    ),
    "esma": (
        ("www.esma.europa.eu", "esma.europa.eu"),
        ("markets", "securities", "settlement"),
        ("liquilens", "undertow"),
    ),
    # Multilateral and cross-market evidence.
    "bis": (
        ("www.bis.org", "bis.org"),
        ("banking", "funding", "markets", "prudential"),
        ("liquilens", "seiche", "undertow"),
    ),
    "financial-stability-board": (
        ("www.fsb.org", "fsb.org"),
        ("banking", "funding", "markets", "systemic-risk"),
        ("liquilens", "seiche", "undertow"),
    ),
    "iosco": (
        ("www.iosco.org", "iosco.org"),
        ("institutions", "markets", "securities"),
        ("liquilens", "undertow"),
    ),
    "imf": (
        ("www.imf.org", "imf.org"),
        ("economy", "funding", "measurement", "policy"),
        ("liquilens", "palimpsest", "seiche"),
    ),
    "world-bank-data": (
        ("api.worldbank.org", "data.worldbank.org"),
        ("economy", "measurement", "policy"),
        ("palimpsest", "seiche"),
    ),
    "oecd": (
        ("www.oecd.org", "oecd.org", "stats.oecd.org"),
        ("economy", "measurement", "policy"),
        ("palimpsest", "seiche"),
    ),
    # Asia-Pacific and Canadian central-bank/prudential evidence.
    "bank-of-japan": (
        ("www.boj.or.jp", "boj.or.jp"),
        ("funding", "markets", "monetary-policy"),
        ("seiche", "undertow"),
    ),
    "japan-fsa": (
        ("www.fsa.go.jp", "fsa.go.jp"),
        ("banking", "institutions", "markets", "prudential"),
        ("liquilens", "undertow"),
    ),
    "mas": (
        ("www.mas.gov.sg", "mas.gov.sg"),
        ("banking", "funding", "markets", "prudential"),
        ("liquilens", "seiche", "undertow"),
    ),
    "hkma": (
        ("www.hkma.gov.hk", "hkma.gov.hk"),
        ("banking", "funding", "monetary-policy", "prudential"),
        ("liquilens", "seiche"),
    ),
    "rbnz": (
        ("www.rbnz.govt.nz", "rbnz.govt.nz"),
        ("banking", "funding", "monetary-policy", "prudential"),
        ("liquilens", "seiche"),
    ),
    "rba": (
        ("www.rba.gov.au", "rba.gov.au"),
        ("funding", "markets", "monetary-policy"),
        ("seiche",),
    ),
    "apra": (
        ("www.apra.gov.au", "apra.gov.au"),
        ("banking", "institutions", "prudential"),
        ("liquilens",),
    ),
    "bank-of-canada": (
        ("www.bankofcanada.ca", "bankofcanada.ca"),
        ("funding", "markets", "monetary-policy"),
        ("seiche", "undertow"),
    ),
    "osfi": (
        ("www.osfi-bsif.gc.ca", "osfi-bsif.gc.ca"),
        ("banking", "institutions", "prudential"),
        ("liquilens",),
    ),
}

_CONFIG_KEYS = frozenset({"schema_version", "source", "limits", "targets"})
_SOURCE_KEYS = frozenset(_SOURCE)
_LIMIT_KEYS = frozenset(
    {
        "input_bytes",
        "input_rows",
        "line_bytes",
        "url_chars",
        "warc_record_bytes",
        "feature_rows",
        "news_events",
        "network_timeout_seconds",
        "archive_requests_per_second",
    }
)
_TARGET_KEYS = frozenset(
    {
        "id",
        "host",
        "aliases",
        "topics",
        "products",
        "scope",
        "training_use",
        "rights_ref",
    }
)

_ROW_ALIASES: dict[str, tuple[str, ...]] = {
    "crawl": ("crawl",),
    "url": ("url", "original"),
    "host": ("url_host_name", "host"),
    "capture_at": ("fetch_time", "timestamp", "capture_at"),
    "status": ("fetch_status", "status", "statuscode"),
    "digest": ("content_digest", "digest"),
    "mime": (
        "content_mime_detected",
        "content_mime_type",
        "mime",
        "mimetype",
    ),
    "languages": ("content_languages", "languages", "language"),
    "warc_filename": ("warc_filename", "filename"),
    "warc_offset": ("warc_record_offset", "offset"),
    "warc_length": ("warc_record_length", "length"),
}


class CommonCrawlLakeError(RuntimeError):
    """Base class for fail-loud Common Crawl lake errors."""


class ConfigurationError(CommonCrawlLakeError):
    """The committed host allowlist or a hard limit is invalid."""


class ValidationError(CommonCrawlLakeError):
    """An import row, database, feature file, or archive response is invalid."""


class LimitExceeded(CommonCrawlLakeError):
    """An input, row, response, or output exceeded its reviewed bound."""


class WarehouseBusy(CommonCrawlLakeError):
    """Another process already owns the private warehouse lock."""


class TransportError(CommonCrawlLakeError):
    """A fixed Common Crawl public endpoint could not be read safely."""


@dataclass(frozen=True)
class Limits:
    input_bytes: int
    input_rows: int
    line_bytes: int
    url_chars: int
    warc_record_bytes: int
    feature_rows: int
    news_events: int
    network_timeout_seconds: int
    archive_requests_per_second: float


@dataclass(frozen=True)
class Target:
    id: str
    host: str
    aliases: tuple[str, ...]
    topics: tuple[str, ...]
    products: tuple[str, ...]
    scope: str
    training_use: str
    rights_ref: str


@dataclass(frozen=True)
class LakeConfig:
    collection_catalog_url: str
    index_base_url: str
    data_base_url: str
    terms_url: str
    limits: Limits
    targets: tuple[Target, ...]

    @property
    def target_by_host(self) -> dict[str, Target]:
        return {
            host: target
            for target in self.targets
            for host in (target.host, *target.aliases)
        }

    @property
    def target_by_id(self) -> dict[str, Target]:
        return {target.id: target for target in self.targets}

    @property
    def scope_sha256(self) -> str:
        value = [
            {
                "id": target.id,
                "host": target.host,
                "aliases": list(target.aliases),
                "topics": list(target.topics),
                "products": list(target.products),
                "scope": target.scope,
                "training_use": target.training_use,
                "rights_ref": target.rights_ref,
            }
            for target in self.targets
        ]
        return hashlib.sha256(_canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class Observation:
    target_id: str
    crawl: str
    canonical_url: str
    url_sha256: str
    capture_at: str
    fetch_status: int
    content_digest: str
    mime_type: str
    languages: str
    warc_filename: str
    warc_record_offset: int
    warc_record_length: int
    locator_sha256: str


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _exact_object(value: object, fields: frozenset[str], path: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ConfigurationError(f"{path} must be an object")
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields)
    if missing or unknown:
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if unknown:
            detail.append(f"unknown {unknown}")
        raise ConfigurationError(f"{path} has " + " and ".join(detail))
    return value


def _positive_int(value: object, path: str, *, maximum: int) -> int:
    if type(value) is not int or value < 1 or value > maximum:
        raise ConfigurationError(f"{path} must be an integer from 1 to {maximum}")
    return value


def _positive_float(value: object, path: str, *, maximum: float) -> float:
    if type(value) not in (int, float) or isinstance(value, bool):
        raise ConfigurationError(f"{path} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > maximum:
        raise ConfigurationError(f"{path} must be greater than zero and at most {maximum}")
    return number


def load_config(path: Path | str = DEFAULT_CONFIG) -> LakeConfig:
    """Load and strictly validate the reviewed source, target, and limit contract."""

    config_path = Path(path)
    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"cannot read Common Crawl config: {exc}") from exc
    if len(raw) > 256 * 1024:
        raise ConfigurationError("Common Crawl config exceeds 256 KiB")
    try:
        document = json.loads(raw)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ConfigurationError("Common Crawl config is not valid JSON") from exc
    root = _exact_object(document, _CONFIG_KEYS, "config")
    if root["schema_version"] != 2 or isinstance(root["schema_version"], bool):
        raise ConfigurationError("Common Crawl config requires schema_version 2")

    source = _exact_object(root["source"], _SOURCE_KEYS, "source")
    if source != _SOURCE:
        raise ConfigurationError("source endpoints must match the fixed Common Crawl hosts")

    limits_doc = _exact_object(root["limits"], _LIMIT_KEYS, "limits")
    limits = Limits(
        input_bytes=_positive_int(
            limits_doc["input_bytes"], "limits.input_bytes", maximum=2 * 1024**4
        ),
        input_rows=_positive_int(
            limits_doc["input_rows"], "limits.input_rows", maximum=1_000_000_000
        ),
        line_bytes=_positive_int(
            limits_doc["line_bytes"], "limits.line_bytes", maximum=4 * 1024 * 1024
        ),
        url_chars=_positive_int(
            limits_doc["url_chars"], "limits.url_chars", maximum=16_384
        ),
        warc_record_bytes=_positive_int(
            limits_doc["warc_record_bytes"],
            "limits.warc_record_bytes",
            maximum=64 * 1024 * 1024,
        ),
        feature_rows=_positive_int(
            limits_doc["feature_rows"], "limits.feature_rows", maximum=1_000_000
        ),
        news_events=_positive_int(
            limits_doc["news_events"], "limits.news_events", maximum=100_000
        ),
        network_timeout_seconds=_positive_int(
            limits_doc["network_timeout_seconds"],
            "limits.network_timeout_seconds",
            maximum=120,
        ),
        archive_requests_per_second=_positive_float(
            limits_doc["archive_requests_per_second"],
            "limits.archive_requests_per_second",
            maximum=1.0,
        ),
    )

    targets_doc = root["targets"]
    if not isinstance(targets_doc, list) or not targets_doc:
        raise ConfigurationError("targets must be a non-empty list")
    if len(targets_doc) != len(_APPROVED_TARGETS):
        raise ConfigurationError("targets must contain the complete approved target set")
    targets: list[Target] = []
    seen_ids: set[str] = set()
    seen_hosts: set[str] = set()
    for index, value in enumerate(targets_doc):
        item = _exact_object(value, _TARGET_KEYS, f"targets[{index}]")
        target_id = item["id"]
        host = item["host"]
        aliases = item["aliases"]
        topics = item["topics"]
        products = item["products"]
        if type(target_id) is not str or not _TARGET_ID_RE.fullmatch(target_id):
            raise ConfigurationError(f"targets[{index}].id is invalid")
        if type(host) is not str or host != host.lower() or not _HOST_RE.fullmatch(host):
            raise ConfigurationError(f"targets[{index}].host is invalid")
        if (
            not isinstance(aliases, list)
            or len(aliases) > 8
            or aliases != list(dict.fromkeys(aliases))
            or any(
                type(alias) is not str
                or alias != alias.lower()
                or not _HOST_RE.fullmatch(alias)
                or alias == host
                for alias in aliases
            )
        ):
            raise ConfigurationError(f"targets[{index}].aliases is invalid")
        if (
            not isinstance(topics, list)
            or not topics
            or len(topics) > 12
            or topics != list(dict.fromkeys(topics))
            or any(type(topic) is not str or not _TARGET_ID_RE.fullmatch(topic) for topic in topics)
        ):
            raise ConfigurationError(f"targets[{index}].topics is invalid")
        if (
            not isinstance(products, list)
            or not products
            or products != list(dict.fromkeys(products))
            or products != sorted(products)
            or any(product not in _APPROVED_PRODUCTS for product in products)
        ):
            raise ConfigurationError(f"targets[{index}].products is invalid")
        expected = _APPROVED_TARGETS.get(target_id)
        target_hosts = (host, *aliases)
        if expected != (target_hosts, tuple(topics), tuple(products)):
            raise ConfigurationError(f"target {target_id!r} is not in the code-level allowlist")
        if target_id in seen_ids or any(value in seen_hosts for value in target_hosts):
            raise ConfigurationError("target ids and hosts must be unique")
        scope = item["scope"]
        rights_ref = item["rights_ref"]
        if scope != "institution-level public record":
            raise ConfigurationError(f"target {target_id!r} must remain institution-level")
        if item["training_use"] != "metadata_only":
            raise ConfigurationError(f"target {target_id!r} must remain metadata_only")
        if type(rights_ref) is not str or len(rights_ref.strip()) < 20 or len(rights_ref) > 512:
            raise ConfigurationError(f"target {target_id!r} needs a bounded rights reference")
        targets.append(
            Target(
                id=target_id,
                host=host,
                aliases=tuple(aliases),
                topics=tuple(topics),
                products=tuple(products),
                scope=scope,
                training_use="metadata_only",
                rights_ref=rights_ref,
            )
        )
        seen_ids.add(target_id)
        seen_hosts.update(target_hosts)
    if set(seen_ids) != set(_APPROVED_TARGETS):
        raise ConfigurationError("approved targets are missing from config")
    return LakeConfig(
        collection_catalog_url=source["collection_catalog_url"],
        index_base_url=source["index_base_url"],
        data_base_url=source["data_base_url"],
        terms_url=source["terms_url"],
        limits=limits,
        targets=tuple(targets),
    )


def warehouse_path(value: Path | str | None = None) -> Path:
    """Resolve the private warehouse path, honoring the Hetzner environment seam."""

    raw = value or os.getenv("PALIMPSEST_COMMON_CRAWL_WAREHOUSE_DIR") or DEFAULT_WAREHOUSE
    path = Path(raw).expanduser()
    if path == Path(path.anchor) or ".." in path.parts:
        raise ConfigurationError("warehouse cannot be a filesystem root or contain '..'")
    return path


def database_path(warehouse: Path | str | None = None) -> Path:
    return warehouse_path(warehouse) / DEFAULT_DATABASE_NAME


CHINA_JOINS_KIND = "common-crawl-china-observation-joins"
CHINA_JOINS_SCHEMA = "palimpsest.common-crawl-china-joins.v1"
CHINA_JOINS_FILENAME = "china-observation-lake-joins.json"
SANITIZED_MATCH_KEYS = (
    "match_kind",
    "target_id",
    "host",
    "crawl",
    "capture_at",
    "mime_type",
    "languages",
    "content_digest",
    "locator_sha256",
    "relation",
    "uncertainty",
)
_LAKE_LEAK_KEYS = frozenset(
    {
        "canonical_url",
        "url",
        "warc_filename",
        "warc_record_offset",
        "warc_record_length",
        "input_sha256",
    }
)


def existing_database_path(value: Path | str | None = None) -> Path | None:
    """Return the sqlite path only when the file already exists. Never create it."""

    if value is not None:
        path = Path(value).expanduser()
        if path.is_file():
            return path
        candidate = path / DEFAULT_DATABASE_NAME
        return candidate if candidate.is_file() else None
    env = (os.getenv("PALIMPSEST_COMMON_CRAWL_WAREHOUSE_DIR") or "").strip()
    candidates: list[Path] = []
    if env:
        env_path = Path(env).expanduser()
        candidates.append(env_path if env_path.suffix == ".sqlite3" else env_path / DEFAULT_DATABASE_NAME)
    candidates.append(Path("/var/lib/palimpsest/common-crawl") / DEFAULT_DATABASE_NAME)
    candidates.append(DEFAULT_WAREHOUSE / DEFAULT_DATABASE_NAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def open_existing_database(value: Path | str | None = None) -> sqlite3.Connection | None:
    """Read-only connection to an already-present warehouse. None if absent."""

    path = existing_database_path(value)
    if path is None:
        return None
    try:
        uri = path.resolve().as_posix().replace("?", "%3F")
        connection = sqlite3.connect(f"file:{uri}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("SELECT 1")
    except sqlite3.Error:
        return None
    return connection


def lake_observation_count(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("SELECT COUNT(*) AS n FROM observations").fetchone()
    except sqlite3.Error:
        return 0
    if row is None:
        return 0
    return int(row["n"] if "n" in row.keys() else row[0])


def public_url_identity(
    url: object, *, maximum_chars: int = 4096
) -> tuple[str, str] | None:
    """Stable ``url_sha256`` and host for an already-public URL. None if unusable."""

    if type(url) is not str or not url:
        return None
    try:
        canonical, host = _canonical_url(url, maximum_chars=maximum_chars)
    except (ValidationError, ValueError):
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), host


def sanitized_match(row: Mapping[str, Any], match_kind: str) -> dict[str, Any]:
    """Public receipt fields. Never include lake URLs or WARC fetch coordinates."""

    host = ""
    canonical = row["canonical_url"] if "canonical_url" in row.keys() else ""
    if isinstance(canonical, str) and canonical:
        try:
            host = (urlsplit(canonical).hostname or "").lower()
        except ValueError:
            host = ""
    if match_kind == "host":
        receipt = {
            "match_kind": "host",
            "target_id": row["target_id"],
            "host": host or None,
            "crawl": row["crawl"],
            "capture_at": row["capture_at"],
            "mime_type": None,
            "languages": None,
            "content_digest": None,
            "locator_sha256": None,
            "relation": "instrument-archive-context-not-url-corroboration",
            "uncertainty": (
                "Common Crawl host coverage on the node lake. "
                "Not a matching URL. Not a deletion claim."
            ),
        }
    else:
        receipt = {
            "match_kind": match_kind,
            "target_id": row["target_id"],
            "host": host or None,
            "crawl": row["crawl"],
            "capture_at": row["capture_at"],
            "mime_type": row["mime_type"] or None,
            "languages": row["languages"] or None,
            "content_digest": row["content_digest"] or None,
            "locator_sha256": row["locator_sha256"] or None,
            "relation": "archive-coverage-not-deletion",
            "uncertainty": (
                "Common Crawl capture on the node lake. "
                "Coverage gap is not a deletion."
            ),
        }
    leaked = _LAKE_LEAK_KEYS.intersection(receipt)
    if leaked:
        raise ValidationError(f"sanitized lake match leaked private keys: {sorted(leaked)}")
    return receipt


def public_match_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value.get(key) for key in SANITIZED_MATCH_KEYS}


def match_observation(
    connection: sqlite3.Connection,
    record: Mapping[str, Any],
    config: LakeConfig | None = None,
) -> dict[str, Any] | None:
    """Newest URL, digest, or allowlisted-host match. Read-only. No network."""

    cfg = config or load_config()
    url = record.get("url") or record.get("source_url")
    identity = public_url_identity(url, maximum_chars=cfg.limits.url_chars)
    if identity is not None:
        url_sha, _host = identity
        row = connection.execute(
            """
            SELECT * FROM observations
             WHERE url_sha256 = ?
             ORDER BY capture_at DESC, observation_id DESC
             LIMIT 1
            """,
            (url_sha,),
        ).fetchone()
        if row is not None:
            return sanitized_match(row, "url")
    digest = record.get("content_digest")
    if isinstance(digest, str):
        normalized = digest.strip().upper()
        if _DIGEST_RE.fullmatch(normalized):
            row = connection.execute(
                """
                SELECT * FROM observations
                 WHERE content_digest = ?
                 ORDER BY capture_at DESC, observation_id DESC
                 LIMIT 1
                """,
                (normalized,),
            ).fetchone()
            if row is not None:
                return sanitized_match(row, "digest")
    if identity is not None:
        host = identity[1]
        target = cfg.target_by_host.get(host)
        if target is not None:
            row = connection.execute(
                """
                SELECT * FROM observations
                 WHERE target_id = ?
                 ORDER BY capture_at DESC, observation_id DESC
                 LIMIT 1
                """,
                (target.id,),
            ).fetchone()
            if row is not None:
                return sanitized_match(row, "host")
    return None


def china_lake_receipt_paths() -> list[Path]:
    paths: list[Path] = []
    env = (os.getenv("PALIMPSEST_CHINA_LAKE_JOINS") or "").strip()
    if env:
        paths.append(Path(env).expanduser())
    paths.append(ROOT / "readings" / "common-crawl-china-joins-latest.json")
    env_wh = (os.getenv("PALIMPSEST_COMMON_CRAWL_WAREHOUSE_DIR") or "").strip()
    if env_wh:
        paths.append(Path(env_wh).expanduser() / "derived" / CHINA_JOINS_FILENAME)
    paths.append(Path("/var/lib/palimpsest/common-crawl") / "derived" / CHINA_JOINS_FILENAME)
    default_derived = DEFAULT_WAREHOUSE / "derived" / CHINA_JOINS_FILENAME
    if default_derived.is_file():
        paths.append(default_derived)
    return paths


def load_china_lake_receipt(path: Path | str | None = None) -> dict[str, Any] | None:
    """Load a sanitized join receipt. Missing or unreadable files abstain."""

    candidates = [Path(path).expanduser()] if path is not None else china_lake_receipt_paths()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("kind") == CHINA_JOINS_KIND:
            return data
    return None


def _row_value(row: Mapping[str, Any], name: str, default: Any = "") -> Any:
    for alias in _ROW_ALIASES[name]:
        if alias in row and row[alias] not in (None, ""):
            return row[alias]
    return default


def _crawl(value: object, fallback: str | None) -> str:
    text = str(value or fallback or "").strip()
    match = _CRAWL_RE.fullmatch(text)
    if not match:
        raise ValidationError(f"invalid or missing crawl id: {text!r}")
    year, week = int(match.group(1)), int(match.group(2))
    if year < 2008 or year > 2100 or week < 1 or week > 53:
        raise ValidationError(f"crawl id is outside valid bounds: {text!r}")
    return text


def _timestamp(value: object) -> str:
    if type(value) is datetime:
        parsed = value
    else:
        text = str(value or "").strip()
        if _TIMESTAMP14_RE.fullmatch(text):
            try:
                parsed = datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            except ValueError as exc:
                raise ValidationError(f"invalid capture timestamp: {text!r}") from exc
        else:
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError as exc:
                raise ValidationError(f"invalid capture timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("capture timestamp must include a timezone")
    normalized = parsed.astimezone(UTC).replace(microsecond=0)
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


def _integer(value: object, path: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{path} must be an integer")
    if type(value) is int:
        number = value
    elif type(value) is float:
        if not math.isfinite(value) or not value.is_integer():
            raise ValidationError(f"{path} must be an exact integer")
        number = int(value)
    elif type(value) is str:
        text = value.strip()
        if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", text) is None:
            raise ValidationError(f"{path} must be an exact integer")
        number = int(text)
    else:
        raise ValidationError(f"{path} must be an integer")
    if number < minimum or number > maximum:
        raise ValidationError(f"{path} must be from {minimum} to {maximum}")
    return number


def _canonical_url(value: object, *, maximum_chars: int) -> tuple[str, str]:
    if type(value) is not str or not value or len(value) > maximum_chars:
        raise ValidationError("url is missing or exceeds the configured length")
    if "\\" in value or any(ord(char) < 0x20 or char.isspace() for char in value):
        raise ValidationError("url contains whitespace, controls, or a backslash")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("url is structurally invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValidationError("url must be absolute HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise ValidationError("url cannot contain credentials")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValidationError("url host is not valid IDNA") from exc
    if not _HOST_RE.fullmatch(host):
        raise ValidationError("url host is invalid")
    scheme = parsed.scheme.lower()
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        raise ValidationError("url uses a non-default port")
    path = parsed.path or "/"
    canonical = urlunsplit((scheme, host, path, parsed.query, ""))
    if len(canonical) > maximum_chars:
        raise ValidationError("canonical url exceeds the configured length")
    return canonical, host


def _content_digest(value: object) -> str:
    text = str(value or "").strip().upper()
    if text in {"", "-"}:
        return ""
    if text.startswith("SHA1:"):
        text = text[5:]
    if not _DIGEST_RE.fullmatch(text):
        raise ValidationError("content digest is not a Common Crawl SHA-1 base32 digest")
    return text


def _mime(value: object) -> str:
    text = str(value or "").strip().lower().split(";", 1)[0]
    if not text:
        return ""
    if len(text) > 128 or not _MIME_RE.fullmatch(text):
        raise ValidationError("mime type is invalid")
    return text


def _languages(value: object) -> str:
    if isinstance(value, (list, tuple)):
        text = ",".join(str(item).strip() for item in value if str(item).strip())
    else:
        text = str(value or "").strip()
    if len(text) > 128 or not _LANGUAGE_RE.fullmatch(text):
        raise ValidationError("content languages are invalid")
    return ",".join(dict.fromkeys(part.lower() for part in text.split(",") if part))


def _warc_filename(value: object, crawl: str) -> str:
    text = str(value or "").strip()
    if len(text) > 1024 or not text.endswith(".warc.gz"):
        raise ValidationError("warc_filename is missing or invalid")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValidationError("warc_filename must be a safe relative object key")
    prefix = ("crawl-data", crawl)
    if tuple(path.parts[:2]) != prefix or "warc" not in path.parts:
        raise ValidationError("warc_filename does not belong to the declared crawl")
    return text


def normalize_observation(
    row: Mapping[str, Any],
    config: LakeConfig,
    *,
    crawl: str | None = None,
) -> Observation | None:
    """Normalize one URL Index row, returning ``None`` for an unapproved host.

    Rows from unapproved hosts are counted as out-of-scope and discarded. Once a row
    names an approved host, every evidence field is strict and a malformed value aborts
    the transaction rather than quietly weakening the archive.
    """

    if not isinstance(row, Mapping):
        raise ValidationError("URL Index row must be an object")
    canonical_url, host = _canonical_url(
        _row_value(row, "url"), maximum_chars=config.limits.url_chars
    )
    declared_host = str(_row_value(row, "host", host)).strip().lower()
    if declared_host and declared_host != host:
        raise ValidationError("url_host_name disagrees with the URL host")
    target = config.target_by_host.get(host)
    if target is None:
        return None
    crawl_id = _crawl(_row_value(row, "crawl"), crawl)
    capture_at = _timestamp(_row_value(row, "capture_at"))
    status = _integer(_row_value(row, "status"), "fetch_status", minimum=100, maximum=599)
    offset = _integer(
        _row_value(row, "warc_offset"),
        "warc_record_offset",
        minimum=0,
        maximum=9_007_199_254_740_991,
    )
    length = _integer(
        _row_value(row, "warc_length"),
        "warc_record_length",
        minimum=1,
        maximum=config.limits.warc_record_bytes,
    )
    filename = _warc_filename(_row_value(row, "warc_filename"), crawl_id)
    locator_doc = {"filename": filename, "offset": offset, "length": length}
    return Observation(
        target_id=target.id,
        crawl=crawl_id,
        canonical_url=canonical_url,
        url_sha256=hashlib.sha256(canonical_url.encode("utf-8")).hexdigest(),
        capture_at=capture_at,
        fetch_status=status,
        content_digest=_content_digest(_row_value(row, "digest")),
        mime_type=_mime(_row_value(row, "mime")),
        languages=_languages(_row_value(row, "languages")),
        warc_filename=filename,
        warc_record_offset=offset,
        warc_record_length=length,
        locator_sha256=hashlib.sha256(_canonical_json(locator_doc)).hexdigest(),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValidationError(f"JSON row contains duplicate key {key!r}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValidationError(f"JSON row contains non-finite number {value!r}")


@contextmanager
def _binary_input(path: Path):
    if path.name.endswith(".gz"):
        handle = gzip.open(path, "rb")
    else:
        handle = path.open("rb")
    try:
        yield handle
    finally:
        handle.close()


def _input_format(path: Path, explicit: str | None = None) -> str:
    if explicit:
        chosen = explicit.lower()
    else:
        name = path.name[:-3] if path.name.endswith(".gz") else path.name
        chosen = "csv" if name.endswith(".csv") else "jsonl"
    if chosen not in {"jsonl", "csv"}:
        raise ValidationError("input format must be jsonl or csv")
    return chosen


def _iter_jsonl(path: Path, limits: Limits) -> Iterator[dict[str, Any]]:
    with _binary_input(path) as handle:
        for line_number, raw in enumerate(handle, 1):
            if len(raw) > limits.line_bytes:
                raise LimitExceeded(f"{path}:{line_number} exceeds the line byte cap")
            if not raw.strip():
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationError(f"{path}:{line_number} is not UTF-8") from exc
            try:
                value = json.loads(
                    text,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_constant,
                )
            except ValidationError:
                raise
            except (ValueError, TypeError) as exc:
                raise ValidationError(f"{path}:{line_number} is not valid JSON") from exc
            if type(value) is not dict:
                raise ValidationError(f"{path}:{line_number} must be a JSON object")
            yield value


def _iter_csv(path: Path, limits: Limits) -> Iterator[dict[str, Any]]:
    csv.field_size_limit(limits.line_bytes)
    with _binary_input(path) as binary:
        text = _BoundedTextLines(binary, limits.line_bytes, path)
        reader = csv.DictReader(text)
        if not reader.fieldnames:
            raise ValidationError(f"{path} has no CSV header")
        if len(reader.fieldnames) > 128 or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValidationError(f"{path} has duplicate or excessive CSV columns")
        for row_number, row in enumerate(reader, 2):
            if None in row:
                raise ValidationError(f"{path}:{row_number} has excess CSV fields")
            yield dict(row)


class _BoundedTextLines:
    """A minimal UTF-8 line iterator for csv.DictReader with a byte cap per line."""

    def __init__(self, binary, maximum: int, path: Path):
        self.binary = binary
        self.maximum = maximum
        self.path = path
        self.line_number = 0

    def __iter__(self):
        return self

    def __next__(self) -> str:
        raw = self.binary.readline(self.maximum + 1)
        if not raw:
            raise StopIteration
        self.line_number += 1
        if len(raw) > self.maximum:
            raise LimitExceeded(f"{self.path}:{self.line_number} exceeds the line byte cap")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(f"{self.path}:{self.line_number} is not UTF-8") from exc


def iter_export_rows(
    path: Path | str,
    limits: Limits,
    *,
    input_format: str | None = None,
) -> Iterator[dict[str, Any]]:
    input_path = Path(path)
    chosen = _input_format(input_path, input_format)
    iterator = _iter_csv if chosen == "csv" else _iter_jsonl
    yield from iterator(input_path, limits)


def _input_sha256(path: Path, maximum_bytes: int) -> tuple[str, int]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValidationError(f"cannot stat import file: {exc}") from exc
    if size < 1 or size > maximum_bytes:
        raise LimitExceeded(f"input file size {size} is outside the 1..{maximum_bytes} byte cap")
    digest = hashlib.sha256()
    consumed = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(8 * 1024 * 1024)
                if not chunk:
                    break
                consumed += len(chunk)
                if consumed > maximum_bytes:
                    raise LimitExceeded(f"input exceeds the {maximum_bytes} byte cap")
                digest.update(chunk)
    except OSError as exc:
        raise ValidationError(f"cannot hash import file: {exc}") from exc
    if consumed != size:
        raise ValidationError("input file changed while it was hashed")
    return digest.hexdigest(), size


@contextmanager
def _warehouse_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = root / LOCK_NAME
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WarehouseBusy("another Common Crawl lake process owns the lock") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA temp_store = FILE")
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create the private schema or verify that its version is understood."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS lake_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ingest_runs (
            input_sha256 TEXT PRIMARY KEY,
            input_name TEXT NOT NULL,
            input_bytes INTEGER NOT NULL,
            input_format TEXT NOT NULL,
            crawl_hint TEXT,
            scope_sha256 TEXT NOT NULL,
            ingested_at TEXT NOT NULL,
            rows_seen INTEGER NOT NULL,
            rows_accepted INTEGER NOT NULL,
            rows_out_of_scope INTEGER NOT NULL,
            rows_duplicate INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS observations (
            observation_id INTEGER PRIMARY KEY,
            target_id TEXT NOT NULL,
            crawl TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            url_sha256 TEXT NOT NULL,
            capture_at TEXT NOT NULL,
            fetch_status INTEGER NOT NULL,
            content_digest TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            languages TEXT NOT NULL,
            warc_filename TEXT NOT NULL,
            warc_record_offset INTEGER NOT NULL,
            warc_record_length INTEGER NOT NULL,
            locator_sha256 TEXT NOT NULL,
            input_sha256 TEXT NOT NULL REFERENCES ingest_runs(input_sha256),
            UNIQUE(crawl, canonical_url, capture_at, warc_filename, warc_record_offset),
            UNIQUE(locator_sha256)
        );

        CREATE INDEX IF NOT EXISTS observations_target_crawl
            ON observations(target_id, crawl);
        CREATE INDEX IF NOT EXISTS observations_url_time
            ON observations(target_id, url_sha256, capture_at);
        CREATE INDEX IF NOT EXISTS observations_capture_time
            ON observations(capture_at);

        CREATE TABLE IF NOT EXISTS record_objects (
            locator_sha256 TEXT PRIMARY KEY REFERENCES observations(locator_sha256),
            object_sha256 TEXT NOT NULL,
            object_bytes INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            retrieved_at TEXT NOT NULL
        );
        """
    )
    row = connection.execute(
        "SELECT value FROM lake_metadata WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO lake_metadata(key, value) VALUES ('schema_version', ?)",
            (str(DATABASE_SCHEMA_VERSION),),
        )
    elif row["value"] != str(DATABASE_SCHEMA_VERSION):
        raise ValidationError(
            f"database schema {row['value']!r} is not supported by this binary"
        )
    connection.commit()


_INSERT_OBSERVATION = """
    INSERT OR IGNORE INTO observations (
        target_id, crawl, canonical_url, url_sha256, capture_at, fetch_status,
        content_digest, mime_type, languages, warc_filename,
        warc_record_offset, warc_record_length, locator_sha256, input_sha256
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _observation_values(observation: Observation, input_sha256: str) -> tuple[Any, ...]:
    return (
        observation.target_id,
        observation.crawl,
        observation.canonical_url,
        observation.url_sha256,
        observation.capture_at,
        observation.fetch_status,
        observation.content_digest,
        observation.mime_type,
        observation.languages,
        observation.warc_filename,
        observation.warc_record_offset,
        observation.warc_record_length,
        observation.locator_sha256,
        input_sha256,
    )


def _iso_now(now: datetime | None = None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def ingest_export(
    input_path: Path | str,
    *,
    config_path: Path | str = DEFAULT_CONFIG,
    warehouse: Path | str | None = None,
    crawl: str | None = None,
    input_format: str | None = None,
    kill_switch: KillSwitch | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Atomically ingest one bounded URL Index export into the private warehouse.

    A duplicate file is idempotent. Any malformed in-scope row rolls back the whole
    file. Unapproved hosts are counted and discarded before publication or training.
    """

    config = load_config(config_path)
    gate = kill_switch or KillSwitch()
    if gate.is_halted():
        return {"collector": "common-crawl-lake", "status": "halted"}
    path = Path(input_path)
    chosen_format = _input_format(path, input_format)
    crawl_hint = _crawl("", crawl) if crawl else None
    input_sha256, input_bytes = _input_sha256(path, config.limits.input_bytes)
    root = warehouse_path(warehouse)
    with _warehouse_lock(root):
        if gate.is_halted():
            return {"collector": "common-crawl-lake", "status": "halted"}
        connection = _connect(root / DEFAULT_DATABASE_NAME)
        try:
            initialize_database(connection)
            prior = connection.execute(
                "SELECT * FROM ingest_runs WHERE input_sha256 = ?", (input_sha256,)
            ).fetchone()
            if prior is not None:
                return {
                    "collector": "common-crawl-lake",
                    "status": "unchanged",
                    "input_sha256": input_sha256,
                    "input_bytes": input_bytes,
                    "rows_seen": prior["rows_seen"],
                    "rows_accepted": prior["rows_accepted"],
                    "rows_out_of_scope": prior["rows_out_of_scope"],
                    "rows_duplicate": prior["rows_duplicate"],
                }

            # The FK requires the run row to exist before observations. It remains inside
            # the same transaction, so a row error removes both run and observations.
            generated_at = _iso_now(now)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO ingest_runs (
                    input_sha256, input_name, input_bytes, input_format, crawl_hint,
                    scope_sha256, ingested_at, rows_seen, rows_accepted,
                    rows_out_of_scope, rows_duplicate
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0)
                """,
                (
                    input_sha256,
                    path.name[:255],
                    input_bytes,
                    chosen_format,
                    crawl_hint,
                    config.scope_sha256,
                    generated_at,
                ),
            )
            seen = accepted = out_of_scope = duplicates = 0
            for row in iter_export_rows(path, config.limits, input_format=chosen_format):
                seen += 1
                if seen > config.limits.input_rows:
                    raise LimitExceeded(
                        f"input exceeds the {config.limits.input_rows} row cap"
                    )
                observation = normalize_observation(row, config, crawl=crawl_hint)
                if observation is None:
                    out_of_scope += 1
                    continue
                if observation.capture_at > generated_at:
                    raise ValidationError(
                        "capture timestamp cannot be later than the import knowledge time"
                    )
                cursor = connection.execute(
                    _INSERT_OBSERVATION,
                    _observation_values(observation, input_sha256),
                )
                if cursor.rowcount == 1:
                    accepted += 1
                else:
                    duplicates += 1
                if seen % 10_000 == 0 and gate.is_halted():
                    raise CommonCrawlLakeError("global kill switch engaged during import")
            if seen == 0:
                raise ValidationError("input contains no URL Index rows")
            if gate.is_halted():
                raise CommonCrawlLakeError("global kill switch engaged during import")
            verified_sha256, verified_bytes = _input_sha256(
                path, config.limits.input_bytes
            )
            if verified_sha256 != input_sha256 or verified_bytes != input_bytes:
                raise ValidationError("input file changed during import")
            if gate.is_halted():
                raise CommonCrawlLakeError("global kill switch engaged during import")
            connection.execute(
                """
                UPDATE ingest_runs
                   SET rows_seen = ?, rows_accepted = ?, rows_out_of_scope = ?,
                       rows_duplicate = ?
                 WHERE input_sha256 = ?
                """,
                (seen, accepted, out_of_scope, duplicates, input_sha256),
            )
            connection.commit()
            return {
                "collector": "common-crawl-lake",
                "status": "success",
                "generated_at": generated_at,
                "input_sha256": input_sha256,
                "input_bytes": input_bytes,
                "scope_sha256": config.scope_sha256,
                "rows_seen": seen,
                "rows_accepted": accepted,
                "rows_out_of_scope": out_of_scope,
                "rows_duplicate": duplicates,
                "database": str(root / DEFAULT_DATABASE_NAME),
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


_FEATURE_QUERY = """
WITH ranked AS (
    SELECT
        observation_id, target_id, crawl, canonical_url, capture_at,
        fetch_status, content_digest, mime_type, languages,
        warc_record_length, ingest_runs.ingested_at,
        ROW_NUMBER() OVER (
            PARTITION BY target_id, crawl, canonical_url
            ORDER BY capture_at DESC, observation_id DESC
        ) AS row_number
    FROM observations
    JOIN ingest_runs USING (input_sha256)
    WHERE capture_at <= ? AND ingest_runs.ingested_at <= ?
),
states AS (
    SELECT * FROM ranked WHERE row_number = 1
),
crawl_order AS (
    SELECT
        target_id,
        crawl,
        LAG(crawl) OVER (PARTITION BY target_id ORDER BY crawl) AS previous_crawl
    FROM (SELECT DISTINCT target_id, crawl FROM states)
),
aggregates AS (
    SELECT
        target_id,
        crawl,
        MIN(capture_at) AS first_capture_at,
        MAX(capture_at) AS last_capture_at,
        MAX(ingested_at) AS available_at,
        COUNT(*) AS unique_urls,
        SUM(CASE WHEN fetch_status BETWEEN 200 AND 299 THEN 1 ELSE 0 END) AS live_urls,
        SUM(CASE WHEN fetch_status >= 400 THEN 1 ELSE 0 END) AS error_urls,
        SUM(CASE WHEN languages LIKE '%zho%' OR languages LIKE '%zh%' THEN 1 ELSE 0 END)
            AS chinese_language_urls,
        SUM(warc_record_length) AS archive_bytes
    FROM states
    GROUP BY target_id, crawl
),
pairs AS (
    SELECT
        current.target_id,
        current.crawl,
        COUNT(previous.canonical_url) AS retained_urls,
        SUM(CASE WHEN previous.canonical_url IS NULL THEN 1 ELSE 0 END) AS appeared_urls,
        SUM(CASE
            WHEN previous.canonical_url IS NOT NULL
             AND current.fetch_status BETWEEN 200 AND 299
             AND previous.fetch_status BETWEEN 200 AND 299
             AND current.content_digest <> ''
             AND previous.content_digest <> ''
            THEN 1 ELSE 0 END) AS comparable_urls,
        SUM(CASE
            WHEN previous.canonical_url IS NOT NULL
             AND current.fetch_status BETWEEN 200 AND 299
             AND previous.fetch_status BETWEEN 200 AND 299
             AND current.content_digest <> ''
             AND previous.content_digest <> ''
             AND current.content_digest <> previous.content_digest
            THEN 1 ELSE 0 END) AS mutated_urls
    FROM states AS current
    JOIN crawl_order AS ordering
      ON ordering.target_id = current.target_id AND ordering.crawl = current.crawl
    LEFT JOIN states AS previous
      ON previous.target_id = current.target_id
     AND previous.crawl = ordering.previous_crawl
     AND previous.canonical_url = current.canonical_url
    GROUP BY current.target_id, current.crawl
)
SELECT
    aggregate.target_id,
    aggregate.crawl,
    ordering.previous_crawl,
    aggregate.first_capture_at,
    aggregate.last_capture_at,
    aggregate.available_at,
    aggregate.unique_urls,
    aggregate.live_urls,
    aggregate.error_urls,
    aggregate.chinese_language_urls,
    aggregate.archive_bytes,
    COALESCE(pair.retained_urls, 0) AS retained_urls,
    COALESCE(pair.appeared_urls, aggregate.unique_urls) AS appeared_urls,
    COALESCE(pair.comparable_urls, 0) AS comparable_urls,
    COALESCE(pair.mutated_urls, 0) AS mutated_urls,
    COALESCE(previous_aggregate.unique_urls, 0) AS previous_unique_urls
FROM aggregates AS aggregate
JOIN crawl_order AS ordering
  ON ordering.target_id = aggregate.target_id AND ordering.crawl = aggregate.crawl
LEFT JOIN pairs AS pair
  ON pair.target_id = aggregate.target_id AND pair.crawl = aggregate.crawl
LEFT JOIN aggregates AS previous_aggregate
  ON previous_aggregate.target_id = aggregate.target_id
 AND previous_aggregate.crawl = ordering.previous_crawl
ORDER BY aggregate.target_id, aggregate.crawl
"""


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 8)


def _robust_high_score(value: float | None, history: list[float]) -> float | None:
    """One-sided robust z-score using only earlier observations."""

    if value is None or len(history) < 6:
        return None
    center = statistics.median(history)
    deviations = [abs(item - center) for item in history]
    mad = statistics.median(deviations)
    if mad == 0:
        return 0.0 if value <= center else 20.0
    score = max(0.0, (value - center) / (1.4826 * mad))
    return round(min(score, 20.0), 6)


def build_feature_rows(
    connection: sqlite3.Connection,
    config: LakeConfig,
    *,
    as_of: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Build deterministic point-in-time aggregate features for ML and analysis.

    The anomaly score for each row uses only earlier crawls for that target. This
    prequential ordering prevents later revisions from leaking into earlier training rows.
    """

    if as_of is None:
        cutoff = "9999-12-31T23:59:59Z"
    elif isinstance(as_of, datetime):
        cutoff = _iso_now(as_of)
    elif isinstance(as_of, str):
        cutoff = _timestamp(as_of)
    else:
        raise ValueError("as_of must be a datetime, timestamp string, or None")

    target_map = config.target_by_id
    raw_rows = connection.execute(_FEATURE_QUERY, (cutoff, cutoff)).fetchall()
    if len(raw_rows) > config.limits.feature_rows:
        raise LimitExceeded(
            f"feature export exceeds the {config.limits.feature_rows} row cap"
        )
    histories: dict[str, dict[str, list[float]]] = {}
    output: list[dict[str, Any]] = []
    for raw in raw_rows:
        target = target_map.get(raw["target_id"])
        if target is None:
            raise ValidationError(f"database contains unknown target {raw['target_id']!r}")
        unique_urls = int(raw["unique_urls"])
        previous_urls = int(raw["previous_unique_urls"])
        retained = int(raw["retained_urls"])
        comparable = int(raw["comparable_urls"])
        mutations = int(raw["mutated_urls"])
        not_observed = max(0, previous_urls - retained)
        mutation_rate = _ratio(mutations, comparable)
        archive_gap_rate = _ratio(not_observed, previous_urls)
        error_rate = _ratio(int(raw["error_urls"]), unique_urls)
        coverage_ratio = _ratio(unique_urls, previous_urls)
        retention_ratio = _ratio(retained, previous_urls)

        history = histories.setdefault(
            target.id,
            {"mutation_rate": [], "archive_gap_rate": [], "error_rate": []},
        )
        component_scores = {
            "mutation_rate": _robust_high_score(mutation_rate, history["mutation_rate"]),
            "archive_gap_rate": _robust_high_score(
                archive_gap_rate, history["archive_gap_rate"]
            ),
            "error_rate": _robust_high_score(error_rate, history["error_rate"]),
        }
        available_scores = [value for value in component_scores.values() if value is not None]
        anomaly_score = max(available_scores) if available_scores else None
        if anomaly_score is None:
            anomaly_state = "warming_up"
        elif anomaly_score >= 4.5:
            anomaly_state = "archive_anomaly"
        else:
            anomaly_state = "within_archive_baseline"

        features = {
            "unique_urls": unique_urls,
            "live_urls": int(raw["live_urls"]),
            "error_urls": int(raw["error_urls"]),
            "chinese_language_urls": int(raw["chinese_language_urls"]),
            "archive_record_bytes": int(raw["archive_bytes"] or 0),
            "previous_unique_urls": previous_urls,
            "retained_urls": retained,
            "appeared_urls": int(raw["appeared_urls"]),
            "not_observed_urls": not_observed,
            "comparable_urls": comparable,
            "mutated_urls": mutations,
            "coverage_ratio": coverage_ratio,
            "retention_ratio": retention_ratio,
            "archive_gap_rate": archive_gap_rate,
            "mutation_rate": mutation_rate,
            "error_rate": error_rate,
        }
        row: dict[str, Any] = {
            "schema_version": FEATURE_SCHEMA_VERSION,
            "method_version": METHOD_VERSION,
            "target_id": target.id,
            "host": target.host,
            "aliases": list(target.aliases),
            "topics": list(target.topics),
            "products": list(target.products),
            "crawl": raw["crawl"],
            "previous_crawl": raw["previous_crawl"],
            "first_capture_at": raw["first_capture_at"],
            "last_capture_at": raw["last_capture_at"],
            "available_at": raw["available_at"],
            "scope": target.scope,
            "source": "Common Crawl URL Index and WARC locators",
            "features": features,
            "label": {
                "censorship": "unlabeled",
                "absence_semantics": "archive-coverage-gap-not-deletion",
            },
            "model": {
                "id": MODEL_ID,
                "minimum_prior_crawls": 6,
                "state": anomaly_state,
                "score": anomaly_score,
                "component_scores": component_scores,
            },
            "rights": {
                "training_use": "derived_only",
                "license_or_terms_ref": target.rights_ref,
            },
        }
        row["feature_sha256"] = hashlib.sha256(_canonical_json(row)).hexdigest()
        output.append(row)

        for name, value in (
            ("mutation_rate", mutation_rate),
            ("archive_gap_rate", archive_gap_rate),
            ("error_rate", error_rate),
        ):
            if value is not None:
                history[name].append(value)
    return output


def _atomic_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_feature_export(
    database: Path | str,
    output_path: Path | str,
    *,
    config_path: Path | str = DEFAULT_CONFIG,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    connection = _connect(Path(database))
    try:
        initialize_database(connection)
        rows = build_feature_rows(connection, config, as_of=as_of)
    finally:
        connection.close()
    payload = b"".join(_canonical_json(row) + b"\n" for row in rows)
    destination = Path(output_path)
    _atomic_bytes(destination, payload)
    return {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "status": "success",
        "rows": len(rows),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "output": str(destination),
    }


def build_summary(
    connection: sqlite3.Connection,
    config: LakeConfig,
    feature_rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    totals = connection.execute(
        """
        SELECT
            COUNT(*) AS observations,
            COUNT(DISTINCT url_sha256) AS unique_urls,
            COUNT(DISTINCT crawl) AS crawls,
            MIN(capture_at) AS first_capture_at,
            MAX(capture_at) AS last_capture_at,
            COALESCE(SUM(warc_record_length), 0) AS indexed_record_bytes
        FROM observations
        """
    ).fetchone()
    runs = connection.execute("SELECT COUNT(*) AS count FROM ingest_runs").fetchone()["count"]
    objects = connection.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(object_bytes), 0) AS bytes FROM record_objects"
    ).fetchone()
    latest: dict[str, dict[str, Any]] = {}
    for row in feature_rows:
        latest[row["target_id"]] = row
    targets = []
    for target in config.targets:
        row = latest.get(target.id)
        targets.append(
            {
                "id": target.id,
                "host": target.host,
                "aliases": list(target.aliases),
                "topics": list(target.topics),
                "products": list(target.products),
                "latest_crawl": row["crawl"] if row else None,
                "latest_capture_at": row["last_capture_at"] if row else None,
                "latest_available_at": row["available_at"] if row else None,
                "unique_urls": row["features"]["unique_urls"] if row else 0,
                "mutated_urls": row["features"]["mutated_urls"] if row else 0,
                "archive_gap_rate": row["features"]["archive_gap_rate"] if row else None,
                "anomaly_state": row["model"]["state"] if row else "no_data",
                "anomaly_score": row["model"]["score"] if row else None,
            }
        )
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "generated_at": _iso_now(now),
        "status": "reporting" if int(totals["observations"]) else "no_data",
        "source": "Common Crawl public monthly archive",
        "method": (
            "strict institutional URL Index import with exact WARC locators; "
            "prequential target-level mutation and coverage features"
        ),
        "scope_sha256": config.scope_sha256,
        "observations": int(totals["observations"]),
        "unique_urls": int(totals["unique_urls"]),
        "crawls": int(totals["crawls"]),
        "ingest_runs": int(runs),
        "indexed_record_bytes": int(totals["indexed_record_bytes"]),
        "retained_warc_records": int(objects["count"]),
        "retained_warc_bytes": int(objects["bytes"]),
        "first_capture_at": totals["first_capture_at"],
        "last_capture_at": totals["last_capture_at"],
        "targets": targets,
        "training": {
            "feature_schema": FEATURE_SCHEMA_VERSION,
            "feature_rows": len(feature_rows),
            "raw_text_policy": "excluded-unless-a-separate-rights-review-allows-it",
            "split_rule": "knowledge-time-ordered-point-in-time-only",
            "label_state": "unlabeled",
        },
        "caveats": [
            "Common Crawl coverage is incomplete and popularity-biased.",
            "A missing capture is an archive coverage gap, not evidence of deletion.",
            "A digest change proves archived bytes changed; it does not identify cause or intent.",
            "Targets are reviewed first-party institutions and source bodies remain outside public output.",
        ],
    }
    summary["summary_sha256"] = hashlib.sha256(_canonical_json(summary)).hexdigest()
    return summary


def write_summary(
    database: Path | str,
    output_path: Path | str,
    *,
    config_path: Path | str = DEFAULT_CONFIG,
    now: datetime | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    connection = _connect(Path(database))
    try:
        initialize_database(connection)
        features = build_feature_rows(connection, config)
        summary = build_summary(connection, config, features, now=now)
    finally:
        connection.close()
    _atomic_bytes(Path(output_path), _canonical_json(summary) + b"\n")
    return summary


def _sql_literal(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValidationError("SQL parameter contains a control character")
    return "'" + value.replace("'", "''") + "'"


def _existing_real_directory(value: Path | str, *, label: str) -> tuple[Path, os.stat_result]:
    path = Path(value)
    if not path.is_absolute():
        raise ValidationError(f"{label} must be an absolute path")
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValidationError(f"{label} must be an existing directory: {exc}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise ValidationError(f"{label} must not be a symlink")
    if not stat.S_ISDIR(before.st_mode):
        raise ValidationError(f"{label} must be an existing directory")
    try:
        resolved = path.resolve(strict=True)
        after = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"cannot safely resolve {label}: {exc}") from exc
    if resolved != path:
        raise ValidationError(f"{label} must be canonical and contain no symlink components")
    if (before.st_dev, before.st_ino, before.st_mode) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
    ):
        raise ValidationError(f"{label} changed during validation")
    return resolved, after


def validate_duckdb_spill_directory(
    temp_directory: Path | str,
    *,
    bulk_volume_root: Path | str,
) -> Path:
    """Return a canonical DuckDB spill path on the reviewed non-root volume.

    The bulk-volume root is the private warehouse bind mount selected by the
    operator. Requiring the spill directory to be a strict descendant on the
    same device prevents a typo, symlink, or nested mount from redirecting a
    large spill onto the node's root disk.
    """

    volume, volume_stat = _existing_real_directory(bulk_volume_root, label="bulk_volume_root")
    spill, spill_stat = _existing_real_directory(temp_directory, label="temp_directory")
    if volume == Path("/"):
        raise ValidationError("bulk_volume_root cannot be the filesystem root")
    try:
        relative_spill = spill.relative_to(volume)
    except ValueError as exc:
        raise ValidationError("temp_directory must be inside bulk_volume_root") from exc
    if relative_spill == Path("."):
        raise ValidationError("temp_directory must be below bulk_volume_root")
    try:
        root_device = Path("/").stat().st_dev
    except OSError as exc:
        raise ValidationError(f"cannot identify the root filesystem: {exc}") from exc
    if volume_stat.st_dev == root_device:
        raise ValidationError("bulk_volume_root is on the root filesystem")
    if spill_stat.st_dev != volume_stat.st_dev:
        raise ValidationError("temp_directory and bulk_volume_root must use the same filesystem")
    return spill


def render_duckdb_export_sql(
    crawl: str,
    index_glob: str,
    output_path: str,
    *,
    temp_directory: Path | str,
    bulk_volume_root: Path | str,
    config_path: Path | str = DEFAULT_CONFIG,
    expected_scope_sha256: str | None = None,
) -> str:
    """Render a reviewed local-DuckDB URL Index export query.

    ``index_glob`` should point at a locally mirrored Parquet partition. The query emits
    newline-delimited gzip JSON containing metadata and WARC locators only.
    """

    config = load_config(config_path)
    if (
        expected_scope_sha256 is not None
        and config.scope_sha256 != expected_scope_sha256
    ):
        raise ValidationError("target scope changed after the filter plan was built")
    crawl_id = _crawl(crawl, None)
    if not index_glob or len(index_glob) > 4096:
        raise ValidationError("index_glob is missing or too long")
    if not output_path or len(output_path) > 4096:
        raise ValidationError("output_path is missing or too long")
    spill = validate_duckdb_spill_directory(temp_directory, bulk_volume_root=bulk_volume_root)
    hosts = ", ".join(_sql_literal(host) for host in sorted(config.target_by_host))
    return f"""-- Generated by Palimpsest. Query a LOCAL Common Crawl URL Index mirror.
SET memory_limit = {_sql_literal(DUCKDB_MEMORY_LIMIT)};
SET threads = {DUCKDB_THREADS};
SET temp_directory = {_sql_literal(str(spill))};
SET max_temp_directory_size = {_sql_literal(DUCKDB_MAX_TEMP_DIRECTORY_SIZE)};

COPY (
    SELECT
        {_sql_literal(crawl_id)} AS crawl,
        url,
        url_host_name,
        CAST(fetch_time AS VARCHAR) AS fetch_time,
        fetch_status,
        content_digest,
        COALESCE(content_mime_detected, content_mime_type, '') AS content_mime_detected,
        COALESCE(content_languages, '') AS content_languages,
        warc_filename,
        warc_record_offset,
        warc_record_length
    FROM read_parquet({_sql_literal(index_glob)}, hive_partitioning = true)
    WHERE crawl = {_sql_literal(crawl_id)}
      AND subset = 'warc'
      AND url_host_name IN ({hosts})
      AND warc_record_length BETWEEN 1 AND {config.limits.warc_record_bytes}
) TO {_sql_literal(output_path)}
  (FORMAT JSON, ARRAY false, COMPRESSION GZIP);
"""


def plan_index_ingest(
    *,
    config_path: Path | str = DEFAULT_CONFIG,
    plan_path: Path | str = DEFAULT_INDEX_PLAN,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Emit an index-only JSONL plan for one prior public crawl.

    This never downloads WARCs, never copies a second parquet mirror, never
    contacts the URL Index, and never writes a URL dump. The next ingest, if
    operators run it, is a tens-of-MB allowlisted-host JSONL like the existing
    18MB inbox — not another 169G table.
    """

    if not dry_run:
        raise CommonCrawlLakeError(
            "plan-index-ingest is index-only JSONL; pass --dry-run. "
            "WARC download and a second parquet mirror are forbidden"
        )
    raw = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigurationError("index plan must be a JSON object")
    config = load_config(config_path)
    crawls = raw.get("planned_crawls")
    if not isinstance(crawls, list) or not crawls:
        raise ConfigurationError("index plan is missing planned_crawls")
    queries = []
    for crawl in crawls:
        if not isinstance(crawl, str):
            raise ConfigurationError("planned crawl ids must be strings")
        crawl_id = _crawl(crawl, None)
        queries.append(
            {
                "crawl": crawl_id,
                "mode": "index_only_jsonl",
                "index_url": f"{config.index_base_url}{crawl_id}-index",
                "n_targets": len(config.targets),
                "expected_volume": raw.get("expected_volume")
                or "tens of MB, like the existing 18MB inbox",
                "download_warc": False,
                "parquet_mirror": False,
                "emit_url_dump": False,
            }
        )
    return {
        "schema": raw.get("schema") or "palimpsest-common-crawl-index-plan/v1",
        "collector": "common-crawl-lake",
        "command": "plan-index-ingest",
        "mode": "index_only_jsonl",
        "dry_run": True,
        "status": "planned",
        "download_warc": False,
        "parquet_mirror": False,
        "commit_url_dumps": False,
        "n_targets": len(config.targets),
        "n_crawls": len(queries),
        "planned_crawls": [row["crawl"] for row in queries],
        "current_in_lake": raw.get("current_in_lake"),
        "minimum_prior_crawls_for_scores": int(
            raw.get("minimum_prior_crawls_for_scores") or 6
        ),
        "allowlist_source": raw.get("allowlist_source")
        or "config/common_crawl_targets.json",
        "rights": raw.get("rights")
        or {
            "training_use": "derived_only",
            "raw_text_policy": "excluded",
            "url_list_policy": "do_not_publish",
        },
        "queries": queries,
    }


def _strict_json_bytes(payload: bytes, *, maximum: int, label: str) -> Any:
    if len(payload) > maximum:
        raise LimitExceeded(f"{label} exceeds the {maximum} byte cap")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{label} is not UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ValidationError:
        raise
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"{label} is not valid JSON") from exc


def probe_exact_url(
    url: str,
    *,
    config_path: Path | str = DEFAULT_CONFIG,
    limit: int = 10,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
    fetch: Callable[..., bytes] = safe_fetch_bytes,
) -> dict[str, Any]:
    """Perform one bounded exact-URL CDX diagnostic against the latest crawl.

    Wildcards are refused. This path proves reachability and exact-URL coverage; it is
    intentionally not the bulk ingestion mechanism.
    """

    config = load_config(config_path)
    canonical_url, host = _canonical_url(url, maximum_chars=config.limits.url_chars)
    if host not in config.target_by_host:
        raise ValidationError("probe URL host is not in the reviewed target allowlist")
    if "*" in canonical_url:
        raise ValidationError("exact URL probe refuses wildcards")
    if type(limit) is not int or limit < 1 or limit > 25:
        raise ValidationError("probe limit must be from 1 to 25")
    gate = kill_switch or KillSwitch()
    gate.require_live()
    ceiling = rate_ceiling or RateCeiling(
        rate=config.limits.archive_requests_per_second, capacity=1
    )
    ceiling.acquire()
    try:
        catalog_raw = fetch(
            config.collection_catalog_url,
            max_bytes=2 * 1024 * 1024,
            timeout=config.limits.network_timeout_seconds,
            max_redirects=0,
        )
    except Exception as exc:
        raise TransportError(f"Common Crawl collection catalog fetch failed: {exc}") from exc
    catalog = _strict_json_bytes(
        catalog_raw, maximum=2 * 1024 * 1024, label="Common Crawl collection catalog"
    )
    if not isinstance(catalog, list) or not catalog:
        raise ValidationError("Common Crawl collection catalog is empty or malformed")
    collection = catalog[0]
    if type(collection) is not dict:
        raise ValidationError("Common Crawl latest collection entry is malformed")
    collection_id = str(collection.get("id") or "")
    if not _CRAWL_RE.fullmatch(collection_id):
        raise ValidationError("Common Crawl latest collection id is invalid")
    params = urlencode(
        {
            "url": canonical_url,
            "output": "json",
            "filter": "status:200",
            "limit": str(limit),
        }
    )
    endpoint = f"{config.index_base_url}{quote(collection_id, safe='')}-index?{params}"
    gate.require_live()
    ceiling.acquire()
    try:
        result_raw = fetch(
            endpoint,
            max_bytes=2 * 1024 * 1024,
            timeout=config.limits.network_timeout_seconds,
            max_redirects=0,
        )
    except Exception as exc:
        raise TransportError(f"Common Crawl exact URL probe failed: {exc}") from exc
    records = []
    for line_number, raw in enumerate(result_raw.splitlines(), 1):
        if not raw.strip():
            continue
        value = _strict_json_bytes(
            raw, maximum=64 * 1024, label=f"CDX result line {line_number}"
        )
        if type(value) is not dict:
            raise ValidationError(f"CDX result line {line_number} is not an object")
        records.append(value)
        if len(records) > limit:
            raise ValidationError("CDX returned more rows than the requested limit")
    return {
        "collector": "common-crawl-exact-url-probe",
        "status": "covered" if records else "no_capture",
        "collection": collection_id,
        "url_sha256": hashlib.sha256(canonical_url.encode("utf-8")).hexdigest(),
        "records": records,
        "absence_semantics": "no-capture-is-not-deletion",
    }


def _safe_relative_object_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValidationError("stored object path is unsafe")
    return path


def retrieve_warc_record(
    locator_sha256: str,
    *,
    config_path: Path | str = DEFAULT_CONFIG,
    warehouse: Path | str | None = None,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
    fetch: Callable[..., bytes] = safe_fetch_bytes,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch one explicitly selected compressed WARC record by byte range.

    The exact bytes are stored content-addressed under the private warehouse. This method
    never parses, executes, publishes, or marks the source bytes as training-eligible.
    """

    if type(locator_sha256) is not str or not _SHA256_RE.fullmatch(locator_sha256):
        raise ValidationError("locator_sha256 is invalid")
    config = load_config(config_path)
    root = warehouse_path(warehouse)
    gate = kill_switch or KillSwitch()
    gate.require_live()
    ceiling = rate_ceiling or RateCeiling(
        rate=config.limits.archive_requests_per_second, capacity=1
    )
    with _warehouse_lock(root):
        connection = _connect(root / DEFAULT_DATABASE_NAME)
        try:
            initialize_database(connection)
            observation = connection.execute(
                "SELECT * FROM observations WHERE locator_sha256 = ?", (locator_sha256,)
            ).fetchone()
            if observation is None:
                raise ValidationError("locator is not present in the reviewed warehouse")
            prior = connection.execute(
                "SELECT * FROM record_objects WHERE locator_sha256 = ?", (locator_sha256,)
            ).fetchone()
            if prior is not None:
                relative = _safe_relative_object_path(prior["relative_path"])
                object_path = root / relative
                raw = object_path.read_bytes()
                if (
                    len(raw) != int(prior["object_bytes"])
                    or hashlib.sha256(raw).hexdigest() != prior["object_sha256"]
                ):
                    raise ValidationError("retained WARC object fails its stored identity")
                return {
                    "status": "unchanged",
                    "locator_sha256": locator_sha256,
                    "object_sha256": prior["object_sha256"],
                    "object_bytes": prior["object_bytes"],
                    "path": str(object_path),
                }

            length = int(observation["warc_record_length"])
            offset = int(observation["warc_record_offset"])
            endpoint = config.data_base_url + observation["warc_filename"]
            gate.require_live()
            ceiling.acquire()
            try:
                raw = fetch(
                    endpoint,
                    max_bytes=length,
                    timeout=config.limits.network_timeout_seconds,
                    max_redirects=0,
                    headers={"Range": f"bytes={offset}-{offset + length - 1}"},
                )
            except Exception as exc:
                raise TransportError(f"Common Crawl WARC range fetch failed: {exc}") from exc
            if len(raw) != length:
                raise ValidationError(
                    f"WARC range returned {len(raw)} bytes, expected exactly {length}"
                )
            if not raw.startswith(b"\x1f\x8b"):
                raise ValidationError("WARC range is not a gzip member")
            digest = hashlib.sha256(raw).hexdigest()
            relative = Path("records") / "sha256" / digest[:2] / f"{digest}.warc.gz"
            destination = root / relative
            if destination.exists():
                existing = destination.read_bytes()
                if existing != raw:
                    raise ValidationError("content-addressed WARC object collision")
            else:
                _atomic_bytes(destination, raw)
            retrieved_at = _iso_now(now)
            connection.execute(
                """
                INSERT INTO record_objects (
                    locator_sha256, object_sha256, object_bytes, relative_path, retrieved_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (locator_sha256, digest, len(raw), str(relative), retrieved_at),
            )
            connection.commit()
            return {
                "status": "success",
                "locator_sha256": locator_sha256,
                "object_sha256": digest,
                "object_bytes": len(raw),
                "path": str(destination),
                "training_use": "metadata_only",
            }
        finally:
            connection.close()
