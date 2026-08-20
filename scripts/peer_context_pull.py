#!/usr/bin/env python3
"""Peer-context warehouse plus the review-ranker CLI.

The fleet job assembles attributed GreatFire / OONI / CDT / Weiboscope rows and
the per-peer latest files the ranker fits. Passing ``--now`` / ``--root`` still
runs the review ranker over those files. No catalog crawl, no Weiboscope 2012
dump, no generative brief.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collectors.weiboscope import probe_public_index
from core.china_observation import iso_z
from core.governance import KillSwitch
from core.peer_context import (
    METHOD_VERSION,
    SCHEMA_VERSION,
    build_peer_document,
    cdt_items_from_readings,
    collect_palimpsest_urls,
    load_peer_document,
)
from core.peer_features import (
    append_history,
    build_feature_table,
    write_feature_jsonl,
    write_json,
)
from core.safe_fetch import FetchError, safe_fetch
from processors.peer_context import (
    JOB,
    SCHEMA,
    build_peer_context,
)

READINGS = ROOT / "readings"
LATEST = READINGS / "peer-context-latest.json"
HISTORY = READINGS / "peer-context-history.jsonl"
OUT = LATEST
HIST = HISTORY
FEATURES = READINGS / "peer-context-features.jsonl"
OONI_OUT = READINGS / "ooni-peer-context-latest.json"
OONI_HIST = READINGS / "ooni-peer-context-history.jsonl"
CDT_OUT = READINGS / "cdt-context-latest.json"
CDT_HIST = READINGS / "cdt-context-history.jsonl"
WEIBO_OUT = READINGS / "weiboscope-context-latest.json"
GF_CACHE = READINGS / "greatfire-context-latest.json"
OONI_GFW = READINGS / "ooni-gfw-latest.json"


def _warehouse_path() -> Path | None:
    for raw in (
        os.getenv("PALIMPSEST_OONI_WAREHOUSE_DIR", "").strip(),
        os.getenv("PALIMPSEST_OONI_WAREHOUSE_HOST_PATH", "").strip(),
        os.getenv("PALIMPSEST_OONI_WAREHOUSE", "").strip(),
    ):
        if raw:
            candidate = Path(raw)
            if candidate.is_dir():
                return candidate
    for rel in ("data/ooni-bulk", "warehouse/ooni-bulk"):
        candidate = ROOT / rel
        if candidate.is_dir():
            return candidate
    return None


WAREHOUSE = _warehouse_path()
USER_AGENT = (
    "Palimpsest/0.2 (+https://palimpsest.info; Weiboscope index probe only; "
    "no 2012 dump)"
)


def _parse_now(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _history_fingerprint(snapshot: dict) -> dict:
    unusual = [
        f"{row.get('peer')}:{row.get('series_id')}"
        for row in snapshot.get("peer_series") or []
        if isinstance(row, dict) and row.get("unusual") is True
    ]
    return {
        "n_peer_series": snapshot.get("n_peer_series"),
        "n_peer_series_scored": snapshot.get("n_peer_series_scored"),
        "n_peer_series_warming_up": snapshot.get("n_peer_series_warming_up"),
        "n_joins": snapshot.get("n_joins"),
        "unusual": unusual,
    }


def run(*, now: datetime | str | None = None, root: Path | None = None) -> dict:
    root = Path(root or ROOT)
    clock = _parse_now(now) if isinstance(now, str) else now
    return build_peer_context(root / "readings", now=clock)


def write_outputs(snapshot: dict, *, root: Path | None = None) -> None:
    root = Path(root or ROOT)
    readings = root / "readings"
    readings.mkdir(parents=True, exist_ok=True)
    latest = readings / "peer-context-latest.json"
    history = readings / "peer-context-history.jsonl"
    previous = None
    if latest.is_file():
        try:
            loaded = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            previous = loaded
    latest.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if previous is not None and _history_fingerprint(previous) == _history_fingerprint(snapshot):
        return
    history_row = {
        "schema": SCHEMA,
        "job": JOB,
        "generated_at": snapshot.get("generated_at"),
        "n_peer_series": snapshot.get("n_peer_series"),
        "n_peer_series_scored": snapshot.get("n_peer_series_scored"),
        "n_peer_series_warming_up": snapshot.get("n_peer_series_warming_up"),
        "n_joins": snapshot.get("n_joins"),
        "unusual": _history_fingerprint(snapshot)["unusual"],
    }
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(history_row, ensure_ascii=False) + "\n")


def _http_fetch(url: str) -> tuple[int, str]:
    proxy = os.getenv("PALIMPSEST_PROXY", "").strip() or None
    try:
        body = safe_fetch(
            url,
            max_bytes=8 * 1024,
            timeout=15,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/plain"},
            proxy=proxy,
        )
        return 200, body
    except FetchError as exc:
        message = str(exc)
        if message.startswith("http status "):
            token = message.rsplit(" ", 1)[-1]
            if token.isdigit():
                return int(token), ""
        raise OSError(message) from exc


def assemble_warehouse(
    *, fetch=None, now: datetime | None = None, probe_weiboscope: bool = True
) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("peer-context: halted by kill switch — abstaining")
        return None

    now = now or datetime.now(timezone.utc)
    urls = collect_palimpsest_urls(READINGS, root=ROOT)
    greatfire = load_peer_document(GF_CACHE)
    cdt_items = cdt_items_from_readings(READINGS)
    weiboscope = None
    if probe_weiboscope:
        def _silent(_url: str) -> tuple[int, str]:
            raise OSError("silent")

        try:
            weiboscope = probe_public_index(fetch or _http_fetch, now=now)
        except OSError:
            weiboscope = probe_public_index(_silent, now=now)

    document = build_peer_document(
        urls=urls,
        greatfire=greatfire,
        cdt_items=cdt_items,
        weiboscope=weiboscope,
        gfw_path=OONI_GFW if OONI_GFW.is_file() else None,
        warehouse=WAREHOUSE,
        now=now,
    )
    document["schema_version"] = SCHEMA_VERSION
    document["method_version"] = METHOD_VERSION
    document["generated_at"] = iso_z(now)
    table = build_feature_table(
        greatfire=greatfire,
        ooni=document.get("ooni"),
        cdt_items_or_doc=cdt_items,
        now=now,
    )
    docs = table["documents"]
    document["feature_rows"] = table["n_rows"]
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_feature_jsonl(FEATURES, table["rows"])
    if docs["ooni"] is None:
        print("peer-context: OONI silent or miss — not publishing ooni-peer-context-latest")
    else:
        write_json(OONI_OUT, docs["ooni"])
        append_history(OONI_HIST, {
            "generated_at": document["generated_at"],
            "n_hits": docs["ooni"]["n_hits"],
        })
    if docs["cdt"] is None:
        print("peer-context: CDT silent — not publishing cdt-context-latest")
    else:
        write_json(CDT_OUT, docs["cdt"])
        append_history(CDT_HIST, {
            "generated_at": document["generated_at"],
            "n_titles": docs["cdt"]["n_items"],
        })
    write_json(WEIBO_OUT, docs["weiboscope"])
    with HIST.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "generated_at": document["generated_at"],
            "n_hosts": document["n_hosts"],
            "n_greatfire": document["n_greatfire"],
            "n_ooni": document["n_ooni"],
            "n_cdt": document["n_cdt"],
            "n_feature_rows": table["n_rows"],
            "weiboscope_dump_on_node": False,
        }, ensure_ascii=False) + "\n")
    print(
        f"peer-context: {document['n_hosts']} hosts, "
        f"{document['n_greatfire']} GreatFire, {document['n_ooni']} OONI, "
        f"{document['n_cdt']} CDT, {table['n_rows']} feature rows; "
        "Weiboscope dump not on node"
    )
    return document


def _ranker_main(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--now",
        default=None,
        help="ISO-8601 generated_at (tests). Default: current UTC.",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Repository root (tests). Default: this checkout.",
    )
    args = parser.parse_args(argv)
    if KillSwitch().is_halted():
        print("peer-context: global kill switch is engaged", file=sys.stderr)
        return 2
    snapshot = run(now=args.now, root=Path(args.root) if args.root else None)
    write_outputs(snapshot, root=Path(args.root) if args.root else None)
    summary = {
        "schema": SCHEMA,
        "job": JOB,
        "generated_at": snapshot.get("generated_at"),
        "n_peer_series": snapshot.get("n_peer_series"),
        "n_peer_series_scored": snapshot.get("n_peer_series_scored"),
        "n_peer_series_warming_up": snapshot.get("n_peer_series_warming_up"),
        "n_joins": snapshot.get("n_joins"),
        "n_objects_considered": snapshot.get("n_objects_considered"),
    }
    print(
        f"{JOB}: series={summary['n_peer_series']} "
        f"scored={summary['n_peer_series_scored']} "
        f"warming_up={summary['n_peer_series_warming_up']} "
        f"joins={summary['n_joins']}",
        file=sys.stderr,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None, **warehouse_kwargs):
    """Warehouse assemble by default; ranker when argv is an explicit list."""

    if argv is not None:
        return _ranker_main(argv)
    return assemble_warehouse(**warehouse_kwargs)


if __name__ == "__main__":
    raise SystemExit(0 if main() is not False else 1)
