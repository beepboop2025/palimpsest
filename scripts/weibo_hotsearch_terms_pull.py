"""Publish the public Weibo hot-search BOARD dump.

Same keyless archive ingest as ``scripts/weibo_hotsearch_pull.py``. This
writer stores every distinct board title in the current window. It does
not scrape user timelines, comments, DMs, or private profiles.

Usage:  PYTHONPATH=. python -m scripts.weibo_hotsearch_terms_pull
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from collectors.weibo_hotsearch import collect_range
from core.weibo_hotsearch_terms import JOB_NAME, write_weibo_hotsearch_terms


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
DDTI = READINGS / "ddti-latest.json"
GAZETTEER = ROOT / "config" / "zh_censorship_gazetteer.json"
WINDOW_DAYS = 7
_HAS_CJK = re.compile(r"[㐀-鿿]")


def _load_ddti_terms(path: Path = DDTI) -> list[dict]:
    try:
        ranked = json.loads(path.read_text(encoding="utf-8")).get("ranked") or []
    except (OSError, json.JSONDecodeError):
        return []
    return [row for row in ranked if _HAS_CJK.search(str(row.get("term") or ""))]


def _load_gazetteer_terms(path: Path = GAZETTEER) -> list[str]:
    try:
        cats = json.loads(path.read_text(encoding="utf-8")).get("categories") or {}
    except (OSError, json.JSONDecodeError):
        return []
    out: list[str] = []
    for entries in cats.values():
        for entry in entries or []:
            zh = str((entry or {}).get("zh") or "").strip()
            if len(zh) >= 2:
                out.append(zh)
    return out


def write_from_days(
    days: Mapping[str, list[dict]] | None,
    *,
    generated_at: str,
    readings: Path = READINGS,
    ddti_path: Path | None = None,
    gazetteer_path: Path | None = None,
) -> dict[str, Any] | None:
    ddti_terms = _load_ddti_terms(ddti_path or DDTI)
    sensitive = set(_load_gazetteer_terms(gazetteer_path or GAZETTEER)) | {
        str(row.get("term")) for row in ddti_terms if row.get("term")
    }
    return write_weibo_hotsearch_terms(
        days,
        generated_at=generated_at,
        readings=readings,
        ddti_terms=ddti_terms,
        sensitive_terms=sensitive,
    )


def main(*, now: datetime | None = None, days: Mapping[str, list[dict]] | None = None) -> dict[str, Any] | None:
    clock = now or datetime.now(timezone.utc)
    generated = clock.strftime("%Y-%m-%dT%H:%M:%SZ")
    if days is None:
        dates = [
            (clock - timedelta(days=index)).strftime("%Y-%m-%d")
            for index in range(WINDOW_DAYS, -1, -1)
        ]
        days = collect_range(dates)
    if not days:
        print(f"{JOB_NAME}: archive returned nothing parseable — abstaining")
        return None
    document = write_from_days(days, generated_at=generated, readings=READINGS)
    if document is None:
        print(f"{JOB_NAME}: no usable board titles — abstaining")
        return None
    print(
        f"{JOB_NAME}: {document['n_titles']} titles · "
        f"{document['board_entries']} board entries · "
        f"{document['regimes']['suppressed_invisible']} suppressed_invisible / "
        f"{document['regimes']['contained_visible']} contained_visible"
    )
    return document


if __name__ == "__main__":
    main()
