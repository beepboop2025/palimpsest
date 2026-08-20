"""Pull the fused public-board term dump from verified keyless archives.

Usage:  PYTHONPATH=. python -m scripts.public_board_terms_pull
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from collectors.public_board_archives import collect_archives, load_catalog
from core.governance import KillSwitch
from core.public_board_terms import JOB_NAME, write_public_board_terms
from core.safe_fetch import FetchError, safe_fetch


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
DDTI = READINGS / "ddti-latest.json"
WINDOW_DAYS = 2
USER_AGENT = (
    "Palimpsest/0.2 (+https://palimpsest.info; open-source censorship "
    "research; public board archives only)"
)


def _http_fetch(url: str) -> tuple[int, str]:
    proxy = os.getenv("PALIMPSEST_PROXY", "").strip() or None
    try:
        body = safe_fetch(
            url,
            max_bytes=2 * 1024 * 1024,
            timeout=25,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json,text/markdown,text/plain,text/html,*/*",
            },
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


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


def _load_ddti_terms(path: Path = DDTI) -> list[dict]:
    ranked = (_load_json(path) or {}).get("ranked") or []
    return [row for row in ranked if isinstance(row, dict) and row.get("term")]


def main(
    *,
    fetch=None,
    now: datetime | None = None,
    readings: Path | None = None,
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    kill = KillSwitch()
    if kill.is_halted():
        print(f"{JOB_NAME}: halted by kill switch — abstaining")
        return None

    clock = now or datetime.now(timezone.utc)
    generated = clock.strftime("%Y-%m-%dT%H:%M:%SZ")
    dates = [
        (clock - timedelta(days=index)).strftime("%Y-%m-%d")
        for index in range(WINDOW_DAYS, -1, -1)
    ]
    readings_dir = readings or READINGS
    collected = collect_archives(
        dates=dates,
        fetch=fetch or _http_fetch,
        catalog=catalog or load_catalog(),
        extra_readings={
            "public-hot-boards": _load_json(readings_dir / "public-hot-boards-latest.json"),
            "wikipedia-gazetteer-rc": _load_json(
                readings_dir / "wikipedia-gazetteer-rc-latest.json"
            ),
        },
    )
    if collected["n_boards_ok"] == 0 and collected["n_sightings"] == 0:
        print(
            f"{JOB_NAME}: every board silent, login-walled, or unreachable — abstaining "
            f"(boards={[row['status'] for row in collected['boards']]})"
        )
        return None

    document = write_public_board_terms(
        collected,
        generated_at=generated,
        readings=readings_dir,
        ddti_terms=_load_ddti_terms(readings_dir / "ddti-latest.json"),
    )
    if document is None:
        print(f"{JOB_NAME}: no usable board titles — abstaining")
        return None
    print(
        f"{JOB_NAME}: {document['n_titles']} titles · "
        f"{document['n_boards_ok']}/{document['n_boards']} boards · "
        f"{document['regimes']['suppressed_invisible']} suppressed_invisible / "
        f"{document['regimes']['contained_visible']} contained_visible"
    )
    return document


if __name__ == "__main__":
    main()
