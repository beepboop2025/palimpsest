"""Dry-run-first repair for the audited Eastmoney CensorWatch capture incident.

This utility is intentionally narrower than a general migration framework.  It:

* repairs missing/old-fabricated post URLs only when an immutable raw capture
  contains one unambiguous ``data-postid -> href`` mapping on an exact allowed
  HTTPS Eastmoney host;
* quarantines (never deletes) archived HTTP-200 validation shells only when the
  audited three-part predicate matches;
* converts explicitly selected false velocity snapshots into abstentions;
* clears derived Redis caches after a successful database commit.

The default mode writes a dry-run manifest and changes no database, archive, or
Redis state.  Applying requires both ``--apply`` and that existing manifest.
The raw captures and collection logs are read-only in both modes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bs4 import BeautifulSoup

from censorwatch.classifier import is_eastmoney_validation_shell
from censorwatch.collectors.eastmoney_guba import EastmoneyGubaCollector

SCHEMA_VERSION = "palimpsest.censorwatch-eastmoney-repair.v1"
SOURCE = "eastmoney_guba"
REDIS_KEYS = ("censorwatch:velocity:latest", "health:eastmoney_guba")
ABSTENTION_SCOPE = "abstain:invalid-eastmoney-capture-repair"
_FABRICATED_URL = re.compile(
    r"^https://guba\.eastmoney\.com/news,(?P<post_id>\d+)\.html$"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _plan_digest(plan: dict[str, Any]) -> str:
    unsigned = dict(plan)
    unsigned.pop("plan_sha256", None)
    return _sha256_bytes(_canonical_bytes(unsigned))


def _result_digest(result: dict[str, Any]) -> str:
    unsigned = dict(result)
    unsigned.pop("result_sha256", None)
    return _sha256_bytes(_canonical_bytes(unsigned))


def _atomic_json(path: Path, value: dict[str, Any], *, exclusive: bool) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive and path.exists():
        raise FileExistsError(f"refusing to overwrite manifest: {path}")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        if exclusive:
            # Hard-link creation is atomic and fails if a reviewed manifest has
            # appeared since the pre-check; it can never replace that file.
            os.link(tmp, path)
            tmp.unlink()
        else:
            os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _strict_json(path: Path) -> dict[str, Any]:
    def reject_duplicate(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"duplicate JSON key: {key}")
            out[key] = value
        return out

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate)
    if not isinstance(value, dict):
        raise ValueError("repair manifest must be a JSON object")
    return value


def is_old_fabricated_url(url: str | None, post_id: str) -> bool:
    """Identify only the exact URL shape produced by the removed fallback."""
    if not isinstance(url, str):
        return False
    match = _FABRICATED_URL.fullmatch(url.strip())
    return bool(match and match.group("post_id") == str(post_id))


def _raw_records(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item
    elif isinstance(value, dict):
        # BaseCollector writes a top-level list, but accepting these two explicit
        # wrappers keeps the reader compatible with copied immutable envelopes.
        for key in ("records", "pages"):
            nested = value.get(key)
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        yield item


def build_raw_url_index(raw_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read immutable raw JSON and return unambiguous post URL evidence.

    Conflicting hrefs for the same post ID are reported and excluded.  A repair
    can therefore never choose between competing raw observations heuristically.
    """
    root = raw_dir.resolve()
    candidates: dict[str, dict[str, list[dict[str, str]]]] = {}
    files_read = files_invalid = 0

    for path in sorted(root.rglob("*.json")):
        if not path.is_file():
            continue
        try:
            raw_bytes = path.read_bytes()
            payload = json.loads(raw_bytes)
        except (OSError, UnicodeError, json.JSONDecodeError):
            files_invalid += 1
            continue
        files_read += 1
        file_rel = str(path.relative_to(root))
        file_hash = _sha256_bytes(raw_bytes)
        for record in _raw_records(payload):
            html = record.get("html")
            if not isinstance(html, str):
                continue
            soup = BeautifulSoup(html, "html.parser")
            anchors = []
            for row in soup.select("tr.listitem"):
                anchor = row.select_one(
                    "td:nth-of-type(3) a[data-postid][href]"
                )
                if anchor is not None:
                    anchors.append(anchor)
            for anchor in anchors:
                post_id = str(anchor.get("data-postid") or "").strip()
                href = anchor.get("href")
                url = EastmoneyGubaCollector._resolve_post_url(href)
                if not post_id or not url:
                    continue
                evidence = {
                    "raw_file": file_rel,
                    "raw_sha256": file_hash,
                    "captured_href": str(href),
                }
                by_url = candidates.setdefault(post_id, {})
                if evidence not in by_url.setdefault(url, []):
                    by_url[url].append(evidence)

    index: dict[str, dict[str, Any]] = {}
    conflicts: dict[str, list[str]] = {}
    for post_id, by_url in sorted(candidates.items()):
        if len(by_url) != 1:
            conflicts[post_id] = sorted(by_url)
            continue
        url = next(iter(by_url))
        index[post_id] = {"url": url, "evidence": by_url[url]}

    report = {
        "raw_root": str(root),
        "files_read": files_read,
        "files_invalid": files_invalid,
        "unambiguous_mappings": len(index),
        "conflicts": conflicts,
    }
    return index, report


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_archive_path(value: str, archive_root: Path) -> Path:
    """Resolve stored absolute paths and the two historical relative forms."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    cwd_candidate = path.resolve()
    if _inside(cwd_candidate, archive_root):
        return cwd_candidate
    return (archive_root / path).resolve()


def _archive_shell_action(
    row: Any,
    *,
    archive_root: Path,
    quarantine_run_root: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    if not row.archive_path:
        return None, None
    source_dir = _resolve_archive_path(str(row.archive_path), archive_root)
    if not _inside(source_dir, archive_root):
        return None, "archive_path_outside_root"
    page = source_dir / "page.html"
    try:
        body = page.read_bytes()
    except FileNotFoundError:
        return None, "archive_page_missing"
    except OSError:
        return None, "archive_page_unreadable"

    # The predicate is intentionally explicit in the manifest as well as shared
    # with the live classifier.  All three booleans must be true.
    low = body.lower()
    predicate = {
        "bytes_lt_10240": len(body) < 10 * 1024,
        "contains_validate_js": b"validate.js" in low,
        "contains_validate_css": b"validate.css" in low,
    }
    if not all(predicate.values()) or not is_eastmoney_validation_shell(body):
        return None, None

    destination = (
        quarantine_run_root / SOURCE / f"{row.post_id}--row-{row.id}"
    ).resolve()
    return {
        "row_id": int(row.id),
        "post_id": str(row.post_id),
        "from_archive_path": str(source_dir),
        "to_quarantine_path": str(destination),
        "page_bytes": len(body),
        "page_sha256": _sha256_bytes(body),
        "predicate": predicate,
    }, None


def create_plan(
    session,
    *,
    raw_dir: Path,
    archive_dir: Path,
    quarantine_dir: Path,
    false_snapshot_ids: list[int],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a read-only repair plan from current DB/filesystem/raw evidence."""
    from censorwatch.models import CensoredPost, DeletionVelocitySnapshot, PostDeletion

    now = now or _utc_now()
    run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    archive_root = archive_dir.resolve()
    quarantine_run_root = (quarantine_dir.resolve() / run_id).resolve()
    raw_index, raw_report = build_raw_url_index(raw_dir)

    confirmed_deletions = (
        session.query(PostDeletion)
        .filter(PostDeletion.source == SOURCE)
        .count()
    )
    if confirmed_deletions:
        raise RuntimeError(
            f"refusing automated repair: {confirmed_deletions} confirmed Eastmoney "
            "deletion row(s) require a separate evidence review"
        )

    posts = (
        session.query(CensoredPost)
        .filter(CensoredPost.source == SOURCE)
        .order_by(CensoredPost.id.asc())
        .all()
    )
    url_repairs: list[dict[str, Any]] = []
    quarantines: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    substantive_archives_kept = 0
    rows_without_archive = 0

    for row in posts:
        current_url = row.url
        needs_url = not current_url or is_old_fabricated_url(current_url, str(row.post_id))
        if needs_url:
            raw_match = raw_index.get(str(row.post_id))
            if raw_match and raw_match["url"] != current_url:
                url_repairs.append({
                    "row_id": int(row.id),
                    "post_id": str(row.post_id),
                    "from_url": current_url,
                    "to_url": raw_match["url"],
                    "raw_evidence": raw_match["evidence"],
                })
            else:
                unresolved.append({
                    "row_id": int(row.id),
                    "post_id": str(row.post_id),
                    "issue": "no_unambiguous_immutable_raw_url_mapping",
                })

        if not row.archive_path:
            rows_without_archive += 1
        action, issue = _archive_shell_action(
            row, archive_root=archive_root, quarantine_run_root=quarantine_run_root
        )
        if action:
            quarantines.append(action)
        elif issue:
            unresolved.append({
                "row_id": int(row.id), "post_id": str(row.post_id), "issue": issue
            })
        elif row.archive_path:
            substantive_archives_kept += 1

    snapshot_actions: list[dict[str, Any]] = []
    requested_ids = sorted(set(false_snapshot_ids))
    snapshots = []
    if requested_ids:
        snapshots = (
            session.query(DeletionVelocitySnapshot)
            .filter(DeletionVelocitySnapshot.id.in_(requested_ids))
            .order_by(DeletionVelocitySnapshot.id.asc())
            .all()
        )
        found = {int(snapshot.id) for snapshot in snapshots}
        missing = set(requested_ids) - found
        if missing:
            raise RuntimeError(f"false snapshot id(s) not found: {sorted(missing)}")
    for snapshot in snapshots:
        snapshot_actions.append({
            "snapshot_id": int(snapshot.id),
            "generated_at": (
                snapshot.generated_at.isoformat() if snapshot.generated_at else None
            ),
            "before": {
                "n_deletions": snapshot.n_deletions,
                "n_terms": snapshot.n_terms,
                "top_term": snapshot.top_term,
                "top_velocity": snapshot.top_velocity,
                "ranked": snapshot.ranked,
                "scope": snapshot.scope,
            },
            "after": {
                "n_deletions": None,
                "n_terms": 0,
                "top_term": None,
                "top_velocity": None,
                "ranked": [],
                "scope": ABSTENTION_SCOPE,
            },
        })

    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry-run",
        "run_id": run_id,
        "generated_at": now.isoformat(),
        "source": SOURCE,
        "safety": {
            "raw_captures": "read-only",
            "collection_logs": "untouched",
            "archives": "quarantine-not-delete",
            "url_authority": "unambiguous immutable raw data-postid/href only",
            "shell_predicate": "bytes<10240 AND validate.js AND validate.css",
        },
        "preconditions": {
            "confirmed_deletions": confirmed_deletions,
            "post_rows": len(posts),
        },
        "paths": {
            "archive_root": str(archive_root),
            "quarantine_run_root": str(quarantine_run_root),
        },
        "raw_evidence": raw_report,
        "actions": {
            "url_repairs": url_repairs,
            "archive_quarantines": quarantines,
            "velocity_abstentions": snapshot_actions,
            "redis_delete_keys": list(REDIS_KEYS),
        },
        "unresolved": unresolved,
        "counts": {
            "post_rows": len(posts),
            "url_repairs": len(url_repairs),
            "archive_quarantines": len(quarantines),
            "substantive_archives_kept": substantive_archives_kept,
            "rows_without_archive": rows_without_archive,
            "velocity_abstentions": len(snapshot_actions),
            "unresolved": len(unresolved),
        },
    }
    plan["plan_sha256"] = _plan_digest(plan)
    return plan


def validate_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported repair manifest schema")
    if plan.get("mode") != "dry-run":
        raise ValueError("apply requires an unmodified dry-run manifest")
    if plan.get("source") != SOURCE:
        raise ValueError("repair manifest source mismatch")
    if plan.get("plan_sha256") != _plan_digest(plan):
        raise ValueError("repair manifest digest mismatch")
    actions = plan.get("actions")
    if not isinstance(actions, dict):
        raise ValueError("repair manifest actions missing")


def _current_snapshot(snapshot: Any) -> dict[str, Any]:
    return {
        "n_deletions": snapshot.n_deletions,
        "n_terms": snapshot.n_terms,
        "top_term": snapshot.top_term,
        "top_velocity": snapshot.top_velocity,
        "ranked": snapshot.ranked,
        "scope": snapshot.scope,
    }


def apply_plan(session, plan: dict[str, Any], *, redis_url: str) -> dict[str, Any]:
    """Revalidate and apply one signed dry-run plan, returning an audit result."""
    from censorwatch.models import CensoredPost, DeletionVelocitySnapshot, PostDeletion

    validate_plan(plan)
    if session.query(PostDeletion).filter(PostDeletion.source == SOURCE).count() != 0:
        raise RuntimeError("confirmed Eastmoney deletions appeared after dry-run; aborting")
    current_post_count = (
        session.query(CensoredPost).filter(CensoredPost.source == SOURCE).count()
    )
    if current_post_count != int(plan["preconditions"]["post_rows"]):
        raise RuntimeError("Eastmoney post corpus changed after dry-run; generate a new plan")

    actions = plan["actions"]
    current_raw_index, _ = build_raw_url_index(Path(plan["raw_evidence"]["raw_root"]))
    for action in actions.get("url_repairs", []):
        current = current_raw_index.get(action["post_id"])
        if not current or current["url"] != action["to_url"]:
            raise RuntimeError(
                f"immutable raw URL evidence changed since dry-run: {action['post_id']}"
            )
        current_evidence = {
            (item["raw_file"], item["raw_sha256"], item["captured_href"])
            for item in current["evidence"]
        }
        planned_evidence = {
            (item["raw_file"], item["raw_sha256"], item["captured_href"])
            for item in action["raw_evidence"]
        }
        if not planned_evidence or not planned_evidence.issubset(current_evidence):
            raise RuntimeError(
                f"raw capture fingerprint changed since dry-run: {action['post_id']}"
            )

    moved: list[tuple[Path, Path]] = []
    try:
        for action in actions.get("url_repairs", []):
            row = (
                session.query(CensoredPost)
                .filter(CensoredPost.id == int(action["row_id"]))
                .filter(CensoredPost.source == SOURCE)
                .one()
            )
            if row.url != action.get("from_url") or str(row.post_id) != action["post_id"]:
                raise RuntimeError(f"URL row changed since dry-run: {action['row_id']}")
            row.url = action["to_url"]

        for action in actions.get("archive_quarantines", []):
            row = (
                session.query(CensoredPost)
                .filter(CensoredPost.id == int(action["row_id"]))
                .filter(CensoredPost.source == SOURCE)
                .one()
            )
            source = Path(action["from_archive_path"]).resolve()
            destination = Path(action["to_quarantine_path"]).resolve()
            archive_root = Path(plan["paths"]["archive_root"]).resolve()
            quarantine_root = Path(plan["paths"]["quarantine_run_root"]).resolve()
            if not _inside(source, archive_root) or not _inside(destination, quarantine_root):
                raise RuntimeError(f"manifest path escaped allowed roots: {action['row_id']}")
            current_archive = (
                _resolve_archive_path(str(row.archive_path), archive_root)
                if row.archive_path is not None else None
            )
            if current_archive != source:
                raise RuntimeError(f"archive row changed since dry-run: {action['row_id']}")
            body = (source / "page.html").read_bytes()
            if _sha256_bytes(body) != action["page_sha256"]:
                raise RuntimeError(f"archive bytes changed since dry-run: {action['row_id']}")
            if not is_eastmoney_validation_shell(body):
                raise RuntimeError(f"archive no longer matches shell predicate: {action['row_id']}")
            if destination.exists():
                raise RuntimeError(f"quarantine destination already exists: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            moved.append((source, destination))
            row.archive_path = None

        for action in actions.get("velocity_abstentions", []):
            snapshot = (
                session.query(DeletionVelocitySnapshot)
                .filter(DeletionVelocitySnapshot.id == int(action["snapshot_id"]))
                .one()
            )
            if _current_snapshot(snapshot) != action["before"]:
                raise RuntimeError(
                    f"velocity snapshot changed since dry-run: {action['snapshot_id']}"
                )
            for key, value in action["after"].items():
                setattr(snapshot, key, value)

        session.commit()
    except Exception:
        session.rollback()
        # Best-effort filesystem rollback.  Nothing is deleted either way.
        for source, destination in reversed(moved):
            if destination.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), str(source))
        raise

    redis_deleted = None
    redis_error = None
    try:
        import redis

        client = redis.from_url(redis_url, decode_responses=True)
        try:
            redis_deleted = client.delete(*actions.get("redis_delete_keys", REDIS_KEYS))
        finally:
            client.close()
    except Exception as exc:  # DB/filesystem repair remains valid; report cache failure loudly.
        redis_error = f"{type(exc).__name__}: {exc}"

    result = {
        "schema_version": SCHEMA_VERSION,
        "mode": "applied",
        "run_id": plan["run_id"],
        "applied_at": _utc_now().isoformat(),
        "source_plan_sha256": plan["plan_sha256"],
        "counts": plan["counts"],
        "redis_deleted": redis_deleted,
        "redis_error": redis_error,
        "quarantine_run_root": plan["paths"]["quarantine_run_root"],
    }
    result["result_sha256"] = _result_digest(result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True,
                        help="dry-run manifest to create, or existing manifest to apply")
    parser.add_argument("--apply", action="store_true",
                        help="apply the exact existing dry-run manifest")
    parser.add_argument("--result-manifest", type=Path,
                        help="required with --apply; new applied-result manifest")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    parser.add_argument("--raw-dir", type=Path,
                        default=Path(os.getenv("RAW_DATA_DIR", "./data/raw")) / SOURCE)
    parser.add_argument("--archive-dir", type=Path,
                        default=Path(os.getenv("CENSORWATCH_ARCHIVE_DIR", "./data/censorwatch/archive")))
    parser.add_argument("--quarantine-dir", type=Path,
                        default=Path(os.getenv("CENSORWATCH_QUARANTINE_DIR", "./data/censorwatch/quarantine")))
    parser.add_argument("--false-snapshot-id", type=int, action="append", default=[],
                        help="explicit false velocity snapshot id (repeatable)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.database_url:
        raise SystemExit("DATABASE_URL or --database-url is required")
    # api.database constructs its engine at import time.
    os.environ["DATABASE_URL"] = args.database_url
    from api.database import SessionLocal

    session = SessionLocal()
    try:
        if args.apply:
            if os.getenv("CENSORWATCH_ENABLED", "").strip().lower() in {
                "1", "true", "yes", "on"
            }:
                raise SystemExit("refusing apply while CENSORWATCH_ENABLED is true")
            if args.result_manifest is None:
                raise SystemExit("--result-manifest is required with --apply")
            plan = _strict_json(args.manifest.resolve())
            result = apply_plan(session, plan, redis_url=args.redis_url)
            _atomic_json(args.result_manifest, result, exclusive=True)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            if result.get("redis_error"):
                print("WARNING: Redis cache clearing failed; clear listed keys manually", file=sys.stderr)
                return 2
            return 0

        if args.result_manifest is not None:
            raise SystemExit("--result-manifest is only valid with --apply")
        plan = create_plan(
            session,
            raw_dir=args.raw_dir,
            archive_dir=args.archive_dir,
            quarantine_dir=args.quarantine_dir,
            false_snapshot_ids=args.false_snapshot_id,
        )
        _atomic_json(args.manifest, plan, exclusive=True)
        print(json.dumps(plan["counts"], ensure_ascii=False, indent=2, sort_keys=True))
        print(f"DRY RUN ONLY: review {args.manifest.resolve()} before applying")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
