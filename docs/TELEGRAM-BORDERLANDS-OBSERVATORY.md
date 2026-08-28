# Telegram China-Myanmar Borderlands Observatory

Reviewed: 2026-08-27

This is Palimpsest's authorization-gated Telegram source registry for the
China-Myanmar borderlands. Its distinguishing discovery focus is Shan:
Shan-language and Shan-region newsrooms sit beside Kachin/China-border
reporting, wider Myanmar context, Chinese state-media agenda surfaces,
Chinese-language newsrooms, and labelled aggregators and advocacy sources.

The registry is not a bulk Telegram corpus. A public channel address establishes
neither permission nor corroboration. Heavy Palimpsest analysis must use the
underlying licensed/open product evidence, not a scraped Telegram dataset.

## Reviewed coverage and authority

The registry contains 50 reviewed locators:

- 3 project-owned Dragon Den broadcast channels that are collectable;
- 36 external entries retained as discovery-only despite their prior `active`
  identity/availability review;
- 9 candidate entries with unreliable public message coordinates;
- 2 quarantined handles whose returned identity did not match the request;
- 0 public discussion groups collected.

Schema v3 adds `collection_authorization` to every profile. The only collectable
states are:

- `project-owned`;
- `explicit-consent`, backed by an ongoing consent record; and
- `licensed`, backed by a reviewed license and scope.

`discovery-only` is never fetched. `load_channels()` returns only an active
broadcast channel with a collectable authorization, and `collect_channels()`
repeats the same check before calling the network. Passing a source dictionary
directly cannot bypass the gate.

The registry remains useful for source mapping: it records identity, desk,
regions, languages, independence group, source class, verification clock, risk
tier, and the reason the locator matters. SSPP Info and Taang TV, for example,
remain labelled conflict-party statement surfaces. That label can guide future
licensed research but never turns repetition into independent confirmation.

## Current bounded collection

Only these project-owned handles are currently returned to the collector:

- `DragonDenWhispers`;
- `DragonDenCyber`; and
- `DragonDenBorderlands`.

For an authorized row, a run fetches the newest
`https://t.me/s/{handle}` preview and may resume a bounded historical walk with
`?before=<message_id>`. The default is two pages, and the hard ceiling is 20.
The kill switch, safe HTTP fetcher, source identity match, and shared rate
ceiling still apply. A login wall, empty preview, unreachable page, or handle
mismatch stays an explicit gap.

Page receipts keep locator and response hashes, status, clocks, and post counts,
not raw HTML. No participant, sender, phone, profile, reaction, view, precise
location, DM, private-chat, or media-binary fields exist in the warehouse.

## Warehouse boundary

Authorized observations use the SQLite warehouse at
`data/telegram-public-channels/telegram-public-channels.sqlite3`, or the
directory selected by `PALIMPSEST_TELEGRAM_WAREHOUSE_DIR`. The warehouse uses a
single-writer lock, WAL, full synchronous commits, `0700` directories, and a
`0600` database. It stores source policy at capture time, message coordinates,
bounded links, clocks, hashes, media-presence type, content versions, and run
receipts.

The public latest document is bounded. Only the three project-owned sources can
produce full observations or feed the existing social-spread join. External
registry entries produce no network receipt, private body, public metadata row,
or implicit zero while they are discovery-only.

## Adding a consented or licensed source

Changing `collection_state` is insufficient. A review must add a profile with
one of the collectable authorization states and document, outside the public
repository when necessary:

1. the rights holder or consenting person;
2. the exact channels and data fields in scope;
3. whether historical material is included;
4. allowed storage, analysis, publication, and retention;
5. grant time, expiry/revocation path, and proof reference;
6. the Telegram access method allowed by the current platform terms; and
7. a deletion procedure that removes downstream private copies.

An expired, absent, ambiguous, or revoked record must resolve to
`discovery-only` before the next fetch. There is no CLI override for this gate.

## Why forks do not change the boundary

Official TDLib and the official local Bot API server may improve asynchronous
operation, files, and webhook connectivity for authorized first-party workflows.
User-mode forks, member-list extensions, deleted-message capture, obfuscation,
or account rotation do not create permission and are not production candidates.
Telegram flood waits are honored; they are not evaded with additional accounts,
tokens, proxies, or forks.

## Promotion checklist

Before calling this lane live, verify:

1. the exact deployed commit contains registry schema v3 and warehouse schema
   v1;
2. `load_channels()` returns exactly the three project-owned Dragon Den rows;
3. a discovery-only row passed directly to `collect_channels()` fails before
   the fetch callback is invoked;
4. the persistent warehouse path resolves inside the intended host bind;
5. `public_spread=true` remains limited to the three Dragon Den handles;
6. the deployed timer does not claim external coverage or a complete history;
7. candidate, quarantine, login-wall, silence, and rights-gated states remain
   explicit gaps rather than zeros.
