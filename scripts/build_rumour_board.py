#!/usr/bin/env python3
"""Seal the live-watch summary and publish the rumour-board desk.

No network. 4chan catalogs are accepted only when injected. Telegram preview
scraping is not authorized.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from xml.sax.saxutils import escape as xml_escape

from collectors import fourchan_catalog
from core import live_event as live_event_model
from core import rumour_board as rumour_model
from core import vantage_join as join_model
from core.place_gazetteer import load_place_gazetteer
from scripts import build_newsroom as newsroom_builder
from scripts import site_nav


ROOT = Path(__file__).resolve().parent.parent
TAP_REGISTRY = ROOT / "config" / "live_taps.json"
GAZETTEER = ROOT / "config" / "china_place_gazetteer.json"
WATCH_PATH = ROOT / "readings" / "live-watch-latest.json"
RUMOUR_PATH = ROOT / "readings" / "rumour-board-latest.json"
PAGE_PATH = ROOT / "news" / "china" / "rumour" / "index.html"
JSON_FEED_PATH = ROOT / "news" / "china" / "rumour" / "feed.json"
RSS_FEED_PATH = ROOT / "news" / "china" / "rumour" / "feed.xml"
SITE = "https://palimpsest.info"


class RumourBoardBuildError(ValueError):
    """The rumour desk cannot be sealed from the checked-in inputs."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _human(value: str) -> str:
    return value.replace("T", " ").replace("Z", " UTC")


def build_documents(
    *,
    generated_at: str | None = None,
    catalogs: Mapping[str, Any] | None = None,
    vantage: str = "box-local",
    readings_dir: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    generated = generated_at or _now()
    taps = live_event_model.load_tap_registry(TAP_REGISTRY)
    gazetteer = load_place_gazetteer(GAZETTEER)
    receipt = fourchan_catalog.collect(
        catalogs=catalogs,
        lemmas=gazetteer["lemmas"],
        observed_at=generated,
        vantage=vantage,
    )
    watch = live_event_model.empty_watch_document(
        generated,
        [
            {
                "tap_id": tap["tap_id"],
                "status": receipt["status"] if tap["tap_id"] == fourchan_catalog.TAP_ID else "not-attempted",
                "error_code": (
                    receipt["error_code"]
                    if tap["tap_id"] == fourchan_catalog.TAP_ID
                    else "not-wired"
                ),
            }
            for tap in taps
        ],
    )
    if receipt["status"] == "success":
        watch["status"] = "live" if receipt["accepted"] else "warming_up"
        watch["n_events"] = receipt["accepted"]
        watch["coverage"]["accepted"] = receipt["accepted"]
        watch["coverage"]["dropped"] = receipt["dropped"]
        watch["coverage"]["attempted"] = 1
        watch["coverage"]["not_attempted"] = watch["n_taps"] - 1
        for row in watch["taps"]:
            if row["tap_id"] == fourchan_catalog.TAP_ID:
                row["status"] = "success"
                row["accepted"] = receipt["accepted"]
                row["dropped"] = receipt["dropped"]
                row["error_code"] = None
        live_event_model.validate_live_watch(watch)
    rumour = rumour_model.project_events(receipt["events"], generated_at=generated)
    join = join_model.project_join(
        join_model.load_warehouse_readings(readings_dir or (ROOT / "readings")),
        generated_at=generated,
    )
    return watch, rumour, join


def _tap_table(watch: Mapping[str, Any] | None) -> str:
    if watch is None:
        return ""
    rows = "".join(
        (
            "<tr>"
            f"<td><code>{newsroom_builder._h(row['tap_id'])}</code></td>"
            f"<td>{newsroom_builder._h(row['status'])}</td>"
            f"<td>{int(row['accepted'])}</td>"
            f"<td>{newsroom_builder._h(row['error_code'] or 'none')}</td>"
            "</tr>"
        )
        for row in watch["taps"]
    )
    return f"""<section class="rb-methods" aria-labelledby="vantage-receipt-title">
  <p class="rb-stamp">Coverage receipt</p>
  <h2 id="vantage-receipt-title">{watch['n_taps']} public vantages configured. {watch['coverage']['not_attempted']} not attempted.</h2>
  <p>A zero is a coverage receipt, not silence. These doors write NDJSON on the node. Git only gets this summary.</p>
  <table class="rb-taps"><thead><tr><th>Tap</th><th>Status</th><th>Accepted</th><th>Halt</th></tr></thead><tbody>{rows}</tbody></table>
</section>"""


def _cards(rows: list[str], *, section_id: str, label: str) -> str:
    return f'<section class="rb-ledger" id="{section_id}" aria-label="{label}">{"".join(rows)}</section>'


def _join_section(join: Mapping[str, Any] | None) -> str:
    if join is None or join.get("status") != "WAREHOUSE_JOIN":
        return ""
    blocks: list[str] = [
        """<section class="rb-methods" aria-labelledby="join-title" id="join">
  <p class="rb-stamp">The join</p>
  <h2 id="join-title">Sit on warehouses incrementally. Then join what you already sealed.</h2>
  <p>These rows are projections of readings Palimpsest already published. They are not a new scrape. A shared host is not an exact URL join. Official text alone is the censor's story.</p>
</section>"""
    ]
    pulse_cards = [
        (
            f'<article class="rb-card rb-pulse" id="{newsroom_builder._h(row["pulse_id"])}">'
            f'<p class="rb-kicker">{newsroom_builder._h(row["warehouse"])} · '
            f'{newsroom_builder._h(_human(row["observed_at"]))}</p>'
            f'<h3>{newsroom_builder._h(row["title"])}</h3>'
            f'<p>{newsroom_builder._h(row["note"])}</p>'
            f'<p class="rb-relation">{newsroom_builder._h(row["relation"])}</p>'
            "</article>"
        )
        for row in join["pulses"]
    ]
    if pulse_cards:
        blocks.append(_cards(pulse_cards, section_id="pulses", label="Warehouse pulses"))
    host_cards = [
        (
            f'<article class="rb-card" id="{newsroom_builder._h(row["row_id"])}">'
            f'<p class="rb-kicker">{newsroom_builder._h(row["wire_source"])} · '
            f'{newsroom_builder._h(row["host"])} · '
            f'{newsroom_builder._h(_human(row["observed_at"]))}</p>'
            f'<h3><a href="{newsroom_builder._h(row["url"])}">{newsroom_builder._h(row["headline"])}</a></h3>'
            f'<p>{newsroom_builder._h(row["ooni_note"])}</p>'
            f'<p class="rb-relation">{newsroom_builder._h(row["relation"])}</p>'
            "<small>Host overlap only. Not an exact URL join. Not corroboration.</small>"
            "</article>"
        )
        for row in join["host_joins"]
    ]
    if host_cards:
        blocks.append(
            '<section class="rb-methods"><p class="rb-stamp">Host-surface joins</p>'
            "<h2>Wire headline, same host blocked in OONI.</h2>"
            "<p>This is the closest sealed pair today. It is still not the four-leg tuple.</p>"
            f"</section>{_cards(host_cards, section_id='host-joins', label='Host-surface joins')}"
        )
    demand_cards = [
        (
            f'<article class="rb-card" id="{newsroom_builder._h(row["row_id"])}">'
            f'<p class="rb-kicker">{newsroom_builder._h(row["surface"])} · rank '
            f'{int(row["rank"])} · {newsroom_builder._h(_human(row["observed_at"]))}</p>'
            f'<h3>{newsroom_builder._h(row["title"])}</h3>'
            f'<p class="rb-relation">{newsroom_builder._h(row["relation"])}</p>'
            "<small>Title and rank only. Public archive. Not a logged-in Weibo session.</small>"
            "</article>"
        )
        for row in join["demand"]
    ]
    if demand_cards:
        blocks.append(
            '<section class="rb-methods"><p class="rb-stamp">Captured demand</p>'
            "<h2>Query demand, not posts. Titles Palimpsest already captured.</h2></section>"
            + _cards(demand_cards, section_id="demand", label="Demand ranks")
        )
    archive_cards = [
        (
            f'<article class="rb-card" id="{newsroom_builder._h(row["row_id"])}">'
            f'<p class="rb-kicker">wayback · {newsroom_builder._h(row["status"])} · '
            f'{int(row["n_captures"])} captures</p>'
            f'<h3>{newsroom_builder._h(row["term"])}</h3>'
            + (
                f'<p><a href="{newsroom_builder._h(row["url"])}">{newsroom_builder._h(row["url"])}</a></p>'
                if row["url"]
                else "<p>No sealed URL on this watch row.</p>"
            )
            + f'<p class="rb-relation">{newsroom_builder._h(row["relation"])}</p></article>'
        )
        for row in join["archive"]
    ]
    if archive_cards:
        blocks.append(
            '<section class="rb-methods"><p class="rb-stamp">Archive watch</p>'
            "<h2>Still in Wayback, or not yet witnessed.</h2></section>"
            + _cards(archive_cards, section_id="archive", label="Wayback watchlist")
        )
    blocked_cards = [
        (
            f'<article class="rb-card" id="{newsroom_builder._h(row["row_id"])}">'
            f'<p class="rb-kicker">ooni-gfw · {int(row["anomaly_pct"])}% · '
            f'{int(row["measurements"])} measurements</p>'
            f'<h3>{newsroom_builder._h(row["host"])}</h3>'
            f'<p>{newsroom_builder._h(row["title"])}</p>'
            f'<p class="rb-relation">{newsroom_builder._h(row["relation"])}</p></article>'
        )
        for row in join["blocked"]
    ]
    if blocked_cards:
        blocks.append(
            '<section class="rb-methods"><p class="rb-stamp">OONI host anomalies</p>'
            "<h2>Blocked in OONI, at host grain.</h2></section>"
            + _cards(blocked_cards, section_id="blocked", label="OONI blocked hosts")
        )
    tuple_note = (
        f'<aside class="rb-limit"><p>{int(join["n_tuples"])} exact four-leg tuples '
        "cleared the join gate. Host overlap is not that product row.</p></aside>"
    )
    blocks.append(tuple_note)
    return "".join(blocks)


def render_page(
    document: Mapping[str, Any],
    watch: Mapping[str, Any] | None = None,
    join: Mapping[str, Any] | None = None,
) -> str:
    entries = document["entries"]
    if not entries:
        ledger = (
            '<section class="rb-empty" id="rumour"><p class="rb-stamp">Taken from rumour boards</p>'
            "<h2>No rumour-board row has cleared the filter.</h2>"
            "<p>Grow the named Telegram preview list first, through ScamShield "
            "pins and social_sources.json. Add 4chan only as a filtered board "
            "watcher if you still want that noise. /news and /int are the only "
            "boards considered. /pol and /b are noise and legal risk, not a "
            "vantage. Drop media. Drop anything that looks like a minor. If "
            "the filter is unsure, drop the post. Even then, 4chan is a weak "
            "China live source. It is rumor and diaspora chatter. Treat it as "
            "context, never as corroboration.</p></section>"
        )
    else:
        cards = "".join(
            (
                f'<article class="rb-card" id="{newsroom_builder._h(row["entry_id"])}">'
                f'<p class="rb-kicker">{newsroom_builder._h(row["surface"])} · '
                f'{newsroom_builder._h(_human(row["observed_at"]))}</p>'
                f'<h3>{newsroom_builder._h(row["title"])}</h3>'
                f'<p class="rb-relation">{newsroom_builder._h(row["relation"])}</p>'
                f"<small>gazetteer {newsroom_builder._h(', '.join(row['gazetteer_hits']) or 'none')}</small>"
                "</article>"
            )
            for row in entries
        )
        ledger = f'<section class="rb-ledger" id="rumour" aria-label="Rumour board rows">{cards}</section>'
    stamp = watch["generated_at"] if watch is not None else document["generated_at"]
    tap_count = watch["n_taps"] if watch is not None else 0
    join_status = join["status"] if join is not None else "COVERAGE_ONLY"
    pulse_count = join["n_pulses"] if join is not None else 0
    demand_count = join["n_demand"] if join is not None else 0
    host_count = join["n_host_joins"] if join is not None else 0
    stamp_line = (
        f"{join_status} · {_human(stamp)} · {pulse_count} warehouse pulses · "
        f"{demand_count} demand ranks · {host_count} host joins · "
        f"{tap_count} taps · {document['n_entries']} rumour rows"
    )
    body = f"""<body class="ps newsroom-page rumour-board-page">
{site_nav.render("/news/")}
<main id="main">
  <header class="rb-hero">
    <p class="rb-kicker">Palimpsest / Public vantages</p>
    <h1>More public vantages, continuously, then join them.</h1>
    <p>The way to get more grey data is more public vantages, continuously, then join them. One headline on Xinhua, gone from Baidu hot, blocked in OONI, still in Wayback: that tuple is the product. Official text alone is the censor's story.</p>
    <p class="rb-stamp">{newsroom_builder._h(stamp_line)}</p>
  </header>
  <div class="rb-shell">
    <nav class="rb-nav" aria-label="China intelligence tabs"><a href="/news/china/situation/">Situation synthesis</a><a href="/news/china/">Article stream</a><a href="/news/china/whispers/">Whispers</a><a aria-current="page" href="/news/china/rumour/">Public vantages</a></nav>
    {_join_section(join)}
    <section class="rb-methods" aria-labelledby="volume-title">
      <p class="rb-stamp">Four methods that change the volume</p>
      <h2 id="volume-title">Drink firehoses. Watch a URL list. Sit on warehouses. Never keep the HTML.</h2>
      <p>Drink firehoses. A public stream pushes every event. You filter with the gazetteer and write one line. That is how you get bulk without hitting a thousand sites. Wikimedia EventStreams, Bluesky Jetstream, Certificate Transparency.</p>
      <p>Watch a URL list the cheap way. Keep a reviewed set of public URLs. Every few seconds: HEAD or a tiny GET, store status and a hash. When the hash flips, that is the live event. Bodies stay out. This is how deletion research scales.</p>
      <p>Take other people's warehouses incrementally. GDELT drops every 15 minutes. OONI, Censored Planet, IODA, and Common Crawl already do the heavy lift. Pull only the new slice. You already join some of these. You do not sit on them.</p>
      <p>Never keep the HTML. Live bulk is NDJSON on the node: source, url, title, hash, time, vantage. Rotate daily. Python reads the tail. Git only gets a summary.</p>
    </section>
    <section class="rb-methods" aria-labelledby="grey-stack-title">
      <p class="rb-stamp">The legal grey stack</p>
      <h2 id="grey-stack-title">Sit on what the law forces out. Treat Wikipedia as a seismograph. Query demand, not posts. Read the country as numbers.</h2>
      <p>Listed-company announcements on 巨潮 are a public JSON firehose. Titles, times, PDF links. Same idea at city scale: 政府信息公开. Titles and links only.</p>
      <p>Wikimedia EventStreams is a public SSE. Filter zhwiki plus gazetteer titles. You want deletes, revdels, and sudden protection, not every typo. Pair it with the hourly pageviews dump. You never asked anyone in-country.</p>
      <p>Baidu, Toutiao, Douyin, Bilibili: titles and ranks only, abstain if a cookie wall appears. Do not chase signed search APIs or danmaku profiles. The signal is the rank flip.</p>
      <p>OpenSky, OpenStreetMap minute diffs, AQICN and CNEMC-style station feeds. None of that is an article. Together it is a live physical pulse you can join to the wire when a city goes quiet, a plant vanishes off the map, or flights stop.</p>
      <p>Chinese that lives outside the GFW: PTT, public HK boards, official CGTN/Xinhua YouTube titles, Bluesky Jetstream filtered on the gazetteer. Same Telegram rule: named surfaces, no login, never corroboration by itself.</p>
      <p>Birth and death of names. Certificate Transparency for .cn and Chinese O-names. A new host today that is on a block list tomorrow is a live pair.</p>
    </section>
    {_tap_table(watch)}
    <aside class="rb-limit"><p>{newsroom_builder._h(document["scope"])}</p><p>Rumour-board rows do not corroborate a publisher report and do not add an independent source group.</p></aside>
    {ledger}
    <section class="rb-methods" aria-labelledby="hard-no-title">
      <p class="rb-stamp">What you do not add</p>
      <h2 id="hard-no-title">Logged-in Weibo or WeChat is how people get hurt.</h2>
      <p>What you do not add: logged-in Weibo or WeChat, private messages, follower graphs, a browser session you did not own, or asking anyone inside to run a probe. That is not a stronger censorship tool. That is how people get hurt, and it is why CensorWatch is feature-flagged and isolated.</p>
    </section>
    <ol class="rb-limits">{"".join(f"<li>{newsroom_builder._h(item)}</li>" for item in document["limitations"])}</ol>
  </div>
</main>
<footer class="nw-footer"><div class="nw-shell"><a href="/news/china/situation/">Situation desk</a> · <a href="/docs/LIVE-EVENT-INGEST.md">Live-event ingest</a> · <a href="/protocol/live-watch-v1.schema.json">Watch schema</a>.</div></footer>
{site_nav.FOOT}
</body></html>"""
    return newsroom_builder._head(
        title="Public vantages · Palimpsest",
        description=(
            "More public vantages, continuously, then join them. One headline "
            "on Xinhua, gone from Baidu hot, blocked in OONI, still in Wayback: "
            "that tuple is the product."
        ),
        canonical=f"{SITE}/news/china/rumour/",
        page_type="website",
        modified_at=stamp,
        feed_base="/news/china/rumour",
        extra_styles=("/assets/rumour-board.css",),
        json_ld={
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": "Public vantages",
            "url": f"{SITE}/news/china/rumour/",
            "description": (
                "The way to get more grey data is more public vantages, "
                "continuously, then join them. Official text alone is the "
                "censor's story."
            ),
        },
    ) + body


def _feed_items(
    document: Mapping[str, Any],
    join: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    items = [
        {
            "id": row["entry_id"],
            "url": f"{SITE}/news/china/rumour/#{row['entry_id']}",
            "title": row["title"],
            "date_published": row["observed_at"],
            "content_text": row["relation"],
        }
        for row in document["entries"][:80]
    ]
    if join is None:
        return items
    for row in join.get("host_joins") or []:
        items.append(
            {
                "id": row["row_id"],
                "url": f"{SITE}/news/china/rumour/#{row['row_id']}",
                "title": row["headline"],
                "date_published": row["observed_at"],
                "content_text": row["relation"],
            }
        )
    for row in join.get("demand") or []:
        items.append(
            {
                "id": row["row_id"],
                "url": f"{SITE}/news/china/rumour/#{row['row_id']}",
                "title": row["title"],
                "date_published": row["observed_at"],
                "content_text": row["relation"],
            }
        )
    for row in join.get("pulses") or []:
        items.append(
            {
                "id": row["pulse_id"],
                "url": f"{SITE}/news/china/rumour/#{row['pulse_id']}",
                "title": row["title"],
                "date_published": row["observed_at"],
                "content_text": row["relation"],
            }
        )
    return items[:200]


def render_json_feed(
    document: Mapping[str, Any],
    join: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Palimpsest public vantages",
        "home_page_url": f"{SITE}/news/china/rumour/",
        "feed_url": f"{SITE}/news/china/rumour/feed.json",
        "description": document["scope"],
        "items": _feed_items(document, join),
    }


def render_rss(
    document: Mapping[str, Any],
    join: Mapping[str, Any] | None = None,
) -> str:
    items = "".join(
        (
            "<item>"
            f"<title>{xml_escape(row['title'])}</title>"
            f"<link>{xml_escape(row['url'])}</link>"
            f"<guid isPermaLink=\"false\">{xml_escape(row['id'])}</guid>"
            f"<pubDate>{xml_escape(row['date_published'])}</pubDate>"
            f"<description>{xml_escape(row['content_text'])}</description>"
            "</item>"
        )
        for row in _feed_items(document, join)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "<channel>"
        "<title>Palimpsest public vantages</title>"
        f"<link>{SITE}/news/china/rumour/</link>"
        f"<description>{xml_escape(document['scope'])}</description>"
        f'<atom:link href="{SITE}/news/china/rumour/feed.xml" rel="self" type="application/rss+xml" />'
        f"{items}"
        "</channel></rss>\n"
    )


def write_outputs(
    watch: Mapping[str, Any],
    rumour: Mapping[str, Any],
    join: Mapping[str, Any] | None = None,
) -> dict[Path, bytes]:
    outputs = {
        WATCH_PATH: newsroom_builder._pretty_json(watch),
        RUMOUR_PATH: newsroom_builder._pretty_json(rumour),
        PAGE_PATH: render_page(rumour, watch, join).encode("utf-8"),
        JSON_FEED_PATH: newsroom_builder._pretty_json(render_json_feed(rumour, join)),
        RSS_FEED_PATH: render_rss(rumour, join).encode("utf-8"),
    }
    for path, payload in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return outputs


def check_outputs() -> None:
    watch = json.loads(WATCH_PATH.read_text(encoding="utf-8"))
    rumour = json.loads(RUMOUR_PATH.read_text(encoding="utf-8"))
    live_event_model.validate_live_watch(watch)
    rumour_model.validate_rumour_board(rumour)
    page = PAGE_PATH.read_text(encoding="utf-8")
    if "Taken from rumour boards" not in page:
        raise RumourBoardBuildError("rumour page lost its rumour label")
    if "independent source group" not in page:
        raise RumourBoardBuildError("rumour page lost its independence disclaimer")
    if "that tuple is the product" not in page:
        raise RumourBoardBuildError("page lost the join-tuple product line")
    if "more public vantages, continuously, then join" not in page:
        raise RumourBoardBuildError("page lost the public-vantage product line")
    if "Never keep the HTML" not in page:
        raise RumourBoardBuildError("page lost the NDJSON storage rule")
    if "Sit on warehouses incrementally" not in page:
        raise RumourBoardBuildError("page lost the warehouse-join section")
    if "Host overlap only" not in page and "warehouse pulses" not in page:
        raise RumourBoardBuildError("page lost the sealed warehouse projection")
    if "do not add an independent source group" not in page:
        raise RumourBoardBuildError("page lost the independence lock")


def _parse_now(value: str) -> str:
    text = value.strip()
    try:
        moment = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError as exc:
        raise RumourBoardBuildError("--now must be a valid ISO-8601 timestamp") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise RumourBoardBuildError("--now must include a timezone")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--now",
        help="seal at this timezone-aware ISO-8601 clock (for deterministic replay)",
    )
    args = parser.parse_args(argv)
    try:
        if args.check:
            check_outputs()
            return 0
        generated_at = _parse_now(args.now) if args.now else None
        watch, rumour, join = build_documents(generated_at=generated_at)
        write_outputs(watch, rumour, join)
        return 0
    except (
        RumourBoardBuildError,
        live_event_model.LiveEventError,
        rumour_model.RumourBoardError,
        join_model.VantageJoinError,
    ) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
