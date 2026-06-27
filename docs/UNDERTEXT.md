# UNDERTEXT — Differential Censorship Tomography

> Recovering the *scriptio inferior* of China's information space — the erased lower-text
> of a palimpsest that bleeds through when you read from many angles.

UNDERTEXT is the **active** measurement layer of Palimpsest. Where the passive legs (China
Digital Times, FreeWeibo) *witness* censorship after the fact, UNDERTEXT *measures* it.

## 1. The problem

China's true information state is opaque. The highest-value signals — official misconduct
and real economic distress — are often not absent from the public internet; they are
**public for a moment, then scraped off**, and they are served **differentially**: the
censor can show different realities to different observers (by geography, account cohort,
surface, and time).

Palimpsest already encodes the thesis that *deletion velocity is the leading indicator*
(the DDTI). But a passive feed reader only sees what a handful of sources happen to surface.
UNDERTEXT closes that gap by observing actively, from many angles.

## 2. The method (one line)

> Fire the **same logical query** at China's public surfaces from **many controlled vantage
> points**, content-address every response, and treat the **divergence** — between vantages
> and across time — as the intelligence.

This is a CT scan of the censorship apparatus: you cannot see inside the opaque body, so you
fire probes *through* it from many angles and reconstruct the hidden structure from how each
probe is attenuated and distorted.

**Lineage (respected censorship-measurement science, not intrusion).** This synthesizes
three established traditions: OONI (network-layer interference measurement), Citizen Lab
(differential-account studies of WeChat/keyword censorship), and GreatFire/FreeWeibo
(confirmed-deletion surfacing). The contribution is the *synthesis* — automated,
content-addressed, many-vantage, closed-loop, and feeding the DDTI index directly.

## 3. The vantage tensor

```
observation = f( query × geo × cohort × surface × time )
```

| Axis | What it varies | Examples |
| --- | --- | --- |
| `geo` | where the request appears to come from | `GLOBAL` (outside view) vs an in-country view |
| `cohort` | what kind of account/session asks | anonymous web, aged account, new account |
| `surface` | which public endpoint | search results, topic feeds, public registries |
| `time` | re-probing on a schedule | deletion velocity falls out for free |

Tomography is reconstructing the censorship function over this tensor. The open core in this
repository (`collectors/undertext.py`) ships the tensor, the divergence math, and a generic
**web vantage** that uses the optional `PALIMPSEST_PROXY` egress seam. The simplest live
configuration — `GLOBAL` (direct) vs an in-country egress view of the same query — is already
real intelligence: *what the global internet sees versus what an in-country exit is served.*

> **In-country vantage backends** (residential exits, in-app device reads) are
> deployment-specific *infrastructure*, not part of this open core, and are never a person:
> see [OSINT_SOURCES.md](OSINT_SOURCES.md) and [ETHICS.md](ETHICS.md).

## 4. The key idea: divergence is the payload

A content-addressed cache normally uses a *hit* to skip work. UNDERTEXT content-addresses
**reality** instead, so a hit that returns *different content* is the alarm:

```
observation_key = content_key(query, lang, geo, cohort, surface)   # excludes time + content
content_fp      = content_key(normalized_body)                      # the actual reality
```

| Pattern | Meaning |
| --- | --- |
| same key, later time, was present → now absent | **deletion** (latency ⇒ severity grade) |
| same key, later time, present but `content_fp` changed | **mutation** — a quiet edit (e.g. a notice altered after award) |
| same query+time, two `geo`, fingerprints differ | **geo fork** — localized blocking ⇒ localized stress |
| same query+time, author-cohort present, public-cohort absent | **cohort fork** — a shadowban tell |

Deletion / shadowban / geo-targeting detection becomes a cheap hash-diff over a replayable
baseline store. **Replayability is what makes a divergence claim evidentiary** — a divergence
you cannot reproduce is not a finding. Fingerprints use the same sha256/`0x1f` scheme as the
dedup layer, and privileged actions are recorded in the hash-chained audit log
(`core/governance.py`).

## 5. What it looks for

**Official misconduct** — public-but-mutable sources: court-judgment portals (silently
de-published judgments), discipline-inspection notices, procurement portals, land auctions.
The content-addressed diff catches the **act of redaction** — for example a case that vanishes
right after the official involved is promoted.

**Economic distress** — official statistics can hide stress; the censor's behavior does not.
Wage-arrears protests, factory closures, and bank-run rumors tend to be deleted **fastest in
the affected province**, so deletion-velocity-by-geo is a real-time economic-stress nowcast.
This is a transparency/accountability reading, never a market or trading signal.

## 6. Discovering the emerging frontier (human-ratified)

Divergences on *unknown* terms are exactly how a new euphemism or a breaking scandal first
shows up — at the instant the censor reacts, often ahead of the news. UNDERTEXT does not run
an autonomous loop that tries to provoke the censor. Instead, the terms that produce real
divergence are surfaced as **candidates for the human-ratified gazetteer**
(`processors/gazetteer_evolution.py`): a human authors the final entry. This keeps the
sensitive layer human-authored (see [ETHICS.md](ETHICS.md)) while still discovering new
vocabulary faster than manual curation.

## 7. Governance (the analytical-OSINT line, held)

- **Public reads only.** No account creation, no CAPTCHA-solving, no impersonation, no
  intrusion, no injection.
- **Measurement, not manipulation.** UNDERTEXT observes differential responses; it never
  alters the surfaces it measures.
- **Evidentiary.** Content-addressed fingerprints + hash-chained audit ⇒ every divergence is
  reproducible.
- **Gated and kill-switchable.** Active probing runs only behind `core/governance.py`: the
  kill switch halts it instantly (fail-safe) and the rate ceiling keeps it polite. The web
  vantage consults both before any outbound request.

## 8. Where it plugs in

```
collectors/undertext.py
  Vantage / Probe / Observation     # the tensor coordinates and one reading
  DivergenceDetector                # time-divergence + cross-vantage forks
  JsonBaselineStore                 # replayable baselines (you only see deletion if you remember)
  WebVantagePoint                   # governance-gated generic web vantage (PALIMPSEST_PROXY seam)
  divergence_to_observation()       # adapter → the existing DDTI observation schema
```

Each `Divergence` maps onto Palimpsest's `deletion_signal` vocabulary and flows into the
**existing** DDTI selectivity/novelty index (`processors/ddti_index.py`) and the
human-ratified gazetteer evolver. UNDERTEXT is the *active* front-end to the *passive* loop
Palimpsest already ships. See `tests/test_undertext.py` for the end-to-end proof that an
UNDERTEXT deletion scores in the DDTI index.

## 9. The one decision that defines behavior

The `observation_key` granularity is UNDERTEXT's safety knob: too coarse and you miss real
divergence; too fine and nothing ever compares equal across time. It is documented at the
definition site so a reviewer can see and tune the choice.
