#!/usr/bin/env python3
"""Refresh the retained primary archive and atomically publish metadata only.

This host adapter deliberately has no scratch-store fallback. The installed
scheduler is the sole collector owner; legacy Celery owners must stay stopped.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from datetime import datetime, timezone

from core.evidence_documents import EvidenceDocumentStore
from core.governance import KillSwitch
from core.primary_documents import (
    DEFAULT_CONFIG_PATH,
    PrimaryDocumentError,
    canonical_json_bytes,
    collect_primary_documents,
    load_primary_source_registry,
    strict_json_loads,
    validate_primary_document_index,
)
from core.safe_fetch import safe_fetch_bytes
from scripts.primary_documents_pull import _atomic_write

ROOT = Path(__file__).resolve().parents[1]
MAX_INDEX_BYTES = 8 * 1024 * 1024


def regular(path: Path, *, private: bool = False) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PrimaryDocumentError("host primary path is not a single regular file")
    if private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise PrimaryDocumentError("primary scheduler lock must be owned private mode 0600")
    return info


def read_index(path: Path, registry):
    info = regular(path)
    if info.st_size > MAX_INDEX_BYTES:
        raise PrimaryDocumentError("primary index exceeds the bounded metadata size")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise PrimaryDocumentError("primary index changed while opening")
        raw = handle.read(MAX_INDEX_BYTES + 1)
    if len(raw) > MAX_INDEX_BYTES:
        raise PrimaryDocumentError("primary index exceeds the bounded metadata size")
    value = strict_json_loads(raw, label="retained primary-document index")
    validate_primary_document_index(value, registry=registry)
    return value, (info.st_dev, info.st_ino, hashlib.sha256(raw).hexdigest())


def refresh(*, store: Path, output: Path, lock: Path, config: Path, fetcher=None, now=None, kill_switch=None) -> dict:
    for path in (store, output, lock):
        if not path.is_absolute() or ".." in path.parts:
            raise PrimaryDocumentError("host primary paths must be explicit absolute paths")
        if path.is_relative_to(ROOT):
            raise PrimaryDocumentError("host primary state must be outside the source checkout")
    if store.is_relative_to(output.parent) or output.is_relative_to(store):
        raise PrimaryDocumentError("private primary archive must be outside public readings")
    # Existing owner and strict private-tree validation must run before egress.
    # Never create, relocate, chmod, chown or loosen the retained archive here.
    info = store.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise PrimaryDocumentError("existing primary archive is not a directory")
    registry = load_primary_source_registry(config)
    archive = EvidenceDocumentStore(store, max_document_bytes=registry.max_document_bytes)
    lock_info = regular(lock, private=True)
    descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (lock_info.st_dev, lock_info.st_ino):
            raise PrimaryDocumentError("primary scheduler lock changed while opening")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"schema": "palimpsest.primary-refresh.v1", "status": "already_running"}
        previous, identity = read_index(output, registry)
        kill = kill_switch or KillSwitch()
        if kill.is_halted():
            return {"schema": "palimpsest.primary-refresh.v1", "status": "halted"}
        observed = now or datetime.now(timezone.utc)
        if observed.strftime("%Y-%m-%dT%H:%M:%SZ") < previous["generated_at"]:
            raise PrimaryDocumentError("primary retrieval clock cannot regress")
        proxy = os.getenv("PALIMPSEST_PROXY", "").strip() or None
        transport = fetcher or (lambda url, **kwargs: safe_fetch_bytes(url, proxy=proxy, **kwargs))
        def fetch(url, **kwargs):
            kill.require_live()
            return transport(url, **kwargs)
        result = collect_primary_documents(registry, fetch, archive, now=observed, previous=previous)
        if kill.is_halted():
            # Private commits completed before the halt remain retained; do not
            # replace the index with synthetic transport errors from the gate.
            return {"schema": "palimpsest.primary-refresh.v1", "status": "halted"}
        # Strict index validation excludes raw bytes, private paths and arbitrary
        # keys. A successful private commit precedes every new public vintage.
        validate_primary_document_index(result, registry=registry)
        payload = canonical_json_bytes(result)
        if len(payload) > MAX_INDEX_BYTES:
            raise PrimaryDocumentError("generated primary metadata exceeds size bound")
        _, current_identity = read_index(output, registry)
        if identity != current_identity:
            raise PrimaryDocumentError("primary index advanced during capture; retained private commits")
        if (lock.lstat().st_dev, lock.lstat().st_ino) != (opened.st_dev, opened.st_ino):
            raise PrimaryDocumentError("primary scheduler lock pathname changed")
        _atomic_write(output, payload)
        coverage = result["coverage"]
        status = "sources_unavailable"
        if coverage["successful_sources"] == coverage["registered_sources"]:
            status = "refreshed"
        elif coverage["successful_sources"]:
            status = "partial"
        return {
            "schema": "palimpsest.primary-refresh.v1",
            "status": status,
            "coverage_status": coverage["status"],
            "generated_at": result["generated_at"],
            "registered_sources": coverage["registered_sources"],
            "successful_sources": coverage["successful_sources"],
            "n_documents": result["n_documents"],
            "n_vintages": result["n_vintages"],
            "n_new_vintages": result["n_new_vintages"],
            "metadata_sha256": hashlib.sha256(payload).hexdigest(),
        }
    finally:
        os.close(descriptor)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args(argv)
    result = refresh(store=args.store, output=args.output, lock=args.lock, config=args.config)
    print(json.dumps(result, sort_keys=True))
    return 3 if result["status"] in {"sources_unavailable", "halted"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
