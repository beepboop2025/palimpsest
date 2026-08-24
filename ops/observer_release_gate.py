#!/usr/bin/env python3
"""Validate fresh observer reports against a reviewed release carry-forward.

The operational observers deliberately return exit 2 while evidence is stale.
This verifier does not turn that exit into success.  It proves, separately,
that every problem in a fresh post-release report was present before the
release and followed an explicitly reviewed baseline-state to exact-final-state
carry-forward rule.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASELINE_SCHEMA = "palimpsest-observer-release-baseline.v1"
POLICY_SCHEMA = "palimpsest-observer-release-policy.v1"
PROOF_SCHEMA = "palimpsest-observer-release-proof.v1"
WATCHDOG_SCHEMA = "palimpsest-freshness-watchdog.v1"
WITNESS_SCHEMA = "palimpsest-witness-status.v1"
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_BASELINE_BYTES = 64 * 1024
MAX_PROBLEMS = 128
MAX_REPORT_AGE = timedelta(minutes=5)
MAX_FUTURE_SKEW = timedelta(minutes=1)
MAX_TRANSACTION_AGE = timedelta(hours=8)


class GateError(ValueError):
    """A release observer proof did not satisfy the fail-closed contract."""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise GateError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise GateError(f"non-finite JSON number: {value}")


def _decode_json(
    payload: bytes, path: Path, *, maximum: int = MAX_DOCUMENT_BYTES
) -> dict[str, Any]:
    if not payload or len(payload) > maximum:
        raise GateError(f"invalid document size: {path}")
    try:
        value = json.loads(
            payload.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise GateError(f"invalid JSON document: {path}") from error
    if type(value) is not dict:
        raise GateError(f"document root is not an object: {path}")
    return value


def _load_json(path: Path, *, maximum: int = MAX_DOCUMENT_BYTES) -> dict[str, Any]:
    return _decode_json(path.read_bytes(), path, maximum=maximum)


def _timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise GateError(f"{field} is not a bounded timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GateError(f"{field} is not an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GateError(f"{field} has no timezone")
    return parsed.astimezone(timezone.utc)


def _now(value: str | None) -> datetime:
    return _timestamp(value, field="--now") if value else datetime.now(timezone.utc)


def _fresh_report(generated_at: Any, *, now: datetime) -> datetime:
    observed = _timestamp(generated_at, field="generated_at")
    if observed > now + MAX_FUTURE_SKEW or now - observed > MAX_REPORT_AGE:
        raise GateError("observer report is not fresh")
    return observed


def _bounded_identifier(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 160
        or any(character.isspace() for character in value)
    ):
        raise GateError(f"invalid {field}")
    return value


def _bounded_hex(value: Any, *, length: int, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise GateError(f"invalid {field}")
    return value


def _watchdog_identity(problem: Any) -> tuple[Any, ...]:
    keys = {"condition", "required", "scope", "state", "subject"}
    if type(problem) is not dict or set(problem) != keys:
        raise GateError("watchdog problem shape is invalid")
    condition = _bounded_identifier(problem["condition"], field="condition")
    scope = _bounded_identifier(problem["scope"], field="scope")
    subject = _bounded_identifier(problem["subject"], field="subject")
    state = _bounded_identifier(problem["state"], field="state")
    if type(problem["required"]) is not bool or condition != f"{scope}/{subject}":
        raise GateError("watchdog problem identity is invalid")
    return condition, scope, subject, state, problem["required"]


def _witness_identity(problem: Any) -> tuple[Any, ...]:
    keys = {"condition", "message", "state"}
    if type(problem) is not dict or set(problem) != keys:
        raise GateError("witness freshness problem shape is invalid")
    condition = _bounded_identifier(problem["condition"], field="condition")
    state = _bounded_identifier(problem["state"], field="state")
    message = problem["message"]
    if not isinstance(message, str) or not message or len(message) > 1024:
        raise GateError("witness freshness message is invalid")
    return condition, state


def _stable_identity(observer: str, identity: tuple[Any, ...]) -> tuple[Any, ...]:
    if observer == "watchdog":
        condition, scope, subject, _state, required = identity
        return condition, scope, subject, required
    condition, _state = identity
    return (condition,)


def _identity_state(observer: str, identity: tuple[Any, ...]) -> str:
    return identity[3] if observer == "watchdog" else identity[1]


def _encoded_identity(observer: str, value: Any) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise GateError("observer baseline identities are invalid")
    if observer == "watchdog" and len(value) == 5:
        condition, scope, subject, state, required = value
        return _watchdog_identity(
            {
                "condition": condition,
                "scope": scope,
                "subject": subject,
                "state": state,
                "required": required,
            }
        )
    if observer == "witness" and len(value) == 2:
        condition, state = value
        return _witness_identity(
            {"condition": condition, "state": state, "message": "baseline"}
        )
    raise GateError("observer baseline identities are invalid")


def _unique_identities(observer: str, values: Any, identity) -> list[tuple[Any, ...]]:
    if not isinstance(values, list) or len(values) > MAX_PROBLEMS:
        raise GateError("observer problem inventory is invalid")
    identities = [identity(value) for value in values]
    if len(set(identities)) != len(identities):
        raise GateError("observer problem identities are duplicated")
    stable = [_stable_identity(observer, value) for value in identities]
    if len(set(stable)) != len(stable):
        raise GateError("observer problem conditions are duplicated")
    return sorted(identities)


def _report(
    observer: str, document: dict[str, Any], *, now: datetime
) -> tuple[datetime, str, list[tuple[Any, ...]]]:
    generated_at = _fresh_report(document.get("generated_at"), now=now)
    invocation_id = _bounded_hex(
        document.get("invocation_id"), length=32, field="observer invocation_id"
    )
    status = document.get("status")
    active_count = document.get("active_count")
    if type(active_count) is not int or not 0 <= active_count <= MAX_PROBLEMS:
        raise GateError("observer active_count is invalid")

    if observer == "watchdog":
        if document.get("schema_version") != WATCHDOG_SCHEMA:
            raise GateError("watchdog status schema is invalid")
        identities = _unique_identities(
            observer, document.get("problems"), _watchdog_identity
        )
        if active_count != len(identities):
            raise GateError("watchdog active_count does not match problems")
    elif observer == "witness":
        if document.get("schema_version") != WITNESS_SCHEMA:
            raise GateError("witness status schema is invalid")
        chain_alerts = document.get("chain_alerts")
        if not isinstance(chain_alerts, list) or len(chain_alerts) > MAX_PROBLEMS:
            raise GateError("witness chain alert inventory is invalid")
        if chain_alerts:
            raise GateError("witness chain, prefix, or fetch alert blocks release")
        if document.get("inventory_complete") is not True:
            raise GateError("witness status inventory is incomplete")
        identities = _unique_identities(
            observer, document.get("freshness_problems"), _witness_identity
        )
        if active_count != len(identities):
            raise GateError("witness active_count does not match problems")
    else:
        raise GateError("unknown observer")

    expected_status = "healthy" if not identities else "degraded"
    if status != expected_status:
        raise GateError("observer status does not match its problem inventory")
    return generated_at, invocation_id, identities


def _policy(
    path: Path, *, observer: str, now: datetime
) -> tuple[
    str,
    dict[tuple[Any, ...], tuple[tuple[Any, ...], frozenset[str]]],
]:
    payload = path.read_bytes()
    document = _decode_json(payload, path)
    expected_keys = {
        "schema_version",
        "reviewed_at",
        "expires_at",
        "watchdog",
        "witness",
    }
    if (
        set(document) != expected_keys
        or document.get("schema_version") != POLICY_SCHEMA
    ):
        raise GateError("observer release policy shape is invalid")
    reviewed_at = _timestamp(document["reviewed_at"], field="policy reviewed_at")
    expires_at = _timestamp(document["expires_at"], field="policy expires_at")
    if not reviewed_at < expires_at or not reviewed_at <= now <= expires_at:
        raise GateError("observer release policy is not currently valid")

    entries = document.get(observer)
    if not isinstance(entries, list) or not entries or len(entries) > MAX_PROBLEMS:
        raise GateError("observer release policy inventory is invalid")
    approved: dict[tuple[Any, ...], tuple[tuple[Any, ...], frozenset[str]]] = {}
    for entry in entries:
        if (
            type(entry) is not dict
            or not isinstance(entry.get("reason"), str)
            or not isinstance(entry.get("baseline_states"), list)
        ):
            raise GateError("observer release policy entry is invalid")
        reason = entry["reason"]
        if not reason or len(reason) > 512:
            raise GateError("observer release policy reason is invalid")
        baseline_states = entry["baseline_states"]
        if (
            not baseline_states
            or len(baseline_states) > 8
            or any(
                _bounded_identifier(value, field="baseline state") != value
                for value in baseline_states
            )
            or len(set(baseline_states)) != len(baseline_states)
        ):
            raise GateError("observer release policy baseline states are invalid")
        problem = {
            key: value
            for key, value in entry.items()
            if key not in {"reason", "baseline_states"}
        }
        final_identity = (
            _watchdog_identity(problem)
            if observer == "watchdog"
            else _witness_identity({**problem, "message": reason})
        )
        stable = _stable_identity(observer, final_identity)
        if stable in approved:
            raise GateError("observer release policy conditions are duplicated")
        approved[stable] = (final_identity, frozenset(baseline_states))
    return hashlib.sha256(payload).hexdigest(), approved


def _encode_baseline(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(payload) > MAX_BASELINE_BYTES:
        raise GateError("observer baseline exceeds its byte ceiling")
    return base64.b64encode(payload).decode("ascii")


def _decode_baseline(value: str) -> dict[str, Any]:
    if not value or len(value) > 4 * MAX_BASELINE_BYTES:
        raise GateError("observer baseline token is invalid")
    try:
        payload = base64.b64decode(value.encode("ascii"), validate=True)
        document = json.loads(
            payload.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, binascii.Error, json.JSONDecodeError) as error:
        raise GateError("observer baseline token is invalid") from error
    if len(payload) > MAX_BASELINE_BYTES or type(document) is not dict:
        raise GateError("observer baseline token is invalid")
    canonical = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if payload != canonical or base64.b64encode(payload).decode("ascii") != value:
        raise GateError("observer baseline token is not canonical")
    return document


def capture(
    observer: str,
    status_path: Path,
    policy_path: Path,
    *,
    now: datetime,
    transaction_id: str,
    deploy_sha: str,
    controller_sha: str,
) -> str:
    generated_at, invocation_id, identities = _report(
        observer, _load_json(status_path), now=now
    )
    policy_sha256, _approved = _policy(policy_path, observer=observer, now=now)
    return _encode_baseline(
        {
            "schema_version": BASELINE_SCHEMA,
            "observer": observer,
            "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
            "invocation_id": invocation_id,
            "transaction_id": _bounded_hex(
                transaction_id, length=32, field="transaction id"
            ),
            "deploy_sha": _bounded_hex(deploy_sha, length=40, field="deploy SHA"),
            "controller_sha": _bounded_hex(
                controller_sha, length=40, field="controller SHA"
            ),
            "policy_sha256": policy_sha256,
            "identities": [list(identity) for identity in identities],
        }
    )


def compare(
    observer: str,
    status_path: Path,
    policy_path: Path,
    baseline_token: str,
    *,
    now: datetime,
    expected_invocation_id: str,
    transaction_id: str,
    deploy_sha: str,
    controller_sha: str,
) -> dict[str, Any]:
    baseline = _decode_baseline(baseline_token)
    if (
        set(baseline)
        != {
            "schema_version",
            "observer",
            "generated_at",
            "invocation_id",
            "transaction_id",
            "deploy_sha",
            "controller_sha",
            "policy_sha256",
            "identities",
        }
        or baseline.get("schema_version") != BASELINE_SCHEMA
    ):
        raise GateError("observer baseline shape is invalid")
    if baseline.get("observer") != observer:
        raise GateError("observer baseline belongs to another observer")
    bindings = {
        "transaction_id": (transaction_id, 32),
        "deploy_sha": (deploy_sha, 40),
        "controller_sha": (controller_sha, 40),
    }
    for field, (expected, length) in bindings.items():
        _bounded_hex(expected, length=length, field=field)
        if baseline.get(field) != expected:
            raise GateError(f"observer baseline {field} binding changed")
    baseline_generated_at = _timestamp(
        baseline.get("generated_at"), field="baseline generated_at"
    )
    baseline_invocation_id = _bounded_hex(
        baseline.get("invocation_id"), length=32, field="baseline invocation_id"
    )
    if now - baseline_generated_at > MAX_TRANSACTION_AGE:
        raise GateError("observer release transaction exceeded its age limit")
    baseline_values = baseline.get("identities")
    if not isinstance(baseline_values, list) or len(baseline_values) > MAX_PROBLEMS:
        raise GateError("observer baseline identities are invalid")
    baseline_identity_list = [
        _encoded_identity(observer, value) for value in baseline_values
    ]
    if baseline_identity_list != sorted(baseline_identity_list):
        raise GateError("observer baseline identities are not canonical")
    baseline_identities = {
        _stable_identity(observer, value): value for value in baseline_identity_list
    }
    if len(baseline_identities) != len(baseline_identity_list):
        raise GateError("observer baseline identities are invalid")

    policy_sha256, approved = _policy(policy_path, observer=observer, now=now)
    if baseline.get("policy_sha256") != policy_sha256:
        raise GateError("observer policy changed after baseline capture")
    final_generated_at, final_invocation_id, final_identities_list = _report(
        observer, _load_json(status_path), now=now
    )
    if final_invocation_id == baseline_invocation_id:
        raise GateError("observer final report did not use a new invocation")
    if final_invocation_id != _bounded_hex(
        expected_invocation_id, length=32, field="expected invocation_id"
    ):
        raise GateError("observer report belongs to another invocation")
    if final_generated_at <= baseline_generated_at:
        raise GateError("observer final report is not newer than its baseline")
    final_identities = {
        _stable_identity(observer, value): value for value in final_identities_list
    }
    if len(final_identities) != len(final_identities_list):
        raise GateError("observer final identities are invalid")
    for stable, final_identity in final_identities.items():
        baseline_identity = baseline_identities.get(stable)
        if baseline_identity is None:
            raise GateError("observer acquired a new or changed problem")
        rule = approved.get(stable)
        if rule is None:
            raise GateError("observer retained an unapproved release problem")
        approved_final, approved_baseline_states = rule
        if final_identity != approved_final:
            raise GateError("observer final problem state is not approved")
        if _identity_state(observer, baseline_identity) not in approved_baseline_states:
            raise GateError("observer problem transition is not approved")

    return {
        "schema_version": PROOF_SCHEMA,
        "observer": observer,
        "status": "healthy" if not final_identities else "reviewed-degradation",
        "baseline_generated_at": baseline["generated_at"],
        "final_generated_at": final_generated_at.isoformat().replace("+00:00", "Z"),
        "invocation_id": final_invocation_id,
        "transaction_id": transaction_id,
        "deploy_sha": deploy_sha,
        "controller_sha": controller_sha,
        "active_count": len(final_identities),
        "policy_sha256": policy_sha256,
        "carried_identities": [
            list(value) for value in sorted(final_identities.values())
        ],
    }


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("baseline", "compare"))
    parser.add_argument("--observer", choices=("watchdog", "witness"), required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--now")
    parser.add_argument("--transaction-id", required=True)
    parser.add_argument("--deploy-sha", required=True)
    parser.add_argument("--controller-sha", required=True)
    parser.add_argument("--expected-invocation-id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        now = _now(args.now)
        if args.mode == "baseline":
            if args.baseline is not None:
                raise GateError("--baseline is only valid in compare mode")
            if args.expected_invocation_id is not None:
                raise GateError(
                    "--expected-invocation-id is only valid in compare mode"
                )
            print(
                capture(
                    args.observer,
                    args.status,
                    args.policy,
                    now=now,
                    transaction_id=args.transaction_id,
                    deploy_sha=args.deploy_sha,
                    controller_sha=args.controller_sha,
                )
            )
        else:
            if args.baseline is None or args.expected_invocation_id is None:
                raise GateError(
                    "compare mode requires --baseline and --expected-invocation-id"
                )
            proof = compare(
                args.observer,
                args.status,
                args.policy,
                args.baseline,
                now=now,
                expected_invocation_id=args.expected_invocation_id,
                transaction_id=args.transaction_id,
                deploy_sha=args.deploy_sha,
                controller_sha=args.controller_sha,
            )
            print(json.dumps(proof, sort_keys=True, separators=(",", ":")))
    except (OSError, TypeError, GateError) as error:
        print(f"observer release gate failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
