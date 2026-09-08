"""Capture the reviewed official publication watch into a private immutable store."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from collectors.china_publication_watch import DEFAULT_CONFIG, METHOD_VERSION, canonical, extract, fetch_document, load_config, observe, sha
from processors.china_publication_watch import build_document, validate_document
from scripts.china_economic_health_pull import atomic_json

ROOT = Path(__file__).resolve().parents[1]


def immutable(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != raw:
            raise ValueError("private evidence hash collision or changed immutable file")


def collect(store: Path, output: Path, *, config: Path = DEFAULT_CONFIG, fetch=None, now: datetime | None = None) -> dict:
    configuration = load_config(config)
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    store.chmod(0o700)
    def clock():
        return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds").replace("+00:00", "Z")
    with (store / "collection.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = store / "state.json"
        retained = json.loads(state_path.read_text()) if state_path.exists() else {"method_version": METHOD_VERSION, "documents": {}}
        if retained["method_version"] != METHOD_VERSION:
            raise ValueError("private watch state uses another parser version")
        # Authenticate retained last-success bytes before deriving a change.
        configured = {row["id"]: row for row in configuration["documents"]}
        for identity, previous in retained["documents"].items():
            if identity not in configured:
                continue
            if previous.get("url", configured[identity]["url"]) != configured[identity]["url"]:
                raise ValueError("a watch identity was reassigned to another URL")
            if previous.get("raw_sha256"):
                raw = (store / "responses" / (previous["raw_sha256"] + ".bin")).read_bytes()
                text = (store / "texts" / (previous["text_sha256"] + ".txt")).read_bytes()
                if sha(raw) != previous["raw_sha256"] or sha(text) != previous["text_sha256"]:
                    raise ValueError("private watch capture failed digest verification")
                parsed = extract(raw, configured[identity])
                if parsed["text_sha256"] != previous["text_sha256"]:
                    raise ValueError("retained content selector changed without a method migration")
                # Reconstruct derived counters from authenticated retained bytes,
                # never trust editable state counters to create a revision claim.
                previous.update(parsed)
        def request(row):
            try:
                result = (fetch or fetch_document)(row)
                return row, result, None, clock()
            except Exception as exc:
                return row, None, exc, clock()
        public_rows, next_state = [], {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for row, response, error, checked_at in pool.map(request, configuration["documents"]):
                public, state, receipt = observe(row, retained["documents"].get(row["id"]), checked_at=checked_at, response=response, error=error)
                if response is not None and response.body:
                    immutable(store / "responses" / (sha(response.body) + ".bin"), response.body)
                if state.get("text"):
                    immutable(store / "texts" / (state["text_sha256"] + ".txt"), state["text"].encode())
                immutable(store / "receipts" / (public["capture_id"] + ".json"), canonical(receipt))
                public_rows.append(public)
                next_state[row["id"]] = state
        captures = sum(1 for _ in (store / "receipts").glob("*.json"))
        document = build_document(public_rows, configuration, generated_at=clock(), retained_captures=captures)
        atomic_json(state_path, {"method_version": METHOD_VERSION, "documents": next_state})
        immutable(store / "runs" / (sha(canonical(document)) + ".json"), canonical(document))
        atomic_json(output, document)
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path(os.getenv("PALIMPSEST_PUBLICATION_WATCH_STORE", ROOT / "data/private/china-publication-watch")))
    parser.add_argument("--output", type=Path, default=ROOT / "readings/china-publication-watch-latest.json")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        validate_document(json.loads(args.output.read_text()))
    else:
        result = collect(args.store, args.output, config=args.config)
        print(json.dumps({"status": result["status"], "coverage": result["coverage"]}, sort_keys=True))


if __name__ == "__main__":
    main()
