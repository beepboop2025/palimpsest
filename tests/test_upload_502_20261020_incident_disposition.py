"""The second exceptional disposition must never become automatic no-mutation proof.

This mirrors the 28 September suite and adds what the 20 October incident needs:
its exact digests and acknowledgement, a wider but still closing age window, the
one reviewed post-submission provider event, an independent refusal for every
local trace of recovery, rollback or preparation, and the full dry-run and
retire sequence. No test reads the wall clock where the result depends on it.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
import json
import os
import runpy
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "ops/railway/retire-upload-502-20261020"
SEPTEMBER = ROOT / "ops/railway/retire-upload-502-20260928"
SERVICE_UNIT = ROOT / "ops/systemd/palimpsest-retired-upload-observer-20261020.service"
TIMER_UNIT = ROOT / "ops/systemd/palimpsest-retired-upload-observer-20261020.timer"
SEPTEMBER_SERVICE = ROOT / "ops/systemd/palimpsest-retired-upload-observer.service"
SEPTEMBER_TIMER = ROOT / "ops/systemd/palimpsest-retired-upload-observer.timer"
MESSAGE = "palimpsest-hetzner-9c8ba25f1a0c-7b9c2a923620-f0e823dcc632569cbfcff69f2b1eef78"
SEPTEMBER_SUBMISSION = "600a32145644507e6111213ecd18edfc"
PREDECESSOR = "0bcbcc89-7540-486a-b55e-a4005fde4fd2"
TOKEN_EVENT_ID = "82dcb88c-3b02-4803-9771-47a2dcac5b38"
# The moment the residual uncertainty was accepted for this incident.
ACCEPTED = datetime(2026, 10, 31, 19, 20, tzinfo=UTC)


@pytest.fixture
def m():
    return runpy.run_path(str(HELPER))


@pytest.fixture
def candidate():
    return {
        "prepared_at": "2026-10-20T17:13:26Z",
        "message": MESSAGE,
        "submission_id": "f0e823dcc632569cbfcff69f2b1eef78",
        "predecessor": {"deployment_id": PREDECESSOR, "manifest_sha256": "0" * 64},
        "rollback_evidence": {"topology": {
            "created_at": "2026-10-20T16:56:38.609Z", "deployment_id": PREDECESSOR,
            "image_digest": "sha256:" + "a" * 64, "reason": "deploy"}},
    }


def page(m, name, rows):
    return m["canonical"]({"data": {name: {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                           "edges": [{"node": r} for r in rows]}}})


def deployments(m):
    return [dict(id=PREDECESSOR, projectId=m["PROJECT"], environmentId=m["ENVIRONMENT"],
                 serviceId=m["SERVICE"], createdAt="2026-10-20T16:56:38.609Z", status="SUCCESS",
                 deploymentStopped=False, meta={"cliMessage": "old-good"}),
            dict(id="older", projectId=m["PROJECT"], environmentId=m["ENVIRONMENT"],
                 serviceId=m["SERVICE"], createdAt="2026-10-20T16:40:12.000Z", status="REMOVED",
                 deploymentStopped=True, meta={"cliMessage": "older-good"})]


def token_event(m):
    return dict(id=TOKEN_EVENT_ID, projectId=m["PROJECT"], environmentId=m["ENVIRONMENT"],
                createdAt="2026-10-23T20:20:42.075Z", object="Token", action="created",
                payload={"name": "synthetic-12"})


def deployed_event(m):
    return dict(id="event-deployed", projectId=m["PROJECT"], environmentId=m["ENVIRONMENT"],
                createdAt="2026-10-20T16:59:12.989Z", object="Deployment", action="deployed",
                payload={"id": PREDECESSOR, "cliMessage": "old-good"})


def events(m):
    return [token_event(m), deployed_event(m)]


def freeze(monkeypatch, g, *moments):
    """Replace the helper's clock; later calls repeat the final moment."""
    sequence = list(moments)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setitem(g, "datetime", Frozen)


# Exact binding to this incident, and only this incident.

def test_pinned_to_the_exact_october_incident(m):
    assert m["INCIDENT"] == "palimpsest-upload-502-20261020"
    assert m["JOURNAL"] == "ed73080a31732f0ca2ce656a8c5023c8079c41de5316f5cc5106157fbc39adc8"
    assert m["HOLD"] == "15027b0d752b4484f2c80c4c67a3245606a99059c24cdf189c689ca96ec8e3a9"
    assert m["PIN"] == "de1f08959413869e362fc354f2dbde749dd772712dcd413128fe5d24fe89c2cd"
    assert m["UPLOAD"] == "448710321ecd2687b1912117c80b116171042d09ecc67d7973de5e8659b13a2d"
    assert m["RECONCILER"] == "fb2bdd9542691842492620e78be60e9bc23b6ff3daa50045fe0f71ff8a0651ca"
    assert m["SUBMISSION"] == "f0e823dcc632569cbfcff69f2b1eef78"
    assert m["ACK"] == "accept-unregistered-provider-job-uncertainty-for-f0e823dcc632569cbfcff69f2b1eef78"
    assert m["ACK"].endswith(m["SUBMISSION"]) and MESSAGE.endswith(m["SUBMISSION"])
    assert m["PREDECESSOR"] == PREDECESSOR
    assert m["PREPARED_AT"] == "2026-10-20T17:13:26Z"
    assert m["ROOT"] == Path("/var/lib/palimpsest/railway-control/incident-dispositions/palimpsest-upload-502-20261020")
    assert m["OBSERVER_TIMER"] == TIMER_UNIT.name


def test_october_and_september_cannot_dispose_of_each_other(m):
    s = runpy.run_path(str(SEPTEMBER))
    for key in ("INCIDENT", "ACK", "JOURNAL", "HOLD", "PIN", "ROOT", "OBSERVER_TIMER"):
        assert m[key] != s[key], key
    # Same 502 output bytes, same reviewed reconciler, same Railway scope.
    for key in ("UPLOAD", "RECONCILER", "PROJECT", "ENVIRONMENT", "SERVICE", "RESIDUAL", "HELPER"):
        assert m[key] == s[key], key
    assert SEPTEMBER_SUBMISSION in s["ACK"] and SEPTEMBER_SUBMISSION not in HELPER.read_text()


def test_default_is_read_only_and_never_mutates_provider(m):
    source = HELPER.read_text()
    assert "--retire" in source and "store_true" in source
    assert '"no_mutation_proved": False' in source
    assert "_can_close_predecessor_without_mutation" not in source
    for operation in ("deploymentCancel", "deploymentRollback", "deploymentRedeploy",
                      "deploymentRestart", "deploymentRemove", "serviceInstanceDeploy"):
        assert operation not in source
    assert '"up"' not in source and "'up'" not in source
    for q in m["queries"]():
        assert q.startswith("query ") and "mutation" not in q
    assert "includeDeleted:true" in m["queries"]()[0]
    assert "status:" not in m["queries"]()[0]


# The explicit, closing age window.

def test_disposition_window_bound_is_explicit(m):
    assert m["MIN_AGE"] == 24 * 3600
    assert m["MAX_AGE"] == 14 * 24 * 3600
    closes = m["clock"](m["PREPARED_AT"]) + timedelta(seconds=m["MAX_AGE"])
    assert closes == datetime(2026, 11, 3, 17, 13, 26, tzinfo=UTC)
    assert "24 hour to 14 day" in HELPER.read_text()


def test_age_at_acceptance_is_admitted_with_modest_headroom(m, candidate):
    m["age"](candidate, ACCEPTED)
    elapsed = ACCEPTED - m["clock"](candidate["prepared_at"])
    assert timedelta(hours=266) < elapsed < timedelta(hours=267)
    headroom = timedelta(seconds=m["MAX_AGE"]) - elapsed
    assert timedelta(hours=48) < headroom < timedelta(hours=96)


@pytest.mark.parametrize("hours", [24, 96, 96.001, 200, 266.12, 336])
def test_incident_age_admitted_only_inside_bound(m, candidate, hours):
    m["age"](candidate, m["clock"](candidate["prepared_at"]) + timedelta(hours=hours))


@pytest.mark.parametrize("hours", [-1, 0, 23.999, 336.001, 337, 400, 24 * 30])
def test_incident_age_rejects_early_or_expired_disposition(m, candidate, hours):
    with pytest.raises(m["Refused"], match="disposition window"):
        m["age"](candidate, m["clock"](candidate["prepared_at"]) + timedelta(hours=hours))


# Provider deployment inventory, including a candidate row appearing on Railway.

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


@pytest.mark.parametrize("status", ["INITIALIZING", "QUEUED", "BUILDING", "DEPLOYING", "SUCCESS", "FAILED", "CRASHED", "REMOVED"])
def test_candidate_row_appearing_on_railway_uses_normal_reconciliation(m, candidate, status):
    rows = deployments(m)
    rows[1]["meta"]["cliMessage"] = candidate["message"]
    rows[1]["status"] = status
    with pytest.raises(m["Refused"], match="normal reconciliation"):
        m["validate_inventory"](page(m, "deployments", rows), candidate)


def test_candidate_as_newest_provider_row_refused(m, candidate):
    late = dict(deployments(m)[1], id="late", createdAt="2026-10-20T17:14:02.000Z", status="QUEUED",
                deploymentStopped=False, meta={"cliMessage": candidate["message"]})
    with pytest.raises(m["Refused"]):
        m["validate_inventory"](page(m, "deployments", [late, *deployments(m)]), candidate)


def test_newer_provider_deployment_refused(m, candidate):
    rows = deployments(m)
    rows[0]["createdAt"] = "2026-10-20T17:47:00Z"
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


# Provider events: only the one reviewed later event is admitted.

def test_reviewed_later_events_are_exactly_one_token_creation(m):
    assert m["REVIEWED_LATER_EVENTS"] == {TOKEN_EVENT_ID: {
        "object": "Token", "action": "created",
        "createdAt": "2026-10-23T20:20:42.075Z", "payload_keys": ["name"]}}


def test_predecessor_activation_spanned_with_reviewed_token_event(m, candidate):
    assert len(m["validate_events"](page(m, "events", events(m)), candidate)) == 2


@pytest.mark.parametrize("field,value", [("id", "00000000-0000-4000-8000-000000000000"),
                                         ("object", "Deployment"), ("action", "deleted"),
                                         ("createdAt", "2026-10-23T20:20:43.075Z"),
                                         ("payload", {}), ("payload", {"name": "x", "id": PREDECESSOR}),
                                         ("payload", {"name": "x", "serviceId": "86a6f49c"})])
def test_any_change_to_the_reviewed_event_refuses(m, candidate, field, value):
    rows = events(m)
    rows[0][field] = value
    with pytest.raises(m["Refused"], match="unreviewed activity after submission"):
        m["validate_events"](page(m, "events", rows), candidate)


@pytest.mark.parametrize("extra", [
    dict(id="second-token", object="Token", action="created", payload={"name": "another"}),
    dict(id="deploy-created", object="Deployment", action="created", payload={"id": "late"}),
    dict(id="variable", object="Variable", action="updated", payload={"name": "X"}),
])
def test_any_other_later_event_refuses(m, candidate, extra):
    later = dict(extra, projectId=m["PROJECT"], environmentId=m["ENVIRONMENT"], createdAt="2026-10-25T00:00:00.000Z")
    with pytest.raises(m["Refused"], match="unreviewed activity after submission"):
        m["validate_events"](page(m, "events", [later, *events(m)]), candidate)


def test_event_exactly_at_submission_boundary_refuses(m, candidate):
    boundary = dict(deployed_event(m), id="at-boundary", action="created", createdAt="2026-10-20T17:13:26Z",
                    payload={"id": "unknown"})
    with pytest.raises(m["Refused"], match="unreviewed activity"):
        m["validate_events"](page(m, "events", [token_event(m), boundary, deployed_event(m)]), candidate)


@pytest.mark.parametrize("position", [0, 1])
def test_candidate_event_prevents_disposition_before_or_after_boundary(m, candidate, position):
    rows = events(m)
    rows[position]["payload"]["cliMessage"] = candidate["message"]
    with pytest.raises(m["Refused"], match="recorded the candidate"):
        m["validate_events"](page(m, "events", rows), candidate)


@pytest.mark.parametrize("field,value", [("projectId", "other"), ("environmentId", "other"),
                                         ("payload", None), ("action", "created"), ("object", "Service")])
def test_event_scope_and_activation_must_be_proved(m, candidate, field, value):
    rows = events(m)
    rows[1][field] = value
    with pytest.raises(m["Refused"]):
        m["validate_events"](page(m, "events", rows), candidate)


# The repeated provider and origin observation inside a disposition.

@pytest.fixture
def provider(m, candidate):
    manifest = b'{"synthetic": "manifest"}\n'
    candidate["predecessor"]["manifest_sha256"] = m["digest"](manifest)
    expected = {k: candidate["rollback_evidence"]["topology"][k] for k in ("created_at", "deployment_id", "image_digest", "reason")}
    state = SimpleNamespace(active=[dict(expected), dict(expected)], origins=(manifest, manifest),
                            inventory=deployments(m), events=events(m))

    def railway(arguments, name, value, timeout):
        assert arguments[0] == "api" and arguments[1].startswith("query ") and (name, value) == ("RAILWAY_TOKEN", "t")
        if "deployments(" in arguments[1]:
            return page(m, "deployments", state.inventory)
        return page(m, "events", state.events)

    n = {"_railway": railway, "_status": lambda *token: b'{"synthetic": "status"}',
         "_topology": lambda raw: state.active.pop(0), "_live_manifests": lambda label: state.origins}
    return SimpleNamespace(n=n, state=state, candidate=candidate, manifest=manifest)


def test_observation_admits_exact_predecessor_on_both_origins(m, provider):
    result, raw = m["observe"](provider.n, provider.candidate, ("RAILWAY_TOKEN", "t"))
    assert result["active"]["deployment_id"] == PREDECESSOR
    assert set(raw) == {"deployments.json", "events.json", "topology-before.json",
                        "provider-manifest.json", "public-manifest.json", "topology-after.json"}
    assert raw["provider-manifest.json"] == raw["public-manifest.json"] == provider.manifest


def test_candidate_active_on_railway_refuses_observation(m, provider):
    provider.state.active[0]["deployment_id"] = "candidate-deployment"
    with pytest.raises(m["Refused"], match="active predecessor identity moved"):
        m["observe"](provider.n, provider.candidate, ("RAILWAY_TOKEN", "t"))


def test_candidate_row_in_inventory_refuses_observation(m, provider):
    provider.state.inventory[1]["meta"]["cliMessage"] = MESSAGE
    with pytest.raises(m["Refused"], match="normal reconciliation"):
        m["observe"](provider.n, provider.candidate, ("RAILWAY_TOKEN", "t"))


@pytest.mark.parametrize("fault", ["origins-differ", "manifest-moved", "topology-moved"])
def test_origin_or_topology_movement_refuses_observation(m, provider, fault):
    if fault == "origins-differ":
        provider.state.origins = (provider.manifest, provider.manifest + b" ")
    if fault == "manifest-moved":
        provider.state.origins = (b"other", b"other")
    if fault == "topology-moved":
        provider.state.active[1]["created_at"] = "2026-10-20T17:20:00.000Z"
    with pytest.raises(m["Refused"], match="moved"):
        m["observe"](provider.n, provider.candidate, ("RAILWAY_TOKEN", "t"))


# Local authority: every refusal named by the incident review.

@pytest.fixture
def proof(m, monkeypatch, tmp_path, candidate):
    g = m["local_proof"].__globals__
    state, control, etc = tmp_path / "state", tmp_path / "control", tmp_path / "etc"
    root = control / "incident-dispositions" / m["INCIDENT"]
    for directory in (state / "candidates", state / "receipts", state / "reconciliations", control, etc):
        directory.mkdir(parents=True, exist_ok=True)
    body = deepcopy(candidate)
    receipt = b'{"synthetic": "predecessor receipt"}\n'
    body["host_deployed_sha"] = "b" * 40
    body["predecessor"]["receipt_sha256"] = m["digest"](receipt)
    body["predecessor"]["archive_path"] = str(state / "receipts" / f"{m['digest'](receipt)}.json")
    raw = {"candidate": m["canonical"](body),
           "hold": m["canonical"]({"rollback": {"attempt_path": None, "attempt_sha256": None, "attempted": False},
                                   "status": "DATA HOLD"}),
           "pin": b'{"synthetic": "pin"}\n', "upload": b'{"code": "UPLOAD_FAILED"}\n'}
    journal = m["digest"](raw["candidate"])
    paths = {"STATE": state, "CONTROL": control, "ROOT": root,
             "BASE_PIN": etc / "railway-publication-base.json",
             "DATA_HOLD": etc / "railway-publication-data-hold.json",
             "DEPLOYED_COMMIT": etc / "deployed-commit",
             "MAINTENANCE_HOLD": tmp_path / "continuity" / "maintenance-hold.json"}
    for path, data in {state / "pending-candidate.json": raw["candidate"],
                       state / "candidates" / f"{journal}.json": raw["candidate"],
                       state / "latest-success.json": receipt,
                       Path(body["predecessor"]["archive_path"]): receipt,
                       state / "last-upload.json": raw["upload"],
                       paths["BASE_PIN"]: raw["pin"], paths["DATA_HOLD"]: raw["hold"],
                       paths["DEPLOYED_COMMIT"]: b"b" * 40 + b"\n"}.items():
        path.write_bytes(data)
    for key, value in {**paths, "JOURNAL": journal, "HOLD": m["digest"](raw["hold"]),
                       "PIN": m["digest"](raw["pin"]), "UPLOAD": m["digest"](raw["upload"])}.items():
        monkeypatch.setitem(g, key, value)
    calls = []
    n = {"_directory": lambda *a, **kw: None,
         "_read": lambda path, **kw: Path(path).read_bytes(),
         "_validate_candidate": lambda c: calls.append("candidate"),
         "_validate_pin": lambda p: calls.append("pin"),
         "_validate_candidate_pin_binding": lambda c, p, r: calls.append("binding"),
         "_validate_bundle": lambda c, **kw: calls.append("bundle"),
         "_validate_release_manifest_anchor": lambda c, **kw: calls.append("manifest"),
         "_matching_preparation": lambda c, **kw: False,
         "_predecessor_from_receipt": lambda raw_, receipt_, pin: deepcopy(body["predecessor"]),
         "_read_attempt": lambda *a, **kw: None,
         "_validate_evidence": lambda c, **kw: (b"provider", b"provider")}
    return SimpleNamespace(m=m, g=g, n=n, state=state, control=control, etc=etc, root=root, raw=raw,
                           body=body, journal=journal, calls=calls, tmp=tmp_path, paths=paths, monkeypatch=monkeypatch)


def prove(proof, **kw):
    return proof.m["local_proof"](proof.n, os.getuid(), os.getgid(), **kw)


def test_local_proof_admits_only_the_complete_exact_incident(proof):
    candidate, originals = prove(proof)
    assert candidate == proof.body
    assert set(originals) == {"candidate.json", "pin.json", "latest-success.json", "upload.json",
                              "hold.json", "provider-manifest.json", "public-manifest.json"}
    assert proof.calls == ["candidate", "pin", "binding", "bundle", "manifest"]


@pytest.mark.parametrize("name,key", [("candidate.json", None), ("hold.json", "DATA_HOLD"),
                                      ("pin.json", "BASE_PIN"), ("upload.json", None)])
def test_digest_mismatch_refused(proof, name, key):
    path = proof.paths[key] if key else proof.state / {"candidate.json": "pending-candidate.json",
                                                         "upload.json": "last-upload.json"}[name]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(proof.m["Refused"], match=f"{name} is not the exact incident"):
        prove(proof)


def test_exact_october_digests_refuse_any_other_bytes(proof):
    real = runpy.run_path(str(HELPER))
    for key in ("JOURNAL", "HOLD", "PIN", "UPLOAD"):
        proof.monkeypatch.setitem(proof.g, key, real[key])
    with pytest.raises(proof.m["Refused"], match="is not the exact incident"):
        prove(proof)


@pytest.mark.parametrize("field,value", [("submission_id", SEPTEMBER_SUBMISSION),
                                         ("message", f"palimpsest-hetzner-x-y-{SEPTEMBER_SUBMISSION}"),
                                         ("prepared_at", "2026-09-28T15:46:21Z"),
                                         ("predecessor", "other-deployment")])
def test_candidate_must_be_the_exact_october_submission(proof, field, value):
    body = deepcopy(proof.body)
    if field == "predecessor":
        body["predecessor"]["deployment_id"] = value
    else:
        body[field] = value
    raw = proof.m["canonical"](body)
    (proof.state / "pending-candidate.json").write_bytes(raw)
    proof.monkeypatch.setitem(proof.g, "JOURNAL", proof.m["digest"](raw))
    with pytest.raises(proof.m["Refused"], match="exact 20 October submission"):
        prove(proof)


def test_existing_recovery_receipt_refused(proof):
    (proof.state / "reconciliations" / f"{proof.journal}.json").write_bytes(b"{}")
    with pytest.raises(proof.m["Refused"], match="normal recovery receipt already exists"):
        prove(proof)


@pytest.mark.parametrize("directory", ["rollback-attempts", "rollback-attempt-topologies", "rollback-responses"])
def test_any_rollback_trace_refused(proof, directory):
    (proof.state / directory).mkdir()
    (proof.state / directory / f"{proof.journal}.json").write_bytes(b"{}")
    with pytest.raises(proof.m["Refused"], match="rollback was attempted"):
        prove(proof)


def test_reconciler_rollback_attempt_record_refused(proof):
    proof.n["_read_attempt"] = lambda *a, **kw: (Path("attempt.json"), "0" * 64, {})
    with pytest.raises(proof.m["Refused"], match="rollback was attempted"):
        prove(proof)


def test_hold_recording_a_rollback_attempt_refused(proof):
    hold = proof.m["canonical"]({"rollback": {"attempt_path": "a", "attempt_sha256": "0" * 64, "attempted": True},
                                 "status": "DATA HOLD"})
    proof.paths["DATA_HOLD"].write_bytes(hold)
    proof.monkeypatch.setitem(proof.g, "HOLD", proof.m["digest"](hold))
    with pytest.raises(proof.m["Refused"], match="hold records rollback uncertainty"):
        prove(proof)


@pytest.mark.parametrize("kind", ["unmatched-file", "dangling-symlink"])
def test_present_preparation_file_refused_even_when_reconciler_would_not_match(proof, kind):
    path = proof.state / "pending-preparation.json"
    if kind == "unmatched-file":
        path.write_bytes(b'{"schema_version": "palimpsest.direct-publication-preparation.v1"}\n')
    else:
        path.symlink_to(proof.tmp / "missing-preparation.json")
    with pytest.raises(proof.m["Refused"], match="pre-mutation evidence exists"):
        prove(proof)


def test_reconciler_matching_preparation_refused(proof):
    proof.n["_matching_preparation"] = lambda c, **kw: True
    with pytest.raises(proof.m["Refused"], match="pre-mutation evidence exists"):
        prove(proof)


@pytest.mark.parametrize("gate", ["maintenance", "rotation"])
def test_concurrent_maintenance_or_rotation_refused(proof, gate):
    path = proof.paths["MAINTENANCE_HOLD"] if gate == "maintenance" else proof.control / "rotation-intent.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{}")
    with pytest.raises(proof.m["Refused"], match="maintenance or rotation"):
        prove(proof)


@pytest.mark.parametrize("fault,message", [("deployed", "host deployment changed"),
                                           ("receipt", "latest publication receipt moved"),
                                           ("binding", "predecessor receipt binding differs"),
                                           ("archive", "immutable candidate archive differs"),
                                           ("predecessor-archive", "immutable predecessor receipt differs")])
def test_moved_host_receipt_or_archives_refused(proof, fault, message):
    if fault == "deployed":
        proof.paths["DEPLOYED_COMMIT"].write_bytes(b"c" * 40 + b"\n")
    if fault == "receipt":
        (proof.state / "latest-success.json").write_bytes(b'{"newer": "receipt"}\n')
    if fault == "binding":
        proof.n["_predecessor_from_receipt"] = lambda *a: {"deployment_id": "other"}
    if fault == "archive":
        (proof.state / "candidates" / f"{proof.journal}.json").write_bytes(b"{}")
    if fault == "predecessor-archive":
        Path(proof.body["predecessor"]["archive_path"]).write_bytes(b"{}")
    with pytest.raises(proof.m["Refused"], match=message):
        prove(proof)


def test_reentry_after_hold_consumption_reads_only_the_retained_original(proof):
    original = proof.paths["DATA_HOLD"].read_bytes()
    proof.root.mkdir(parents=True)
    (proof.root / "original-hold.json").write_bytes(original)
    proof.paths["DATA_HOLD"].unlink()
    with pytest.raises(FileNotFoundError):
        prove(proof)  # Without a durable decision a missing hold is never treated as consumed.
    _, originals = prove(proof, hold_consumed=True)
    assert originals["hold.json"] == original


def test_reentry_refuses_a_hold_raised_again_after_consumption(proof):
    # A hold re-raised between the two unlinks is not the reviewed original.
    proof.root.mkdir(parents=True)
    (proof.root / "original-hold.json").write_bytes(proof.paths["DATA_HOLD"].read_bytes())
    proof.paths["DATA_HOLD"].write_bytes(proof.raw["hold"].replace(b"DATA HOLD", b"DATA HOLD again"))
    with pytest.raises(proof.m["Refused"], match="hold.json is not the exact incident"):
        prove(proof, hold_consumed=True)


# The complete dry run and disposition sequence.

@pytest.fixture
def flow(m, monkeypatch, tmp_path, candidate):
    g = m["execute"].__globals__
    state, control, etc = tmp_path / "state", tmp_path / "control", tmp_path / "etc"
    root = control / "incident-dispositions" / m["INCIDENT"]
    for directory in (state, control, etc):
        directory.mkdir()
    (control / "publish.lock").write_bytes(b"")
    reconciler = tmp_path / "reviewed-reconciler.py"
    reconciler.write_bytes(b"# reviewed reconciler bytes\n")
    hold = etc / "railway-publication-data-hold.json"
    originals = {"candidate.json": b"candidate", "pin.json": b"pin", "latest-success.json": b"receipt",
                 "upload.json": b"upload", "hold.json": b"hold",
                 "provider-manifest.json": b"manifest", "public-manifest.json": b"manifest"}
    hold.write_bytes(originals["hold.json"])
    (state / "pending-candidate.json").write_bytes(originals["candidate.json"])
    events_, proofs = [], []

    def atomic(path, raw, *, uid, gid, mode, replace):
        if path.exists() and not replace:
            raise FileExistsError(path)
        events_.append(("write", path.name))
        path.write_bytes(raw)

    def unlink(path):
        events_.append(("unlink", path.name))
        path.unlink()

    def local_proof(n, uid, gid, *, hold_consumed=False):
        events_.append(("local_proof", hold_consumed))
        return deepcopy(candidate), dict(proofs.pop(0) if proofs else originals)

    def observe(n, candidate_, token):
        count = sum(1 for event in events_ if event[0] == "observe")
        events_.append(("observe", token[0]))
        started = datetime(2026, 10, 31, 19, 20, tzinfo=UTC) + timedelta(seconds=40 * count)
        return ({"started_at": started.isoformat(), "finished_at": (started + timedelta(seconds=5)).isoformat(),
                 "active": {"deployment_id": PREDECESSOR}, "artifacts": {}},
                {"deployments.json": f"deployments-{count}".encode(), "events.json": b"events"})

    n = {"_read": lambda path, **kw: Path(path).read_bytes(), "_atomic": atomic, "_unlink": unlink,
         "_ensure_directory": lambda path, **kw: path.mkdir(parents=True, exist_ok=True)}
    for key, value in {"ROOT": root, "STATE": state, "CONTROL": control, "DATA_HOLD": hold, "HELPER": reconciler,
                       "local_proof": local_proof, "observe": observe, "load_helper": lambda: n,
                       "observer_ready": lambda n_, gid: events_.append(("observer_ready",)),
                       "time": SimpleNamespace(sleep=lambda seconds: events_.append(("sleep", seconds)))}.items():
        monkeypatch.setitem(g, key, value)
    monkeypatch.setattr(g["os"], "geteuid", lambda: 0)
    monkeypatch.setattr(g["pwd"], "getpwnam", lambda name: SimpleNamespace(pw_uid=os.getuid()))
    monkeypatch.setattr(g["grp"], "getgrnam", lambda name: SimpleNamespace(gr_gid=os.getgid()))
    monkeypatch.setenv("RAILWAY_TOKEN", "synthetic-test-token")
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
    freeze(monkeypatch, g, ACCEPTED)
    return SimpleNamespace(m=m, g=g, root=root, state=state, hold=hold, reconciler=reconciler,
                           originals=originals, events=events_, proofs=proofs, candidate=candidate,
                           monkeypatch=monkeypatch, tmp=tmp_path)


def gates_intact(flow):
    return ((flow.state / "pending-candidate.json").read_bytes() == b"candidate"
            and flow.hold.read_bytes() == b"hold")


def test_dry_run_is_the_default_and_consumes_nothing(flow):
    result = flow.m["execute"](False, None)
    assert result["status"] == "eligible_for_explicit_incident_disposition"
    decision = result["proposed_decision"]
    assert decision["no_mutation_proved"] is False and decision["acknowledgement"] == flow.m["ACK"]
    assert [event[0] for event in flow.events] == ["local_proof", "observe", "sleep", "observe", "local_proof"]
    assert ("sleep", flow.m["OBSERVATION_GAP"]) in flow.events
    assert not flow.root.exists() and gates_intact(flow)


def test_main_without_flags_runs_only_the_dry_run(flow, monkeypatch, capsys):
    seen = []
    monkeypatch.setitem(flow.g, "execute", lambda retire, ack: seen.append((retire, ack)) or {"status": "eligible"})
    monkeypatch.setattr(flow.g["sys"], "argv", [str(HELPER)])
    assert flow.m["main"]() == 0
    assert seen == [(False, None)]


def test_retire_records_decision_before_consuming_hold_then_journal(flow):
    completion = flow.m["execute"](True, flow.m["ACK"])
    order = [event for event in flow.events if event[0] in ("observer_ready", "write", "unlink")]
    assert order[0] == ("observer_ready",)
    hold_removed = order.index(("unlink", "railway-publication-data-hold.json"))
    assert ("write", "decision.json") in order[:hold_removed]
    assert order[hold_removed:] == [("unlink", "railway-publication-data-hold.json"),
                                    ("unlink", "pending-candidate.json"), ("write", "completion.json")]
    assert not flow.hold.exists() and not (flow.state / "pending-candidate.json").exists()
    decision_raw = (flow.root / "decision.json").read_bytes()
    decision = json.loads(decision_raw)
    m = flow.m
    assert set(decision) == {"schema_version", "incident", "journal_sha256", "original_hold_sha256", "recorded_at",
                             "acknowledgement", "no_mutation_proved", "residual_uncertainty", "provider_mutations",
                             "routine_reconciler_changed", "reconciler_sha256", "observations", "originals",
                             "submission_id", "helper_sha256", "disposition_window", "reviewed_later_events"}
    assert decision["schema_version"] == "palimpsest.explicit-incident-disposition.v1"
    assert (decision["incident"], decision["journal_sha256"], decision["original_hold_sha256"]) == (m["INCIDENT"], m["JOURNAL"], m["HOLD"])
    assert decision["acknowledgement"] == m["ACK"] and decision["submission_id"] == m["SUBMISSION"]
    assert decision["no_mutation_proved"] is False and decision["provider_mutations"] == []
    assert decision["routine_reconciler_changed"] is False and decision["reconciler_sha256"] == m["RECONCILER"]
    assert decision["residual_uncertainty"] == m["RESIDUAL"]
    assert decision["helper_sha256"] == m["digest"](HELPER.read_bytes())
    assert decision["disposition_window"] == {"min_age_seconds": 86400, "max_age_seconds": 1209600,
                                              "incident_age_seconds": 957994}  # 266.11 hours
    assert decision["reviewed_later_events"] == m["REVIEWED_LATER_EVENTS"]
    assert len(decision["observations"]) == 2 and decision["recorded_at"] == ACCEPTED.isoformat()
    assert decision["originals"]["hold.json"] == {"sha256": m["digest"](b"hold"), "bytes": 4}
    assert completion == json.loads((flow.root / "completion.json").read_bytes()) == {
        "schema_version": "palimpsest.incident-disposition-completion.v1", "incident": m["INCIDENT"],
        "decision_sha256": m["digest"](decision_raw),
        "outcome": "retired_with_unregistered_provider_job_uncertainty",
        "no_mutation_proved": False, "residual_uncertainty": m["RESIDUAL"]}
    assert (flow.root / "original-reconciler.py").read_bytes() == flow.reconciler.read_bytes()
    for name, raw in flow.originals.items():
        assert (flow.root / f"original-{name}").read_bytes() == raw
    for label, deployments_raw in (("first", b"deployments-0"), ("second", b"deployments-1")):
        assert (flow.root / f"{label}-{m['digest'](deployments_raw)}-deployments.json").read_bytes() == deployments_raw


def test_retire_refused_without_a_fresh_october_observation(flow, monkeypatch):
    def not_ready(n, gid):
        raise flow.m["Refused"]("a recent successful observation by this exact helper is required")
    monkeypatch.setitem(flow.g, "observer_ready", not_ready)
    with pytest.raises(flow.m["Refused"], match="recent successful observation"):
        flow.m["execute"](True, flow.m["ACK"])
    assert not flow.root.exists() and gates_intact(flow)


@pytest.mark.parametrize("now", [datetime(2026, 11, 3, 17, 13, 27, tzinfo=UTC),
                                 datetime(2026, 10, 21, 17, 13, 25, tzinfo=UTC)])
def test_execute_refuses_outside_window_before_contacting_provider(flow, monkeypatch, now):
    freeze(monkeypatch, flow.g, now)
    with pytest.raises(flow.m["Refused"], match="disposition window"):
        flow.m["execute"](True, flow.m["ACK"])
    assert [event[0] for event in flow.events] == ["local_proof"]
    assert not flow.root.exists() and gates_intact(flow)


def test_window_closing_during_observation_refuses(flow, monkeypatch):
    freeze(monkeypatch, flow.g, datetime(2026, 11, 3, 17, 13, 0, tzinfo=UTC), datetime(2026, 11, 3, 17, 14, 0, tzinfo=UTC))
    with pytest.raises(flow.m["Refused"], match="disposition window"):
        flow.m["execute"](True, flow.m["ACK"])
    assert not flow.root.exists() and gates_intact(flow)


def test_local_authority_change_during_observation_refuses(flow):
    flow.proofs.extend([dict(flow.originals), {**flow.originals, "hold.json": b"raised again"}])
    with pytest.raises(flow.m["Refused"], match="local authority changed"):
        flow.m["execute"](True, flow.m["ACK"])
    assert not flow.root.exists() and gates_intact(flow)


def test_predecessor_change_between_observations_refuses(flow, monkeypatch):
    observe = flow.g["observe"]
    def moving(n, candidate, token):
        result, raw = observe(n, candidate, token)
        if sum(1 for event in flow.events if event[0] == "observe") == 2:
            result["active"] = {"deployment_id": "candidate-deployment"}
        return result, raw
    monkeypatch.setitem(flow.g, "observe", moving)
    with pytest.raises(flow.m["Refused"], match="predecessor changed between observations"):
        flow.m["execute"](True, flow.m["ACK"])
    assert not flow.root.exists() and gates_intact(flow)


def test_paired_observations_must_finish_within_their_bound(flow, monkeypatch):
    observe = flow.g["observe"]
    def slow(n, candidate, token):
        result, raw = observe(n, candidate, token)
        result["finished_at"] = (datetime.fromisoformat(result["started_at"]) + timedelta(seconds=301)).isoformat()
        return result, raw
    monkeypatch.setitem(flow.g, "observe", slow)
    with pytest.raises(flow.m["Refused"], match="paired observations exceeded"):
        flow.m["execute"](True, flow.m["ACK"])
    assert not flow.root.exists() and gates_intact(flow)


def test_reentry_after_hold_consumption_reproves_and_consumes_only_the_journal(flow):
    m = flow.m
    previous = {"incident": m["INCIDENT"], "journal_sha256": m["JOURNAL"],
                "no_mutation_proved": False, "residual_uncertainty": m["RESIDUAL"], "recorded_at": "durable"}
    flow.root.mkdir(parents=True)
    (flow.root / "decision.json").write_bytes(m["canonical"](previous))
    flow.hold.unlink()
    result = m["execute"](True, m["ACK"])
    assert ("local_proof", True) in flow.events and ("observe", "RAILWAY_TOKEN") in flow.events
    assert [event for event in flow.events if event[0] == "unlink"] == [("unlink", "pending-candidate.json")]
    assert (flow.root / "decision.json").read_bytes() == m["canonical"](previous)
    assert result["decision_sha256"] == m["digest"](m["canonical"](previous))


def test_restart_after_journal_consumption_writes_only_separate_completion(flow):
    m = flow.m
    decision = {"incident": m["INCIDENT"], "journal_sha256": m["JOURNAL"],
                "no_mutation_proved": False, "residual_uncertainty": m["RESIDUAL"]}
    flow.root.mkdir(parents=True)
    (flow.root / "decision.json").write_bytes(m["canonical"](decision))
    flow.hold.unlink()
    (flow.state / "pending-candidate.json").unlink()
    result = m["execute"](True, m["ACK"])
    assert flow.events == [("write", "completion.json")]
    assert result["no_mutation_proved"] is False
    assert result["outcome"] == "retired_with_unregistered_provider_job_uncertainty"
    assert (flow.root / "decision.json").read_bytes() == m["canonical"](decision)


def test_restart_refuses_when_original_hold_remains_without_candidate(flow, monkeypatch):
    m = flow.m
    decision = {"incident": m["INCIDENT"], "journal_sha256": m["JOURNAL"],
                "no_mutation_proved": False, "residual_uncertainty": m["RESIDUAL"]}
    flow.root.mkdir(parents=True)
    (flow.root / "decision.json").write_bytes(m["canonical"](decision))
    (flow.state / "pending-candidate.json").unlink()
    monkeypatch.setitem(flow.g, "HOLD", m["digest"](b"hold"))
    with pytest.raises(m["Refused"], match="original hold remains"):
        m["execute"](True, m["ACK"])
    assert flow.hold.read_bytes() == b"hold" and flow.events == []


@pytest.mark.parametrize("field,value", [("incident", "palimpsest-upload-502-20260928"),
                                         ("journal_sha256", "e06381214a2c40495a83daa38d2137586a140bc5d525cd480c10a37abb87ae02"),
                                         ("no_mutation_proved", True), ("residual_uncertainty", "proved")])
def test_existing_decision_for_anything_else_refuses(flow, field, value):
    m = flow.m
    decision = {"incident": m["INCIDENT"], "journal_sha256": m["JOURNAL"],
                "no_mutation_proved": False, "residual_uncertainty": m["RESIDUAL"], field: value}
    flow.root.mkdir(parents=True)
    (flow.root / "decision.json").write_bytes(m["canonical"](decision))
    with pytest.raises(m["Refused"], match="existing incident decision differs"):
        m["execute"](True, m["ACK"])
    assert gates_intact(flow)


# Gate consumption, copied from the September proofs.

def test_immutable_evidence_refuses_conflict(m, tmp_path):
    path = tmp_path / "evidence"
    path.write_bytes(b"original")
    writes = []
    n = {"_read": lambda path, **kw: path.read_bytes(), "_atomic": lambda *a, **kw: writes.append(a)}
    with pytest.raises(m["Refused"], match="conflicts"):
        m["immutable"](n, path, b"different", 0)
    assert writes == [] and path.read_bytes() == b"original"


def test_decision_is_durable_before_gate_consumption(m, monkeypatch):
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


def test_candidate_disappearing_before_consumption_refuses(m, monkeypatch, tmp_path):
    g = m["consume"].__globals__
    monkeypatch.setitem(g, "immutable", lambda *args: None)
    monkeypatch.setitem(g, "DATA_HOLD", tmp_path / "railway-publication-data-hold.json")
    monkeypatch.setitem(g, "STATE", tmp_path)
    monkeypatch.setattr(g["pwd"], "getpwnam", lambda name: SimpleNamespace(pw_uid=123))
    with pytest.raises(m["Refused"], match="candidate disappeared"):
        m["consume"]({"_unlink": lambda path: pytest.fail("nothing may be consumed")}, {}, b"decision", 0)


# Acknowledgement and invocation.

@pytest.mark.parametrize("acknowledgement", [
    None, "", "accept", "accept-unregistered-provider-job-uncertainty-for-" + SEPTEMBER_SUBMISSION,
    "accept-unregistered-provider-job-uncertainty-for-ed73080a31732f0ca2ce656a8c5023c8079c41de5316f5cc5106157fbc39adc8",
    "accept-unregistered-provider-job-uncertainty-for-f0e823dcc632569cbfcff69f2b1eef78 ",
    "ACCEPT-UNREGISTERED-PROVIDER-JOB-UNCERTAINTY-FOR-F0E823DCC632569CBFCFF69F2B1EEF78"])
def test_wrong_acknowledgement_refused_before_loading_dependency(m, monkeypatch, acknowledgement):
    g = m["execute"].__globals__
    monkeypatch.setattr(g["os"], "geteuid", lambda: 0)
    monkeypatch.setitem(g, "load_helper", lambda: pytest.fail("dependency loaded before acknowledgement"))
    with pytest.raises(m["Refused"], match="acknowledgement"):
        m["execute"](True, acknowledgement)


def test_exact_acknowledgement_passes_only_the_acknowledgement_gate(m, monkeypatch):
    g = m["execute"].__globals__
    monkeypatch.setattr(g["os"], "geteuid", lambda: 0)
    def reached():
        raise RuntimeError("dependency reached")
    monkeypatch.setitem(g, "load_helper", reached)
    with pytest.raises(RuntimeError, match="dependency reached"):
        m["execute"](True, m["ACK"])


def test_main_refuses_retire_without_acknowledgement(m, monkeypatch, capsys):
    g = m["main"].__globals__
    monkeypatch.setattr(g["os"], "geteuid", lambda: 0)
    monkeypatch.setitem(g, "load_helper", lambda: pytest.fail("dependency loaded before acknowledgement"))
    monkeypatch.setattr(g["sys"], "argv", [str(HELPER), "--retire"])
    assert m["main"]() == 1
    assert "acknowledgement" in capsys.readouterr().err


def test_observer_and_disposition_modes_cannot_be_combined(m, monkeypatch, capsys):
    monkeypatch.setattr(m["main"].__globals__["sys"], "argv", [str(HELPER), "--observe", "--retire"])
    assert m["main"]() == 1
    assert "cannot be combined" in capsys.readouterr().err


@pytest.fixture
def scoped(m, monkeypatch, tmp_path):
    g = m["load_helper"].__globals__
    for key in [k for k in os.environ if k.startswith("PALIMPSEST_")]:
        monkeypatch.delenv(key)
    for key, value in {"RAILWAY_PROJECT_ID": m["PROJECT"], "RAILWAY_ENVIRONMENT_ID": m["ENVIRONMENT"],
                       "RAILWAY_SERVICE_ID": m["SERVICE"]}.items():
        monkeypatch.setenv(key, value)
    dependency = tmp_path / "installed-reconciler"
    dependency.write_bytes(b"REVIEWED = 'installed'\n")
    dependency.chmod(0o644)  # Not group or world writable, whatever the local umask.
    root = tmp_path / "incident"
    root.mkdir()
    monkeypatch.setitem(g, "HELPER", dependency)
    monkeypatch.setitem(g, "ROOT", root)
    monkeypatch.setitem(g, "RECONCILER", m["digest"](dependency.read_bytes()))
    real_lstat = Path.lstat
    def root_owned(self):
        st = real_lstat(self)
        return SimpleNamespace(st_uid=0, st_mode=st.st_mode, st_size=st.st_size)
    return SimpleNamespace(m=m, g=g, dependency=dependency, root=root, monkeypatch=monkeypatch, root_owned=root_owned)


def test_reviewed_dependency_loads_only_when_root_owned_and_exact(scoped):
    with pytest.raises(scoped.m["Refused"], match="not root-controlled"):
        scoped.m["load_helper"]()  # The test file belongs to this user, not root.
    scoped.monkeypatch.setattr(Path, "lstat", scoped.root_owned)
    assert scoped.m["load_helper"]()["REVIEWED"] == "installed"
    scoped.dependency.write_bytes(b"REVIEWED = 'upgraded'\n")
    with pytest.raises(scoped.m["Refused"], match="differs from the reviewed incident dependency"):
        scoped.m["load_helper"]()


def test_observer_prefers_the_archived_reconciler_after_disposition(scoped):
    scoped.monkeypatch.setattr(Path, "lstat", scoped.root_owned)
    (scoped.root / "original-reconciler.py").write_bytes(scoped.dependency.read_bytes().replace(b"installed", b"archived"))
    (scoped.root / "original-reconciler.py").chmod(0o600)
    scoped.monkeypatch.setitem(scoped.g, "RECONCILER", scoped.m["digest"]((scoped.root / "original-reconciler.py").read_bytes()))
    scoped.dependency.write_bytes(b"REVIEWED = 'routine upgrade'\n")
    assert scoped.m["load_helper"](observer=True)["REVIEWED"] == "archived"
    with pytest.raises(scoped.m["Refused"], match="differs from the reviewed incident dependency"):
        scoped.m["load_helper"]()  # Disposition itself never uses the archive.


@pytest.mark.parametrize("variable,value", [("RAILWAY_PROJECT_ID", "other"), ("RAILWAY_ENVIRONMENT_ID", "other"),
                                            ("RAILWAY_SERVICE_ID", None), ("PALIMPSEST_RAILWAY_STATE_ROOT", "/tmp/elsewhere"),
                                            ("PALIMPSEST_PUBLIC_ORIGIN", "https://example.org")])
def test_dependency_refuses_other_scope_or_path_overrides(scoped, variable, value):
    scoped.monkeypatch.setattr(Path, "lstat", scoped.root_owned)
    if value is None:
        scoped.monkeypatch.delenv(variable)
    else:
        scoped.monkeypatch.setenv(variable, value)
    with pytest.raises(scoped.m["Refused"], match="scope differs|overrides are forbidden"):
        scoped.m["load_helper"]()


@pytest.mark.parametrize("tokens", [{}, {"RAILWAY_TOKEN": "a", "RAILWAY_API_TOKEN": "b"}])
def test_exactly_one_railway_token_is_required(flow, monkeypatch, tokens):
    monkeypatch.delenv("RAILWAY_TOKEN", raising=False)
    for key, value in tokens.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(flow.m["Refused"], match="exactly one Railway token"):
        flow.m["execute"](False, None)
    assert flow.events == [] and gates_intact(flow)


def test_observer_refuses_a_changed_retained_archive(m, observer, monkeypatch):
    monkeypatch.setitem(observer.g, "JOURNAL", "0" * 64)
    with pytest.raises(m["Refused"], match="retained candidate changed"):
        m["observe_retired"]()
    assert observer.events == [] and observer.writes == {}


def test_nonroot_cannot_dispose_or_observe(m, monkeypatch):
    monkeypatch.setattr(m["execute"].__globals__["os"], "geteuid", lambda: 501)
    with pytest.raises(m["Refused"], match="requires root"):
        m["execute"](False, None)
    with pytest.raises(m["Refused"], match="requires root"):
        m["observe_retired"]()


# The October observer: readiness, matching and the local DATA HOLD only.

@pytest.fixture
def ready(m, monkeypatch, tmp_path):
    g = m["observer_ready"].__globals__
    root = tmp_path / "incident"
    root.mkdir()
    monkeypatch.setitem(g, "ROOT", root)
    systemctl, failing = [], set()
    def run(args, **kw):
        systemctl.append(args)
        return SimpleNamespace(returncode=1 if args[1] in failing else 0)
    monkeypatch.setattr(g["subprocess"], "run", run)
    base = {"incident": m["INCIDENT"], "journal_sha256": m["JOURNAL"],
            "status": "no_matching_candidate_observed", "helper_sha256": m["digest"](HELPER.read_bytes())}
    def write(**changes):
        value = {**base, "observed_at": datetime.now(UTC).isoformat(), **changes}
        (root / "observer-latest.json").write_bytes(m["canonical"](value))
    return SimpleNamespace(m=m, n={"_read": lambda path, **kw: path.read_bytes()}, write=write,
                           systemctl=systemctl, failing=failing)


def test_observer_ready_requires_the_october_timer_and_a_fresh_exact_observation(ready):
    ready.write()
    ready.m["observer_ready"](ready.n, 0)
    assert [(args[1], args[-1]) for args in ready.systemctl] == [("is-enabled", TIMER_UNIT.name),
                                                                ("is-active", TIMER_UNIT.name)]


@pytest.mark.parametrize("changes", [
    {"incident": "palimpsest-upload-502-20260928"},
    {"journal_sha256": "e06381214a2c40495a83daa38d2137586a140bc5d525cd480c10a37abb87ae02"},
    {"status": "late_candidate_observed"}, {"status": "late_candidate_observed_hold_deferred"},
    {"status": "late_candidate_observed_hold_failed"},
    {"helper_sha256": "fc6e5383821ff24ebb804bfdf3aaacbbeb3d1cad5de3989e747541795c51d04c"},
    "stale", "future"])
def test_observer_ready_refuses_foreign_late_stale_or_future_observation(ready, changes):
    if changes == "stale":
        changes = {"observed_at": (datetime.now(UTC) - timedelta(seconds=301)).isoformat()}
    if changes == "future":
        changes = {"observed_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat()}
    ready.write(**changes)
    with pytest.raises(ready.m["Refused"], match="recent successful observation"):
        ready.m["observer_ready"](ready.n, 0)


@pytest.mark.parametrize("operation", ["is-enabled", "is-active"])
def test_observer_ready_refuses_a_disabled_or_inactive_timer(ready, operation):
    ready.write()
    ready.failing.add(operation)
    with pytest.raises(ready.m["Refused"], match="not enabled and active"):
        ready.m["observer_ready"](ready.n, 0)


def test_observer_units_are_october_specific_with_september_sandbox(m):
    service, timer = SERVICE_UNIT.read_text(), TIMER_UNIT.read_text()
    assert "ExecStart=/usr/local/sbin/palimpsest-retire-upload-502-20261020 --observe\n" in service
    assert "20260928" not in service and "OnUnitInactiveSec=1min" in timer
    def body(text):
        return [line for line in text.splitlines() if not line.startswith(("Description=", "ExecStart="))]
    assert body(service) == body(SEPTEMBER_SERVICE.read_text())
    assert body(timer) == body(SEPTEMBER_TIMER.read_text())


def test_observer_is_bounded_local_hold_only(m):
    source = HELPER.read_text()
    assert "no_matching_candidate_observed" in source
    assert 'reason="candidate_identity_ambiguous"' in source
    assert "observer_ready(n, gid)" in source


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


@pytest.mark.parametrize("message", ["palimpsest-hetzner-x-y-" + SEPTEMBER_SUBMISSION, MESSAGE[:-1], MESSAGE + "0"])
def test_observer_does_not_conflate_another_submission(m, candidate, message):
    rows = deployments(m)
    rows[1]["meta"]["cliMessage"] = message
    assert m["matching_candidates"](rows, b"{}", candidate["message"]) == {}


def test_observer_refuses_malformed_topology(m, candidate):
    with pytest.raises(json.JSONDecodeError):
        m["matching_candidates"](deployments(m), b"not-json", candidate["message"])


@pytest.fixture
def observer(m, monkeypatch, candidate, tmp_path):
    g = m["observe_retired"].__globals__
    raw = m["canonical"](candidate)
    monkeypatch.setitem(g, "JOURNAL", m["digest"](raw))
    monkeypatch.setitem(g, "ROOT", tmp_path / "incident")
    monkeypatch.setitem(g, "CONTROL", tmp_path)
    monkeypatch.setitem(g, "STATE", tmp_path / "state")
    monkeypatch.setattr(g["os"], "geteuid", lambda: 0)
    monkeypatch.setattr(g["pwd"], "getpwnam", lambda _: SimpleNamespace(pw_uid=99))
    monkeypatch.setattr(g["grp"], "getgrnam", lambda _: SimpleNamespace(gr_gid=99))
    monkeypatch.setenv("RAILWAY_TOKEN", "test-token")
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
    reads, writes, retained, events_ = [], {}, {}, []
    n = {"_directory": lambda *a, **kw: None,
         "_read": lambda path, **kw: reads.append(path) or raw,
         "_validate_candidate": lambda _: None,
         "_status": lambda *a: events_.append("status") or b'{"active": []}',
         "_topology": lambda _: events_.append("topology"),
         "_ensure_directory": lambda *a, **kw: None,
         "_atomic": lambda path, data, **kw: writes.update({path.name: json.loads(data)})}
    monkeypatch.setitem(g, "load_helper", lambda **kw: n)
    monkeypatch.setitem(g, "api", lambda *a: events_.append("inventory") or page(m, "deployments", deployments(m)))
    monkeypatch.setitem(g, "immutable", lambda n, path, data, gid: retained.update({path.name: data}))
    monkeypatch.setitem(g, "hold_if_idle", lambda *a: pytest.fail("unexpected hold lock"))
    return SimpleNamespace(g=g, n=n, reads=reads, writes=writes, retained=retained, events=events_, tmp=tmp_path)


def test_observation_reads_only_the_immutable_october_archive_without_writer_lock(m, observer, monkeypatch):
    monkeypatch.setattr(observer.g["fcntl"], "flock", lambda *a: pytest.fail("writer lock requested for read-only observation"))
    result = m["observe_retired"]()
    assert result["status"] == "no_matching_candidate_observed"
    assert result["incident"] == "palimpsest-upload-502-20261020"
    assert result["helper_sha256"] == m["digest"](HELPER.read_bytes())
    assert observer.events == ["inventory", "status", "topology"]
    assert observer.reads == [observer.tmp / "state" / "candidates" / f"{observer.g['JOURNAL']}.json"]
    assert observer.writes["observer-latest.json"] == result


@pytest.mark.parametrize("hold_result", [False, True, "conflict"])
def test_late_observation_retained_and_fails_even_if_hold_deferred_or_conflicts(m, observer, monkeypatch, candidate, hold_result):
    status = m["canonical"]({"active": [{"id": "late", "meta": {"cliMessage": candidate["message"]}}]})
    observer.n["_status"] = lambda *a: status
    def hold(*a):
        assert observer.retained  # Raw evidence precedes any attempted hold.
        if hold_result == "conflict":
            raise m["Refused"]("existing unrelated hold")
        return hold_result
    monkeypatch.setitem(observer.g, "hold_if_idle", hold)
    result = m["observe_retired"]()
    expected = {False: "late_candidate_observed_hold_deferred", True: "late_candidate_observed",
                "conflict": "late_candidate_observed_hold_failed"}[hold_result]
    assert result["status"] == expected
    assert result["hold_written"] is (hold_result is True)
    assert result["matching_deployment_ids"] == ["late"]
    assert len(observer.retained) == 3
    assert observer.writes["observer-latest.json"] == result
    monkeypatch.setitem(observer.g, "observe_retired", lambda: result)
    monkeypatch.setattr(observer.g["sys"], "argv", [str(HELPER), "--observe"])
    assert m["main"]() == 1


@pytest.mark.parametrize("busy", [False, True])
def test_hold_write_uses_real_nonblocking_writer_lock_for_the_october_candidate(m, monkeypatch, tmp_path, candidate, busy):
    import fcntl
    g = m["hold_if_idle"].__globals__
    monkeypatch.setitem(g, "CONTROL", tmp_path)
    lock = tmp_path / "publish.lock"
    lock.write_bytes(b"")
    writes = []
    n = {"_read": lambda path, **kw: path.read_bytes(), "_write_hold": lambda *a, **kw: writes.append(kw)}
    fd = os.open(lock, os.O_RDONLY)
    try:
        if busy:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert m["hold_if_idle"](n, candidate, os.getgid()) is (not busy)
        assert len(writes) == int(not busy)
    finally:
        os.close(fd)
    if busy:
        assert m["hold_if_idle"](n, candidate, os.getgid()) is True
        assert len(writes) == 1
    assert writes[0]["journal"] == m["JOURNAL"]
    assert writes[0]["archive"] == m["STATE"] / "candidates" / f"{m['JOURNAL']}.json"
    assert writes[0]["reason"] == "candidate_identity_ambiguous" and writes[0]["attempt"] is None


def test_unexpected_observer_blocking_error_is_not_success_exit_75(m, monkeypatch, capsys):
    g = m["main"].__globals__
    def fail():
        raise BlockingIOError("unexpected provider or local I/O error")
    monkeypatch.setitem(g, "observe_retired", fail)
    monkeypatch.setattr(g["sys"], "argv", [str(HELPER), "--observe"])
    assert m["main"]() == 1
    assert "incident observation failed" in capsys.readouterr().err


def test_busy_publication_lock_during_disposition_is_retryable_exit_75(m, monkeypatch, capsys):
    g = m["main"].__globals__
    def busy(retire, acknowledgement):
        raise BlockingIOError("lock busy")
    monkeypatch.setitem(g, "execute", busy)
    monkeypatch.setattr(g["sys"], "argv", [str(HELPER)])
    assert m["main"]() == 75
    assert "publication lock is busy" in capsys.readouterr().err
