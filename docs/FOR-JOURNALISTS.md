# For journalists — find a deleted post, see the trail, export it, cite it

Palimpsest is a public-data censorship observatory. This page is the
human path from a disappeared public post to a citable evidence row.
It is written for a reporter who is not the operator.

The desk is [palimpsest.info/news/china/erasure/](https://palimpsest.info/news/china/erasure/).
Machine files: [JSON](https://palimpsest.info/readings/erasure-trail-latest.json)
and [CSV](https://palimpsest.info/readings/erasure-trail.csv).
OpenAPI: `GET /readings/erasure-trail-latest.json` and
`GET /readings/erasure-trail.csv` on [openapi.json](../openapi.json).

## What Palimpsest captures

- **Public posts** that were already published and that a collector later
  recorded as deleted, mutated, unreachable, or reconstructed from archives.
- **Public deletion and blocking ledgers** (China Digital Times, GreatFire,
  FreeWeibo-style feeds) when those feeds answer. If every ledger is
  unreachable the runner **abstains** rather than publish a zero.
- **Wayback reconstructions** of those public URLs, including lookup
  addresses and witnessed snapshots when a capture exists.
- **GFW injector telemetry** from separate instruments (OONI, Censored
  Planet, Inside View, Bleedthrough). Those are network measurements, not
  post deletions, and they keep their own pages.

## What Palimpsest does not capture

- Private WeChat, private messages, or anything behind a personal login.
- Classified systems, internal government dockets, or captcha-walled
  case files.
- In-country accounts, or anyone inside China asked to run a probe.
- Follower graphs, comments, engagement, locations, media binaries, or
  consumer profiles.

Watch the censor, never the censored. The site will **not** invent a live
reading when a collector is silent.

## Find → trail → export → cite

1. **Find.** Open the [erasure desk](https://palimpsest.info/news/china/erasure/).
   Each row is a public record. Search the table, or download the CSV and
   filter locally.
2. **Trail.** Read first-seen, last-seen, last-confirmed-alive (when known),
   the source URL, the Wayback snapshot or lookup, and the SHA-256 of the
   public excerpt. A lookup URL is an address to try. A snapshot URL is a
   witnessed capture.
3. **Export.** Download
   [erasure-trail.csv](https://palimpsest.info/readings/erasure-trail.csv)
   or
   [erasure-trail-latest.json](https://palimpsest.info/readings/erasure-trail-latest.json).
   The Frictionless package at [datapackage.json](https://palimpsest.info/datapackage.json)
   lists the JSON after each catalog rebuild.
4. **Cite.** Use the row's citation line, or write:

   > Palimpsest erasure trail (`<key>`), “`<title>`”, first seen `<first_seen>`,
   > last seen `<last_seen>`, SHA-256 `<content_sha256>`. Public source:
   > `<source_url>`. Desk: https://palimpsest.info/news/china/erasure/`#<key>`

   Cite Palimpsest as the observatory that recorded a public disappearance,
   not as a witness inside China and not as proof of motive.

## Related surfaces

| Surface | What it is for |
| --- | --- |
| [Erasure desk](https://palimpsest.info/news/china/erasure/) | Find, trail, export, cite |
| [OSINT China](https://palimpsest.info/osint-china.html) | Every China signal and its freshness |
| [China Situation](https://palimpsest.info/news/china/situation/) | Publisher reports + OSINT context with URL, hash, snapshot |
| [For researchers](https://palimpsest.info/for-researchers.html) | Methods, limits, and every public file |
| [China capture](CHINA-CAPTURE.md) | Which collector emits which observation field |

## Honesty rules that do not bend

- Public data only. Nobody inside China is asked to act.
- No exploits, no Wallbleed, no logged-in scraping, no Baike live fetch.
- A Situation OSINT join is context, not corroboration.
- Absence is a coverage gap. It is never relabelled as calm.
