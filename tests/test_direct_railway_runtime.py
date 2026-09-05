"""Contracts for the direct Hetzner-to-Railway publication runtime."""

from __future__ import annotations

import hashlib
import inspect
import fcntl
import os
import json
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta

import pytest


ROOT = Path(__file__).resolve().parent.parent
PUBLISHER = ROOT / "ops" / "railway" / "palimpsest-railway-publish"
MEASUREMENT = ROOT / "ops" / "measurement" / "palimpsest-measurement-refresh"
PUBLISH_TIMER = ROOT / "ops" / "systemd" / "palimpsest-railway-publish.timer"
PUBLISH_SERVICE = ROOT / "ops" / "systemd" / "palimpsest-railway-publish.service"
ADVANCE_BASE = ROOT / "ops" / "railway" / "advance-direct-publication-base"
ROTATE_BASE = ROOT / "ops" / "railway" / "rotate-direct-publication-base"
RECONCILE = ROOT / "ops" / "railway" / "reconcile-direct-publication-candidate"


def _publisher_shell_function(name: str, next_name: str) -> str:
    source = PUBLISHER.read_text(encoding="utf-8")
    start = source.index(f"{name}() {{")
    end = source.index(f"\n{next_name}() {{", start)
    return source[start:end]


def _candidate_fixture() -> dict[str, object]:
    release = "a" * 40
    return {
        "base_sha": "b" * 40,
        "host_deployed_sha": "c" * 40,
        "input_sha256": "d" * 64,
        "message": f"palimpsest-hetzner-{release[:12]}-{'d' * 12}-{'0' * 32}",
        "news_source_tail": {
            "count": 0,
            "sha256": hashlib.sha256(b"[]\n").hexdigest(),
        },
        "predecessor": {
            "archive_path": "/var/lib/palimpsest/railway-publication/receipts/"
            + "e" * 64
            + ".json",
            "base_sha": "f" * 40,
            "deployment_id": "505bd041-4c52-4ce7-a137-dc3e4c55cacb",
            "input_sha256": "1" * 64,
            "manifest_sha256": "2" * 64,
            "receipt_sha256": "e" * 64,
            "release_sha": "3" * 40,
            "schema_version": "palimpsest.hetzner-railway-publication.v1",
            "tree_sha256": "4" * 64,
            "wire_generated_at": "2026-08-30T12:00:00Z",
        },
        "prepared_at": "2026-08-30T12:05:00Z",
        "receipt_deadline_at": "2026-08-30T12:17:00Z",
        "publication_base": {
            "kind": "verified_transition",
            "path": "/etc/palimpsest/railway-publication-base.json",
            "sha256": "5" * 64,
        },
        "release_bundle": {
            "bytes": 1,
            "metadata_path": "/var/lib/palimpsest/railway-publication/release-bundles/a.json",
            "metadata_sha256": "6" * 64,
            "path": "/var/lib/palimpsest/railway-publication/release-bundles/a.bundle",
            "sha256": "7" * 64,
        },
        "release_manifest": {
            "bytes": 1,
            "file_count": 1,
            "path": "/var/lib/palimpsest/railway-publication/release-manifests/a.json",
            "sha256": "b" * 64,
            "total_bytes": 1,
            "tree_sha256": "c" * 64,
        },
        "release_sha": release,
        "rollback_evidence": {
            "captured_at": "2026-08-30T12:04:00Z",
            "provider_manifest": {"bytes": 1, "path": "/provider", "sha256": "8" * 64},
            "public_manifest": {"bytes": 1, "path": "/public", "sha256": "8" * 64},
            "schema_version": "palimpsest.direct-publication-rollback-evidence.v1",
            "topology": {
                "bytes": 1,
                "created_at": "2026-08-30T11:00:00Z",
                "deployment_id": "505bd041-4c52-4ce7-a137-dc3e4c55cacb",
                "environment_id": "1d4d9eef-7bad-4c7b-a003-0e66fe9a8fe2",
                "image_digest": "sha256:" + "9" * 64,
                "path": "/topology",
                "project_id": "f7c86128-53a7-458a-a931-6628c6e61fb2",
                "reason": "deploy",
                "service_id": "86a6f49c-b9dc-4be8-acd1-dd180c693230",
                "sha256": "a" * 64,
            },
        },
        "schema_version": "palimpsest.direct-publication-candidate.v2",
        "status": "mutation_unresolved",
        "submission_id": "0" * 32,
        "wire_canonical_sha256": "e" * 64,
        "wire_generated_at": "2026-08-30T12:00:00Z",
    }


def _release_manifest_fixture(release_sha: str = "a" * 40) -> dict[str, object]:
    return {
        "built_at": "2026-08-30T12:03:00Z",
        "critical_files": {
            "index.html": {"bytes": 10, "sha256": "9" * 64},
            "readings/china-publication-rights-latest.json": {
                "bytes": 42,
                "sha256": "f" * 64,
            },
        },
        "deployment_source": "local-git-archive",
        "file_count": 1,
        "github_required": False,
        "schema_version": "palimpsest.railway-static-release.v1",
        "source_commit": release_sha,
        "state": "artifact_ready",
        "total_bytes": 10,
        "tree_sha256": "c" * 64,
    }


def _attestation_fixture(
    candidate: dict[str, object],
    *,
    attested_at: str = "2026-08-30T12:02:00Z",
    situation_at: str = "2026-08-30T12:01:00Z",
) -> dict[str, object]:
    wire_at = str(candidate["wire_generated_at"])
    wire_digest = str(candidate["wire_canonical_sha256"])
    return {
        "artifacts": {
            "china_situation": {
                "canonical_sha256": "1" * 64,
                "generated_at": situation_at,
                "inputs": {
                    "newswire_canonical_sha256": wire_digest,
                    "newswire_generated_at": wire_at,
                },
                "path": "readings/china-situation-latest.json",
                "schema_version": "palimpsest-china-situation.v1",
            },
            "newswire": {
                "canonical_sha256": wire_digest,
                "generated_at": wire_at,
                "path": "readings/newswire-latest.json",
                "schema_version": "palimpsest-newswire.v1",
            },
        },
        "attested_at": attested_at,
        "limitations": [
            "Metadata only; quarantined source artifacts are not republished here.",
            "No source values, observations, or per-record identifiers are included.",
            "This attestation conveys no observation or publication authority.",
            "Unavailable or restricted evidence is not a directional signal.",
        ],
        "mode": "rights-suppressed",
        "publication_allowed": False,
        "publication_sha": candidate["release_sha"],
        "rights_status": {
            "bytes": 42,
            "path": "readings/china-publication-rights-latest.json",
            "sha256": "f" * 64,
        },
        "schema_version": "palimpsest.publication-freshness-attestation.v1",
    }


def _measurement_evidence_payload(signal_id: str, order: int) -> bytes:
    return (
        json.dumps(
            {
                "measurement": signal_id,
                "order": order,
                "schema_version": "palimpsest.test-measurement.v1",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _availability_newsroom_fixture(
    namespace: dict[str, object],
    *,
    generated_at: str = "2026-08-30T12:02:00Z",
) -> dict[str, object]:
    restricted = namespace["RESTRICTED_AVAILABILITY_SIGNALS"]
    live_signals = {
        "ddti",
        "gdelt",
        "weibo-hotsearch",
        "silence-index",
        "blocklist",
        "net4people",
    }
    stories: list[dict[str, object]] = []
    for row in namespace["NEWSROOM_IDENTITY_ROWS"]:
        signal_id, slug, section, order, story_type, priority, related = row
        identity = {
            "section": section,
            "order": order,
            "type": story_type,
            "priority": priority,
            "related_signal_ids": list(related),
        }
        if signal_id in restricted:
            label = namespace["AVAILABILITY_LABELS"][signal_id]
            status = "degraded"
            claim_type = "availability"
            statement = (
                f"No current finding is published for {label} because public "
                "value publication is restricted by the active source policy."
            )
            metric = json.loads(json.dumps(namespace["NULL_METRIC"]))
            headline = f"{label}: public value unavailable"
            dek = namespace["AVAILABILITY_DEK"]
            evidence = {
                "input": {
                    "bytes": None,
                    "filename": f"{signal_id}-latest.json",
                    "sha256": None,
                },
                "source_timestamp": None,
                "url": namespace["RIGHTS_EVIDENCE_URL"],
            }
            method = dict(namespace["AVAILABILITY_METHOD"])
            limitations = list(namespace["AVAILABILITY_LIMITATIONS"])
        elif signal_id in live_signals:
            status = "live"
            claim_type = "observation"
            statement = f"A current aggregate measurement is available for {signal_id}."
            metric = {
                "denominator": {"label": "configured records", "value": 39},
                "label": f"{signal_id} public aggregate",
                "unit": "count",
                "value": order,
            }
            headline = f"{signal_id}: current aggregate measurement"
            dek = "A bounded current aggregate is available with its evidence receipt."
            evidence_raw = _measurement_evidence_payload(signal_id, order)
            evidence = {
                "input": {
                    "bytes": len(evidence_raw),
                    "filename": f"{signal_id}-latest.json",
                    "sha256": hashlib.sha256(evidence_raw).hexdigest(),
                },
                "source_timestamp": generated_at,
                "url": f"https://palimpsest.info/readings/{signal_id}-latest.json",
            }
            method = {"summary": "Deterministic aggregate measurement", "version": 1}
            limitations = ["This aggregate does not identify a cause."]
        else:
            status = "stale"
            claim_type = "availability"
            statement = (
                f"No current finding is published for {signal_id} because the "
                "source status is stale."
            )
            metric = json.loads(json.dumps(namespace["NULL_METRIC"]))
            headline = f"{signal_id}: no current finding"
            dek = (
                "The source is stale; retained values are not presented as a "
                "current result."
            )
            input_sha = hashlib.sha256(signal_id.encode()).hexdigest()
            evidence = {
                "input": {
                    "bytes": 100 + order,
                    "filename": f"{signal_id}-latest.json",
                    "sha256": input_sha,
                },
                "source_timestamp": generated_at,
                "url": f"https://palimpsest.info/readings/{signal_id}-latest.json",
            }
            method = {"summary": "Deterministic aggregate availability", "version": 1}
            limitations = [
                "Current finding withheld because freshness has expired.",
                "Unavailable evidence is not a directional signal.",
            ]
        claim_core = {
            "claim_type": claim_type,
            "metric": metric,
            "signal_id": signal_id,
            "statement": statement,
            "status": status,
        }
        stories.append(
            {
                "claim_fingerprint": "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        claim_core,
                        allow_nan=False,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "claims": [{"statement": statement, "type": claim_type}],
                "dek": dek,
                "evidence": evidence,
                "headline": headline,
                "id": f"palimpsest-news:{signal_id}",
                "limitations": limitations,
                "method": method,
                "metric": metric,
                "modified_at": generated_at,
                **identity,
                "published_at": generated_at,
                "signal_id": signal_id,
                "slug": slug,
                "status": status,
                "url": f"https://palimpsest.info/news/{slug}/",
            }
        )
    status_counts = {
        status: sum(story["status"] == status for story in stories)
        for status in ("live", "degraded", "stale", "missing", "corrupt")
    }
    sections = [
        {
            "dek": f"Evidence section for {title}.",
            "id": section_id,
            "order": order,
            "title": title,
        }
        for section_id, (order, title) in sorted(
            namespace["SECTION_IDENTITIES"].items(), key=lambda item: item[1][0]
        )
    ]
    return {
        "coverage": {
            "counts": status_counts,
            "live": status_counts["live"],
            "reporting": (
                status_counts["live"]
                + status_counts["degraded"]
                + status_counts["stale"]
            ),
            "status": "degraded",
            "total": 39,
        },
        "feed_id": "palimpsest-china-newsroom",
        "generated_at": generated_at,
        "headline": "Public evidence edition with explicit availability states",
        "method": "Deterministic aggregate-only editorial transform",
        "n_stories": 39,
        "schema_version": "palimpsest-news.v1",
        "scope": "Aggregate evidence with current results and explicit no-result records.",
        "sections": sections,
        "source": "https://palimpsest.info/readings/osint-china-latest.json",
        "source_commit": "a" * 40,
        "stories": stories,
        "title": "Palimpsest China Newsroom",
        "url": "https://palimpsest.info/news/",
    }


def _freshness_fixture(
    *,
    checked_at: datetime,
    wire_at: datetime,
    publication_at: datetime,
    release_sha: str = "a" * 40,
    tree_sha: str = "c" * 64,
) -> dict[str, object]:
    def stamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    return {
        "checked_at": stamp(checked_at),
        "clocks": {
            "publication": {
                "age_seconds": max(
                    0, int((checked_at - publication_at).total_seconds())
                ),
                "freshness_budget_seconds": 3600,
                "generated_at": stamp(publication_at),
                "status": "fresh",
            },
            "wire": {
                "age_seconds": max(0, int((checked_at - wire_at).total_seconds())),
                "freshness_budget_seconds": 1800,
                "generated_at": stamp(wire_at),
                "status": "fresh",
            },
        },
        "rights": {"mode": "rights-suppressed", "publication_allowed": False},
        "schema_version": "palimpsest.publication-freshness.v1",
        "service": "palimpsest-publication",
        "source_commit": release_sha,
        "status": "fresh",
        "tree_sha256": tree_sha,
    }


def _newsroom_measurement_evidence(
    newsroom: dict[str, object],
) -> dict[str, bytes]:
    evidence: dict[str, bytes] = {}
    for story in newsroom["stories"]:
        if story["status"] != "live" or story["claims"][0]["type"] not in {
            "finding",
            "integrity",
            "method",
            "observation",
        }:
            continue
        raw = _measurement_evidence_payload(story["signal_id"], story["order"])
        source = story["evidence"]["input"]
        assert source["bytes"] == len(raw)
        assert source["sha256"] == hashlib.sha256(raw).hexdigest()
        evidence[f"readings/{source['filename']}"] = raw
    return evidence


def _recompute_story_fingerprint(story: dict[str, object]) -> None:
    claim = story["claims"][0]
    claim_core = {
        "claim_type": claim["type"],
        "metric": story["metric"],
        "signal_id": story["signal_id"],
        "statement": claim["statement"],
        "status": story["status"],
    }
    story["claim_fingerprint"] = "sha256:" + hashlib.sha256(
        json.dumps(
            claim_core,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _newsroom_wire_fixture(newsroom: dict[str, object]) -> dict[str, object]:
    generated_at = str(newsroom["generated_at"])
    return {
        "events": [
            {
                "desk": "economy",
                "evidence_groups": [
                    {
                        "group_id": "source-a-editorial",
                        "roles": ["media"],
                        "source_ids": ["source-a"],
                    }
                ],
                "evidence_refs": [
                    {
                        "source_name": "Example Publisher",
                        "title": "Example source report",
                        "url": "https://example.com/report-a",
                    }
                ],
                "evidence_strength": "single-source",
                "event_id": "event-" + "1" * 24,
                "headline": "Example source report",
                "published_at": generated_at,
                "topics": ["economy"],
                "updated_at": generated_at,
                "url": "https://palimpsest.info/news/wire/event-"
                + "1" * 24
                + "/",
                "version_id": "eventv-" + "2" * 24,
            },
            {
                "desk": "rights",
                "evidence_groups": [
                    {
                        "group_id": "source-b-editorial",
                        "roles": ["media"],
                        "source_ids": ["source-b"],
                    },
                    {
                        "group_id": "source-c-editorial",
                        "roles": ["media"],
                        "source_ids": ["source-c"],
                    },
                ],
                "evidence_refs": [
                    {
                        "source_name": "Second Publisher",
                        "title": "Corroborated source report",
                        "url": "https://example.org/report-b",
                    }
                ],
                "evidence_strength": "multi-source",
                "event_id": "event-" + "3" * 24,
                "headline": "Corroborated source report",
                "published_at": generated_at,
                "topics": ["rights"],
                "updated_at": generated_at,
                "url": "https://palimpsest.info/news/wire/event-"
                + "3" * 24
                + "/",
                "version_id": "eventv-" + "4" * 24,
            },
        ],
        "generated_at": generated_at,
        "n_events": 2,
    }


def _newsroom_feed_fixture(newsroom: dict[str, object]) -> dict[str, bytes]:
    from scripts import build_newsroom

    wire = _newsroom_wire_fixture(newsroom)

    def pretty(value: object) -> bytes:
        return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()

    return {
        "news/feed.json": pretty(build_newsroom.build_json_feed(newsroom, wire)),
        "news/feed.xml": build_newsroom.build_rss(newsroom, wire),
        "news/instruments/feed.json": pretty(
            build_newsroom.build_json_feed(newsroom)
        ),
        "news/instruments/feed.xml": build_newsroom.build_rss(newsroom),
    }


def _source_tail_proof(feeds: dict[str, bytes]) -> dict[str, object]:
    items = json.loads(feeds["news/feed.json"])["items"][39:]
    raw = (
        json.dumps(
            items,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    return {"count": len(items), "sha256": hashlib.sha256(raw).hexdigest()}


def _denied_analysis_feed_fixture(generated_at: str) -> dict[str, bytes]:
    from scripts import build_newsroom

    json_raw = (
        json.dumps(
            build_newsroom.build_china_analysis_availability_json_feed(
                generated_at
            ),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode()
    return {
        "news/china/analysis/feed.json": json_raw,
        "news/china/analysis/feed.xml": (
            build_newsroom.build_china_analysis_availability_rss(generated_at)
        ),
    }


def _successor_pin_fixture(namespace: dict[str, object]) -> dict[str, object]:
    history = Path("/var/lib/palimpsest/railway-control/base-rotation-history")
    anchor_digest = str(namespace["INCIDENT_PIN_SHA256"])
    predecessor_pin_digest = "1" * 64
    predecessor_target = "2" * 40
    receipt_digest = "3" * 64
    manifest_digest = "4" * 64
    topology_digest = "5" * 64
    release_sha = "6" * 40
    target_sha = "b" * 40
    generation = 3
    return {
        "anchor": {
            "path": str(history / "pins" / f"{anchor_digest}.json"),
            "schema_version": namespace["PIN_SCHEMA"],
            "sha256": anchor_digest,
            "target_sha": namespace["INCIDENT_PIN_TARGET"],
        },
        "generation": generation,
        "host": {
            "canonical_head": namespace["INCIDENT_BASE"],
            "deployed_commit": namespace["INCIDENT_BASE"],
        },
        "installed": {key: "7" * 64 for key in namespace["SUCCESSOR_INSTALLED_KEYS"]},
        "live": {
            "file_count": 1,
            "provider_manifest": {
                "bytes": 2,
                "path": str(
                    history / "manifests" / "provider" / f"{manifest_digest}.json"
                ),
                "sha256": manifest_digest,
            },
            "public_manifest": {
                "bytes": 2,
                "path": str(
                    history / "manifests" / "public" / f"{manifest_digest}.json"
                ),
                "sha256": manifest_digest,
            },
            "release_sha": release_sha,
            "total_bytes": 10,
            "tree_sha256": "8" * 64,
        },
        "origins": {
            "provider": namespace["PROVIDER_ORIGIN"],
            "public": namespace["PUBLIC_ORIGIN"],
        },
        "predecessor": {
            "pin": {
                "generation": generation - 1,
                "path": str(history / "pins" / f"{predecessor_pin_digest}.json"),
                "schema_version": namespace["SUCCESSOR_PIN_SCHEMA"],
                "sha256": predecessor_pin_digest,
                "target_sha": predecessor_target,
            },
            "publication_receipt": {
                "base_sha": predecessor_target,
                "deployment_id": "505bd041-4c52-4ce7-a137-dc3e4c55cacb",
                "host_deployed_sha": namespace["INCIDENT_BASE"],
                "input_sha256": "9" * 64,
                "manifest_sha256": manifest_digest,
                "path": str(history / "receipts" / f"{receipt_digest}.json"),
                "publication_base_sha256": predecessor_pin_digest,
                "release_sha": release_sha,
                "schema_version": namespace["V2_SCHEMA"],
                "sha256": receipt_digest,
                "tree_sha256": "8" * 64,
                "wire_generated_at": "2026-08-30T12:00:00Z",
            },
        },
        "railway": {
            "created_at": "2026-08-30T12:01:00Z",
            "deployment_id": "505bd041-4c52-4ce7-a137-dc3e4c55cacb",
            "environment_id": namespace["ENVIRONMENT_ID"],
            "image_digest": "sha256:" + "a" * 64,
            "project_id": namespace["PROJECT_ID"],
            "reason": "deploy",
            "service_id": namespace["SERVICE_ID"],
            "topology": {
                "bytes": 2,
                "path": str(history / "topologies" / f"{topology_digest}.json"),
                "sha256": topology_digest,
            },
        },
        "recorded_at": "2026-08-30T12:02:00Z",
        "rotation_record_path": str(
            history / "rotations" / f"{generation}-{target_sha}-{receipt_digest}.json"
        ),
        "schema_version": namespace["SUCCESSOR_PIN_SCHEMA"],
        "status": "verified",
        "target": {"base_sha": target_sha, "public_main_sha": target_sha},
    }


def test_direct_runtimes_are_executable_and_share_the_snapshot_lock() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")
    measurement = MEASUREMENT.read_text(encoding="utf-8")

    assert os.access(PUBLISHER, os.X_OK)
    assert os.access(MEASUREMENT, os.X_OK)
    shared_lock = "/var/lib/palimpsest/railway-publication/data.lock"
    assert shared_lock in publisher
    assert shared_lock in measurement
    assert 'export PALIMPSEST_PUBLICATION_SNAPSHOT_ROOT="$checkout"' in publisher
    assert '"$PYTHON_BIN" -m scripts.event_analysis_live' in publisher
    assert '--wire "$checkout/readings/newswire-latest.json"' in publisher
    assert '--readings "$checkout/readings"' in publisher
    assert '--output "$generated_analysis"' in publisher
    assert (
        'cp -p "$generated_analysis" "$checkout/readings/event-analysis-latest.json"'
        in publisher
    )
    assert "ANALYSIS_FILE" not in publisher


def test_publisher_keeps_systemd_wx_protection_and_self_heals_origin_drift() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")
    service = (
        ROOT / "ops" / "systemd" / "palimpsest-railway-publish.service"
    ).read_text(encoding="utf-8")

    assert "MemoryDenyWriteExecute=true" in service
    assert "PALIMPSEST_RAILWAY_NODE_OPTIONS:---jitless" in publisher
    assert publisher.count('NODE_OPTIONS="$RAILWAY_NODE_OPTIONS"') == 5
    assert "prove_unchanged_live_release" in publisher
    assert "unchanged live manifests differ from the durable release anchor" in publisher
    assert "unchanged capture lacks exact durable and two-origin semantic proof" in publisher
    assert "provider_receipt_sha" not in publisher


def test_publisher_blocks_every_rotation_intent_inode_after_root_lock() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")

    canonical_intent = 'readonly ROTATION_INTENT="$CONTROL_ROOT/rotation-intent.json"'
    acquire = 'exec 9<"$LOCK_FILE"'
    lock = "if ! flock -n 9; then"
    verify = '[[ "$(stat -c \'%d:%i\' "$LOCK_FILE")" == "$lock_identity" ]]'
    barrier = '[[ ! -e "$ROTATION_INTENT" && ! -L "$ROTATION_INTENT" ]] || {'
    refusal = 'log "prepared base rotation blocks direct publication" >&2'
    recovery = "recover_abandoned_preparation"
    barrier_position = publisher.index(barrier)
    positions = [
        publisher.index(canonical_intent),
        publisher.index(acquire),
        publisher.index(lock),
        publisher.index(verify),
        barrier_position,
        publisher.index(refusal, barrier_position),
        publisher.index(recovery, barrier_position),
    ]

    assert positions == sorted(positions)
    barrier_block = publisher[
        barrier_position : publisher.index(recovery, barrier_position)
    ]
    assert "exit 1" in barrier_block
    # `-e` catches every existing regular/directory/unsafe inode, while `-L`
    # additionally catches a dangling symlink whose target does not exist.
    assert '-e "$ROTATION_INTENT"' in barrier_block
    assert '-L "$ROTATION_INTENT"' in barrier_block


def test_publisher_lock_collision_is_an_explicit_temporary_failure() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")
    start = publisher.index("if ! flock -n 9; then")
    end = publisher.index("\nfi", start) + len("\nfi")
    collision = publisher[start:end]

    assert "exit 75" in collision
    assert "exit 0" not in collision
    assert "retry this capture" in collision
    assert ">&2" in collision


def test_independent_publication_timer_coalesces_after_each_completed_run() -> None:
    timer = PUBLISH_TIMER.read_text(encoding="utf-8")

    assert "OnBootSec=2m" in timer
    assert "OnUnitInactiveSec=5m" in timer
    assert "RandomizedDelaySec=30s" in timer
    assert "OnCalendar=" not in timer
    assert "Persistent=" not in timer
    assert "Unit=palimpsest-railway-publish.service" in timer


def test_silence_index_gets_a_distinct_bounded_timeout() -> None:
    measurement = MEASUREMENT.read_text(encoding="utf-8")

    assert 'JOB_TIMEOUT="${PALIMPSEST_COLLECTOR_TIMEOUT:-12m}"' in measurement
    assert (
        'SILENCE_INDEX_TIMEOUT="${PALIMPSEST_SILENCE_INDEX_TIMEOUT:-18m}"'
        in measurement
    )
    assert 'timeout_limit="$JOB_TIMEOUT"' in measurement
    assert 'if [[ "$name" == "silence-index" ]]; then' in measurement
    assert 'timeout_limit="$SILENCE_INDEX_TIMEOUT"' in measurement
    assert 'timeout --signal=TERM --kill-after=10s "$timeout_limit" "$@"' in measurement


def test_direct_publisher_uses_a_verified_public_base_pin_without_rewriting_host_identity() -> (
    None
):
    publisher = PUBLISHER.read_text(encoding="utf-8")
    service = PUBLISH_SERVICE.read_text(encoding="utf-8")

    assert "PALIMPSEST_RAILWAY_BASE_FILE" in publisher
    assert "palimpsest.direct-publication-base-transition.v1" in publisher
    assert "palimpsest.direct-publication-base.v2" in publisher
    assert 'base_pin_kind="verified_transition"' in publisher
    assert 'base_pin_kind="verified_successor"' in publisher
    assert "verified publication transition pin is mandatory" in publisher
    assert 'merge-base --is-ancestor "$canonical_head" "$base_sha"' in publisher
    assert 'merge-base --is-ancestor "$base_sha" refs/remotes/origin/main' in publisher
    assert "root:$PUBLICATION_BASE_GROUP mode 0640" in publisher
    # Seven exact blobs remain mandatory for the v1 bootstrap pin.  Each v2
    # successor authenticates the complete fourteen-artifact runtime lane.
    assert publisher.count("validate_installed_transition_artifact \\") == 21
    for path in (
        "/usr/local/sbin/palimpsest-continuity-guard",
        "/etc/systemd/system/palimpsest-continuity-guard.service",
        "/etc/systemd/system/palimpsest-continuity-guard.timer",
        "/usr/local/sbin/palimpsest-event-analysis-live",
        "/etc/systemd/system/palimpsest-event-analysis-live.service",
        "/etc/systemd/system/palimpsest-event-analysis-live.service.d/90-railway-publish.conf",
        "/usr/local/sbin/palimpsest-railway-publish",
        "/usr/local/sbin/palimpsest-advance-direct-publication-base",
        "/usr/local/sbin/palimpsest-rotate-direct-publication-base",
        "/usr/local/sbin/palimpsest-reconcile-direct-publication-candidate",
        "/usr/local/sbin/palimpsest-direct-watchdog",
        "/etc/systemd/system/palimpsest-railway-publish.service",
        "/etc/systemd/system/palimpsest-direct-watchdog.service",
        "/etc/systemd/system/palimpsest-direct-watchdog.timer",
    ):
        assert path in publisher
    assert "host_deployed_sha" in publisher
    assert (
        "accepting exact published-ancestor receipt bound by successor lineage"
        in publisher
    )
    assert "canonical host identity differs from the publication base pin" in publisher
    assert "ReadOnlyPaths=-/etc/palimpsest/railway-publication-base.json" in service
    assert (
        "ReadOnlyPaths=-/etc/palimpsest/railway-publication-data-hold.json" in service
    )
    assert "InaccessiblePaths=-/run/docker.sock" in service


def test_successor_bridge_receipt_outputs_escape_base_reader_scope() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")
    reader_start = publisher.index("read_publication_base() {")
    reader_end = publisher.index("\n}\n\npersist_release_bundle()", reader_start)
    reader = publisher[reader_start:reader_end]
    consumer_start = publisher.index("\nread_publication_base\n", reader_end)
    consumer = publisher[consumer_start:]
    local_names = {
        name
        for line in reader.splitlines()
        if line.lstrip().startswith("local ")
        for name in line.lstrip().removeprefix("local ").split()
    }
    bridge_outputs = {
        "bridge_receipt_path",
        "bridge_receipt_sha",
        "bridge_receipt_base_sha",
        "bridge_receipt_pin_sha256",
        "bridge_receipt_host_sha",
        "bridge_receipt_release_sha",
        "bridge_receipt_input_sha256",
        "bridge_receipt_wire_generated_at",
        "bridge_receipt_manifest_sha256",
        "bridge_receipt_tree_sha256",
        "bridge_receipt_deployment_id",
    }

    assert "set -Eeuo pipefail" in publisher
    assert bridge_outputs.isdisjoint(local_names)
    for output in bridge_outputs:
        assert f'{output}="$(jq -er ' in reader
        assert f'"${output}"' in consumer


def test_generated_release_is_durably_bundled_before_any_railway_mutation() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")

    ordered = (
        'build-static-bundle.sh" "$release_sha" "$release"',
        "\nwrite_preparation_journal\n",
        "\npersist_release_manifest_anchor\n",
        "\npersist_release_bundle\n",
        "\ncapture_predecessor_rollback_evidence\n",
        'candidate_tmp="$(mktemp "$STATE_ROOT/.pending-candidate.XXXXXX")"',
        '"$RAILWAY_BIN" up --detach',
        'receipt_tmp="$(mktemp "$STATE_ROOT/.latest-success.XXXXXX")"',
    )
    positions = [publisher.index(fragment) for fragment in ordered]
    assert positions == sorted(positions)
    for fragment in (
        'git bundle create "$bundle_tmp" "$release_ref" "^$base_sha"',
        'bundle verify "$bundle_tmp"',
        '[[ ! -e "$verify_repo/objects/info/alternates" ]]',
        'rev-list --parents -n 1 "$release_ref"',
        'ln "$bundle_tmp" "$release_bundle_path"',
        "palimpsest.incremental-release-bundle.v1",
        'RECEIPT_ARCHIVE_ROOT="$STATE_ROOT/receipts"',
        'PENDING_CANDIDATE="$STATE_ROOT/pending-candidate.json"',
        'PENDING_PREPARATION="$STATE_ROOT/pending-preparation.json"',
        "palimpsest.direct-publication-preparation.v1",
        "release manifest anchor document is not closed-schema",
        "release manifest anchor contains a duplicate key",
        "palimpsest.direct-publication-rollback-evidence.v1",
        "captured exact predecessor manifests and active Railway topology",
        "recovered an abandoned pre-mutation preparation without touching Railway",
        "unresolved candidate journal blocks a second Railway mutation",
    ):
        assert fragment in publisher
    assert "clone --quiet --no-local --no-checkout" in publisher
    assert "clone --quiet --shared" not in publisher


def test_publisher_prepares_clone_before_ordered_atomic_capture() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")

    publish_lock = publisher.index("if ! flock -n 9; then")
    clone = publisher.index(
        'git clone --quiet --no-local --no-checkout "$SOURCE_REPOSITORY" "$checkout"'
    )
    newswire_lock = publisher.index('exec 7<"$NEWSWIRE_LOCK_FILE"')
    newswire_shared = publisher.index("flock -s 7", newswire_lock)
    data_lock = publisher.index('exec 8<"$DATA_LOCK_FILE"', newswire_shared)
    data_shared = publisher.index("flock -s 8", data_lock)
    latest_copy = publisher.index(
        'stage_snapshot_file "$WIRE_FILE" "$snapshot_wire"', data_shared
    )
    ledger_copy = publisher.index(
        'stage_snapshot_file "$LEDGER_FILE" "$snapshot_ledger"', latest_copy
    )
    status_copy = publisher.index(
        'stage_snapshot_file "$WIRE_STATUS_FILE" "$snapshot_status"', ledger_copy
    )
    status_binding = publisher.index(
        'status_binding="$(validate_newswire_snapshot_receipt', status_copy
    )
    data_unlock = publisher.index("flock -u 8", status_binding)
    newswire_unlock = publisher.index("flock -u 7", data_unlock)
    first_builder = publisher.index('"$PYTHON_BIN" -c', newswire_unlock)

    assert (
        publish_lock
        < clone
        < newswire_lock
        < newswire_shared
        < data_lock
        < data_shared
        < latest_copy
        < ledger_copy
        < status_copy
        < status_binding
        < data_unlock
        < newswire_unlock
        < first_builder
    )
    assert "publish -> newswire -> data" in publisher
    assert 'source="$snapshot_readings/${relative#readings/}"' in publisher
    assert (
        'cp -p "$snapshot_wire" "$checkout/readings/newswire-latest.json"' in publisher
    )
    assert (
        'cp -p "$snapshot_ledger" "$checkout/readings/newswire-versions.jsonl"'
        in publisher
    )
    assert 'source="$HOST_READINGS/${relative#readings/}"' not in publisher
    assert (
        'cp -p "$WIRE_FILE" "$checkout/readings/newswire-latest.json"' not in publisher
    )


def test_newswire_shared_capture_blocks_across_ledger_latest_pause(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "newswire.lock"
    ledger = tmp_path / "newswire-versions.jsonl"
    latest = tmp_path / "newswire-latest.json"
    status = tmp_path / "newswire-status.json"
    lock.write_bytes(b"")
    ledger.write_text('{"version":"old"}\n', encoding="utf-8")
    latest.write_text(
        '{"generated_at":"2026-08-31T17:00:00Z","version":"old"}\n',
        encoding="utf-8",
    )

    producer_paused = threading.Event()
    finish_producer = threading.Event()
    consumer_waiting = threading.Event()
    consumer_acquired = threading.Event()
    captured: dict[str, bytes] = {}
    errors: list[BaseException] = []
    latest_bytes = b'{"generated_at":"2026-08-31T18:00:00Z","version":"new"}\n'

    def producer() -> None:
        try:
            with lock.open("rb") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                ledger.write_text('{"version":"new"}\n', encoding="utf-8")
                producer_paused.set()
                assert finish_producer.wait(timeout=5)
                latest.write_bytes(latest_bytes)
                status.write_text(
                    json.dumps(
                        {
                            "attempted_at": "2026-08-31T17:59:59Z",
                            "completed_at": "2026-08-31T18:00:01Z",
                            "failure_class": None,
                            "fresh_sources": 1,
                            "output_generated_at": "2026-08-31T18:00:00Z",
                            "output_sha256": hashlib.sha256(latest_bytes).hexdigest(),
                            "schema_version": "palimpsest-evidence-wire-attempt.v1",
                            "status": "success",
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def consumer() -> None:
        try:
            assert producer_paused.wait(timeout=5)
            with lock.open("rb") as handle:
                consumer_waiting.set()
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                consumer_acquired.set()
                captured["ledger"] = ledger.read_bytes()
                captured["latest"] = latest.read_bytes()
                captured["status"] = status.read_bytes()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    producer_thread = threading.Thread(target=producer)
    consumer_thread = threading.Thread(target=consumer)
    producer_thread.start()
    assert producer_paused.wait(timeout=5)
    consumer_thread.start()
    assert consumer_waiting.wait(timeout=5)
    assert not consumer_acquired.wait(timeout=0.1)
    finish_producer.set()
    producer_thread.join(timeout=5)
    consumer_thread.join(timeout=5)

    assert not producer_thread.is_alive()
    assert not consumer_thread.is_alive()
    assert errors == []
    assert captured["ledger"] == b'{"version":"new"}\n'
    assert captured["latest"] == latest_bytes
    receipt = json.loads(captured["status"])
    assert receipt["status"] == "success"
    assert receipt["fresh_sources"] == 1
    assert receipt["output_generated_at"] == "2026-08-31T18:00:00Z"
    assert receipt["output_sha256"] == hashlib.sha256(captured["latest"]).hexdigest()


def test_publisher_status_binding_and_freshness_reserve_precede_mutation() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")
    server = (ROOT / "ops" / "railway" / "static_server.py").read_text(encoding="utf-8")

    assert "WIRE_FRESHNESS_SECONDS = 30 * 60" in server
    assert "readonly WIRE_FRESHNESS_BUDGET_SECONDS=1800" in publisher
    assert "readonly MAX_MUTATION_TO_RECEIPT_SECONDS=720" in publisher
    assert "readonly DESIRED_LIVE_MARGIN_SECONDS=300" in publisher
    assert "readonly RECEIPT_COMMIT_RESERVE_SECONDS=10" in publisher
    for contract in (
        'status.get("status") != "success"',
        'status["fresh_sources"] < 1',
        'status["output_generated_at"] != wire["generated_at"]',
        "output_sha256 != hashlib.sha256(wire_raw).hexdigest()",
        "newswire status receipt clocks are not causally ordered",
    ):
        assert contract in publisher

    fast_branch = publisher.index('if [[ "$receipt_input_sha" == "$input_sha256" ]]')
    fast_reserve = publisher.index(
        'require_pre_mutation_freshness_reserve "$wire_generated_at"', fast_branch
    )
    fast_proof = publisher.index(
        '&& prove_unchanged_live_release "$receipt_manifest_path"', fast_reserve
    )
    fast_exit = publisher.index("    exit 0", fast_proof)
    build = publisher.index('build-static-bundle.sh" "$release_sha" "$release"')
    assert fast_branch < fast_reserve < fast_proof < fast_exit < build
    assert '"$receipt_wire_generated_at" == "$wire_generated_at"' in publisher[
        fast_branch:fast_proof
    ]
    assert "provider_receipt_sha" not in publisher

    host_snapshot = publisher.index(
        'stage_snapshot_file "$WIRE_STATUS_FILE" "$snapshot_status"'
    )
    private_refresh = publisher.index(
        '"$PYTHON_BIN" -m scripts.newswire_pull', host_snapshot
    )
    target_registry = publisher.index(
        '--config "$checkout/config/news_sources.json"', private_refresh
    )
    private_output = publisher.index('--output "$snapshot_wire"', target_registry)
    snapshot_validation = publisher.index(
        'status_binding="$(validate_newswire_snapshot_receipt', private_output
    )
    assert (
        host_snapshot
        < private_refresh
        < target_registry
        < private_output
        < snapshot_validation
        < fast_branch
    )
    assert '--output "$WIRE_FILE"' not in publisher[
        private_refresh:snapshot_validation
    ]

    parallel_check = publisher.index(
        '"$PYTHON_BIN" -m scripts.build_newsroom --check \\\n'
        '  --signal-rendered "$newsroom_rendered_marker"'
    )
    mutating_render = publisher.index(
        'PALIMPSEST_EPHEMERAL_BUILD=1 \\\n'
        '  "$PYTHON_BIN" -m scripts.build_newsroom',
        parallel_check,
    )
    writer_wait = publisher.index(
        '--publish-after "$newsroom_rendered_marker"', mutating_render
    )
    barrier_release = publisher.index(
        'mv "$newsroom_check_marker_tmp" "$newsroom_check_marker"',
        mutating_render,
    )
    parallel_wait = publisher.index(
        'wait "$newsroom_check_pid"', barrier_release
    )
    assert (
        parallel_check
        < mutating_render
        < writer_wait
        < barrier_release
        < parallel_wait
        < build
    )
    bri_parallel = publisher.index(
        '(\n  set -Eeuo pipefail\n  "$PYTHON_BIN" -m scripts.build_bri_observatory'
    )
    situation_parallel = publisher.index(
        '(\n  set -Eeuo pipefail\n  "$PYTHON_BIN" -m scripts.build_china_situation',
        bri_parallel,
    )
    bri_wait = publisher.index('wait "$bri_build_pid"', situation_parallel)
    situation_wait = publisher.index(
        'wait "$situation_build_pid"', bri_wait
    )
    catalog_build = publisher.index(
        '"$PYTHON_BIN" -m scripts.build_data_catalog', situation_wait
    )
    assert bri_parallel < situation_parallel < bri_wait < situation_wait < catalog_build

    ordered = (
        'build-static-bundle.sh" "$release_sha" "$release"',
        'wire_canonical_sha256="$(validate_release_freshness_attestation',
        'require_pre_mutation_freshness_reserve "$wire_generated_at"',
        "\nwrite_preparation_journal\n",
        "\npersist_release_manifest_anchor\n",
        "\npersist_release_bundle\n",
        "\ncapture_predecessor_rollback_evidence\n",
        'candidate_tmp="$(mktemp "$STATE_ROOT/.pending-candidate.XXXXXX")"',
        'ln "$candidate_tmp" "$PENDING_CANDIDATE"',
        '"$RAILWAY_BIN" up --detach',
    )
    positions: list[int] = []
    cursor = build
    for fragment in ordered:
        position = publisher.index(fragment, cursor)
        positions.append(position)
        cursor = position + 1
    assert positions == sorted(positions)
    reserve_to_mutation = publisher[positions[2] : positions[-1]]
    assert (
        "pending-preparation"
        not in reserve_to_mutation.split("write_preparation_journal", maxsplit=1)[0]
    )
    assert (
        "pending-candidate"
        not in reserve_to_mutation.split('candidate_tmp="', maxsplit=1)[0]
    )
    assert publisher.count(
        'require_pre_mutation_freshness_reserve "$wire_generated_at"'
    ) == 3
    final_reserve = publisher.rindex(
        'require_pre_mutation_freshness_reserve "$wire_generated_at"'
    )
    assert positions[-4] < final_reserve < positions[-3]
    assert (
        'while bounded_deadline_timeout "$RAILWAY_COMMAND_TIMEOUT_SECONDS"'
        in publisher
    )
    assert "for _ in $(seq 1 120)" not in publisher
    assert "for _ in $(seq 1 36)" not in publisher


def test_publisher_uses_the_rights_gate_canonical_attestation_encoding() -> None:
    validator = _publisher_shell_function(
        "validate_release_freshness_attestation", "validate_structured_newsroom"
    )
    attestation_encoder = validator.split("canonical_attestation =", maxsplit=1)[1]

    assert "indent=2" in attestation_encoder
    assert 'separators=(",", ":")' not in attestation_encoder


def test_publisher_status_validator_rejects_unbound_latest(
    tmp_path: Path,
) -> None:
    wire = tmp_path / "newswire-latest.json"
    status = tmp_path / "newswire-status.json"
    wire_bytes = b'{"generated_at":"2026-08-31T18:00:00Z"}\n'
    wire.write_bytes(wire_bytes)
    receipt = {
        "attempted_at": "2026-08-31T17:59:59Z",
        "completed_at": "2026-08-31T18:00:01Z",
        "failure_class": None,
        "fresh_sources": 2,
        "output_generated_at": "2026-08-31T18:00:00Z",
        "output_sha256": hashlib.sha256(wire_bytes).hexdigest(),
        "schema_version": "palimpsest-evidence-wire-attempt.v1",
        "status": "success",
    }
    status.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    function = _publisher_shell_function(
        "validate_newswire_snapshot_receipt",
        "require_pre_mutation_freshness_reserve",
    )

    def invoke() -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            (
                "set -Eeuo pipefail",
                f"PYTHON_BIN={shlex.quote(sys.executable)}",
                function,
                "validate_newswire_snapshot_receipt "
                f"{shlex.quote(str(status))} {shlex.quote(str(wire))} "
                "2026-08-31T18:00:02Z",
            )
        )
        return subprocess.run(
            ["/bin/bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

    valid = invoke()
    assert valid.returncode == 0, valid.stderr
    assert valid.stdout == "2026-08-31T18:00:00Z\t2026-08-31T18:00:01Z\t2\n"

    receipt["output_sha256"] = "0" * 64
    status.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    forged = invoke()
    assert forged.returncode != 0
    assert "digest does not bind" in forged.stderr


def test_stale_reserve_short_circuits_all_publication_mutations(
    tmp_path: Path,
) -> None:
    function = _publisher_shell_function(
        "require_pre_mutation_freshness_reserve", "validate_live_freshness_proofs"
    )
    preparation = tmp_path / "preparation"
    candidate = tmp_path / "candidate"
    railway = tmp_path / "railway"
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"PYTHON_BIN={shlex.quote(sys.executable)}",
            "WIRE_FRESHNESS_BUDGET_SECONDS=1800",
            "MAX_MUTATION_TO_RECEIPT_SECONDS=720",
            "DESIRED_LIVE_MARGIN_SECONDS=300",
            "log() { :; }",
            function,
            'require_pre_mutation_freshness_reserve "2000-01-01T00:00:00Z"',
            f"touch {shlex.quote(str(preparation))}",
            f"touch {shlex.quote(str(candidate))}",
            f"touch {shlex.quote(str(railway))}",
        )
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "mutation-to-receipt bound" in result.stderr
    assert not preparation.exists()
    assert not candidate.exists()
    assert not railway.exists()


def test_mutation_deadline_is_absolute_and_never_exceeds_declared_bound() -> None:
    function = _publisher_shell_function(
        "require_pre_mutation_freshness_reserve", "validate_live_freshness_proofs"
    )
    wire_clock = datetime.now(UTC).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    before = int(datetime.now(UTC).timestamp())
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"PYTHON_BIN={shlex.quote(sys.executable)}",
            "WIRE_FRESHNESS_BUDGET_SECONDS=1800",
            "MAX_MUTATION_TO_RECEIPT_SECONDS=720",
            "DESIRED_LIVE_MARGIN_SECONDS=300",
            "mutation_proof_deadline_epoch=0",
            "log() { :; }",
            function,
            f'require_pre_mutation_freshness_reserve "{wire_clock}"',
            'printf "%s\\n" "$mutation_proof_deadline_epoch"',
        )
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    after = int(datetime.now(UTC).timestamp())

    assert result.returncode == 0, result.stderr
    deadline = int(result.stdout.strip())
    assert before + 715 <= deadline <= after + 720


def test_latest_success_waits_for_exact_two_origin_freshness_and_attestation_proof() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")

    provider_freshness = publisher.index(
        '"$PROVIDER_ORIGIN/freshness?receipt=$freshness_nonce"'
    )
    public_freshness = publisher.index(
        '"$PUBLIC_ORIGIN/freshness?receipt=$freshness_nonce"', provider_freshness
    )
    http_200 = publisher.index(
        '"$provider_freshness_http" == 200 && "$public_freshness_http" == 200',
        public_freshness,
    )
    provider_attestation = publisher.index(
        '"$PROVIDER_ORIGIN/readings/publication-freshness-attestation-latest.json?receipt=$freshness_nonce"',
        http_200,
    )
    public_attestation = publisher.index(
        '"$PUBLIC_ORIGIN/readings/publication-freshness-attestation-latest.json?receipt=$freshness_nonce"',
        provider_attestation,
    )
    attestation_identity = publisher.index(
        'cmp -s "$provider_live_attestation" "$release_freshness_attestation"',
        public_attestation,
    )
    wire_identity = publisher.index(
        'provider_wire_canonical_sha256="$(validate_release_freshness_attestation',
        attestation_identity,
    )
    freshness_identity = publisher.index(
        'validate_live_freshness_proofs "$provider_freshness" "$public_freshness"',
        wire_identity,
    )
    final_topology = publisher.index(
        'candidate_final_topology="$work_root/candidate-final-railway-status.json"',
        freshness_identity,
    )
    receipt = publisher.index(
        'receipt_tmp="$(mktemp "$STATE_ROOT/.latest-success.XXXXXX")"',
        final_topology,
    )

    assert (
        provider_freshness
        < public_freshness
        < http_200
        < provider_attestation
        < public_attestation
        < attestation_identity
        < wire_identity
        < freshness_identity
        < final_topology
        < receipt
    )
    temp_fsync = publisher.index(
        'fsync_paths_and_directory "$receipt_tmp" "$STATE_ROOT"', receipt
    )
    reserve_check = publisher.index(
        'bounded_deadline_timeout 1 "$RECEIPT_COMMIT_RESERVE_SECONDS"',
        temp_fsync,
    )
    promote = publisher.index(
        'mv -f "$receipt_tmp" "$SUCCESS_RECEIPT"', reserve_check
    )
    canonical_fsync = publisher.index(
        'fsync_paths_and_directory "$SUCCESS_RECEIPT" "$STATE_ROOT"', promote
    )
    post_durability_deadline = publisher.index(
        "if ! bounded_deadline_timeout 1 0", canonical_fsync
    )
    restore_log = publisher.index(
        "success receipt became durable after its absolute deadline; restoring "
        "predecessor receipt and leaving the candidate unresolved",
        post_durability_deadline,
    )
    archive_authentication = publisher.index(
        '"$(sha256_file "$receipt_archive_path")" == "$receipt_sha256"',
        restore_log,
    )
    restore_copy = publisher.index(
        'cp -- "$receipt_archive_path" "$receipt_tmp"', archive_authentication
    )
    restore_temp_fsync = publisher.index(
        'fsync_paths_and_directory "$receipt_tmp" "$STATE_ROOT"', restore_copy
    )
    restore_promote = publisher.index(
        'mv -f "$receipt_tmp" "$SUCCESS_RECEIPT"', restore_temp_fsync
    )
    restore_canonical_fsync = publisher.index(
        'fsync_paths_and_directory "$SUCCESS_RECEIPT" "$STATE_ROOT"',
        restore_promote,
    )
    failed_restore_branch = publisher.index("  exit 1\nfi", restore_canonical_fsync)
    candidate_identity = publisher.index(
        '"$(sha256_file "$PENDING_CANDIDATE")" == "$candidate_journal_sha256"',
        failed_restore_branch,
    )
    candidate_consumption = publisher.index(
        'unlink "$PENDING_CANDIDATE"', candidate_identity
    )
    assert (
        receipt
        < temp_fsync
        < reserve_check
        < promote
        < canonical_fsync
        < post_durability_deadline
        < restore_log
        < archive_authentication
        < restore_copy
        < restore_temp_fsync
        < restore_promote
        < restore_canonical_fsync
        < failed_restore_branch
        < candidate_identity
        < candidate_consumption
    )
    assert "readings/newswire-latest.json?receipt=" not in publisher
    for contract in (
        'proof.get("source_commit") != expected_release',
        'proof.get("tree_sha256") != expected_tree',
        '("wire", expected_wire, 1800)',
        '("publication", expected_publication, 3600)',
        'row.get("status") != "fresh"',
    ):
        assert contract in publisher


def test_post_deploy_newsroom_smoke_uses_snapshot_and_availability_contracts() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")
    reconciler = RECONCILE.read_text(encoding="utf-8")
    manifest_builder = (
        ROOT / "ops" / "railway" / "build_release_manifest.py"
    ).read_text(encoding="utf-8")
    assert '<strong>Edition generated</strong>' in publisher
    assert "<strong>31</strong> measurements" not in publisher
    assert "availability_ids != RESTRICTED" not in publisher
    assert "newsroom_measurement_count" in publisher
    assert "newsroom_availability_count" in publisher
    assert "validate_newsroom_html" in publisher
    assert "strong_counts != [expected]" in publisher
    assert "plain_counts != [expected]" in publisher
    assert "newsroom HTML falsely labels snapshot measurements" in publisher
    assert "The Board · Current evidence" not in publisher
    assert "measurements available when built" not in publisher
    assert "measurements live' <<<\"$news_html\" ||" not in publisher
    provider_newsroom = publisher.index(
        '"$PROVIDER_ORIGIN/readings/newsroom-latest.json?receipt=$freshness_nonce"'
    )
    public_newsroom = publisher.index(
        '"$PUBLIC_ORIGIN/readings/newsroom-latest.json?receipt=$freshness_nonce"',
        provider_newsroom,
    )
    provider_binding = publisher.index(
        'cmp -s "$provider_live_newsroom" "$release/readings/newsroom-latest.json"',
        public_newsroom,
    )
    public_binding = publisher.index(
        'cmp -s "$public_live_newsroom" "$release/readings/newsroom-latest.json"',
        provider_binding,
    )
    structured_validation = publisher.index(
        '"$PYTHON_BIN" -I -S - "$public_live_newsroom"', public_binding
    )
    assert (
        provider_newsroom
        < public_newsroom
        < provider_binding
        < public_binding
        < structured_validation
    )
    assert "provider or public structured newsroom differs" in publisher
    assert '"readings/newsroom-latest.json"' in manifest_builder
    assert '"news/index.html"' in manifest_builder
    assert '"board-alarm"' in publisher
    assert "live structured newsroom has an unsafe availability record" in publisher
    provider_html = publisher.index(
        '"$PROVIDER_ORIGIN/news/?receipt=$freshness_nonce"'
    )
    public_html = publisher.index(
        '"$PUBLIC_ORIGIN/news/?receipt=$freshness_nonce"', provider_html
    )
    html_binding = publisher.index(
        'cmp -s "$provider_live_news_html" "$release_news_html"', public_html
    )
    html_manifest_binding = publisher.index(
        '"$(sha256_file "$provider_live_news_html")" == "$news_html_manifest_sha256"',
        html_binding,
    )
    assert provider_html < public_html < html_binding < html_manifest_binding

    assert '<strong>Edition generated</strong>' in reconciler
    assert "<strong>31</strong> measurements" not in reconciler
    assert "availability_ids != RESTRICTED_AVAILABILITY_SIGNALS" not in reconciler
    assert "strong_counts" in reconciler
    assert "plain_counts" in reconciler
    assert "_validate_live_newsroom_html_proofs" in reconciler
    assert "_newsroom_story_kind" in reconciler
    assert "The Board · Current evidence" not in reconciler
    assert "measurements available when built" not in reconciler
    assert "provider and public structured newsrooms differ" in reconciler
    assert "require_current_freshness" not in reconciler
    assert "inverse durable-commit marker" in reconciler


def test_publisher_newsroom_clock_is_causally_bound_to_wire_and_manifest(
    tmp_path: Path,
) -> None:
    function = _publisher_shell_function(
        "validate_structured_newsroom", "validate_installed_transition_artifact"
    )
    namespace = runpy.run_path(str(RECONCILE))
    newsroom = _availability_newsroom_fixture(namespace)
    for story in newsroom["stories"]:
        signal_id = story["signal_id"]
        if signal_id not in namespace["RESTRICTED_AVAILABILITY_SIGNALS"]:
            continue
        claim_core = {
            "claim_type": "availability",
            "metric": story["metric"],
            "signal_id": signal_id,
            "statement": story["claims"][0]["statement"],
            "status": "degraded",
        }
        story["claim_fingerprint"] = "sha256:" + hashlib.sha256(
            json.dumps(
                claim_core,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        story["method"] = {
            "summary": (
                "Availability-only public projection under the active China "
                "economic source policy"
            ),
            "version": 1,
        }
        story["limitations"] = [
            "Current finding withheld: public value publication is restricted",
            "Availability is not a zero, a normal reading, or evidence of direction.",
        ]
    path = tmp_path / "newsroom.json"

    def invoke(generated_at: str) -> subprocess.CompletedProcess[str]:
        newsroom["generated_at"] = generated_at
        for story in newsroom["stories"]:
            story["published_at"] = generated_at
            story["modified_at"] = generated_at
            if story["evidence"]["source_timestamp"] is not None:
                story["evidence"]["source_timestamp"] = generated_at
        path.write_text(
            json.dumps(newsroom, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        script = "\n".join(
            (
                "set -Eeuo pipefail",
                f"PYTHON_BIN={shlex.quote(sys.executable)}",
                function,
                "validate_structured_newsroom "
                f"{shlex.quote(str(path))} "
                "2026-08-30T12:00:00Z 2026-08-30T12:03:00Z",
            )
        )
        return subprocess.run(
            ["/bin/bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

    for boundary in ("2026-08-30T12:00:00Z", "2026-08-30T12:03:00Z"):
        accepted = invoke(boundary)
        assert accepted.returncode == 0, accepted.stderr
        assert accepted.stdout.strip() == "6\t33"

    for impossible_clock in ("2020-01-01T00:00:00Z", "2026-08-30T12:03:01Z"):
        rejected = invoke(impossible_clock)
        assert rejected.returncode != 0
        assert "clocks are not causally ordered" in rejected.stderr

    rejected = invoke("2026-08-30T12:02:00.500Z")
    assert rejected.returncode != 0
    assert "not a strict UTC whole-second clock" in rejected.stderr

    newsroom["unexpected_top_level"] = "shadow payload"
    rejected = invoke("2026-08-30T12:02:00Z")
    assert rejected.returncode != 0
    assert "identity is invalid" in rejected.stderr
    newsroom.pop("unexpected_top_level")

    board = next(
        story for story in newsroom["stories"] if story["signal_id"] == "board-alarm"
    )
    board["leaked_value"] = 987654.321
    rejected = invoke("2026-08-30T12:02:00Z")
    assert rejected.returncode != 0
    assert "story identity is invalid: board-alarm" in rejected.stderr
    board.pop("leaked_value")

    for field, forged, expected in (
        ("claim_fingerprint", "sha256:" + "f" * 64, "fingerprint is invalid"),
        (
            "method",
            {"summary": "FDR007 latest reading 987654.321", "version": 1},
            "unsafe rights availability record",
        ),
        (
            "limitations",
            ["FDR007 latest reading 987654.321"],
            "availability limitations are invalid",
        ),
    ):
        original = board[field]
        board[field] = forged
        rejected = invoke("2026-08-30T12:02:00Z")
        assert rejected.returncode != 0
        assert f"{expected}: board-alarm" in rejected.stderr
        board[field] = original

    measurement = next(
        story
        for story in newsroom["stories"]
        if story["signal_id"] == "ddti"
    )
    for field, forged in (
        ("unit", None),
        ("denominator", {"label": None, "value": -1}),
    ):
        original = measurement["metric"][field]
        measurement["metric"][field] = forged
        _recompute_story_fingerprint(measurement)
        rejected = invoke("2026-08-30T12:02:00Z")
        assert rejected.returncode != 0
        assert "ambiguous result semantics: ddti" in rejected.stderr
        measurement["metric"][field] = original
        _recompute_story_fingerprint(measurement)

    original_version = measurement["method"]["version"]
    measurement["method"]["version"] = 0
    rejected = invoke("2026-08-30T12:02:00Z")
    assert rejected.returncode != 0
    assert "method version is invalid: ddti" in rejected.stderr
    measurement["method"]["version"] = original_version

    original_sha = measurement["evidence"]["input"]["sha256"]
    measurement["evidence"]["input"]["sha256"] = "0" * 63
    rejected = invoke("2026-08-30T12:02:00Z")
    assert rejected.returncode != 0
    assert "story evidence is invalid: ddti" in rejected.stderr
    measurement["evidence"]["input"]["sha256"] = original_sha

    original_evidence = json.loads(json.dumps(measurement["evidence"]))
    measurement["evidence"]["input"]["sha256"] = None
    measurement["evidence"]["input"]["bytes"] = None
    measurement["evidence"]["source_timestamp"] = None
    rejected = invoke("2026-08-30T12:02:00Z")
    assert rejected.returncode != 0
    assert "measurement evidence is invalid: ddti" in rejected.stderr
    measurement["evidence"] = json.loads(json.dumps(original_evidence))

    measurement["evidence"]["url"] = (
        "https://palimpsest.info/readings/wrong-latest.json"
    )
    rejected = invoke("2026-08-30T12:02:00Z")
    assert rejected.returncode != 0
    assert "story evidence is invalid: ddti" in rejected.stderr
    measurement["evidence"] = json.loads(json.dumps(original_evidence))

    ordinary_availability = next(
        story
        for story in newsroom["stories"]
        if story["signal_id"] == "public-deletion-ledgers"
    )
    ordinary_availability["limitations"].append(
        "Public value publication is restricted by policy."
    )
    rejected = invoke("2026-08-30T12:02:00Z")
    assert rejected.returncode != 0
    assert "ordinary availability carries a rights-only marker" in rejected.stderr
    ordinary_availability["limitations"].pop()


def test_publisher_newsroom_html_validator_rejects_any_contradictory_count(
    tmp_path: Path,
) -> None:
    function = _publisher_shell_function(
        "validate_newsroom_html", "validate_installed_transition_artifact"
    )
    path = tmp_path / "news.html"

    def invoke(payload: bytes) -> subprocess.CompletedProcess[str]:
        path.write_bytes(payload)
        script = "\n".join(
            (
                "set -Eeuo pipefail",
                f"PYTHON_BIN={shlex.quote(sys.executable)}",
                function,
                "validate_newsroom_html "
                f"{shlex.quote(str(path))} 6 33",
            )
        )
        return subprocess.run(
            ["/bin/bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

    valid = (
        b"<strong>Edition generated</strong>"
        b"6 measurements \xc2\xb7 33 availability notices \xc2\xb7 source records"
        b"<strong>6</strong> measurements \xc2\xb7 <strong>33</strong> "
        b"availability notices in this edition"
    )
    accepted = invoke(valid)
    assert accepted.returncode == 0, accepted.stderr

    contradictory = valid.replace(
        b"6 measurements \xc2\xb7 33 availability notices \xc2\xb7 source records",
        b"7 measurements \xc2\xb7 32 availability notices \xc2\xb7 source records"
        b"6 measurements \xc2\xb7 33 availability notices \xc2\xb7 hidden proof",
    )
    rejected = invoke(contradictory)
    assert rejected.returncode != 0
    assert "not exactly bound" in rejected.stderr


def test_publisher_pre_mutation_binds_every_measurement_evidence_file(
    tmp_path: Path,
) -> None:
    source = PUBLISHER.read_text(encoding="utf-8")
    measurement_functions = source[
        source.index("measurement_evidence_inventory() {") : source.index(
            "\nvalidate_newsroom_feeds() {"
        )
    ]
    manifest_functions = source[
        source.index("manifest_artifact_identity() {") : source.index(
            "\nfetch_live_artifact_pair() {"
        )
    ]
    namespace = runpy.run_path(str(RECONCILE))
    newsroom = _availability_newsroom_fixture(namespace)
    live_stories = [story for story in newsroom["stories"] if story["status"] == "live"]
    for story, claim_type in zip(
        live_stories,
        ("finding", "integrity", "method", "observation", "finding", "integrity"),
        strict=True,
    ):
        story["claims"][0]["type"] = claim_type
    newsroom_path = tmp_path / "readings" / "newsroom-latest.json"
    newsroom_path.parent.mkdir()
    newsroom_path.write_bytes(namespace["_canonical"](newsroom))
    evidence = _newsroom_measurement_evidence(newsroom)
    critical: dict[str, object] = {}
    for relative, raw in evidence.items():
        path = tmp_path / relative
        path.write_bytes(raw)
        critical[relative] = {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    manifest_path = tmp_path / "railway-release.json"

    def invoke() -> subprocess.CompletedProcess[str]:
        manifest_path.write_text(
            json.dumps({"critical_files": critical}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        script = "\n".join(
            (
                "set -Eeuo pipefail",
                f"PYTHON_BIN={shlex.quote(sys.executable)}",
                "log() { printf '%s\\n' \"$*\" >&2; }",
                "sha256_file() { shasum -a 256 \"$1\" | awk '{print $1}'; }",
                "stat() { if [[ \"$1\" == '-c' && \"$2\" == '%s' ]]; then command stat -f '%z' \"$3\"; else command stat \"$@\"; fi; }",
                measurement_functions,
                manifest_functions,
                "validate_measurement_evidence_local "
                f"{shlex.quote(str(newsroom_path))} "
                f"{shlex.quote(str(manifest_path))} "
                f"{shlex.quote(str(tmp_path))} 6",
            )
        )
        return subprocess.run(
            ["/bin/bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

    accepted = invoke()
    assert accepted.returncode == 0, accepted.stderr

    first_relative = next(iter(evidence))
    (tmp_path / first_relative).write_bytes(evidence[first_relative] + b" ")
    rejected = invoke()
    assert rejected.returncode != 0
    assert "not byte-bound to the release manifest" in rejected.stderr

    (tmp_path / first_relative).write_bytes(evidence[first_relative])
    first_identity = critical.pop(first_relative)
    rejected = invoke()
    assert rejected.returncode != 0
    assert "absent from the release manifest" in rejected.stderr

    critical[first_relative] = first_identity
    critical[first_relative]["sha256"] = "f" * 64
    rejected = invoke()
    assert rejected.returncode != 0
    assert "differs from its newsroom evidence claim" in rejected.stderr


def test_publisher_newsroom_feeds_bind_every_story_and_source_tail(
    tmp_path: Path,
) -> None:
    function = _publisher_shell_function(
        "validate_newsroom_feeds", "validate_newsroom_html"
    )
    namespace = runpy.run_path(str(RECONCILE))
    newsroom = _availability_newsroom_fixture(namespace)
    feeds = _newsroom_feed_fixture(newsroom)
    newsroom_path = tmp_path / "newsroom.json"
    newsroom_path.write_bytes(namespace["_canonical"](newsroom))
    wire_path = tmp_path / "private-wire.json"
    wire_path.write_bytes(namespace["_canonical"](_newsroom_wire_fixture(newsroom)))

    def invoke(payloads: dict[str, bytes]) -> subprocess.CompletedProcess[str]:
        paths: dict[str, Path] = {}
        for relative, raw in payloads.items():
            path = tmp_path / relative.replace("/", "-")
            path.write_bytes(raw)
            paths[relative] = path
        script = "\n".join(
            (
                "set -Eeuo pipefail",
                f"PYTHON_BIN={shlex.quote(sys.executable)}",
                function,
                "validate_newsroom_feeds "
                f"{shlex.quote(str(newsroom_path))} "
                f"{shlex.quote(str(paths['news/feed.json']))} "
                f"{shlex.quote(str(paths['news/feed.xml']))} "
                f"{shlex.quote(str(paths['news/instruments/feed.json']))} "
                f"{shlex.quote(str(paths['news/instruments/feed.xml']))} "
                f"{shlex.quote(str(wire_path))}",
            )
        )
        return subprocess.run(
            ["/bin/bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

    accepted = invoke(feeds)
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout == (
        f"2\t{_source_tail_proof(feeds)['sha256']}\n"
    )

    reordered = json.loads(feeds["news/instruments/feed.json"])
    reordered["items"][0], reordered["items"][1] = (
        reordered["items"][1],
        reordered["items"][0],
    )
    forged_order = dict(feeds)
    forged_order["news/instruments/feed.json"] = (
        json.dumps(reordered, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    rejected = invoke(forged_order)
    assert rejected.returncode != 0
    assert "exact newsroom instrument prefix" in rejected.stderr

    forged_tail = dict(feeds)
    forged_tail["news/feed.xml"] = feeds["news/feed.xml"].replace(
        b"event-" + b"1" * 24,
        b"event-" + b"9" * 24,
        1,
    )
    rejected = invoke(forged_tail)
    assert rejected.returncode != 0
    assert "cross-bound between JSON and RSS" in rejected.stderr

    forged_source_json = dict(feeds)
    combined = json.loads(feeds["news/feed.json"])
    combined["items"][39]["summary"] = "Unbound cached source summary."
    forged_source_json["news/feed.json"] = (
        json.dumps(combined, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    rejected = invoke(forged_source_json)
    assert rejected.returncode != 0
    assert "differs from the private wire projection" in rejected.stderr

    from scripts import build_newsroom

    forged_wire = _newsroom_wire_fixture(newsroom)
    forged_wire["events"][0]["headline"] = "Consistently forged source report"
    consistently_forged = dict(feeds)
    consistently_forged["news/feed.json"] = (
        json.dumps(
            build_newsroom.build_json_feed(newsroom, forged_wire),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode()
    consistently_forged["news/feed.xml"] = build_newsroom.build_rss(
        newsroom, forged_wire
    )
    rejected = invoke(consistently_forged)
    assert rejected.returncode != 0
    assert "differs from the private wire projection" in rejected.stderr


def test_publisher_denied_china_analysis_feeds_are_exact_availability(
    tmp_path: Path,
) -> None:
    function = _publisher_shell_function(
        "validate_denied_china_analysis_feeds", "validate_newsroom_html"
    )
    generated_at = "2026-08-30T12:02:00Z"
    feeds = _denied_analysis_feed_fixture(generated_at)
    json_path = tmp_path / "feed.json"
    rss_path = tmp_path / "feed.xml"

    def invoke(payloads: dict[str, bytes]) -> subprocess.CompletedProcess[str]:
        json_path.write_bytes(payloads["news/china/analysis/feed.json"])
        rss_path.write_bytes(payloads["news/china/analysis/feed.xml"])
        script = "\n".join(
            (
                "set -Eeuo pipefail",
                f"PYTHON_BIN={shlex.quote(sys.executable)}",
                function,
                "validate_denied_china_analysis_feeds "
                f"{shlex.quote(str(json_path))} {shlex.quote(str(rss_path))} "
                f"{generated_at}",
            )
        )
        return subprocess.run(
            ["/bin/bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

    accepted = invoke(feeds)
    assert accepted.returncode == 0, accepted.stderr

    forged = dict(feeds)
    document = json.loads(forged["news/china/analysis/feed.json"])
    document["items"][0]["_palimpsest"]["finding_state"] = "confirmed"
    forged["news/china/analysis/feed.json"] = (
        json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    rejected = invoke(forged)
    assert rejected.returncode != 0
    assert "not canonical availability" in rejected.stderr

    generated_at = "2026-08-30T12:03:00Z"
    rejected = invoke(feeds)
    assert rejected.returncode != 0
    assert "not canonical availability" in rejected.stderr


def test_unchanged_release_rejects_same_sha_with_mismatched_static_artifact(
    tmp_path: Path,
) -> None:
    function = _publisher_shell_function(
        "manifest_artifact_identity", "fetch_live_artifact_pair"
    )
    expected = b'{"edition":"current"}\n'
    stale = b'{"edition":"cached"}\n'
    manifest = tmp_path / "railway-release.json"
    provider = tmp_path / "provider-feed.json"
    public = tmp_path / "public-feed.json"
    manifest.write_text(
        json.dumps(
            {
                "critical_files": {
                    "news/feed.json": {
                        "bytes": len(expected),
                        "sha256": hashlib.sha256(expected).hexdigest(),
                    }
                },
                "source_commit": "a" * 40,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def invoke() -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            (
                "set -Eeuo pipefail",
                "log() { printf '%s\\n' \"$*\" >&2; }",
                "sha256_file() { shasum -a 256 \"$1\" | awk '{print $1}'; }",
                "stat() { command stat -f '%z' \"${@: -1}\"; }",
                function,
                "validate_manifest_bound_pair "
                f"{shlex.quote(str(manifest))} news/feed.json "
                f"{shlex.quote(str(provider))} {shlex.quote(str(public))}",
            )
        )
        return subprocess.run(
            ["/bin/bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

    provider.write_bytes(expected)
    public.write_bytes(expected)
    accepted = invoke()
    assert accepted.returncode == 0, accepted.stderr

    provider.write_bytes(stale)
    public.write_bytes(stale)
    rejected = invoke()
    assert rejected.returncode != 0
    assert "not byte-bound to the release manifest" in rejected.stderr

    provider.write_bytes(expected)
    public.write_bytes(stale)
    rejected = invoke()
    assert rejected.returncode != 0
    assert "not byte-identical" in rejected.stderr


def test_unchanged_release_accepts_canonical_local_git_archive_manifest(
    tmp_path: Path,
) -> None:
    function = _publisher_shell_function(
        "validate_durable_release_manifest", "prove_unchanged_live_release"
    )
    release = "a" * 40
    tree = "b" * 64
    manifest_root = tmp_path / "release-manifests"
    manifest_root.mkdir()
    manifest_path = manifest_root / f"{release}.json"
    manifest = {
        "built_at": "2026-08-30T12:03:00Z",
        "critical_files": {},
        "deployment_source": "local-git-archive",
        "file_count": 1,
        "github_required": False,
        "schema_version": "palimpsest.railway-static-release.v1",
        "source_commit": release,
        "state": "artifact_ready",
        "total_bytes": 1,
        "tree_sha256": tree,
    }

    def invoke() -> subprocess.CompletedProcess[str]:
        raw = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        manifest_path.write_bytes(raw)
        manifest_path.chmod(0o600)
        digest = hashlib.sha256(raw).hexdigest()
        script = "\n".join(
            (
                "set -Eeuo pipefail",
                f"RELEASE_MANIFEST_ROOT={shlex.quote(str(manifest_root))}",
                "log() { printf '%s\\n' \"$*\" >&2; }",
                "sha256_file() { shasum -a 256 \"$1\" | awk '{print $1}'; }",
                "stat() { if [[ \"$2\" == '%a' ]]; then command stat -f '%Lp' \"${@: -1}\"; else command stat \"$@\"; fi; }",
                function,
                "validate_durable_release_manifest "
                f"{shlex.quote(str(manifest_path))} {release} {tree} {digest}",
            )
        )
        return subprocess.run(
            ["/bin/bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

    accepted = invoke()
    assert accepted.returncode == 0, accepted.stderr

    manifest["deployment_source"] = "hetzner_direct"
    rejected = invoke()
    assert rejected.returncode != 0
    assert "invalid identity" in rejected.stderr


def test_two_origin_freshness_validator_binds_release_tree_and_wire(
    tmp_path: Path,
) -> None:
    function = _publisher_shell_function(
        "validate_live_freshness_proofs", "validate_installed_transition_artifact"
    )
    checked = datetime.now(UTC).replace(microsecond=0)
    wire_at = checked - timedelta(seconds=60)
    publication_at = checked - timedelta(seconds=30)
    release = "a" * 40
    tree = "b" * 64

    def stamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    proof = {
        "checked_at": stamp(checked),
        "clocks": {
            "publication": {
                "age_seconds": 30,
                "freshness_budget_seconds": 3600,
                "generated_at": stamp(publication_at),
                "status": "fresh",
            },
            "wire": {
                "age_seconds": 60,
                "freshness_budget_seconds": 1800,
                "generated_at": stamp(wire_at),
                "status": "fresh",
            },
        },
        "rights": {"mode": "rights-suppressed", "publication_allowed": False},
        "schema_version": "palimpsest.publication-freshness.v1",
        "service": "palimpsest-publication",
        "source_commit": release,
        "status": "fresh",
        "tree_sha256": tree,
    }
    provider = tmp_path / "provider.json"
    public = tmp_path / "public.json"
    manifest = tmp_path / "railway-release.json"
    provider.write_text(json.dumps(proof) + "\n", encoding="utf-8")
    public.write_text(json.dumps(proof) + "\n", encoding="utf-8")
    manifest.write_text(
        json.dumps({"built_at": stamp(publication_at)}) + "\n", encoding="utf-8"
    )

    def invoke() -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            (
                "set -Eeuo pipefail",
                f"PYTHON_BIN={shlex.quote(sys.executable)}",
                function,
                "validate_live_freshness_proofs "
                f"{shlex.quote(str(provider))} {shlex.quote(str(public))} "
                f"{shlex.quote(str(manifest))} {release} {tree} {stamp(wire_at)}",
            )
        )
        return subprocess.run(
            ["/bin/bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

    valid = invoke()
    assert valid.returncode == 0, valid.stderr

    forged = json.loads(json.dumps(proof))
    forged["clocks"]["wire"]["generated_at"] = stamp(wire_at - timedelta(seconds=1))
    public.write_text(json.dumps(forged) + "\n", encoding="utf-8")
    rejected = invoke()
    assert rejected.returncode != 0
    assert "wire freshness proof is not live and bound" in rejected.stderr

    cached = json.loads(json.dumps(proof))
    cached_checked = checked - timedelta(minutes=5)
    cached["checked_at"] = stamp(cached_checked)
    cached["clocks"]["publication"]["age_seconds"] = max(
        0, int((cached_checked - publication_at).total_seconds())
    )
    cached["clocks"]["wire"]["age_seconds"] = max(
        0, int((cached_checked - wire_at).total_seconds())
    )
    provider.write_text(json.dumps(cached) + "\n", encoding="utf-8")
    public.write_text(json.dumps(cached) + "\n", encoding="utf-8")
    rejected = invoke()
    assert rejected.returncode != 0
    assert "cached or delayed response" in rejected.stderr


def test_publisher_unlinks_each_private_temporary_file_separately() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")
    unlink_lines = [
        line.strip()
        for line in publisher.splitlines()
        if line.strip().startswith("unlink ")
    ]

    assert unlink_lines
    assert all(len(line.split()) == 2 for line in unlink_lines)
    for temporary in ("provider_tmp", "public_tmp", "topology_tmp"):
        assert f'unlink "${temporary}"' in unlink_lines


def test_repository_builders_cannot_inherit_railway_or_ambient_git_authority() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8")

    unset_token = publisher.index("unset RAILWAY_TOKEN RAILWAY_API_TOKEN")
    first_builder = publisher.index('"$PYTHON_BIN" -c')
    first_railway = publisher.index('"$RAILWAY_BIN" up --detach')
    assert unset_token < first_builder < first_railway
    assert publisher.count('"$railway_token_name=$railway_token_value"') == 5
    assert "GIT_NO_REPLACE_OBJECTS=1" in publisher
    assert "GIT_CONFIG_GLOBAL=/dev/null" in publisher
    assert "GIT_CONFIG_SYSTEM=/dev/null" in publisher
    assert "refs/replace" in publisher
    assert '"$SOURCE_REPOSITORY/.git/shallow"' in publisher
    assert '"$SOURCE_REPOSITORY/.git/info/grafts"' in publisher


def test_one_time_base_advance_is_incident_specific_and_closed_schema() -> None:
    assert os.access(ADVANCE_BASE, os.X_OK)
    namespace = runpy.run_path(str(ADVANCE_BASE))
    source = ADVANCE_BASE.read_text(encoding="utf-8")

    assert namespace["INCIDENT_BASE_SHA"] == (
        "b22d809bca5ca8aed8255e8a89a06a88dc9cbcb9"
    )
    assert namespace["INCIDENT_LIVE_SHA"] == (
        "ae5ecacd2e151d15af3fe06a7cd1219aa51573e7"
    )
    assert namespace["INCIDENT_DEPLOYMENT_ID"] == (
        "505bd041-4c52-4ce7-a137-dc3e4c55cacb"
    )
    assert "target base is not the exact fetched public main tip" in source
    assert "provider and public live manifests are not byte-identical" in source
    assert "publication base pin already exists; incident is closed" in source
    for installed_contract in (
        "publisher_sha256",
        "reconciler_sha256",
        "transition_helper_sha256",
        "watchdog_sha256",
        "publisher_service_sha256",
        "watchdog_service_sha256",
        "watchdog_timer_sha256",
    ):
        assert installed_contract in source

    strict_json = namespace["_strict_json"]
    transition_error = namespace["TransitionError"]
    with pytest.raises(transition_error, match="duplicate key"):
        strict_json(b'{"status":"verified","status":"forged"}', "test")
    with pytest.raises(transition_error, match="exact incident receipt"):
        namespace["_validate_incident_receipt"](b"{}\n")


def test_repeatable_base_rotation_owns_the_zero_quiesce_lock_transaction() -> None:
    assert os.access(ROTATE_BASE, os.X_OK)
    namespace = runpy.run_path(str(ROTATE_BASE))
    source = ROTATE_BASE.read_text(encoding="utf-8")

    assert namespace["PIN_SCHEMA"] == "palimpsest.direct-publication-base.v2"
    assert namespace["ROTATION_SCHEMA"] == (
        "palimpsest.direct-publication-base-rotation.v1"
    )
    assert namespace["ACKNOWLEDGEMENT"] == ("rotate-palimpsest-direct-publication-base")
    transaction = inspect.getsource(namespace["perform_rotation"])
    installer = inspect.getsource(namespace["_install_target_artifacts"])
    lock = transaction.index("_acquire_lock(")
    admission_blockers = transaction.index("_require_no_rotation_blockers(", lock)
    intent = transaction.index("_persist_rotation_intent(", lock)
    install = transaction.index("_install_target_artifacts(", lock)
    final_blockers = transaction.index("_require_no_rotation_blockers(", install)
    inactive = transaction.index("_require_service_inactive(", install)
    archive = transaction.index("_archive_bytes(record_path, record_raw", install)
    replace = transaction.index("_atomic_replace_pin(", archive)
    assert transaction.count("_require_no_rotation_blockers(") == 2
    assert (
        lock
        < admission_blockers
        < intent
        < install
        < final_blockers
        < inactive
        < archive
        < replace
    )
    assert '["/usr/bin/systemctl", "daemon-reload"]' in installer
    ensure_control_root = transaction.index("_ensure_control_root(")
    assert ensure_control_root < install
    for unit_name in (
        "palimpsest-direct-watchdog.service",
        "palimpsest-event-analysis-live.service",
        "palimpsest-railway-publish.service",
    ):
        unit = (ROOT / "ops" / "systemd" / unit_name).read_text(encoding="utf-8")
        assert "ReadOnlyPaths=/var/lib/palimpsest/railway-control" in unit
        assert "ReadOnlyPaths=-/var/lib/palimpsest/railway-control" not in unit
    assert "systemctl disable" not in source
    assert "systemctl stop" not in source
    assert "maintenance-begin" not in source
    assert '(state_root / "pending-candidate.json", "pending candidate")' in source
    assert '(state_root / "pending-preparation.json", "pending preparation")' in source
    assert '(data_hold, "DATA HOLD")' in source


def test_candidate_reconciler_has_closed_adopt_preserve_rollback_hold_states() -> None:
    assert os.access(RECONCILE, os.X_OK)
    source = RECONCILE.read_text(encoding="utf-8")

    assert '"deployment", "list"' in source
    assert "candidate has multiple Railway deployments" in source
    assert "predecessor_already_live" in source
    assert "predecessor_rolled_back" in source
    assert "prior rollback attempt has not proved restoration" in source
    assert "deploymentRollback(id: $id)" in source
    assert "mutation_may_execute" in source
    assert "palimpsest.direct-publication-data-hold.v1" in source
    assert '"status": "DATA HOLD"' in source
    assert "candidate journal changed during adoption" in source
    assert "A crash may occur after the fsynced pending journal" in source
    assert "existing recovery receipt differs from re-proved recovery" in source
    assert "RAILWAY_BIN" in source
    assert '"up"' not in source
    attempt = source.index("attempt = _write_attempt")
    root_guard = source.index("_write_hold(", attempt)
    final_status = source.index("guarded_raw = _status", root_guard)
    mutation = source.index(
        'response = _graphql("mutation PalimpsestRollback', final_status
    )
    assert attempt < root_guard < final_status < mutation
    adoption = source.index("proof = _prove_candidate(")
    journal_reproof = source.index(
        "candidate journal changed during adoption", adoption
    )
    final_reserve = source.index(
        "candidate adoption lacks the final receipt-commit reserve", journal_reproof
    )
    receipt = source.index("document = _build_success(", final_reserve)
    receipt_deadline = source.index(
        "candidate adoption missed the receipt deadline", receipt
    )
    consume = source.index("_consume_success(", receipt_deadline)
    assert adoption < journal_reproof < final_reserve < receipt < receipt_deadline < consume
    unmarked = source.index("if _consumes(current, candidate, journal, archive):")
    restore = source.index("_atomic(", unmarked)
    re_adoption = source.index("proof = _prove_candidate(", restore)
    assert unmarked < restore < re_adoption


def test_reconciler_rejects_open_candidate_schema_and_bad_clocks() -> None:
    namespace = runpy.run_path(str(RECONCILE))
    validate = namespace["_validate_candidate"]
    error = namespace["ReconciliationError"]
    candidate = _candidate_fixture()
    validate(candidate)
    forged = json.loads(json.dumps(candidate))
    forged["unreviewed"] = True
    with pytest.raises(error, match="closed schema"):
        validate(forged)
    forged = json.loads(json.dumps(candidate))
    forged["prepared_at"] = "2999-01-01T00:00:00Z"
    with pytest.raises(error, match="future"):
        validate(forged)
    forged = json.loads(json.dumps(candidate))
    forged["schema_version"] = "palimpsest.direct-publication-candidate.v1"
    with pytest.raises(error, match="identity"):
        validate(forged)
    for tail in (
        {"count": True, "sha256": "f" * 64},
        {"count": -1, "sha256": "f" * 64},
        {"count": 1, "sha256": "F" * 64},
        {"count": 1, "sha256": "f" * 64, "events": []},
    ):
        forged = json.loads(json.dumps(candidate))
        forged["news_source_tail"] = tail
        with pytest.raises(error, match="news source tail"):
            validate(forged)


def test_reconciler_candidate_receipt_deadline_is_exact_and_never_extended() -> None:
    namespace = runpy.run_path(str(RECONCILE))
    validate = namespace["_validate_candidate"]
    timely = namespace["_candidate_adoption_is_timely"]
    prepared = namespace["_receipt_was_prepared_in_window"]
    error = namespace["ReconciliationError"]
    candidate = _candidate_fixture()

    validate(candidate)
    assert timely(
        candidate,
        now=datetime(2026, 8, 30, 12, 16, 49, tzinfo=UTC),
        reserve_seconds=10,
    )
    assert not timely(
        candidate,
        now=datetime(2026, 8, 30, 12, 16, 51, tzinfo=UTC),
        reserve_seconds=10,
    )
    assert prepared(
        {"recorded_at": "2026-08-30T12:16:59Z"}, candidate
    )
    assert not prepared(
        {"recorded_at": "2026-08-30T12:17:01Z"}, candidate
    )
    assert not prepared(
        {"recorded_at": "2026-08-30T12:04:59Z"}, candidate
    )

    forged = json.loads(json.dumps(candidate))
    forged["receipt_deadline_at"] = forged["prepared_at"]
    with pytest.raises(error, match="does not follow preparation"):
        validate(forged)

    forged = json.loads(json.dumps(candidate))
    forged["receipt_deadline_at"] = "2026-08-30T12:17:01Z"
    with pytest.raises(error, match="mutation bound"):
        validate(forged)

    forged = json.loads(json.dumps(candidate))
    forged["prepared_at"] = "2026-08-30T12:20:00Z"
    forged["receipt_deadline_at"] = "2026-08-30T12:25:01Z"
    with pytest.raises(error, match="live wire margin"):
        validate(forged)

    forged = json.loads(json.dumps(candidate))
    forged["receipt_deadline_at"] = "2026-08-30T12:17:00+00:00"
    with pytest.raises(error, match="strict UTC"):
        validate(forged)


def test_reconciler_restores_predecessor_when_receipt_fsync_misses_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(RECONCILE))
    consume = namespace["_consume_success"]
    globals_ = consume.__globals__
    error = namespace["ReconciliationError"]
    candidate = _candidate_fixture()
    document = {
        "candidate": {
            "archive_path": "/var/lib/palimpsest/candidates/candidate.json",
            "journal_sha256": "a" * 64,
        },
        "recorded_at": "2026-08-30T12:16:59Z",
    }
    predecessor_raw = b'{"schema_version":"predecessor"}\n'
    events: list[tuple[str, object]] = []

    def atomic(_path: Path, raw: bytes, **_kwargs: object) -> None:
        events.append(("atomic", raw))

    def timely(_candidate: dict[str, object]) -> bool:
        events.append(("deadline", None))
        return False

    monkeypatch.setitem(globals_, "_atomic", atomic)
    monkeypatch.setitem(globals_, "_candidate_adoption_is_timely", timely)
    monkeypatch.setitem(
        globals_, "_clear_preparation", lambda *_args, **_kwargs: None
    )
    monkeypatch.setitem(globals_, "_clear_hold", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(
        globals_, "_unlink", lambda path: events.append(("unlink", path))
    )

    with pytest.raises(error, match="became durable after its deadline"):
        consume(document, candidate, predecessor_raw, uid=501, gid=502)

    assert events == [
        ("atomic", globals_["_canonical"](document)),
        ("deadline", None),
        ("atomic", predecessor_raw),
    ]

def test_reconciler_live_freshness_proof_rejects_old_cached_receipts() -> None:
    namespace = runpy.run_path(str(RECONCILE))
    validate = namespace["_validate_live_freshness_proofs"]
    validate_attestations = namespace["_validate_live_attestations"]
    error = namespace["ReconciliationError"]
    checked_at = datetime(2026, 8, 30, 12, 10, 0, tzinfo=UTC)
    wire_at = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)
    publication_at = datetime(2026, 8, 30, 12, 9, 0, tzinfo=UTC)
    candidate = _candidate_fixture()
    manifest = _release_manifest_fixture()
    manifest["built_at"] = publication_at.isoformat().replace("+00:00", "Z")
    attestation_raw = namespace["_canonical"](_attestation_fixture(candidate))
    manifest["critical_files"][
        "readings/publication-freshness-attestation-latest.json"
    ] = {
        "bytes": len(attestation_raw),
        "sha256": hashlib.sha256(attestation_raw).hexdigest(),
    }
    attestation = validate_attestations(
        attestation_raw,
        attestation_raw,
        manifest=manifest,
        candidate=candidate,
    )
    proof = _freshness_fixture(
        checked_at=checked_at,
        wire_at=wire_at,
        publication_at=publication_at,
    )
    raw = (json.dumps(proof, sort_keys=True, separators=(",", ":")) + "\n").encode()

    validate(
        raw,
        raw,
        manifest=manifest,
        candidate=candidate,
        attestation=attestation,
        now=checked_at,
    )

    old = json.loads(json.dumps(proof))
    old_checked = checked_at - timedelta(minutes=5)
    old["checked_at"] = old_checked.isoformat().replace("+00:00", "Z")
    old["clocks"]["wire"]["age_seconds"] = int(
        (old_checked - wire_at).total_seconds()
    )
    old["clocks"]["publication"]["age_seconds"] = max(
        0, int((old_checked - publication_at).total_seconds())
    )
    old_raw = (
        json.dumps(old, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    with pytest.raises(error, match="not current"):
        validate(
            old_raw,
            old_raw,
            manifest=manifest,
            candidate=candidate,
            attestation=attestation,
            now=checked_at,
        )

    forged = json.loads(json.dumps(proof))
    forged["clocks"]["wire"]["generated_at"] = "2026-08-30T12:00:01Z"
    forged_raw = (
        json.dumps(forged, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    with pytest.raises(error, match="wire freshness proof is not live and bound"):
        validate(
            forged_raw,
            raw,
            manifest=manifest,
            candidate=candidate,
            attestation=attestation,
            now=checked_at,
        )

    forged_attestation = _attestation_fixture(candidate)
    forged_attestation["artifacts"]["newswire"]["canonical_sha256"] = "0" * 64
    forged_attestation_raw = namespace["_canonical"](forged_attestation)
    forged_manifest = json.loads(json.dumps(manifest))
    forged_manifest["critical_files"][
        "readings/publication-freshness-attestation-latest.json"
    ] = {
        "bytes": len(forged_attestation_raw),
        "sha256": hashlib.sha256(forged_attestation_raw).hexdigest(),
    }
    with pytest.raises(error, match="candidate identity"):
        validate_attestations(
            forged_attestation_raw,
            forged_attestation_raw,
            manifest=forged_manifest,
            candidate=candidate,
        )


def test_reconciler_newsroom_proof_derives_measurement_and_availability_counts() -> None:
    namespace = runpy.run_path(str(RECONCILE))
    validate = namespace["_validate_live_newsroom"]
    validate_proofs = namespace["_validate_live_newsroom_proofs"]
    validate_html = namespace["_validate_live_newsroom_html"]
    validate_html_proofs = namespace["_validate_live_newsroom_html_proofs"]
    error = namespace["ReconciliationError"]
    newsroom = _availability_newsroom_fixture(namespace)

    raw = (
        json.dumps(newsroom, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    manifest = _release_manifest_fixture()
    manifest["critical_files"]["readings/newsroom-latest.json"] = {
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    html = (
        b"<strong>Edition generated</strong>"
        b"6 measurements \xc2\xb7 33 availability notices \xc2\xb7 source records"
        b"<strong>6</strong> measurements \xc2\xb7 <strong>33</strong> "
        b"availability notices in this edition"
    )
    manifest["critical_files"]["news/index.html"] = {
        "bytes": len(html),
        "sha256": hashlib.sha256(html).hexdigest(),
    }
    clock_args = {
        "expected_wire_generated_at": "2026-08-30T12:00:00Z",
        "manifest_built_at": manifest["built_at"],
    }
    assert validate_proofs(
        raw,
        raw,
        manifest=manifest,
        expected_wire_generated_at=clock_args["expected_wire_generated_at"],
    ) == (6, 33)
    with pytest.raises(error, match="structured newsrooms differ"):
        validate_proofs(
            raw,
            raw + b" ",
            manifest=manifest,
            expected_wire_generated_at=clock_args["expected_wire_generated_at"],
        )
    validate_html_proofs(
        html,
        html,
        manifest=manifest,
        measurement_count=6,
        availability_count=33,
    )
    with pytest.raises(error, match="provider and public newsroom HTML differ"):
        validate_html_proofs(
            html + b" ",
            html,
            manifest=manifest,
            measurement_count=6,
            availability_count=33,
        )
    cached_same_counts = html.replace(b"source records", b"prior edition")
    with pytest.raises(error, match="not byte-bound"):
        validate_html_proofs(
            cached_same_counts,
            cached_same_counts,
            manifest=manifest,
            measurement_count=6,
            availability_count=33,
        )

    for boundary in (
        clock_args["expected_wire_generated_at"],
        clock_args["manifest_built_at"],
    ):
        boundary_raw = (
            json.dumps(
                _availability_newsroom_fixture(namespace, generated_at=boundary),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        assert validate(boundary_raw, **clock_args) == (6, 33)

    for impossible_clock in ("2020-01-01T00:00:00Z", "2026-08-30T12:03:01Z"):
        impossible_raw = (
            json.dumps(
                _availability_newsroom_fixture(
                    namespace, generated_at=impossible_clock
                ),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        with pytest.raises(error, match="clocks are not causally ordered"):
            validate(impossible_raw, **clock_args)

    fractional_clock = _availability_newsroom_fixture(
        namespace, generated_at="2026-08-30T12:02:00.500Z"
    )
    fractional_raw = (
        json.dumps(fractional_clock, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    with pytest.raises(error, match="not a strict UTC clock"):
        validate(fractional_raw, **clock_args)

    validate_html(html, measurement_count=6, availability_count=33)

    leaked = json.loads(json.dumps(newsroom))
    board = next(
        story for story in leaked["stories"] if story["signal_id"] == "board-alarm"
    )
    board["metric"]["value"] = 3.2
    leaked_raw = (
        json.dumps(leaked, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    with pytest.raises(error, match="unsafe availability record: board-alarm"):
        validate(leaked_raw, **clock_args)

    for field, forged in (
        ("claim_fingerprint", "sha256:" + "f" * 64),
        ("method", {"summary": "FDR007 latest reading 987654.321", "version": 1}),
        ("limitations", ["FDR007 latest reading 987654.321"]),
        ("leaked_value", 987654.321),
    ):
        forged_newsroom = json.loads(json.dumps(newsroom))
        forged_board = next(
            story
            for story in forged_newsroom["stories"]
            if story["signal_id"] == "board-alarm"
        )
        forged_board[field] = forged
        forged_raw = (
            json.dumps(forged_newsroom, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        with pytest.raises(error, match="unsafe availability record: board-alarm"):
            validate(forged_raw, **clock_args)

    top_level_shadow = json.loads(json.dumps(newsroom))
    top_level_shadow["unexpected_top_level"] = "shadow payload"
    with pytest.raises(error, match="closed schema"):
        validate(
            (
                json.dumps(top_level_shadow, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode(),
            **clock_args,
        )

    duplicate = json.loads(json.dumps(newsroom))
    duplicate["stories"][-1]["signal_id"] = duplicate["stories"][-2]["signal_id"]
    duplicate_raw = (
        json.dumps(duplicate, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    with pytest.raises(error, match="signal inventory"):
        validate(duplicate_raw, **clock_args)

    mixed = json.loads(json.dumps(newsroom))
    extra_availability = next(
        story
        for story in mixed["stories"]
        if story["signal_id"] == "public-deletion-ledgers"
    )
    extra_availability["metric"]["value"] = 1
    _recompute_story_fingerprint(extra_availability)
    mixed_raw = (
        json.dumps(mixed, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    with pytest.raises(error, match="mixes claim and metric semantics"):
        validate(mixed_raw, **clock_args)

    missing_measurement = json.loads(json.dumps(newsroom))
    measurement = next(
        story
        for story in missing_measurement["stories"]
        if story["signal_id"] == "ddti"
    )
    measurement["metric"] = {
        "denominator": {"label": None, "value": None},
        "label": None,
        "unit": None,
        "value": None,
    }
    _recompute_story_fingerprint(measurement)
    missing_measurement_raw = (
        json.dumps(
            missing_measurement, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode()
    with pytest.raises(error, match="mixes claim and metric semantics"):
        validate(missing_measurement_raw, **clock_args)

    with pytest.raises(error, match="exactly bound"):
        validate_html(
            b"<strong>Edition generated</strong>"
            b"7 measurements \xc2\xb7 32 availability notices \xc2\xb7 source records"
            b"<strong>7</strong> measurements \xc2\xb7 <strong>32</strong> "
            b"availability notices in this edition",
            measurement_count=6,
            availability_count=33,
        )
    with pytest.raises(error, match="exactly bound"):
        validate_html(
            b"<strong>Edition generated</strong>"
            b"7 measurements \xc2\xb7 32 availability notices \xc2\xb7 source records"
            b"6 measurements \xc2\xb7 33 availability notices \xc2\xb7 hidden proof"
            b"<strong>6</strong> measurements \xc2\xb7 <strong>33</strong> "
            b"availability notices in this edition",
            measurement_count=6,
            availability_count=33,
        )
    with pytest.raises(error, match="continuously live"):
        validate_html(
            b"<strong>Edition generated</strong>measurements live"
            b"6 measurements \xc2\xb7 33 availability notices \xc2\xb7 source records"
            b"<strong>6</strong> measurements \xc2\xb7 <strong>33</strong> "
            b"availability notices in this edition",
            measurement_count=6,
            availability_count=33,
        )


def test_reconciler_newsroom_feeds_bind_all_instruments_and_source_tail() -> None:
    namespace = runpy.run_path(str(RECONCILE))
    validate_newsroom = namespace["_validate_live_newsroom"]
    validate_feeds = namespace["_validate_live_newsroom_feeds"]
    error = namespace["ReconciliationError"]
    newsroom = _availability_newsroom_fixture(namespace)
    newsroom_raw = namespace["_canonical"](newsroom)
    assert validate_newsroom(
        newsroom_raw,
        expected_wire_generated_at="2026-08-30T12:00:00Z",
        manifest_built_at="2026-08-30T12:03:00Z",
    ) == (6, 33)
    feeds = _newsroom_feed_fixture(newsroom)
    manifest = _release_manifest_fixture()
    for path, raw in feeds.items():
        manifest["critical_files"][path] = {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    assert validate_feeds(
        feeds,
        feeds,
        manifest=manifest,
        newsroom_raw=newsroom_raw,
    ) == (
        _source_tail_proof(feeds)["count"],
        _source_tail_proof(feeds)["sha256"],
    )

    provider_mismatch = dict(feeds)
    provider_mismatch["news/feed.json"] += b" "
    with pytest.raises(error, match="provider and public static artifacts differ"):
        validate_feeds(
            provider_mismatch,
            feeds,
            manifest=manifest,
            newsroom_raw=newsroom_raw,
        )

    reordered = json.loads(feeds["news/instruments/feed.json"])
    reordered["items"][0], reordered["items"][1] = (
        reordered["items"][1],
        reordered["items"][0],
    )
    reordered_raw = (json.dumps(reordered, ensure_ascii=False, indent=2) + "\n").encode()
    reordered_feeds = dict(feeds)
    reordered_feeds["news/instruments/feed.json"] = reordered_raw
    reordered_manifest = json.loads(json.dumps(manifest))
    reordered_manifest["critical_files"]["news/instruments/feed.json"] = {
        "bytes": len(reordered_raw),
        "sha256": hashlib.sha256(reordered_raw).hexdigest(),
    }
    with pytest.raises(error, match="exact newsroom instrument prefix"):
        validate_feeds(
            reordered_feeds,
            reordered_feeds,
            manifest=reordered_manifest,
            newsroom_raw=newsroom_raw,
        )

    forged_rss = feeds["news/feed.xml"].replace(
        b"event-" + b"1" * 24,
        b"event-" + b"9" * 24,
        1,
    )
    forged_feeds = dict(feeds)
    forged_feeds["news/feed.xml"] = forged_rss
    forged_manifest = json.loads(json.dumps(manifest))
    forged_manifest["critical_files"]["news/feed.xml"] = {
        "bytes": len(forged_rss),
        "sha256": hashlib.sha256(forged_rss).hexdigest(),
    }
    with pytest.raises(error, match="cross-bound between JSON and RSS"):
        validate_feeds(
            forged_feeds,
            forged_feeds,
            manifest=forged_manifest,
            newsroom_raw=newsroom_raw,
        )

    forged_json_document = json.loads(feeds["news/feed.json"])
    forged_json_document["items"][39]["content_text"] = "Unbound source body."
    forged_json_raw = (
        json.dumps(forged_json_document, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    forged_json_feeds = dict(feeds)
    forged_json_feeds["news/feed.json"] = forged_json_raw
    forged_json_manifest = json.loads(json.dumps(manifest))
    forged_json_manifest["critical_files"]["news/feed.json"] = {
        "bytes": len(forged_json_raw),
        "sha256": hashlib.sha256(forged_json_raw).hexdigest(),
    }
    with pytest.raises(error, match="JSON source contract is invalid"):
        validate_feeds(
            forged_json_feeds,
            forged_json_feeds,
            manifest=forged_json_manifest,
            newsroom_raw=newsroom_raw,
        )

    from scripts import build_newsroom

    forged_wire = _newsroom_wire_fixture(newsroom)
    forged_wire["events"][0]["headline"] = "Consistently forged source report"
    consistent_forgery = dict(feeds)
    consistent_forgery["news/feed.json"] = (
        json.dumps(
            build_newsroom.build_json_feed(newsroom, forged_wire),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode()
    consistent_forgery["news/feed.xml"] = build_newsroom.build_rss(
        newsroom, forged_wire
    )
    consistent_manifest = json.loads(json.dumps(manifest))
    for path in ("news/feed.json", "news/feed.xml"):
        raw = consistent_forgery[path]
        consistent_manifest["critical_files"][path] = {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    forged_proof = validate_feeds(
        consistent_forgery,
        consistent_forgery,
        manifest=consistent_manifest,
        newsroom_raw=newsroom_raw,
    )
    assert forged_proof != (
        _source_tail_proof(feeds)["count"],
        _source_tail_proof(feeds)["sha256"],
    )


def test_reconciler_rejects_corruption_in_any_ordinary_story_contract() -> None:
    namespace = runpy.run_path(str(RECONCILE))
    validate = namespace["_validate_live_newsroom"]
    error = namespace["ReconciliationError"]
    clock_args = {
        "expected_wire_generated_at": "2026-08-30T12:00:00Z",
        "manifest_built_at": "2026-08-30T12:03:00Z",
    }

    def reject(
        signal_id: str,
        mutate: object,
        message: str,
    ) -> None:
        newsroom = _availability_newsroom_fixture(namespace)
        story = next(
            row for row in newsroom["stories"] if row["signal_id"] == signal_id
        )
        mutate(story)
        with pytest.raises(error, match=message):
            validate(namespace["_canonical"](newsroom), **clock_args)

    reject("ddti", lambda story: story.update(id="palimpsest-news:wrong"), "identity")
    reject("ddti", lambda story: story.update(headline=7), "headline")
    reject("ddti", lambda story: story.update(modified_at="2026-08-30T12:01:00Z"), "timestamps")
    reject(
        "ddti",
        lambda story: story["evidence"]["input"].update(sha256="0" * 63),
        "evidence",
    )
    reject(
        "ddti",
        lambda story: (
            story["evidence"]["input"].update(sha256=None, bytes=None),
            story["evidence"].update(source_timestamp=None),
        ),
        "measurement evidence",
    )
    reject(
        "ddti",
        lambda story: story["evidence"].update(
            url="https://palimpsest.info/readings/wrong-latest.json"
        ),
        "story evidence",
    )
    reject(
        "ddti",
        lambda story: story["evidence"].update(
            source_timestamp="2026-08-30T12:01:59Z"
        ),
        "story evidence",
    )
    reject(
        "ddti",
        lambda story: story.update(claim_fingerprint="sha256:" + "0" * 64),
        "fingerprint",
    )
    reject(
        "ddti",
        lambda story: story["method"].update(version=0),
        "method version",
    )
    reject("ddti", lambda story: story.update(order=True), "identity")
    reject("ddti", lambda story: story.update(unexpected="value"), "identity")
    reject(
        "public-deletion-ledgers",
        lambda story: story["limitations"].append(
            "Public value publication is restricted by policy."
        ),
        "rights-only marker",
    )
    reject(
        "public-deletion-ledgers",
        lambda story: story["method"].update(version=False),
        "method version",
    )


def test_reconciler_adopts_only_fresh_candidate_within_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(RECONCILE))
    prove = namespace["_prove_candidate"]
    globals_ = prove.__globals__
    now = datetime.now(UTC).replace(microsecond=0)

    def stamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    candidate = _candidate_fixture()
    candidate["prepared_at"] = stamp(now - timedelta(seconds=60))
    candidate["wire_generated_at"] = stamp(now - timedelta(seconds=120))
    candidate["receipt_deadline_at"] = stamp(now + timedelta(minutes=10))
    manifest = _release_manifest_fixture(str(candidate["release_sha"]))
    manifest["built_at"] = stamp(now - timedelta(seconds=30))
    attestation_raw = globals_["_canonical"](
        _attestation_fixture(
            candidate,
            attested_at=stamp(now - timedelta(seconds=60)),
            situation_at=stamp(now - timedelta(seconds=90)),
        )
    )
    manifest["critical_files"][
        "readings/publication-freshness-attestation-latest.json"
    ] = {
        "bytes": len(attestation_raw),
        "sha256": hashlib.sha256(attestation_raw).hexdigest(),
    }
    manifest_raw = globals_["_canonical"](manifest)
    candidate["release_manifest"]["sha256"] = hashlib.sha256(
        manifest_raw
    ).hexdigest()
    candidate["release_manifest"]["bytes"] = len(manifest_raw)
    candidate["release_manifest"]["tree_sha256"] = manifest["tree_sha256"]

    deployment = {
        "id": "705bd041-4c52-4ce7-a137-dc3e4c55cacb",
        "meta": {"imageDigest": "sha256:" + "8" * 64},
        "status": "SUCCESS",
    }
    active = {
        "created_at": stamp(now - timedelta(seconds=20)),
        "deployment_id": deployment["id"],
        "image_digest": deployment["meta"]["imageDigest"],
        "reason": "deploy",
    }
    freshness = _freshness_fixture(
        checked_at=now,
        wire_at=now - timedelta(seconds=120),
        publication_at=now - timedelta(seconds=30),
    )
    freshness_raw = globals_["_canonical"](freshness)
    newsroom_raw = globals_["_canonical"](
        _availability_newsroom_fixture(
            namespace, generated_at=stamp(now - timedelta(seconds=30))
        )
    )
    newsroom_document = json.loads(newsroom_raw)
    measurement_evidence = _newsroom_measurement_evidence(newsroom_document)
    manifest["critical_files"]["readings/newsroom-latest.json"] = {
        "bytes": len(newsroom_raw),
        "sha256": hashlib.sha256(newsroom_raw).hexdigest(),
    }
    manifest_raw = globals_["_canonical"](manifest)
    candidate["release_manifest"]["sha256"] = hashlib.sha256(
        manifest_raw
    ).hexdigest()
    candidate["release_manifest"]["bytes"] = len(manifest_raw)
    html = (
        b"<strong>Edition generated</strong>"
        b"6 measurements \xc2\xb7 33 availability notices \xc2\xb7 source records"
        b"<strong>6</strong> measurements \xc2\xb7 <strong>33</strong> "
        b"availability notices in this edition"
    )
    manifest["critical_files"]["news/index.html"] = {
        "bytes": len(html),
        "sha256": hashlib.sha256(html).hexdigest(),
    }
    feeds = _newsroom_feed_fixture(newsroom_document)
    for path, raw in feeds.items():
        manifest["critical_files"][path] = {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    candidate["news_source_tail"] = _source_tail_proof(feeds)
    denied_analysis_feeds = _denied_analysis_feed_fixture(
        str(newsroom_document["generated_at"])
    )
    for path, raw in {**measurement_evidence, **denied_analysis_feeds}.items():
        manifest["critical_files"][path] = {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    manifest_raw = globals_["_canonical"](manifest)
    candidate["release_manifest"]["sha256"] = hashlib.sha256(
        manifest_raw
    ).hexdigest()
    candidate["release_manifest"]["bytes"] = len(manifest_raw)
    fetched_urls: list[str] = []

    def fetch(url: str, **_kwargs: object) -> bytes:
        fetched_urls.append(url)
        if "/readings/publication-freshness-attestation-latest.json?" in url:
            return attestation_raw
        if "/news/?" in url:
            return html
        if "/readings/newsroom-latest.json?" in url:
            return newsroom_raw
        for path, raw in feeds.items():
            if f"/{path}?" in url:
                return raw
        for path, raw in measurement_evidence.items():
            if f"/{path}?" in url:
                return raw
        for path, raw in denied_analysis_feeds.items():
            if f"/{path}?" in url:
                return raw
        if "/freshness?" in url:
            return freshness_raw
        raise AssertionError(f"unexpected live proof URL: {url}")

    monkeypatch.setitem(
        globals_, "_live_manifests", lambda _label: (manifest_raw, manifest_raw)
    )
    monkeypatch.setitem(globals_, "_fetch", fetch)
    monkeypatch.setitem(globals_, "_status", lambda *_args, **_kwargs: b"{}")
    monkeypatch.setitem(globals_, "_topology", lambda *_args, **_kwargs: active)

    assert prove(
        candidate,
        deployment,
        active,
        manifest_raw,
        "RAILWAY_TOKEN",
        "x",
    ) == (manifest, manifest_raw)
    assert all("/readings/newswire-latest.json?" not in url for url in fetched_urls)
    for relative in measurement_evidence:
        assert sum(f"/{relative}?" in url for url in fetched_urls) == 2
    for relative in denied_analysis_feeds:
        assert sum(f"/{relative}?" in url for url in fetched_urls) == 2

    source_tail = dict(candidate["news_source_tail"])
    candidate["news_source_tail"]["sha256"] = "0" * 64
    assert (
        prove(
            candidate,
            deployment,
            active,
            manifest_raw,
            "RAILWAY_TOKEN",
            "x",
        )
        is None
    )
    candidate["news_source_tail"] = source_tail

    mismatched_relative = next(iter(measurement_evidence))

    def mismatched_fetch(url: str, **kwargs: object) -> bytes:
        raw = fetch(url, **kwargs)
        if url.startswith(str(globals_["PUBLIC_ORIGIN"])) and (
            f"/{mismatched_relative}?" in url
        ):
            return raw + b" "
        return raw

    monkeypatch.setitem(globals_, "_fetch", mismatched_fetch)
    assert (
        prove(
            candidate,
            deployment,
            active,
            manifest_raw,
            "RAILWAY_TOKEN",
            "x",
        )
        is None
    )
    monkeypatch.setitem(globals_, "_fetch", fetch)

    candidate["receipt_deadline_at"] = stamp(now - timedelta(seconds=1))
    assert (
        prove(
            candidate,
            deployment,
            active,
            manifest_raw,
            "RAILWAY_TOKEN",
            "x",
        )
        is None
    )


def test_reconciler_accepts_and_exactly_binds_a_successor_base_pin() -> None:
    namespace = runpy.run_path(str(RECONCILE))
    validate_pin = namespace["_validate_pin"]
    globals_ = validate_pin.__globals__
    globals_["PROJECT_ID"] = "f7c86128-53a7-458a-a931-6628c6e61fb2"
    globals_["ENVIRONMENT_ID"] = "1d4d9eef-7bad-4c7b-a003-0e66fe9a8fe2"
    globals_["SERVICE_ID"] = "86a6f49c-b9dc-4be8-acd1-dd180c693230"
    validate_candidate = namespace["_validate_candidate"]
    validate_binding = namespace["_validate_candidate_pin_binding"]
    predecessor_from_receipt = namespace["_predecessor_from_receipt"]
    build_success = namespace["_build_success"]
    canonical = namespace["_canonical"]
    error = namespace["ReconciliationError"]

    pin = _successor_pin_fixture(globals_)
    validate_pin(pin)
    pin_raw = canonical(pin)

    candidate = _candidate_fixture()
    candidate["publication_base"] = {
        "kind": "verified_successor",
        "path": str(namespace["BASE_PIN"]),
        "sha256": hashlib.sha256(pin_raw).hexdigest(),
    }
    candidate["release_manifest"]["path"] = str(
        namespace["STATE_ROOT"]
        / "release-manifests"
        / f"{candidate['release_sha']}.json"
    )
    validate_candidate(candidate)
    validate_binding(candidate, pin, pin_raw)

    forged = json.loads(json.dumps(candidate))
    forged["publication_base"]["kind"] = "verified_transition"
    with pytest.raises(error, match="pin changed"):
        validate_binding(forged, pin, pin_raw)

    manifest = _release_manifest_fixture(str(candidate["release_sha"]))
    manifest_raw = canonical(manifest)
    receipt = build_success(
        candidate,
        journal="f" * 64,
        archive=namespace["STATE_ROOT"] / "candidates" / ("f" * 64 + ".json"),
        deployment_id="605bd041-4c52-4ce7-a137-dc3e4c55cacb",
        manifest=manifest,
        manifest_raw=manifest_raw,
    )
    receipt_raw = canonical(receipt)
    predecessor = predecessor_from_receipt(receipt_raw, receipt, pin)
    assert predecessor["release_sha"] == candidate["release_sha"]
    assert receipt["publication_base"]["kind"] == "verified_successor"


def test_reconciler_rejects_forged_successor_pin_relationships() -> None:
    namespace = runpy.run_path(str(RECONCILE))
    validate_pin = namespace["_validate_pin"]
    globals_ = validate_pin.__globals__
    globals_["PROJECT_ID"] = "f7c86128-53a7-458a-a931-6628c6e61fb2"
    globals_["ENVIRONMENT_ID"] = "1d4d9eef-7bad-4c7b-a003-0e66fe9a8fe2"
    globals_["SERVICE_ID"] = "86a6f49c-b9dc-4be8-acd1-dd180c693230"
    error = namespace["ReconciliationError"]
    pin = _successor_pin_fixture(globals_)

    forged = json.loads(json.dumps(pin))
    forged["predecessor"]["publication_receipt"]["publication_base_sha256"] = "f" * 64
    with pytest.raises(error, match="receipt identity"):
        validate_pin(forged)

    forged = json.loads(json.dumps(pin))
    forged["live"]["provider_manifest"]["path"] = "/tmp/forged.json"
    with pytest.raises(error, match="path is not canonical"):
        validate_pin(forged)


def test_release_manifest_anchor_is_duplicate_safe_closed_and_nonempty() -> None:
    namespace = runpy.run_path(str(RECONCILE))
    validate = namespace["_validate_manifest"]
    error = namespace["ReconciliationError"]
    release_sha = "a" * 40
    manifest = _release_manifest_fixture(release_sha)
    raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()

    assert validate(raw, release=release_sha, tree="c" * 64) == manifest

    forged = {**manifest, "unreviewed": True}
    with pytest.raises(error, match="closed schema"):
        validate(
            (json.dumps(forged, sort_keys=True, separators=(",", ":")) + "\n").encode(),
            release=release_sha,
        )

    empty_critical = {**manifest, "critical_files": {}}
    with pytest.raises(error, match="identity is invalid"):
        validate(
            (
                json.dumps(empty_critical, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode(),
            release=release_sha,
        )

    duplicate = raw.replace(
        b'"schema_version":"palimpsest.railway-static-release.v1",',
        b'"schema_version":"palimpsest.railway-static-release.v1",'
        b'"schema_version":"forged",',
    )
    with pytest.raises(error, match="duplicate key"):
        validate(duplicate, release=release_sha)


def test_reconciler_repairs_candidate_archive_and_preserves_bound_artifacts(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(RECONCILE))
    archive_candidate = namespace["_candidate_archive"]
    clear_preparation = namespace["_clear_preparation"]
    globals_ = archive_candidate.__globals__
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    pending = state / "pending-candidate.json"
    candidate = _candidate_fixture()
    candidate["release_bundle"] = {
        **candidate["release_bundle"],
        "path": str(state / "release-bundles" / f"{candidate['release_sha']}.bundle"),
        "metadata_path": str(
            state / "release-bundles" / f"{candidate['release_sha']}.json"
        ),
    }
    raw = (json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n").encode()
    pending.write_bytes(raw)
    pending.chmod(0o600)
    globals_["STATE_ROOT"] = state
    globals_["CANDIDATE_JOURNAL"] = pending

    digest = namespace["_digest"](raw)
    archive = archive_candidate(raw, digest, uid=os.getuid(), gid=os.getgid())
    assert archive.read_bytes() == raw
    assert archive.stat().st_ino == pending.stat().st_ino

    evidence = state / "predecessors" / str(candidate["release_sha"])
    evidence.mkdir(parents=True)
    sentinel = evidence / "provider-railway-release.json"
    sentinel.write_bytes(b"bound predecessor\n")
    preparation = state / "pending-preparation.json"
    preparation.write_text(
        json.dumps(
            {
                "base_sha": candidate["base_sha"],
                "bundle_metadata_path": candidate["release_bundle"]["metadata_path"],
                "bundle_path": candidate["release_bundle"]["path"],
                "evidence_directory": str(evidence),
                "input_sha256": candidate["input_sha256"],
                "prepared_at": "2026-08-30T12:04:30Z",
                "release_manifest_path": candidate["release_manifest"]["path"],
                "release_sha": candidate["release_sha"],
                "schema_version": "palimpsest.direct-publication-preparation.v1",
                "status": "pre_mutation",
                "submission_id": candidate["submission_id"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    preparation.chmod(0o600)
    clear_preparation(candidate, uid=os.getuid(), gid=os.getgid())
    assert not preparation.exists()
    assert sentinel.read_bytes() == b"bound predecessor\n"
    assert archive.read_bytes() == raw


def test_recovery_receipt_is_idempotently_consumed_after_crash(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(RECONCILE))
    write_recovery = namespace["_write_recovery"]
    globals_ = write_recovery.__globals__
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    pending = state / "pending-candidate.json"
    pending.write_bytes(b"candidate\n")
    pending.chmod(0o600)
    globals_["STATE_ROOT"] = state
    globals_["CANDIDATE_JOURNAL"] = pending
    globals_["DATA_HOLD"] = tmp_path / "data-hold.json"
    candidate = _candidate_fixture()
    topology = {
        "created_at": "2026-08-30T12:06:00Z",
        "deployment_id": "605bd041-4c52-4ce7-a137-dc3e4c55cacb",
        "image_digest": "sha256:" + "9" * 64,
        "reason": "deploymentRollback",
    }
    journal = "f" * 64
    archive = state / "candidates" / f"{journal}.json"

    unlink = globals_["_unlink"]
    globals_["_unlink"] = lambda _path: None
    first = write_recovery(
        candidate,
        journal=journal,
        archive=archive,
        outcome="predecessor_rolled_back",
        topology=topology,
        uid=os.getuid(),
        gid=os.getgid(),
        attempt_digest="1" * 64,
        response_digest="2" * 64,
    )
    globals_["_unlink"] = unlink
    assert pending.exists()
    second = write_recovery(
        candidate,
        journal=journal,
        archive=archive,
        outcome="predecessor_rollback_reconciled",
        topology=topology,
        uid=os.getuid(),
        gid=os.getgid(),
        attempt_digest="1" * 64,
    )
    assert second == first
    assert not pending.exists()
    assert len(list((state / "reconciliations").iterdir())) == 1


def test_data_hold_clear_is_candidate_bound_and_attempt_state_is_monotonic(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(RECONCILE))
    write_hold = namespace["_write_hold"]
    clear_hold = namespace["_clear_hold"]
    error = namespace["ReconciliationError"]
    globals_ = write_hold.__globals__
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    hold_path = tmp_path / "data-hold.json"
    globals_["STATE_ROOT"] = state
    globals_["DATA_HOLD"] = hold_path
    globals_["ROOT_UID"] = os.getuid()
    candidate = _candidate_fixture()
    journal = "f" * 64
    archive = state / "candidates" / f"{journal}.json"

    initial = write_hold(
        candidate,
        journal=journal,
        archive=archive,
        reason="active_topology_unrelated",
        attempt=None,
        gid=os.getgid(),
    )
    assert initial["rollback"] == {
        "attempt_path": None,
        "attempt_sha256": None,
        "attempted": False,
    }

    unrelated = json.loads(json.dumps(candidate))
    unrelated["message"] = "palimpsest-hetzner-unrelated"
    with pytest.raises(error, match="identity differs"):
        clear_hold(
            unrelated,
            journal=journal,
            archive=archive,
            gid=os.getgid(),
        )
    assert hold_path.exists()

    attempt_path = state / "rollback-attempts" / f"{journal}.json"
    attempt = (attempt_path, "1" * 64, {"status": "mutation_may_execute"})
    upgraded = write_hold(
        candidate,
        journal=journal,
        archive=archive,
        reason="rollback_restore_unproven",
        attempt=attempt,
        gid=os.getgid(),
    )
    assert upgraded["rollback"] == {
        "attempt_path": str(attempt_path),
        "attempt_sha256": "1" * 64,
        "attempted": True,
    }

    not_downgraded = write_hold(
        candidate,
        journal=journal,
        archive=archive,
        reason="active_topology_unrelated",
        attempt=None,
        gid=os.getgid(),
    )
    assert not_downgraded["rollback"] == upgraded["rollback"]
    clear_hold(candidate, journal=journal, archive=archive, gid=os.getgid())
    assert not hold_path.exists()


def test_prior_attempt_requires_fresh_rollback_and_terminal_failure_gate() -> None:
    namespace = runpy.run_path(str(RECONCILE))
    fresh = namespace["_is_fresh_rollback"]
    terminal = namespace["_terminal_nonactivating"]
    attempt = {
        "candidate_deployment_id": "705bd041-4c52-4ce7-a137-dc3e4c55cacb",
        "predecessor_deployment_id": "505bd041-4c52-4ce7-a137-dc3e4c55cacb",
        "created_at": "2026-08-30T12:05:00Z",
    }
    same_image_deploy = {
        "created_at": "2026-08-30T12:06:00Z",
        "deployment_id": "805bd041-4c52-4ce7-a137-dc3e4c55cacb",
        "image_digest": "sha256:" + "9" * 64,
        "reason": "deploy",
    }
    assert not fresh(same_image_deploy, attempt, "sha256:" + "9" * 64)
    rollback = {**same_image_deploy, "reason": "deploymentRollback"}
    provider_rollback = {**same_image_deploy, "reason": "rollback"}
    assert fresh(provider_rollback, attempt, "sha256:" + "9" * 64)
    assert fresh(rollback, attempt, "sha256:" + "9" * 64)
    assert not fresh(
        {**rollback, "created_at": attempt["created_at"]},
        attempt,
        "sha256:" + "9" * 64,
    )
    assert not terminal(None)
    assert not terminal({"status": "QUEUED"})
    assert not terminal({"status": "BUILDING"})
    assert terminal({"status": "FAILED"})
    assert terminal({"status": "CANCELLED"})

    close_without_mutation = namespace["_can_close_predecessor_without_mutation"]
    assert close_without_mutation(None, preparation_proves_pre_mutation=True)
    assert not close_without_mutation(None, preparation_proves_pre_mutation=False)
    assert not close_without_mutation(
        {"status": "QUEUED"}, preparation_proves_pre_mutation=True
    )
    assert close_without_mutation(
        {"status": "FAILED"}, preparation_proves_pre_mutation=False
    )


@pytest.mark.parametrize(
    ("case", "expected_mutations"),
    (("guard_moved", 0), ("prior_attempt", 0), ("stable", 1)),
)
def test_reconciler_mutates_at_most_once_after_the_durable_guard(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_mutations: int,
) -> None:
    namespace = runpy.run_path(str(RECONCILE))
    reconcile_locked = namespace["_reconcile_locked"]
    globals_ = reconcile_locked.__globals__
    error = namespace["ReconciliationError"]
    candidate = _candidate_fixture()
    predecessor_raw = b'{"schema_version":"test-predecessor"}\n'
    predecessor_digest = hashlib.sha256(predecessor_raw).hexdigest()
    candidate["predecessor"]["receipt_sha256"] = predecessor_digest
    candidate["predecessor"]["archive_path"] = str(
        globals_["STATE_ROOT"] / "receipts" / f"{predecessor_digest}.json"
    )
    pin_raw = (
        b'{"target":{"base_sha":"' + str(candidate["base_sha"]).encode() + b'"}}\n'
    )
    candidate["publication_base"]["sha256"] = hashlib.sha256(pin_raw).hexdigest()
    candidate_raw = (
        json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    archive = (
        globals_["STATE_ROOT"]
        / "candidates"
        / (hashlib.sha256(candidate_raw).hexdigest() + ".json")
    )

    reads = {
        globals_["CANDIDATE_JOURNAL"]: candidate_raw,
        globals_["BASE_PIN"]: pin_raw,
        globals_["DEPLOYED_COMMIT"]: (
            str(candidate["host_deployed_sha"]) + "\n"
        ).encode(),
        globals_["SUCCESS_RECEIPT"]: predecessor_raw,
        globals_["STATE_ROOT"]
        / "receipts"
        / f"{predecessor_digest}.json": predecessor_raw,
    }

    def fake_read(path: Path, **_kwargs: object) -> bytes:
        return reads[path]

    candidate_deployment_id = "705bd041-4c52-4ce7-a137-dc3e4c55cacb"
    rollback_deployment_id = "805bd041-4c52-4ce7-a137-dc3e4c55cacb"
    candidate_image = "sha256:" + "8" * 64
    predecessor_image = str(candidate["rollback_evidence"]["topology"]["image_digest"])
    deployment = {
        "createdAt": "2026-08-30T12:05:01Z",
        "id": candidate_deployment_id,
        "meta": {
            "cliMessage": candidate["message"],
            "imageDigest": candidate_image,
            "reason": "deploy",
        },
        "status": "SUCCESS",
    }
    active = {
        "created_at": "2026-08-30T12:05:01Z",
        "deployment_id": candidate_deployment_id,
        "image_digest": candidate_image,
        "reason": "deploy",
    }
    moved = {
        **active,
        "deployment_id": "905bd041-4c52-4ce7-a137-dc3e4c55cacb",
    }
    restored = {
        "created_at": "2026-08-30T12:06:00Z",
        "deployment_id": rollback_deployment_id,
        "image_digest": predecessor_image,
        "reason": "deploymentRollback",
    }
    statuses = [b"active-initial", b"active-before-guard"]
    if case == "guard_moved":
        statuses.append(b"moved-after-guard")
    elif case == "stable":
        statuses.extend([b"active-after-guard", b"restored", b"restored-final"])
    else:
        statuses = [b"active-initial"]
    status_iter = iter(statuses)
    topology_by_raw = {
        b"active-initial": active,
        b"active-before-guard": active,
        b"active-after-guard": active,
        b"moved-after-guard": moved,
        b"restored": restored,
        b"restored-final": restored,
    }

    def fake_topology(
        raw: bytes,
        *,
        deployment: str | None = None,
        image: str | None = None,
        reason: str | None = None,
    ) -> dict[str, str]:
        value = topology_by_raw[raw]
        if deployment is not None and value["deployment_id"] != deployment:
            raise error("deployment differs")
        if image is not None and value["image_digest"] != image:
            raise error("image differs")
        if reason is not None and value["reason"] != reason:
            raise error("reason differs")
        return value

    attempt_document = {
        "candidate_deployment_id": candidate_deployment_id,
        "candidate_journal_sha256": hashlib.sha256(candidate_raw).hexdigest(),
        "candidate_topology_path": "/private/topology.json",
        "created_at": "2026-08-30T12:05:30.000000Z",
        "predecessor_deployment_id": candidate["predecessor"]["deployment_id"],
        "schema_version": globals_["ATTEMPT_SCHEMA"],
        "status": "mutation_may_execute",
        "topology_sha256": "a" * 64,
    }
    attempt = (Path("/private/attempt.json"), "1" * 64, attempt_document)
    mutation_calls: list[str] = []

    def fake_graphql(
        query: str, _deployment_id: str, _token_name: str, _token_value: str
    ) -> bytes:
        if query.startswith("mutation "):
            mutation_calls.append(query)
            return b'{"data":{"deploymentRollback":true}}\n'
        return b'{"data":{"deployment":{"canRollback":true}}}\n'

    monkeypatch.setitem(globals_, "_read", fake_read)
    monkeypatch.setitem(
        globals_, "_candidate_archive", lambda *_args, **_kwargs: archive
    )
    monkeypatch.setitem(globals_, "_read_hold", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "_validate_pin", lambda _pin: None)
    monkeypatch.setitem(
        globals_, "_validate_candidate_pin_binding", lambda *_args, **_kwargs: None
    )
    monkeypatch.setitem(globals_, "_validate_bundle", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(
        globals_,
        "_validate_release_manifest_anchor",
        lambda *_args, **_kwargs: b"anchor",
    )
    monkeypatch.setitem(
        globals_, "_matching_preparation", lambda *_args, **_kwargs: False
    )
    monkeypatch.setitem(
        globals_,
        "_predecessor_from_receipt",
        lambda *_args, **_kwargs: candidate["predecessor"],
    )
    monkeypatch.setitem(
        globals_, "_validate_evidence", lambda *_args, **_kwargs: (b"saved", b"saved")
    )
    monkeypatch.setitem(globals_, "_inventory", lambda *_args: [deployment])
    monkeypatch.setitem(globals_, "_status", lambda *_args: next(status_iter))
    monkeypatch.setitem(globals_, "_topology", fake_topology)
    monkeypatch.setitem(
        globals_,
        "_read_attempt",
        lambda *_args, **_kwargs: attempt if case == "prior_attempt" else None,
    )
    monkeypatch.setitem(globals_, "_existing_recovery", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "_prove_candidate", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "_prove_predecessor", lambda *_args, **_kwargs: False)
    monkeypatch.setitem(globals_, "_graphql", fake_graphql)
    monkeypatch.setitem(globals_, "_validate_query", lambda *_args: None)
    monkeypatch.setitem(globals_, "_write_attempt", lambda *_args, **_kwargs: attempt)
    monkeypatch.setitem(globals_, "_write_hold", lambda *_args, **_kwargs: {})
    monkeypatch.setitem(globals_, "_write_response", lambda *_args, **_kwargs: "2" * 64)
    monkeypatch.setitem(
        globals_, "_live_manifests", lambda *_args: (b"saved", b"saved")
    )
    monkeypatch.setitem(
        globals_,
        "_write_recovery",
        lambda *_args, **kwargs: {"outcome": kwargs["outcome"]},
    )

    if case == "stable":
        result = reconcile_locked(
            uid=os.getuid(),
            gid=os.getgid(),
            token_name="RAILWAY_TOKEN",
            token_value="x",
        )
        assert result == {"outcome": "predecessor_rolled_back"}
    else:
        expected = (
            "prior rollback attempt"
            if case == "prior_attempt"
            else "deployment differs|changed after rollback guard"
        )
        with pytest.raises(error, match=expected):
            reconcile_locked(
                uid=os.getuid(),
                gid=os.getgid(),
                token_name="RAILWAY_TOKEN",
                token_value="x",
            )
    assert len(mutation_calls) == expected_mutations


def test_recovery_accepts_carried_receipt_only_from_hash_bound_ancestor(monkeypatch):
    namespace = runpy.run_path(str(RECONCILE))
    validate = namespace["_validate_pin"]
    globals_ = validate.__globals__
    globals_.update(PROJECT_ID="f7c86128-53a7-458a-a931-6628c6e61fb2",
                    ENVIRONMENT_ID="1d4d9eef-7bad-4c7b-a003-0e66fe9a8fe2",
                    SERVICE_ID="86a6f49c-b9dc-4be8-acd1-dd180c693230")
    parent = _successor_pin_fixture(globals_)
    canonical = namespace["_canonical"]
    parent_raw = canonical(parent)
    parent_digest = hashlib.sha256(parent_raw).hexdigest()
    current = json.loads(parent_raw)
    current["generation"] += 1
    proof = current["predecessor"]["pin"]
    proof.update(generation=parent["generation"], sha256=parent_digest,
                 path=str(namespace["CONTROL_ROOT"] / "base-rotation-history" / "pins" / (parent_digest + ".json")),
                 target_sha=parent["target"]["base_sha"])
    current["rotation_record_path"] = current["rotation_record_path"].replace("/3-", "/4-")
    reads = []
    def read(path, **kwargs):
        reads.append((path, kwargs))
        assert path == Path(proof["path"])
        return parent_raw
    import types
    monkeypatch.setitem(globals_, "grp", types.SimpleNamespace(getgrnam=lambda _: types.SimpleNamespace(gr_gid=123)))
    monkeypatch.setitem(globals_, "_read", read)
    validate(current)
    assert reads[0][1] == {"uid": 0, "gid": 123, "mode": 0o640}
    error = namespace["ReconciliationError"]
    for mutation in ("digest", "base", "generation", "path"):
        forged = json.loads(canonical(current))
        if mutation == "digest":
            forged["predecessor"]["pin"]["sha256"] = "e" * 64
        elif mutation == "base":
            forged["predecessor"]["publication_receipt"]["base_sha"] = "e" * 40
        elif mutation == "generation":
            forged["predecessor"]["pin"]["generation"] -= 1
        else:
            forged["predecessor"]["pin"]["path"] = "/tmp/untrusted.json"
        with pytest.raises(error):
            validate(forged)
    monkeypatch.setitem(globals_, "_read", lambda *a, **kw: parent_raw + b" ")
    with pytest.raises(error, match="receipt identity"):
        validate(current)
