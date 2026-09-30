"""The exceptional disposition must never become automatic no-mutation proof."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
import json
import runpy
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "ops/railway/retire-upload-502-20260928"


@pytest.fixture
def m():
    return runpy.run_path(str(HELPER))


@pytest.fixture
def candidate():
    return {
        "prepared_at": "2026-09-28T15:46:21Z",
        "message": "unique-incident-message",
        "predecessor": {"deployment_id": "predecessor"},
        "rollback_evidence": {"topology": {"created_at": "2026-09-28T15:32:08.683Z"}},
    }


def page(m, name, rows):
    return m["canonical"]({"data": {name: {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                           "edges": [{"node": r} for r in rows]}}})


def deployments(m):
    return [dict(id="predecessor", projectId=m["PROJECT"], environmentId=m["ENVIRONMENT"],
                 serviceId=m["SERVICE"], createdAt="2026-09-28T15:32:08.683Z", status="SUCCESS",
                 deploymentStopped=False, meta={"cliMessage": "old-good"}),
            dict(id="older", projectId=m["PROJECT"], environmentId=m["ENVIRONMENT"],
                 serviceId=m["SERVICE"], createdAt="2026-09-28T15:18:24.000Z", status="REMOVED",
                 deploymentStopped=True, meta={"cliMessage": "older-good"})]


def events(m):
    return [dict(id="event-1", projectId=m["PROJECT"], environmentId=m["ENVIRONMENT"],
                 createdAt="2026-09-28T15:34:04.480Z", object="Deployment", action="deployed",
                 payload={"id": "predecessor", "cliMessage": "old-good"})]


def test_pinned_incident_only_and_default_is_read_only(m):
    source = HELPER.read_text()
    assert m["JOURNAL"] == "e06381214a2c40495a83daa38d2137586a140bc5d525cd480c10a37abb87ae02"
    assert m["HOLD"] == "070575e1202df0fc15956a53491b2c9e61b75b44c2e34f2a17e6d4c874af4a4c"
    assert m["UPLOAD"] == "448710321ecd2687b1912117c80b116171042d09ecc67d7973de5e8659b13a2d"
    assert "--retire" in source and "store_true" in source
    assert '"no_mutation_proved": False' in source
    assert "_can_close_predecessor_without_mutation" not in source
    for q in m["queries"]():
        assert q.startswith("query ") and "mutation " not in q
    assert "includeDeleted:true" in m["queries"]()[0]
    assert "status:" not in m["queries"]()[0]


@pytest.mark.parametrize("hours", [24, 40, 96])
def test_incident_age_admitted_only_inside_bound(m, candidate, hours):
    m["age"](candidate, m["clock"](candidate["prepared_at"]) + timedelta(hours=hours))


@pytest.mark.parametrize("hours", [-1, 0, 23.999, 96.001, 240])
def test_incident_age_rejects_unbounded_or_early_disposition(m, candidate, hours):
    with pytest.raises(m["Refused"]):
        m["age"](candidate, m["clock"](candidate["prepared_at"]) + timedelta(hours=hours))


def test_exact_predecessor_inventory_admitted(m, candidate):
    assert len(m["validate_inventory"](page(m, "deployments", deployments(m)), candidate)) == 2


@pytest.mark.parametrize("status", ["INITIALIZING", "QUEUED", "WAITING", "BUILDING", "DEPLOYING", "REMOVING", "UNKNOWN"])
def test_pending_or_unknown_history_refused(m, candidate, status):
    rows = deployments(m)
    rows[1]["status"] = status
    with pytest.raises(m["Refused"]):
        m["validate_inventory"](page(m, "deployments", rows), candidate)


@pytest.mark.parametrize("field,value", [("projectId", "other"), ("environmentId", "other"),
                                         ("serviceId", "other"), ("deploymentStopped", True),
                                         ("status", "FAILED"), ("id", "other")])
def test_predecessor_scope_or_identity_changes_refused(m, candidate, field, value):
    rows = deployments(m)
    rows[0][field] = value
    with pytest.raises(m["Refused"]):
        m["validate_inventory"](page(m, "deployments", rows), candidate)


def test_even_terminal_matching_candidate_uses_normal_reconciliation(m, candidate):
    rows = deployments(m)
    rows[1]["meta"]["cliMessage"] = candidate["message"]
    with pytest.raises(m["Refused"], match="normal reconciliation"):
        m["validate_inventory"](page(m, "deployments", rows), candidate)


def test_newer_provider_deployment_refused(m, candidate):
    rows = deployments(m)
    rows[0]["createdAt"] = "2026-09-28T15:47:00Z"
    with pytest.raises(m["Refused"]):
        m["validate_inventory"](page(m, "deployments", rows), candidate)


@pytest.mark.parametrize("fault", ["empty", "duplicate", "unordered", "errors", "pagination"])
def test_connection_cannot_hide_missing_or_ambiguous_history(m, candidate, fault):
    rows = deployments(m)
    if fault == "empty": rows = []
    if fault == "duplicate": rows.append(deepcopy(rows[0]))
    if fault == "unordered": rows.reverse()
    value = json.loads(page(m, "deployments", rows))
    if fault == "errors": value["errors"] = [{"message": "partial result"}]
    if fault == "pagination": value["data"]["deployments"]["pageInfo"]["hasNextPage"] = "false"
    with pytest.raises(m["Refused"]):
        m["validate_inventory"](m["canonical"](value), candidate)


def test_predecessor_event_spans_activation(m, candidate):
    assert m["validate_events"](page(m, "events", events(m)), candidate)


@pytest.mark.parametrize("field,value", [("createdAt", "2026-09-28T15:46:21Z"),
                                         ("projectId", "other"), ("environmentId", "other"),
                                         ("payload", None), ("action", "created"), ("object", "Service")])
def test_event_boundary_scope_and_activation_must_be_proved(m, candidate, field, value):
    rows = events(m)
    rows[0][field] = value
    with pytest.raises(m["Refused"]):
        m["validate_events"](page(m, "events", rows), candidate)


def test_candidate_event_prevents_disposition(m, candidate):
    rows = events(m)
    rows[0]["payload"]["cliMessage"] = candidate["message"]
    with pytest.raises(m["Refused"]):
        m["validate_events"](page(m, "events", rows), candidate)


def test_immutable_evidence_refuses_conflict(m, tmp_path):
    path = tmp_path / "evidence"
    path.write_bytes(b"original")
    writes = []
    n = {"_read": lambda path, **kw: path.read_bytes(), "_atomic": lambda *a, **kw: writes.append(a)}
    with pytest.raises(m["Refused"], match="conflicts"):
        m["immutable"](n, path, b"different", 0)
    assert writes == [] and path.read_bytes() == b"original"


def test_decision_is_durable_before_gate_consumption(m, tmp_path, monkeypatch):
    calls = []
    g = m["consume"].__globals__
    monkeypatch.setitem(g, "immutable", lambda n, path, raw, gid: calls.append("decision-fsynced"))
    monkeypatch.setattr(g["pwd"], "getpwnam", lambda name: SimpleNamespace(pw_uid=123))
    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace())
    originals = {"hold.json": b"hold", "candidate.json": b"candidate"}
    n = {"_read": lambda path, **kw: b"hold" if "data-hold" in str(path) else b"candidate",
         "_unlink": lambda path: calls.append(path.name)}
    m["consume"](n, originals, b"decision", 0)
    assert calls == ["decision-fsynced", "railway-publication-data-hold.json", "pending-candidate.json"]


def test_fsync_failure_consumes_neither_gate(m, monkeypatch):
    calls = []
    def fail(*args): raise OSError("disk full")
    monkeypatch.setitem(m["consume"].__globals__, "immutable", fail)
    with pytest.raises(OSError):
        m["consume"]({"_unlink": lambda path: calls.append(path)}, {}, b"decision", 0)
    assert calls == []


def test_changed_hold_is_not_consumed(m, monkeypatch):
    calls = []
    g = m["consume"].__globals__
    monkeypatch.setitem(g, "immutable", lambda *args: None)
    monkeypatch.setattr(g["pwd"], "getpwnam", lambda name: SimpleNamespace(pw_uid=123))
    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace())
    with pytest.raises(m["Refused"], match="gate changed"):
        m["consume"]({"_read": lambda *a, **kw: b"different", "_unlink": lambda path: calls.append(path)},
                     {"hold.json": b"original"}, b"decision", 0)
    assert calls == []


def test_restart_after_hold_consumption_preserves_decision_and_consumes_only_journal(m, monkeypatch):
    g = m["consume"].__globals__
    decisions, removed = [], []
    exists = {"railway-publication-data-hold.json", "pending-candidate.json"}
    monkeypatch.setitem(g, "immutable", lambda n, path, raw, gid: decisions.append(raw))
    monkeypatch.setattr(g["pwd"], "getpwnam", lambda name: SimpleNamespace(pw_uid=123))
    def lstat(path):
        if path.name not in exists: raise FileNotFoundError(path)
        return SimpleNamespace()
    monkeypatch.setattr(Path, "lstat", lstat)
    def unlink(path):
        if path.name == "pending-candidate.json" and not removed:
            raise AssertionError("hold must be consumed first")
        if path.name == "pending-candidate.json" and len(removed) == 1:
            removed.append("simulated-crash")
            raise OSError("crash after hold consumption")
        removed.append(path.name)
        exists.remove(path.name)
    n = {"_read": lambda path, **kw: b"hold" if "data-hold" in str(path) else b"candidate",
         "_unlink": unlink}
    originals = {"hold.json": b"hold", "candidate.json": b"candidate"}
    with pytest.raises(OSError, match="crash"):
        m["consume"](n, originals, b"same-durable-decision", 0)
    assert exists == {"pending-candidate.json"}
    m["consume"](n, originals, b"same-durable-decision", 0)
    assert decisions == [b"same-durable-decision"] * 2
    assert exists == set()
    assert removed == ["railway-publication-data-hold.json", "simulated-crash", "pending-candidate.json"]


def test_restart_after_journal_consumption_writes_only_separate_completion(m, monkeypatch, tmp_path):
    g = m["execute"].__globals__
    root, state, control = tmp_path / "incident", tmp_path / "state", tmp_path / "control"
    for p in (root, state, control): p.mkdir()
    (control / "publish.lock").write_bytes(b"")
    decision = {"incident": m["INCIDENT"], "journal_sha256": m["JOURNAL"],
                "no_mutation_proved": False, "residual_uncertainty": m["RESIDUAL"]}
    (root / "decision.json").write_bytes(m["canonical"](decision))
    writes = []
    def atomic(path, raw, **kw):
        writes.append(path.name)
        path.write_bytes(raw)
    n = {"_read": lambda path, **kw: path.read_bytes(), "_atomic": atomic}
    monkeypatch.setitem(g, "ROOT", root)
    monkeypatch.setitem(g, "STATE", state)
    monkeypatch.setitem(g, "CONTROL", control)
    monkeypatch.setitem(g, "load_helper", lambda: n)
    monkeypatch.setattr(g["os"], "geteuid", lambda: 0)
    monkeypatch.setattr(g["pwd"], "getpwnam", lambda name: SimpleNamespace(pw_uid=123))
    monkeypatch.setattr(g["grp"], "getgrnam", lambda name: SimpleNamespace(gr_gid=123))
    monkeypatch.setenv("RAILWAY_TOKEN", "synthetic-test-token")
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
    result = m["execute"](True, m["ACK"])
    assert writes == ["completion.json"]
    assert result["no_mutation_proved"] is False
    assert result["outcome"] == "retired_with_unregistered_provider_job_uncertainty"
    assert (root / "decision.json").read_bytes() == m["canonical"](decision)
    assert list(state.iterdir()) == []


def test_observer_dependency_survives_routine_upgrade(m):
    import inspect
    source = inspect.getsource(m["load_helper"])
    assert 'ROOT / "original-reconciler.py"' in source
    assert "observer and" in source
    assert "digest(dependency.read_bytes()) != RECONCILER" in source


@pytest.mark.parametrize("status", ["INITIALIZING", "QUEUED", "BUILDING", "SUCCESS", "FAILED", "REMOVED"])
def test_observer_detects_any_late_identity_even_before_activation(m, candidate, status):
    late = {"id": "late", "status": status, "meta": {"cliMessage": candidate["message"]}}
    assert m["matching_candidates"]([late], b"{}", candidate["message"]) == {"late": late}


def test_observer_finds_old_active_candidate_beyond_history_page(m, candidate):
    late = {"id": "late", "status": "SUCCESS", "meta": {"cliMessage": candidate["message"]}}
    topology = m["canonical"]({"environments": {"edges": [{"node": {"serviceInstances": {
        "edges": [{"node": {"activeDeployments": [late]}}]}}}]}})
    assert m["matching_candidates"](deployments(m), topology, candidate["message"]) == {"late": late}


def test_observer_does_not_conflate_other_submission(m, candidate):
    assert m["matching_candidates"](deployments(m), b"{}", candidate["message"]) == {}


def test_observer_refuses_malformed_topology(m, candidate):
    with pytest.raises(json.JSONDecodeError):
        m["matching_candidates"](deployments(m), b"not-json", candidate["message"])


def test_acknowledgement_checked_before_loading_dependency(m, monkeypatch):
    g = m["execute"].__globals__
    monkeypatch.setattr(g["os"], "geteuid", lambda: 0)
    monkeypatch.setitem(g, "load_helper", lambda: pytest.fail("dependency loaded before acknowledgement"))
    with pytest.raises(m["Refused"], match="acknowledgement"):
        m["execute"](True, "accept")


def test_nonroot_cannot_dispose(m, monkeypatch):
    monkeypatch.setattr(m["execute"].__globals__["os"], "geteuid", lambda: 501)
    with pytest.raises(m["Refused"], match="requires root"):
        m["execute"](False, None)


def test_observer_is_bounded_local_hold_only(m):
    source = HELPER.read_text()
    assert "deploymentCancel" not in source and "deploymentRollback" not in source
    assert "--observe" in (ROOT / "ops/systemd/palimpsest-retired-upload-observer.service").read_text()
    assert "OnUnitInactiveSec=1min" in (ROOT / "ops/systemd/palimpsest-retired-upload-observer.timer").read_text()
    assert "no_matching_candidate_observed" in source
    assert 'reason="candidate_identity_ambiguous"' in source
    assert "observer_ready(n, gid)" in source
