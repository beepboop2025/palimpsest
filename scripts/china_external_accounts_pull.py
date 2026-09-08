"""Collect private SAFE evidence; publish only permission-gated coverage metadata."""
from __future__ import annotations

import argparse
import csv
import fcntl
import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from collectors.china_external_accounts import PARSER_VERSION, SCHEMA, digest, discover, fetch, load_registry, parse_workbook
from core.safe_fetch import FetchError
from processors.china_external_accounts import build_private_analysis, compare_vintages, public_projection
from scripts.china_economic_health_pull import atomic_bytes, atomic_json

ROOT = Path(__file__).resolve().parents[1]


def _error_type(exc: Exception) -> str:
    # Public errors are an enum. Detailed messages remain in private receipts.
    for kind in (FetchError, ValueError, OSError, RuntimeError):
        if isinstance(exc, kind):
            return kind.__name__
    return "RuntimeError"


def _private_path(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _immutable(path: Path, raw: bytes) -> None:
    _private_path(path.parent)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("immutable SAFE evidence changed")
    else:
        atomic_bytes(path, raw)
        path.chmod(0o600)


def export_csv(captures: list[dict]) -> bytes:
    stream = io.StringIO(newline="")
    columns = ["capture_id", "source_url", "source_page", "raw_sha256", "collected_at", "observation_key", "kind", "sheet", "region", "metric", "period", "frequency", "unit", "flow", "source_row", "source_column", "row_label", "raw_value", "value", "status"]
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for capture in captures:
        for row in capture["observations"]:
            value = {**row, "capture_id": capture["capture_id"], "source_url": capture["url"], "source_page": capture["source_page"], "raw_sha256": capture["raw_sha256"], "collected_at": capture["collected_at"]}
            writer.writerow({k: value[k] for k in columns})
    return stream.getvalue().encode()


def collect(*, store: Path, output: Path, transport=fetch, pause_seconds: float = 0.5, max_workbooks: int = 12) -> dict:
    if not 1 <= max_workbooks <= 24 or not 0 <= pause_seconds <= 60:
        raise ValueError("SAFE collection bounds invalid")
    # This store includes restricted numeric tables. It must never sit in a public tree.
    forbidden = {"readings", "public", "dist", "site", "_site"}
    if forbidden.intersection(store.resolve().parts):
        raise ValueError("SAFE private store cannot be a publication directory")
    _private_path(store)
    with (store / "collection.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _collect(store, output, transport, pause_seconds, max_workbooks)


def _collect(store, output, transport, pause_seconds, max_workbooks):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    registry = load_registry()
    manifest_path = store / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"schema": SCHEMA, "captures": {}}
    if manifest.get("schema") != SCHEMA or not isinstance(manifest.get("captures"), dict):
        raise ValueError("SAFE manifest contract mismatch")
    found, failures, receipts, private_failures = [], [], [], []
    for source in registry["sources"]:
        try:
            raw = transport(source["url"])
            _immutable(store / "discovery" / (digest(raw) + ".html"), raw)
            receipts.append({"url": source["url"], "sha256": digest(raw), "checked_at": now, "kind": "discovery"})
            found.extend(discover(raw, source, now=now))
        except (ValueError, OSError, RuntimeError, FetchError) as exc:
            failures.append({"source_id": source["id"], "url": source["url"], "error_type": _error_type(exc)})
            private_failures.append({**failures[-1], "detail": str(exc)[:500]})
        time.sleep(pause_seconds)
    new, checked = 0, 0
    for item in found[:max_workbooks]:
        checked += 1
        try:
            raw = transport(item["url"])
            _immutable(store / "raw" / (digest(raw) + ".xlsx"), raw)
            receipts.append({"url": item["url"], "sha256": digest(raw), "checked_at": now, "kind": "workbook"})
            original = [v["collected_at"] for v in manifest["captures"].values() if v["url"] == item["url"] and v["raw_sha256"] == digest(raw)]
            capture = parse_workbook(raw, item=item, collected_at=min(original) if original else now)
            normalized = (json.dumps(capture, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()
            _immutable(store / "captures" / (capture["capture_id"] + ".json"), normalized)
            if capture["capture_id"] not in manifest["captures"]:
                manifest["captures"][capture["capture_id"]] = {key: capture[key] for key in ("capture_id", "url", "source_id", "kind", "attachment_date", "collected_at", "raw_sha256", "parser_version")}
                manifest["captures"][capture["capture_id"]]["normalized_sha256"] = digest(normalized)
                new += 1
        except (ValueError, OSError, RuntimeError, FetchError) as exc:
            failures.append({"source_id": item["source_id"], "url": item["url"], "error_type": _error_type(exc)})
            private_failures.append({**failures[-1], "detail": str(exc)[:500]})
        time.sleep(pause_seconds)
    atomic_json(manifest_path, manifest)
    manifest_path.chmod(0o600)
    receipt_raw = (json.dumps({"checked_at": now, "receipts": receipts, "failures": private_failures}, sort_keys=True) + "\n").encode()
    _immutable(store / "receipts" / (digest(receipt_raw) + ".json"), receipt_raw)
    # One current attachment vintage per regional year, plus the current BOP.
    candidates = []
    for entry in manifest["captures"].values():
        if entry["parser_version"] != PARSER_VERSION:
            continue
        raw = (store / "captures" / (entry["capture_id"] + ".json")).read_bytes()
        if digest(raw) != entry["normalized_sha256"]:
            raise ValueError("SAFE retained normalized hash mismatch")
        capture = json.loads(raw)
        raw_path = store / "raw" / (entry["raw_sha256"] + ".xlsx")
        if digest(raw_path.read_bytes()) != entry["raw_sha256"]:
            raise ValueError("SAFE retained raw hash mismatch")
        candidates.append(capture)
    selected, revisions = {}, []
    for capture in sorted(candidates, key=lambda c: (c["attachment_date"], c["collected_at"], c["capture_id"])):
        year = capture["sheets"][0]["period_start"][:4] if capture["kind"] == "regional" else "all"
        key = (capture["kind"], year)
        if key in selected:
            revisions.append(compare_vintages(selected[key], capture))
        selected[key] = capture
    captures = [selected[key] for key in sorted(selected)]
    analysis = build_private_analysis(captures)
    analysis["vintage_comparisons"] = revisions
    analysis["revision_baseline"] = "No earlier captured vintage exists yet." if not revisions else "Comparisons use retained local capture clocks."
    csv_raw = export_csv(captures)
    atomic_bytes(store / "observations.csv", csv_raw)
    (store / "observations.csv").chmod(0o600)
    atomic_json(store / "analysis.json", {"schema": SCHEMA + ".private-analysis", "generated_at": now, "rights": registry["rights"],
                "history_export": {"file": "observations.csv", "sha256": digest(csv_raw), "bytes": len(csv_raw)}, "analysis": analysis})
    (store / "analysis.json").chmod(0o600)
    public = public_projection(captures=captures, generated_at=now, failures=failures, checked_workbooks=checked, new_captures=new)
    atomic_json(output, public)
    return public


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path(os.environ.get("PALIMPSEST_EXTERNAL_ACCOUNTS_STORE", ROOT / "data/review/china-external-accounts")))
    parser.add_argument("--output", type=Path, default=ROOT / "readings/china-external-accounts-latest.json")
    parser.add_argument("--max-workbooks", type=int, default=12)
    parser.add_argument("--pause-seconds", type=float, default=0.5)
    args = parser.parse_args(argv)
    result = collect(store=args.store, output=args.output, max_workbooks=args.max_workbooks, pause_seconds=args.pause_seconds)
    print(json.dumps({"coverage": result["coverage"], "collection": result["collection"]}, sort_keys=True))
    return 0 if result["coverage"]["workbooks_retained"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
