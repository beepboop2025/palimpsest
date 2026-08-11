#!/usr/bin/env python3
"""Operate Palimpsest's private Common Crawl evidence lake on the Hetzner node."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from collectors.common_crawl_lake import (
    DEFAULT_CONFIG,
    CommonCrawlLakeError,
    ingest_export,
    probe_exact_url,
    render_duckdb_export_sql,
    retrieve_warc_record,
    warehouse_path,
    write_feature_export,
    write_summary,
)
from processors.archive_context import write_archive_context


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NEWSWIRE = ROOT / "readings" / "newswire-latest.json"
DEFAULT_OSINT = ROOT / "readings" / "osint-china-latest.json"


def _now(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _paths(warehouse: Path | str | None) -> dict[str, Path]:
    root = warehouse_path(warehouse)
    derived = root / "derived"
    return {
        "root": root,
        "database": root / "common-crawl.sqlite3",
        "features": derived / "common-crawl-features.jsonl",
        "summary": derived / "common-crawl-summary.json",
        "context": derived / "archive-news-context.json",
        "training": derived / "story-ranking-features.jsonl",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--warehouse",
        type=Path,
        default=None,
        help="private root, default PALIMPSEST_COMMON_CRAWL_WAREHOUSE_DIR",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="ingest one URL Index export")
    ingest.add_argument("input", type=Path)
    ingest.add_argument("--crawl", help="crawl id when the export omits its crawl column")
    ingest.add_argument("--format", choices=("jsonl", "csv"), default=None)
    ingest.add_argument("--now", type=_now, default=None)

    inbox = subparsers.add_parser("import-inbox", help="idempotently ingest every export in an inbox")
    inbox.add_argument("inbox", type=Path)

    features = subparsers.add_parser("features", help="write the ML-ready temporal feature cut")
    features.add_argument("--output", type=Path, default=None)
    features.add_argument("--as-of", type=_now, default=None)

    summary = subparsers.add_parser("summary", help="write the private aggregate lake summary")
    summary.add_argument("--output", type=Path, default=None)
    summary.add_argument("--now", type=_now, default=None)

    context = subparsers.add_parser("context", help="join RSS, archive, and current OSINT context")
    context.add_argument("--newswire", type=Path, default=DEFAULT_NEWSWIRE)
    context.add_argument("--osint", type=Path, default=DEFAULT_OSINT)
    context.add_argument("--features", type=Path, default=None)
    context.add_argument("--output", type=Path, default=None)
    context.add_argument("--training-output", type=Path, default=None)
    context.add_argument("--now", type=_now, default=None)

    refresh = subparsers.add_parser("refresh", help="rebuild features, summary, and newsroom context")
    refresh.add_argument("--newswire", type=Path, default=DEFAULT_NEWSWIRE)
    refresh.add_argument("--osint", type=Path, default=DEFAULT_OSINT)
    refresh.add_argument("--now", type=_now, default=None)

    sql = subparsers.add_parser("sql", help="render the local DuckDB URL Index export query")
    sql.add_argument("--crawl", required=True)
    sql.add_argument("--index-glob", required=True)
    sql.add_argument("--output", required=True)

    probe = subparsers.add_parser("probe", help="one bounded exact-URL index diagnostic")
    probe.add_argument("url")
    probe.add_argument("--limit", type=int, default=10)

    fetch = subparsers.add_parser("fetch-record", help="retain one selected WARC byte range")
    fetch.add_argument("locator_sha256")
    return parser


def _exports(inbox: Path) -> list[Path]:
    if not inbox.is_dir():
        raise CommonCrawlLakeError(f"inbox is not a directory: {inbox}")
    suffixes = (".jsonl", ".jsonl.gz", ".csv", ".csv.gz")
    files = [path for path in inbox.iterdir() if path.is_file() and path.name.endswith(suffixes)]
    return sorted(files, key=lambda path: path.name)


def _context(args, paths: dict[str, Path]) -> dict:
    feature_path = getattr(args, "features", None) or paths["features"]
    return write_archive_context(
        newswire_path=args.newswire,
        osint_path=args.osint,
        features_path=feature_path,
        context_path=getattr(args, "output", None) or paths["context"],
        training_path=getattr(args, "training_output", None) or paths["training"],
        config_path=args.config,
        now=args.now,
    )


def run(args: argparse.Namespace) -> dict | str:
    paths = _paths(args.warehouse)
    if args.command == "ingest":
        return ingest_export(
            args.input,
            config_path=args.config,
            warehouse=paths["root"],
            crawl=args.crawl,
            input_format=args.format,
            now=args.now,
        )
    if args.command == "import-inbox":
        results = [
            ingest_export(path, config_path=args.config, warehouse=paths["root"])
            for path in _exports(args.inbox)
        ]
        return {
            "collector": "common-crawl-lake",
            "status": "success",
            "files": len(results),
            "new_files": sum(result.get("status") == "success" for result in results),
            "unchanged_files": sum(result.get("status") == "unchanged" for result in results),
            "halted": any(result.get("status") == "halted" for result in results),
        }
    if args.command == "features":
        return write_feature_export(
            paths["database"],
            args.output or paths["features"],
            config_path=args.config,
            as_of=args.as_of,
        )
    if args.command == "summary":
        return write_summary(
            paths["database"],
            args.output or paths["summary"],
            config_path=args.config,
            now=args.now,
        )
    if args.command == "context":
        return _context(args, paths)
    if args.command == "refresh":
        features = write_feature_export(
            paths["database"], paths["features"], config_path=args.config
        )
        summary = write_summary(
            paths["database"], paths["summary"], config_path=args.config, now=args.now
        )
        context = _context(args, paths)
        return {
            "collector": "common-crawl-lake",
            "status": "success",
            "features": features,
            "summary_sha256": summary["summary_sha256"],
            "context": context,
        }
    if args.command == "sql":
        return render_duckdb_export_sql(
            args.crawl, args.index_glob, args.output, config_path=args.config
        )
    if args.command == "probe":
        result = probe_exact_url(args.url, config_path=args.config, limit=args.limit)
        return {
            "collector": result["collector"],
            "status": result["status"],
            "collection": result["collection"],
            "url_sha256": result["url_sha256"],
            "records": len(result["records"]),
            "absence_semantics": result["absence_semantics"],
        }
    if args.command == "fetch-record":
        return retrieve_warc_record(
            args.locator_sha256,
            config_path=args.config,
            warehouse=paths["root"],
        )
    raise AssertionError(f"unhandled command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except (CommonCrawlLakeError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"collector": "common-crawl-lake", "status": "failed", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    if isinstance(result, str):
        print(result, end="" if result.endswith("\n") else "\n")
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
