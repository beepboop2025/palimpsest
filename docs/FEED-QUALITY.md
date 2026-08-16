# Feed quality contract

Palimpsest publishes eight logical feeds in two formats: RSS 2.0 for readers
and JSON Feed 1.1 for software. The public directory is `/feeds/`.

The feeds do not all make the same kind of claim. A subscriber must be able to
identify the item type, authorship, added value, and evidence limit without
opening the website.

## Public inventory

| Job | RSS | JSON Feed | Item responsibility |
| --- | --- | --- | --- |
| Live AI eval findings | `/journal/feed.xml` | `/journal/feed.json` | Palimpsest result |
| AI eval methods journal | `/evals/feed.xml` | `/evals/feed.json` | Palimpsest method article |
| Instrument measurements | `/news/instruments/feed.xml` | `/news/instruments/feed.json` | Palimpsest measurement |
| Source index plus measurements | `/news/feed.xml` | `/news/feed.json` | Explicitly mixed |
| China publisher source index | `/news/china/feed.xml` | `/news/china/feed.json` | Publisher source record with Palimpsest context |
| China situation synthesis | `/news/china/situation/feed.xml` | `/news/china/situation/feed.json` | Reports, social observations, and measurements with relations preserved |
| China censorship analysis | `/news/china/analysis/feed.xml` | `/news/china/analysis/feed.json` | Palimpsest analysis |
| Reviewed Telegram context | `/news/china/whispers/feed.xml` | `/news/china/whispers/feed.json` | Unverified context |

## Required item labels

Every title begins with a plain-language type label. JSON Feed items also carry
a stable `_palimpsest.kind` value.

- `[Palimpsest eval finding]`: a dated interpretation derived from sealed eval
  artifacts. It names controls, uncertainty, limitations, evidence coverage,
  and deterministic-template authorship.
- `[Palimpsest method article]`: a Palimpsest-authored methods article with
  named evidence and falsifiers.
- `[Palimpsest measurement]`: an output from a named Palimpsest instrument. It
  includes the current result, receipt, status, and limitation.
- `[Palimpsest analysis]`: bounded Palimpsest interpretation with citation and
  revision metadata.
- `[Source report]` or `[Corroborated source report]`: a publisher report kept
  attributed to the publisher. Palimpsest indexes the report and may add source
  grouping, related measurements, revision history, unknowns, and next checks.
  It does not adopt the report as a Palimpsest finding.
- `[Unverified context]`: sanitized, human-reviewed context that can suggest a
  later check. It does not count as evidence or corroboration.
- `[Situation synthesis]`: a deterministic projection that places attributed
  reporting, exact-link social context, and declared Observatory measurements
  together. It preserves each input's relation and does not imply verification.

## Transport and identity requirements

Each endpoint must:

1. parse as its declared format;
2. declare its own canonical `feed_url` or Atom `rel="self"` URL;
3. use stable, unique item IDs or GUIDs;
4. expose a direct item URL and preserve an original publisher URL separately
   for source records;
5. include a purpose-specific title and description;
6. use UTF-8 and explicit language metadata where the format supports it; and
7. be fetched network-only by the Palimpsest service worker because each feed
   is a mutable current-edition head.

The instrument-only feed must never identify itself as `/news/feed.*`. That
would make two different products appear to be one feed and can cause clients
to deduplicate or subscribe to the wrong stream.

## Source-report boundary

Source feeds intentionally do less than a newspaper. The original publisher
remains the destination for the report itself. Palimpsest adds inspectable
structure around it: attribution, source relationships, topic links,
measurement context when available, revisions, and explicit unknowns.

Multiple independent publisher groups are described as corroborated reporting,
not proof of truth, intent, impact, or causation. A single publisher group is
explicitly described as not independently verified or refuted by Palimpsest.

## Release check

`tests/test_feed_clarity.py` checks the inventory, endpoint identities, item
labels, typed JSON metadata, XML parsing, unique IDs, source boundaries, and
service-worker coverage. The existing generator tests continue to check the
underlying evidence and revision contracts.
