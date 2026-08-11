"""Bounded, keyless metadata ingestion for five public research corpora.

The upstream repositories contain material that is useful to censorship research but is
not appropriate to mirror into a public dashboard: notice bodies and affected account
identifiers, URL test targets, censorship keywords, and circumvention routing rules.  This
collector therefore reads only Git's public smart-HTTP *ref advertisement*.  It records the
exact default-branch commit, a SHA-256 of the bounded advertisement, and aggregate ref
counts.  Individual ref names and every repository blob are discarded before publication.

There is no arbitrary-repository mode.  Both the committed configuration and this module's
independent allowlist must agree on all five repository identities, branches, licence
claims, status labels, and publication policy before the first request is made.  Endpoints
are constructed from that allowlist, redirects are disabled, credentials are never read,
and every response has both a per-source cap and a whole-run byte budget.

One invocation creates a single observation.  ``research-corpus-latest.json`` is replaced
atomically and ``research-corpus-history.jsonl`` is a logical append-only ledger: publishing
a new row atomically replaces the file with its previous bytes followed verbatim by one
canonical JSON line.  History is itself bounded and is never trimmed or rewritten in place.

Standard-library only apart from the repository's hardened ``core.safe_fetch`` transport.
Tests inject a byte-returning transport and perform no network I/O.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from core.governance import KillSwitch
from core.safe_fetch import ResponseTooLarge as SafeResponseTooLarge
from core.safe_fetch import safe_fetch_bytes


UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "research_corpus_sources.json"
DEFAULT_READINGS = ROOT / "readings"
LATEST_NAME = "research-corpus-latest.json"
HISTORY_NAME = "research-corpus-history.jsonl"
LOCK_NAME = ".research-corpus.lock"
TRANSACTION_NAME = ".research-corpus-transaction.json"
METHOD_VERSION = 1

REQUEST_USER_AGENT = (
    "palimpsest.info research-corpus collector/1.0 "
    "(metadata-only public Git reads; contact desk@palimpsest.info)"
)
REQUEST_HEADERS = {
    "User-Agent": REQUEST_USER_AGENT,
    "Accept": "application/x-git-upload-pack-advertisement",
}

_METHOD_PUBLIC_TEXT = {
    # Keep each released method's exact public declarations here. Historical rows are
    # immutable, so a later method may change its wording only by adding a new entry rather
    # than silently making the existing append-only ledger unreadable.
    1: {
        "source": "five statically allowlisted public Git repositories on github.com",
        "method": (
            "one bounded keyless Git smart-HTTP ref advertisement per source; exact "
            "default-branch cursor plus aggregate ref-count deltas"
        ),
        "scope": (
            "github/gov-takedowns, github/dmca, citizenlab/test-lists, "
            "citizenlab/chat-censorship, and gfwlist/gfwlist"
        ),
        "privacy": (
            "metadata only; repository blobs, notice bodies, affected identifiers, "
            "individual ref names, URL test targets, and keyword/routing corpora are "
            "neither requested nor published"
        ),
    },
}
PUBLIC_SOURCE = _METHOD_PUBLIC_TEXT[METHOD_VERSION]["source"]
PUBLIC_METHOD = _METHOD_PUBLIC_TEXT[METHOD_VERSION]["method"]
PUBLIC_SCOPE = _METHOD_PUBLIC_TEXT[METHOD_VERSION]["scope"]
PUBLIC_PRIVACY = _METHOD_PUBLIC_TEXT[METHOD_VERSION]["privacy"]

_CONFIG_MAX_BYTES = 128 * 1024
_SOURCE_REF_CAP_MAX = 8 * 1024 * 1024
_RUN_CAP_MAX = 32 * 1024 * 1024
_HISTORY_CAP_MAX = 256 * 1024 * 1024
_LATEST_CAP_MAX = 4 * 1024 * 1024
_PUBLIC_ROW_CAP_MAX = 1024 * 1024
_TIMEOUT_MAX_SECONDS = 60
_PACKET_CAP_MAX = 200_000
_REF_NAME_CAP_MAX = 4096
_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

# This is a second, code-level boundary around the committed config.  In particular, adding
# an endpoint-looking field to JSON cannot create egress: unknown fields are rejected and
# URLs are built only after exact equality with this table has been established.
_APPROVED_SOURCES: dict[str, dict[str, Any]] = {
    "github-gov-takedowns": {
        "repository": "github/gov-takedowns",
        "branch": "master",
        "corpus_kind": "government-takedown-notices",
        "source_status": "public-git-repository",
        "sensitivity_class": "notice-bodies-and-affected-identifiers",
        "publication_mode": "metadata-only",
        "license": {
            "status": "not-stated-by-upstream-repository",
            "spdx": None,
            "use_policy": "metadata-only-no-content-redistribution",
        },
    },
    "github-dmca": {
        "repository": "github/dmca",
        "branch": "master",
        "corpus_kind": "copyright-takedown-notices",
        "source_status": "public-git-repository",
        "sensitivity_class": "notice-bodies-and-affected-identifiers",
        "publication_mode": "metadata-only",
        "license": {
            "status": "not-stated-by-upstream-repository",
            "spdx": None,
            "use_policy": "metadata-only-no-content-redistribution",
        },
    },
    "citizenlab-test-lists": {
        "repository": "citizenlab/test-lists",
        "branch": "master",
        "corpus_kind": "censorship-measurement-test-lists",
        "source_status": "public-git-repository",
        "sensitivity_class": "url-test-target-corpus",
        "publication_mode": "metadata-only",
        "license": {
            "status": "declared-by-upstream-repository",
            "spdx": "CC-BY-NC-SA-4.0",
            "use_policy": "metadata-only-no-content-redistribution",
        },
    },
    "citizenlab-chat-censorship": {
        "repository": "citizenlab/chat-censorship",
        "branch": "master",
        "corpus_kind": "chat-censorship-research-artifacts",
        "source_status": "public-git-repository",
        "sensitivity_class": "keyword-and-trigger-corpus",
        "publication_mode": "metadata-only",
        "license": {
            "status": "declared-by-upstream-repository",
            "spdx": "CC-BY-NC-SA-4.0",
            "use_policy": "metadata-only-no-content-redistribution",
        },
    },
    "gfwlist-gfwlist": {
        "repository": "gfwlist/gfwlist",
        "branch": "master",
        "corpus_kind": "circumvention-routing-rules",
        "source_status": "public-git-repository",
        "sensitivity_class": "network-target-rule-corpus",
        "publication_mode": "metadata-only",
        "license": {
            "status": "declared-by-upstream-repository",
            "spdx": "LGPL-2.1-only",
            "use_policy": "metadata-only-no-content-redistribution",
        },
    },
}

_TOP_LEVEL_KEYS = {
    "schema_version",
    "collector",
    "method_version",
    "generated_at",
    "last_changed_at",
    "status",
    "source",
    "method",
    "scope",
    "privacy",
    "scope_sha256",
    "n_sources",
    "n_changed",
    "n_unchanged",
    "n_initial",
    "requests_made",
    "bytes_received",
    "sources",
    "snapshot_sha256",
}
_PUBLIC_SOURCE_KEYS = {
    "source_id",
    "repository",
    "branch",
    "corpus_kind",
    "source_status",
    "sensitivity_class",
    "publication_mode",
    "license",
    "commit",
    "previous_commit",
    "cursor_state",
    "last_changed_at",
    "advertisement_sha256",
    "retrieved_bytes",
    "advertised_refs",
    "ref_count_delta",
}
_REF_COUNT_KEYS = {
    "branches",
    "tags",
    "peeled_tags",
    "pull_requests",
    "other",
    "total",
}
_LICENSE_KEYS = {"status", "spdx", "use_policy"}
_FORBIDDEN_PUBLIC_KEYS = {
    "author",
    "body",
    "content",
    "email",
    "keyword",
    "keywords",
    "notice",
    "notice_body",
    "path",
    "paths",
    "ref_name",
    "ref_names",
    "target_url",
    "url",
    "urls",
    "user",
    "username",
}


class ResearchCorpusError(RuntimeError):
    """Base class for fail-loud corpus snapshot errors."""


class ConfigurationError(ResearchCorpusError):
    """The source allowlist or a hard limit is invalid."""


class LimitExceeded(ResearchCorpusError):
    """A response, run, output row, or local history crossed its declared cap."""


class ValidationError(ResearchCorpusError):
    """Git metadata or a prior local publication is malformed."""


class TransportError(ResearchCorpusError):
    """A fixed public Git endpoint could not be read."""

    def __init__(
        self,
        message: str,
        *,
        source_id: str = "",
        sources_completed: int = 0,
        requests_made: int = 0,
        bytes_received: int = 0,
    ):
        super().__init__(message)
        self.source_id = source_id
        self.sources_completed = sources_completed
        self.requests_made = requests_made
        self.bytes_received = bytes_received


class CollectionHalted(ResearchCorpusError):
    """The global gate engaged during a bounded collection round."""

    def __init__(
        self,
        *,
        sources_completed: int,
        requests_made: int,
        bytes_received: int,
    ):
        super().__init__("global kill switch engaged during collection")
        self.sources_completed = sources_completed
        self.requests_made = requests_made
        self.bytes_received = bytes_received


class PublicationBusy(ResearchCorpusError):
    """Another snapshot process already owns the publication lock."""


@dataclass(frozen=True)
class Limits:
    run_bytes: int
    history_bytes: int
    latest_bytes: int
    public_row_bytes: int
    network_timeout_seconds: int
    max_ref_packets: int
    max_ref_name_bytes: int


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    repository: str
    branch: str
    corpus_kind: str
    source_status: str
    sensitivity_class: str
    publication_mode: str
    license_status: str
    license_spdx: str | None
    license_use_policy: str
    ref_response_bytes: int

    @property
    def license_dict(self) -> dict[str, Any]:
        return {
            "status": self.license_status,
            "spdx": self.license_spdx,
            "use_policy": self.license_use_policy,
        }


@dataclass(frozen=True)
class CorpusConfig:
    user_agent: str
    limits: Limits
    sources: tuple[SourceConfig, ...]
    scope_sha256: str


@dataclass(frozen=True)
class RefSummary:
    commit: str
    branches: int
    tags: int
    peeled_tags: int
    pull_requests: int
    other: int

    def counts(self) -> dict[str, int]:
        values = {
            "branches": self.branches,
            "tags": self.tags,
            "peeled_tags": self.peeled_tags,
            "pull_requests": self.pull_requests,
            "other": self.other,
        }
        values["total"] = sum(values.values())
        return values


@dataclass
class RunBudget:
    maximum: int
    consumed: int = 0

    @property
    def remaining(self) -> int:
        return self.maximum - self.consumed

    def consume(self, amount: int) -> None:
        if isinstance(amount, bool) or amount < 0 or amount > self.remaining:
            raise LimitExceeded(f"run response budget of {self.maximum} bytes exceeded")
        self.consumed += amount


Fetch = Callable[..., bytes]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _require_exact_keys(raw: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(raw)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ConfigurationError(f"{label} keys differ (missing={missing}, extra={extra})")


def _bounded_int(
    raw: Mapping[str, Any],
    name: str,
    *,
    minimum: int,
    maximum: int,
    label: str = "limits",
) -> int:
    value = raw.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{label}.{name} must be an integer")
    if value < minimum or value > maximum:
        raise ConfigurationError(
            f"{label}.{name} must be between {minimum} and {maximum}"
        )
    return value


def _read_regular_file_bounded(
    path: Path,
    *,
    maximum: int,
    label: str,
    missing_ok: bool,
) -> bytes | None:
    """Open once, reject links/devices, and read no more than ``maximum + 1``."""

    try:
        before = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ValidationError(f"{label} does not exist") from None
    except OSError as exc:
        raise ValidationError(f"cannot inspect {label}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValidationError(f"{label} must be a regular file, not a link or device")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ValidationError(f"{label} does not exist") from None
    except OSError as exc:
        raise ValidationError(f"cannot open {label} safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValidationError(f"{label} changed before it was opened")
        if opened.st_size > maximum:
            raise LimitExceeded(f"{label} exceeds its {maximum} byte ceiling")
        chunks: list[bytes] = []
        consumed = 0
        while consumed <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - consumed))
            if not chunk:
                break
            chunks.append(chunk)
            consumed += len(chunk)
        if consumed > maximum:
            raise LimitExceeded(f"{label} exceeds its {maximum} byte ceiling")
        after = os.fstat(descriptor)
        if (
            after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or consumed != after.st_size
        ):
            raise ValidationError(f"{label} changed while it was being read")
    finally:
        os.close(descriptor)
    try:
        current = path.lstat()
    except OSError as exc:
        raise ValidationError(f"{label} changed while it was being read") from exc
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise ValidationError(f"{label} was replaced while it was being read")
    return b"".join(chunks)


def _read_config_document(path: Path) -> dict[str, Any]:
    try:
        raw = _read_regular_file_bounded(
            path,
            maximum=_CONFIG_MAX_BYTES,
            label="research-corpus config",
            missing_ok=False,
        )
        assert raw is not None
        document = json.loads(raw.decode("utf-8"))
    except LimitExceeded as exc:
        raise ConfigurationError("research-corpus config exceeds 128 KiB") from exc
    except ValidationError as exc:
        raise ConfigurationError(str(exc)) from exc
    except ConfigurationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ConfigurationError("cannot read research-corpus config") from exc
    if not isinstance(document, dict):
        raise ConfigurationError("research-corpus config must be an object")
    return document


def load_config(path: Path | str = DEFAULT_CONFIG) -> CorpusConfig:
    """Load the committed allowlist and enforce non-configurable safety ceilings."""

    document = _read_config_document(Path(path))
    _require_exact_keys(
        document,
        {"schema_version", "user_agent", "limits", "sources"},
        "config",
    )
    if document.get("schema_version") != 1:
        raise ConfigurationError("research-corpus config requires schema_version 1")
    if document.get("user_agent") != REQUEST_USER_AGENT:
        raise ConfigurationError("user_agent must be the contact-bearing collector identity")

    raw_limits = document.get("limits")
    if not isinstance(raw_limits, dict):
        raise ConfigurationError("limits must be an object")
    _require_exact_keys(
        raw_limits,
        {
            "run_bytes",
            "history_bytes",
            "latest_bytes",
            "public_row_bytes",
            "network_timeout_seconds",
            "max_ref_packets",
            "max_ref_name_bytes",
        },
        "limits",
    )
    limits = Limits(
        run_bytes=_bounded_int(
            raw_limits, "run_bytes", minimum=64 * 1024, maximum=_RUN_CAP_MAX
        ),
        history_bytes=_bounded_int(
            raw_limits, "history_bytes", minimum=1024 * 1024, maximum=_HISTORY_CAP_MAX
        ),
        latest_bytes=_bounded_int(
            raw_limits, "latest_bytes", minimum=64 * 1024, maximum=_LATEST_CAP_MAX
        ),
        public_row_bytes=_bounded_int(
            raw_limits,
            "public_row_bytes",
            minimum=16 * 1024,
            maximum=_PUBLIC_ROW_CAP_MAX,
        ),
        network_timeout_seconds=_bounded_int(
            raw_limits,
            "network_timeout_seconds",
            minimum=1,
            maximum=_TIMEOUT_MAX_SECONDS,
        ),
        max_ref_packets=_bounded_int(
            raw_limits, "max_ref_packets", minimum=16, maximum=_PACKET_CAP_MAX
        ),
        max_ref_name_bytes=_bounded_int(
            raw_limits, "max_ref_name_bytes", minimum=64, maximum=_REF_NAME_CAP_MAX
        ),
    )
    if limits.public_row_bytes > limits.latest_bytes:
        raise ConfigurationError("public_row_bytes cannot exceed latest_bytes")

    raw_sources = document.get("sources")
    if not isinstance(raw_sources, list):
        raise ConfigurationError("sources must be a list")
    if len(raw_sources) != len(_APPROVED_SOURCES):
        raise ConfigurationError("sources must declare every approved corpus exactly once")

    sources: list[SourceConfig] = []
    seen: set[str] = set()
    source_keys = {
        "id",
        "repository",
        "branch",
        "corpus_kind",
        "source_status",
        "sensitivity_class",
        "publication_mode",
        "license",
        "ref_response_bytes",
    }
    for index, raw_source in enumerate(raw_sources):
        label = f"sources[{index}]"
        if not isinstance(raw_source, dict):
            raise ConfigurationError(f"{label} must be an object")
        _require_exact_keys(raw_source, source_keys, label)
        source_id = raw_source.get("id")
        if not isinstance(source_id, str) or source_id not in _APPROVED_SOURCES:
            raise ConfigurationError(f"{label}.id is outside the approved source allowlist")
        if source_id in seen:
            raise ConfigurationError(f"duplicate source id {source_id!r}")
        seen.add(source_id)
        approved = _APPROVED_SOURCES[source_id]
        for field in (
            "repository",
            "branch",
            "corpus_kind",
            "source_status",
            "sensitivity_class",
            "publication_mode",
        ):
            if raw_source.get(field) != approved[field]:
                raise ConfigurationError(
                    f"{label}.{field} differs from the code-level source allowlist"
                )
        raw_license = raw_source.get("license")
        if not isinstance(raw_license, dict):
            raise ConfigurationError(f"{label}.license must be an object")
        _require_exact_keys(raw_license, _LICENSE_KEYS, f"{label}.license")
        if raw_license != approved["license"]:
            raise ConfigurationError(
                f"{label}.license differs from the reviewed source declaration"
            )
        response_cap = _bounded_int(
            raw_source,
            "ref_response_bytes",
            minimum=4096,
            maximum=_SOURCE_REF_CAP_MAX,
            label=label,
        )
        sources.append(
            SourceConfig(
                source_id=source_id,
                repository=approved["repository"],
                branch=approved["branch"],
                corpus_kind=approved["corpus_kind"],
                source_status=approved["source_status"],
                sensitivity_class=approved["sensitivity_class"],
                publication_mode=approved["publication_mode"],
                license_status=approved["license"]["status"],
                license_spdx=approved["license"]["spdx"],
                license_use_policy=approved["license"]["use_policy"],
                ref_response_bytes=response_cap,
            )
        )
    if seen != set(_APPROVED_SOURCES):
        raise ConfigurationError("source allowlist is incomplete")
    sources.sort(key=lambda item: item.source_id)
    if sum(source.ref_response_bytes for source in sources) > limits.run_bytes:
        raise ConfigurationError(
            "run_bytes must cover the sum of all per-source response ceilings"
        )

    normalized = {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "user_agent": REQUEST_USER_AGENT,
        "limits": {
            name: getattr(limits, name)
            for name in Limits.__dataclass_fields__
        },
        "sources": [
            {
                "id": source.source_id,
                "repository": source.repository,
                "branch": source.branch,
                "corpus_kind": source.corpus_kind,
                "source_status": source.source_status,
                "sensitivity_class": source.sensitivity_class,
                "publication_mode": source.publication_mode,
                "license": source.license_dict,
                "ref_response_bytes": source.ref_response_bytes,
            }
            for source in sources
        ],
    }
    scope_sha256 = hashlib.sha256(_canonical_json(normalized)).hexdigest()
    return CorpusConfig(REQUEST_USER_AGENT, limits, tuple(sources), scope_sha256)


def _validate_source_identity(source: SourceConfig) -> None:
    approved = _APPROVED_SOURCES.get(source.source_id)
    if approved is None:
        raise ConfigurationError("source is outside the approved source allowlist")
    expected = (
        approved["repository"],
        approved["branch"],
        approved["corpus_kind"],
        approved["source_status"],
        approved["sensitivity_class"],
        approved["publication_mode"],
        approved["license"]["status"],
        approved["license"]["spdx"],
        approved["license"]["use_policy"],
    )
    actual = (
        source.repository,
        source.branch,
        source.corpus_kind,
        source.source_status,
        source.sensitivity_class,
        source.publication_mode,
        source.license_status,
        source.license_spdx,
        source.license_use_policy,
    )
    if actual != expected:
        raise ConfigurationError("source identity differs from the approved source allowlist")


def ref_advertisement_url(source: SourceConfig) -> str:
    """Return the one approved Git smart-HTTP endpoint for ``source``."""

    _validate_source_identity(source)
    return (
        f"https://github.com/{source.repository}.git/info/refs"
        "?service=git-upload-pack"
    )


def _pkt_records(raw: bytes, *, maximum: int):
    """Yield strict protocol-v0 ``("data"|"flush", payload)`` records."""

    offset = 0
    packets = 0
    while offset < len(raw):
        if len(raw) - offset < 4:
            raise ValidationError("truncated Git packet length")
        prefix = raw[offset : offset + 4]
        offset += 4
        try:
            length = int(prefix.decode("ascii"), 16)
        except (UnicodeError, ValueError) as exc:
            raise ValidationError("invalid Git packet length") from exc
        packets += 1
        if packets > maximum:
            raise LimitExceeded(f"Git advertisement exceeds {maximum} packets")
        if length == 0:
            yield "flush", None
            continue
        if length in (1, 2):
            # Delimiter/response-end controls belong to protocol v2.  This endpoint is
            # intentionally parsed as a v0 advertisement, so accepting them would blur
            # message boundaries and permit concatenated responses.
            raise ValidationError("protocol-v2 control packet in Git v0 advertisement")
        if length < 4:
            raise ValidationError("reserved Git packet length")
        payload_length = length - 4
        end = offset + payload_length
        if end > len(raw):
            raise ValidationError("truncated Git packet payload")
        yield "data", raw[offset:end]
        offset = end
    if offset != len(raw):  # defensive; the loop normally makes this unreachable
        raise ValidationError("trailing bytes after Git packet stream")


def _validate_ref_name(raw: bytes, *, maximum_bytes: int) -> str:
    if not raw or len(raw) > maximum_bytes:
        raise ValidationError("Git ref name is empty or exceeds its byte cap")
    try:
        name = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("Git ref name is not valid UTF-8") from exc
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
        raise ValidationError("Git ref name contains a control character")
    if name != "HEAD" and not name.startswith("refs/"):
        raise ValidationError("unexpected pseudo-ref in Git advertisement")
    return name


def parse_ref_advertisement(
    raw: bytes,
    *,
    branch: str,
    max_packets: int,
    max_ref_name_bytes: int,
) -> RefSummary:
    """Parse a protocol-v0 upload-pack advertisement without retaining ref names."""

    if not isinstance(raw, bytes) or not raw:
        raise ValidationError("Git ref advertisement must be non-empty bytes")
    target = f"refs/heads/{branch}"
    head_commit: str | None = None
    target_commit: str | None = None
    symbolic_head: str | None = None
    counts = {
        "branches": 0,
        "tags": 0,
        "peeled_tags": 0,
        "pull_requests": 0,
        "other": 0,
    }
    ref_packets = 0

    state = "service"
    for kind, payload in _pkt_records(raw, maximum=max_packets):
        if state == "service":
            if kind != "data" or payload != b"# service=git-upload-pack\n":
                raise ValidationError("missing Git upload-pack service announcement")
            state = "service-flush"
            continue
        if state == "service-flush":
            if kind != "flush":
                raise ValidationError("Git service announcement is not flush-delimited")
            state = "refs"
            continue
        if state == "done":
            raise ValidationError("data appears after the terminal Git flush")
        if kind == "flush":
            if ref_packets == 0:
                raise ValidationError("Git advertisement contains no refs")
            state = "done"
            continue
        assert payload is not None  # kind == data
        ref_packets += 1
        line = payload[:-1] if payload.endswith(b"\n") else payload
        capabilities = b""
        if b"\x00" in line:
            line, capabilities = line.split(b"\x00", 1)
            if ref_packets != 1:
                raise ValidationError("Git capabilities appear after the first ref")
        if b" " not in line:
            raise ValidationError("malformed Git ref record")
        object_raw, ref_raw = line.split(b" ", 1)
        try:
            object_id = object_raw.decode("ascii")
        except UnicodeError as exc:
            raise ValidationError("Git object id is not ASCII") from exc
        if _SHA1.fullmatch(object_id) is None:
            raise ValidationError("Git object id is not a lowercase SHA-1")
        name = _validate_ref_name(ref_raw, maximum_bytes=max_ref_name_bytes)
        if name == "HEAD":
            if head_commit is not None or ref_packets != 1:
                raise ValidationError("duplicate or misplaced HEAD advertisement")
            head_commit = object_id
            try:
                capability_text = capabilities.decode("ascii")
            except UnicodeError as exc:
                raise ValidationError("Git capabilities are not ASCII") from exc
            for capability in capability_text.split():
                if capability.startswith("symref=HEAD:"):
                    symbolic_head = capability[len("symref=HEAD:") :]
            continue
        if b"\x00" in capabilities:
            raise ValidationError("malformed Git capability record")
        if name == target:
            if target_commit is not None:
                raise ValidationError("target branch appears more than once")
            target_commit = object_id
        if name.startswith("refs/heads/"):
            counts["branches"] += 1
        elif name.startswith("refs/tags/") and name.endswith("^{}"):
            counts["peeled_tags"] += 1
        elif name.startswith("refs/tags/"):
            counts["tags"] += 1
        elif name.startswith("refs/pull/"):
            counts["pull_requests"] += 1
        else:
            counts["other"] += 1

    if state != "done":
        raise ValidationError("Git advertisement is missing its terminal flush")
    if head_commit is None or target_commit is None:
        raise ValidationError("Git advertisement omitted HEAD or the approved branch")
    if symbolic_head != target:
        raise ValidationError("upstream default branch differs from the approved branch")
    if head_commit != target_commit:
        raise ValidationError("HEAD and the approved branch point to different commits")
    return RefSummary(commit=target_commit, **counts)


def _default_fetch(url: str, **kwargs: Any) -> bytes:
    return safe_fetch_bytes(url, **kwargs)


def _fetch_source(
    source: SourceConfig,
    config: CorpusConfig,
    budget: RunBudget,
    fetch: Fetch,
) -> tuple[bytes, RefSummary]:
    if budget.remaining <= 0:
        raise LimitExceeded("whole-run response budget is exhausted")
    cap = min(source.ref_response_bytes, budget.remaining)
    try:
        raw = fetch(
            ref_advertisement_url(source),
            max_bytes=cap,
            timeout=config.limits.network_timeout_seconds,
            max_redirects=0,
            headers=dict(REQUEST_HEADERS),
        )
    except SafeResponseTooLarge as exc:
        raise LimitExceeded(
            f"{source.source_id} response exceeded its {cap} byte ceiling"
        ) from exc
    except ResearchCorpusError:
        raise
    except Exception as exc:
        raise TransportError(
            f"{source.source_id} ref advertisement was unavailable",
            source_id=source.source_id,
            requests_made=1,
        ) from exc
    if not isinstance(raw, bytes):
        raise ValidationError(f"{source.source_id} transport returned non-bytes")
    if len(raw) > cap:
        raise LimitExceeded(
            f"{source.source_id} response exceeds its {cap} byte ceiling"
        )
    budget.consume(len(raw))
    summary = parse_ref_advertisement(
        raw,
        branch=source.branch,
        max_packets=config.limits.max_ref_packets,
        max_ref_name_bytes=config.limits.max_ref_name_bytes,
    )
    return raw, summary


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("snapshot time must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _previous_source_index(previous: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    if not previous:
        return {}
    sources = previous.get("sources")
    if not isinstance(sources, list):
        return {}
    return {
        str(item.get("source_id")): item
        for item in sources
        if isinstance(item, Mapping) and isinstance(item.get("source_id"), str)
    }


def _count_delta(
    current: Mapping[str, int], previous: Mapping[str, Any] | None
) -> dict[str, int] | None:
    if not isinstance(previous, Mapping) or set(previous) != _REF_COUNT_KEYS:
        return None
    out: dict[str, int] = {}
    for key in sorted(_REF_COUNT_KEYS):
        value = previous.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        out[key] = int(current[key]) - value
    return out


def _snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    unhashed = dict(snapshot)
    unhashed.pop("snapshot_sha256", None)
    return hashlib.sha256(_canonical_json(unhashed)).hexdigest()


def _assert_public_minimized(value: Any) -> None:
    """Reject fields that could turn a metadata heartbeat into corpus republication."""

    stack = [value]
    visited = 0
    while stack:
        item = stack.pop()
        visited += 1
        if visited > 200_000:
            raise ValidationError("public snapshot is structurally excessive")
        if isinstance(item, Mapping):
            for key, child in item.items():
                key_text = str(key).lower()
                if key_text in _FORBIDDEN_PUBLIC_KEYS:
                    raise ValidationError(
                        f"public research-corpus snapshot contains forbidden field {key_text!r}"
                    )
                stack.append(child)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, str):
            if len(item) > 4096:
                raise ValidationError("public research-corpus string exceeds 4096 characters")
            if "://" in item:
                raise ValidationError("public research-corpus snapshot contains a network URL")


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{label} must be a non-negative integer")
    return value


def _validate_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValidationError(f"{label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{label} must carry a timezone")
    return value


def _validate_ref_counts(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != _REF_COUNT_KEYS:
        raise ValidationError(f"{label} has an invalid aggregate ref schema")
    result = {
        key: _nonnegative_int(value[key], f"{label}.{key}")
        for key in _REF_COUNT_KEYS
    }
    subtotal = sum(result[key] for key in _REF_COUNT_KEYS if key != "total")
    if result["total"] != subtotal:
        raise ValidationError(f"{label}.total does not match its aggregate buckets")
    return result


def _validate_snapshot_shape(
    snapshot: Any,
    *,
    config: CorpusConfig | None = None,
    verify_hash: bool = True,
) -> None:
    if not isinstance(snapshot, dict) or set(snapshot) != _TOP_LEVEL_KEYS:
        raise ValidationError("research-corpus snapshot has an invalid top-level schema")
    method_version = snapshot.get("method_version")
    if (
        snapshot.get("schema_version") != 1
        or snapshot.get("collector") != "research-corpus"
        or isinstance(method_version, bool)
        or not isinstance(method_version, int)
        or method_version < 1
        or method_version > METHOD_VERSION
        or (config is not None and method_version != METHOD_VERSION)
        or snapshot.get("status") != "success"
    ):
        raise ValidationError("research-corpus snapshot identity is invalid")
    exact_public_text = (
        {
            "source": PUBLIC_SOURCE,
            "method": PUBLIC_METHOD,
            "scope": PUBLIC_SCOPE,
            "privacy": PUBLIC_PRIVACY,
        }
        if method_version == METHOD_VERSION
        else _METHOD_PUBLIC_TEXT.get(method_version)
    )
    if exact_public_text is None:
        raise ValidationError("research-corpus snapshot method has no historical schema")
    for field, expected in exact_public_text.items():
        if snapshot.get(field) != expected:
            raise ValidationError(f"research-corpus snapshot has invalid {field} text")
    _validate_timestamp(snapshot.get("generated_at"), "generated_at")
    _validate_timestamp(snapshot.get("last_changed_at"), "last_changed_at")
    if _SHA256.fullmatch(str(snapshot.get("scope_sha256") or "")) is None:
        raise ValidationError("snapshot scope_sha256 is invalid")
    if config is not None and snapshot["scope_sha256"] != config.scope_sha256:
        raise ValidationError("snapshot belongs to a different source scope")
    _assert_public_minimized(snapshot)

    raw_sources = snapshot.get("sources")
    if not isinstance(raw_sources, list):
        raise ValidationError("snapshot sources must be a list")
    n_sources = _nonnegative_int(snapshot.get("n_sources"), "n_sources")
    if n_sources != len(raw_sources) or n_sources != len(_APPROVED_SOURCES):
        raise ValidationError("snapshot source denominator is inconsistent")
    changed = _nonnegative_int(snapshot.get("n_changed"), "n_changed")
    unchanged = _nonnegative_int(snapshot.get("n_unchanged"), "n_unchanged")
    initial = _nonnegative_int(snapshot.get("n_initial"), "n_initial")
    if changed + unchanged + initial != n_sources:
        raise ValidationError("snapshot cursor-state counts are inconsistent")
    requests = _nonnegative_int(snapshot.get("requests_made"), "requests_made")
    if requests != n_sources:
        raise ValidationError("snapshot request count is inconsistent")
    bytes_received = _nonnegative_int(snapshot.get("bytes_received"), "bytes_received")

    seen: set[str] = set()
    state_counts = {"changed": 0, "unchanged": 0, "initial": 0}
    retrieved_total = 0
    config_index = {source.source_id: source for source in config.sources} if config else {}
    for item in raw_sources:
        if not isinstance(item, dict) or set(item) != _PUBLIC_SOURCE_KEYS:
            raise ValidationError("snapshot source entry has an invalid schema")
        source_id = item.get("source_id")
        if not isinstance(source_id, str) or source_id not in _APPROVED_SOURCES:
            raise ValidationError("snapshot source is outside the approved allowlist")
        if source_id in seen:
            raise ValidationError("snapshot repeats a source")
        seen.add(source_id)
        approved = _APPROVED_SOURCES[source_id]
        for field in (
            "repository",
            "branch",
            "corpus_kind",
            "source_status",
            "sensitivity_class",
            "publication_mode",
        ):
            if item.get(field) != approved[field]:
                raise ValidationError(f"snapshot source {source_id} has invalid {field}")
        if item.get("license") != approved["license"]:
            raise ValidationError(f"snapshot source {source_id} has invalid licence metadata")
        if _SHA1.fullmatch(str(item.get("commit") or "")) is None:
            raise ValidationError(f"snapshot source {source_id} has an invalid commit cursor")
        previous_commit = item.get("previous_commit")
        if previous_commit is not None and _SHA1.fullmatch(str(previous_commit)) is None:
            raise ValidationError(f"snapshot source {source_id} has an invalid prior cursor")
        cursor_state = item.get("cursor_state")
        if cursor_state not in state_counts:
            raise ValidationError(f"snapshot source {source_id} has an invalid cursor state")
        state_counts[str(cursor_state)] += 1
        if cursor_state == "initial" and previous_commit is not None:
            raise ValidationError("initial cursor cannot have a previous commit")
        if cursor_state == "unchanged" and previous_commit != item.get("commit"):
            raise ValidationError("unchanged cursor must equal its previous commit")
        if cursor_state == "changed" and (
            previous_commit is None or previous_commit == item.get("commit")
        ):
            raise ValidationError("changed cursor must differ from a previous commit")
        _validate_timestamp(item.get("last_changed_at"), f"{source_id}.last_changed_at")
        if _SHA256.fullmatch(str(item.get("advertisement_sha256") or "")) is None:
            raise ValidationError(f"snapshot source {source_id} has an invalid response hash")
        retrieved = _nonnegative_int(item.get("retrieved_bytes"), f"{source_id}.retrieved_bytes")
        if config is not None and retrieved > config_index[source_id].ref_response_bytes:
            raise ValidationError(f"snapshot source {source_id} exceeds its response cap")
        retrieved_total += retrieved
        current_counts = _validate_ref_counts(
            item.get("advertised_refs"), f"{source_id}.advertised_refs"
        )
        delta = item.get("ref_count_delta")
        if delta is not None:
            if not isinstance(delta, dict) or set(delta) != _REF_COUNT_KEYS:
                raise ValidationError(f"snapshot source {source_id} has invalid ref deltas")
            for key in _REF_COUNT_KEYS:
                value = delta.get(key)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValidationError(f"snapshot source {source_id} has non-integer ref delta")
            if delta["total"] != sum(
                delta[key] for key in _REF_COUNT_KEYS if key != "total"
            ):
                raise ValidationError(f"snapshot source {source_id} has inconsistent ref deltas")
        if current_counts["branches"] < 1:
            raise ValidationError(f"snapshot source {source_id} advertises no branches")
    if seen != set(_APPROVED_SOURCES):
        raise ValidationError("snapshot source allowlist is incomplete")
    if state_counts != {"changed": changed, "unchanged": unchanged, "initial": initial}:
        raise ValidationError("snapshot cursor-state totals do not match source entries")
    if retrieved_total != bytes_received:
        raise ValidationError("snapshot byte total does not match source entries")
    if verify_hash:
        digest = str(snapshot.get("snapshot_sha256") or "")
        if _SHA256.fullmatch(digest) is None or digest != _snapshot_digest(snapshot):
            raise ValidationError("snapshot SHA-256 does not verify")


def collect_snapshot(
    config: CorpusConfig,
    *,
    previous: Mapping[str, Any] | None = None,
    fetch: Fetch = _default_fetch,
    now: datetime | None = None,
    kill_switch: KillSwitch | None = None,
) -> dict[str, Any]:
    """Acquire exactly one bounded ref snapshot for the complete allowlist."""

    observed = now or datetime.now(UTC)
    observed_text = _utc_text(observed)
    if previous is not None:
        _validate_snapshot_shape(previous, config=config)
    previous_index = _previous_source_index(previous)
    budget = RunBudget(config.limits.run_bytes)
    source_rows: list[dict[str, Any]] = []

    for source in config.sources:
        if kill_switch is not None and kill_switch.is_halted():
            raise CollectionHalted(
                sources_completed=len(source_rows),
                requests_made=len(source_rows),
                bytes_received=budget.consumed,
            )
        try:
            raw, summary = _fetch_source(source, config, budget, fetch)
        except TransportError as exc:
            raise TransportError(
                str(exc),
                source_id=exc.source_id or source.source_id,
                sources_completed=len(source_rows),
                requests_made=len(source_rows) + max(1, exc.requests_made),
                bytes_received=budget.consumed,
            ) from exc
        current_counts = summary.counts()
        prior = previous_index.get(source.source_id)
        prior_commit = prior.get("commit") if prior else None
        if prior_commit is None:
            cursor_state = "initial"
        elif prior_commit == summary.commit:
            cursor_state = "unchanged"
        else:
            cursor_state = "changed"
        last_changed_at = (
            str(prior.get("last_changed_at"))
            if cursor_state == "unchanged" and prior and prior.get("last_changed_at")
            else observed_text
        )
        source_rows.append(
            {
                "source_id": source.source_id,
                "repository": source.repository,
                "branch": source.branch,
                "corpus_kind": source.corpus_kind,
                "source_status": source.source_status,
                "sensitivity_class": source.sensitivity_class,
                "publication_mode": source.publication_mode,
                "license": source.license_dict,
                "commit": summary.commit,
                "previous_commit": prior_commit,
                "cursor_state": cursor_state,
                "last_changed_at": last_changed_at,
                "advertisement_sha256": hashlib.sha256(raw).hexdigest(),
                "retrieved_bytes": len(raw),
                "advertised_refs": current_counts,
                "ref_count_delta": _count_delta(
                    current_counts, prior.get("advertised_refs") if prior else None
                ),
            }
        )

    state_counts = {
        state: sum(1 for item in source_rows if item["cursor_state"] == state)
        for state in ("changed", "unchanged", "initial")
    }
    any_movement = state_counts["changed"] > 0 or state_counts["initial"] > 0
    last_changed_at = (
        observed_text
        if any_movement or not previous
        else str(previous.get("last_changed_at") or previous.get("generated_at"))
    )
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "collector": "research-corpus",
        "method_version": METHOD_VERSION,
        "generated_at": observed_text,
        "last_changed_at": last_changed_at,
        "status": "success",
        "source": PUBLIC_SOURCE,
        "method": PUBLIC_METHOD,
        "scope": PUBLIC_SCOPE,
        "privacy": PUBLIC_PRIVACY,
        "scope_sha256": config.scope_sha256,
        "n_sources": len(source_rows),
        "n_changed": state_counts["changed"],
        "n_unchanged": state_counts["unchanged"],
        "n_initial": state_counts["initial"],
        "requests_made": len(source_rows),
        "bytes_received": budget.consumed,
        "sources": source_rows,
        "snapshot_sha256": "",
    }
    snapshot["snapshot_sha256"] = _snapshot_digest(snapshot)
    _validate_snapshot_shape(snapshot, config=config)
    if len(_canonical_json(snapshot)) + 1 > config.limits.public_row_bytes:
        raise LimitExceeded("public research-corpus snapshot exceeds its row ceiling")
    return snapshot


def _ensure_regular_or_missing(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValidationError(f"cannot inspect {label}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValidationError(f"{label} must be a regular file, not a link or device")


def _read_bounded(path: Path, *, maximum: int, label: str) -> bytes | None:
    return _read_regular_file_bounded(
        path,
        maximum=maximum,
        label=label,
        missing_ok=True,
    )


def load_previous_latest(
    readings: Path | str,
    config: CorpusConfig,
) -> dict[str, Any] | None:
    """Load and verify the prior cursor snapshot; reject corruption rather than reset it."""

    path = Path(readings) / LATEST_NAME
    raw = _read_bounded(path, maximum=config.limits.latest_bytes, label="latest snapshot")
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError("latest research-corpus snapshot is invalid JSON") from exc
    if isinstance(value, dict) and value.get("scope_sha256") != config.scope_sha256:
        # A reviewed config/method scope change starts a fresh cursor series.  The old row
        # remains in append-only history, while the next snapshot honestly reports initial.
        _validate_snapshot_shape(value)
        return None
    _validate_snapshot_shape(value, config=config)
    return value


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_regular_or_missing(path, path.name)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _validated_history_bytes(raw: bytes, config: CorpusConfig) -> list[dict[str, Any]]:
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise ValidationError("append-only history has a truncated final line")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(raw.splitlines(), 1):
        if not line:
            raise ValidationError("append-only history contains a blank line")
        if len(line) + 1 > config.limits.public_row_bytes:
            raise LimitExceeded(f"history row {number} exceeds the public row ceiling")
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValidationError(f"history row {number} is invalid JSON") from exc
        _validate_snapshot_shape(row)
        rows.append(row)
    return rows


def _latest_payload(candidate: Mapping[str, Any], config: CorpusConfig) -> bytes:
    payload = json.dumps(
        candidate,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    if len(payload) > config.limits.latest_bytes:
        raise LimitExceeded("latest research-corpus snapshot exceeds its byte ceiling")
    return payload


def _transaction_payload(candidate: Mapping[str, Any], config: CorpusConfig) -> bytes:
    payload = _canonical_json({
        "schema": "research-corpus-publication/v1",
        "snapshot": candidate,
    }) + b"\n"
    if len(payload) > config.limits.latest_bytes:
        raise LimitExceeded("research-corpus transaction exceeds its byte ceiling")
    return payload


def _remove_transaction(path: Path) -> None:
    _ensure_regular_or_missing(path, "research-corpus transaction")
    try:
        path.unlink()
    except FileNotFoundError:
        return
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path.parent, directory_flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _recover_publication_unlocked(root: Path, config: CorpusConfig) -> bool:
    """Finish an interrupted two-file commit while the publication lock is held."""

    transaction_path = root / TRANSACTION_NAME
    raw = _read_bounded(
        transaction_path,
        maximum=config.limits.latest_bytes,
        label="research-corpus transaction",
    )
    if raw is None:
        return False
    try:
        transaction = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError("research-corpus transaction is invalid JSON") from exc
    if (
        not isinstance(transaction, dict)
        or set(transaction) != {"schema", "snapshot"}
        or transaction.get("schema") != "research-corpus-publication/v1"
        or not isinstance(transaction.get("snapshot"), dict)
    ):
        raise ValidationError("research-corpus transaction envelope is invalid")
    candidate = dict(transaction["snapshot"])
    # The transaction may have been created immediately before a method/config rollout.
    # Complete the already-validated historical commit first, then let scope comparison
    # start the new method with initial cursors.
    _validate_snapshot_shape(candidate)
    row = _canonical_json(candidate) + b"\n"
    if len(row) > config.limits.public_row_bytes:
        raise LimitExceeded("pending research-corpus row exceeds its byte ceiling")
    latest_payload = _latest_payload(candidate, config)

    history_path = root / HISTORY_NAME
    existing = _read_bounded(
        history_path,
        maximum=config.limits.history_bytes,
        label="append-only history",
    ) or b""
    rows = _validated_history_bytes(existing, config)
    candidate_hash = candidate["snapshot_sha256"]
    if rows and any(row_item.get("snapshot_sha256") == candidate_hash for row_item in rows[:-1]):
        raise ValidationError("pending transaction is not the final history row")
    if not rows or rows[-1].get("snapshot_sha256") != candidate_hash:
        if len(existing) + len(row) > config.limits.history_bytes:
            raise LimitExceeded("append-only history cannot recover pending transaction")
        _atomic_write(history_path, existing + row)
    _atomic_write(root / LATEST_NAME, latest_payload)
    _remove_transaction(transaction_path)
    return True


def _publish_snapshot_unlocked(
    snapshot: Mapping[str, Any],
    config: CorpusConfig,
    *,
    root: Path,
) -> dict[str, bool]:
    """Commit latest/history under the caller-held lock with a recovery journal."""

    candidate = dict(snapshot)
    _validate_snapshot_shape(candidate, config=config)
    row = _canonical_json(candidate) + b"\n"
    if len(row) > config.limits.public_row_bytes:
        raise LimitExceeded("public research-corpus snapshot exceeds its row ceiling")
    # Compute and bound *all* bytes before the first durable mutation.  Once the journal
    # lands, every subsequent crash point is recoverable by _recover_publication_unlocked.
    latest_payload = _latest_payload(candidate, config)
    transaction_payload = _transaction_payload(candidate, config)

    root.mkdir(parents=True, exist_ok=True)
    history_path = root / HISTORY_NAME
    existing = _read_bounded(
        history_path,
        maximum=config.limits.history_bytes,
        label="append-only history",
    )
    existing = existing or b""
    rows = _validated_history_bytes(existing, config)
    history_appended = not (
        rows and rows[-1].get("snapshot_sha256") == candidate.get("snapshot_sha256")
    )
    history_payload = existing
    if history_appended:
        if len(existing) + len(row) > config.limits.history_bytes:
            raise LimitExceeded("append-only history has reached its byte ceiling")
        history_payload = existing + row

    transaction_path = root / TRANSACTION_NAME
    _atomic_write(transaction_path, transaction_payload)
    # Prefix preservation is the append-only invariant.  The journal makes either
    # single-file commit recoverable if the second replace or directory fsync fails.
    if history_appended:
        _atomic_write(history_path, history_payload)
    _atomic_write(root / LATEST_NAME, latest_payload)
    _remove_transaction(transaction_path)
    return {"history_appended": history_appended, "latest_updated": True}


def publish_snapshot(
    snapshot: Mapping[str, Any],
    config: CorpusConfig,
    *,
    readings: Path | str = DEFAULT_READINGS,
) -> dict[str, bool]:
    """Race-safe public wrapper for one recoverable latest/history commit."""

    root = Path(readings)
    with _PublicationLock(root):
        _recover_publication_unlocked(root, config)
        # Refuse to use a corrupt latest as the predecessor. A scope mismatch is valid and
        # returns None; corruption without a transaction remains a hard failure.
        load_previous_latest(root, config)
        return _publish_snapshot_unlocked(snapshot, config, root=root)


class _PublicationLock:
    def __init__(self, root: Path):
        self.path = root / LOCK_NAME
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.path, flags, 0o644)
        except OSError as exc:
            raise ValidationError("research-corpus lock cannot be opened safely") from exc
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValidationError("research-corpus lock is not a regular file")
        try:
            self.handle = os.fdopen(descriptor, "a+b")
        except Exception:
            os.close(descriptor)
            raise
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise PublicationBusy("another research-corpus snapshot is active") from exc
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise ValidationError("research-corpus lock cannot be acquired") from exc
        return self

    def __exit__(self, *_args):
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def run_snapshot(
    *,
    config_path: Path | str = DEFAULT_CONFIG,
    readings: Path | str = DEFAULT_READINGS,
    fetch: Fetch = _default_fetch,
    now: datetime | None = None,
    kill_switch: KillSwitch | None = None,
) -> dict[str, Any]:
    """Perform and publish one complete snapshot, or abstain before egress if halted."""

    config = load_config(config_path)
    gate = kill_switch or KillSwitch()
    if gate.is_halted():
        return {
            "collector": "research-corpus",
            "status": "halted",
            "n_sources": 0,
            "requests_made": 0,
            "bytes_received": 0,
            "error": "global kill switch is engaged",
        }
    root = Path(readings)
    with _PublicationLock(root):
        _recover_publication_unlocked(root, config)
        previous = load_previous_latest(root, config)
        try:
            snapshot = collect_snapshot(
                config,
                previous=previous,
                fetch=fetch,
                now=now,
                kill_switch=gate,
            )
        except CollectionHalted as exc:
            return {
                "collector": "research-corpus",
                "status": "halted",
                "generated_at": _utc_text(now or datetime.now(UTC)),
                "n_sources": 0,
                "sources_expected": len(config.sources),
                "sources_completed": exc.sources_completed,
                "requests_made": exc.requests_made,
                "bytes_received": exc.bytes_received,
                "error": "global kill switch engaged during collection; no snapshot was published",
            }
        except TransportError as exc:
            # A transport outage is an abstention, never a five-source zero.  Preserve the
            # last good latest/history files and return a small operational status for logs.
            return {
                "collector": "research-corpus",
                "status": "skipped",
                "generated_at": _utc_text(now or datetime.now(UTC)),
                "n_sources": 0,
                "sources_expected": len(config.sources),
                "sources_completed": exc.sources_completed,
                "requests_made": exc.requests_made,
                "bytes_received": exc.bytes_received,
                "error": (
                    f"{exc.source_id or 'upstream Git'} unavailable; "
                    "no snapshot was published"
                ),
            }
        if gate.is_halted():
            return {
                "collector": "research-corpus",
                "status": "halted",
                "generated_at": snapshot["generated_at"],
                "n_sources": 0,
                "sources_expected": len(config.sources),
                "sources_completed": len(config.sources),
                "requests_made": snapshot["requests_made"],
                "bytes_received": snapshot["bytes_received"],
                "error": "global kill switch engaged before publication; no snapshot was published",
            }
        publication = _publish_snapshot_unlocked(snapshot, config, root=root)
    return {**snapshot, "publication": publication}


__all__ = [
    "CorpusConfig",
    "ConfigurationError",
    "DEFAULT_CONFIG",
    "DEFAULT_READINGS",
    "HISTORY_NAME",
    "LATEST_NAME",
    "LimitExceeded",
    "METHOD_VERSION",
    "PublicationBusy",
    "REQUEST_HEADERS",
    "REQUEST_USER_AGENT",
    "RefSummary",
    "ResearchCorpusError",
    "SourceConfig",
    "TransportError",
    "ValidationError",
    "collect_snapshot",
    "load_config",
    "load_previous_latest",
    "parse_ref_advertisement",
    "publish_snapshot",
    "ref_advertisement_url",
    "run_snapshot",
]
