# Palimpsest Intelligence Commons

This directory is the machine-readable public map connecting Palimpsest,
Seiche, ScamShield, and NarcoScope without pretending that their measurements
have the same grain, cadence, licence, or evidentiary meaning.

- [`manifest-v1.json`](manifest-v1.json) is the UI and integration manifest.
- [`../../protocol/lab-evidence-envelope-v1.schema.json`](../../protocol/lab-evidence-envelope-v1.schema.json)
  is the strict shared record schema.
- [`../../protocol/lab-evidence-envelope-v1.md`](../../protocol/lab-evidence-envelope-v1.md)
  defines the behavioral rules that JSON Schema cannot express.

The manifest's top-level and row keys are a stable v1 UI contract. Consumers
must reject unknown schema versions and must not infer a connection from two
projects merely appearing in the same lane.

## Four visible lanes

| Lane | Owning public projects | What it can say |
| --- | --- | --- |
| Information controls | Palimpsest | Which public deletion, blocking, interference, erasure, and model-control signals reported, with freshness and coverage attached. |
| Monetary plumbing | Palimpsest and Seiche | What observed CNY funding, fixing, cross-border, and related plumbing measures show under each source's evidence and licence gate. |
| Illicit-market observables | NarcoScope | What public aggregate seizure, retail-price, mortality, and wastewater modalities report at their native jurisdiction and cadence. |
| Reviewed laundering and scam signals | ScamShield and Palimpsest | What privacy-minimized patterns or reviewed monetary events occurred inside an explicitly bounded, authorized sampling frame. |

The lanes stay separate on purpose. They are a structured reading surface, not
one composite “China risk” number. No lane establishes that drug proceeds,
scams, censorship, or underground banking caused a funding-market move. No
typology match establishes a person's guilt, identity, source of funds, or
membership in a network.

## Public project roles and live links

| Project | Public role | Current public surface |
| --- | --- | --- |
| Palimpsest | OSINT roll-up, evidence protocol, provenance ledger, review boundary | [OSINT China](https://palimpsest.info/osint-china.html) and its [machine-readable reading](https://palimpsest.info/readings/osint-china-latest.json) |
| Seiche | Money-market plumbing and sealed market context | [Seiche](https://seiche.info/) and the [CN-CNY overview](https://api.seiche.info/api/v2/markets/CN-CNY/overview) |
| ScamShield | Authorized-surface assessment and reviewed aggregate export | [Public repository](https://github.com/beepboop2025/scamshield) and the active [reviewed intelligence pack](https://palimpsest.info/integrations/scamshield/intelligence-pack-v1.json) |
| NarcoScope | Aggregate drug-market observatory | [Live observatory](https://narcoscope.com/) |

Each project remains independently governed. The manifest links public
surfaces; it does not transfer ownership, merge licences, or make one project a
runtime dependency of another.

## Directional connections

`connections` are one-way and carry an honest status:

- `ACTIVE` means the named public contract or current project integration is in
  use.
- `REVIEW_GATED` means the local bridge exists, but its output remains private
  or a human-review candidate until a separate publication decision.
- `REPOSITORY_READY` means the producer, schema, deterministic artifact and
  validation gates exist in source control, but the canonical deployment has
  not yet been verified live.
- `PLANNED` reserves a contract and public source without claiming that the
  outbound aggregate exists.

The active Palimpsest → ScamShield path publishes inert typologies and source
references. The review-gated ScamShield → Palimpsest path creates private
Evidence Capsules and privacy-minimized review candidates; nothing is
auto-published. The active Palimpsest → Seiche path is external China context,
not a hidden model feature.

The reverse Seiche → Palimpsest projection is deliberately `PLANNED`. Its live
API link is useful now, but a future envelope must still pass Seiche's
observation, eligibility, provenance, and redistribution gates. When displayed,
it remains context-only: it does not enter a composite, forecast, trading rule,
or causal model.

## NarcoScope public aggregate

NarcoScope now produces the deterministic object at exactly:

```text
/data/narcoscope-palimpsest-v1.json
```

Its schema is `narcoscope.palimpsest.china-aggregate.v1`; the root is an object
with five official China dataset envelopes: retail prices, seizures, precursor
corridor incidents, aggregate OFAC designation coverage and wildlife
confiscation coverage. Each envelope preserves its native temporal grain,
publisher, source URL, edition, local input date and SHA-256, measurement
semantics, data, and limitations. The checked-in Palimpsest copy is byte-for-byte
pinned from the producer and is not a substitute for verifying the canonical
deployment.

The connection is `ACTIVE`: NarcoScope's production Vite deployment exposes the
canonical JSON with the same bytes as the reviewed Palimpsest pin. Availability
does not relax the interpretation rules. Annual records remain annual and
missing modalities remain missing. The object contains no designation subject
name, alias, exact address, entity identifier, raw communication, private lead,
or entity-to-entity allegation.

## Publication gate

A public consumer must, in order:

1. parse with duplicate-key and non-finite-number rejection;
2. validate the declared contract schema; for Lab Evidence Envelopes, also
   enforce their time, interval, reference, hash, and supersession rules;
3. preserve the source's native cadence and declare the sampling frame;
4. apply privacy, review, source-dominance, freshness, and redistribution gates;
5. render `OBSERVED`, `DERIVED`, and `SCENARIO` values as visibly different
   evidence classes; and
6. keep every limitation beside the value it constrains.

For ScamShield, reviewed victim-reported loss and verified-transfer aggregates
may carry bounded sums only after their own publication gates pass. Other
message-derived monetary classes remain counts or withheld values. A reviewed
sample is not a total market estimate, a national proceeds estimate, or proof
of a predicate offence.

For Seiche, intelligence-commons observations are context. They do not acquire
final-vintage status, real-money eligibility, or independent corroboration by
being copied through Palimpsest. For all sources, a primary endpoint and a
mirror backed by the same upstream pipeline are one source group.

## Privacy, licence, and private-analysis boundary

The shared envelope is aggregate-only and has no field for raw messages or
exact indicators. The public manifest contains only public URLs. It contains no local filesystem
location, secret, case record, private repository reference, implementation,
model parameter, or calibration.

Private owner-operated analysis may consume validated public envelopes inward.
It must not send private code, calibrated output, restricted source values, or
scenario numbers back into these public projects. A public project must remain
buildable and intelligible without access to that private layer.

Redistribution is not inherited through a connection. `LINK_ONLY`,
`RESTRICTED`, or `UNKNOWN` values remain redacted; a content hash proves which
bytes were used but does not grant permission to republish them.

## Contract checks

The focused tests are standard-library-only and offline:

```bash
python3 -m pytest -q tests/test_intelligence_commons_contract.py
```

They pin manifest keys, project and lane references, directional status,
approved public URL hosts, the NarcoScope artifact location and pinned-copy
privacy rules, the shared schema's
privacy and evidence gates, the non-causality language, and the absence of local
paths or indicator-shaped values.
