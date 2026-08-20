"""Wikipedia recent-changes for gazetteer terms — titles and revisions only.

Public MediaWiki API. No editor usernames, no user pages, no talk profiling.
``rcprop`` is titles/timestamps/ids/sizes. Abstain if both wikis are silent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

from core.china_observation import enrich_observation, load_gazetteer_index

GAZETTEER_PATH = Path("config/zh_censorship_gazetteer.json")
ZH_API = "https://zh.wikipedia.org/w/api.php"
EN_API = "https://en.wikipedia.org/w/api.php"
# No ``user`` in rcprop — SAFETY.md: no editor profiling.
RCPROP = "title|timestamp|ids|sizes"
RC_LIMIT = 50
MIN_EN_GLOSS = 6


def load_gazetteer_needles(path: Path = GAZETTEER_PATH) -> tuple[frozenset[str], frozenset[str]]:
    if path == GAZETTEER_PATH:
        index, _terms = load_gazetteer_index()
    else:
        index = {}
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        for entries in (doc.get("categories") or {}).values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                zh = str(entry.get("zh") or "").strip()
                if len(zh) < 2:
                    continue
                index[zh] = {"zh": zh, "en": str(entry.get("en") or "").strip()}
    zh = frozenset(index)
    en = frozenset(
        row["en"].casefold()
        for row in index.values()
        if len(row.get("en") or "") >= MIN_EN_GLOSS
    )
    return zh, en


def title_matches(title: str, needles: frozenset[str]) -> str | None:
    if not title:
        return None
    folded = title.casefold()
    for needle in needles:
        if needle and (needle in title or needle.casefold() in folded):
            return needle
    return None


def recent_changes_url(api: str) -> str:
    query = urlencode(
        {
            "action": "query",
            "list": "recentchanges",
            "rcnamespace": "0",
            "rctype": "edit|new",
            "rcprop": RCPROP,
            "rclimit": str(RC_LIMIT),
            "format": "json",
        }
    )
    return f"{api}?{query}"


def parse_recent_changes(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    query = payload.get("query")
    if not isinstance(query, dict):
        return []
    rows = query.get("recentchanges")
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        out.append(
            {
                "title": title,
                "timestamp": str(row.get("timestamp") or "").strip() or None,
                "revid": row.get("revid"),
                "old_revid": row.get("old_revid"),
                "type": str(row.get("type") or "").strip() or None,
                "newlen": row.get("newlen"),
                "oldlen": row.get("oldlen"),
            }
        )
    return out


def article_url(lang: str, title: str) -> str:
    return f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"


def fetch_wiki(api: str, *, fetch: Callable[[str], str | None]) -> list[dict]:
    raw = fetch(recent_changes_url(api))
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parse_recent_changes(payload)


def collect_wikipedia_rc(
    *,
    fetch: Callable[[str], str | None],
    gazetteer_path: Path = GAZETTEER_PATH,
) -> tuple[list[dict], dict[str, int | bool]]:
    zh_needles, en_needles = load_gazetteer_needles(gazetteer_path)
    zh_rows = fetch_wiki(ZH_API, fetch=fetch)
    en_rows = fetch_wiki(EN_API, fetch=fetch)
    observations: list[dict] = []
    seen: set[str] = set()
    for lang, rows, needles in (
        ("zh", zh_rows, zh_needles),
        ("en", en_rows, en_needles | frozenset(n.casefold() for n in zh_needles)),
    ):
        for row in rows:
            title = row["title"]
            matched = title_matches(title, needles)
            if not matched:
                continue
            url = article_url(lang, title)
            if url in seen:
                continue
            seen.add(url)
            delta = None
            if isinstance(row.get("newlen"), int) and isinstance(row.get("oldlen"), int):
                delta = int(row["newlen"]) - int(row["oldlen"])
            text = (
                f"Wikipedia {lang} recent change: {title}. "
                f"type={row.get('type') or 'edit'}; revid={row.get('revid')}; "
                f"size_delta={delta}."
            )
            stamp = row.get("timestamp")
            observations.append(
                enrich_observation(
                    {
                        "terms": [matched],
                        "detected_at": stamp,
                        "title": title,
                        "text": text,
                        "url": url,
                        "source": "wikipedia-gazetteer-rc",
                        "rights_policy": "public-wikipedia-titles-revisions-only",
                    },
                    text=text,
                    source_url=url,
                    first_seen=stamp,
                    last_seen=stamp,
                    last_confirmed_alive=stamp,
                    provenance={
                        "collector": "wikipedia_gazetteer_rc",
                        "method": "MediaWiki recentchanges; rcprop excludes user",
                        "vantage": "outside-china-public-source",
                        "wiki": lang,
                        "schema_version": "palimpsest-china-observation.v1",
                        "method_version": 1,
                    },
                )
            )
            extra = observations[-1].setdefault("provenance", {})
            if isinstance(row.get("revid"), int):
                extra["revid"] = row["revid"]
            if isinstance(row.get("old_revid"), int):
                extra["old_revid"] = row["old_revid"]
            if row.get("type"):
                extra["rc_type"] = row["type"]
            if delta is not None:
                extra["size_delta"] = delta
    stats = {
        "zh_rc_rows": len(zh_rows),
        "en_rc_rows": len(en_rows),
        "matched": len(observations),
        "editor_fields": False,
        "silent": not zh_rows and not en_rows,
    }
    return observations, stats
