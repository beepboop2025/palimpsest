from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ops import observer_release_gate as gate


NOW_TEXT = "2026-08-24T18:10:00Z"
NOW = datetime(2026, 8, 24, 18, 10, tzinfo=timezone.utc)
TRANSACTION_ID = "a" * 32
DEPLOY_SHA = "b" * 40
CONTROLLER_SHA = "c" * 40
INVOCATION_ID = "d" * 32
BASELINE_INVOCATION_ID = "1" * 32


def _capture(observer: str, status: Path, policy: Path, *, now: datetime) -> str:
    return gate.capture(
        observer,
        status,
        policy,
        now=now,
        transaction_id=TRANSACTION_ID,
        deploy_sha=DEPLOY_SHA,
        controller_sha=CONTROLLER_SHA,
    )


def _compare(
    observer: str,
    status: Path,
    policy: Path,
    baseline: str,
    *,
    now: datetime,
) -> dict:
    return gate.compare(
        observer,
        status,
        policy,
        baseline,
        now=now,
        expected_invocation_id=INVOCATION_ID,
        transaction_id=TRANSACTION_ID,
        deploy_sha=DEPLOY_SHA,
        controller_sha=CONTROLLER_SHA,
    )


def _policy(path: Path) -> Path:
    value = {
        "schema_version": gate.POLICY_SCHEMA,
        "reviewed_at": "2026-08-24T18:00:00Z",
        "expires_at": "2026-08-31T23:59:59Z",
        "watchdog": [
            {
                "condition": "evidence/gdelt",
                "scope": "evidence",
                "subject": "gdelt",
                "state": "stale",
                "required": True,
                "baseline_states": ["stale"],
                "reason": "reviewed upstream rate limit",
            }
        ],
        "witness": [
            {
                "condition": "osint/gdelt",
                "state": "stale",
                "baseline_states": ["stale"],
                "reason": "reviewed upstream rate limit",
            }
        ],
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _watchdog(
    path: Path,
    generated_at: str,
    problems: list[dict],
    *,
    invocation_id: str = INVOCATION_ID,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": gate.WATCHDOG_SCHEMA,
                "generated_at": generated_at,
                "invocation_id": invocation_id,
                "status": "healthy" if not problems else "degraded",
                "active_count": len(problems),
                "problems": problems,
            }
        ),
        encoding="utf-8",
    )
    return path


def _witness(
    path: Path,
    generated_at: str,
    problems: list[dict],
    *,
    chain_alerts: list[dict] | None = None,
    invocation_id: str = INVOCATION_ID,
) -> Path:
    alerts = chain_alerts or []
    path.write_text(
        json.dumps(
            {
                "schema_version": gate.WITNESS_SCHEMA,
                "generated_at": generated_at,
                "invocation_id": invocation_id,
                "status": "healthy" if not problems and not alerts else "degraded",
                "active_count": len(problems) + len(alerts),
                "inventory_complete": True,
                "chain_alerts": alerts,
                "freshness_problems": problems,
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def watchdog_problem() -> dict:
    return {
        "condition": "evidence/gdelt",
        "scope": "evidence",
        "subject": "gdelt",
        "state": "stale",
        "required": True,
    }


def test_watchdog_final_must_be_subset_of_baseline_and_reviewed_policy(
    tmp_path: Path, watchdog_problem: dict
) -> None:
    policy = _policy(tmp_path / "policy.json")
    status = _watchdog(
        tmp_path / "status.json",
        "2026-08-24T18:09:00Z",
        [
            watchdog_problem,
            {
                **watchdog_problem,
                "condition": "pipeline/wayback",
                "scope": "pipeline",
                "subject": "wayback",
                "state": "failed",
            },
        ],
        invocation_id=BASELINE_INVOCATION_ID,
    )
    baseline = _capture("watchdog", status, policy, now=NOW)

    _watchdog(status, "2026-08-24T18:10:30Z", [watchdog_problem])
    proof = _compare(
        "watchdog",
        status,
        policy,
        baseline,
        now=datetime(2026, 8, 24, 18, 11, tzinfo=timezone.utc),
    )
    assert proof["status"] == "reviewed-degradation"
    assert proof["active_count"] == 1


def test_new_changed_and_unapproved_watchdog_problems_fail_closed(
    tmp_path: Path, watchdog_problem: dict
) -> None:
    policy = _policy(tmp_path / "policy.json")
    status = _watchdog(
        tmp_path / "status.json",
        "2026-08-24T18:09:00Z",
        [watchdog_problem],
        invocation_id=BASELINE_INVOCATION_ID,
    )
    baseline = _capture("watchdog", status, policy, now=NOW)
    _watchdog(
        status,
        "2026-08-24T18:10:30Z",
        [{**watchdog_problem, "state": "missing"}],
    )
    with pytest.raises(gate.GateError, match="final problem state is not approved"):
        _compare(
            "watchdog",
            status,
            policy,
            baseline,
            now=datetime(2026, 8, 24, 18, 11, tzinfo=timezone.utc),
        )


def test_explicit_reviewed_state_transition_is_accepted_and_other_transition_fails(
    tmp_path: Path, watchdog_problem: dict
) -> None:
    policy = _policy(tmp_path / "policy.json")
    document = json.loads(policy.read_text(encoding="utf-8"))
    document["watchdog"][0]["state"] = "degraded"
    document["watchdog"][0]["baseline_states"] = ["stale"]
    policy.write_text(json.dumps(document), encoding="utf-8")
    status = _watchdog(
        tmp_path / "status.json",
        "2026-08-24T18:09:00Z",
        [watchdog_problem],
        invocation_id=BASELINE_INVOCATION_ID,
    )
    baseline = _capture("watchdog", status, policy, now=NOW)

    _watchdog(
        status,
        "2026-08-24T18:10:30Z",
        [{**watchdog_problem, "state": "degraded"}],
    )
    proof = _compare(
        "watchdog",
        status,
        policy,
        baseline,
        now=datetime(2026, 8, 24, 18, 11, tzinfo=timezone.utc),
    )
    assert proof["status"] == "reviewed-degradation"
    assert proof["carried_identities"][0][3] == "degraded"

    document = json.loads(policy.read_text(encoding="utf-8"))
    document["watchdog"][0]["baseline_states"] = ["missing"]
    policy.write_text(json.dumps(document), encoding="utf-8")
    _watchdog(
        status,
        "2026-08-24T18:10:30Z",
        [{**watchdog_problem, "state": "degraded"}],
        invocation_id=BASELINE_INVOCATION_ID,
    )
    rejected_baseline = _capture("watchdog", status, policy, now=NOW)
    _watchdog(
        status,
        "2026-08-24T18:10:40Z",
        [{**watchdog_problem, "state": "degraded"}],
    )
    with pytest.raises(gate.GateError, match="transition is not approved"):
        _compare(
            "watchdog",
            status,
            policy,
            rejected_baseline,
            now=datetime(2026, 8, 24, 18, 11, tzinfo=timezone.utc),
        )


def test_witness_ignores_prose_but_rejects_chain_alerts_and_state_changes(
    tmp_path: Path,
) -> None:
    policy = _policy(tmp_path / "policy.json")
    status = _witness(
        tmp_path / "status.json",
        "2026-08-24T18:09:00Z",
        [{"condition": "osint/gdelt", "state": "stale", "message": "old prose"}],
        invocation_id=BASELINE_INVOCATION_ID,
    )
    baseline = _capture("witness", status, policy, now=NOW)
    _witness(
        status,
        "2026-08-24T18:10:30Z",
        [{"condition": "osint/gdelt", "state": "stale", "message": "new prose"}],
    )
    proof = _compare(
        "witness",
        status,
        policy,
        baseline,
        now=datetime(2026, 8, 24, 18, 11, tzinfo=timezone.utc),
    )
    assert proof["active_count"] == 1

    _witness(
        status,
        "2026-08-24T18:10:40Z",
        [],
        chain_alerts=[
            {"chain": "eval-registry", "kind": "prefix", "message": "rewritten"}
        ],
    )
    with pytest.raises(gate.GateError, match="chain, prefix, or fetch"):
        _compare(
            "witness",
            status,
            policy,
            baseline,
            now=datetime(2026, 8, 24, 18, 11, tzinfo=timezone.utc),
        )

    incomplete = json.loads(status.read_text(encoding="utf-8"))
    incomplete["chain_alerts"] = []
    incomplete["inventory_complete"] = False
    incomplete["active_count"] = len(incomplete["freshness_problems"])
    status.write_text(json.dumps(incomplete), encoding="utf-8")
    with pytest.raises(gate.GateError, match="inventory is incomplete"):
        _compare(
            "witness",
            status,
            policy,
            baseline,
            now=datetime(2026, 8, 24, 18, 11, tzinfo=timezone.utc),
        )


def test_stale_report_expired_policy_and_policy_swap_fail_closed(
    tmp_path: Path, watchdog_problem: dict
) -> None:
    policy = _policy(tmp_path / "policy.json")
    status = _watchdog(
        tmp_path / "status.json", "2026-08-24T18:00:00Z", [watchdog_problem]
    )
    with pytest.raises(gate.GateError, match="not fresh"):
        _capture("watchdog", status, policy, now=NOW)

    _watchdog(
        status,
        "2026-08-24T18:09:00Z",
        [watchdog_problem],
        invocation_id=BASELINE_INVOCATION_ID,
    )
    baseline = _capture("watchdog", status, policy, now=NOW)
    document = json.loads(policy.read_text(encoding="utf-8"))
    document["watchdog"][0]["reason"] = "changed after capture"
    policy.write_text(json.dumps(document), encoding="utf-8")
    _watchdog(status, "2026-08-24T18:10:30Z", [watchdog_problem])
    with pytest.raises(gate.GateError, match="policy changed"):
        _compare(
            "watchdog",
            status,
            policy,
            baseline,
            now=datetime(2026, 8, 24, 18, 11, tzinfo=timezone.utc),
        )

    _watchdog(status, "2026-09-01T00:00:00Z", [watchdog_problem])
    with pytest.raises(gate.GateError, match="not currently valid"):
        _capture(
            "watchdog",
            status,
            policy,
            now=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )


def test_baseline_transaction_bindings_and_final_invocation_fail_closed(
    tmp_path: Path, watchdog_problem: dict
) -> None:
    policy = _policy(tmp_path / "policy.json")
    status = _watchdog(
        tmp_path / "status.json",
        "2026-08-24T18:09:00Z",
        [watchdog_problem],
        invocation_id=BASELINE_INVOCATION_ID,
    )
    baseline = _capture("watchdog", status, policy, now=NOW)
    _watchdog(status, "2026-08-24T18:10:30Z", [watchdog_problem])
    compare_now = datetime(2026, 8, 24, 18, 11, tzinfo=timezone.utc)
    defaults = {
        "expected_invocation_id": INVOCATION_ID,
        "transaction_id": TRANSACTION_ID,
        "deploy_sha": DEPLOY_SHA,
        "controller_sha": CONTROLLER_SHA,
    }

    for field, wrong in (
        ("transaction_id", "e" * 32),
        ("deploy_sha", "e" * 40),
        ("controller_sha", "f" * 40),
    ):
        arguments = {**defaults, field: wrong}
        with pytest.raises(gate.GateError, match=f"baseline {field} binding changed"):
            gate.compare(
                "watchdog",
                status,
                policy,
                baseline,
                now=compare_now,
                **arguments,
            )

    with pytest.raises(gate.GateError, match="another invocation"):
        gate.compare(
            "watchdog",
            status,
            policy,
            baseline,
            now=compare_now,
            **{**defaults, "expected_invocation_id": "e" * 32},
        )


def test_expired_and_ambiguous_baseline_tokens_fail_closed(
    tmp_path: Path, watchdog_problem: dict
) -> None:
    policy = _policy(tmp_path / "policy.json")
    status = _watchdog(
        tmp_path / "status.json",
        "2026-08-24T18:09:00Z",
        [watchdog_problem],
        invocation_id=BASELINE_INVOCATION_ID,
    )
    baseline = _capture("watchdog", status, policy, now=NOW)

    expired_now = NOW + gate.MAX_TRANSACTION_AGE + timedelta(seconds=1)
    _watchdog(
        status,
        expired_now.isoformat().replace("+00:00", "Z"),
        [watchdog_problem],
    )
    with pytest.raises(gate.GateError, match="exceeded its age limit"):
        _compare("watchdog", status, policy, baseline, now=expired_now)

    valid_now = datetime(2026, 8, 24, 18, 11, tzinfo=timezone.utc)
    document = json.loads(base64.b64decode(baseline, validate=True))
    noncanonical_payload = json.dumps(document, indent=2).encode("utf-8")
    noncanonical = base64.b64encode(noncanonical_payload).decode("ascii")
    with pytest.raises(gate.GateError, match="not canonical"):
        _compare("watchdog", status, policy, noncanonical, now=valid_now)

    document["identities"].append(document["identities"][0])
    duplicated = gate._encode_baseline(document)
    with pytest.raises(gate.GateError, match="identities are invalid"):
        _compare("watchdog", status, policy, duplicated, now=valid_now)


def test_baseline_identity_order_and_invocation_replay_fail_closed(
    tmp_path: Path, watchdog_problem: dict
) -> None:
    policy = _policy(tmp_path / "policy.json")
    status = _watchdog(
        tmp_path / "status.json",
        "2026-08-24T18:09:00Z",
        [
            watchdog_problem,
            {
                "condition": "pipeline/wayback",
                "scope": "pipeline",
                "subject": "wayback",
                "state": "failed",
                "required": True,
            },
        ],
        invocation_id=BASELINE_INVOCATION_ID,
    )
    baseline = _capture("watchdog", status, policy, now=NOW)
    document = json.loads(base64.b64decode(baseline, validate=True))
    document["identities"].reverse()
    reordered = gate._encode_baseline(document)
    final_now = datetime(2026, 8, 24, 18, 11, tzinfo=timezone.utc)
    _watchdog(status, "2026-08-24T18:10:30Z", [watchdog_problem])
    with pytest.raises(gate.GateError, match="identities are not canonical"):
        _compare("watchdog", status, policy, reordered, now=final_now)

    _watchdog(
        status,
        "2026-08-24T18:10:40Z",
        [watchdog_problem],
        invocation_id=BASELINE_INVOCATION_ID,
    )
    with pytest.raises(gate.GateError, match="did not use a new invocation"):
        gate.compare(
            "watchdog",
            status,
            policy,
            baseline,
            now=final_now,
            expected_invocation_id=BASELINE_INVOCATION_ID,
            transaction_id=TRANSACTION_ID,
            deploy_sha=DEPLOY_SHA,
            controller_sha=CONTROLLER_SHA,
        )


def test_checked_in_policy_is_exact_and_expiring() -> None:
    policy_path = Path("ops/observer-release-policy-20260824.json")
    document = json.loads(policy_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == gate.POLICY_SCHEMA
    assert len(document["watchdog"]) == 14
    assert len(document["witness"]) == 5
    assert document["expires_at"] == "2026-08-31T23:59:59Z"
    assert all(item["reason"] for item in document["watchdog"] + document["witness"])
    assert all(
        item["baseline_states"] for item in document["watchdog"] + document["witness"]
    )
    silence_watchdog = next(
        item
        for item in document["watchdog"]
        if item["condition"] == "osint/silence-index"
    )
    silence_witness = next(
        item
        for item in document["witness"]
        if item["condition"] == "osint/silence-index"
    )
    assert silence_watchdog["baseline_states"] == ["stale", "degraded"]
    assert silence_witness["baseline_states"] == ["stale", "degraded"]
