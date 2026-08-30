# For journalists — inspect censorship practice piece by piece

Palimpsest is a public-data censorship observatory. This page is the human path
from a captured post, article, or topic signal to a bounded censorship-practice
dossier and its underlying reconstruction. It is written for a reporter who is
not the operator.

The desk is [palimpsest.info/news/china/erasure/](https://palimpsest.info/news/china/erasure/).
Machine files:
[dossiers](https://palimpsest.info/readings/censorship-practice-dossiers-latest.json),
[raw JSON](https://palimpsest.info/readings/erasure-trail-latest.json), and
[raw CSV](https://palimpsest.info/readings/erasure-trail.csv). OpenAPI exposes
all three endpoints in [openapi.json](../openapi.json).

The dossier is the claim-bearing object. It states whether a disappearance was
observed, a practice was reported by a peer, a topic-level pattern was measured,
or a candidate still requires review. It also states the mechanism, explicit
actor attribution, timeline, measurements, counter-readings, and unknowns. An
article reporting censorship is not described as itself censored.

Each raw row is a **Palimpsest reconstruction**, not automatically a censorship
finding and not a CDT or Wayback wrapper.
The unique object is the joined record: public text we actually hold,
language (lexical CJK/Latin split, not machine translation), first-seen /
last-seen / last-confirmed-alive, every deletion confirmation, every
Wayback / archive.today / Ghostarchive address with timestamp brackets,
content hash, gazetteer hits, source and mirror URLs, and related
GDELT / OONI / GreatFire / CDT / Weibo / UNDERTEXT / Bleedthrough
joins. Uncertainty is named. Article bodies that were never captured
stay unnamed as missing — we do not invent them.

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
   Start with the qualifying dossiers. Search the raw table or download the CSV
   only when you need records that did not clear the claim gate.
2. **Qualify.** Preserve `observed_disappearance`, `peer_reported`,
   `pattern_signal`, or `review_required` in your wording. Read the actor basis
   before naming CCP, a PRC authority, a platform, or a local authority.
3. **Trail.** Read first-seen, last-seen, last-confirmed-alive (when known),
   the source URL, the Wayback snapshot or lookup, and the SHA-256 of the
   public excerpt. A lookup URL is an address to try. A snapshot URL is a
   witnessed capture.
4. **Export.** Download
   [censorship-practice-dossiers-latest.json](https://palimpsest.info/readings/censorship-practice-dossiers-latest.json),
   [erasure-trail.csv](https://palimpsest.info/readings/erasure-trail.csv)
   or
   [erasure-trail-latest.json](https://palimpsest.info/readings/erasure-trail-latest.json).
   The Frictionless package at [datapackage.json](https://palimpsest.info/datapackage.json)
   lists the JSON after each catalog rebuild.
5. **Cite.** Use the dossier's citation line. For a raw reconstruction, write:

   > Palimpsest erasure trail (`<key>`), “`<title>`”, first seen `<first_seen>`,
   > last seen `<last_seen>`, SHA-256 `<content_sha256>`. Public source:
   > `<source_url>`. Desk: https://palimpsest.info/news/china/erasure/`#<key>`

   Cite Palimpsest only for the evidence state it records. A raw archive
   transition may be reconstruction context rather than a live disappearance;
   neither object is proof of motive.

## Related surfaces

| Surface | What it is for |
| --- | --- |
| [Censorship-practice dossiers and erasure desk](https://palimpsest.info/news/china/erasure/) | Qualify, inspect mechanism and actor evidence, trail, export, cite |
| [OSINT China](https://palimpsest.info/osint-china.html) | Every China signal and its freshness |
| [China Situation](https://palimpsest.info/news/china/situation/) | Publisher reports + OSINT context with URL, hash, snapshot |
| [For researchers](https://palimpsest.info/for-researchers.html) | Methods, limits, and every public file |
| [China capture](CHINA-CAPTURE.md) | Which collector emits which observation field |

## Honesty rules that do not bend

- Public data only. Nobody inside China is asked to act.
- No exploits, no Wallbleed, no logged-in scraping, no Baike live fetch.
- A Situation OSINT join is context, not corroboration.
- Absence is a coverage gap. It is never relabelled as calm.
- An ordinary edit is not a deletion. A deletion is not automatically
  censorship. A censorship report is not proof the report itself was censored.
