"""Public aggregate hot boards — Baidu, Toutiao, Douyin titles and ranks only.

Each board is a candidate JSON surface. A login wall, captcha, HTML shell, or
empty list is a silent board. No user pages, no profiles, no engagement.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from core.china_observation import enrich_observation, iso_z, public_text
from core.governance import KillSwitch, RateCeiling


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "public_hot_boards.json"
LOGIN_MARKERS = (
    "passport.baidu.com",
    "wappass.baidu.com",
    "login.snssdk.com",
    "sso.toutiao.com",
    "captcha",
    "请登录",
)
Fetch = Callable[[str], tuple[int, str]]


def load_boards(path: Path | str = DEFAULT_CONFIG) -> list[dict[str, Any]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    boards = []
    for raw in doc.get("boards") or []:
        if not isinstance(raw, dict):
            continue
        url = public_text(raw.get("url"), limit=2048)
        if not url.startswith("https://"):
            continue
        boards.append({
            "name": public_text(raw.get("name"), limit=40),
            "kind": public_text(raw.get("kind"), limit=16),
            "url": url,
            "note": public_text(raw.get("note"), limit=240),
        })
    return boards


def login_walled(status: int | str, body: str) -> bool:
    if status in (401, 403) or (isinstance(status, str) and status.startswith("error:")):
        return True
    blob = body or ""
    if not blob:
        return False
    stripped = blob.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return any(marker in blob for marker in LOGIN_MARKERS[:4])
    return True


def parse_baidu(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    cards = data.get("cards") if isinstance(data, dict) else None
    if not isinstance(cards, list):
        return []
    out: list[dict[str, Any]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        for row in card.get("content") or []:
            if not isinstance(row, dict):
                continue
            title = public_text(row.get("word") or row.get("query") or row.get("hotWord"), limit=200)
            if not title:
                continue
            rank = row.get("index") or row.get("hotRank") or row.get("rank")
            out.append({"title": title, "rank": rank if isinstance(rank, int) else len(out) + 1})
    return out


def parse_toutiao(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        title = public_text(row.get("Title") or row.get("title"), limit=200)
        if not title:
            continue
        rank = row.get("ClusterIdStr") or row.get("rank") or index
        out.append({"title": title, "rank": rank if isinstance(rank, int) else index})
    return out


def parse_douyin(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("word_list") or payload.get("data")
    if isinstance(rows, dict):
        rows = rows.get("word_list") or rows.get("list")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        title = public_text(row.get("word") or row.get("sentence") or row.get("title"), limit=200)
        if not title:
            continue
        out.append({"title": title, "rank": index})
    return out


PARSERS = {
    "baidu": parse_baidu,
    "toutiao": parse_toutiao,
    "douyin": parse_douyin,
}


def collect_boards(
    *,
    boards: list[Mapping[str, Any]] | None = None,
    fetch: Fetch,
    kill_switch: KillSwitch | None = None,
    rate_ceiling: RateCeiling | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    generated = iso_z(now)
    kill = kill_switch or KillSwitch()
    watch = list(boards) if boards is not None else load_boards()
    observations: list[dict[str, Any]] = []
    board_rows: list[dict[str, Any]] = []
    n_ok = 0

    for board in watch:
        kill.require_live()
        if rate_ceiling is not None:
            rate_ceiling.acquire()
        url = board["url"]
        kind = board["kind"]
        status: int | str
        body = ""
        try:
            status, body = fetch(url)
        except OSError as exc:
            status = f"error:{type(exc).__name__}"
            body = ""
        items: list[dict[str, Any]] = []
        if login_walled(status, body):
            state = "login_walled" if status in (401, 403) or not (body or "").lstrip().startswith(("{", "[")) else "unreachable"
        elif status != 200:
            state = "unreachable"
        else:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = None
            parser = PARSERS.get(kind)
            items = parser(payload) if parser and payload is not None else []
            state = "ok" if items else "empty-feed"
        if state == "ok":
            n_ok += 1
        board_rows.append({
            "name": board["name"],
            "kind": kind,
            "url": url,
            "http_status": status,
            "n_items": len(items),
            "status": state,
            "note": board.get("note"),
        })
        for item in items:
            title = item["title"]
            text = f"{kind} hot board #{item['rank']}: {title}"
            observations.append(enrich_observation(
                {
                    "terms": [],
                    "detected_at": generated,
                    "title": title,
                    "text": text,
                    "url": url,
                    "source": f"public-hot-boards:{kind}",
                    "rights_policy": "public-aggregate-board",
                },
                text=text,
                source_url=url,
                first_seen=generated,
                last_seen=generated,
                last_confirmed_alive=generated,
                provenance={
                    "collector": "public_hot_boards",
                    "method": "keyless public aggregate-board JSON; titles and ranks only",
                    "vantage": "outside-china-public-source",
                    "board": board["name"],
                    "rank": item["rank"],
                    "schema_version": "palimpsest-china-observation.v1",
                    "method_version": 1,
                },
            ))

    return {
        "generated_at": generated,
        "n_boards": len(watch),
        "n_boards_ok": n_ok,
        "n_observations": len(observations),
        "boards": board_rows,
        "observations": observations,
    }
