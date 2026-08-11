#!/usr/bin/env python3
"""Run one local-only, metadata-only DuckDB filter into hidden staging."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, ContextManager

from collectors.common_crawl_lake import render_duckdb_export_sql


CRAWL_RE = re.compile(r"CC-MAIN-[0-9]{4}-[0-9]{2}\Z")
MAX_CONFIG_BYTES = 16 * 1024
WAREHOUSE = Path("/var/lib/palimpsest/common-crawl")
DUCKDB_PATH = Path("/usr/local/bin/duckdb")
DUCKDB_VERSION = "1.5.5"
DUCKDB_SHA256_PATH = Path("/etc/palimpsest/duckdb.sha256")
NETWORK_HELPER_PATH = Path(
    "/usr/local/libexec/palimpsest-network-lane/current/network_lane.py"
)
NETWORK_STATE = Path("/var/lib/palimpsest/network-lane")
TARGET_CONFIG = Path(__file__).resolve().parent / "config/common_crawl_targets.json"
MIN_FILTER_FREE_BYTES = 160 * 1024 * 1024 * 1024
FILTER_RECEIPT_SCHEMA = "palimpsest-common-crawl-local-filter/v1"
MAX_PIN_BYTES = 256


class FilterConfigurationError(RuntimeError):
    """The fixed local filter plan is unsafe or malformed."""


class FilterTemporaryError(RuntimeError):
    """The dataset lock is currently owned by a mirror or another filter."""


@dataclass(frozen=True)
class FilterPlan:
    crawl: str
    mirror_config: Path
    partition: Path
    warehouse: Path
    spill: Path
    output: Path


def _real_directory(path: Path, *, label: str) -> tuple[Path, os.stat_result]:
    if not path.is_absolute():
        raise FilterConfigurationError(f"{label} must be absolute")
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
        after = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise FilterConfigurationError(f"cannot validate {label}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise FilterConfigurationError(f"{label} must be a real directory")
    if resolved != path:
        raise FilterConfigurationError(f"{label} contains a symlink component")
    if (before.st_dev, before.st_ino, before.st_mode) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
    ):
        raise FilterConfigurationError(f"{label} changed during validation")
    if before.st_mode & stat.S_IWOTH:
        raise FilterConfigurationError(f"{label} must not be world-writable")
    return resolved, after


def _strict_json(payload: bytes) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise FilterConfigurationError("mirror config repeats a key")
            document[key] = value
        return document

    try:
        parsed = json.loads(payload.decode("utf-8"), object_pairs_hook=object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FilterConfigurationError("mirror config is not strict UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise FilterConfigurationError("mirror config must be an object")
    return parsed


def _read_mirror_config(
    path: Path,
    crawl: str,
    *,
    expected_uid: int,
    require_production_path: bool,
    allowed_mirror_parent: Path,
) -> Path:
    expected_path = Path("/etc/palimpsest/common-crawl-mirror") / f"{crawl}.json"
    if require_production_path and path != expected_path:
        raise FilterConfigurationError(f"mirror config must be exactly {expected_path}")
    try:
        resolved = path.resolve(strict=True)
        before = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise FilterConfigurationError(f"cannot validate mirror config: {exc}") from exc
    if resolved != path or stat.S_ISLNK(before.st_mode):
        raise FilterConfigurationError("mirror config contains a symlink component")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        information = os.fstat(descriptor)
        if not stat.S_ISREG(information.st_mode) or information.st_nlink != 1:
            raise FilterConfigurationError("mirror config must be a single regular file")
        if information.st_uid != expected_uid or information.st_mode & (
            stat.S_IWGRP | stat.S_IWOTH
        ):
            raise FilterConfigurationError("mirror config ownership or mode is unsafe")
        if information.st_size <= 0 or information.st_size > MAX_CONFIG_BYTES:
            raise FilterConfigurationError("mirror config size is invalid")
        body = bytearray()
        while chunk := os.read(descriptor, min(4096, MAX_CONFIG_BYTES + 1 - len(body))):
            body.extend(chunk)
            if len(body) > MAX_CONFIG_BYTES:
                raise FilterConfigurationError("mirror config exceeds its size limit")
        payload = bytes(body)
    finally:
        os.close(descriptor)
    document = _strict_json(payload)
    expected_keys = {
        "schema_version",
        "crawl",
        "volume_root",
        "manifest_path",
        "mirror_root",
        "threads",
        "retries",
        "downloader_sha256",
    }
    if set(document) != expected_keys or document.get("schema_version") != 1:
        raise FilterConfigurationError("mirror config has an unsupported shape")
    if document.get("crawl") != crawl or not isinstance(
        document.get("mirror_root"), str
    ):
        raise FilterConfigurationError("mirror config does not match the crawl")
    mirror_root, _ = _real_directory(
        Path(document["mirror_root"]), label="mirror root"
    )
    try:
        relative = mirror_root.relative_to(allowed_mirror_parent)
    except ValueError as exc:
        raise FilterConfigurationError(
            f"mirror root must be below {allowed_mirror_parent}"
        ) from exc
    if relative == Path("."):
        raise FilterConfigurationError("mirror root must be below a dedicated mount")
    return mirror_root


def build_filter_plan(
    crawl: str,
    mirror_config: Path,
    *,
    warehouse: Path = WAREHOUSE,
    expected_config_uid: int = 0,
    require_non_root_volume: bool = True,
    require_production_config_path: bool = True,
    allowed_mirror_parent: Path = Path("/mnt"),
) -> FilterPlan:
    if CRAWL_RE.fullmatch(crawl) is None:
        raise FilterConfigurationError("crawl must match CC-MAIN-YYYY-WW")
    mirror_root = _read_mirror_config(
        mirror_config,
        crawl,
        expected_uid=expected_config_uid,
        require_production_path=require_production_config_path,
        allowed_mirror_parent=allowed_mirror_parent,
    )
    partition, partition_info = _real_directory(
        mirror_root
        / "cc-index/table/cc-main/warc"
        / f"crawl={crawl}"
        / "subset=warc",
        label="mirrored Parquet partition",
    )
    warehouse, warehouse_info = _real_directory(warehouse, label="warehouse")
    if require_non_root_volume and warehouse_info.st_dev == Path("/").stat().st_dev:
        raise FilterConfigurationError("warehouse must use a non-root filesystem")
    if partition_info.st_dev != warehouse_info.st_dev:
        raise FilterConfigurationError("mirror and warehouse must use the same Volume")
    return FilterPlan(
        crawl=crawl,
        mirror_config=mirror_config,
        partition=partition,
        warehouse=warehouse,
        spill=warehouse / "duckdb-spill" / crawl,
        output=warehouse / f".{crawl}.jsonl.gz.staging",
    )


def _read_sha256_pin(path: Path, *, expected_uid: int) -> str:
    try:
        information = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FilterConfigurationError(f"cannot validate DuckDB hash pin: {exc}") from exc
    if (
        resolved != path
        or not stat.S_ISREG(information.st_mode)
        or information.st_nlink != 1
        or information.st_uid != expected_uid
        or information.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not 1 <= information.st_size <= MAX_PIN_BYTES
    ):
        raise FilterConfigurationError("DuckDB hash pin ownership or mode is unsafe")
    try:
        pin = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise FilterConfigurationError(f"cannot read DuckDB hash pin: {exc}") from exc
    if re.fullmatch(r"[0-9a-f]{64}", pin) is None:
        raise FilterConfigurationError("DuckDB hash pin must be lowercase SHA-256")
    return pin


def _validate_duckdb(
    path: Path,
    *,
    expected_uid: int,
    sha256_path: Path,
    expected_pin_uid: int,
) -> dict[str, str]:
    try:
        information = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FilterConfigurationError(f"cannot validate DuckDB: {exc}") from exc
    if resolved != path or not stat.S_ISREG(information.st_mode):
        raise FilterConfigurationError("DuckDB must be a real regular file")
    if information.st_uid != expected_uid or information.st_mode & (
        stat.S_IWGRP | stat.S_IWOTH
    ):
        raise FilterConfigurationError("DuckDB ownership or mode is unsafe")
    if not information.st_mode & stat.S_IXUSR:
        raise FilterConfigurationError("DuckDB is not executable")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    expected_digest = _read_sha256_pin(sha256_path, expected_uid=expected_pin_uid)
    if digest != expected_digest:
        raise FilterConfigurationError("DuckDB does not match its root-owned SHA-256 pin")
    try:
        version = subprocess.run(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            shell=False,
            timeout=10,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FilterConfigurationError(f"cannot identify DuckDB: {exc}") from exc
    if version.returncode != 0 or re.fullmatch(
        rf"v{re.escape(DUCKDB_VERSION)}(?:\s.*)?", version.stdout.strip()
    ) is None:
        raise FilterConfigurationError(
            f"DuckDB must report exact version {DUCKDB_VERSION}"
        )
    return {"path": str(path), "sha256": digest, "version": DUCKDB_VERSION}


def _load_network_helper(path: Path) -> ModuleType:
    try:
        resolved = path.resolve(strict=True)
        information = resolved.lstat()
    except (OSError, RuntimeError) as exc:
        raise FilterConfigurationError(f"cannot validate network-lane helper: {exc}") from exc
    if path == NETWORK_HELPER_PATH:
        current = path.parent
        try:
            current_info = current.lstat()
            target = os.readlink(current)
        except OSError as exc:
            raise FilterConfigurationError(
                f"cannot validate network-lane current link: {exc}"
            ) from exc
        if (
            not stat.S_ISLNK(current_info.st_mode)
            or current_info.st_uid != 0
            or re.fullmatch(r"[0-9a-f]{40}", target) is None
            or resolved != current.parent / target / path.name
        ):
            raise FilterConfigurationError(
                "network-lane current link is not a root-owned revision link"
            )
    elif resolved != path:
        raise FilterConfigurationError("network-lane helper contains a symlink component")
    if (
        not stat.S_ISREG(information.st_mode)
        or information.st_nlink != 1
        or information.st_uid != 0
        or information.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise FilterConfigurationError("network-lane helper ownership or mode is unsafe")
    module_name = "palimpsest_network_lane_runtime"
    specification = importlib.util.spec_from_file_location(module_name, resolved)
    if specification is None or specification.loader is None:
        raise FilterConfigurationError("cannot load network-lane helper")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


@contextmanager
def guarded_completed_mirror(
    plan: FilterPlan,
    *,
    helper_path: Path = NETWORK_HELPER_PATH,
    state_dir: Path = NETWORK_STATE,
):
    """Serialize against mirrors and yield a freshly verified input receipt."""

    helper = _load_network_helper(helper_path)
    try:
        mirror_plan = helper.load_mirror_plan(plan.mirror_config, plan.crawl)
        expected_partition = (
            mirror_plan.mirror_root
            / "cc-index/table/cc-main/warc"
            / f"crawl={plan.crawl}"
            / "subset=warc"
        )
        if expected_partition != plan.partition:
            raise FilterConfigurationError(
                "filter partition does not match the guarded mirror plan"
            )
        with helper.exclusive_dataset(state_dir):
            yield helper.verify_completed_mirror(
                mirror_plan,
                state_dir=state_dir,
                expected_manifest_uid=0,
            )
    except FilterConfigurationError:
        raise
    except Exception as exc:
        lane_error = getattr(helper, "LaneError", ())
        if isinstance(exc, lane_error):
            lane_temporary_error = getattr(helper, "LaneTemporaryError", ())
            if isinstance(exc, lane_temporary_error):
                raise FilterTemporaryError(str(exc)) from exc
            raise FilterConfigurationError(
                f"guarded mirror readiness failed: {exc}"
            ) from exc
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_receipt(directory: Path, document: dict[str, Any]) -> Path:
    directory.mkdir(mode=0o700, exist_ok=True)
    validated, information = _real_directory(directory, label="filter receipt directory")
    if information.st_uid != os.geteuid() or information.st_mode & (
        stat.S_IRWXG | stat.S_IRWXO
    ):
        raise FilterConfigurationError("filter receipt directory ownership is unsafe")
    destination = validated / (
        f"{document['crawl']}-{document['completed_unix_ns']}-{uuid.uuid4().hex}.json"
    )
    temporary = validated / f".{destination.name}.tmp"
    payload = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o640,
    )
    try:
        os.fchmod(descriptor, 0o640)
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)
    os.replace(temporary, destination)
    parent = os.open(
        validated,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return destination


def _read_bundle_revision(path: Path, *, expected_uid: int) -> str:
    try:
        information = path.lstat()
        resolved = path.resolve(strict=True)
        revision = path.read_text(encoding="ascii").strip()
    except (OSError, RuntimeError, UnicodeDecodeError) as exc:
        raise FilterConfigurationError(f"cannot validate filter bundle revision: {exc}") from exc
    if (
        resolved != path
        or not stat.S_ISREG(information.st_mode)
        or information.st_nlink != 1
        or information.st_uid != expected_uid
        or information.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or re.fullmatch(r"[0-9a-f]{40}", revision) is None
    ):
        raise FilterConfigurationError("filter bundle revision ownership or value is unsafe")
    return revision


def run_filter(
    plan: FilterPlan,
    *,
    duckdb_path: Path = DUCKDB_PATH,
    duckdb_sha256_path: Path = DUCKDB_SHA256_PATH,
    expected_duckdb_uid: int = 0,
    expected_pin_uid: int = 0,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    mirror_guard: Callable[[FilterPlan], ContextManager[dict[str, Any]]] | None = None,
    revision_path: Path | None = None,
    expected_revision_uid: int = 0,
) -> int:
    duckdb_receipt = _validate_duckdb(
        duckdb_path,
        expected_uid=expected_duckdb_uid,
        sha256_path=duckdb_sha256_path,
        expected_pin_uid=expected_pin_uid,
    )
    guard = mirror_guard or guarded_completed_mirror
    with guard(plan) as mirror_receipt:
        return _run_filter_locked(
            plan,
            duckdb_path=duckdb_path,
            duckdb_receipt=duckdb_receipt,
            mirror_receipt=mirror_receipt,
            disk_usage=disk_usage,
            revision_path=revision_path
            or Path(__file__).resolve().parent / "REVISION",
            expected_revision_uid=expected_revision_uid,
        )


def _run_filter_locked(
    plan: FilterPlan,
    *,
    duckdb_path: Path,
    duckdb_receipt: dict[str, str],
    mirror_receipt: dict[str, Any],
    disk_usage: Callable[[Path], Any],
    revision_path: Path,
    expected_revision_uid: int,
) -> int:
    started_ns = time.time_ns()
    bundle_revision = _read_bundle_revision(
        revision_path, expected_uid=expected_revision_uid
    )
    if os.path.lexists(plan.output):
        raise FilterConfigurationError(
            "hidden staging output already exists; review or remove it explicitly"
        )
    plan.spill.mkdir(parents=True, mode=0o700, exist_ok=True)
    spill, spill_info = _real_directory(plan.spill, label="DuckDB spill directory")
    if spill_info.st_uid != os.geteuid() or spill_info.st_mode & (
        stat.S_IWGRP | stat.S_IWOTH
    ):
        raise FilterConfigurationError("DuckDB spill ownership or mode is unsafe")
    if any(spill.iterdir()):
        raise FilterConfigurationError(
            "DuckDB spill directory is not empty; review it before retrying"
        )
    free_bytes = int(disk_usage(plan.warehouse).free)
    if free_bytes < MIN_FILTER_FREE_BYTES:
        raise FilterConfigurationError(
            "local filter requires at least 160 GiB free on the warehouse Volume"
        )
    sql = render_duckdb_export_sql(
        plan.crawl,
        str(plan.partition / "*.parquet"),
        str(plan.output),
        temp_directory=spill,
        bulk_volume_root=plan.warehouse,
        config_path=TARGET_CONFIG,
    )
    sql_sha256 = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    completed = subprocess.run(
        [str(duckdb_path)],
        input=sql.encode("utf-8"),
        stdout=None,
        stderr=None,
        check=False,
        shell=False,
        cwd="/",
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
    )
    if completed.returncode != 0:
        if completed.returncode < 0:
            return min(255, 128 + abs(completed.returncode))
        return min(255, completed.returncode)
    try:
        output_info = plan.output.lstat()
    except OSError as exc:
        raise FilterConfigurationError("DuckDB produced no staging output") from exc
    if (
        stat.S_ISLNK(output_info.st_mode)
        or not stat.S_ISREG(output_info.st_mode)
        or output_info.st_nlink != 1
        or output_info.st_uid != os.geteuid()
        or output_info.st_size <= 0
    ):
        raise FilterConfigurationError("DuckDB staging output is unsafe or empty")
    plan.output.chmod(0o640)
    descriptor = os.open(plan.output, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(
        plan.output.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    completed_ns = time.time_ns()
    output_information = plan.output.stat()
    receipt = {
        "schema_version": FILTER_RECEIPT_SCHEMA,
        "status": "hidden-staging-ready-for-review",
        "publication_eligible": False,
        "crawl": plan.crawl,
        "started_unix_ns": started_ns,
        "completed_unix_ns": completed_ns,
        "bundle_revision": bundle_revision,
        "tool": duckdb_receipt,
        "sql_sha256": sql_sha256,
        "input": mirror_receipt,
        "output": {
            "path": str(plan.output),
            "bytes": output_information.st_size,
            "sha256": _sha256_file(plan.output),
        },
        "integrity_limit": (
            "mirror inventory binds paths, sizes, and PAR1 framing rather than "
            "full source-object content hashes"
        ),
    }
    receipt_path = _atomic_receipt(plan.warehouse / ".filter-receipts", receipt)
    print(
        json.dumps(
            {
                "crawl": plan.crawl,
                "output": str(plan.output),
                "output_bytes": output_information.st_size,
                "publication_eligible": False,
                "receipt": str(receipt_path),
                "receipt_sha256": _sha256_file(receipt_path),
                "status": "hidden-staging-ready-for-review",
                "tool": duckdb_receipt,
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crawl", required=True)
    parser.add_argument("--mirror-config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = build_filter_plan(args.crawl, args.mirror_config)
        return run_filter(plan)
    except FilterTemporaryError as exc:
        print(f"Common Crawl local filter temporarily refused: {exc}", file=sys.stderr)
        return 75
    except (FilterConfigurationError, OSError, ValueError) as exc:
        print(f"Common Crawl local filter refused: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
