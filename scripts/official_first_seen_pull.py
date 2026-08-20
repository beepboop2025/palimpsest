"""Poll official public landing pages and persist first-seen / rewrite trails.

State lives under ``data/official-first-seen/`` (gitignored node state).
If every page is silent and there is no prior state, the runner abstains.

Usage:  PYTHONPATH=. python -m scripts.official_first_seen_pull
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from collectors.archive_capture import attach_new_url_captures
from collectors.official_first_seen import load_pages, poll_pages
from core.china_observation import iso_z, serialize_observation
from core.governance import KillSwitch, RateCeiling
from core.safe_fetch import FetchError, safe_fetch
from processors.archive_context import attach_derived_archive_context


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "official-first-seen-latest.json"
HIST = READINGS / "official-first-seen-history.jsonl"
STATE = ROOT / "data" / "official-first-seen" / "state.json"
METHOD_VERSION = 1
USER_AGENT = (
    "Palimpsest/0.2 (+https://palimpsest.info; open-source censorship "
    "research; use=reference)"
)


def _http_fetch(url: str) -> tuple[int, str]:
    proxy = os.getenv("PALIMPSEST_PROXY", "").strip() or None
    try:
        body = safe_fetch(
            url,
            max_bytes=2 * 1024 * 1024,
            timeout=25,
            headers={"User-Agent": USER_AGENT},
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


def _load_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_text(url: str) -> str:
    proxy = os.getenv("PALIMPSEST_PROXY", "").strip() or None
    return safe_fetch(
        url,
        max_bytes=512 * 1024,
        timeout=25,
        headers={"User-Agent": USER_AGENT},
        proxy=proxy,
    )


def main(*, fetch=None, now: datetime | None = None, state_path: Path | None = None) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("official-first-seen: halted by kill switch — abstaining")
        return None

    state_file = state_path or STATE
    previous = _load_state(state_file)
    pages = load_pages()
    result = poll_pages(
        pages=pages,
        fetch=fetch or _http_fetch,
        previous=previous,
        kill_switch=kill,
        rate_ceiling=None if fetch is not None else RateCeiling(rate=0.4, capacity=2.0),
        now=now or datetime.now(timezone.utc),
    )
    if result["n_ok"] == 0 and not previous.get("pages"):
        print("official-first-seen: no official page answered and no prior state — abstaining")
        return None

    prior_urls = {
        url for url, row in (previous.get("pages") or {}).items()
        if isinstance(row, dict) and row.get("content_sha256")
    }
    observations = attach_derived_archive_context(
        attach_new_url_captures(
            [serialize_observation(obs) for obs in result["observations"]],
            previous_urls=prior_urls,
            fetch=_save_text if fetch is None else None,
            limit=6,
        )
    )
    generated = iso_z(result["generated_at"]) or iso_z(datetime.now(timezone.utc))
    out = {
        "generated_at": generated,
        "method_version": METHOD_VERSION,
        "source": "Public official landing pages (Xinhua, People's Daily, gov.cn, MFA, PBOC, CAC, NDRC, MIIT, NBS, wenshu landing)",
        "scope": (
            "First-seen public text, content hashes, last-confirmed-alive, and "
            "disappearance/rewrite trails for official landing pages. No Baike. "
            "No China Judgements docket scrape. No person pages."
        ),
        "method": (
            "Keyless GET of reviewed official landing pages; hash comparison "
            "against node-local state; Wayback Save Page Now only for newly "
            "first-seen URLs; snapshot attached only when IA confirmed one."
        ),
        "n_pages": result["n_pages"],
        "n_ok": result["n_ok"],
        "n_unreachable": result["n_unreachable"],
        "n_observations": len(observations),
        "pages": result["pages"],
        "observations": observations,
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"generated_at": generated, "pages": result["pages"]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HIST.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "generated_at": generated,
            "n_ok": out["n_ok"],
            "n_observations": out["n_observations"],
        }, ensure_ascii=False) + "\n")
    print(f"official-first-seen: {out['n_ok']}/{out['n_pages']} pages, {out['n_observations']} observations")
    return out


if __name__ == "__main__":
    main()
