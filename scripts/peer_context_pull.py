#!/usr/bin/env python3
"""Attributed peer-context warehouse.

Writes ``peer-context-latest.json`` (``n_hosts``). The review ranker is a
separate job in ``scripts.peer_context_rank_pull``. No catalog crawl, no
Weiboscope 2012 dump, no generative brief.
"""

from __future__ import annotations

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
        print("peer-context: halted by kill switch; abstaining")
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
        print("peer-context: OONI silent or miss; not publishing ooni-peer-context-latest")
    else:
        write_json(OONI_OUT, docs["ooni"])
        append_history(OONI_HIST, {
            "generated_at": document["generated_at"],
            "n_hits": docs["ooni"]["n_hits"],
        })
    if docs["cdt"] is None:
        print("peer-context: CDT silent; not publishing cdt-context-latest")
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


def main(**warehouse_kwargs):
    """Assemble the attributed warehouse. Ranker lives in peer_context_rank_pull."""

    return assemble_warehouse(**warehouse_kwargs)


if __name__ == "__main__":
    raise SystemExit(0 if main() is not False else 1)
