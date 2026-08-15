# ScamShield ↔ Palimpsest integration

This directory is Palimpsest's reviewed input surface for ScamShield. The raw
Telegram companion is deliberately outside it: the dedicated Dragon Den bot
uses native Telegram forwards between configured public channels, while this
repository receives only private Evidence Capsules and reviewed, sanitized
derivatives. The integration is bidirectional but deliberately local and
bounded:

1. Palimpsest publishes `intelligence-pack-v1.json`. ScamShield loads this
   inert, versioned typology pack; the file cannot contain regexes or code.
2. ScamShield sends only a structured provenance assessment to
   `scripts/scamshield_bridge.py` over stdin. Raw Telegram text is hashed and
   is not sent by default.
3. Palimpsest constructs and verifies an Evidence Capsule v1 with distinct
   typed claims for the money-flow detector, broad threat tier/families, and
   provenance hypotheses, then stores it in the ignored runtime directory
   `var/scamshield-inbox/`.
4. `scripts/scamshield_feed.py` creates privacy-minimized, human-review
   candidates for other Palimpsest surfaces. It emits JSONL to stdout and does
   not publish, call a webhook, or open a network connection.
5. `scripts/review_dragon_whisper.py` can promote one eligible public-channel
   capsule into `palimpsest-dragon-whispers.v1` only after explicit sanitation
   and China-relevance approval. The generated China tab, RSS and JSON Feed all
   consume that same reviewed artifact.

## ScamShield configuration

```bash
export SCAMSHIELD_PALIMPSEST_ROOT=/absolute/path/to/palimpsest-site
export SCAMSHIELD_PSEUDONYM_KEY='a long installation-local secret'
# Optional; these are the defaults:
export SCAMSHIELD_PALIMPSEST_OUTBOX=var/scamshield-inbox
export SCAMSHIELD_SHARE_MIN_TIER=WATCH
```

When `SCAMSHIELD_PALIMPSEST_ROOT` is present, ScamShield prefers this
directory's intelligence pack and sends WATCH-or-higher assessments to the
local bridge. If the bridge fails, Telegram detection continues and the local
assessment records the failed bridge status.

## Generate outbound review candidates

```bash
python3 scripts/scamshield_feed.py \
  --inbox var/scamshield-inbox > /tmp/scamshield-review.jsonl
```

By default, `TYPOLOGY_MATCH` hypotheses are withheld because message text alone
cannot establish source of funds. A controlled analyst surface may add
`--include-typology-matches`; the output still says
`HUMAN_REVIEW_REQUIRED` and never auto-publishes.

`DIRECT_LINK` is accepted only when the authoritative observation's IOC kind
and exact value bind to an IOC extracted from the current message. The bridge
process receives a minimal environment; ScamShield does not pass Telegram
tokens, market API keys, database URLs, or its pseudonym key to Palimpsest.

## Review one individual public-channel signal

The individual Whispers lane is stricter than the general review-candidate
format. It accepts only capsules whose collection receipt says both
`surface=public_channel` and `authorization=public`. Private submissions,
authorized private channels, clean classifications, raw messages and exact
indicator values cannot enter it.

```bash
python3 scripts/review_dragon_whisper.py \
  /secure/review/public-channel-capsule.json \
  --reviewed-at 2026-08-15T08:00:00Z \
  --reviewer-role china-desk-editor \
  --review-note "Reviewed for privacy, scope, and analytical restraint." \
  --headline "Sanitized pattern-level headline" \
  --summary "A reviewer-authored synthesis that contains no source clue." \
  --why-it-matters "The independent reporting question this pattern raises." \
  --uncertainty "The claim, origin, intent, prevalence, and parties remain unknown." \
  --next-check "Compare with independently archived public reporting." \
  --next-check "Seek a separate attributable source class." \
  --approve-sanitized-whisper \
  --confirm-china-relevance
python3 -m scripts.build_newsroom
```

The public outputs are `/news/china/whispers/`, its RSS and JSON Feed, and
`/readings/dragon-whispers-latest.json`. The capsule SHA-256 retained there is a
review receipt, not proof that the Telegram statement is true.

## Data boundary

- Private capsule: assessment structure, exact IOCs, matched indicator terms,
  evidence limitations, and the SHA-256 of the original message; no raw
  message by default.
- Public candidate: tiers, threat families, IOC counts, pseudonymized source,
  corroborated/direct provenance, and limitations; no IOC values or matched
  message fragments.
- Collection scope: user-submitted messages, configured public channels, and
  administrator/operator-authorized surfaces only. The integration never
  claims visibility into all of Telegram.
- Raw companion: separate token and process, explicit public-channel allowlist,
  native forwarding with a mandatory warning, no Palimpsest publication and no
  classifier gate. Protected content is not bypassed or copied.
- Public individual whisper: reviewer-authored context, tier/family labels,
  exact-IOC counts, uncertainty, verification moves and a capsule receipt; no
  source clues, raw wording, links, named parties or corroboration claim.
