# New methods — six observation surfaces

> Every method here is one more angle on the same body. UNDERTEXT taught the platform to read
> censorship as *divergence* across `observation = f(query × geo × cohort × surface × time)`.
> Each method below adds a new **surface** to that tensor and feeds the **existing** DDTI
> selectivity / novelty index unchanged — no new scoring, no new schema, no new trust in any
> model. This document states, for each, the *method*, how it *maps to DDTI*, and how it *holds
> the two safety lines*.

This is a companion to [UNDERTEXT.md](UNDERTEXT.md). Read that first: it defines the vantage
tensor, `content_key` / `normalize_body`, the `DivergenceDetector`, the `JsonBaselineStore`,
and `divergence_to_observation()` — the shared machinery all six methods reuse rather than
re-derive.

## The two lines (non-negotiable, held by every method here)

1. **Public / permitted reads only. Watch the censor, never the censored.** Outside-the-wall
   infrastructure only. No person inside China is ever asked to act; no account creation, no
   CAPTCHA-solving, no impersonation, no jailbreak, no intrusion, no injection, and no
   in-country residential proxy that depends on a person. Every live network leg is
   **injectable**, **inert by default**, **governance-gated** (it consults the kill switch and
   rate ceiling in `core/governance.py` before any outbound request), and **fail-soft** (a
   blocked or failed source returns `present=False` / abstains — never a false zero).

2. **No Beijing-aligned model is ever the analyst.** A Chinese model may be the *subject* under
   observation; it never decides what is sensitive and never judges another response. All
   classification is transparent, lexical / rule-based, and auditable from the text alone — it
   ships as the finding's own evidence.

And the platform-wide rule: **fail loud, not silent.** A number that cannot be stood behind
(velocity from outside China, an unreachable feed) is shown *suppressed*, never faked.

---

## 1. Generative Firewall — `collectors/generative_firewall.py`

**Method.** The censor moved up a layer: a CAC-regulated Chinese LLM is a deployed censorship
apparatus you can interrogate from outside the wall, a thousand times an hour, without putting
any person at risk. This is UNDERTEXT pointed at a model instead of a web page — `surface`
becomes the model id, `geo` becomes `MODEL:<provider>`, and the `cohort` axis becomes the
*ask-language* (asking in Chinese vs English famously flips refusal behaviour). Two censorship
layers are kept strictly separate:

- **Layer 1 — weights-baked.** Present in the open-weights model itself, persists with no
  network. Flat refusals (`REFUSAL_FORK`) and state-narrative substitution (`PARTY_LINE`). Run
  locally at temperature 0 with a fixed seed, so a refusal or party-line answer is
  **deterministic and replayable** — a flip across runs is a real policy change, not sampling
  noise. Default-on, local.
- **Layer 2 — API-layer supervisor.** A hosted endpoint that watches its own stream and, on a
  sensitive hit, *wipes already-emitted tokens mid-generation* and substitutes a refusal
  (`STREAM_SCRUB`). The gap between "token emitted" and "token wiped" is a censorship latency
  readable from outside the wall. Governance-gated and **inert by default** — it is the only
  part that touches a remote Chinese-operated API.

**Maps to DDTI.** Each divergence goes through `divergence_to_observation()` like any other: a
refusal is a deletion (`present=False`); a `PARTY_LINE` and a `REFUSAL_FORK` are selectivity
tells; a `STREAM_SCRUB` carries the one *velocity* signal the social-web legs cannot reach —
and local-replay wall-time is **never** reported as velocity (that is your own GPU's speed, not
the censor's reaction time).

**Holds the lines.** *Line 1:* we send a plain question and record the answer — no jailbreak
(a jailbreak would measure our cleverness, not the censor's policy), no impersonation, no
account abuse. The remote streaming leg is injectable, inert, and gated. *Line 2:* the model is
the **subject**, never the analyst — what to probe is the human-authored ratified gazetteer, and
every judgement (`is_refusal`, `looks_like_party_line`) is lexical and auditable from the text.
A control panel of non-aligned models calibrates what an un-censored answer looks like. *Fail
loud:* an unreachable backend **abstains** (it is not "the censor refused").

## 2. CDN-Edge Differential — `collectors/cdn_edge.py`

**Method.** A Chinese commercial CDN (Alibaba Cloud, Tencent EdgeOne, Wangsu, Baishan) is a
globally-readable cache of the *same* content object, replicated to hundreds of edge POPs
worldwide. A geo-policy decision that mutates an object can therefore be read from the outside,
POP by POP, with no person in China. When the same `host + path` returns a different content
fingerprint at the Frankfurt edge than at the Hong Kong edge, you have witnessed a **geo-fork**
of the content surface. The mechanism is the standard `curl --resolve` technique: pin the edge
IP at the socket layer while keeping the hostname as SNI + `Host:`, so you choose the POP while
TLS and certificate validation stay intact. `geo` becomes the POP label (`CN-HK` / `SG` / `FRA`
/ `LAX`), `surface` becomes `cdn:<provider>:<host><path>`.

**Maps to DDTI.** Reuses `content_key` / `normalize_body` and
`DivergenceDetector.cross_vantage` directly — two differing fingerprints under different `geo`
are already labelled `GEO_FORK` — then `divergence_to_observation()` carries the fork into the
selectivity index unchanged.

**Holds the lines.** *Line 1:* edge IPs are read only from POPs located **outside** mainland
China (Hong Kong is allowed but labelled `CN-HK`, never merged into `GLOBAL`, never read as
in-mainland). POP / edge-IP discovery is deployment-specific and **injected**, never an active
enumeration crawler baked into the core. *Line 2:* all block / fork classification is
lexical / structural and auditable; the subject is a cache, not a person. *Fail loud:*
in-country deletion *velocity* cannot be measured from outside the wall, so
`in_country_deletion_velocity()` returns `None` (suppressed); the overseas cache-propagation
latency is a *different* quantity, reported separately and explicitly labelled.

## 3. Blocklist Archaeology — `collectors/blocklist_archaeology.py`

**Method.** The **inverse** of the rest of the platform. The passive legs *infer* sensitivity
from deletions; a client-embedded blocklist is the censor's **ground-truth trigger list**,
pre-labelled by the platform itself. A term newly present in version N+1 of a client blocklist
is a dated censorship directive — the cleanest novelty signal in the whole system, because the
censor labelled the term *for us*. The module consumes **already-published** client-extracted
artifacts (Citizen Lab's open `chat-censorship` corpus and friends), parses the real on-disk
formats, diffs across versions, and emits each newly-added term. A blocklist is simply a new
`surface` in the tensor.

**Maps to DDTI.** The emitted dict mirrors `undertext.divergence_to_observation` field-for-field
so `gazetteer_evolution.mine_candidates` and `ddti_index.compute_selectivity_novelty` consume it
unchanged; `undertext.content_key` gives each parsed list a replayable audit fingerprint. A
newly-added term becomes a **novelty** observation.

**Holds the lines.** *Line 1:* the module never acquires a client binary or a live keyword-list
URL — acquisition is out-of-tree and **injected** (default: a local-fixtures reader, no network
default). No DRM cracking: an encrypted blob is handled only via an injectable `decryptor` seam
(default `None`, inert); ciphertext with no decryptor raises `BlocklistEncryptedError` (loud),
never an in-module crack. *Line 2:* severity / category / phenomenon are lexical and rule-based,
computed against the human-authored gazetteer and the published code book; a new term is
proposed into the gazetteer only through the existing human-ratification ledger — this module
never writes the gazetteer. *Fail loud:* legacy-encoding fallbacks, encrypted-blob skips, and
sampled-list low confidence are all surfaced explicitly.

## 4. Silence Detection — `processors/silence_index.py`

**Method.** The cleanest censorship leaves a *hole where coverage should be*, not a scar. The
directive apparatus more often tells outlets *not to report* a topic at all — pre-emptive
silence — so there is no 404 to count, only an absence: a topic globally enormous yet
domestically flat (Peng Shuai, the Tangshan assault, the 2023 youth-unemployment-statistics
suspension). This processor operationalises `gdelt_cross_signal.py`'s blackout / containment
reading into a per-topic **Silence Index**, reusing that module as the single source of truth
rather than re-deriving its scoring. The hard problem — not false-flagging ordinary local
disinterest — is handled by two mandatory guard rails: a **China-nexus gate** (no nexus →
`out_of_scope`), and **baseline-aware decoupling** (the signal is a *change in the coupling
ratio* `expected_domestic = coupling_baseline × global_norm`, not a one-shot global≫domestic
level). A reading with neither a coupling baseline nor lexicon corroboration **abstains** — the
guard rail cannot be silently switched off.

**Maps to DDTI.** A scored silence is emitted as an observation into the same pipeline, giving
DDTI a reading for topics that produce *no deletion event at all* — closing the blind spot of a
deletion-counting index.

**Holds the lines.** *Line 1:* GDELT aggregates already-published world news; the
domestic-volume input is **injected** from a permitted outside-the-wall public source (Weibo
hot-search absence, Baidu coverage counts, WeiboScope / GreatFire datasets, CDT presence) and is
never a person-dependent in-country read. *Line 2:* every decision is arithmetic plus
transparent lexical gazetteer matching, auditable from the text. *Fail loud:* if the domestic
input is unreachable or the topic is not loud anywhere, the reading **abstains** (`score = None`)
and is not emitted — shown suppressed, never a false zero.

## 5. GitHub-as-Refuge — `collectors/github_refuge.py`

**Method.** GitHub is the one large platform Beijing cannot fully block without breaking its own
developer economy, so it became a *refuge*: censored material is mirrored into repos that
survive behind HTTPS the GFW cannot filter at repo level (996.ICU, nCovMemory, Terminus2049,
CDT mirrors, A4 / Urumqi-fire archives — all already public knowledge). This collector watches a
human-curated watchlist plus GitHub's own transparency repos (`github/dmca`,
`github/gov-takedowns`) and emits an observation when it sees **pressure** (a takedown naming a
watched repo or a censor-aligned complainant; a repo returning 404/451 or flipping
private / archived / disabled) and/or a **preservation reflex** (a fork-swarm or star-burst —
the Streisand / insurance response). The temporal coincidence of pressure + preservation is the
high-confidence event.

**Maps to DDTI.** Pressure and preservation events become observations on the platform-pressure
surface, feeding the same selectivity / novelty machinery; the coincidence event carries the
highest severity.

**Holds the lines.** *Line 1:* the public GitHub REST API serves data the platform exposes
anonymously; **read-only, no writes ever** — we never star, fork, or file issues (that would be
us *participating* in the reflex, and could endanger a maintainer). We watch the censor's
takedown metadata, never the censored content itself, and collect no personal data about a
maintainer. Inert by default (empty watchlist + no-op fetch → zero network calls);
governance-gated before every read. *Line 2:* classification is arithmetic (counts, burst
ratios) plus substring lexicon matching against a human-curated watchlist with evidence
bindings. *Fail loud:* a 403 is almost always our own rate limit, not a takedown — it abstains,
and we never claim in-China reachability (that is the UNDERTEXT / OONI lane).

## 6. Baike Redaction-Diff — `collectors/baike_redaction.py`

**Method.** Baidu Baike is a state-moderated encyclopedia that silently rewrites contested
entries and has deliberately removed the public's ability to view its own edit history — the
act of redaction is hidden. We reconstruct it from outside the wall by content-addressing the
entry over time and diffing it against the open record (Chinese Wikipedia). Two things are
measured on this `surface`:

- **`ENCYCLOPEDIA_FORK`** — Baike vs Chinese Wikipedia on the same contested entity. Because the
  two prose bodies always differ in register, comparing raw `content_fp` would flag every pair
  (the `PLATFORM_FORK` trap). The payload is a diff of **derived facets**: terms the open record
  carries that Baike omits (`wiki_only_sensitive`), a reference list collapsed to state media
  (`sourcing_monoculture`), missing years of biography (`bio_gap`), and total absence
  (`absent_on_baike`).
- **`STATE_REWRITE`** — Baike rewriting its own history over time. A `DivergenceDetector` catches
  `DELETION` (present→absent) and `MUTATION` (content-fp change); a transparent lexical
  `state_rewrite_signal` then scans the two snapshots for sensitive-term excision, biographical
  truncation, dated-paragraph deletion, sourcing collapse, euphemism substitution, and role
  removal. Two or more markers relabels the mutation as a `STATE_REWRITE` — the act of rewriting
  history, reconstructed because Baike removed the public diff.

**Maps to DDTI.** Both signals go through `divergence_to_observation()` into the existing
selectivity / novelty index; the reconstructed snapshots are the replayable evidence.

**Holds the lines.** *Line 1:* Baike and Wikipedia are public encyclopedias read with plain
anonymous GETs from outside-the-wall infrastructure. We deliberately do **not** authenticate
into Baike's restricted revision history even though an account could — that is not a public
read and would break Line 1; reconstructing the diff from our own snapshots is both the safe and
the correct method. *Line 2:* Baike is the **subject**; every judgement (what is sensitive, fork
vs normal edit, what counts as a state rewrite) is made by transparent lexical / structural
rules shipped as the finding's own `reasons[]` evidence — no LLM decides sensitivity. *Fail
loud:* a blocked or failed fetch is `present=False` with a `fetch_failed` marker kept **distinct**
from a real deletion (never-created, deleted, locked, disambiguation, and fetch-failed are five
different states); from outside the wall the redaction *moment* is unobservable, so velocity is
poll-bounded and shown suppressed.

## 7. Wayback Reconstruction — `collectors/wayback_vantage.py`

**Method.** The platform's one honest blocker is *velocity*: from outside the wall the moment a
Chinese post dies is unobservable, so the passive legs only witness a deletion after an editor
documents it, and UNDERTEXT is *poll-bounded* by our own cadence — we learn a page died somewhere
between two of *our* visits, and only for pages we were already watching. But something else was
watching almost everything, for two decades, and timestamps every capture: the **Internet
Archive**. This surface turns the Archive's **CDX index** into a *retroactive in-country
observer*. For a public Chinese URL, we pull its capture timeline (`collapse=digest`, so each row
is a *distinct content state*) and read the transitions: a `200`→`404/410` is a **DELETION**
bracketed in `[last_live_capture .. first_gone_capture]`; a change in the Archive's own SHA-1
**content digest** between two live captures is a **MUTATION / silent redaction** — detected from
CDX metadata alone, no body fetch. `geo` becomes `ARCHIVE`, `cohort` becomes `crawler`, `surface`
becomes `wayback:<host>`, and the content fingerprint is the Archive's digest. Every event ships
the exact `web.archive.org/web/<ts>/<url>` snapshot on each side — evidence that is a *permanent
public artifact*, not a baseline we alone hold.

**Maps to DDTI.** A reconstruction is emitted as an `undertext.Divergence` and flows through the
**same** `divergence_to_observation()` adapter as every other surface, into
`ddti_index.compute_selectivity_novelty` and the gazetteer evolver. A Wayback DELETION is a
selectivity/velocity tell; a MUTATION is the state-rewrite signal `baike_redaction` reconstructs,
now carrying a real archive-witnessed timestamp instead of a poll-bounded one.

**Holds the lines.** *Line 1:* we read the **Internet Archive**, an outside-the-wall public
mirror — never Chinese infrastructure, never a person, no account, no CAPTCHA, no injection. The
CDX fetch is injectable, **inert by default** (no fetch supplied → zero network), governance-gated
(kill switch + rate ceiling before every request), and fail-soft (an unreachable CDX abstains). A
URL the Archive never captured live is `no_baseline`, kept strictly distinct from a real
`200`→`404`; a live→redirect is treated as *uninformative*, never a fabricated takedown. *Line 2:*
every judgement is arithmetic over HTTP status codes and lexical over a maintainer-authored marker
table, auditable from the CDX row and the snapshot alone — no model decides sensitivity. *Fail
loud:* the deletion moment is known only to within the capture bracket, so velocity is published
as that explicit `[last_live .. first_gone]` bracket, never a false-precise instant; and if CDX is
unreachable for every watched URL the runner abstains rather than publish a hollow all-unknown
signal.

---

## How they fit together

| Method | New `surface` | Primary DDTI signal | Live leg (inert by default) |
| --- | --- | --- | --- |
| Generative Firewall | a state-aligned LLM | selectivity (refusal / party-line) + velocity (stream-scrub) | injected model backend |
| CDN-Edge Differential | an overseas CDN cache POP | selectivity (geo-fork) | injected edge-fetch + POP map |
| Blocklist Archaeology | a client-shipped keyword list | novelty (newly-added term) | injected list loader |
| Silence Detection | a global-vs-domestic coverage gap | selectivity of *absence* (blackout) | injected domestic-volume feed |
| GitHub-as-Refuge | a takedown-transparency / mirror-repo feed | selectivity (pressure) + novelty | injected GitHub read |
| Baike Redaction-Diff | a state encyclopedia | selectivity + mutation (state rewrite) | injected fetch (`PALIMPSEST_PROXY` seam) |
| Wayback Reconstruction | the Internet Archive capture timeline | velocity (deletion bracket) + mutation (silent redaction) | **live** — injected CDX fetch (public archive) |

All six are **standard-library only** in their analytical core, fully unit-testable offline with
an injected fake fetch / generate, and each emits into the DDTI index and gazetteer-evolution
pipeline through the *same* adapter UNDERTEXT already uses. Nothing here reaches real Chinese
infrastructure until a deployer explicitly injects a live source and clears the governance gate.
This is a measurement commons, developed in the open as a public good — never a product, and
never a tool that monetizes the people or topics it observes.
