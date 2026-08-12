#!/usr/bin/env python3
"""Build Palimpsest's human- and machine-readable dataset catalog.

The source file is editorial: names, caveats, collection modes, geographic
scope, and rights cannot be guessed from a JSON filename.  This builder adds
facts that *can* be measured safely from the published artifacts (freshness,
byte size, history rows, and bounded headline counts), then emits three views:

* ``readings/catalog.json``      — compact API used by the Evidence Atlas;
* ``readings/catalog.jsonld``    — schema.org/DCAT discovery metadata;
* ``datapackage.json``           — a Frictionless-style file inventory.

It never reads the private warehouse and never copies raw source material into
the website.  A catalog build therefore cannot accidentally broaden the public
publication boundary.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "public_data_catalog.json"
SITE = "https://palimpsest.info/"
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,63}\Z")
_TIMESTAMP_FIELDS = (
    "generated_at",
    "observed_at",
    "as_of",
    "asof",
    "timestamp",
    "collected_at",
    "updated_at",
    "ts",
)
_TERMINAL_STATES = {"disabled", "gated", "historical", "private-node", "warming"}


def _utc_now() -> datetime:
    """Return a deterministic timestamp when SOURCE_DATE_EPOCH is supplied."""

    raw = os.getenv("SOURCE_DATE_EPOCH", "").strip()
    if raw:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _walk_timestamps(value: Any, *, depth: int = 0) -> Iterable[datetime]:
    """Yield timestamps from a small JSON document without assuming one schema."""

    if depth > 4:
        return
    if isinstance(value, dict):
        for field in _TIMESTAMP_FIELDS:
            parsed = _parse_time(value.get(field))
            if parsed is not None:
                yield parsed
        for child in value.values():
            yield from _walk_timestamps(child, depth=depth + 1)
    elif isinstance(value, list):
        # Latest readings can contain evidence arrays.  Inspecting their first
        # and last items finds the time boundary without walking a huge payload.
        for child in (value[:1] + value[-1:] if len(value) > 1 else value):
            yield from _walk_timestamps(child, depth=depth + 1)


def _duration_seconds(value: str) -> int | None:
    """Parse the deliberately small ISO-8601 cadence subset used by the catalog."""

    match = re.fullmatch(r"P(?:(\d+)W|(\d+)D|(\d+)M)|PT(\d+)H", value)
    if not match:
        return None
    weeks, days, months, hours = match.groups()
    if weeks:
        return int(weeks) * 7 * 86400
    if days:
        return int(days) * 86400
    if months:
        return int(months) * 31 * 86400
    return int(hours) * 3600


def _value_at(document: Any, dotted_path: str) -> Any:
    current = document
    for part in dotted_path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _bounded_count(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return value
    if isinstance(value, (list, dict)):
        return len(value)
    return None


def _safe_repo_path(raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"catalog path must stay inside the repository: {raw!r}")
    resolved = (ROOT / path).resolve()
    if ROOT not in resolved.parents:
        raise ValueError(f"catalog path escapes repository: {raw!r}")
    return resolved


def _line_count(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1024 * 1024), b""))


def _artifact_metadata(spec: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    latest_path = _safe_repo_path(spec.get("latest"))
    history_path = _safe_repo_path(spec.get("history"))
    latest_exists = bool(latest_path and latest_path.is_file())
    history_exists = bool(history_path and history_path.is_file())
    document: dict[str, Any] | None = None
    invalid = False
    if latest_exists and latest_path is not None:
        try:
            loaded = json.loads(latest_path.read_text(encoding="utf-8"))
            document = loaded if isinstance(loaded, dict) else None
            invalid = document is None
        except (OSError, UnicodeError, ValueError, TypeError):
            invalid = True

    observed = max(_walk_timestamps(document), default=None) if document else None
    age_seconds = max(0, int((now - observed).total_seconds())) if observed else None
    cadence_seconds = _duration_seconds(str(spec["cadence"]))
    configured = str(spec["status"])
    if configured in _TERMINAL_STATES:
        evidence_state = configured
    elif not latest_exists:
        evidence_state = "pending"
    elif invalid:
        evidence_state = "invalid"
    elif observed is None:
        evidence_state = "undated"
    elif cadence_seconds is not None and age_seconds is not None and age_seconds > cadence_seconds * 2.5:
        evidence_state = "stale"
    else:
        evidence_state = "fresh"

    counts: dict[str, int | float] = {}
    if document:
        for field in spec.get("count_fields", []):
            value = _bounded_count(_value_at(document, str(field)))
            if value is not None:
                counts[str(field)] = value

    return {
        "evidence_state": evidence_state,
        "observed_at": _iso(observed) if observed else None,
        "age_seconds": age_seconds,
        "counts": counts,
        "latest_bytes": latest_path.stat().st_size if latest_exists and latest_path else 0,
        "history_bytes": history_path.stat().st_size if history_exists and history_path else 0,
        "history_rows": _line_count(history_path) if history_exists and history_path else 0,
        "latest_available": latest_exists,
        "history_available": history_exists,
    }


def _validate(config: dict[str, Any]) -> None:
    if config.get("schema_version") != "1.0.0":
        raise ValueError("unsupported public catalog schema_version")
    datasets = config.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        raise ValueError("catalog requires at least one dataset")
    seen: set[str] = set()
    required = {
        "id", "name", "description", "layer", "stage", "collection_mode",
        "status", "cadence", "geography", "sources", "latest",
        "landing_page", "method", "count_fields", "license",
    }
    for item in datasets:
        if not isinstance(item, dict):
            raise ValueError("each dataset must be an object")
        missing = sorted(required - set(item))
        if missing:
            raise ValueError(f"dataset is missing fields: {', '.join(missing)}")
        slug = str(item["id"])
        if not _SLUG.fullmatch(slug) or slug in seen:
            raise ValueError(f"invalid or duplicate dataset id: {slug!r}")
        seen.add(slug)
        _safe_repo_path(str(item["latest"]))
        _safe_repo_path(str(item["history"])) if item.get("history") else None
        if _duration_seconds(str(item["cadence"])) is None:
            raise ValueError(f"unsupported cadence for {slug}: {item['cadence']!r}")
        license_doc = item.get("license")
        if not isinstance(license_doc, dict) or not license_doc.get("name") or not license_doc.get("url"):
            raise ValueError(f"dataset {slug} needs an explicit license name and URL")
        if urlsplit(str(license_doc["url"])).scheme != "https":
            raise ValueError(f"dataset {slug} license URL must use HTTPS")


def build_catalog(*, now: datetime | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    _validate(config)
    build_time = now or _utc_now()
    datasets = []
    for source in config["datasets"]:
        item = dict(source)
        item["artifacts"] = _artifact_metadata(item, now=build_time)
        item["urls"] = {
            key: urljoin(SITE, str(item[key]))
            for key in ("latest", "history", "landing_page", "method")
            if item.get(key)
        }
        datasets.append(item)

    states: dict[str, int] = {}
    layers: dict[str, int] = {}
    total_bytes = 0
    total_rows = 0
    for item in datasets:
        state = item["artifacts"]["evidence_state"]
        states[state] = states.get(state, 0) + 1
        layers[item["layer"]] = layers.get(item["layer"], 0) + 1
        total_bytes += item["artifacts"]["latest_bytes"] + item["artifacts"]["history_bytes"]
        total_rows += item["artifacts"]["history_rows"]

    catalog = {
        "schema": "palimpsest-data-catalog/v1",
        "generated_at": _iso(build_time),
        **config["catalog"],
        "summary": {
            "datasets": len(datasets),
            "states": dict(sorted(states.items())),
            "layers": dict(sorted(layers.items())),
            "published_bytes": total_bytes,
            "history_rows": total_rows,
        },
        "datasets": datasets,
    }

    jsonld_datasets = []
    resources = []
    for item in datasets:
        distributions = []
        for kind, media in (("latest", "application/json"), ("history", "application/x-ndjson")):
            path = item.get(kind)
            if not path:
                continue
            local = _safe_repo_path(path)
            # Discovery metadata must describe distributions that actually
            # exist. A documented but gated/pending dataset remains in the
            # catalog without advertising a download URL that returns 404.
            if local is None or not local.is_file():
                continue
            distributions.append({
                "@type": "DataDownload",
                "name": f"{item['name']} — {kind}",
                "encodingFormat": media,
                "contentUrl": urljoin(SITE, path),
            })
            resources.append({
                "name": f"{item['id']}-{kind}",
                "path": path,
                "format": "jsonl" if kind == "history" else "json",
                "mediatype": media,
                "bytes": local.stat().st_size,
            })
        jsonld_datasets.append({
            "@type": "Dataset",
            "identifier": item["id"],
            "name": item["name"],
            "description": item["description"],
            "url": urljoin(SITE, item["landing_page"]),
            "license": item["license"]["url"],
            "spatialCoverage": item["geography"],
            "temporalResolution": item["cadence"],
            "dateModified": item["artifacts"]["observed_at"],
            "isAccessibleForFree": True,
            "distribution": distributions,
        })

    jsonld = {
        "@context": "https://schema.org",
        "@type": "DataCatalog",
        "name": config["catalog"]["name"],
        "description": config["catalog"]["description"],
        "url": config["catalog"]["homepage"],
        "publisher": {"@type": "Organization", "name": "Palimpsest", "url": SITE},
        "dateModified": _iso(build_time),
        "dataset": jsonld_datasets,
    }
    datapackage = {
        "profile": "data-package",
        "name": "palimpsest-evidence-atlas",
        "title": config["catalog"]["name"],
        "description": config["catalog"]["description"],
        "homepage": config["catalog"]["homepage"],
        "created": _iso(build_time),
        "contributors": [{"title": "Palimpsest", "email": config["catalog"]["contact"], "role": "publisher"}],
        "resources": resources,
    }
    return catalog, jsonld, datapackage


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate the source and build in memory only")
    parser.add_argument(
        "--now",
        help="build at this timezone-aware ISO-8601 clock (for deterministic replay)",
    )
    args = parser.parse_args(argv)
    build_time = None
    if args.now is not None:
        text = args.now.strip()
        try:
            build_time = datetime.fromisoformat(
                text[:-1] + "+00:00" if text.endswith("Z") else text
            )
        except ValueError:
            parser.error("--now must be a valid ISO-8601 timestamp")
        if build_time.tzinfo is None or build_time.utcoffset() is None:
            parser.error("--now must include a timezone")
        build_time = build_time.astimezone(timezone.utc)
    catalog, jsonld, datapackage = build_catalog(now=build_time)
    if not args.check:
        _atomic_json(ROOT / "readings" / "catalog.json", catalog)
        _atomic_json(ROOT / "readings" / "catalog.jsonld", jsonld)
        _atomic_json(ROOT / "datapackage.json", datapackage)
    print(json.dumps({
        "status": "ok",
        "datasets": catalog["summary"]["datasets"],
        "published_bytes": catalog["summary"]["published_bytes"],
        "history_rows": catalog["summary"]["history_rows"],
        "written": not args.check,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
