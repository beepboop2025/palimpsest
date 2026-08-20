"""Pull keyless public aggregate hot boards (Baidu / Toutiao / Douyin).

Abstain if every board is silent or login-walled. Titles and ranks only.

Usage:  PYTHONPATH=. python -m scripts.public_hot_boards_pull
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from collectors.public_hot_boards import collect_boards, load_boards
from core.china_observation import iso_z, serialize_observation
from core.governance import KillSwitch, RateCeiling
from core.safe_fetch import FetchError, safe_fetch


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "public-hot-boards-latest.json"
HIST = READINGS / "public-hot-boards-history.jsonl"
USER_AGENT = (
    "Palimpsest/0.2 (+https://palimpsest.info; open-source censorship "
    "research; public aggregate boards only)"
)


def _http_fetch(url: str) -> tuple[int, str]:
    proxy = os.getenv("PALIMPSEST_PROXY", "").strip() or None
    try:
        body = safe_fetch(
            url,
            max_bytes=1024 * 1024,
            timeout=25,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,*/*"},
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


def main(*, fetch=None, now: datetime | None = None) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("public-hot-boards: halted by kill switch — abstaining")
        return None

    result = collect_boards(
        boards=load_boards(),
        fetch=fetch or _http_fetch,
        kill_switch=kill,
        rate_ceiling=None if fetch is not None else RateCeiling(rate=0.4, capacity=2.0),
        now=now or datetime.now(timezone.utc),
    )
    if result["n_boards_ok"] == 0 and result["n_observations"] == 0:
        print(
            "public-hot-boards: every aggregate board silent or login-walled — abstaining "
            f"(boards={[row['status'] for row in result['boards']]})"
        )
        return None

    generated = iso_z(result["generated_at"]) or iso_z(datetime.now(timezone.utc))
    observations = [serialize_observation(obs) for obs in result["observations"]]
    out = {
        "generated_at": generated,
        "method_version": 1,
        "source": "Public aggregate hot boards (Baidu / Toutiao / Douyin)",
        "scope": (
            "Board titles and ranks only. No user pages, no login, no profiles, "
            "no engagement. Each board reports its own reachability."
        ),
        "method": "Keyless JSON GET of reviewed public board endpoints; silent boards abstain.",
        "n_boards": result["n_boards"],
        "n_boards_ok": result["n_boards_ok"],
        "n_observations": len(observations),
        "boards": result["boards"],
        "observations": observations,
    }
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HIST.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "generated_at": generated,
            "n_boards_ok": out["n_boards_ok"],
            "n_observations": out["n_observations"],
        }, ensure_ascii=False) + "\n")
    print(f"public-hot-boards: {out['n_boards_ok']}/{out['n_boards']} boards, {out['n_observations']} titles")
    return out


if __name__ == "__main__":
    main()
