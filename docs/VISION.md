# Vision — a measurement commons for the fight to keep the internet free

Palimpsest is **not an app that measures Chinese censorship.** It is the open
**measurement layer and data commons** for the global, decade-long fight against
content-layer censorship — a method, a dataset, and a federated observer network —
with China as the first instance and other authoritarian information spaces to follow.

Censorship is a long war, not a battle. The tools that matter in a long war are not
products that ship and end; they are **commons that persist** — the things everyone
else depends on. Palimpsest is built to be one of those.

## The thesis

Network-layer censorship (blocking, throttling) is well measured by OONI, Citizen Lab,
and GreatFire. Content-layer censorship — *what gets deleted, how selectively, how fast,
what is newly dangerous* — is still tracked largely by hand, one story at a time. That is
the gap Palimpsest fills, by treating **deletion itself as data**: the censor, watched
closely enough, becomes a sensor for what a state most fears.

## What already exists (not a promise — runnable today)

- **A validated method.** The DDTI (selectivity + novelty) is backtested against six
  documented events — Li Wenliang, Peng Shuai, Sitong Bridge, the White Paper protests —
  ranking the right term every time. See [VALIDATION.md](VALIDATION.md).
- **One public number.** The **Censorship Fear Index** distils it into a single 0–100
  figure a journalist or researcher reads at a glance — the seed of a recurring public signal.
- **An evidence-grounded lexicon.** 154 Chinese terms, each tagged by evasion mechanism
  and bound to the documented event that censored it, with phylogeny links tracing how
  euphemisms mutate (六四 → 8964 → 五月三十五日 → 八平方).
- **A forecaster.** It predicts which terms will intensify and which evasion classes will
  spawn the next euphemism — falsifiable "called shots" the evolution engine later confirms.
- **It already generalises.** A second country (Iran) loads from config alone. *Adding an
  authoritarian information space is a lexicon, not a rewrite.*
- **Safety as executable code.** Kill-switch, rate ceiling, hash-chained audit. The thing
  it watches is the censor, never the censored.

## The architecture of a commons

1. **Measure the firewall from its outside seam, never from inside.** The richest, *safest*
   signal lives at the cracks between inside and outside — platform forks (Douyin vs TikTok),
   diaspora reposts, circumvention-proxy blocklists. No one is ever put at risk.
2. **A federated observer mesh.** Many small, opt-in, signed, privacy-preserving vantages —
   no single point of risk or failure, tamper-evident via the hash-chained audit. This is the
   answer to capacity, velocity, and longevity at once.
3. **An open dataset + API.** Published for OONI, CDT, journalists, and researchers to ingest
   and build on — and to share their data back. The commons grows by being depended upon.
4. **Region packs.** China, then Iran, then wherever information is unfree — one method,
   many lexicons.
5. **Human-ratified evolution.** The vocabulary of censorship mutates; the gazetteer evolves
   with it, always with a human in the loop, never auto-published.

## Principles that never bend

- **Public data only. Watch the censor, never the censored.** No one inside is asked to act.
- **Fail loud, never silent.** A number we cannot stand behind (e.g. velocity from outside
  China) is shown suppressed, not faked.
- **Open and free for the people it serves.** Journalists and human-rights defenders never
  pay; the public good stays public.
- **Built to outlive any one maintainer.** Open governance, federated nodes, documented
  succession. A commons, not a startup.

## How to join

Run the demo, read [METHODOLOGY.md](METHODOLOGY.md) and [ETHICS.md](ETHICS.md), and:
ingest the feed, contribute a labelled event, harden a region pack, or stand up a seam
vantage. The internet stays free because people keep measuring the dark — together.
