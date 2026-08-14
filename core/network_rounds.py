"""Frozen-panel, longitudinal network-round contract.

The current adapter imports the consented Inside View DNS reading.  Other
protocols remain explicit gated capabilities until a reviewed probe path
exists.  Public rows are target/protocol/vantage scoped and contain no probe ID
or field that purports to estimate a national censorship prevalence.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "network_panels.json"
DEFAULT_OUTPUT_PATH = ROOT / "readings" / "network-rounds-latest.json"
CONFIG_VERSION = "palimpsest-network-panels.v1"
SCHEMA_VERSION = "palimpsest-network-rounds.v1"


class NetworkRoundError(ValueError):
    """A panel definition, imported round, or public ledger failed closed."""


_ID_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_ROUND_ID_RE = re.compile(r"^round-[0-9a-f]{24}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PROTOCOLS = frozenset({"DNS", "HTTP", "HTTPS_TLS", "QUIC"})
_PROTOCOL_STATES = frozenset(
    {"collecting", "consent_review", "infrastructure_required"}
)
_ROLES = frozenset({"measurement", "boundary", "control"})
_TARGETS = {
    "archive.org": ("archive", "boundary"),
    "dns.google": ("control", "control"),
    "duckduckgo.com": ("search", "boundary"),
    "en.wikipedia.org": ("reference", "boundary"),
    "github.com": ("developer-platform", "boundary"),
    "one.one.one.one": ("control", "control"),
    "rsf.org": ("rights", "measurement"),
    "torproject.org": ("circumvention", "measurement"),
    "wikileaks.org": ("information", "measurement"),
    "www.hrw.org": ("rights", "measurement"),
    "zh.wikipedia.org": ("reference", "measurement"),
}

_CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "minimum_comparable_rounds",
        "synchronization_window_minutes",
        "minimum_inside_asns",
        "minimum_inside_regions",
        "external_control_countries",
        "protocols",
        "evidence_controls",
        "panel",
        "claim_boundary",
    }
)
_PROTOCOL_FIELDS = frozenset({"id", "state", "method", "limitation"})
_CONTROL_FIELDS = frozenset({"id", "artifact_url", "purpose"})
_PANEL_FIELDS = frozenset({"id", "version", "targets"})
_TARGET_CONFIG_FIELDS = frozenset({"domain", "category", "role"})
_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "generated_at",
        "panel",
        "scope",
        "method",
        "claim_boundary",
        "protocol_capabilities",
        "evidence_controls",
        "minimum_comparable_rounds",
        "n_rounds",
        "n_comparable_rounds",
        "longitudinal_status",
        "rounds",
    }
)
_PUBLIC_PANEL_FIELDS = frozenset(
    {"id", "version", "sha256", "target_count", "target_categories"}
)
_CAPABILITY_FIELDS = frozenset({"protocol", "state", "method", "limitation"})
_PUBLIC_CONTROL_FIELDS = frozenset({"id", "artifact_url", "purpose", "status"})
_ROUND_FIELDS = frozenset(
    {
        "round_id",
        "protocol",
        "method_id",
        "method_version",
        "started_at",
        "ended_at",
        "synchronization_status",
        "source_input_sha256",
        "control_state",
        "external_control_countries",
        "external_controls_complete",
        "inside_asns",
        "inside_regions",
        "geographic_coverage",
        "routing_control",
        "outage_control",
        "comparable",
        "comparability_failures",
        "targets",
    }
)
_GEOGRAPHY_FIELDS = frozenset(
    {"observed_asns", "required_asns", "observed_regions", "required_regions"}
)
_ROUTING_FIELDS = frozenset({"status", "method"})
_OUTAGE_FIELDS = frozenset(
    {"status", "artifact_url", "generated_at", "instruments_firing"}
)
_TARGET_FIELDS = frozenset(
    {
        "domain",
        "category",
        "role",
        "protocol",
        "attempted_vantages",
        "failed_vantages",
        "clean_vantages",
        "silent_vantages",
        "undetermined_vantages",
        "external_control_probes",
        "outcome",
        "statement",
    }
)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NetworkRoundError("network-round value is not canonical JSON") from exc


def canonical_json_bytes(value: Any) -> bytes:
    return _canonical_bytes(value)


def _exact(value: Any, fields: frozenset[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != fields:
        actual = set(value) if type(value) is dict else set()
        raise NetworkRoundError(
            f"{path} fields do not match contract "
            f"(missing={sorted(fields - actual)}, unknown={sorted(actual - fields)})"
        )
    return value


def _text(value: Any, path: str, *, maximum: int = 2_000) -> str:
    if type(value) is not str:
        raise NetworkRoundError(f"{path} must be text")
    value = unicodedata.normalize("NFC", value)
    if not value.strip() or len(value) > maximum:
        raise NetworkRoundError(f"{path} has invalid length")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise NetworkRoundError(f"{path} contains unsafe Unicode")
    return value


def _normalize_timestamp(value: Any, path: str) -> str:
    if type(value) is not str or len(value) > 40:
        raise NetworkRoundError(f"{path} is not a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NetworkRoundError(f"{path} is not a real timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NetworkRoundError(f"{path} has no timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or not _TS_RE.fullmatch(value):
        raise NetworkRoundError(f"{path} is not a canonical UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise NetworkRoundError(f"{path} is not a real timestamp") from exc
    return value


def _clock(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def _https_url(value: Any, path: str) -> str:
    value = _text(value, path, maximum=2_048)
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise NetworkRoundError(f"{path} is not a safe HTTPS URL")
    return value


def _domain(value: Any, path: str) -> str:
    value = _text(value, path, maximum=253).lower()
    try:
        encoded = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise NetworkRoundError(f"{path} is not a valid domain") from exc
    if encoded != value or value.startswith(".") or value.endswith("."):
        raise NetworkRoundError(f"{path} must be canonical ASCII")
    if any(not label or len(label) > 63 for label in value.split(".")):
        raise NetworkRoundError(f"{path} is invalid")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise NetworkRoundError(f"{path} cannot be an IP literal")


def _target_interpretation(
    domain: str,
    protocol: str,
    *,
    attempted: int,
    failed: int,
    clean: int,
) -> tuple[str, str]:
    if failed:
        return (
            "failed-under-method",
            f"{domain} returned failure evidence under this round's {protocol} "
            "method, same-round controls, and observed China cloud-ASN vantages.",
        )
    if clean:
        return (
            "clean-under-method",
            f"{domain} returned no classified failure evidence under this round's "
            f"{protocol} method and observed vantages.",
        )
    if attempted:
        return (
            "undetermined",
            f"{domain} produced no classifiable {protocol} result under this round's "
            "method and observed vantages.",
        )
    return (
        "not-observed",
        f"{domain} was not observed successfully in this {protocol} round.",
    )


def _round_identity(
    config: Mapping[str, Any],
    *,
    protocol: str,
    started_at: str,
    source_input_sha256: str,
) -> str:
    identity = {
        "panel_sha256": config["sha256"],
        "protocol": protocol,
        "observed_at": started_at,
        "source_input_sha256": source_input_sha256,
    }
    return "round-" + hashlib.sha256(_canonical_bytes(identity)).hexdigest()[:24]


def _expected_comparability_failures(
    row: Mapping[str, Any], config: Mapping[str, Any]
) -> list[str]:
    failures = []
    if not row["external_controls_complete"]:
        failures.append("external-controls-incomplete")
    if len(row["inside_asns"]) < config["minimum_inside_asns"]:
        failures.append("asn-coverage-below-minimum")
    if len(row["inside_regions"]) < config["minimum_inside_regions"]:
        failures.append("regional-coverage-below-minimum")
    duration_seconds = (
        _clock(row["ended_at"]) - _clock(row["started_at"])
    ).total_seconds()
    if row["synchronization_status"] == "window-not-recorded":
        failures.append("round-window-not-recorded")
    elif (
        row["synchronization_status"] != "within-15-minutes"
        or duration_seconds > config["synchronization_window_minutes"] * 60
    ):
        failures.append("synchronization-window-exceeded")
    if row["routing_control"]["status"] != "resolved":
        failures.append("routing-control-incomplete")
    outage = row["outage_control"]
    if outage["status"] == "unavailable" or outage["generated_at"] is None:
        failures.append("outage-control-unavailable")
    else:
        outage_clock = _clock(outage["generated_at"])
        start = _clock(row["started_at"])
        end = _clock(row["ended_at"])
        distance = max((start - outage_clock).total_seconds(), 0.0) + max(
            (outage_clock - end).total_seconds(), 0.0
        )
        if distance > config["synchronization_window_minutes"] * 60:
            failures.append("outage-control-outside-round")
    if any(target["external_control_probes"] < 1 for target in row["targets"]):
        failures.append("per-target-controls-incomplete")
    return sorted(failures)


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NetworkRoundError(f"cannot read network panel config: {path}") from exc
    if type(value) is not dict:
        raise NetworkRoundError("network panel config must be an object")
    return value


def load_network_panel_config(
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    top = dict(_exact(_read_json(Path(path)), _CONFIG_FIELDS, "config"))
    if top["schema_version"] != CONFIG_VERSION:
        raise NetworkRoundError("unsupported network panel config version")
    for field, expected in (
        ("minimum_comparable_rounds", 3),
        ("synchronization_window_minutes", 15),
        ("minimum_inside_asns", 3),
        ("minimum_inside_regions", 3),
    ):
        if top[field] != expected:
            raise NetworkRoundError(f"config.{field} broadens the v1 method")
    if top["external_control_countries"] != ["DE", "NL"]:
        raise NetworkRoundError("external controls do not match the reviewed v1 pair")
    protocols = top["protocols"]
    if type(protocols) is not list or len(protocols) != 4:
        raise NetworkRoundError("config.protocols must contain the four v1 protocols")
    protocol_ids = []
    for index, raw in enumerate(protocols):
        row = _exact(raw, _PROTOCOL_FIELDS, f"protocols[{index}]")
        if row["id"] not in _PROTOCOLS or row["state"] not in _PROTOCOL_STATES:
            raise NetworkRoundError("protocol id/state is invalid")
        _text(row["method"], "protocol.method")
        _text(row["limitation"], "protocol.limitation")
        protocol_ids.append(row["id"])
    if set(protocol_ids) != _PROTOCOLS or len(set(protocol_ids)) != 4:
        raise NetworkRoundError("protocol definitions are duplicate or incomplete")
    states = {row["id"]: row["state"] for row in protocols}
    if states != {
        "DNS": "collecting",
        "HTTP": "consent_review",
        "HTTPS_TLS": "consent_review",
        "QUIC": "infrastructure_required",
    }:
        raise NetworkRoundError("protocol capability states were broadened")
    controls = top["evidence_controls"]
    if type(controls) is not list or len(controls) != 3:
        raise NetworkRoundError("evidence controls are incomplete")
    control_ids = []
    for index, raw in enumerate(controls):
        row = _exact(raw, _CONTROL_FIELDS, f"evidence_controls[{index}]")
        control_ids.append(row["id"])
        _https_url(row["artifact_url"], "evidence_control.artifact_url")
        _text(row["purpose"], "evidence_control.purpose")
    if set(control_ids) != {
        "routing-outage",
        "archived-policy",
        "app-store-before-after",
    }:
        raise NetworkRoundError("evidence controls do not match v1")
    panel = _exact(top["panel"], _PANEL_FIELDS, "panel")
    if panel["id"] != "china-multiprotocol-core-v1" or panel["version"] != 1:
        raise NetworkRoundError("panel identity does not match v1")
    targets = panel["targets"]
    if type(targets) is not list or len(targets) != len(_TARGETS):
        raise NetworkRoundError("panel target set is incomplete")
    seen = {}
    for index, raw in enumerate(targets):
        row = _exact(raw, _TARGET_CONFIG_FIELDS, f"targets[{index}]")
        domain = _domain(row["domain"], "target.domain")
        if (row["category"], row["role"]) != _TARGETS.get(domain):
            raise NetworkRoundError("panel target category/role was broadened")
        seen[domain] = (row["category"], row["role"])
    if seen != _TARGETS or [row["domain"] for row in targets] != sorted(_TARGETS):
        raise NetworkRoundError("panel targets are not the exact sorted v1 set")
    boundary = _text(top["claim_boundary"], "claim_boundary")
    if "national censorship percentage" not in boundary:
        raise NetworkRoundError("claim boundary must prohibit national percentage claims")
    top["sha256"] = hashlib.sha256(_canonical_bytes(top)).hexdigest()
    return top


def _inside_view_round(
    inside_view: Mapping[str, Any],
    config: Mapping[str, Any],
    outage: Mapping[str, Any] | None,
) -> dict[str, Any]:
    required = {
        "generated_at",
        "method_version",
        "control",
        "domains",
        "panel_size",
    }
    if type(inside_view) is not dict or not required <= set(inside_view):
        raise NetworkRoundError("Inside View input is missing required fields")
    observed_at = _normalize_timestamp(inside_view["generated_at"], "inside_view.generated_at")
    if inside_view["method_version"] != 2:
        raise NetworkRoundError("unsupported Inside View method version")
    domains = inside_view["domains"]
    if type(domains) is not list or len(domains) != len(_TARGETS):
        raise NetworkRoundError("Inside View did not report the frozen target panel")
    by_domain = {}
    all_asns = set()
    all_regions = set()
    targets = []
    control_countries = set()
    ownership_states = []
    for raw in domains:
        if type(raw) is not dict:
            raise NetworkRoundError("Inside View domain row is not an object")
        domain = _domain(raw.get("domain"), "inside_view.domain")
        if domain in by_domain or domain not in _TARGETS:
            raise NetworkRoundError("Inside View domain is duplicate or outside the panel")
        by_domain[domain] = raw
        category, role = _TARGETS[domain]
        vantages = raw.get("vantages") or []
        if type(vantages) is not list:
            raise NetworkRoundError("Inside View vantages must be an array")
        for vantage in vantages:
            if type(vantage) is not dict:
                raise NetworkRoundError("Inside View vantage is not an object")
            asn = vantage.get("asn")
            city = vantage.get("city")
            if type(asn) is int and 1 <= asn <= 4_294_967_295:
                all_asns.add(asn)
            if type(city) is str and city.strip():
                all_regions.add(unicodedata.normalize("NFC", city.strip()))
        controls = raw.get("control_countries") or []
        if type(controls) is not list:
            raise NetworkRoundError("Inside View control countries must be an array")
        control_countries.update(value for value in controls if value in {"DE", "NL"})
        failed = raw.get("n_forged", 0)
        clean = raw.get("n_clean", 0)
        silent = raw.get("n_silent", 0)
        undetermined = raw.get("n_undetermined", 0) + raw.get("n_geo_variable", 0)
        counts = [failed, clean, silent, undetermined, raw.get("control_probes", 0)]
        if any(type(value) is not int or value < 0 for value in counts):
            raise NetworkRoundError("Inside View count is invalid")
        attempted = len(vantages)
        if failed + clean + silent + undetermined != attempted:
            raise NetworkRoundError("Inside View classifications do not equal vantages")
        outcome, statement = _target_interpretation(
            domain,
            "DNS",
            attempted=attempted,
            failed=failed,
            clean=clean,
        )
        targets.append(
            {
                "domain": domain,
                "category": category,
                "role": role,
                "protocol": "DNS",
                "attempted_vantages": attempted,
                "failed_vantages": failed,
                "clean_vantages": clean,
                "silent_vantages": silent,
                "undetermined_vantages": undetermined,
                "external_control_probes": raw.get("control_probes", 0),
                "outcome": outcome,
                "statement": statement,
            }
        )
        ownership_states.append(raw.get("ownership_resolved") is True)
    if set(by_domain) != set(_TARGETS):
        raise NetworkRoundError("Inside View panel is incomplete")
    outage_status = "unavailable"
    outage_generated = None
    instruments = None
    if outage is not None:
        if type(outage) is not dict:
            raise NetworkRoundError("outage control must be an object")
        outage_generated = _normalize_timestamp(outage.get("generated_at"), "outage.generated_at")
        instruments = outage.get("instruments_firing")
        if type(instruments) is not int or instruments < 0:
            raise NetworkRoundError("outage instruments_firing is invalid")
        outage_status = "no-wide-outage-observed" if instruments == 0 else "outage-signal-observed"
    input_hash = hashlib.sha256(_canonical_bytes(inside_view)).hexdigest()
    round_row = {
        "round_id": _round_identity(
            config,
            protocol="DNS",
            started_at=observed_at,
            source_input_sha256=input_hash,
        ),
        "protocol": "DNS",
        "method_id": "globalping-inside-view",
        "method_version": 2,
        "started_at": observed_at,
        "ended_at": observed_at,
        "synchronization_status": "window-not-recorded",
        "source_input_sha256": input_hash,
        "control_state": _text(
            inside_view["control"].get("state"), "inside_view.control.state", maximum=40
        ),
        "external_control_countries": sorted(control_countries),
        "external_controls_complete": set(control_countries) == {"DE", "NL"},
        "inside_asns": sorted(all_asns),
        "inside_regions": sorted(all_regions),
        "geographic_coverage": {
            "observed_asns": len(all_asns),
            "required_asns": config["minimum_inside_asns"],
            "observed_regions": len(all_regions),
            "required_regions": config["minimum_inside_regions"],
        },
        "routing_control": {
            "status": "resolved" if all(ownership_states) else "partial",
            "method": "Per-round origin-AS ownership comparison in Inside View v2.",
        },
        "outage_control": {
            "status": outage_status,
            "artifact_url": "https://palimpsest.info/readings/ioda-outages-latest.json",
            "generated_at": outage_generated,
            "instruments_firing": instruments,
        },
        "comparable": False,
        "comparability_failures": [],
        "targets": sorted(targets, key=lambda row: row["domain"]),
    }
    # v2 Inside View recorded per-target same-round controls, but not a bounded
    # global round start/end window. It is useful evidence and remains excluded
    # from longitudinal comparisons until all control requirements are recorded.
    failures = _expected_comparability_failures(round_row, config)
    round_row["comparability_failures"] = failures
    round_row["comparable"] = not failures
    return round_row


def build_network_rounds(
    inside_view: Mapping[str, Any],
    *,
    outage: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    prior_rounds: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a bounded ledger, deduplicating immutable round identities."""

    cfg = dict(config) if config is not None else load_network_panel_config()
    if "sha256" not in cfg:
        raise NetworkRoundError("network panel config must be loaded through its validator")
    current = _inside_view_round(inside_view, cfg, outage)
    rounds = [dict(row) for row in prior_rounds]
    for row in rounds:
        _validate_round(row, cfg)
    by_id = {row["round_id"]: row for row in rounds}
    prior_current = by_id.get(current["round_id"])
    if prior_current is not None:
        # The immutable identity belongs to the primary Inside View reading.
        # Outage control is a contextual snapshot captured when that round was
        # first admitted.  A later graph rebuild may see a newer IODA document;
        # that must neither mutate the receipt nor turn the same measurement
        # into a second round.  Still fail closed if any primary-derived field
        # changed under the same identity (for example, code changed without a
        # method-version bump).
        contextual_fields = {"outage_control", "comparability_failures", "comparable"}
        prior_primary = {
            key: value for key, value in prior_current.items() if key not in contextual_fields
        }
        current_primary = {
            key: value for key, value in current.items() if key not in contextual_fields
        }
        if prior_primary != current_primary:
            raise NetworkRoundError("round identity collision or prior-ledger corruption")
        current = prior_current
    by_id[current["round_id"]] = current
    rounds = sorted(by_id.values(), key=lambda row: (row["started_at"], row["round_id"]))
    if len(rounds) > 1_024:
        raise NetworkRoundError("network round ledger exceeds its v1 bound")
    comparable = [row for row in rounds if row["comparable"]]
    generated_at = max(row["ended_at"] for row in rounds)
    capabilities = [
        {
            "protocol": row["id"],
            "state": row["state"],
            "method": row["method"],
            "limitation": row["limitation"],
        }
        for row in cfg["protocols"]
    ]
    capability_order = {name: index for index, name in enumerate(("DNS", "HTTP", "HTTPS_TLS", "QUIC"))}
    capabilities.sort(key=lambda row: capability_order[row["protocol"]])
    control_status = {
        "routing-outage": (
            "joined"
            if any(
                row["outage_control"]["status"] != "unavailable" for row in rounds
            )
            else "unavailable"
        ),
        "archived-policy": "available-separate-artifact",
        "app-store-before-after": "available-separate-artifact",
    }
    controls = [
        {**row, "status": control_status[row["id"]]}
        for row in cfg["evidence_controls"]
    ]
    panel = cfg["panel"]
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "panel": {
            "id": panel["id"],
            "version": panel["version"],
            "sha256": cfg["sha256"],
            "target_count": len(panel["targets"]),
            "target_categories": sorted({row["category"] for row in panel["targets"]}),
        },
        "scope": (
            "Frozen-target network rounds with protocol, method, China cloud-ASN/region, "
            "same-round external-control, routing, and outage scope retained separately."
        ),
        "method": (
            "Normalize consented aggregate inputs into immutable round receipts; require "
            "a recorded synchronization window and declared geographic/control minima "
            "before a round enters a longitudinal comparison."
        ),
        "claim_boundary": cfg["claim_boundary"],
        "protocol_capabilities": capabilities,
        "evidence_controls": controls,
        "minimum_comparable_rounds": cfg["minimum_comparable_rounds"],
        "n_rounds": len(rounds),
        "n_comparable_rounds": len(comparable),
        "longitudinal_status": (
            "ready"
            if len(comparable) >= cfg["minimum_comparable_rounds"]
            else "warming_up"
        ),
        "rounds": rounds,
    }
    validate_network_rounds(document, config=cfg)
    return document


def _validate_round(row: Mapping[str, Any], config: Mapping[str, Any]) -> None:
    value = _exact(row, _ROUND_FIELDS, "round")
    if type(value["round_id"]) is not str or not _ROUND_ID_RE.fullmatch(value["round_id"]):
        raise NetworkRoundError("round_id is invalid")
    if value["protocol"] not in _PROTOCOLS:
        raise NetworkRoundError("round protocol is invalid")
    protocol_state = {
        item["id"]: item["state"] for item in config["protocols"]
    }[value["protocol"]]
    if protocol_state != "collecting":
        raise NetworkRoundError("round protocol is not approved for collection")
    if type(value["method_version"]) is not int or value["method_version"] < 1:
        raise NetworkRoundError("round method version is invalid")
    _text(value["method_id"], "round.method_id", maximum=80)
    if value["protocol"] == "DNS" and (
        value["method_id"] != "globalping-inside-view"
        or value["method_version"] != 2
    ):
        raise NetworkRoundError("DNS round method does not match the reviewed adapter")
    started = _timestamp(value["started_at"], "round.started_at")
    ended = _timestamp(value["ended_at"], "round.ended_at")
    if _clock(ended) < _clock(started):
        raise NetworkRoundError("round ended before it started")
    if value["synchronization_status"] not in {
        "window-not-recorded",
        "within-15-minutes",
    }:
        raise NetworkRoundError("round synchronization status is invalid")
    if type(value["source_input_sha256"]) is not str or not _SHA_RE.fullmatch(
        value["source_input_sha256"]
    ):
        raise NetworkRoundError("round source hash is invalid")
    if value["round_id"] != _round_identity(
        config,
        protocol=value["protocol"],
        started_at=started,
        source_input_sha256=value["source_input_sha256"],
    ):
        raise NetworkRoundError("round_id does not match its immutable input")
    _text(value["control_state"], "round.control_state", maximum=80)
    if (
        type(value["external_control_countries"]) is not list
        or value["external_control_countries"] != sorted(set(value["external_control_countries"]))
        or not set(value["external_control_countries"]) <= {"DE", "NL"}
        or type(value["external_controls_complete"]) is not bool
    ):
        raise NetworkRoundError("round external controls are invalid")
    if value["external_controls_complete"] is not (
        value["external_control_countries"] == config["external_control_countries"]
    ):
        raise NetworkRoundError("round external-control flag is inconsistent")
    if type(value["inside_asns"]) is not list or value["inside_asns"] != sorted(
        set(value["inside_asns"])
    ) or any(
        type(asn) is not int or not 1 <= asn <= 4_294_967_295
        for asn in value["inside_asns"]
    ):
        raise NetworkRoundError("round ASNs are invalid")
    if type(value["inside_regions"]) is not list or value["inside_regions"] != sorted(
        set(value["inside_regions"])
    ) or any(
        type(region) is not str or _text(region, "round.inside_region", maximum=128) != region
        for region in value["inside_regions"]
    ):
        raise NetworkRoundError("round regions are invalid")
    geography = _exact(value["geographic_coverage"], _GEOGRAPHY_FIELDS, "geography")
    expected_geo = {
        "observed_asns": len(value["inside_asns"]),
        "required_asns": config["minimum_inside_asns"],
        "observed_regions": len(value["inside_regions"]),
        "required_regions": config["minimum_inside_regions"],
    }
    if geography != expected_geo:
        raise NetworkRoundError("round geographic coverage is inconsistent")
    routing = _exact(value["routing_control"], _ROUTING_FIELDS, "routing_control")
    if routing["status"] not in {"resolved", "partial", "unavailable"}:
        raise NetworkRoundError("routing-control status is invalid")
    _text(routing["method"], "routing_control.method")
    outage = _exact(value["outage_control"], _OUTAGE_FIELDS, "outage_control")
    if outage["status"] not in {
        "unavailable",
        "no-wide-outage-observed",
        "outage-signal-observed",
    }:
        raise NetworkRoundError("outage-control status is invalid")
    _https_url(outage["artifact_url"], "outage_control.artifact_url")
    if outage["generated_at"] is not None:
        _timestamp(outage["generated_at"], "outage_control.generated_at")
    if outage["instruments_firing"] is not None and (
        type(outage["instruments_firing"]) is not int
        or outage["instruments_firing"] < 0
    ):
        raise NetworkRoundError("outage-control count is invalid")
    expected_outage_status = (
        "unavailable"
        if outage["instruments_firing"] is None
        else (
            "no-wide-outage-observed"
            if outage["instruments_firing"] == 0
            else "outage-signal-observed"
        )
    )
    if outage["status"] != expected_outage_status or (
        (outage["generated_at"] is None)
        is not (outage["instruments_firing"] is None)
    ):
        raise NetworkRoundError("outage-control status is inconsistent")
    if type(value["comparable"]) is not bool:
        raise NetworkRoundError("round comparable flag is invalid")
    failures = value["comparability_failures"]
    if type(failures) is not list or failures != sorted(set(failures)):
        raise NetworkRoundError("comparability failures are invalid")
    targets = value["targets"]
    if type(targets) is not list or len(targets) != len(_TARGETS):
        raise NetworkRoundError("round target panel is incomplete")
    domains = []
    for raw_target in targets:
        target = _exact(raw_target, _TARGET_FIELDS, "round.target")
        domain = _domain(target["domain"], "round.target.domain")
        domains.append(domain)
        if (target["category"], target["role"]) != _TARGETS.get(domain):
            raise NetworkRoundError("round target category/role is invalid")
        if target["protocol"] != value["protocol"]:
            raise NetworkRoundError("round target protocol does not match round")
        for field in (
            "attempted_vantages",
            "failed_vantages",
            "clean_vantages",
            "silent_vantages",
            "undetermined_vantages",
            "external_control_probes",
        ):
            if type(target[field]) is not int or target[field] < 0:
                raise NetworkRoundError(f"round target {field} is invalid")
        classified = sum(
            target[field]
            for field in (
                "failed_vantages",
                "clean_vantages",
                "silent_vantages",
                "undetermined_vantages",
            )
        )
        if classified != target["attempted_vantages"]:
            raise NetworkRoundError("round target classifications do not equal attempts")
        expected_outcome, expected_statement = _target_interpretation(
            domain,
            value["protocol"],
            attempted=target["attempted_vantages"],
            failed=target["failed_vantages"],
            clean=target["clean_vantages"],
        )
        if (
            target["outcome"] != expected_outcome
            or target["statement"] != expected_statement
        ):
            raise NetworkRoundError("round target interpretation is inconsistent")
    if domains != sorted(_TARGETS):
        raise NetworkRoundError("round target rows are not the frozen sorted panel")
    expected_failures = _expected_comparability_failures(value, config)
    if failures != expected_failures:
        raise NetworkRoundError("round comparability failures are inconsistent")
    if value["comparable"] is not (not expected_failures):
        raise NetworkRoundError("round comparable flag contradicts its controls")


def validate_network_rounds(
    value: Any, *, config: Mapping[str, Any] | None = None
) -> None:
    cfg = dict(config) if config is not None else load_network_panel_config()
    top = _exact(value, _TOP_FIELDS, "network_rounds")
    if top["schema_version"] != SCHEMA_VERSION:
        raise NetworkRoundError("unsupported network-round schema")
    generated_at = _timestamp(top["generated_at"], "generated_at")
    panel = _exact(top["panel"], _PUBLIC_PANEL_FIELDS, "panel")
    if panel != {
        "id": cfg["panel"]["id"],
        "version": cfg["panel"]["version"],
        "sha256": cfg["sha256"],
        "target_count": len(cfg["panel"]["targets"]),
        "target_categories": sorted(
            {row["category"] for row in cfg["panel"]["targets"]}
        ),
    }:
        raise NetworkRoundError("public panel receipt does not match the frozen config")
    _text(top["scope"], "scope")
    _text(top["method"], "method")
    if top["claim_boundary"] != cfg["claim_boundary"]:
        raise NetworkRoundError("public claim boundary does not match config")
    capabilities = top["protocol_capabilities"]
    expected_capabilities = [
        {
            "protocol": row["id"],
            "state": row["state"],
            "method": row["method"],
            "limitation": row["limitation"],
        }
        for row in cfg["protocols"]
    ]
    capability_order = {
        name: index
        for index, name in enumerate(("DNS", "HTTP", "HTTPS_TLS", "QUIC"))
    }
    expected_capabilities.sort(key=lambda row: capability_order[row["protocol"]])
    if capabilities != expected_capabilities:
        raise NetworkRoundError("protocol capability matrix is incomplete")
    for raw in capabilities:
        row = _exact(raw, _CAPABILITY_FIELDS, "protocol_capability")
        if row["protocol"] not in _PROTOCOLS or row["state"] not in _PROTOCOL_STATES:
            raise NetworkRoundError("protocol capability is invalid")
        _text(row["method"], "protocol_capability.method")
        _text(row["limitation"], "protocol_capability.limitation")
    controls = top["evidence_controls"]
    if type(controls) is not list or len(controls) != 3:
        raise NetworkRoundError("public evidence controls are incomplete")
    for raw in controls:
        row = _exact(raw, _PUBLIC_CONTROL_FIELDS, "evidence_control")
        _https_url(row["artifact_url"], "evidence_control.artifact_url")
        _text(row["purpose"], "evidence_control.purpose")
        if row["status"] not in {
            "joined",
            "unavailable",
            "available-separate-artifact",
        }:
            raise NetworkRoundError("evidence-control status is invalid")
    if top["minimum_comparable_rounds"] != 3:
        raise NetworkRoundError("minimum comparable rounds was broadened")
    rounds = top["rounds"]
    if type(rounds) is not list or not 1 <= len(rounds) <= 1_024:
        raise NetworkRoundError("round ledger is outside its bound")
    round_ids = []
    for row in rounds:
        _validate_round(row, cfg)
        round_ids.append(row["round_id"])
        if row["ended_at"] > generated_at:
            raise NetworkRoundError("round ends after ledger generation")
    if round_ids != [row["round_id"] for row in sorted(rounds, key=lambda row: (row["started_at"], row["round_id"]))] or len(round_ids) != len(set(round_ids)):
        raise NetworkRoundError("round ledger is not unique and chronological")
    comparable = sum(row["comparable"] for row in rounds)
    if top["n_rounds"] != len(rounds) or top["n_comparable_rounds"] != comparable:
        raise NetworkRoundError("round ledger counts are inconsistent")
    expected_status = "ready" if comparable >= 3 else "warming_up"
    if top["longitudinal_status"] != expected_status:
        raise NetworkRoundError("longitudinal status is inconsistent")
    control_status = {
        "routing-outage": (
            "joined"
            if any(
                row["outage_control"]["status"] != "unavailable" for row in rounds
            )
            else "unavailable"
        ),
        "archived-policy": "available-separate-artifact",
        "app-store-before-after": "available-separate-artifact",
    }
    expected_controls = [
        {**row, "status": control_status[row["id"]]}
        for row in cfg["evidence_controls"]
    ]
    if controls != expected_controls:
        raise NetworkRoundError("evidence-control matrix is inconsistent")
