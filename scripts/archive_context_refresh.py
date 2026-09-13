"""Admit an offline, metadata-only archive projection from a completed producer.

Private OSINT metrics, editorial scores and source prose never enter the public
projection. Original producer and evidence clocks survive unchanged. The short
promotion phase uses the publisher data lock and the permanent sealed-ledger
lock; a durable candidate permits recovery without overwriting foreign writes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from urllib.parse import urlsplit

from core import sealed_ledger as sealed
from core.governance import KillSwitch
from processors import archive_context as archive
from scripts import translation_refresh as io

LATEST = "archive-news-context-latest.json"
HISTORY = "archive-news-context-history.jsonl"
LEDGER = io.LEDGER
FILES = (LEDGER, HISTORY, LATEST)
SOURCE = "archive-news-context"
POLICY = {"automatic_publication": "prohibited", "human_review_required": True,
          "causal_language": "prohibited-without-a-declared-design", "person_level_analysis": "prohibited"}
PRIVATE_KEYS = {"schema_version", "generated_at", "newswire_generated_at", "osint_generated_at",
                "feature_export_sha256", "scope", "method", "n_events_considered",
                "n_events_contextualized", "events", "publication_policy", "context_sha256"}
EVENT_KEYS = {"event_id", "version_id", "event_url", "published_at", "topics", "evidence_strength",
              "relation", "archive_context", "signal_context", "model_features", "editorial_priority",
              "training_label", "automatic_publication_eligible", "limitations"}
FEATURE_KEYS = {"schema_version", "method_version", "target_id", "host", "aliases", "topics", "products",
                "crawl", "previous_crawl", "first_capture_at", "last_capture_at", "available_at", "scope",
                "source", "features", "label", "model", "rights", "feature_sha256"}
SHA = re.compile(r"[a-f0-9]{64}\Z")
PUBLIC_EVENT_KEYS = {"event_id", "version_id", "event_url", "published_at", "topics", "relation",
                     "archive_context", "automatic_publication_eligible", "family", "match_kind",
                     "public_copy", "limitations"}


class ArchiveRefreshError(ValueError):
    pass


def strict(raw: bytes):
    return archive._strict_json_bytes(raw, maximum=io.MAX_BYTES, label="archive admission")


def canonical(value) -> bytes:
    return archive._canonical_json(value)


def digest_document(document: dict, field="context_sha256") -> None:
    if type(document) is not dict or not SHA.fullmatch(str(document.get(field, ""))):
        raise ArchiveRefreshError("archive document has no content identity")
    unsigned = dict(document)
    digest = unsigned.pop(field)
    if hashlib.sha256(canonical(unsigned)).hexdigest() != digest:
        raise ArchiveRefreshError("archive document content identity differs")


def clock(value):
    return archive._timestamp(value, "archive source clock")


def integer(value, label):
    if type(value) is not int or value < 0:
        raise ArchiveRefreshError(f"invalid archive {label}")
    return value


def safe_text(value, label, maximum=200):
    if type(value) is not str or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ArchiveRefreshError(f"invalid archive {label}")
    return value


def public_url(value):
    safe_text(value, "event URL", 4096)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ArchiveRefreshError("invalid archive public event URL")
    return value


def project(context_raw: bytes, features_raw: bytes, *, config, now: datetime) -> dict:
    context = strict(context_raw)
    digest_document(context)
    if set(context) != PRIVATE_KEYS or context["schema_version"] != archive.CONTEXT_SCHEMA_VERSION:
        raise ArchiveRefreshError("unsupported private archive context schema")
    if context["publication_policy"] != POLICY or context["method"] != archive.CONTEXT_METHOD:
        raise ArchiveRefreshError("archive publication policy differs")
    generated = clock(context["generated_at"])
    for field, ceiling in (("generated_at", 5400), ("newswire_generated_at", 7200), ("osint_generated_at", 7200)):
        source_clock = clock(context[field])
        if not -300 <= (now - source_clock).total_seconds() <= ceiling:
            raise ArchiveRefreshError(f"archive {field} is stale or future-dated")
        if field != "generated_at" and source_clock > generated:
            raise ArchiveRefreshError("archive input clock exceeds producer clock")
    feature_digest = hashlib.sha256(features_raw).hexdigest()
    if context["feature_export_sha256"] != feature_digest:
        raise ArchiveRefreshError("archive feature export differs from producer input")
    rows = {}
    for number, raw in enumerate(features_raw.splitlines(), 1):
        if not raw.strip() or len(raw) > archive.MAX_FEATURE_LINE_BYTES:
            raise ArchiveRefreshError("invalid bounded feature row")
        row = archive._verify_feature_row(strict(raw), number)
        if set(row) != FEATURE_KEYS or row["method_version"] != 1:
            raise ArchiveRefreshError("unsupported archive feature fields")
        target = config.target_by_id.get(row["target_id"])
        if (target is None or row["host"] != target.host or set(row["topics"]) != set(target.topics)
                or set(row["products"]) != set(target.products) or set(row["aliases"]) != set(target.aliases)):
            raise ArchiveRefreshError("archive feature differs from reviewed target")
        if row["rights"].get("training_use") != "derived_only" or target.training_use != "metadata_only":
            raise ArchiveRefreshError("archive feature rights do not permit derived metadata")
        if row["label"].get("absence_semantics") != "archive-coverage-gap-not-deletion":
            raise ArchiveRefreshError("archive absence semantics differ")
        if not re.fullmatch(r"CC-MAIN-\d{4}-\d{2}", row["crawl"]):
            raise ArchiveRefreshError("invalid archive crawl identity")
        capture, first, available = (clock(row[k]) for k in ("last_capture_at", "first_capture_at", "available_at"))
        if first > capture or capture > available or available > generated:
            raise ArchiveRefreshError("archive feature clocks are inconsistent")
        if row["model"].get("state") not in {"warming_up", "archive_anomaly", "within_archive_baseline"}:
            raise ArchiveRefreshError("invalid archive anomaly state")
        score = row["model"].get("score")
        if score is not None and (type(score) not in (int, float) or not math.isfinite(score)):
            raise ArchiveRefreshError("invalid archive anomaly score")
        for name in ("unique_urls", "mutated_urls"):
            integer(row["features"].get(name), name)
        for name in ("mutation_rate", "archive_gap_rate"):
            value = row["features"].get(name)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1):
                raise ArchiveRefreshError("invalid bounded archive feature")
        if row["feature_sha256"] in rows:
            raise ArchiveRefreshError("duplicate feature identity")
        rows[row["feature_sha256"]] = row
        if len(rows) > config.limits.feature_rows:
            raise ArchiveRefreshError("archive feature count exceeds limit")
    if not rows:
        raise ArchiveRefreshError("archive features unavailable")
    considered = integer(context["n_events_considered"], "event count")
    events = context["events"]
    if type(events) is not list or len(events) > config.limits.news_events or len(events) > considered:
        raise ArchiveRefreshError("invalid archive event count")
    if integer(context["n_events_contextualized"], "context count") != len(events):
        raise ArchiveRefreshError("archive context count differs")
    projected, identities = [], set()
    for event in events:
        if type(event) is not dict or set(event) != EVENT_KEYS:
            raise ArchiveRefreshError("unsupported private archive event schema")
        if event["relation"] != "context-not-causation" or event["automatic_publication_eligible"] is not False or event["training_label"] != "unreviewed":
            raise ArchiveRefreshError("archive event claims exceed context authority")
        event_id = safe_text(event["event_id"], "event identity")
        version_id = safe_text(event["version_id"], "version identity")
        if (not re.fullmatch(r"event-[a-f0-9]{24}", event_id)
                or not re.fullmatch(r"eventv-[a-f0-9]{24}", version_id)
                or event["event_url"] != f"https://palimpsest.info/news/wire/{event_id}/"):
            raise ArchiveRefreshError("archive event does not match its canonical public wire identity")
        if (event_id, version_id) in identities:
            raise ArchiveRefreshError("duplicate archive event identity")
        identities.add((event_id, version_id))
        event_clock = clock(event["published_at"])
        if event_clock > clock(context["newswire_generated_at"]):
            raise ArchiveRefreshError("archive event is newer than its captured wire")
        topics = event["topics"]
        if type(topics) is not list or len(topics) > 100 or any(not isinstance(x, str) for x in topics):
            raise ArchiveRefreshError("invalid archive topics")
        for topic in topics:
            safe_text(topic, "topic", 80)
        receipts = event["archive_context"]
        if type(receipts) is not list or len(receipts) > len(config.targets):
            raise ArchiveRefreshError("invalid archive receipts")
        retained = []
        for receipt in receipts:
            if type(receipt) is not dict:
                raise ArchiveRefreshError("invalid archive receipt")
            row = rows.get(receipt.get("feature_sha256"))
            if row is None or "palimpsest" not in row["products"] or not set(topics).intersection(row["topics"]):
                raise ArchiveRefreshError("archive receipt lacks a reviewed topic match")
            if clock(row["available_at"]) > event_clock or clock(row["last_capture_at"]) > event_clock:
                raise ArchiveRefreshError("archive receipt was unavailable at event time")
            expected = archive._archive_receipt(row, event_clock)
            if receipt != expected:
                raise ArchiveRefreshError("archive receipt differs from exact derived feature")
            retained.append(expected)
        if not retained:
            continue
        projected.append({"event_id": event_id, "version_id": version_id,
                          "event_url": public_url(event["event_url"]), "published_at": event["published_at"],
                          "topics": topics, "relation": "context-not-causation", "archive_context": retained,
                          "automatic_publication_eligible": False, "family": "newswire", "match_kind": "topic",
                          "public_copy": archive.TOPIC_PUBLIC_COPY,
                          "limitations": ["Archive links are topical context, not causal findings.",
                                          "Monthly archive evidence may substantially predate this event."]})
    document = {"schema_version": archive.CONTEXT_SCHEMA_VERSION, "generated_at": context["generated_at"],
                "status": "partial", "source": "Common Crawl derived host features joined to captured RSS event metadata",
                "scope": "Metadata-only archive topic context; live observation families are not collected by this bridge.",
                "method": archive.CONTEXT_METHOD, "feature_export_sha256": feature_digest,
                "families": {name: "missing" for name in archive.LIVE_ARCHIVE_CONTEXT_FAMILIES},
                "n_events_considered": considered, "n_events_contextualized": len(projected),
                "n_observations_considered": 0, "n_observations_joined": 0, "events": projected,
                "publication_policy": POLICY.copy(),
                "source_snapshot": {"private_context_sha256": hashlib.sha256(context_raw).hexdigest(),
                                    "newswire_generated_at": context["newswire_generated_at"],
                                    "osint_generated_at": context["osint_generated_at"]}}
    document["context_sha256"] = hashlib.sha256(canonical(document)).hexdigest()
    return document


def history(raw: bytes) -> list[dict]:
    if raw and not raw.endswith(b"\n"):
        raise ArchiveRefreshError("archive history has a partial final row")
    result = []
    for line in raw.splitlines():
        row = strict(line)
        if type(row) is not dict or set(row) != {"generated_at", "n_events_contextualized", "n_observations_joined", "context_sha256"}:
            raise ArchiveRefreshError("unsupported archive history row")
        clock(row["generated_at"])
        integer(row["n_events_contextualized"], "history events")
        integer(row["n_observations_joined"], "history joins")
        if not SHA.fullmatch(str(row["context_sha256"])):
            raise ArchiveRefreshError("invalid archive history identity")
        result.append(row)
    return result


def verify_public(document: dict, ledger: Path, history_raw: bytes):
    digest_document(document)
    if set(document) != {"schema_version", "generated_at", "status", "source", "scope", "method",
                         "feature_export_sha256", "families", "n_events_considered", "n_events_contextualized",
                         "n_observations_considered", "n_observations_joined", "events", "publication_policy",
                         "source_snapshot", "context_sha256"} or document["publication_policy"] != POLICY:
        raise ArchiveRefreshError("unsupported public archive projection")
    if document["schema_version"] != archive.CONTEXT_SCHEMA_VERSION or document["method"] != archive.CONTEXT_METHOD:
        raise ArchiveRefreshError("public archive contract differs")
    for event in document["events"]:
        if set(event) != PUBLIC_EVENT_KEYS or event["automatic_publication_eligible"] is not False:
            raise ArchiveRefreshError("public archive event contains unsupported fields")
    entries, _ = sealed.read_ledger_snapshot(ledger)
    if not sealed.verify(entries)[0]:
        raise ArchiveRefreshError("archive pending ledger is broken")
    newest = next((row for row in reversed(entries) if row["source"] == SOURCE), None)
    if newest is None or newest["payload_sha256"] != sealed.payload_digest(document):
        raise ArchiveRefreshError("archive newest seal differs")
    rows = history(history_raw)
    if not rows or rows[-1]["context_sha256"] != document["context_sha256"]:
        raise ArchiveRefreshError("archive latest history differs")


def metadata(path: Path, before: bytes | None):
    info = path.stat() if before is not None else None
    if info and info.st_uid != os.geteuid():
        raise ArchiveRefreshError("archive output owner requires reviewed installation normalization")
    return {"before": io.digest(before), "uid": info.st_uid if info else os.geteuid(),
            "gid": info.st_gid if info else os.getegid(), "mode": stat.S_IMODE(info.st_mode) if info else 0o640,
            "existing": info is not None, "access_acl": io.access_acl(path) if info else None}


def recover_pending(host: Path, state: Path):
    pending = state / "pending"
    if not pending.exists():
        return
    if pending.is_symlink() or not pending.is_dir():
        raise ArchiveRefreshError("unsafe archive pending transaction")
    receipt = strict(io.regular_bytes(pending / "receipt.json"))
    if set(receipt) != {"files"} or set(receipt["files"]) != set(FILES):
        raise ArchiveRefreshError("invalid archive pending receipt")
    for name in FILES:
        expected = receipt["files"][name]
        raw = io.regular_bytes(pending / name)
        if io.digest(raw) != expected["after"]:
            raise ArchiveRefreshError("archive pending candidate differs")
        if io.digest(io.regular_bytes(host / name, optional=True)) not in {expected["before"], expected["after"]}:
            raise ArchiveRefreshError("host advanced during interrupted archive promotion; preserve both")
    doc = strict(io.regular_bytes(pending / LATEST))
    verify_public(doc, pending / LEDGER, io.regular_bytes(pending / HISTORY))
    for name in FILES:
        expected = receipt["files"][name]
        if io.digest(io.regular_bytes(host / name, optional=True)) != expected["after"]:
            io._replace(host / name, io.regular_bytes(pending / name), expected)
    # Retain small transaction identities; completed full candidates are disposable
    # only after all three exact installed byte identities have been verified.
    for name in FILES:
        if io.digest(io.regular_bytes(host / name)) != receipt["files"][name]["after"]:
            raise ArchiveRefreshError("archive installed identity differs")
    completed = state / "completed"
    completed.mkdir(mode=0o700, exist_ok=True)
    sealed.atomic_replace_bytes(completed / (io.digest(canonical(receipt)) + ".json"), canonical(receipt), mode=0o600)
    shutil.rmtree(pending)
    sealed._fsync_directory(state)


def refresh(*, context: Path, features: Path, host: Path, state: Path, data_lock: Path,
            refresh_lock: Path, config_path: Path, now=None, kill_switch=None) -> dict:
    for path in (context, features, host, state, data_lock, refresh_lock):
        if not path.is_absolute() or ".." in path.parts:
            raise ArchiveRefreshError("archive paths must be explicit absolute paths")
    if state.is_relative_to(host) or context.is_relative_to(host) or features.is_relative_to(host):
        raise ArchiveRefreshError("private archive state must remain outside public readings")
    info = state.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ArchiveRefreshError("archive state must be an existing owned private directory")
    kill = kill_switch or KillSwitch()
    with io.lock(refresh_lock, exclusive=True):
        if kill.is_halted():
            raise ArchiveRefreshError("archive refresh halted")
        with io.lock(data_lock, exclusive=True), io.host_ledger_lock(host):
            recover_pending(host, state)
            before_latest = io.regular_bytes(host / LATEST, optional=True)
            before_history = io.regular_bytes(host / HISTORY, optional=True) or b""
            history(before_history)
            before_ledger = io.valid_ledger(host / LEDGER)
            # Fail before computation if the installed service cannot preserve
            # exact ownership, group, mode and access ACLs at replacement.
            for name in FILES:
                metadata(host / name, io.regular_bytes(host / name, optional=True))
        context_raw, feature_raw = io.regular_bytes(context), io.regular_bytes(features)
        observed = now or datetime.now(timezone.utc)
        document = project(context_raw, feature_raw, config=archive.load_config(config_path), now=observed)
        payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False).encode() + b"\n"
        with io.lock(data_lock, exclusive=True), io.host_ledger_lock(host):
            if kill.is_halted():
                raise ArchiveRefreshError("archive refresh halted before admission")
            if io.regular_bytes(context) != context_raw or io.regular_bytes(features) != feature_raw:
                raise ArchiveRefreshError("archive producer inputs changed during projection")
            if io.regular_bytes(host / LATEST, optional=True) != before_latest or (io.regular_bytes(host / HISTORY, optional=True) or b"") != before_history:
                raise ArchiveRefreshError("archive host output changed during projection")
            current_ledger = io.valid_ledger(host / LEDGER)
            if not current_ledger.startswith(before_ledger):
                raise ArchiveRefreshError("archive ledger history changed during projection")
            if before_latest is not None:
                previous = strict(before_latest)
                digest_document(previous)
                if payload == before_latest:
                    verify_public(document, host / LEDGER, before_history)
                    return {"status": "unchanged", "generated_at": document["generated_at"]}
                if clock(document["generated_at"]) <= clock(previous["generated_at"]):
                    raise ArchiveRefreshError("archive producer clock did not advance")
            rows = history(before_history)
            if rows and clock(document["generated_at"]) <= max(clock(row["generated_at"]) for row in rows):
                raise ArchiveRefreshError("archive history clock would regress")
            staging = Path(tempfile.mkdtemp(prefix="candidate-", dir=state))
            try:
                sealed.atomic_replace_bytes(staging / LEDGER, current_ledger, mode=0o600)
                sealed.append_seal(str(staging / LEDGER), SOURCE, document, now=observed)
                row = {name: document[name] for name in ("generated_at", "n_events_contextualized", "n_observations_joined", "context_sha256")}
                sealed.atomic_replace_bytes(staging / HISTORY, before_history + canonical(row) + b"\n", mode=0o600)
                sealed.atomic_replace_bytes(staging / LATEST, payload, mode=0o600)
                receipt = {"files": {}}
                for name in FILES:
                    record = metadata(host / name, io.regular_bytes(host / name, optional=True))
                    record["after"] = io.digest(io.regular_bytes(staging / name))
                    receipt["files"][name] = record
                sealed.atomic_replace_bytes(staging / "receipt.json", canonical(receipt), mode=0o600)
                os.rename(staging, state / "pending")
                sealed._fsync_directory(state)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            recover_pending(host, state)
    return {"status": "refreshed", "coverage_status": document["status"], "generated_at": document["generated_at"],
            "events": document["n_events_contextualized"], "observations_joined": 0,
            "context_sha256": document["context_sha256"], "model_calls": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("context", "features", "host", "state", "data-lock", "refresh-lock", "config"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = refresh(context=args.context, features=args.features, host=args.host, state=args.state,
                         data_lock=args.data_lock, refresh_lock=args.refresh_lock, config_path=args.config)
    except (OSError, ValueError, KeyError, TypeError, archive.ValidationError, archive.LimitExceeded) as error:
        print(json.dumps({"status": "unavailable", "reason": type(error).__name__, "detail": str(error)[:200]}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
