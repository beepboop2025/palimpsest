# Measuring GFW DNS injection in 2026: what stopped working, and why

*Palimpsest methodological note. All measurements below were taken on 2026-07-30/31 from
two vantages: a European datacenter (AS24940, Hetzner Falkenstein) and a South Asian
residential connection. The residential vantage is deliberately described by region and
latency rather than by city or ISP; what the argument needs is that it was a household line
roughly 90-150 ms from mainland China, and every RTT figure below is unchanged. Every claim
here is reproducible with `dig` and a free API key nowhere.*

---

## The short version

For years, anyone outside China could measure the Great Firewall's DNS injection for free,
because the injector sprayed forged answers at any DNS query crossing the border. That
property, usually called collateral damage, made the GFW self-documenting to the whole
internet.

A researcher trying the obvious 2026 version of that experiment will conclude the injection
has stopped. It has not. The experiment has quietly become wrong, and it fails in the
direction that produces a confident, publishable, false negative.

Two things changed. One is about **where you aim**, and one is about **what you aim at**.

---

## Trap 1: the "Chinese public resolver" is not in China

The natural experiment is to query a well-known Chinese resolver from outside and see if the
answer comes back poisoned:

```
$ dig +short torproject.org @114.114.114.114     # 114DNS
$ dig +short torproject.org @223.5.5.5           # AliDNS
$ dig +short torproject.org @119.29.29.29        # DNSPod
```

From both our vantages, all three return the **genuine** Tor Project addresses, matching an
uncensored Cloudflare baseline exactly. Same for `www.facebook.com`, `rfa.org`,
`www.voanews.com`, `zh.wikipedia.org`, `minghui.org`, `dw.com`. Not one poisoned answer.

The obvious reading is that the GFW no longer poisons these domains. The obvious reading is
wrong, and the tell is in the latency:

```
223.5.5.5    icmp min/avg  75.5 / 80.1 ms     from the residential vantage
119.29.29.29 icmp min/avg  76.8 / 78.5 ms     from the residential vantage
```

Mainland China from South Asia is roughly 90-150 ms. Seventy-five milliseconds is not
mainland China. **These addresses are anycast**, and a query from outside China lands on a
PoP outside China. The packet never approaches the border, so no on-path injector ever sees
it. You have not measured the firewall; you have measured a CDN edge.

This is the single most expensive mistake available here, because it looks exactly like a
finding. We made it ourselves before the control test caught it.

**Rule: never treat a resolver as an in-China vantage without proving the packet entered
China.** RTT floor against a known-mainland reference is the cheap check. `CHAOS TXT
id.server` is the better one.

## Trap 2: aim at routed mainland addresses, and it is still there

Aim the same query at ordinary **routed mainland IP addresses** instead, and the injector
is immediately visible. From the South Asian residential vantage:

| target | `torproject.org` | `rsf.org` | `example.com` |
|---|---|---|---|
| 202.108.22.5 | `209.145.54.50` | `128.121.146.235` | *silent* |
| 180.101.50.242 | `216.234.179.13` | `108.160.162.98` | *silent* |
| 61.135.169.121 | `202.106.1.2` | `43.226.16.8` | *silent* |
| 220.181.38.148 | `64.33.88.161` | `182.50.139.56` | *silent* |

Every censored answer is bogus (`torproject.org` is really `95.216.163.36`), every answer
differs from the last, and `202.106.1.2` is a forged address documented for over a decade.
Reproduced from Germany (AS24940) with different forged values again.

The `example.com` column is what makes this a measurement rather than an anecdote. The same
target, in the same second, stays **silent** for an uncensored domain and **answers** for a
censored one. That pairing rules out "the host simply replies to everything" and it rules
out a broken prober.

**So the injector is alive.** What has narrowed is which paths it fires on: queries toward
real foreign resolvers and ordinary mainland hosts still trigger it; the anycast public
resolvers that everyone reaches for do not, because those queries never arrive.

## Trap 3: the forged pool rotates, so a hardcoded list rots

Twenty sequential probes of one target for one domain returned **ten distinct forged
addresses**. Measured from inside China via volunteer probes, four cities were handed four
different answers for `torproject.org` in the same round:

```
Beijing    AS45090  ->  4.36.66.178
Guangzhou  AS45090  ->  199.59.150.44
Shenzhen   AS37963  ->  64.33.88.161
Chongqing  AS45090  ->  203.161.230.171
```

Any classifier built on a fixed table of known-forged IPs is stale the moment it ships. Ours
was a 14-entry table and it was wrong.

**Rule: establish truth per round from a control arm outside the censored network, and call
an answer forged when it shares no address with that control set.** Never enumerate the
liar's vocabulary; define the truth and measure the distance from it.

A corollary that is easy to get backwards: *the answers disagreeing is not the vantages
disagreeing*. Four cities receiving four different forged IPs are four cities that all agree
the domain is blocked. Compare classification state, never raw answers.

## Trap 4: silence is not evidence of absence

`is_probably_dark(ip)` style checks infer "no resolver here" from **silence**. But a prober
whose egress is filtered also sees silence everywhere. Both produce a tidy, uniform,
completely empty result set, and the pipeline downstream cannot tell them apart.

The fix is a **positive control**: in the same round, probe something known to be injected.
If that comes back clean too, the vantage is blind and the round must publish
`VANTAGE_BLIND`, not "no censorship detected". An observatory may only report that it saw
nothing when it can also show it would have seen something.

---

## What we would do differently, stated as a checklist

1. **Prove your vantage is where you think it is.** RTT floor, `id.server`, or a known
   in-country reference. Anycast will lie to you politely.
2. **Pair every censored probe with an uncensored control** against the *same* target in the
   *same* round. This is the difference between a measurement and a story.
3. **Derive ground truth per round.** No hardcoded forged-IP lists.
4. **Distinguish the three nulls.** "Censor was quiet", "sensor was deaf", and "we never
   asked" are different findings, and only the first is an observation about China. Rate
   limits and budget exhaustion belong in their own state, never folded into a rate.
5. **Compare state, not payloads.** Rotating forged values are one phenomenon; regional
   divergence in whether a domain is blocked is another.
6. **Attribute carefully.** A forged answer proves on-path interference. It does not by
   itself prove *national* filtering: large Chinese cloud operators run their own resolver
   interception, so a result drawn entirely from one ASN is indistinguishable from that
   operator's behaviour. Require agreement across independent networks.

## On consent, which is not a methodology footnote

The cheapest in-China vantages today are volunteer probe networks. Those pools contain
household connections alongside cloud VMs.

Someone hosting a probe consented to run measurements. They did not consent to have their
home connection, inside China, emit DNS queries for `wikileaks.org`. We shipped a collector
that drew household ASNs for exactly that and had to correct it within the hour.

**Pin sensitive queries to datacenter ASNs.** The cost is real and belongs in the published
reading: what you then measure is filtering as experienced on cloud networks, which is not
necessarily what a household sees. Say so, rather than quietly enjoying the better coverage.

This is the one error class no downstream guard can catch, because the harm does not land on
the data.

---

## Reproducing this

```bash
# Trap 1 — the anycast tell (expect ~75ms from South Asia, i.e. NOT mainland)
ping -c 3 223.5.5.5
dig +short torproject.org @223.5.5.5          # genuine answers: you missed the border

# Trap 2 — routed mainland target, censored vs control in the same breath
dig +short torproject.org @202.108.22.5       # forged, and different every time
dig +short example.com    @202.108.22.5       # silent

# Trap 3 — watch the pool rotate
for i in $(seq 1 20); do dig +short torproject.org @111.201.128.37; sleep 2; done | sort -u
```

Palimpsest publishes the resulting signals at
[palimpsest.info](https://palimpsest.info): `bleedthrough` measures injection outward from
outside the wall, and `inside-view` measures what probes inside China actually receive.
Both carry their control state in the reading, because a censorship number without its
control is not a measurement.
