"""Collect EU mirror trade with immutable private captures and a public CSV."""
from __future__ import annotations

import argparse
import csv
import fcntl
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collectors.china_mirror_trade import PARSER_VERSION, build_url, clock, digest, fetch_bytes, parse_response, source_policy
from processors.china_mirror_trade import project, publication_source_group
from scripts.china_economic_health_pull import atomic_json

ROOT = Path(__file__).resolve().parents[1]
CSV_FIELDS = "reporter partner product product_label flow period value_eur weight_kg value_eur_status weight_kg_status value_eur_flag weight_kg_flag snapshot_id source_updated_at collected_at".split()
MAX_STORE_BYTES = 512 * 1024 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def jobs(config: dict) -> list[str]:
    if config.get("schema") != "palimpsest.china-mirror-trade-config.v1" or not 1 <= config["batch_products"] <= 30:
        raise ValueError("unsupported mirror-trade configuration")
    # 77 is reserved; this Eurostat dataset does not disseminate chapter 98.
    chapters = ["TOTAL"] + [f"{n:02}" for n in range(1, 100) if n not in {77, 98}]
    size = config["batch_products"]
    result = [build_url("EU27_2020", partner, chapters[start:start + size], config["start"])
              for partner in config["aggregate_partners"] for start in range(0, len(chapters), size)]
    result += [build_url("EU27_2020", "CN", config["strategic_products"], config["start"])]
    result += [build_url(reporter, "CN", config["member_products"], config["start"]) for reporter in config["member_reporters"]]
    if len(set(result)) != len(result) or len(result) > 40:
        raise ValueError("duplicate or excessive collection jobs")
    coordinates = set()
    for url in result:
        query = source_policy(url)
        for product in query["product"]:
            key = (query["reporter"][0], query["partner"][0], product)
            if key in coordinates:
                raise ValueError("overlapping collection product dimensions")
            coordinates.add(key)
    return result


def immutable(path: Path, raw: bytes) -> None:
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("immutable mirror evidence differs")
        return
    with path.open("xb") as handle:
        handle.write(raw)


def save_capture(store: Path, *, raw: bytes, source_url: str, collected_at: str) -> tuple[dict, list[dict]]:
    identity = digest({"source_url": source_url, "raw_sha256": digest(raw), "parser_version": PARSER_VERSION})
    receipt_path = store / "receipts" / (identity + ".json")
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        collected_at = receipt["collected_at"]
    snapshot, rows = parse_response(raw, source_url=source_url, collected_at=collected_at)
    if receipt_path.exists() and snapshot != receipt:
        raise ValueError("retained mirror acquisition receipt differs")
    if not (store / "raw" / (snapshot["raw_sha256"] + ".json")).exists():
        stored_bytes = sum(path.stat().st_size for folder in ("raw", "receipts") for path in (store / folder).glob("*.json"))
        if stored_bytes + len(raw) + 8192 > MAX_STORE_BYTES:
            raise ValueError("private mirror archive capacity reached; retain last good data")
    immutable(store / "raw" / (snapshot["raw_sha256"] + ".json"), raw)
    immutable(receipt_path, (json.dumps(snapshot, sort_keys=True, indent=2) + "\n").encode())
    return snapshot, rows


def load_capture(store: Path, snapshot_id: str, source_url: str) -> tuple[dict, list[dict]]:
    if len(snapshot_id) != 64 or any(c not in "0123456789abcdef" for c in snapshot_id):
        raise ValueError("invalid retained snapshot id")
    receipt = json.loads((store / "receipts" / (snapshot_id + ".json")).read_text())
    if receipt["source_url"] != source_url or receipt["snapshot_id"] != snapshot_id:
        raise ValueError("retained source identity differs")
    raw_sha = receipt["raw_sha256"]
    if len(raw_sha) != 64 or any(c not in "0123456789abcdef" for c in raw_sha):
        raise ValueError("invalid retained raw hash")
    raw = (store / "raw" / (raw_sha + ".json")).read_bytes()
    if digest(raw) != raw_sha:
        raise ValueError("retained mirror raw hash mismatch")
    return save_capture(store, raw=raw, source_url=source_url, collected_at=receipt["collected_at"])


def export_csv(rows: list[dict]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode()


def reuse_unchanged_cache(snapshots: list[dict], probe: dict, *, last_full_check: str | None, checked_at: str) -> bool:
    """Daily small update check; re-fetch historical vintages at least weekly."""
    if not snapshots or not last_full_check:
        return False
    elapsed = clock(checked_at) - clock(last_full_check)
    return (timedelta(0) <= elapsed < timedelta(days=7)
            and all(snapshot["source_updated_at"] == probe["source_updated_at"] for snapshot in snapshots))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/china_mirror_trade.json")
    parser.add_argument("--store", type=Path, default=Path(os.environ.get("PALIMPSEST_CHINA_MIRROR_TRADE_STORE", ROOT / "data/review/china-mirror-trade")))
    parser.add_argument("--output", type=Path, default=ROOT / "readings/china-mirror-trade-latest.json")
    parser.add_argument("--history-output", type=Path)
    parser.add_argument("--reparse-retained", action="store_true")
    parser.add_argument("--retry-missing", action="store_true", help="Reuse verified retained batches and collect only missing batches")
    parser.add_argument("--full-refresh", action="store_true", help="Recheck every historical batch even if the dataset update clock is unchanged")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--workers", type=int, choices=[1, 2, 3], default=2)
    args = parser.parse_args(argv)
    history_path = args.history_output or args.output.with_name("china-mirror-trade-history.csv")
    if args.check:
        document = json.loads(args.output.read_text())
        publication_source_group(document)
        if digest(history_path.read_bytes()) != document["history_export"]["sha256"]:
            raise ValueError("public mirror history hash mismatch")
        print(json.dumps({"status": "ok", "coverage": document["coverage"]}))
        return 0
    config = json.loads(args.config.read_text())
    urls = jobs(config)
    args.store.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.store, 0o700)
    for name in ("raw", "receipts"):
        (args.store / name).mkdir(exist_ok=True, mode=0o700)
    with (args.store / "refresh.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        index_path = args.store / "index.json"
        index = json.loads(index_path.read_text()) if index_path.exists() else {}
        snapshots, all_rows, failures = {}, {}, []
        def fetch(url):
            raw = fetch_bytes(url)
            return raw, now()
        def accept(url, snapshot, rows):
            snapshots[url] = snapshot
            for row in rows:
                key = tuple(row[k] for k in ("reporter", "partner", "product", "flow", "period"))
                if key in all_rows:
                    raise ValueError("overlapping mirror collection jobs")
                all_rows[key] = row
            index[url] = snapshot["snapshot_id"]
            atomic_json(index_path, index)
        if args.reparse_retained:
            for url in urls:
                snapshot, rows = load_capture(args.store, index[url], url)
                accept(url, snapshot, rows)
        else:
            reuse_cache = args.retry_missing
            complete_cache = all(url in index for url in urls)
            if not args.full_refresh and not args.retry_missing and complete_cache:
                probe_url = build_url("EU27_2020", "CN", ["TOTAL"], now()[:4] + "-01")
                try:
                    probe_raw, probe_collected = fetch(probe_url)
                    probe, _ = save_capture(args.store, raw=probe_raw, source_url=probe_url, collected_at=probe_collected)
                    full_path = args.store / "last-full-refresh.json"
                    previous_full = json.loads(full_path.read_text())["checked_at"] if full_path.exists() else None
                    cached = [load_capture(args.store, index[url], url)[0] for url in urls]
                    reuse_cache = reuse_unchanged_cache(cached, probe, last_full_check=previous_full, checked_at=now())
                    atomic_json(args.store / "dataset-update-check.json", {"checked_at": now(), "source_updated_at": probe["source_updated_at"], "snapshot_id": probe["snapshot_id"], "reused_cache": reuse_cache})
                except Exception as exc:
                    # An unavailable update check does not erase historical evidence.
                    reuse_cache = True
                    failures.append({"source_url": probe_url, "error": type(exc).__name__, "retained": True})
            pending_urls = []
            for url in urls:
                if reuse_cache and url in index:
                    snapshot, rows = load_capture(args.store, index[url], url)
                    accept(url, snapshot, rows)
                else:
                    pending_urls.append(url)
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                pending = {executor.submit(fetch, url): url for url in pending_urls}
                for future in as_completed(pending):
                    url = pending[future]
                    try:
                        raw, collected_at = future.result()
                        snapshot, rows = save_capture(args.store, raw=raw, source_url=url, collected_at=collected_at)
                        accept(url, snapshot, rows)
                        print(json.dumps({"captured": len(snapshots), "expected": len(urls), "rows": len(rows), "raw_bytes": len(raw)}), flush=True)
                    except Exception as exc:
                        retained = False
                        if url in index:
                            snapshot, rows = load_capture(args.store, index[url], url)
                            accept(url, snapshot, rows)
                            retained = True
                        failures.append({"source_url": url, "error": type(exc).__name__, "retained": retained})
                        print(json.dumps({"failed": len(failures), "error": type(exc).__name__, "retained": retained}), flush=True)
            if len(pending_urls) == len(urls) and not failures:
                atomic_json(args.store / "last-full-refresh.json", {"checked_at": now()})
        if not snapshots:
            raise ValueError("no mirror trade captured; existing public output retained")
        rows = [all_rows[key] for key in sorted(all_rows)]
        csv_raw = export_csv(rows)
        document = project(rows, [snapshots[url] for url in sorted(snapshots)], generated_at=now(), failures=failures, expected_batches=len(urls))
        document["history_export"] = {"path": "/readings/china-mirror-trade-history.csv", "sha256": digest(csv_raw), "rows": len(rows), "numeric_cells": document["coverage"]["numeric_cells"]}
        publication_source_group(document)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = history_path.with_name(history_path.name + ".tmp")
        temporary.write_bytes(csv_raw)
        temporary.replace(history_path)
        atomic_json(args.output, document)
        atomic_json(args.store / "latest-receipt.json", {"generated_at": document["generated_at"], "coverage": document["coverage"], "collection": document["collection"], "output_sha256": digest(args.output.read_bytes()), "history_sha256": digest(csv_raw)})
        print(json.dumps({"status": document["status"], "coverage": document["coverage"], "history_bytes": len(csv_raw), "findings": len(document["findings"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
