"""Both retired-upload observers defer, never pass, during one proven rollout.

During an ordinary publication Railway briefly reports two active deployments,
so the strict topology parser refuses. Each observer has already searched the
inventory and topology for its retired message by then. It may exit 75, which
its unit maps through SuccessExitStatus, only when the second deployment is
exactly the in-flight publication: a fresh journal, a bound running publisher
and the captured predecessor. Every other refusal still exits 1. These tests
use the real reconciler parser, candidate validator and strict file reader.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPERS = {
    "20260928": ROOT / "ops/railway/retire-upload-502-20260928",
    "20261020": ROOT / "ops/railway/retire-upload-502-20261020",
}
UNITS = {
    "20260928": ROOT / "ops/systemd/palimpsest-retired-upload-observer.service",
    "20261020": ROOT / "ops/systemd/palimpsest-retired-upload-observer-20261020.service",
}
RECONCILER = ROOT / "ops/railway/reconcile-direct-publication-candidate"
PUBLISHER = ROOT / "ops/railway/palimpsest-railway-publish"
SUBMISSIONS = {"20260928": "600a32145644507e6111213ecd18edfc", "20261020": "f0e823dcc632569cbfcff69f2b1eef78"}
IMAGE = "sha256:" + "a" * 64
LIVE = "11111111-2222-4333-8444-555555555555"
IN_FLIGHT = "66666666-7777-4888-9999-aaaaaaaaaaaa"
OLDER = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
LATE = "12345678-9abc-4def-8123-456789abcdef"
# Shaped like the 02:24:40Z publication of 2026-11-01: submitted at the
# boundary, observers refused 57 and 119 seconds later, committed at 02:27:17Z.
PREPARED = datetime(2026, 11, 1, 2, 24, 40, tzinfo=UTC)
DEADLINE = PREPARED + timedelta(seconds=600)
STARTED = datetime(2026, 11, 1, 2, 11, 19, tzinfo=UTC)
NOW = PREPARED + timedelta(seconds=57)


def stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def journal(m: dict, *, release: str, input_sha: str, submission: str, prepared: datetime,
            deadline: datetime, predecessor: str, predecessor_created: str) -> dict:
    """A complete v2 journal that the real reconciler validator accepts."""
    wire = stamp(prepared - timedelta(minutes=14))
    return {
        "schema_version": "palimpsest.direct-publication-candidate.v2",
        "status": "mutation_unresolved",
        "prepared_at": stamp(prepared),
        "receipt_deadline_at": stamp(deadline),
        "message": f"palimpsest-hetzner-{release[:12]}-{input_sha[:12]}-{submission}",
        "submission_id": submission,
        "input_sha256": input_sha,
        "wire_generated_at": wire,
        "wire_canonical_sha256": "8" * 64,
        "news_source_tail": {"count": 3, "sha256": "9" * 64},
        "host_deployed_sha": "a" * 40,
        "base_sha": "b" * 40,
        "release_sha": release,
        "publication_base": {"kind": "verified_successor", "path": "/etc/palimpsest/railway-publication-base.json",
                             "sha256": "c" * 64},
        "release_bundle": {"bytes": 10, "metadata_path": f"/state/release-bundles/{release}.json",
                           "metadata_sha256": "d" * 64, "path": f"/state/release-bundles/{release}.bundle",
                           "sha256": "e" * 64},
        "release_manifest": {"bytes": 10, "file_count": 1, "path": f"/state/release-manifests/{release}.json",
                             "sha256": "f" * 64, "total_bytes": 10, "tree_sha256": "1" * 64},
        "predecessor": {"archive_path": "/state/receipts/prior.json", "base_sha": "b" * 40,
                        "deployment_id": predecessor, "input_sha256": "2" * 64, "manifest_sha256": "3" * 64,
                        "receipt_sha256": "4" * 64, "release_sha": "5" * 40,
                        "schema_version": "palimpsest.hetzner-railway-publication.v2", "tree_sha256": "6" * 64,
                        "wire_generated_at": wire},
        "rollback_evidence": {
            "captured_at": stamp(prepared),
            "schema_version": "palimpsest.direct-publication-rollback-evidence.v1",
            "provider_manifest": {"bytes": 10, "path": f"/state/predecessors/{release}/provider.json", "sha256": "7" * 64},
            "public_manifest": {"bytes": 10, "path": f"/state/predecessors/{release}/public.json", "sha256": "7" * 64},
            "topology": {"bytes": 10, "created_at": predecessor_created, "deployment_id": predecessor,
                         "environment_id": m["ENVIRONMENT"], "image_digest": IMAGE,
                         "path": f"/state/predecessors/{release}/railway-status.json", "project_id": m["PROJECT"],
                         "reason": "deploy", "service_id": m["SERVICE"], "sha256": "8" * 64},
        },
    }


def deployment(identity: str, message: str, *, created: str, status: str = "SUCCESS", stopped: bool = False) -> dict:
    return {"id": identity, "status": status, "deploymentStopped": stopped, "createdAt": created,
            "instances": [] if stopped else [{"id": "instance", "status": "RUNNING"}],
            "meta": {"cliMessage": message, "imageDigest": IMAGE, "reason": "deploy", "buildOnly": False,
                     "volumeMounts": [],
                     "serviceManifest": {"build": {"builder": "DOCKERFILE", "dockerfilePath": "ops/railway/Dockerfile.static"},
                                         "deploy": {"healthcheckPath": "/healthz", "numReplicas": 1,
                                                    "cronSchedule": None, "requiredMountPath": None}}}}


def topology(m: dict, active: list[dict], latest: dict) -> bytes:
    instance = {"environmentId": m["ENVIRONMENT"], "serviceId": m["SERVICE"], "serviceName": "palimpsest-publication",
                "source": None, "cronSchedule": None, "nextCronRunAt": None,
                "latestDeployment": latest, "activeDeployments": active}
    return json.dumps({
        "id": m["PROJECT"],
        "services": {"edges": [{"node": {"id": m["SERVICE"], "name": "palimpsest-publication"}}]},
        "environments": {"edges": [{"node": {"id": m["ENVIRONMENT"], "canAccess": True, "deletedAt": None,
                                             "volumeInstances": {"edges": []},
                                             "serviceInstances": {"edges": [{"node": instance}]}}}]},
    }).encode()


def unit_show(*, active: str = "activating", sub: str = "start", pid: str = "4242",
              started: datetime | str = STARTED) -> str:
    clock = started if isinstance(started, str) else f"@{int(started.timestamp())}"
    return f"MainPID={pid}\nExecMainStartTimestamp={clock}\nActiveState={active}\nSubState={sub}\n"


@pytest.fixture(params=sorted(HELPERS))
def rollout(request, monkeypatch, tmp_path):
    incident = request.param
    m = runpy.run_path(str(HELPERS[incident]))
    g = m["observe_retired"].__globals__
    for key, value in {"RAILWAY_PROJECT_ID": m["PROJECT"], "RAILWAY_ENVIRONMENT_ID": m["ENVIRONMENT"],
                       "RAILWAY_SERVICE_ID": m["SERVICE"]}.items():
        monkeypatch.setenv(key, value)
    reconciler = runpy.run_path(str(RECONCILER))
    state = tmp_path / "state"
    (state / "candidates").mkdir(parents=True)

    retired = journal(m, release="9c8ba25f1a0c" + "0" * 28, input_sha="7b9c2a923620" + "0" * 52,
                      submission=SUBMISSIONS[incident], prepared=datetime(2026, 10, 20, 17, 13, 26, tzinfo=UTC),
                      deadline=datetime(2026, 10, 20, 17, 23, 26, tzinfo=UTC),
                      predecessor="0bcbcc89-7540-486a-b55e-a4005fde4fd2", predecessor_created="2026-10-20T16:56:38.609Z")
    retired_raw = m["canonical"](retired)
    archive = state / "candidates" / f"{hashlib.sha256(retired_raw).hexdigest()}.json"
    archive.write_bytes(retired_raw)
    archive.chmod(0o600)
    pending = journal(m, release="0123456789ab" + "c" * 28, input_sha="fedcba987654" + "1" * 52,
                      submission="0f" * 16, prepared=PREPARED, deadline=DEADLINE,
                      predecessor=LIVE, predecessor_created="2026-11-01T02:07:20.000Z")
    live = deployment(LIVE, "palimpsest-hetzner-" + "a" * 12 + "-" + "b" * 12 + "-" + "c" * 32,
                      created="2026-11-01T02:07:20.000Z")
    in_flight = deployment(IN_FLIGHT, pending["message"], created="2026-11-01T02:24:52.000Z")
    older = deployment(OLDER, "palimpsest-hetzner-" + "d" * 12 + "-" + "e" * 12 + "-" + "f" * 32,
                       created="2026-11-01T01:50:56.000Z", status="REMOVED", stopped=True)
    s = SimpleNamespace(m=m, g=g, state=state, retired=retired, retired_raw=retired_raw, pending=pending,
                        pending_path=state / "pending-candidate.json", live=live, in_flight=in_flight,
                        inventory=[in_flight, live, older], status=topology(m, [live, in_flight], in_flight),
                        unit=unit_show(), reads=[], writes={}, retained={}, holds=[], incident=incident)

    def write_pending(document=None, *, mode=0o600, raw=None):
        body = raw if raw is not None else m["canonical"](document if document is not None else s.pending)
        s.pending_path.write_bytes(body)
        s.pending_path.chmod(mode)
    s.write_pending = write_pending
    write_pending()

    def read(path, **kw):
        s.reads.append(path)
        return reconciler["_read"](path, **kw)

    n = {"_directory": lambda *a, **kw: None, "_read": read, "_object": reconciler["_object"],
         "_validate_candidate": reconciler["_validate_candidate"], "_status": lambda *a: s.status,
         "_topology": reconciler["_topology"], "_edges": reconciler["_edges"], "_one": reconciler["_one"],
         "_uuid": reconciler["_uuid"], "ReconciliationError": reconciler["ReconciliationError"],
         "_ensure_directory": lambda *a, **kw: None,
         "_atomic": lambda path, data, **kw: s.writes.update({path.name: json.loads(data)})}

    def page(*_args):
        return m["canonical"]({"data": {"deployments": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                        "edges": [{"node": row | {"projectId": m["PROJECT"],
                                                                                  "environmentId": m["ENVIRONMENT"],
                                                                                  "serviceId": m["SERVICE"]}}
                                                                  for row in s.inventory]}}})

    def systemctl(args, **kw):
        assert args[:3] == ["/usr/bin/systemctl", "show", "palimpsest-railway-publish.service"]
        assert "--timestamp=unix" in args and kw.get("check") is True
        if isinstance(s.unit, BaseException):
            raise s.unit
        return SimpleNamespace(stdout=s.unit, returncode=0)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return s.now if tz is None else s.now.astimezone(tz)

    s.now = NOW
    monkeypatch.setitem(g, "datetime", Frozen)
    monkeypatch.setitem(g, "JOURNAL", hashlib.sha256(retired_raw).hexdigest())
    monkeypatch.setitem(g, "STATE", state)
    monkeypatch.setitem(g, "ROOT", tmp_path / "incident")
    monkeypatch.setitem(g, "CONTROL", tmp_path)
    monkeypatch.setitem(g, "load_helper", lambda **kw: n)
    monkeypatch.setitem(g, "api", page)
    monkeypatch.setitem(g, "immutable", lambda n_, path, data, gid: s.retained.update({path.name: data}))
    monkeypatch.setitem(g, "hold_if_idle", lambda *a: s.holds.append(a) or True)
    monkeypatch.setattr(g["fcntl"], "flock", lambda *a: pytest.fail("an observation took the publication lock"))
    monkeypatch.setattr(g["subprocess"], "run", systemctl)
    monkeypatch.setattr(g["os"], "geteuid", lambda: 0)
    monkeypatch.setattr(g["pwd"], "getpwnam", lambda _: SimpleNamespace(pw_uid=os.getuid()))
    monkeypatch.setattr(g["grp"], "getgrnam", lambda _: SimpleNamespace(gr_gid=os.getgid()))
    monkeypatch.setenv("RAILWAY_TOKEN", "test-token")
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
    monkeypatch.setattr(g["sys"], "argv", [str(HELPERS[incident]), "--observe"])
    return s


def observe(s, capsys):
    code = s.m["main"]()
    return code, capsys.readouterr().err


def test_proven_rollout_defers_with_exit_75_and_writes_nothing(rollout, capsys):
    code, err = observe(rollout, capsys)
    assert code == 75
    assert rollout.m["IN_FLIGHT_EXIT"] == 75
    assert "incident observation deferred: no deployment carries the retired message" in err
    proof = json.loads(err[err.index("{"):])
    assert proof == {"submission_id": rollout.pending["submission_id"], "prepared_at": stamp(PREPARED),
                     "receipt_deadline_at": stamp(DEADLINE), "publisher_pid": 4242,
                     "predecessor_deployment_id": LIVE, "in_flight_deployment_id": IN_FLIGHT}
    # No observation, evidence or hold is written; the last clean one stands.
    assert rollout.writes == {} and rollout.retained == {} and rollout.holds == []
    assert rollout.reads == [rollout.state / "candidates" / f"{rollout.g['JOURNAL']}.json", rollout.pending_path]


def test_settled_topology_is_still_an_ordinary_clean_observation(rollout, capsys):
    rollout.status = topology(rollout.m, [rollout.in_flight], rollout.in_flight)
    code, err = observe(rollout, capsys)
    assert code == 0 and err == ""
    assert rollout.writes["observer-latest.json"]["status"] == "no_matching_candidate_observed"
    assert rollout.pending_path not in rollout.reads  # Only an ambiguity consults the journal.


@pytest.mark.parametrize("boundary", ["deadline_plus_reserve", "full_publisher_window"])
def test_publisher_window_bounds_are_inclusive(rollout, capsys, boundary):
    if boundary == "deadline_plus_reserve":
        rollout.now = DEADLINE + timedelta(seconds=10)
    else:
        rollout.now = PREPARED
    assert observe(rollout, capsys)[0] == 75


def _unproven(rollout, case):
    m, pending = rollout.m, dict(rollout.pending)
    if case == "no_pending_journal":
        rollout.pending_path.unlink()
    elif case == "stale_journal":
        rollout.now = DEADLINE + timedelta(seconds=11)
    elif case == "prepared_after_observation":
        rollout.now = PREPARED - timedelta(seconds=1)
    elif case == "deadline_beyond_publisher_bound":
        rollout.write_pending(pending | {"receipt_deadline_at": stamp(PREPARED + timedelta(seconds=601))})
    elif case == "unexpected_journal_state":
        rollout.write_pending(pending | {"status": "rollback_attempted"})
    elif case == "loose_journal_mode":
        rollout.write_pending(mode=0o644)
    elif case == "journal_not_json":
        rollout.write_pending(raw=b"{not json")
    elif case == "journal_is_the_retired_submission":
        rollout.write_pending(raw=rollout.retired_raw)
    elif case == "publisher_inactive":
        rollout.unit = unit_show(active="inactive", sub="dead", pid="0")
    elif case == "publisher_failed":
        rollout.unit = unit_show(active="failed", sub="failed", pid="0")
    elif case == "publisher_stopping":
        rollout.unit = unit_show(active="deactivating", sub="stop-sigterm")
    elif case == "later_invocation_same_second":
        rollout.unit = unit_show(started=PREPARED)
    elif case == "later_invocation":
        rollout.unit = unit_show(started=PREPARED + timedelta(seconds=40))
    elif case == "no_main_pid":
        rollout.unit = unit_show(pid="0")
    elif case == "no_start_clock":
        rollout.unit = unit_show(started="")
    elif case == "inexact_unit_state":
        rollout.unit = unit_show() + "SubState=start\n"
    elif case == "systemctl_unavailable":
        rollout.unit = subprocess.CalledProcessError(1, ["/usr/bin/systemctl"])
    elif case == "three_active":
        extra = deployment(OLDER, "palimpsest-hetzner-unknown", created="2026-11-01T01:50:56.000Z")
        rollout.status = topology(m, [rollout.live, rollout.in_flight, extra], rollout.in_flight)
    elif case == "unknown_second_deployment":
        other = deployment(IN_FLIGHT, "palimpsest-hetzner-not-this-run", created="2026-11-01T02:24:52.000Z")
        rollout.status = topology(m, [rollout.live, other], other)
    elif case == "predecessor_not_captured":
        moved = deployment(OLDER, rollout.live["meta"]["cliMessage"], created="2026-11-01T02:07:20.000Z")
        rollout.status = topology(m, [moved, rollout.in_flight], rollout.in_flight)
    elif case == "predecessor_not_serving":
        stopped = rollout.live | {"deploymentStopped": True}
        rollout.status = topology(m, [stopped, rollout.in_flight], rollout.in_flight)
    elif case == "latest_is_not_the_publication":
        newer = deployment(LATE, "palimpsest-hetzner-someone-else", created="2026-11-01T02:25:30.000Z")
        rollout.status = topology(m, [rollout.live, rollout.in_flight], newer)
    elif case == "same_deployment_twice":
        rollout.status = topology(m, [rollout.in_flight, rollout.in_flight], rollout.in_flight)
    else:
        raise AssertionError(case)


@pytest.mark.parametrize("case", [
    "no_pending_journal", "stale_journal", "prepared_after_observation", "deadline_beyond_publisher_bound",
    "unexpected_journal_state", "loose_journal_mode", "journal_not_json", "journal_is_the_retired_submission",
    "publisher_inactive", "publisher_failed", "publisher_stopping", "later_invocation_same_second",
    "later_invocation", "no_main_pid", "no_start_clock", "inexact_unit_state", "systemctl_unavailable",
    "three_active", "unknown_second_deployment", "predecessor_not_captured", "predecessor_not_serving",
    "latest_is_not_the_publication", "same_deployment_twice",
])
def test_unexplained_non_singular_topology_still_fails_with_exit_1(rollout, capsys, case):
    _unproven(rollout, case)
    code, err = observe(rollout, capsys)
    assert code == 1
    assert err.startswith("incident disposition refused: Railway active deployment inventory is not singular; "
                          "no in-flight publication explains it: ")
    assert rollout.writes == {} and rollout.holds == []


def test_any_other_topology_refusal_is_unchanged_and_never_reads_the_journal(rollout, capsys):
    building = rollout.in_flight | {"status": "BUILDING", "instances": []}
    rollout.status = topology(rollout.m, [rollout.live], building)
    code, err = observe(rollout, capsys)
    assert code == 1
    assert err == "incident disposition refused: Railway newer deployment is not terminal and stopped\n"
    assert rollout.pending_path not in rollout.reads


@pytest.mark.parametrize("where", ["inventory", "active_topology"])
def test_retired_message_during_a_rollout_is_still_a_late_sighting(rollout, capsys, where):
    late = deployment(LATE, rollout.retired["message"], created="2026-11-01T02:25:30.000Z", status="QUEUED")
    if where == "inventory":
        rollout.inventory = [late, *rollout.inventory]
    else:
        rollout.status = topology(rollout.m, [rollout.live, rollout.in_flight, late], rollout.in_flight)
    code, _err = observe(rollout, capsys)
    assert code == 1
    assert len(rollout.holds) == 1
    assert rollout.writes["observer-latest.json"]["status"] == "late_candidate_observed"
    assert rollout.writes["observer-latest.json"]["matching_deployment_ids"] == [LATE]


def test_disposition_mode_never_maps_an_in_flight_deferral_to_success(rollout, capsys, monkeypatch):
    def deferred(retire, acknowledgement):
        raise rollout.m["PublicationInFlight"]("{}")
    monkeypatch.setitem(rollout.g, "execute", deferred)
    monkeypatch.setattr(rollout.g["sys"], "argv", [str(HELPERS[rollout.incident])])
    code, err = observe(rollout, capsys)
    assert code == 1 and err.startswith("incident disposition refused")


@pytest.mark.parametrize("incident", sorted(HELPERS))
def test_units_already_map_only_75_and_windows_mirror_the_publisher(incident):
    unit = UNITS[incident].read_text()
    assert [line for line in unit.splitlines() if line.startswith("SuccessExitStatus")] == ["SuccessExitStatus=75"]
    assert f"ExecStart=/usr/local/sbin/palimpsest-retire-upload-502-{incident} --observe\n" in unit
    publisher = PUBLISHER.read_text()
    m = runpy.run_path(str(HELPERS[incident]))
    assert f"readonly MAX_MUTATION_TO_RECEIPT_SECONDS={m['PUBLISHER_MUTATION_SECONDS']}\n" in publisher
    assert f"readonly RECEIPT_COMMIT_RESERVE_SECONDS={m['PUBLISHER_COMMIT_RESERVE_SECONDS']}\n" in publisher
    assert m["IN_FLIGHT_EXIT"] == 75


def test_both_observers_carry_the_same_in_flight_code():
    def added(path):
        source = path.read_text()
        return source[source.index("def publisher_unit()"):source.index("def observe_retired()")]
    assert added(HELPERS["20260928"]) == added(HELPERS["20261020"])
