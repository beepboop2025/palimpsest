"""Keyless public-board archives — titles and ranks only.

Each source is a candidate. A login wall, captcha, HTML shell, empty list,
or 暂无数据 is a silent board. Never a zero. Parsers whitelist title/rank/pin
and drop user ids, follower graphs, post bodies, and view counts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlparse

from collectors.weibo_hotsearch import parse_day as parse_justjavac_day
from core.china_observation import public_text


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "public_board_archives.json"
Fetch = Callable[[str], tuple[int, str]]

_MD_ITEM = re.compile(
    r"^(?:\d+\.|[-+*])\s+\[([^\]]+)\]\(([^)]+)\)\s*$",
    re.M,
)
_BAND_RANK = re.compile(r"(?:band_rank|realpos|pos)=(\d+)")
_EMPTY = ("暂无数据", "无数据", "no data")
_HTML_TITLE = re.compile(
    r"<h[1-4][^>]*>\s*([^<]{2,180})\s*</h[1-4]>",
    re.I,
)
_LOGIN_MARKERS = (
    "passport.",
    "请登录",
    "captcha",
    "sso.",
    "wappass.",
    "login.snssdk",
)

FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "uid",
        "user_id",
        "userid",
        "sec_uid",
        "follower",
        "followers",
        "following",
        "location",
        "user_info",
        "author_area",
    }
)


def load_catalog(path: Path | str = DEFAULT_CONFIG) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def normalize_title(value: str) -> str:
    title = public_text(value, limit=180)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _rank_from_url(url: str, fallback: int) -> int:
    match = _BAND_RANK.search(url or "")
    if match:
        return int(match.group(1))
    return fallback


def _pinned_from_url(url: str) -> bool:
    blob = url or ""
    return "Refer=new_time" in blob or "cate=10103" in blob


def parse_markdown_titles(
    body: str,
    *,
    sections: list[str] | None = None,
) -> list[dict[str, Any]] | None:
    """Extract ``1. [title](url)`` / ``+ [title](url)`` rows. Empty archive → None."""

    text = body or ""
    if not text.strip():
        return None
    if archive_login_walled(200, text):
        return None
    chunks = [text]
    if sections:
        chunks = []
        for section in sections:
            match = re.search(
                rf"^##\s+{re.escape(section)}\s*$",
                text,
                re.M,
            )
            if not match:
                continue
            start = match.end()
            nxt = re.search(r"^##\s+", text[start:], re.M)
            end = start + nxt.start() if nxt else len(text)
            chunks.append(text[start:end])
        if not chunks:
            return None
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks:
        stripped = chunk.strip()
        if any(token == stripped or token in stripped[:40] for token in _EMPTY) and "[" not in chunk:
            continue
        for match in _MD_ITEM.finditer(chunk):
            title = normalize_title(match.group(1))
            if not title or title in _EMPTY or title in seen:
                continue
            url = match.group(2) or ""
            seen.add(title)
            rows.append(
                {
                    "title": title,
                    "rank": _rank_from_url(url, len(rows) + 1),
                    "pinned": _pinned_from_url(url),
                }
            )
    return rows or None


def parse_justjavac(body: str) -> list[dict[str, Any]] | None:
    parsed = parse_justjavac_day(body)
    if not parsed:
        return None
    return [
        {
            "title": normalize_title(row["title"]),
            "rank": row.get("rank") if isinstance(row.get("rank"), int) else None,
            "pinned": bool(row.get("pinned")),
        }
        for row in parsed
        if normalize_title(row.get("title") or "")
    ] or None


def parse_wikipedia_mostviewed(payload: object) -> list[dict[str, Any]] | None:
    if not isinstance(payload, dict):
        return None
    query = payload.get("query")
    rows = query.get("mostviewed") if isinstance(query, dict) else None
    if not isinstance(rows, list):
        return None
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("ns") not in (0, "0"):
            continue
        title = normalize_title(str(row.get("title") or ""))
        if not title or title.startswith("Special:") or title.startswith("Wikipedia:"):
            continue
        out.append({"title": title, "rank": len(out) + 1, "pinned": False})
    return out or None


def parse_freewechat_index(status: int | str, body: str) -> list[dict[str, Any]] | None:
    """Titles from a public RSS/HTML index. No article bodies, no accounts."""

    if archive_login_walled(status, body):
        return None
    if status != 200 or not body:
        return None
    text = body
    titles: list[str] = []
    if "<rss" in text or "<feed" in text or "<item>" in text:
        for match in re.finditer(r"<title>([^<]{2,180})</title>", text, re.I):
            title = normalize_title(re.sub(r"<!\[CDATA\[|\]\]>", "", match.group(1)))
            if title and title.casefold() not in {"freewechat", "自由微信"}:
                titles.append(title)
    else:
        for match in _HTML_TITLE.finditer(text):
            title = normalize_title(match.group(1))
            if title:
                titles.append(title)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for title in titles:
        if title in seen:
            continue
        seen.add(title)
        out.append({"title": title, "rank": None, "pinned": False})
    return out or None


def archive_url(template: str, date: str, *, board_zh: str = "") -> str:
    year, month, _day = date.split("-")
    encoded = quote(board_zh, safe="") if board_zh else ""
    return (
        template.replace("{date}", date)
        .replace("{year}", year)
        .replace("{month}", month)
        .replace("{board_zh}", encoded or board_zh)
    )


def archive_login_walled(status: int | str, body: str) -> bool:
    """Login wall / captcha for archives. Markdown and JSON are never a wall."""

    if status in (401, 403):
        return True
    blob = body or ""
    if not blob:
        return False
    stripped = blob.lstrip()
    if stripped.startswith(("{", "[", "#", "+ ", "1.", "- ")):
        return False
    folded = blob.casefold()
    return any(marker in folded for marker in _LOGIN_MARKERS)


def _board_state(status: int | str, body: str, items: list[dict[str, Any]] | None) -> str:
    if items:
        return "ok"
    if archive_login_walled(status, body):
        return "login_walled"
    if status != 200:
        return "unreachable"
    return "silent"


def _record_board(
    rows: list[dict[str, Any]],
    *,
    name: str,
    board: str,
    url: str,
    status: int | str,
    body: str,
    items: list[dict[str, Any]] | None,
    note: str = "",
    license_name: str = "",
    role: str = "hot-board",
) -> dict[str, Any]:
    state = _board_state(status, body, items)
    rows.append(
        {
            "name": name,
            "board": board,
            "url": url,
            "http_status": status,
            "n_items": len(items or []),
            "status": state,
            "note": note,
            "license": license_name,
            "role": role,
        }
    )
    return rows[-1]


def collect_archives(
    *,
    dates: list[str],
    fetch: Fetch,
    catalog: Mapping[str, Any] | None = None,
    extra_readings: Mapping[str, Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Fetch candidate archives. Silent/walled boards stay listed; they are not zeros."""

    catalog = dict(catalog or load_catalog())
    extra = extra_readings or {}
    sightings: list[dict[str, Any]] = []
    board_rows: list[dict[str, Any]] = []
    day_keys: set[tuple[str, str, str]] = set()

    def add_items(
        items: list[dict[str, Any]] | None,
        *,
        board: str,
        date: str,
        archive: str,
        role: str = "hot-board",
    ) -> int:
        added = 0
        for item in items or []:
            title = normalize_title(item.get("title") or "")
            if not title:
                continue
            key = (board, title, date)
            if key in day_keys:
                for existing in sightings:
                    if existing["board"] != board or existing["title"] != title or existing["date"] != date:
                        continue
                    if archive not in existing["source_archives"]:
                        existing["source_archives"].append(archive)
                    rank = item.get("rank")
                    if isinstance(rank, int) and (
                        existing["rank"] is None or rank < existing["rank"]
                    ):
                        existing["rank"] = rank
                    existing["pinned"] = existing["pinned"] or bool(item.get("pinned"))
                continue
            day_keys.add(key)
            rank = item.get("rank")
            sightings.append(
                {
                    "board": board,
                    "title": title,
                    "rank": rank if isinstance(rank, int) else None,
                    "pinned": bool(item.get("pinned")),
                    "date": date,
                    "source_archives": [archive],
                    "role": role,
                }
            )
            added += 1
        return added

    for source in catalog.get("wired") or []:
        kind = source.get("kind")
        name = source.get("name")
        if kind == "justjavac_json":
            last_status: int | str = 0
            last_body = ""
            last_url = source["url_template"]
            answered: list[dict[str, Any]] | None = None
            for date in dates:
                url = archive_url(source["url_template"], date)
                last_url = url
                try:
                    status, body = fetch(url)
                except OSError as exc:
                    status, body = f"error:{type(exc).__name__}", ""
                last_status, last_body = status, body
                items = parse_justjavac(body) if status == 200 else None
                if items:
                    answered = items
                    add_items(items, board="weibo", date=date, archive=name)
            _record_board(
                board_rows,
                name=name,
                board="weibo",
                url=last_url,
                status=last_status,
                body=last_body,
                items=answered,
                note=source.get("note", ""),
                license_name=source.get("license", ""),
            )
        elif kind == "lonny_md":
            sections = source.get("sections")
            if source.get("section"):
                sections = [source["section"]]
            last_status = 0
            last_body = ""
            last_url = source["url_template"]
            answered = None
            for date in dates:
                url = archive_url(source["url_template"], date)
                last_url = url
                try:
                    status, body = fetch(url)
                except OSError as exc:
                    status, body = f"error:{type(exc).__name__}", ""
                last_status, last_body = status, body
                items = parse_markdown_titles(body, sections=sections) if status == 200 else None
                if items:
                    answered = items
                    add_items(items, board=source["board"], date=date, archive=name)
            _record_board(
                board_rows,
                name=name,
                board=source["board"],
                url=last_url,
                status=last_status,
                body=last_body,
                items=answered,
                note=source.get("note", ""),
                license_name=source.get("license", ""),
            )
        elif kind == "iiecho_md":
            for board_row in source.get("boards") or []:
                board = board_row["board"]
                zh = board_row["zh"]
                archive = f"{name}:{board}"
                last_status = 0
                last_body = ""
                last_url = source["url_template"]
                answered = None
                for date in dates:
                    url = archive_url(source["url_template"], date, board_zh=zh)
                    last_url = url
                    try:
                        status, body = fetch(url)
                    except OSError as exc:
                        status, body = f"error:{type(exc).__name__}", ""
                    last_status, last_body = status, body
                    items = parse_markdown_titles(body) if status == 200 else None
                    if items:
                        answered = items
                        add_items(items, board=board, date=date, archive=archive)
                _record_board(
                    board_rows,
                    name=archive,
                    board=board,
                    url=last_url,
                    status=last_status,
                    body=last_body,
                    items=answered,
                    note=source.get("license_note") or source.get("note", ""),
                    license_name=source.get("license", ""),
                )
        elif kind == "mediawiki_mostviewed":
            url = source["url"]
            try:
                status, body = fetch(url)
            except OSError as exc:
                status, body = f"error:{type(exc).__name__}", ""
            items = None
            if status == 200:
                try:
                    items = parse_wikipedia_mostviewed(json.loads(body))
                except json.JSONDecodeError:
                    items = None
            date = dates[-1] if dates else ""
            if items:
                add_items(
                    items,
                    board=source["board"],
                    date=date,
                    archive=name,
                    role="wikipedia-mostviewed",
                )
            _record_board(
                board_rows,
                name=name,
                board=source["board"],
                url=url,
                status=status,
                body=body,
                items=items,
                note=source.get("note", ""),
                license_name=source.get("license", ""),
                role="wikipedia-mostviewed",
            )
        elif kind == "in_tree_reading" and name == "wikipedia-gazetteer-rc":
            reading = extra.get("wikipedia-gazetteer-rc")
            items = []
            date = dates[-1] if dates else ""
            if isinstance(reading, Mapping):
                generated = str(reading.get("generated_at") or "")[:10]
                date = generated or date
                for record in reading.get("observations") or []:
                    if isinstance(record, Mapping) and record.get("title"):
                        items.append(
                            {
                                "title": normalize_title(str(record["title"])),
                                "rank": None,
                                "pinned": False,
                            }
                        )
            if items:
                add_items(
                    items,
                    board="zh-wikipedia",
                    date=date,
                    archive=name,
                    role="wikipedia-gazetteer-rc",
                )
            _record_board(
                board_rows,
                name=name,
                board="zh-wikipedia",
                url="in-tree:wikipedia-gazetteer-rc-latest.json",
                status=200 if items else 0,
                body="",
                items=items or None,
                note=source.get("note", ""),
                license_name=source.get("license", ""),
                role="wikipedia-gazetteer-rc",
            )
        elif kind == "in_tree_reading" and name == "public-hot-boards-live":
            reading = extra.get("public-hot-boards")
            grouped: dict[str, list[dict[str, Any]]] = {}
            date = dates[-1] if dates else ""
            if isinstance(reading, Mapping):
                generated = str(reading.get("generated_at") or "")[:10]
                date = generated or date
                for record in reading.get("observations") or []:
                    if not isinstance(record, Mapping) or not record.get("title"):
                        continue
                    source_id = str(record.get("source") or "public-hot-boards")
                    board = source_id.rsplit(":", 1)[-1] if ":" in source_id else "public"
                    rank = None
                    provenance = record.get("provenance")
                    if isinstance(provenance, Mapping) and isinstance(provenance.get("rank"), int):
                        rank = provenance["rank"]
                    grouped.setdefault(board, []).append(
                        {
                            "title": normalize_title(str(record["title"])),
                            "rank": rank,
                            "pinned": False,
                        }
                    )
            any_items = False
            for board, items in grouped.items():
                any_items = True
                add_items(
                    items,
                    board=board,
                    date=date,
                    archive=f"{name}:{board}",
                    role="hot-board",
                )
            _record_board(
                board_rows,
                name=name,
                board="live-json",
                url="in-tree:public-hot-boards-latest.json",
                status=200 if any_items else 0,
                body="",
                items=[{"title": "x"}] if any_items else None,
                note=source.get("note", ""),
                license_name=source.get("license", ""),
            )
            if isinstance(reading, Mapping):
                for row in reading.get("boards") or []:
                    if not isinstance(row, Mapping):
                        continue
                    board_rows.append(
                        {
                            "name": f"live:{row.get('name')}",
                            "board": str(row.get("kind") or ""),
                            "url": str(row.get("url") or ""),
                            "http_status": row.get("http_status"),
                            "n_items": row.get("n_items") or 0,
                            "status": str(row.get("status") or "silent"),
                            "note": str(row.get("note") or ""),
                            "license": "public-aggregate-json",
                            "role": "hot-board",
                        }
                    )

    for source in catalog.get("candidates") or []:
        name = source.get("name")
        items = None
        last_status: int | str = 0
        last_body = ""
        last_url = (source.get("urls") or [""])[0]
        for url in source.get("urls") or []:
            last_url = url
            try:
                status, body = fetch(url)
            except OSError as exc:
                status, body = f"error:{type(exc).__name__}", ""
            last_status, last_body = status, body
            items = parse_freewechat_index(status, body)
            if items:
                break
        date = dates[-1] if dates else ""
        if items:
            add_items(
                items,
                board=source.get("board") or "freewechat",
                date=date,
                archive=name,
                role=source.get("role") or "recovered-listing",
            )
        _record_board(
            board_rows,
            name=name,
            board=source.get("board") or "freewechat",
            url=last_url,
            status=last_status,
            body=last_body,
            items=items,
            note=source.get("note", ""),
            role=source.get("role") or "recovered-listing",
        )

    for skipped in catalog.get("skipped") or []:
        board_rows.append(
            {
                "name": skipped["name"],
                "board": skipped["name"],
                "url": "",
                "http_status": 0,
                "n_items": 0,
                "status": "skipped",
                "note": skipped.get("reason", ""),
                "license": "",
                "role": "skipped",
            }
        )

    n_ok = sum(1 for row in board_rows if row["status"] == "ok")
    return {
        "boards": board_rows,
        "sightings": sightings,
        "n_boards": len(board_rows),
        "n_boards_ok": n_ok,
        "n_sightings": len(sightings),
        "window_days": list(dates),
    }


def host_is_public_archive(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host in {
        "raw.githubusercontent.com",
        "zh.wikipedia.org",
        "freewechat.com",
        "top.baidu.com",
        "www.toutiao.com",
        "www.iesdouyin.com",
    }


__all__ = [
    "FORBIDDEN_PAYLOAD_KEYS",
    "archive_login_walled",
    "archive_url",
    "collect_archives",
    "host_is_public_archive",
    "load_catalog",
    "normalize_title",
    "parse_freewechat_index",
    "parse_justjavac",
    "parse_markdown_titles",
    "parse_wikipedia_mostviewed",
]
