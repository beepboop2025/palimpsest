from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import runpy
import signal
import stat
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
RESTORE_HELPER = ROOT / "ops" / "railway" / "run-producer-restore"
ACTIVATE_HELPER = ROOT / "ops" / "railway" / "enable-hourly-publication"
CANARY_HELPER = ROOT / "ops" / "railway" / "run-activation-canary"
REPOSITORY = "beepboop2025/palimpsest"
ENVIRONMENT = "palimpsest-railway-production"
ACK = "palimpsest-github-environment-v1"
HOST_SHA = "1" * 40
PUBLICATION_SHA = "2" * 40
PUBLIC_RELEASE_SHA = "3" * 40
NEWSWIRE_SHA = "4" * 40
OSINT_SHA = "5" * 40
WORKFLOWS = (
    (1, 332082300, "newswire-refresh.yml", "Refresh evidence wire", "publish"),
    (
        2,
        341368020,
        "osint-china-v2-refresh.yml",
        "Refresh OSINT China roll-up v2",
        "publish",
    ),
    (
        3,
        333753226,
        "collector-health-watchdog.yml",
        "Recover stale collector publications",
        "recover",
    ),
)
ACTIVATORS = {
    "palimpsest-backup.timer",
    "palimpsest-bleedthrough.timer",
    "palimpsest-common-crawl-backup.timer",
    "palimpsest-common-crawl-context.timer",
    "palimpsest-common-crawl-import.path",
    "palimpsest-evidence-wire.timer",
    "palimpsest-measurement-refresh.timer",
    "palimpsest-railway-publish.timer",
    "palimpsest-direct-watchdog.timer",
    "palimpsest-freshness-watchdog.timer",
    "palimpsest-investigative-analysis.timer",
    "palimpsest-investigative-broker.socket",
    "palimpsest-node-offsite-backup.timer",
    "palimpsest-public-osint-sync.timer",
    "palimpsest-witness.timer",
}


def _bind_test_interpreter(
    namespace: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> str:
    expected = str(Path(sys.executable).resolve(strict=True))

    def resolve_exact(name: str) -> str:
        actual = str(Path(name).resolve(strict=True))
        assert actual == expected
        return expected

    trusted = namespace["_trusted_executable"]
    monkeypatch.setitem(trusted.__globals__, "_trusted_executable", resolve_exact)
    return expected


def _canonical(document: object) -> bytes:
    return (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        + b"\n"
    )


def _clock(delta: timedelta = timedelta()) -> str:
    return (
        (datetime.now(UTC) + delta)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _celery_receipt() -> dict[str, object]:
    topology = [
        {"node": "collectors@collector", "queue": "collectors"},
        {"node": "default@worker", "queue": "celery"},
        {"node": "warehouse@warehouse", "queue": "warehouse"},
    ]
    nodes = {item["node"]: item["queue"] for item in topology}
    return {
        "cancellations": [],
        "consumer_state": "consuming",
        "drain_samples": 0,
        "final": {
            "active_queues": {node: [queue] for node, queue in nodes.items()},
            "broker_depth": {
                "celery": 0,
                "censorwatch": 0,
                "collectors": 0,
                "warehouse": 0,
            },
            "task_counts": {
                node: {"active": 0, "reserved": 0, "scheduled": 0} for node in nodes
            },
            "unacknowledged": {"hash": 0, "index": 0},
        },
        "generated_at": _clock(timedelta(minutes=-3)),
        "required_zero_samples": 2,
        "samples_observed": 2,
        "schema_version": "palimpsest-celery-release-gate.v1",
        "status": "quiet",
        "topology": topology,
        "topology_sha256": "a" * 64,
    }


def _handoff() -> dict[str, object]:
    return {
        "artifact_sha256": "1" * 64,
        "expected_deploy_sha": HOST_SHA,
        "fetched_main": PUBLIC_RELEASE_SHA,
        "ledger_sha256": "2" * 64,
        "public_ledger_sha256": "3" * 64,
        "public_manifest_sha256": "4" * 64,
        "public_osint_stub_sha256": "5" * 64,
        "public_release_commit": PUBLIC_RELEASE_SHA,
        "public_rights_status_sha256": "6" * 64,
        "publication_commit": PUBLICATION_SHA,
        "railway_canary_run_id": 81,
        "resume_token": "7" * 32,
        "schema": "palimpsest-public-osint-release-proof.v2",
        "workflow_head_sha": HOST_SHA,
        "workflow_receipt_sha256": "8" * 64,
        "workflow_run_attempt": 1,
        "workflow_run_id": 80,
    }


def _proof_complete_receipt() -> bytes:
    handoff = _handoff()
    sync = {
        "artifact_canonical_sha256": "9" * 64,
        "artifact_sha256": handoff["artifact_sha256"],
        "deployed_commit": HOST_SHA,
        "fetched_main": PUBLIC_RELEASE_SHA,
        "generated_at": _clock(timedelta(minutes=-8)),
        "input_commit": HOST_SHA,
        "installed_at": _clock(timedelta(minutes=-3)),
        "ledger_entries": 4,
        "ledger_head": "a" * 64,
        "ledger_sha256": handoff["ledger_sha256"],
        "public_ledger_sha256": handoff["public_ledger_sha256"],
        "public_manifest_sha256": handoff["public_manifest_sha256"],
        "public_osint_stub_sha256": handoff["public_osint_stub_sha256"],
        "public_release_commit": PUBLIC_RELEASE_SHA,
        "public_rights_status_sha256": handoff["public_rights_status_sha256"],
        "publication_commit": PUBLICATION_SHA,
        "release_proof_sha256": hashlib.sha256(_canonical(handoff)).hexdigest(),
        "schema": "palimpsest-public-osint-sync.v3",
        "status": "installed",
        "sync_mode": "release-pinned",
    }
    units = {
        path: "b" * 64
        for path in {
            "/etc/systemd/system/palimpsest-backup.service",
            "/etc/systemd/system/palimpsest-backup.timer",
            "/etc/systemd/system/palimpsest-backup.service.d/override.conf",
            "/etc/systemd/system/palimpsest-evidence-wire.service",
            "/etc/systemd/system/palimpsest-evidence-wire.timer",
            "/etc/systemd/system/palimpsest-event-analysis-live.service",
            "/etc/systemd/system/palimpsest-freshness-watchdog.service",
            "/etc/systemd/system/palimpsest-freshness-watchdog.timer",
            "/etc/systemd/system/palimpsest-witness.service",
            "/etc/systemd/system/palimpsest-witness.timer",
        }
    }
    compose = {
        name: {
            "container_id": "c" * 64,
            "image_id": "sha256:" + "d" * 64,
            "was_running": True,
        }
        for name in {
            "beat",
            "worker",
            "worker-collectors",
            "worker-warehouse",
            "worker-velocity",
        }
    }
    document = {
        "backup": {
            "core_snapshot": "/private/core",
            "legacy_witness_status": {"path": None, "preserved": False, "sha256": None},
            "pre_change_v4_snapshot": "/private/v4",
            "verification": {"schema_version": "fixture.backup.v1", "status": "ok"},
        },
        "celery": {
            "candidate_consuming": {"status": "quiet"},
            "candidate_fenced": {"status": "quiet"},
            "pre_change": {"status": "quiet"},
            "v4_backup_fenced": {"status": "quiet"},
        },
        "compose_before": compose,
        "controller_manifest_sha256": "e" * 64,
        "deployment": {
            "candidate_image_id": "sha256:" + "f" * 64,
            "candidate_render_gateway_image_id": None,
            "controller_sha": HOST_SHA,
            "controller_tree_sha256": "0" * 64,
            "deployed_sha": HOST_SHA,
            "direction": "forward",
            "previous_checkout_sha": "0" * 40,
            "previous_deployment_receipt_sha": "0" * 40,
        },
        "generated_at": _clock(timedelta(minutes=-4)),
        "installed_unit_sha256": units,
        "observers": {
            "policy_sha256": "1" * 64,
            "watchdog": {
                "baseline_token_sha256": "2" * 64,
                "exit_pair": "0:0",
                "proof": {"status": "ok"},
            },
            "witness": {
                "baseline_token_sha256": "3" * 64,
                "exit_pair": "0:0",
                "proof": {"status": "ok"},
            },
        },
        "publication": {
            "bleedthrough_normalized_sha256": "4" * 64,
            "handoff": handoff,
            "ledger_sha256": handoff["ledger_sha256"],
            "osint_sha256": handoff["artifact_sha256"],
            "sync_receipt": sync,
        },
        "recovery": {
            "schema_version": "palimpsest-deployment-snapshot-recovery.v1",
            "status": "ok",
        },
        "release_proof_present": True,
        "schema_version": "palimpsest-host-release.v1",
        "status": "proof-complete",
        "transaction_id": "7" * 32,
        "writers_restored": False,
    }
    return _canonical(document)


def _phase3_receipt(proof_sha256: str) -> bytes:
    activators = {
        name: {
            "active_state": "active",
            "before_enablement": "enabled",
            "enablement": "enabled",
            "was_active": True,
        }
        for name in ACTIVATORS
    }
    compose = {
        name: {
            "container_id": "b" * 64,
            "hostname": f"{name}-host",
            "image_id": f"sha256:{'c' * 64}",
            "state": "running",
            "was_running": True,
        }
        for name in {
            "beat",
            "worker",
            "worker-collectors",
            "worker-warehouse",
            "worker-velocity",
        }
    }
    document = {
        "backup_on_success": "palimpsest-event-analysis-live.service",
        "backup_release_quiesce_present": False,
        "deployed_sha": HOST_SHA,
        "finalized_at": _clock(timedelta(minutes=-2)),
        "previous_checkout_sha": "0" * 40,
        "previous_deployment_receipt_sha": "0" * 40,
        "proof_complete_receipt": "/var/lib/palimpsest-release/proof.json",
        "proof_complete_receipt_sha256": proof_sha256,
        "release_proof_present": False,
        "restored_activators": activators,
        "restored_beat": compose["beat"],
        "restored_celery": _celery_receipt(),
        "restored_compose_writers": compose,
        "schema_version": "palimpsest-host-release-finalization.v1",
        "status": "finalized",
        "transaction_id": "7" * 32,
        "writers_restored": True,
    }
    return _canonical(document)


FAKE_GH = r"""#!/usr/bin/env python3
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import zipfile
from datetime import datetime, timezone

args = sys.argv[1:]
fixture_root = Path(__file__).resolve().parent.parent
state_path = fixture_root / "state.json"
log_path = fixture_root / "gh.log"
state = json.loads(state_path.read_text())
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args, separators=(",", ":")) + "\n")

def save():
    state_path.write_text(json.dumps(state, sort_keys=True))

def emit(value):
    if isinstance(value, (dict, list)):
        print(json.dumps(value, separators=(",", ":")))
    else:
        print(value)

def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

specs = {
    "332082300": (1, "newswire-refresh.yml", "Refresh evidence wire", "publish"),
    "341368020": (2, "osint-china-v2-refresh.yml", "Refresh OSINT China roll-up v2", "publish"),
    "333753226": (3, "collector-health-watchdog.yml", "Recover stale collector publications", "recover"),
}

failure_match = state.get("fail_match", "")
if failure_match and failure_match in " ".join(args):
    raise SystemExit(int(state.get("fail_status", 42)))

if args[:2] == ["auth", "status"]:
    raise SystemExit(0)

if args[:3] == ["config", "get", "http_unix_socket"]:
    if state.get("http_unix_socket"):
        emit(state["http_unix_socket"])
    raise SystemExit(0)

if args[:2] == ["variable", "get"]:
    if args[2] == "RAILWAY_PUBLICATION_ENABLED":
        emit(state["gate"])
        raise SystemExit(0)
    if args[2] == "RAILWAY_EXCLUSIVE_WRITER_ACK":
        if not state["ack_present"]:
            raise SystemExit(1)
        emit(state["ack_value"])
        raise SystemExit(0)

if args[:2] == ["variable", "set"]:
    value = args[args.index("--body") + 1]
    state["gate"] = value
    if value == "true":
        state["true_writes"] += 1
        created = (fixture_root / "fake-now.txt").read_text()
        controller = {
            "conclusion": "success",
            "created_at": created,
            "event": "schedule",
            "head_branch": "main",
            "head_sha": state["main"],
            "id": 201,
            "name": "Queue exact Railway publication",
            "path": ".github/workflows/railway-publication-controller.yml",
            "run_attempt": 1,
            "status": "completed",
            "updated_at": created,
        }
        release = {
            "conclusion": None,
            "created_at": created,
            "event": "repository_dispatch",
            "head_branch": "main",
            "head_sha": state["main"],
            "id": 202,
            "name": "Tests",
            "path": ".github/workflows/tests.yml",
            "run_attempt": 1,
            "status": "in_progress",
            "updated_at": created,
        }
        if state.get("scheduled_wrong_sha"):
            controller["head_sha"] = "f" * 40
        if state.get("scheduled_race"):
            duplicate = dict(controller)
            duplicate["id"] = 1201
            state["runs"]["343876046"] = [duplicate, controller]
        else:
            state["runs"]["343876046"] = [controller]
        state["runs"]["323903518"] = (
            [] if state.get("controller_no_change") else [release]
        )
    save()
    raise SystemExit(0)

if args[:2] == ["variable", "delete"]:
    state["ack_present"] = False
    save()
    raise SystemExit(0)

if args[:2] == ["variable", "list"]:
    fields = args[args.index("--json") + 1]
    if not state["ack_present"]:
        emit([])
    elif fields == "name,value":
        emit([{"name": "RAILWAY_EXCLUSIVE_WRITER_ACK", "value": state["ack_value"]}])
    else:
        emit([{"name": "RAILWAY_EXCLUSIVE_WRITER_ACK"}])
    raise SystemExit(0)

if args[:2] == ["workflow", "enable"]:
    workflow_id = args[2]
    if workflow_id == "343876046":
        state["controller_state"] = "active"
    else:
        state["workflow_states"][workflow_id] = "active"
    state.setdefault("enable_counts", {})[workflow_id] = (
        state.setdefault("enable_counts", {}).get(workflow_id, 0) + 1
    )
    if (
        state.get("final_activation_schedule_workflow") == workflow_id
        and state["enable_counts"][workflow_id] == 2
    ):
        _order, filename, name, _job = specs[workflow_id]
        state["runs"][workflow_id].insert(0, {
            "conclusion": None,
            "created_at": now(),
            "event": "schedule",
            "head_branch": "main",
            "head_sha": state["main"],
            "id": 990,
            "main_after": state["main"],
            "main_before": state["main"],
            "name": name,
            "path": f".github/workflows/{filename}",
            "run_attempt": 1,
            "status": "queued",
            "updated_at": now(),
        })
    if (
        state.get("historical_rerun_final_workflow") == workflow_id
        and state["enable_counts"][workflow_id] == 2
    ):
        historical = next(item for item in state["runs"][workflow_id] if item["id"] == 77)
        historical["run_attempt"] = 2
        historical["status"] = "queued"
        historical["conclusion"] = None
    save()
    raise SystemExit(0)

if args[:2] == ["workflow", "disable"]:
    if args[2] == "343876046":
        state["controller_state"] = "disabled_manually"
    else:
        state["workflow_states"][args[2]] = "disabled_manually"
    save()
    raise SystemExit(0)

if args[:2] == ["workflow", "run"]:
    workflow_id = args[2]
    order, filename, name, _job = specs[workflow_id]
    run_id = 100 + order
    head_sha = state["main"]
    before_sha = head_sha
    if state.get("wrong_sha_stage") == order:
        head_sha = "f" * 40
    inputs = {}
    for index, item in enumerate(args):
        if item == "-f":
            key, value = args[index + 1].split("=", 1)
            inputs[key] = value
    if order == 1 and not state.get("newswire_no_change"):
        state["main"] = "4" * 40
    elif order == 2 and not state.get("osint_no_change"):
        state["main"] = "5" * 40
    record = {
        "conclusion": "success",
        "created_at": now(),
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": head_sha,
        "id": run_id,
        "main_after": state["main"],
        "main_before": before_sha,
        "name": name,
        "path": f".github/workflows/{filename}",
        "run_attempt": 1,
        "status": "completed",
        "updated_at": now(),
    }
    state["runs"][workflow_id].insert(0, record)
    if state.get("historical_rerun_stage") == order:
        historical = next(item for item in state["runs"][workflow_id] if item["id"] == 77)
        historical["run_attempt"] = 2
        historical["status"] = "queued"
        historical["conclusion"] = None
    state["dispatch_inputs"][workflow_id] = inputs
    if state.get("race_stage") == order:
        race = dict(record)
        race.update({"id": run_id + 1000, "event": "schedule"})
        state["runs"][workflow_id].insert(0, race)
    if order == 3 and state.get("watchdog_dispatch_race"):
        race = dict(record)
        race.update({
            "id": 909,
            "name": specs["332082300"][2],
            "path": ".github/workflows/newswire-refresh.yml",
        })
        state["runs"]["332082300"].insert(0, race)
    save()
    if state.get("sleep_after_dispatch_stage") == order:
        (fixture_root / "sleeping-gh").write_text(str(os.getpid()))
        time.sleep(60)
    if state.get("dispatch_cli_failure_stage") == order:
        raise SystemExit(42)
    raise SystemExit(0)

if args[:2] == ["run", "download"]:
    run_id = int(args[2])
    destination = Path(args[args.index("--dir") + 1])
    if run_id == 201:
        document = {
            "activation_canary": False,
            "controller_repository": "beepboop2025/palimpsest",
            "controller_run_attempt": 1,
            "controller_run_id": 201,
            "controller_workflow_path": ".github/workflows/railway-publication-controller.yml",
            "deploy_railway": True,
            "requested_at": (fixture_root / "fake-now.txt").read_text(),
            "schema_version": "palimpsest.railway-publication-request.v2",
            "scope": "complete",
            "sha": state["main"],
        }
        filename = "railway-publication-request.json"
    elif run_id == 202:
        source = fixture_root / "release-artifact-fixture"
        for item in source.iterdir():
            shutil.copyfile(item, destination / item.name)
        raise SystemExit(0)
    elif run_id == 101:
        run = state["runs"]["332082300"][0]
        no_change = run["main_before"] == run["main_after"]
        document = {
            "acquisition_base_sha": run["main_before"],
            "base_sha": run["main_before"],
            "candidate_changed": not no_change,
            "candidate_sha": run["main_after"],
            "current_main_sha": run["main_after"],
            "event": "workflow_dispatch",
            "head_sha": run["main_before"],
            "output_parents": [] if no_change else [run["main_before"]],
            "output_sha": run["main_after"],
            "push_outcome": "skipped" if no_change else "success",
            "recorded_at": run["updated_at"],
            "repository": "beepboop2025/palimpsest",
            "result": "no_change" if no_change else "committed",
            "retry_candidate_changed": None,
            "retry_candidate_sha": None,
            "retry_outcome": "skipped",
            "run_attempt": 1,
            "run_id": run_id,
            "schema_version": "palimpsest.newswire-manual-outcome.v1",
            "synchronized_candidate_changed": None,
            "workflow": ".github/workflows/newswire-refresh.yml",
            "workflow_name": "Refresh evidence wire",
        }
        filename = "newswire-manual-outcome.json"
    elif run_id == 102:
        run = state["runs"]["341368020"][0]
        inputs = state["dispatch_inputs"]["341368020"]
        no_change = run["main_before"] == run["main_after"]
        document = {
            "acquisition_base_sha": run["main_before"],
            "base_sha": run["main_before"],
            "candidate_changed": not no_change,
            "candidate_sha": run["main_after"],
            "current_main_sha": run["main_after"],
            "event": "workflow_dispatch",
            "expected_deploy_sha": run["main_before"],
            "head_sha": run["main_before"],
            "output_parents": [] if no_change else [run["main_before"]],
            "output_sha": run["main_after"],
            "publication_commit": run["main_after"],
            "push_exit_code": None if no_change else 0,
            "push_outcome": "skipped" if no_change else "success",
            "recorded_at": run["updated_at"],
            "release_nonce": inputs["release_nonce"],
            "repository": "beepboop2025/palimpsest",
            "result": "no_change" if no_change else "committed",
            "retry_candidate_changed": None,
            "retry_candidate_sha": None,
            "retry_exit_code": None,
            "retry_outcome": "skipped",
            "run_attempt": 1,
            "run_id": run_id,
            "schema_version": "palimpsest.osint-manual-outcome.v1",
            "synchronized_candidate_changed": None,
            "workflow": ".github/workflows/osint-china-v2-refresh.yml",
            "workflow_name": "Refresh OSINT China roll-up v2",
        }
        filename = "osint-manual-outcome.json"
    elif run_id == 103:
        run = state["runs"]["333753226"][0]
        plan = {
            "bundle_generated_at": None,
            "bundle_stale": False,
            "dispatch": (["newswire-refresh.yml"] if state.get("watchdog_nonempty") else []),
            "escalations": [],
            "generated_at": run["created_at"],
            "problems": [],
            "schema_version": "collector-watchdog-plan.v2",
        }
        document = {
            "actor": "fake-operator",
            "checkout_sha": run["main_before"],
            "dispatch_step_outcome": "success",
            "dispatches": [],
            "event": "workflow_dispatch",
            "event_sha": run["main_before"],
            "final_main_sha": run["main_before"],
            "observed_at": run["created_at"],
            "observed_main_sha": run["main_before"],
            "plan": plan,
            "plan_sha256": hashlib.sha256((json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest(),
            "repository": "beepboop2025/palimpsest",
            "run_attempt": 1,
            "run_id": run_id,
            "schema_version": "palimpsest.collector-health-watchdog-receipt.v1",
            "status": "success",
            "workflow": ".github/workflows/collector-health-watchdog.yml",
            "workflow_name": "Recover stale collector publications",
        }
        filename = "collector-health-watchdog-receipt.json"
    else:
        raise SystemExit(98)
    if state.get("late_schedule_stage") == run_id - 100:
        workflow_id = next(
            key for key, value in specs.items() if value[0] == run_id - 100
        )
        scheduled = {
            "conclusion": None,
            "created_at": now(),
            "event": "schedule",
            "head_branch": "main",
            "head_sha": run["main_before"],
            "id": 800 + run_id,
            "main_after": state["main"],
            "main_before": state["main"],
            "name": specs[workflow_id][2],
            "path": f".github/workflows/{specs[workflow_id][1]}",
            "run_attempt": 1,
            "status": "queued",
            "updated_at": now(),
        }
        state["runs"][workflow_id].insert(0, scheduled)
        save()
    path = destination / filename
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    path.chmod(0o600)
    raise SystemExit(0)

if args[:2] == ["run", "view"]:
    if int(args[2]) != 202:
        raise SystemExit(96)
    jobs = []
    for name, conclusion in {
        "Deploy and prove exact Railway publication": "success",
        "Deploy exact complete Pages edition": "success",
        "Package exact complete Pages edition": "success",
        "Verify exact Pages and native MCP rights closure": "skipped",
        "contract": "success",
    }.items():
        jobs.append({"name": name, "conclusion": conclusion})
    if state.get("release_missing_job"):
        jobs.pop()
    emit({"jobs": jobs})
    raise SystemExit(0)

if args and args[0] == "api":
    endpoint = next((item for item in args if item.startswith("repos/")), "")
    if endpoint.endswith("/actions/runs/202/pending_deployments"):
        if "--method" in args:
            state["release_approved"] = True
            release = state["runs"]["323903518"][0]
            release["status"] = "completed"
            release["conclusion"] = (
                "failure" if state.get("release_failure") else "success"
            )
            release["updated_at"] = (fixture_root / "fake-now.txt").read_text()
            save()
            emit({})
        elif state["release_approved"]:
            emit([])
        else:
            if state.get("simulate_frozen_cron_ticks") and not state.get(
                "frozen_cron_ticks_observed"
            ):
                state["frozen_cron_ticks_observed"] = True
                state["newswire_17_suppressed"] = (
                    state["workflow_states"]["332082300"] == "disabled_manually"
                )
                state["next_controller_13_suppressed"] = (
                    state["controller_state"] == "disabled_manually"
                )
                if not state["newswire_17_suppressed"]:
                    state["main"] = "a" * 40
                if not state["next_controller_13_suppressed"]:
                    duplicate = dict(state["runs"]["343876046"][0])
                    duplicate["id"] = 1201
                    state["runs"]["343876046"].insert(0, duplicate)
            state["pending_reads"] = state.get("pending_reads", 0) + 1
            if state.get("approval_main_drift_read") == state["pending_reads"]:
                state["main"] = "f" * 40
            if state.get("approval_release_race_read") == state["pending_reads"]:
                race = dict(state["runs"]["323903518"][0])
                race["id"] = 1202
                race["event"] = "workflow_dispatch"
                state["runs"]["323903518"].insert(0, race)
            if state.get("approval_controller_rerun_read") == state["pending_reads"]:
                state["runs"]["343876046"][0]["run_attempt"] = 2
            if state.get("approval_release_rerun_read") == state["pending_reads"]:
                state["runs"]["323903518"][0]["run_attempt"] = 2
            if state.get("approval_pending_drift_read") == state["pending_reads"]:
                save()
                emit([])
                raise SystemExit(0)
            save()
            emit([{
                "current_user_can_approve": True,
                "environment": {"id": 20705508397, "name": "palimpsest-railway-production"},
            }])
        raise SystemExit(0)
    if endpoint.endswith("/environments/palimpsest-railway-production"):
        state["environment_reads"] = state.get("environment_reads", 0) + 1
        if state.get("approval_environment_drift_read") == state["environment_reads"]:
            state["can_admins_bypass"] = True
        if (
            state.get("approval_controller_rerun_after_env_read")
            == state["environment_reads"]
        ):
            state["runs"]["343876046"][0]["run_attempt"] = 2
        if (
            state.get("approval_release_status_after_env_read")
            == state["environment_reads"]
        ):
            state["runs"]["323903518"][0]["status"] = "waiting"
        save()
        emit({
            "can_admins_bypass": state.get("can_admins_bypass", False),
            "deployment_branch_policy": {
                "custom_branch_policies": True,
                "protected_branches": False,
            },
            "id": 20705508397,
            "name": "palimpsest-railway-production",
            "protection_rules": [{
                "id": 63841171,
                "prevent_self_review": False,
                "reviewers": [{
                    "reviewer": {"id": 215868371, "login": "beepboop2025"},
                    "type": "User",
                }],
                "type": "required_reviewers",
            }, {"id": 63841172, "type": "branch_policy"}],
        })
        raise SystemExit(0)
    if endpoint.endswith(
        "/environments/palimpsest-railway-production/deployment-branch-policies?per_page=100"
    ):
        emit({
            "branch_policies": [{"id": 58388360, "name": "main", "type": "branch"}],
            "total_count": 1,
        })
        raise SystemExit(0)
    if endpoint.endswith(
        "/environments/palimpsest-railway-production/secrets?per_page=100"
    ):
        emit({"secrets": [{"name": "PALIMPSEST_RAILWAY_TOKEN"}], "total_count": 1})
        raise SystemExit(0)
    if endpoint.endswith(
        "/environments/palimpsest-railway-production/variables?per_page=100"
    ):
        variables = [{
            "name": "RAILWAY_EXCLUSIVE_WRITER_ACK",
            "value": "palimpsest-github-environment-v1",
        }]
        if state.get("environment_variable_drift"):
            variables.append({"name": "EXTRA_AUTHORITY", "value": "unexpected"})
        emit({"total_count": len(variables), "variables": variables})
        raise SystemExit(0)
    if endpoint.endswith("/commits/main"):
        state["main_reads"] += 1
        if state.get("drift_after_main_reads") and state["main_reads"] >= state["drift_after_main_reads"]:
            state["main"] = "f" * 40
        save()
        emit(state["main"])
        raise SystemExit(0)
    contents = re.search(
        r"/contents/(\.github/workflows/[^?]+)\?ref=([0-9a-f]{40})", endpoint
    )
    if contents:
        path = Path.cwd() / contents.group(1)
        payload = path.read_text()
        if (
            state.get("main_advance_after_last_proved")
            and contents.group(2) == "4" * 40
            and contents.group(1).endswith("osint-china-v2-refresh.yml")
        ):
            state["main"] = "5" * 40
            save()
        if (
            state.get("workflow_drift_stage") == 2
            and contents.group(2) == "4" * 40
            and contents.group(1).endswith("osint-china-v2-refresh.yml")
        ):
            payload += "\n# injected drift\n"
        sys.stdout.write(payload)
        raise SystemExit(0)
    workflow = re.search(r"/actions/workflows/(\d+)$", endpoint)
    if workflow:
        workflow_id = workflow.group(1)
        if workflow_id == "343876046":
            filename = "railway-publication-controller.yml"
            name = "Queue exact Railway publication"
            workflow_state = state["controller_state"]
        else:
            _order, filename, name, _job = specs[workflow_id]
            workflow_state = state["workflow_states"][workflow_id]
        emit({
            "id": int(workflow_id),
            "name": name,
            "path": f".github/workflows/{filename}",
            "state": workflow_state,
        })
        raise SystemExit(0)
    active = re.search(r"/actions/workflows/(\d+)/runs\?status=([^&]+)&per_page=100", endpoint)
    if active:
        count = sum(
            item.get("status") == active.group(2)
            for item in state["runs"][active.group(1)]
        )
        if state.get("active_run_conflict") and active.group(2) == "in_progress":
            count = 1
        if state.get("fail_after_true") and state["gate"] == "true" and active.group(2) == "in_progress":
            count = 1
        emit(count)
        raise SystemExit(0)
    inventory = re.search(r"/actions/workflows/(\d+)/runs\?per_page=100", endpoint)
    if inventory:
        workflow_id = inventory.group(1)
        runs = state["runs"][workflow_id]
        unexpected = [item for item in runs if item["id"] >= 900]
        if unexpected and state.get("unexpected_visibility_lag_reads") and workflow_id == "332082300":
            state["unexpected_inventory_reads"] = state.get("unexpected_inventory_reads", 0) + 1
            if state["unexpected_inventory_reads"] <= state["unexpected_visibility_lag_reads"]:
                runs = [item for item in runs if item["id"] < 900]
            save()
        if (
            unexpected
            and state.get("cleanup_schedule_advances")
            and set(state["workflow_states"].values()) == {"disabled_manually"}
            and not state.get("cleanup_schedule_advanced")
        ):
            state["main"] = "a" * 40
            for item in unexpected:
                item["status"] = "completed"
                item["conclusion"] = "success"
                item["updated_at"] = now()
            state["cleanup_schedule_advanced"] = True
            save()
            runs = state["runs"][workflow_id]
        emit({"workflow_runs": runs})
        raise SystemExit(0)
    jobs = re.search(r"/actions/runs/(\d+)/jobs\?filter=latest&per_page=100", endpoint)
    if jobs:
        run_id = int(jobs.group(1))
        if run_id == 201:
            emit({
                "jobs": [{
                    "conclusion": "success",
                    "name": "dispatch",
                    "run_attempt": 1,
                    "status": "completed",
                    "steps": [{
                        "conclusion": "success",
                        "name": "Dispatch exact release",
                        "number": 1,
                        "status": "completed",
                    }],
                }],
                "total_count": 1,
            })
            raise SystemExit(0)
        stage = run_id - 100
        if state.get("missing_jobs_stage") == stage:
            emit({"jobs": [], "total_count": 0})
            raise SystemExit(0)
        workflow_id = ("332082300", "341368020", "333753226")[stage - 1]
        conclusion = "failure" if state.get("failed_job_stage") == stage else "success"
        emit({
            "jobs": [{
                "conclusion": "success",
                "name": specs[workflow_id][3],
                "run_attempt": 1,
                "status": "completed",
                "steps": [{
                    "conclusion": conclusion,
                    "name": "Meaningful producer work",
                    "number": 1,
                    "status": "completed",
                }],
            }],
            "total_count": 1,
        })
        raise SystemExit(0)
    run_endpoint = re.search(r"/actions/runs/(\d+)$", endpoint)
    if run_endpoint:
        run_id = int(run_endpoint.group(1))
        run = next(
            item
            for inventory in state["runs"].values()
            for item in inventory
            if item["id"] == run_id
        )
        emit(run)
        raise SystemExit(0)
    archive_endpoint = re.search(r"/actions/artifacts/(\d+)/zip$", endpoint)
    if archive_endpoint:
        artifact_id = int(archive_endpoint.group(1))
        paths = {
            701: (
                fixture_root / "controller-outcome-no-change.zip"
                if state.get("controller_no_change")
                else fixture_root / "controller-outcome-dispatched.zip"
            ),
            702: fixture_root / "controller-request.zip",
            703: fixture_root / "release-artifact.zip",
        }
        path = paths.get(artifact_id)
        if path is None:
            raise SystemExit(95)
        sys.stdout.buffer.write(path.read_bytes())
        raise SystemExit(0)
    artifacts = re.search(r"/actions/runs/(\d+)/artifacts\?per_page=100", endpoint)
    if artifacts:
        run_id = int(artifacts.group(1))
        if run_id == 201:
            paths = [(
                701,
                "railway-publication-controller-outcome-201-1",
                (
                    fixture_root / "controller-outcome-no-change.zip"
                    if state.get("controller_no_change")
                    else fixture_root / "controller-outcome-dispatched.zip"
                ),
            )]
            if not state.get("controller_no_change"):
                paths.append((702, "railway-publication-request-201-1", fixture_root / "controller-request.zip"))
            rows = []
            for artifact_id, name, path in paths:
                raw = path.read_bytes()
                rows.append({
                    "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                    "expired": False,
                    "id": artifact_id,
                    "name": name,
                    "size_in_bytes": len(raw),
                    "workflow_run": {"id": run_id},
                })
            emit({"artifacts": rows, "total_count": len(rows)})
            raise SystemExit(0)
        elif run_id == 202:
            path = fixture_root / "release-artifact.zip"
            raw = path.read_bytes()
            digest = (
                "f" * 64
                if state.get("release_archive_digest_mismatch")
                else hashlib.sha256(raw).hexdigest()
            )
            emit({"artifacts": [{
                "digest": "sha256:" + digest,
                "expired": False,
                "id": 703,
                "name": f"railway-continuous-release-{state['main']}-run-202-attempt-1",
                "size_in_bytes": len(raw),
                "workflow_run": {"id": run_id},
            }], "total_count": 1})
            raise SystemExit(0)
        elif run_id == 101:
            name = f"newswire-manual-outcome-{run_id}-1"
        elif run_id == 102:
            name = f"osint-manual-outcome-{run_id}-1"
        elif run_id == 103:
            name = f"collector-health-watchdog-receipt-{run_id}-1"
        else:
            emit({"artifacts": [], "total_count": 0})
            raise SystemExit(0)
        emit({"artifacts": [{
            "digest": "sha256:" + ("a" * 64),
            "expired": False,
            "id": run_id + 500,
            "name": name,
            "size_in_bytes": 1024,
            "workflow_run": {"id": run_id},
        }], "total_count": 1})
        raise SystemExit(0)
    compare = re.search(r"/compare/([0-9a-f]{40})\.\.\.([0-9a-f]{40})", endpoint)
    if compare:
        before, after = compare.groups()
        if before == after:
            emit({
                "ahead_by": 0,
                "behind_by": 0,
                "commits": [],
                "status": "identical",
                "total_commits": 0,
            })
            raise SystemExit(0)
        message = (
            "data: evidence wire refresh (2026-08-28T00:00:00Z) [skip pytest]"
            if after == "4" * 40
            else "data: OSINT China refresh (2026-08-28T00:01:00Z) [skip pytest]"
        )
        emit({
            "ahead_by": 1,
            "behind_by": 0,
            "commits": [{
                "commit": {"message": message},
                "parents": [{"sha": before}],
                "sha": after,
            }],
            "status": "ahead",
            "total_commits": 1,
        })
        raise SystemExit(0)

raise SystemExit(97)
"""


FAKE_CURL = r"""#!/usr/bin/env python3
import json
from pathlib import Path
import shutil
import sys

args = sys.argv[1:]
fixture_root = Path(__file__).resolve().parent.parent
destination = Path(args[args.index("--output") + 1])
url = args[-1]
if "publication-freshness-attestation-latest.json" in url:
    source = fixture_root / "live-freshness.json"
elif "railway-release.json" in url:
    source = fixture_root / "live-manifest.json"
else:
    raise SystemExit(93)
shutil.copyfile(source, destination)
state = json.loads((fixture_root / "state.json").read_text())
with (fixture_root / "gh.log").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(["curl", url], separators=(",", ":")) + "\n")
if state.get("live_mismatch") and "www.palimpsest.info" in url:
    with destination.open("ab") as handle:
        handle.write(b" ")
print("200", end="")
"""


def _release_fixture(directory: Path, fixed_now: datetime) -> tuple[Path, Path]:
    namespace = runpy.run_path(
        str(ROOT / "tests" / "test_railway_activation_canary_helper.py")
    )
    writer = namespace["_write_artifact"]
    shared = writer.__globals__
    shared["SHA"] = OSINT_SHA
    shared["CONTROLLER_RUN_ID"] = 201
    shared["CONTROLLER_REQUEST_ARTIFACT_ID"] = 702
    shared["RELEASE_RUN_ID"] = 202
    prerequisite = shared["_newswire_prerequisite"](publication_sha=OSINT_SHA)
    freshness_document = json.loads(shared["_freshness_attestation"](prerequisite))
    attested_at = fixed_now.replace(microsecond=0)
    newswire_at = attested_at - timedelta(minutes=5)
    situation_at = attested_at - timedelta(minutes=4)

    def stamp(value: datetime) -> str:
        return (
            value.astimezone(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    freshness_document["attested_at"] = stamp(attested_at)
    freshness_document["artifacts"]["newswire"]["generated_at"] = stamp(newswire_at)
    freshness_document["artifacts"]["china_situation"]["generated_at"] = stamp(
        situation_at
    )
    freshness_document["artifacts"]["china_situation"]["inputs"][
        "newswire_generated_at"
    ] = freshness_document["artifacts"]["newswire"]["generated_at"]
    freshness = _canonical(freshness_document)
    manifest = shared["_manifest"](freshness)
    requested_at = stamp(fixed_now)
    shared["REQUESTED_AT"] = requested_at
    request = {
        "activation_canary": False,
        "controller_repository": REPOSITORY,
        "controller_run_attempt": 1,
        "controller_run_id": 201,
        "controller_workflow_path": ".github/workflows/railway-publication-controller.yml",
        "deploy_railway": True,
        "requested_at": requested_at,
        "schema_version": "palimpsest.railway-publication-request.v2",
        "scope": "complete",
        "sha": OSINT_SHA,
    }
    request_raw = shared["_canonical"](request)
    request_archive = shared["_zip_artifact"](
        directory.parent / "controller-request.zip",
        {"railway-publication-request.json": request_raw},
    )
    writer(
        directory,
        manifest,
        controller_artifact_digest=str(request_archive["digest"]),
        controller_request_sha256=hashlib.sha256(request_raw).hexdigest(),
    )
    shared["_zip_artifact"](
        directory.parent / "release-artifact.zip",
        {item.name: item.read_bytes() for item in directory.iterdir()},
    )
    outcome_common = {
        "activation_canary": False,
        "controller_run_attempt": 1,
        "controller_run_id": 201,
        "event": "schedule",
        "force": False,
        "gate_enabled": True,
        "head_sha": OSINT_SHA,
        "main_sha": OSINT_SHA,
        "recorded_at": requested_at,
        "repository": REPOSITORY,
        "schema_version": "palimpsest.railway-publication-controller-outcome.v1",
        "workflow": ".github/workflows/railway-publication-controller.yml",
        "workflow_name": "Queue exact Railway publication",
    }
    dispatched = {
        **outcome_common,
        "live_sha": PUBLIC_RELEASE_SHA,
        "request_artifact_digest": request_archive["digest"],
        "request_artifact_id": 702,
        "request_artifact_name": "railway-publication-request-201-1",
        "request_artifact_size": request_archive["size"],
        "request_sha256": hashlib.sha256(request_raw).hexdigest(),
        "requested_at": requested_at,
        "result": "dispatched",
    }
    no_change = {
        **outcome_common,
        "live_sha": OSINT_SHA,
        "request_artifact_digest": None,
        "request_artifact_id": None,
        "request_artifact_name": None,
        "request_artifact_size": None,
        "request_sha256": None,
        "requested_at": None,
        "result": "no_change",
    }
    shared["_zip_artifact"](
        directory.parent / "controller-outcome-dispatched.zip",
        {
            "railway-publication-controller-outcome.json": shared["_canonical"](
                dispatched
            )
        },
    )
    shared["_zip_artifact"](
        directory.parent / "controller-outcome-no-change.zip",
        {
            "railway-publication-controller-outcome.json": shared["_canonical"](
                no_change
            )
        },
    )
    manifest_path = directory.parent / "live-manifest.json"
    freshness_path = directory.parent / "live-freshness.json"
    manifest_path.write_bytes(manifest)
    freshness_path.write_bytes(freshness)
    return manifest_path, freshness_path


def _state(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "ack_present": True,
        "ack_value": ACK,
        "controller_state": "active",
        "dispatch_inputs": {str(item[1]): {} for item in WORKFLOWS},
        "gate": "false",
        "main": PUBLIC_RELEASE_SHA,
        "main_reads": 0,
        "runs": {
            **{str(item[1]): [] for item in WORKFLOWS},
            "323903518": [],
            "343876046": [],
        },
        "release_approved": False,
        "true_writes": 0,
        "workflow_states": {str(item[1]): "disabled_manually" for item in WORKFLOWS},
    }
    value.update(overrides)
    return value


def _prepare(tmp_path: Path, **state_overrides: object) -> dict[str, Path]:
    tmp_path.chmod(0o700)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(mode=0o700)
    gh = fake_bin / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o700)
    curl = fake_bin / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o700)
    fake_now = tmp_path / "fake-now.txt"
    fixed_now = datetime.now(UTC).replace(minute=10, second=0, microsecond=0)
    fake_now.write_text(fixed_now.isoformat().replace("+00:00", "Z"))
    release_artifact = tmp_path / "release-artifact-fixture"
    manifest, freshness = _release_fixture(
        release_artifact,
        fixed_now,
    )
    proof = tmp_path / "proof.json"
    proof.write_bytes(_proof_complete_receipt())
    proof.chmod(0o600)
    phase3 = tmp_path / "phase3-finalized.json"
    phase3.write_bytes(_phase3_receipt(hashlib.sha256(proof.read_bytes()).hexdigest()))
    phase3.chmod(0o600)
    handoff = tmp_path / "phase2-v2-handoff.json"
    handoff.write_bytes(_canonical(_handoff()))
    handoff.chmod(0o600)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state(**state_overrides), sort_keys=True))
    log = tmp_path / "gh.log"
    log.touch()
    return {
        "activation_receipt": tmp_path / "hourly-activation.json",
        "evidence": tmp_path / "producer-evidence",
        "fake_bin": fake_bin,
        "fake_now": fake_now,
        "freshness": freshness,
        "handoff": handoff,
        "log": log,
        "manifest": manifest,
        "phase3": phase3,
        "proof": proof,
        "restore_receipt": tmp_path / "producer-restore.json",
        "release_artifact": release_artifact,
        "state": state_path,
    }


def _environment(harness: dict[str, Path]) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "EXPECTED_HOST_SHA": HOST_SHA,
            "EXPECTED_PUBLIC_RELEASE_SHA": PUBLIC_RELEASE_SHA,
            "FAKE_GH_LOG": str(harness["log"]),
            "FAKE_GH_STATE": str(harness["state"]),
            "FAKE_LIVE_FRESHNESS": str(harness["freshness"]),
            "FAKE_LIVE_MANIFEST": str(harness["manifest"]),
            "FAKE_NOW": harness["fake_now"].read_text(),
            "FAKE_RELEASE_ARTIFACT_DIR": str(harness["release_artifact"]),
            "FAKE_REPO_ROOT": str(ROOT),
            "PATH": f"{harness['fake_bin']}:{environment['PATH']}",
            "PHASE2_V2_HANDOFF_RECEIPT": str(harness["handoff"]),
            "PHASE3_FINALIZED_RECEIPT": str(harness["phase3"]),
            "PHASE3_PROOF_COMPLETE_RECEIPT": str(harness["proof"]),
            "PRODUCER_RESTORE_EVIDENCE_DIR": str(harness["evidence"]),
            "PRODUCER_RESTORE_RECEIPT": str(harness["restore_receipt"]),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def _run_restore(
    harness: dict[str, Path], *, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = _environment(harness)
    if extra:
        environment.update(extra)
    return subprocess.run(
        [str(RESTORE_HELPER)],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=30,
    )


def _run_restore_with_command_timeout(
    harness: dict[str, Path], *, timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    environment = _environment(harness)
    driver = """
import runpy
import sys

path = sys.argv[1]
timeout = float(sys.argv[2])
namespace = runpy.run_path(path)
namespace["main"].__globals__["COMMAND_TIMEOUT_SECONDS"] = timeout
sys.argv = [path]
raise SystemExit(namespace["main"]())
"""
    return subprocess.run(
        [sys.executable, "-c", driver, str(RESTORE_HELPER), str(timeout_seconds)],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=30,
    )


def _run_activation(
    harness: dict[str, Path],
    *,
    typed_sha: str,
    extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = _environment(harness)
    environment.update(
        {
            "HOURLY_ACTIVATION_RECEIPT": str(harness["activation_receipt"]),
            "PRODUCER_RESTORE_RECEIPT": str(harness["restore_receipt"]),
        }
    )
    if extra:
        environment.update(extra)
    driver = """
import datetime
import os
import runpy
import sys

path = sys.argv[1]
namespace = runpy.run_path(path)
fixed = datetime.datetime.fromisoformat(os.environ["FAKE_NOW"].replace("Z", "+00:00"))
clock = [fixed]
globals_ = namespace["main"].__globals__
globals_["_utc_now"] = lambda: clock[0]

def terminal_window(_deadline):
    clock[0] = fixed.replace(minute=20, second=0, microsecond=0)
    return (
        clock[0],
        fixed.replace(minute=30, second=0, microsecond=0),
    )

globals_["_wait_terminal_reactivation_window"] = terminal_window
cross = os.environ.get("FAKE_CLOCK_CROSS", "")
if cross == "arming-13":
    original_freeze = globals_["_freeze_producers"]
    def freeze_then_cross(*args, **kwargs):
        result = original_freeze(*args, **kwargs)
        clock[0] = fixed.replace(minute=13, second=0, microsecond=0)
        return result
    globals_["_freeze_producers"] = freeze_then_cross
elif cross == "terminal-admission-30":
    original_exact_inventories = globals_["_exact_run_inventories"]
    def inventories_then_cross(*args, **kwargs):
        result = original_exact_inventories(*args, **kwargs)
        if clock[0].minute == 20:
            clock[0] = fixed.replace(minute=30, second=0, microsecond=0)
        return result
    globals_["_exact_run_inventories"] = inventories_then_cross
elif cross == "terminal-enable-cross-30":
    original_set_workflow_state = globals_["_set_workflow_state"]
    def enable_then_cross(*args, **kwargs):
        result = original_set_workflow_state(*args, **kwargs)
        if (
            kwargs.get("workflow_id") == 343876046
            and kwargs.get("state") == "active"
            and clock[0].minute == 20
        ):
            clock[0] = fixed.replace(minute=30, second=0, microsecond=0)
        return result
    globals_["_set_workflow_state"] = enable_then_cross
elif cross == "terminal-58":
    original_live_proof = globals_["_validate_live_proof"]
    def live_then_cross(*args, **kwargs):
        result = original_live_proof(*args, **kwargs)
        clock[0] = fixed.replace(minute=58, second=0, microsecond=0)
        return result
    globals_["_validate_live_proof"] = live_then_cross
sys.argv = [path]
raise SystemExit(namespace["main"]())
"""
    return subprocess.run(
        [sys.executable, "-c", driver, str(ACTIVATE_HELPER)],
        capture_output=True,
        check=False,
        env=environment,
        input=f"{typed_sha}\n",
        text=True,
        timeout=60,
    )


def _load_state(harness: dict[str, Path]) -> dict[str, object]:
    return json.loads(harness["state"].read_text())


def _calls(harness: dict[str, Path]) -> list[list[str]]:
    return [json.loads(line) for line in harness["log"].read_text().splitlines()]


def _hourly_failure(harness: dict[str, Path]) -> dict[str, object]:
    raw = harness["activation_receipt"].read_bytes()
    receipt = json.loads(raw)
    assert raw == _canonical(receipt)
    assert stat.S_IMODE(harness["activation_receipt"].stat().st_mode) == 0o400
    assert receipt["schema_version"] == (
        "palimpsest.railway-hourly-publication-failure.v1"
    )
    assert receipt["status"] in {"failed-closed", "cleanup-unproved"}
    return receipt


def _newswire_outcome(branch: str) -> dict[str, object]:
    head_sha = PUBLIC_RELEASE_SHA
    output_sha = NEWSWIRE_SHA
    document: dict[str, object] = {
        "acquisition_base_sha": head_sha,
        "base_sha": head_sha,
        "candidate_changed": True,
        "candidate_sha": output_sha,
        "current_main_sha": output_sha,
        "event": "workflow_dispatch",
        "head_sha": head_sha,
        "output_parents": [head_sha],
        "output_sha": output_sha,
        "push_outcome": "success",
        "recorded_at": _clock(),
        "repository": REPOSITORY,
        "result": "committed",
        "retry_candidate_changed": None,
        "retry_candidate_sha": None,
        "retry_outcome": "skipped",
        "run_attempt": 1,
        "run_id": 101,
        "schema_version": "palimpsest.newswire-manual-outcome.v1",
        "synchronized_candidate_changed": None,
        "workflow": ".github/workflows/newswire-refresh.yml",
        "workflow_name": "Refresh evidence wire",
    }
    if branch == "retry":
        document.update(
            {
                "candidate_sha": "6" * 40,
                "push_outcome": "failure",
                "retry_candidate_changed": True,
                "retry_candidate_sha": output_sha,
                "retry_outcome": "success",
            }
        )
    elif branch == "no_change":
        document.update(
            {
                "candidate_changed": False,
                "candidate_sha": head_sha,
                "current_main_sha": head_sha,
                "output_parents": [],
                "output_sha": head_sha,
                "push_outcome": "skipped",
                "result": "no_change",
            }
        )
    elif branch != "initial":
        raise AssertionError(f"unsupported Newswire fixture branch: {branch}")
    return document


def _osint_outcome(branch: str) -> dict[str, object]:
    head_sha = NEWSWIRE_SHA
    output_sha = OSINT_SHA
    document: dict[str, object] = {
        "acquisition_base_sha": head_sha,
        "base_sha": head_sha,
        "candidate_changed": True,
        "candidate_sha": output_sha,
        "current_main_sha": output_sha,
        "event": "workflow_dispatch",
        "expected_deploy_sha": head_sha,
        "head_sha": head_sha,
        "output_parents": [head_sha],
        "output_sha": output_sha,
        "publication_commit": output_sha,
        "push_exit_code": 0,
        "push_outcome": "success",
        "recorded_at": _clock(),
        "release_nonce": "9" * 32,
        "repository": REPOSITORY,
        "result": "committed",
        "retry_candidate_changed": None,
        "retry_candidate_sha": None,
        "retry_exit_code": None,
        "retry_outcome": "skipped",
        "run_attempt": 1,
        "run_id": 102,
        "schema_version": "palimpsest.osint-manual-outcome.v1",
        "synchronized_candidate_changed": None,
        "workflow": ".github/workflows/osint-china-v2-refresh.yml",
        "workflow_name": "Refresh OSINT China roll-up v2",
    }
    if branch == "contract_retry":
        document.update({"push_exit_code": 76, "push_outcome": "failure"})
    elif branch == "race_retry":
        document.update(
            {
                "candidate_sha": "6" * 40,
                "push_exit_code": 75,
                "push_outcome": "failure",
                "retry_candidate_changed": True,
                "retry_candidate_sha": output_sha,
                "retry_exit_code": 76,
                "retry_outcome": "failure",
            }
        )
    elif branch == "no_change":
        document.update(
            {
                "candidate_changed": False,
                "candidate_sha": head_sha,
                "current_main_sha": head_sha,
                "output_parents": [],
                "output_sha": head_sha,
                "publication_commit": head_sha,
                "push_exit_code": None,
                "push_outcome": "skipped",
                "result": "no_change",
            }
        )
    elif branch != "initial":
        raise AssertionError(f"unsupported OSINT fixture branch: {branch}")
    return document


def test_manual_outcome_validators_accept_only_exact_claimed_branches() -> None:
    namespace = runpy.run_path(str(RESTORE_HELPER))
    validate_newswire = namespace["_validate_newswire_artifact"]
    validate_osint = namespace["_validate_osint_artifact"]

    for branch in ("initial", "retry"):
        document = _newswire_outcome(branch)
        assert (
            validate_newswire(
                _canonical(document),
                run_id=101,
                head_sha=PUBLIC_RELEASE_SHA,
                main_after=NEWSWIRE_SHA,
                repository=REPOSITORY,
            )[0]
            == "published"
        )
    document = _newswire_outcome("no_change")
    assert (
        validate_newswire(
            _canonical(document),
            run_id=101,
            head_sha=PUBLIC_RELEASE_SHA,
            main_after=PUBLIC_RELEASE_SHA,
            repository=REPOSITORY,
        )[0]
        == "no_change"
    )

    for branch in ("initial", "contract_retry", "race_retry"):
        document = _osint_outcome(branch)
        assert (
            validate_osint(
                _canonical(document),
                run_id=102,
                head_sha=NEWSWIRE_SHA,
                nonce="9" * 32,
                main_after=OSINT_SHA,
                repository=REPOSITORY,
            )[0]
            == "published"
        )
    document = _osint_outcome("no_change")
    assert (
        validate_osint(
            _canonical(document),
            run_id=102,
            head_sha=NEWSWIRE_SHA,
            nonce="9" * 32,
            main_after=NEWSWIRE_SHA,
            repository=REPOSITORY,
        )[0]
        == "no_change"
    )


@pytest.mark.parametrize(
    ("branch", "field", "poison"),
    [
        ("initial", "candidate_sha", "6" * 40),
        ("initial", "synchronized_candidate_changed", False),
        ("initial", "synchronized_candidate_changed", "true"),
        ("initial", "retry_candidate_changed", False),
        ("initial", "retry_candidate_sha", "7" * 40),
        ("retry", "retry_candidate_changed", None),
        ("retry", "retry_candidate_sha", "7" * 40),
        ("no_change", "synchronized_candidate_changed", True),
        ("no_change", "retry_candidate_changed", False),
        ("no_change", "retry_candidate_sha", "7" * 40),
    ],
)
def test_newswire_outcome_rejects_poisoned_branch_fields(
    branch: str, field: str, poison: object
) -> None:
    namespace = runpy.run_path(str(RESTORE_HELPER))
    document = _newswire_outcome(branch)
    document[field] = poison
    main_after = PUBLIC_RELEASE_SHA if branch == "no_change" else NEWSWIRE_SHA

    with pytest.raises(namespace["RestoreError"]):
        namespace["_validate_newswire_artifact"](
            _canonical(document),
            run_id=101,
            head_sha=PUBLIC_RELEASE_SHA,
            main_after=main_after,
            repository=REPOSITORY,
        )


@pytest.mark.parametrize(
    ("branch", "field", "poison"),
    [
        ("initial", "candidate_sha", "6" * 40),
        ("initial", "synchronized_candidate_changed", False),
        ("initial", "synchronized_candidate_changed", "true"),
        ("initial", "retry_candidate_changed", False),
        ("initial", "retry_candidate_sha", "7" * 40),
        ("initial", "push_exit_code", False),
        ("race_retry", "retry_candidate_changed", None),
        ("race_retry", "retry_candidate_sha", "7" * 40),
        ("no_change", "synchronized_candidate_changed", True),
        ("no_change", "retry_candidate_changed", False),
        ("no_change", "retry_candidate_sha", "7" * 40),
    ],
)
def test_osint_outcome_rejects_poisoned_branch_fields(
    branch: str, field: str, poison: object
) -> None:
    namespace = runpy.run_path(str(RESTORE_HELPER))
    document = _osint_outcome(branch)
    document[field] = poison
    main_after = NEWSWIRE_SHA if branch == "no_change" else OSINT_SHA

    with pytest.raises(namespace["RestoreError"]):
        namespace["_validate_osint_artifact"](
            _canonical(document),
            run_id=102,
            head_sha=NEWSWIRE_SHA,
            nonce="9" * 32,
            main_after=main_after,
            repository=REPOSITORY,
        )


def test_restore_and_terminal_hourly_activation_are_exact(tmp_path: Path) -> None:
    harness = _prepare(tmp_path)
    restored = _run_restore(harness)

    assert restored.returncode == 0, restored.stderr
    restore_raw = harness["restore_receipt"].read_bytes()
    restore = json.loads(restore_raw)
    assert restore_raw == _canonical(restore)
    assert stat.S_IMODE(harness["restore_receipt"].stat().st_mode) == 0o400
    assert restore["schema_version"] == "palimpsest.github-producer-restore.v2"
    assert restore["initial_main_sha"] == PUBLIC_RELEASE_SHA
    assert restore["source_graph"]["host_sha"] == HOST_SHA
    assert restore["source_graph"]["publication_sha"] == PUBLICATION_SHA
    assert restore["source_graph"]["public_release_sha"] == PUBLIC_RELEASE_SHA
    assert restore["final_main_sha"] == OSINT_SHA
    assert [item["outcome"] for item in restore["completed_stages"]] == [
        "published",
        "published",
        "abstained",
    ]
    for reference in restore["completed_stages"]:
        path = Path(reference["receipt"])
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
        assert (
            hashlib.sha256(path.read_bytes()).hexdigest() == reference["receipt_sha256"]
        )
    state = _load_state(harness)
    assert state["gate"] == "false"
    assert state["ack_present"] is True
    assert set(state["workflow_states"].values()) == {"active"}
    assert state["true_writes"] == 0
    assert not any(call[:2] == ["run", "cancel"] for call in _calls(harness))

    activated = _run_activation(harness, typed_sha=OSINT_SHA)

    assert activated.returncode == 0, activated.stderr
    activation_raw = harness["activation_receipt"].read_bytes()
    activation = json.loads(activation_raw)
    assert activation_raw == _canonical(activation)
    assert stat.S_IMODE(harness["activation_receipt"].stat().st_mode) == 0o400
    assert activation["schema_version"] == (
        "palimpsest.railway-hourly-publication-steady-state.v1"
    )
    assert activation["status"] == "verified"
    assert activation["source_commit"] == OSINT_SHA
    assert activation["controller"]["event"] == "schedule"
    assert activation["controller"]["run_id"] == 201
    assert activation["release"]["run_id"] == 202
    assert activation["live"]["source_commit"] == OSINT_SHA
    assert activation["workflow_freeze"] == {
        "controller_disabled_after_binding": True,
        "producers_disabled_before_gate": True,
        "reactivated_at": activation["workflow_freeze"]["reactivated_at"],
        "terminal_receipt_cutoff": activation["workflow_freeze"][
            "terminal_receipt_cutoff"
        ],
        "terminal_window_utc_minute_offsets": {
            "admission_end": 30,
            "admission_start": 20,
            "proof_end": 40,
            "receipt_cutoff": 50,
        },
    }
    assert {item["id"] for item in activation["workflows"]} == {
        332082300,
        341368020,
        333753226,
        343876046,
    }
    assert {item["state"] for item in activation["workflows"]} == {"active"}
    assert (
        activation["authority"]["protected_environment_contract"]["can_admins_bypass"]
        is False
    )
    state = _load_state(harness)
    assert state["gate"] == "true"
    assert state["ack_present"] is True
    assert state["true_writes"] == 1
    assert state["controller_state"] == "active"
    assert set(state["workflow_states"].values()) == {"active"}
    true_calls = [
        call
        for call in _calls(harness)
        if call[:3] == ["variable", "set", "RAILWAY_PUBLICATION_ENABLED"]
        and call[call.index("--body") + 1] == "true"
    ]
    assert len(true_calls) == 1
    assert all(call[0] == "curl" for call in _calls(harness)[-4:])


def test_scheduled_controller_no_change_opens_steady_state_without_tests_child(
    tmp_path: Path,
) -> None:
    harness = _prepare(tmp_path)
    assert _run_restore(harness).returncode == 0
    state = _load_state(harness)
    state["controller_no_change"] = True
    harness["state"].write_text(json.dumps(state, sort_keys=True))

    result = _run_activation(harness, typed_sha=OSINT_SHA)

    assert result.returncode == 0, result.stderr
    receipt = json.loads(harness["activation_receipt"].read_bytes())
    assert receipt["status"] == "verified"
    assert receipt["controller"]["result"] == "no_change"
    assert receipt["release"] is None
    assert receipt["artifact_evidence"]["controller_request_artifact"] is None
    assert receipt["artifact_evidence"]["release_artifact"] is None
    assert _load_state(harness)["runs"]["323903518"] == []
    assert all(call[0] == "curl" for call in _calls(harness)[-4:])


def test_long_release_suppresses_newswire_17_and_next_controller_13_ticks(
    tmp_path: Path,
) -> None:
    harness = _prepare(tmp_path)
    assert _run_restore(harness).returncode == 0
    state = _load_state(harness)
    state["simulate_frozen_cron_ticks"] = True
    harness["state"].write_text(json.dumps(state, sort_keys=True))

    result = _run_activation(harness, typed_sha=OSINT_SHA)

    assert result.returncode == 0, result.stderr
    state = _load_state(harness)
    assert state["newswire_17_suppressed"] is True
    assert state["next_controller_13_suppressed"] is True
    assert state["runs"]["332082300"][-1]["event"] == "workflow_dispatch"
    assert len(state["runs"]["343876046"]) == 1
    assert state["controller_state"] == "active"
    assert set(state["workflow_states"].values()) == {"active"}
    assert json.loads(harness["activation_receipt"].read_bytes())["status"] == (
        "verified"
    )


@pytest.mark.parametrize("mode", ["race", "wrong_sha", "missing_jobs"])
def test_first_stage_adversity_disables_all_and_clears_authority(
    tmp_path: Path, mode: str
) -> None:
    overrides = {
        "race": {"race_stage": 1},
        "wrong_sha": {"wrong_sha_stage": 1},
        "missing_jobs": {"missing_jobs_stage": 1},
    }[mode]
    harness = _prepare(tmp_path, **overrides)

    result = _run_restore(harness)

    assert result.returncode != 0
    failure = json.loads(harness["restore_receipt"].read_bytes())
    assert failure["status"] == "cleanup-unproved"
    state = _load_state(harness)
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    assert set(state["workflow_states"].values()) == {"disabled_manually"}
    assert not any(call[:2] == ["run", "cancel"] for call in _calls(harness))


def test_unproved_second_stage_advance_is_not_resumable(
    tmp_path: Path,
) -> None:
    harness = _prepare(tmp_path, failed_job_stage=2)

    result = _run_restore(harness)

    assert result.returncode != 0
    state = _load_state(harness)
    assert set(state["workflow_states"].values()) == {"disabled_manually"}
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    first = harness["evidence"] / "01-newswire-refresh.json"
    assert first.is_file()
    assert not (harness["evidence"] / "02-osint-china-v2-refresh.json").exists()
    failure = json.loads(harness["restore_receipt"].read_bytes())
    assert failure["status"] == "cleanup-unproved"
    assert failure["final_main_sha"] == OSINT_SHA
    assert len(failure["completed_stages"]) == 1


def test_server_accepted_cli_failure_is_never_resume_authority(tmp_path: Path) -> None:
    harness = _prepare(tmp_path, dispatch_cli_failure_stage=1)

    result = _run_restore(harness)

    assert result.returncode == 42
    failure = json.loads(harness["restore_receipt"].read_bytes())
    assert failure["status"] == "cleanup-unproved"
    assert failure["failure"]["resume_main_sha"] is None
    assert failure["final_main_sha"] == NEWSWIRE_SHA
    state = _load_state(harness)
    assert state["runs"]["332082300"]
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    assert set(state["workflow_states"].values()) == {"disabled_manually"}


def test_server_accepted_dispatch_timeout_is_never_resume_authority(
    tmp_path: Path,
) -> None:
    harness = _prepare(tmp_path, sleep_after_dispatch_stage=1)

    result = _run_restore_with_command_timeout(harness, timeout_seconds=0.5)

    assert result.returncode != 0
    failure = json.loads(harness["restore_receipt"].read_bytes())
    assert failure["status"] == "cleanup-unproved"
    assert failure["failure"]["resume_main_sha"] is None
    assert failure["final_main_sha"] == NEWSWIRE_SHA
    state = _load_state(harness)
    assert state["runs"]["332082300"]
    assert state["ack_present"] is False
    assert set(state["workflow_states"].values()) == {"disabled_manually"}


def test_cleanup_main_advance_after_last_proved_stage_is_not_resumable(
    tmp_path: Path,
) -> None:
    harness = _prepare(tmp_path, main_advance_after_last_proved=True)

    result = _run_restore(harness)

    assert result.returncode != 0
    failure = json.loads(harness["restore_receipt"].read_bytes())
    assert failure["status"] == "cleanup-unproved"
    assert failure["failure"]["resume_main_sha"] is None
    assert failure["final_main_sha"] == OSINT_SHA
    assert failure["completed_stages"][0]["main_after"] == NEWSWIRE_SHA
    state = _load_state(harness)
    assert state["ack_present"] is False
    assert set(state["workflow_states"].values()) == {"disabled_manually"}


def test_late_scheduled_run_and_post_disable_advance_are_never_resumable(
    tmp_path: Path,
) -> None:
    harness = _prepare(
        tmp_path,
        cleanup_schedule_advances=True,
        late_schedule_stage=1,
    )

    result = _run_restore(harness)

    assert result.returncode != 0
    failure = json.loads(harness["restore_receipt"].read_bytes())
    assert failure["status"] == "cleanup-unproved"
    assert failure["failure"]["resume_main_sha"] is None
    assert failure["final_main_sha"] == "a" * 40
    state = _load_state(harness)
    assert state["cleanup_schedule_advanced"] is True
    assert state["ack_present"] is False
    assert set(state["workflow_states"].values()) == {"disabled_manually"}


def test_api_lagged_unexpected_run_breaks_repeated_cleanup_quiet_proof(
    tmp_path: Path,
) -> None:
    harness = _prepare(
        tmp_path,
        late_schedule_stage=1,
        unexpected_visibility_lag_reads=3,
        workflow_drift_stage=2,
    )

    result = _run_restore(harness)

    assert result.returncode != 0
    failure = json.loads(harness["restore_receipt"].read_bytes())
    assert failure["status"] == "cleanup-unproved"
    assert failure["failure"]["resume_main_sha"] is None
    state = _load_state(harness)
    assert state["unexpected_inventory_reads"] >= 4
    assert state["ack_present"] is False
    assert set(state["workflow_states"].values()) == {"disabled_manually"}


def test_final_activation_boundary_schedule_refreezes_every_owner(
    tmp_path: Path,
) -> None:
    harness = _prepare(
        tmp_path,
        final_activation_schedule_workflow="332082300",
    )

    result = _run_restore(harness)

    assert result.returncode != 0
    failure = json.loads(harness["restore_receipt"].read_bytes())
    assert failure["status"] == "cleanup-unproved"
    state = _load_state(harness)
    assert state["ack_present"] is False
    assert set(state["workflow_states"].values()) == {"disabled_manually"}


@pytest.mark.parametrize("when", ["stage", "final_activation"])
def test_historical_same_id_rerun_is_never_resume_authority(
    tmp_path: Path, when: str
) -> None:
    harness = _prepare(tmp_path)
    state = _load_state(harness)
    state["runs"]["332082300"] = [
        {
            "conclusion": "success",
            "created_at": _clock(timedelta(hours=-1)),
            "event": "schedule",
            "head_branch": "main",
            "head_sha": PUBLIC_RELEASE_SHA,
            "id": 77,
            "name": "Refresh evidence wire",
            "path": ".github/workflows/newswire-refresh.yml",
            "run_attempt": 1,
            "status": "completed",
            "updated_at": _clock(timedelta(hours=-1)),
        }
    ]
    if when == "stage":
        state["historical_rerun_stage"] = 1
    else:
        state["historical_rerun_final_workflow"] = "332082300"
    harness["state"].write_text(json.dumps(state, sort_keys=True))

    result = _run_restore(harness)

    assert result.returncode != 0
    terminal = json.loads(harness["restore_receipt"].read_bytes())
    assert terminal["status"] == "cleanup-unproved"
    assert terminal["failure"]["resume_main_sha"] is None
    state = _load_state(harness)
    historical = next(item for item in state["runs"]["332082300"] if item["id"] == 77)
    assert historical["run_attempt"] == 2
    assert historical["status"] == "queued"
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    assert set(state["workflow_states"].values()) == {"disabled_manually"}


def test_workflow_yaml_drift_between_stages_refreezes_all(tmp_path: Path) -> None:
    harness = _prepare(tmp_path, workflow_drift_stage=2)

    result = _run_restore(harness)

    assert result.returncode != 0
    failure = json.loads(harness["restore_receipt"].read_bytes())
    assert failure["status"] == "failed-closed"
    assert failure["final_main_sha"] == NEWSWIRE_SHA
    assert len(failure["completed_stages"]) == 1
    state = _load_state(harness)
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    assert set(state["workflow_states"].values()) == {"disabled_manually"}

    state["ack_present"] = True
    state["ack_value"] = ACK
    state["workflow_drift_stage"] = None
    state["newswire_no_change"] = True
    state["runs"] = {str(item[1]): [] for item in WORKFLOWS}
    harness["state"].write_text(json.dumps(state, sort_keys=True))
    retry_receipt = tmp_path / "producer-restore-retry.json"
    retry_evidence = tmp_path / "producer-evidence-retry"
    retried = _run_restore(
        harness,
        extra={
            "PRODUCER_RESTORE_EVIDENCE_DIR": str(retry_evidence),
            "PRODUCER_RESTORE_RECEIPT": str(retry_receipt),
            "PRODUCER_RESTORE_RESUME_RECEIPT": str(harness["restore_receipt"]),
        },
    )

    assert retried.returncode == 0, retried.stderr
    retry = json.loads(retry_receipt.read_bytes())
    assert retry["status"] == "verified"
    assert retry["initial_main_sha"] == NEWSWIRE_SHA
    assert retry["final_main_sha"] == OSINT_SHA
    assert retry["resume"]["receipt"] == str(harness["restore_receipt"])


def test_osint_exact_no_change_and_watchdog_exact_abstention_are_accepted(
    tmp_path: Path,
) -> None:
    harness = _prepare(tmp_path, osint_no_change=True)

    result = _run_restore(harness)

    assert result.returncode == 0, result.stderr
    receipt = json.loads(harness["restore_receipt"].read_bytes())
    assert receipt["final_main_sha"] == NEWSWIRE_SHA
    assert [item["outcome"] for item in receipt["completed_stages"]] == [
        "published",
        "no_change",
        "abstained",
    ]


@pytest.mark.parametrize("mode", ["nonempty_receipt", "producer_race"])
def test_watchdog_non_abstention_is_rejected_and_watchdog_is_refrozen(
    tmp_path: Path, mode: str
) -> None:
    harness = _prepare(
        tmp_path,
        watchdog_nonempty=mode == "nonempty_receipt",
        watchdog_dispatch_race=mode == "producer_race",
    )

    result = _run_restore(harness)

    assert result.returncode != 0
    state = _load_state(harness)
    assert set(state["workflow_states"].values()) == {"disabled_manually"}
    assert state["gate"] == "false"
    assert state["ack_present"] is False


def test_signal_during_dispatch_terminates_child_and_rolls_back(tmp_path: Path) -> None:
    harness = _prepare(tmp_path, sleep_after_dispatch_stage=1)
    marker = tmp_path / "sleeping-gh"
    environment = _environment(harness)
    environment["FAKE_SLEEP_MARKER"] = str(marker)
    process = subprocess.Popen(
        [str(RESTORE_HELPER)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists()
    process.send_signal(signal.SIGTERM)
    _, stderr = process.communicate(timeout=15)

    assert process.returncode == 143, stderr
    failure = json.loads(harness["restore_receipt"].read_bytes())
    assert failure["status"] == "cleanup-unproved"
    assert failure["failure"]["resume_main_sha"] is None
    assert failure["final_main_sha"] == NEWSWIRE_SHA
    state = _load_state(harness)
    assert state["runs"]["332082300"]
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    assert set(state["workflow_states"].values()) == {"disabled_manually"}


def test_wrong_typed_sha_never_opens_gate_and_clears_ack(tmp_path: Path) -> None:
    harness = _prepare(tmp_path)
    assert _run_restore(harness).returncode == 0

    result = _run_activation(harness, typed_sha="f" * 40)

    assert result.returncode != 0
    state = _load_state(harness)
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    assert state["true_writes"] == 0
    assert _hourly_failure(harness)["status"] == "failed-closed"
    assert state["controller_state"] == "active"
    assert set(state["workflow_states"].values()) == {"active"}


def test_activation_rejects_duplicate_scheduled_controller_and_rolls_back(
    tmp_path: Path,
) -> None:
    harness = _prepare(tmp_path)
    assert _run_restore(harness).returncode == 0
    state = _load_state(harness)
    state["scheduled_race"] = True
    harness["state"].write_text(json.dumps(state, sort_keys=True))

    result = _run_activation(harness, typed_sha=OSINT_SHA)

    assert result.returncode != 0
    state = _load_state(harness)
    assert state["gate"] == "false"
    assert state["true_writes"] == 1
    assert state["ack_present"] is False
    assert _hourly_failure(harness)["status"] == "cleanup-unproved"
    assert state["controller_state"] == "active"
    assert set(state["workflow_states"].values()) == {"active"}


def test_post_true_failure_forces_false_and_removes_only_exact_ack(
    tmp_path: Path,
) -> None:
    harness = _prepare(tmp_path)
    assert _run_restore(harness).returncode == 0
    state = _load_state(harness)
    state["fail_after_true"] = True
    harness["state"].write_text(json.dumps(state, sort_keys=True))

    result = _run_activation(harness, typed_sha=OSINT_SHA)

    assert result.returncode != 0
    state = _load_state(harness)
    assert state["true_writes"] == 1
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    assert _hourly_failure(harness)["status"] == "cleanup-unproved"
    assert state["controller_state"] == "active"
    assert set(state["workflow_states"].values()) == {"active"}


@pytest.mark.parametrize(
    "mode", ["scheduled_wrong_sha", "release_missing_job", "live_mismatch"]
)
def test_scheduled_release_adversity_rolls_gate_back_and_clears_ack(
    tmp_path: Path, mode: str
) -> None:
    harness = _prepare(tmp_path)
    assert _run_restore(harness).returncode == 0
    state = _load_state(harness)
    state[mode] = True
    harness["state"].write_text(json.dumps(state, sort_keys=True))

    result = _run_activation(harness, typed_sha=OSINT_SHA)

    assert result.returncode != 0
    state = _load_state(harness)
    assert state["true_writes"] == 1
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    expected_status = (
        "cleanup-unproved" if mode == "scheduled_wrong_sha" else "failed-closed"
    )
    assert _hourly_failure(harness)["status"] == expected_status
    assert state["controller_state"] == "active"
    assert set(state["workflow_states"].values()) == {"active"}
    assert not any(call[:2] == ["run", "cancel"] for call in _calls(harness))


@pytest.mark.parametrize("mode", ["can_admins_bypass", "environment_variable_drift"])
def test_hourly_environment_contract_drift_never_opens_gate(
    tmp_path: Path, mode: str
) -> None:
    harness = _prepare(tmp_path)
    assert _run_restore(harness).returncode == 0
    state = _load_state(harness)
    state[mode] = True
    harness["state"].write_text(json.dumps(state, sort_keys=True))

    result = _run_activation(harness, typed_sha=OSINT_SHA)

    assert result.returncode != 0
    state = _load_state(harness)
    assert state["true_writes"] == 0
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    assert _hourly_failure(harness)["status"] == "failed-closed"


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        (
            "approval_pending_drift_read",
            "pending protected release changed at approval boundary",
        ),
        ("approval_main_drift_read", "public main is not the exact restored final SHA"),
        (
            "approval_environment_drift_read",
            "protected environment",
        ),
        ("approval_release_race_read", "Tests release run set changed"),
        (
            "approval_controller_rerun_read",
            "new controller run is not the exact scheduled authority",
        ),
        (
            "approval_release_rerun_read",
            "new downstream run is not the exact release child",
        ),
        (
            "approval_controller_rerun_after_env_read",
            "bound run identity changed during final approval revalidation",
        ),
        (
            "approval_release_status_after_env_read",
            "bound run identity changed during final approval revalidation",
        ),
    ],
)
def test_hourly_approval_boundary_revalidates_every_mutable_authority(
    tmp_path: Path, drift: str, message: str
) -> None:
    harness = _prepare(tmp_path)
    assert _run_restore(harness).returncode == 0
    state = _load_state(harness)
    state[drift] = 3
    harness["state"].write_text(json.dumps(state, sort_keys=True))

    result = _run_activation(harness, typed_sha=OSINT_SHA)

    assert result.returncode != 0
    assert message in result.stderr
    state = _load_state(harness)
    assert state["release_approved"] is False
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    assert state["controller_state"] == "active"
    assert set(state["workflow_states"].values()) == {"active"}
    assert _hourly_failure(harness)["status"] == "cleanup-unproved"


def test_hourly_release_archive_must_match_github_digest(tmp_path: Path) -> None:
    harness = _prepare(tmp_path)
    assert _run_restore(harness).returncode == 0
    state = _load_state(harness)
    state["release_archive_digest_mismatch"] = True
    harness["state"].write_text(json.dumps(state, sort_keys=True))

    result = _run_activation(harness, typed_sha=OSINT_SHA)

    assert result.returncode != 0
    assert "archive bytes do not match GitHub metadata" in result.stderr
    state = _load_state(harness)
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    assert _hourly_failure(harness)["status"] == "failed-closed"


def test_hourly_github_unix_socket_override_stops_before_gate(tmp_path: Path) -> None:
    harness = _prepare(tmp_path)
    assert _run_restore(harness).returncode == 0
    state = _load_state(harness)
    state["http_unix_socket"] = "/tmp/forged-github.sock"
    harness["state"].write_text(json.dumps(state, sort_keys=True))

    result = _run_activation(harness, typed_sha=OSINT_SHA)

    assert result.returncode != 0
    assert "http_unix_socket transport is not empty" in result.stderr
    state = _load_state(harness)
    assert state["true_writes"] == 0
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    assert _hourly_failure(harness)["status"] == "failed-closed"


def test_restore_github_unix_socket_override_stops_before_dispatch(
    tmp_path: Path,
) -> None:
    harness = _prepare(tmp_path)
    state = _load_state(harness)
    state["http_unix_socket"] = "/tmp/forged-github.sock"
    harness["state"].write_text(json.dumps(state, sort_keys=True))

    result = _run_restore(harness)

    assert result.returncode != 0
    assert "http_unix_socket transport is not empty" in result.stderr
    state = _load_state(harness)
    assert all(not runs for runs in state["runs"].values())
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    terminal = json.loads(harness["restore_receipt"].read_bytes())
    assert terminal["status"] == "cleanup-unproved"


def test_restore_rejects_public_main_advance_without_exact_resume_receipt(
    tmp_path: Path,
) -> None:
    harness = _prepare(tmp_path, main=OSINT_SHA)

    result = _run_restore(harness)

    assert result.returncode != 0
    receipt = json.loads(harness["restore_receipt"].read_bytes())
    assert receipt["status"] == "cleanup-unproved"
    assert receipt["final_main_sha"] == OSINT_SHA
    state = _load_state(harness)
    assert state["ack_present"] is False
    assert set(state["workflow_states"].values()) == {"disabled_manually"}


@pytest.mark.parametrize("forgery", ["proof_digest", "public_release_sha"])
def test_restore_rejects_forged_h_p_r_prerequisite_and_cleans_authority(
    tmp_path: Path, forgery: str
) -> None:
    harness = _prepare(tmp_path)
    extra: dict[str, str] = {}
    if forgery == "proof_digest":
        proof = json.loads(harness["proof"].read_bytes())
        proof["deployment"]["controller_tree_sha256"] = "f" * 64
        harness["proof"].write_bytes(_canonical(proof))
    else:
        extra["EXPECTED_PUBLIC_RELEASE_SHA"] = "f" * 40

    result = _run_restore(harness, extra=extra)

    assert result.returncode != 0
    terminal = json.loads(harness["restore_receipt"].read_bytes())
    assert terminal["status"] == "cleanup-unproved"
    state = _load_state(harness)
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    assert set(state["workflow_states"].values()) == {"disabled_manually"}


def test_unfamiliar_ack_is_never_deleted(tmp_path: Path) -> None:
    harness = _prepare(tmp_path, ack_value="unfamiliar-writer")

    result = _run_restore(harness)

    assert result.returncode != 0
    state = _load_state(harness)
    assert state["ack_present"] is True
    assert not any(call[:2] == ["variable", "delete"] for call in _calls(harness))


class _BrokenOutput:
    def write(self, _value: str) -> int:
        raise BrokenPipeError("injected")

    def flush(self) -> None:
        raise BrokenPipeError("injected")


def test_postcommit_output_reporting_is_warning_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    restore = runpy.run_path(str(RESTORE_HELPER))
    activate = runpy.run_path(str(ACTIVATE_HELPER))
    broken = _BrokenOutput()
    monkeypatch.setattr(sys, "stdout", broken)

    restore["_report_terminal_warning_only"](
        tmp_path / "restore.json", "a" * 64, "verified"
    )
    activate["_report_warning_only"](tmp_path / "activation.json", "b" * 64, "verified")


@pytest.mark.parametrize("crossing", ["prelink", "postfsync"])
def test_verified_hourly_receipt_cannot_commit_after_terminal_cutoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, crossing: str
) -> None:
    namespace = runpy.run_path(str(ACTIVATE_HELPER))
    tmp_path.chmod(0o700)
    destination = tmp_path / "verified.json"
    cutoff = datetime(2026, 8, 28, 10, 50, tzinfo=UTC)
    clock = [cutoff if crossing == "prelink" else cutoff - timedelta(seconds=1)]
    namespace["_write_final_receipt"].__globals__["_utc_now"] = lambda: clock[0]
    original_fsync = os.fsync
    fsync_calls = 0

    def crossing_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        original_fsync(descriptor)
        fsync_calls += 1
        if crossing == "postfsync" and fsync_calls == 2:
            clock[0] = cutoff

    monkeypatch.setattr(os, "fsync", crossing_fsync)
    state = namespace["RunState"]()

    with pytest.raises(namespace["ActivationError"], match=":50 cutoff"):
        namespace["_write_final_receipt"](
            destination,
            {"schema_version": "fixture.v1", "status": "verified"},
            state,
            not_after=cutoff,
        )

    assert state.receipt_committed is False
    assert destination.exists() is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "interrupt_after", ["controller_run_id", "release_unbound", "controller_unbound"]
)
def test_controller_binding_transition_never_drops_both_uncertainty_flags(
    interrupt_after: str,
) -> None:
    namespace = runpy.run_path(str(ACTIVATE_HELPER))

    class InterruptingState:
        def __init__(self) -> None:
            object.__setattr__(self, "armed", False)
            object.__setattr__(self, "interrupt_after", interrupt_after)
            object.__setattr__(self, "controller_run_id", None)
            object.__setattr__(self, "controller_unbound", True)
            object.__setattr__(self, "release_unbound", False)
            object.__setattr__(self, "armed", True)

        def __setattr__(self, name: str, value: object) -> None:
            object.__setattr__(self, name, value)
            if self.armed and name == self.interrupt_after:
                raise RuntimeError("injected assignment-boundary signal")

    state = InterruptingState()
    before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    with pytest.raises(RuntimeError, match="assignment-boundary"):
        namespace["_bind_controller_run"](state, 201)
    after = signal.pthread_sigmask(signal.SIG_BLOCK, set())

    assert before == after
    assert state.controller_unbound or state.release_unbound


@pytest.mark.parametrize(
    ("helper", "error_name"),
    [
        (RESTORE_HELPER, "RestoreError"),
        (ACTIVATE_HELPER, "ActivationError"),
    ],
)
def test_process_group_timeout_terminates_descendant(
    tmp_path: Path,
    helper: Path,
    error_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(helper))
    interpreter = _bind_test_interpreter(namespace, monkeypatch)
    child_started = tmp_path / "child-started"
    child_terminated = tmp_path / "child-terminated"
    child = tmp_path / "child.py"
    child.write_text(
        """
import signal
import sys
import time
from pathlib import Path

started = Path(sys.argv[1])
terminated = Path(sys.argv[2])

def stop(_signum, _frame):
    terminated.write_text("yes")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
started.write_text("yes")
while True:
    time.sleep(1)
""".lstrip()
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        """
import subprocess
import sys
import time

subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]])
while True:
    time.sleep(1)
""".lstrip()
    )

    deadline = namespace["Deadline"](time.monotonic() + 0.5)
    with pytest.raises(namespace[error_name], match="bounded timeout"):
        namespace["_bounded_command"](
            deadline,
            [
                interpreter,
                str(parent),
                str(child),
                str(child_started),
                str(child_terminated),
            ],
            label="descendant probe",
        )
    for _ in range(40):
        if child_terminated.exists():
            break
        time.sleep(0.05)
    assert child_started.exists()
    assert child_terminated.exists()


@pytest.mark.parametrize(
    ("helper", "error_name"),
    [
        (RESTORE_HELPER, "RestoreError"),
        (ACTIVATE_HELPER, "ActivationError"),
    ],
)
@pytest.mark.parametrize("ignore_term", [False, True])
def test_exited_direct_parent_cannot_leave_an_orphan_descendant(
    tmp_path: Path,
    helper: Path,
    error_name: str,
    ignore_term: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(helper))
    interpreter = _bind_test_interpreter(namespace, monkeypatch)
    child_started = tmp_path / "orphan-started"
    child_terminated = tmp_path / "orphan-terminated"
    child = tmp_path / "orphan.py"
    child.write_text(
        """
import os
import signal
import sys
import time
from pathlib import Path

started = Path(sys.argv[1])
terminated = Path(sys.argv[2])
ignore_term = sys.argv[3] == "ignore"

def stop(_signum, _frame):
    terminated.write_text("yes")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, signal.SIG_IGN if ignore_term else stop)
started.write_text(str(os.getpid()))
while True:
    time.sleep(1)
""".lstrip()
    )
    parent = tmp_path / "short-parent.py"
    parent.write_text(
        """
import subprocess
import sys
import time
from pathlib import Path

subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]])
started = Path(sys.argv[2])
for _ in range(500):
    if started.exists():
        break
    time.sleep(0.01)
raise SystemExit(0 if started.exists() else 97)
""".lstrip()
    )

    deadline = namespace["Deadline"](time.monotonic() + 15)
    with pytest.raises(namespace[error_name], match="left a surviving child process"):
        namespace["_bounded_command"](
            deadline,
            [
                interpreter,
                str(parent),
                str(child),
                str(child_started),
                str(child_terminated),
                "ignore" if ignore_term else "handle",
            ],
            label="orphan descendant probe",
        )

    child_pid = int(child_started.read_text())
    child_exists = True
    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            child_exists = False
            break
        time.sleep(0.05)
    assert child_exists is False
    assert child_terminated.exists() is (not ignore_term)


@pytest.mark.parametrize(
    ("helper", "error_name"),
    [
        (RESTORE_HELPER, "RestoreError"),
        (ACTIVATE_HELPER, "ActivationError"),
        (CANARY_HELPER, "CanaryError"),
    ],
)
def test_operator_executable_trust_rejects_writable_file_or_parent(
    tmp_path: Path,
    helper: Path,
    error_name: str,
) -> None:
    namespace = runpy.run_path(str(helper))
    trusted_executable = namespace["_trusted_executable"]
    executable = tmp_path / "operator-tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    tmp_path.chmod(0o700)

    assert trusted_executable(str(executable)) == str(executable.resolve())

    for mode in (0o720, 0o702):
        executable.chmod(mode)
        with pytest.raises(namespace[error_name], match="is not trusted"):
            trusted_executable(str(executable))
    executable.chmod(0o700)

    try:
        for mode in (0o720, 0o702):
            tmp_path.chmod(mode)
            with pytest.raises(namespace[error_name], match="is not trusted"):
                trusted_executable(str(executable))
    finally:
        tmp_path.chmod(0o700)


def test_static_authority_contracts() -> None:
    restore = RESTORE_HELPER.read_text()
    activate = ACTIVATE_HELPER.read_text()

    assert '"--body",\n            "true"' not in restore
    assert activate.count('"--body",\n        "true"') == 1
    assert 'run", "cancel' not in restore
    assert 'run", "cancel' not in activate
    for name in (
        "EXPECTED_HOST_SHA",
        "EXPECTED_PUBLIC_RELEASE_SHA",
        "PHASE3_FINALIZED_RECEIPT",
        "PHASE3_PROOF_COMPLETE_RECEIPT",
        "PHASE2_V2_HANDOFF_RECEIPT",
        "PRODUCER_RESTORE_RESUME_RECEIPT",
    ):
        assert name in restore
    assert "palimpsest.github-producer-restore.v2" in restore
    assert "palimpsest.railway-hourly-publication-steady-state.v1" in activate
    assert "CONTROLLER_WORKFLOW_ID = 343876046" in activate
    assert "RELEASE_WORKFLOW_ID = 323903518" in activate
    assert "minute=13" in activate
    assert "_validate_artifact_receipts" in activate
    assert "provider and www release manifests differ" in activate
    assert "start_new_session=True" in restore
    assert "start_new_session=True" in activate
    assert "signal.pthread_sigmask" in restore
    assert "signal.pthread_sigmask" in activate
    assert stat.S_IMODE(RESTORE_HELPER.stat().st_mode) == 0o755
    assert stat.S_IMODE(ACTIVATE_HELPER.stat().st_mode) == 0o755


@pytest.mark.parametrize("helper", [RESTORE_HELPER, ACTIVATE_HELPER])
def test_operator_command_environment_rejects_ambient_transport_overrides(
    monkeypatch: pytest.MonkeyPatch, helper: Path
) -> None:
    namespace = runpy.run_path(str(helper))
    hostile = {
        "CURL_CA_BUNDLE": "/tmp/forged-ca",
        "SSL_CERT_FILE": "/tmp/forged-cert",
        "SSL_CERT_DIR": "/tmp/forged-dir",
        "CURL_SSL_BACKEND": "forged",
        "LD_PRELOAD": "/tmp/forged.so",
        "DYLD_INSERT_LIBRARIES": "/tmp/forged.dylib",
        "GH_CONFIG_DIR": "/tmp/forged-gh",
        "XDG_CONFIG_HOME": "/tmp/forged-xdg",
        "HTTPS_PROXY": "http://forged.invalid",
    }
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)
    gh_environment = namespace["_command_environment"]("/usr/bin/gh")
    curl_environment = namespace["_command_environment"]("/usr/bin/curl")
    assert not set(hostile).intersection(gh_environment)
    assert not set(hostile).intersection(curl_environment)
    assert gh_environment["GH_HOST"] == "github.com"
    assert "HOME" not in curl_environment


def test_hourly_arming_window_honors_real_prompt_and_snapshot_budget() -> None:
    namespace = runpy.run_path(str(ACTIVATE_HELPER))
    observed = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
    start, end = namespace["_next_arming_window"](observed)
    scheduled = start.replace(minute=13)
    assert start.minute == 9
    assert end.minute == 10 and end.second == 30
    assert scheduled - end == timedelta(minutes=2, seconds=30)
    assert namespace["PROMPT_TIMEOUT_SECONDS"] == 90
    terminal_start, terminal_end = namespace["_next_terminal_reactivation_window"](
        observed.replace(minute=13)
    )
    assert terminal_start.minute == 20
    assert terminal_end.minute == 30
    assert terminal_end - terminal_start == timedelta(minutes=10)
    next_start, _ = namespace["_next_terminal_reactivation_window"](
        observed.replace(minute=58)
    )
    assert next_start.hour == (observed.hour + 1) % 24
    assert next_start.minute == 20


@pytest.mark.parametrize(("minute", "hour_delta"), [(29, 0), (31, 1), (39, 1)])
def test_terminal_reactivation_admission_has_no_short_budget_cliff(
    minute: int, hour_delta: int
) -> None:
    namespace = runpy.run_path(str(ACTIVATE_HELPER))
    observed = datetime(2026, 8, 28, 10, minute, tzinfo=UTC)
    start, end = namespace["_next_terminal_reactivation_window"](observed)
    assert start.hour == (observed.hour + hour_delta) % 24
    assert start.minute == 20
    assert end.minute == 30


@pytest.mark.parametrize(
    ("cross", "message"),
    [
        ("arming-13", "producer freeze crossed the :13 schedule tick"),
        (
            "terminal-admission-30",
            "terminal workflow reactivation admission was missed",
        ),
        (
            "terminal-enable-cross-30",
            "terminal workflow reactivation crossed admission end",
        ),
        ("terminal-58", "terminal live proof crossed the :50 cutoff"),
    ],
)
def test_hourly_wall_clock_cutoffs_fail_closed_before_cron_collision(
    tmp_path: Path, cross: str, message: str
) -> None:
    harness = _prepare(tmp_path)
    assert _run_restore(harness).returncode == 0

    result = _run_activation(
        harness,
        typed_sha=OSINT_SHA,
        extra={"FAKE_CLOCK_CROSS": cross},
    )

    assert result.returncode != 0
    assert message in result.stderr
    state = _load_state(harness)
    assert state["gate"] == "false"
    assert state["ack_present"] is False
    assert state["controller_state"] == "active"
    assert set(state["workflow_states"].values()) == {"active"}
    assert _hourly_failure(harness)["status"] == "failed-closed"
