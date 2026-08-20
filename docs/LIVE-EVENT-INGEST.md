# Live-event ingest

The way to get more grey data is more public vantages, continuously, then
join them. One headline on Xinhua, gone from Baidu hot, blocked in OONI,
still in Wayback: that tuple is the product. Official text alone is the
censor's story.

## Four methods that change the volume

Drink firehoses. A public stream pushes every event. You filter with the
gazetteer and write one line. That is how you get bulk without hitting a
thousand sites.

Watch a URL list the cheap way. Keep a reviewed set of public URLs. Every
few seconds: HEAD or a tiny GET, store status and a hash. When the hash
flips, that is the live event. Bodies stay out.

Take other people's warehouses incrementally. GDELT drops every 15 minutes.
OONI, Censored Planet, IODA, and Common Crawl already do the heavy lift.
Pull only the new slice.

Never keep the HTML. Live bulk is NDJSON on the node: source, url, title,
hash, time, vantage. Rotate daily. Python reads the tail. Git only gets a
summary.

## What is stored where

- **Node:** `/var/lib/palimpsest/data/live/YYYY-MM-DD.ndjson`
  One metadata line per observation. Bodies stay out. Day files rotate.
- **Git:** `readings/live-watch-latest.json` and
  `readings/rumour-board-latest.json`
  Sealed coverage summaries. A zero is a coverage receipt, not silence.

The repository `data/` directory is already gitignored. Local tests may write
there. They must not commit day files.

## Relations

Every live event carries one locked relation. Rumour-board rows use
`rumour-board-context-not-corroboration`. They appear on
`/news/china/rumour/` and as a situation briefing. They never increment
independent publisher groups. Rank flips, wiki edits, CT names, and
Telegram previews use the same rule: context, never corroboration.

## Telegram first

Grow named channels through `config/social_sources.json` and the ScamShield
pinset. `t.me/s/` preview scraping is not authorized. The social-observation
contract rejects `/s/`, `/c/`, and `joinchat` permalinks.

See `config/telegram_growth.json`.

## 4chan, if you still want that noise

`collectors/fourchan_catalog.py` accepts injected catalogs only. Boards are
`/news` and `/int`. `/pol` and `/b` raise. Media fields are never copied
forward. A missing gazetteer hit or an unsure minor-safety read drops the
thread. `PALIMPSEST_FOURCHAN_ENABLED=1` does not open a socket. Even then,
4chan is a weak China live source. It is rumor and diaspora chatter.

## Place gazetteer

`config/china_place_gazetteer.json` is empty until it is ratified. An empty
lemma list matches nothing. Do not reuse `config/zh_censorship_gazetteer.json`
as a city list.

## What you do not add

Logged-in Weibo or WeChat, private messages, follower graphs, a browser
session you did not own, or asking anyone inside to run a probe. That is
not a stronger censorship tool. That is how people get hurt, and it is why
CensorWatch is feature-flagged and isolated.

## Build

```bash
PYTHONPATH=. python -m scripts.build_rumour_board
PYTHONPATH=. python -m scripts.build_rumour_board --now 2026-08-20T09:50:33Z
PYTHONPATH=. python -m scripts.build_rumour_board --check
```

`--now` seals the public receipts at a replay clock. The evidence mesh rejects
a live-watch or rumour-board `generated_at` that sits after the mesh clock.
