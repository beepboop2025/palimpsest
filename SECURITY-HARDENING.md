# Security hardening: threat model and defences

Palimpsest is overwhelmingly an **outbound** measurement tool. It reads public, sometimes
adversarial surfaces (Chinese web feeds, state-aligned model APIs) and turns deletion into
data. The collection and analysis paths listen on nothing at all.

It does, however, ship and publicly advertise **one inbound service**: the MCP server
(`mcp/palimpsest_mcp.py`, advertised in `server.json` and reachable at
`https://api.seiche.info/palimpsest/mcp`), which re-serves the already-published readings to
LLM agents. That is a real listener and it belongs in the threat model, so §1(e) states it
explicitly rather than letting "no inbound surface" stand as a blanket claim. The security
questions are therefore:

- Can a hostile server weaponise *our own client* against us?
- If a collector box is compromised, how large is the blast radius?
- Can our secrets (two of them — §4) be turned into a bigger loss?
- Can a censor feed us fake data and poison the measurement?
- What can someone do to the one inbound listener we do expose?

This document states each risk plainly and points at the exact code that answers it. It sits
alongside [SAFETY.md](SAFETY.md) (source protection) and [docs/ETHICS.md](docs/ETHICS.md)
(do-no-harm rules and the OSINT-only line); it does not restate them, it covers the software
and operational security beneath them. Both safety lines hold throughout: **public reads
only**, and **no Beijing-aligned model is ever the analyst**.

---

## 1. Threat model

The adversary is the surface we read, whoever might reach a collector box, and whoever sends
requests to the published MCP endpoint. Most of the threats are about *what a response can do
to us* and *what a compromise can reach*; §1(e) covers the one direction that runs the other
way.

**(a) A hostile server weaponising our client.** A server we GET from can answer however it
likes. The concrete abuses:

- **SSRF via redirect.** A `302 Location: http://169.254.169.254/…` (cloud metadata),
  `http://127.0.0.1/…`, or an RFC-1918 address tries to make our client attack our own
  network or read cloud credentials. DNS rebinding is the same attack with a name that
  resolves public on the check and internal on the connect.
- **Decompression bomb.** A few KB of gzip that expands to gigabytes, to OOM the box.
- **Oversized or endless body.** A response with no natural end, to exhaust memory.
- **TLS downgrade / interception.** A stripped or forged certificate on a monitored path.
- **Odd schemes.** A redirect to `file://`, `ftp://`, or `gopher://` to reach the local
  filesystem or another service.

**(b) Blast radius of a compromised collector.** A collector box may hold a scoped API key and
operator-approved network configuration (§4). If it is compromised, the exposure should stop at
that box: it must not
reach the operator's other projects, vaults, keys, or identity, and its egress must not be
traceable to a person inside the censoring jurisdiction.

**(c) Secret exposure.** The scheduled repository workflows use one secret: an OpenRouter API
key for model readings. A leak can spend against it, so the key must be dedicated and capped.
The generic fetch libraries accept operator-supplied proxy configuration for approved surfaces,
but no repository workflow carries `PALIMPSEST_PROXY`, and it cannot activate Baike collection.

**(d) Data poisoning.** A censor who notices they are being measured can feed fake deletions
or fake "still live" answers to skew the index. This is a *measurement-integrity* threat, not
a code-execution one, and it is handled by the detection design (control-post probing,
multi-observation confirmation, fail-soft abstention) rather than by the fetch layer.

**(e) The one inbound surface: the MCP server.** `mcp/palimpsest_mcp.py` is a stdlib
JSON-RPC 2.0 listener that exposes the published signals as agent tools. Threat-modelled
honestly, it is a small surface but not a zero one:

- **What it can do.** Four read-only tools. Every one of them GETs a fixed URL from a
  hard-coded allow-list (the `SIGNALS` dict maps a caller-supplied *name* to a path we chose;
  a caller can never supply a URL) and returns the JSON already served publicly at
  `palimpsest.info`. It writes nothing, holds no secret, touches no database, and has no
  authentication because there is nothing behind it that is not already public.
- **What it cannot do.** No tool reaches a collector, the ledger, the proxy seam, or any key.
  The process is stateless apart from a ten-minute in-memory cache; killing it loses nothing.
  It binds `127.0.0.1` and is reached only through a reverse proxy with a path allow-list, so
  the internet never speaks to the Python process directly.
- **What is left.** Resource exhaustion (an unauthenticated caller can make us fetch and
  parse), which is the reverse proxy's job to rate-limit; and prompt injection, which is a
  real exposure here and not a bounded one. It used to be written down as bounded, on the
  grounds that we hand an agent our own published measurement JSON rather than arbitrary
  fetched web text. That reasoning does not survive contact with what the JSON contains. A
  Generative Firewall Index `excerpt` is verbatim output of a model under study; a GDELT or
  Weibo `headline` is scraped text. Publishing it ourselves changes who serves it, not who
  wrote it, and the readers are increasingly agents, so a model under study can reach the
  caller's agent through us.

  What actually bounds it: fields carrying text we did not author are neutralized before
  they leave the server and named to the caller in `untrusted_fields`, with a note saying
  they are data to analyze rather than instructions to follow. Neutralizing means removing
  the invisible and bidi channels used to hide instructions from a human reviewer, including
  the Unicode Tags block, which encodes plain ASCII that renders as nothing. It does **not**
  mean editing what was said: visible characters survive intact, and the zero-width joiners
  are deliberately kept because they are meaning-bearing in Persian, in Indic scripts and in
  emoji sequences. An excerpt is the research artifact, so the honest position is that the
  text is passed through, flagged, and stripped only of the channels a reader cannot see.
  Any subtree too deeply nested to walk is declared in `neutralization_gap` rather than
  passed off as clean. A visible-text instruction in a model's own output is therefore still
  delivered verbatim, by design, and defending against it is the calling agent's job.

The read-only, already-public nature of the payload is what keeps this surface honest: an
attacker who fully compromised the MCP process would learn nothing that is not on the website,
and could only lie to agents about readings they can verify against `palimpsest.info` and the
hash chain. Reader-facing documentation of the server itself is in
[docs/MCP-SERVER.md](docs/MCP-SERVER.md).

What is explicitly **not** in the model: "hacking back" (out of scope by design — see §6).

---

## 2. Client self-defence — `core/safe_fetch.py`

**State this accurately, because the gap matters.** `core/safe_fetch.py` is a written, tested,
standard-library-only hardened fetch. Its first production caller is the optional
`scripts/import_nemesis_snapshot.py` publication bridge, which permits one operator-configured
HTTPS endpoint and disables redirects. **No live collector calls it yet.** It protects that
narrow import boundary today and remains the intended egress chokepoint for the collectors.

What the live reads actually do today: a plain `urllib.request.urlopen`, or an
`httpx.AsyncClient` on the async paths. Most carry a timeout and about half carry an explicit
byte cap; TLS is verified because that is urllib's and httpx's default. None of them get the
SSRF/private-address guard, the per-redirect re-validation, the IP pinning against DNS
rebinding, the decompression-bomb cap, or the scheme allow-list.

Rather than restate that inventory in prose where it would rot, it is **kept as a test**:
[`tests/test_egress_policy.py`](tests/test_egress_policy.py) scans every first-party directory
for direct-egress call sites and fails unless each one is listed with a one-line justification.
That list is the authoritative, current inventory of un-hardened egress — each entry a named
piece of attack surface, with the honest reason it has not moved (async-only path, a POST body
this GET-only fetch cannot carry, a deliberately pinned-IP CDN probe that is structurally
outside the design, a loopback Ollama backend the SSRF guard would correctly refuse). Shrinking
that list *is* the migration; growing it silently is not possible, because a new un-hardened
call site fails the suite.

Two things keep the collector gap survivable in the meantime. Every live collector read is an anonymous request to
a **hard-coded, first-party-chosen URL** — no collector fetches a URL supplied by the surface it
is reading, which is what makes the missing SSRF guard a latent risk rather than an open door.
And nothing fetched is ever executed (§5). Until the list empties, read the rest of this section
as a description of a capability that exists and is tested, not of a control that is deployed.

What `safe_fetch()` provides once a caller does use it is below. It is standard-library only, so
the whole defence is auditable in one short file. Any refusal raises a `FetchError` (or a
subclass), which the caller treats as an **abstention**, never a false zero (see §5).

The exception hierarchy is the contract:

| Exception | Raised when |
| --- | --- |
| `FetchError` | Base class. Any refusal by the hardened fetch (bad scheme, DNS failure, HTTP ≥ 400, proxy failure). |
| `BlockedAddressError` | SSRF guard tripped: the host resolved to a non-public address. |
| `ResponseTooLarge` | Body, or its decompressed form, exceeded the byte cap (size / bomb guard). |
| `TooManyRedirects` | The redirect chain exceeded the cap (default 5). |

The protections, each tied to a threat in §1:

- **SSRF guard, on every hop.** `_validate_public()` resolves the host with
  `getaddrinfo` and refuses if *any* resolved address is private, loopback, link-local
  (which covers the `169.254.169.254` metadata endpoint), reserved, multicast, or
  unspecified. It runs before the first connection **and again on every redirect hop**, so a
  `302` to an internal address is caught, not followed.
- **IP pinning closes DNS rebinding.** `_validate_public()` returns the exact validated
  IP(s); `_connect()` connects to that *pinned* IP while still presenting the original
  hostname for SNI and certificate verification. A name that rebinds to an internal address
  between the check and the connect cannot swap the target, and TLS still verifies the real
  name.
- **Decompression-bomb cap.** `_maybe_decompress()` inflates gzip/deflate through a hard
  output cap (`max_bytes + 1`). If the output exceeds the cap **or** any input is left
  unconsumed once the cap is hit, it raises `ResponseTooLarge`. A kilobyte that wants to
  become a gigabyte is stopped at the cap.
- **Byte cap.** `_read_capped()` reads at most `max_bytes + 1` (default 8 MiB) and rejects an
  over-cap body, so an endless response cannot exhaust memory.
- **TLS verification on by default.** `ssl.create_default_context()` verifies both the
  certificate chain and the hostname; there is no "insecure" toggle. A downgrade or forged
  cert fails the connection.
- **Scheme allowlist.** Only `http` and `https` are permitted (`_ALLOWED_SCHEMES`). A
  redirect to `file://`, `ftp://`, or `gopher://` raises `FetchError`.
- **It never executes what it fetches.** `safe_fetch()` returns decoded text for a parser to
  treat as untrusted data. No fetched byte is ever passed to an interpreter, deserialiser, or
  shell.

The generic proxy path (`_fetch_via_proxy`, used when an approved caller supplies a proxy) is
kept minimal and clearly delimited: host resolution happens *at the trusted proxy*, so
client-side IP pinning does not apply there, but the scheme allowlist, byte cap, redirect cap,
and timeout still hold. This transport capability is not source authorization. Baike is denied
before either generic UNDERTEXT client path and its collector has no live client.

These defences are pinned by offline tests in
[`tests/test_safe_fetch.py`](tests/test_safe_fetch.py): loopback and metadata IPs are refused,
non-http schemes are rejected, an oversized body raises `ResponseTooLarge`, and a real gzip
bomb (≈1 MB from under 2 KB on the wire) is rejected by the decompression cap. Those tests
exercise the module directly — they prove the guard works, not that anything is behind it.

**No-dangerous-sinks guard.** [`tests/test_no_dangerous_sinks.py`](tests/test_no_dangerous_sinks.py)
scans every collection/processing path (`collectors`, `processors`, `core`, `censorwatch`,
`api`, `storage`, `scripts`) and **fails the test suite** if a code-execution sink is ever
introduced: `eval`/`exec`, `pickle.load(s)`, `marshal.load(s)`, `subprocess.*`,
`os.system`/`os.popen`, `__import__`, `yaml.load`, or `shell=True`. This turns "we never
execute fetched bytes" from a promise into a test. (`compile`/`re.compile` are excluded; a
mention in a comment is documentation, not a sink.)

"Fails the test suite" is the accurate phrasing, and the suite is now a gate rather than
advice: [`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs
`pytest tests/ censorwatch/tests/` on every push and every pull request. Before that workflow
existed, every guard test here — this one and the egress-policy inventory above — could only
fail on a contributor's laptop, which is documentation rather than a guard. Locally the same
gate is one command:

```bash
PYTHONPATH=. python3 -m pytest tests/ -q
```

(see [CONTRIBUTING.md](CONTRIBUTING.md); the two suites and their two different counts are
described there). Note what CI does *not* do: it holds no secrets and makes no network reads,
because none of the guard tests need any. The data-refresh workflows, which do hold secrets,
run on schedule and never on `pull_request`.

---

## 3. Isolation architecture

Two independent goals: keep egress clean, and keep a compromise contained.

**Egress and sandboxing.** A live collector should run in a disposable, least-privilege
container: **no inbound ports** (a collector has no service to expose — the MCP server of
§1(e) is a separate process and must stay separate), outbound-only, and nothing
mounted from the host beyond what the run needs. When it is done, throw it away. Operational
scaffolding lives under [`ops/`](ops/): the launchd scheduling for the recurring reading, and a
hardened non-root, read-only, capability-dropped container at [`ops/docker/`](ops/docker/) — the
recommended packaging for any always-on collector box. The analytical core is standard-library
only, which keeps that image small and its supply chain short.

**Keep network authorization explicit.** Approved public collectors use only their documented
source paths. This repository configures no in-country route and never relies on an identifiable
person's connection. Baike acquisition is disabled in its runner and denied in both generic
UNDERTEXT client paths. A proxy argument is transport configuration, not permission to add a
source.

**Blast-radius containment.** The collector box should hold only what it needs: the code, the
scoped secrets (§4), and its own working files. It must be separated from the operator's
other projects, private vaults, and unrelated credentials, so that compromising the box yields
a censorship collector and a capped API key, nothing more. The most sensitive collection path
(CensorWatch deletion detection) is additionally feature-flagged and writes to its own
database tables, inert unless `CENSORWATCH_ENABLED` is set (see SAFETY.md).

---

## 4. Secret scoping and rotation

There is **one scheduled secret**, stored in two places. Get the inventory right before
touching it, because rotating only the copy you remember silently breaks live publishing.

| Secret | What it is | Where it is stored | What reads it |
| --- | --- | --- | --- |
| `OPENROUTER_API_KEY` | OpenRouter API key for the live model readings | **(1)** local git-ignored env file `~/.config/palimpsest/gfi.env`, mode `0600` · **(2)** a **GitHub Actions repository secret** on `beepboop2025/palimpsest` | locally: `scripts/run_gfi.sh` → `scripts/generative_firewall_reading.py` · in Actions: `gfi-refresh.yml`, `erasure-refresh.yml`, `gfi-validation-sample.yml` |

Neither value is ever committed. The workflows that carry `OPENROUTER_API_KEY` run only on
`schedule` and `workflow_dispatch`, never on `pull_request`, so a fork's code can never read
it. The stdlib analytical core needs no key at all — only the live ops runners do, and they
**fail loud** if the key is unset rather than emitting a false reading;
`ops/install_schedule.sh` refuses to install the scheduled agent if the local env file is
missing.

**Scope it so a leak is capped.** Use a **dedicated, low-spend, rotatable** key for this box:

- a hard spending limit / credit cap on the key, so a compromise is a bounded bill;
- no privileges beyond model inference (no billing, no org admin, no other services);
- one key per collector box, so revoking one never touches another.

A compromised box then means a capped charge, not an account takeover or a pivot.

**Rotating `OPENROUTER_API_KEY` — both copies, in this order.** Skipping step 3 leaves the
daily published readings running on a key you revoked in step 4, and the next `gfi-refresh`
run fails silently as an abstention rather than an alert.

1. Create a new key in the OpenRouter dashboard with the same low spend cap.
2. Update the **local** copy in place, preserving permissions:
   `printf 'export OPENROUTER_API_KEY=%s\n' "$NEW" > ~/.config/palimpsest/gfi.env && chmod 600 ~/.config/palimpsest/gfi.env`
3. Update the **GitHub Actions repository secret** — the copy that publishes:
   `gh secret set OPENROUTER_API_KEY --repo beepboop2025/palimpsest --body "$NEW"`
   (or Settings → Secrets and variables → Actions → `OPENROUTER_API_KEY` → Update).
   This one secret feeds `gfi-refresh.yml`, `erasure-refresh.yml` and
   `gfi-validation-sample.yml`; updating it covers all three.
4. Revoke the old key in the dashboard.
5. Confirm **both** paths, not just the local one:
   - local: `zsh scripts/run_gfi.sh`, then check `readings/state/gfi.log` for a clean run;
   - Actions: `gh workflow run gfi-refresh.yml --repo beepboop2025/palimpsest` and check that
     the run produces a real reading rather than an abstention.

Rotate on any suspected exposure, on operator change, and on a routine schedule.

**Kill switch.** Independent of the key, the governance layer (`core/governance.py`) provides
a fail-safe halt: creating the kill file (default `./.palimpsest_halt`) or setting
`PALIMPSEST_HALT=1` stops all governed collection instantly, with no redeploy. It is
fail-safe by design — any error reading the gate is treated as "halted", so an outage stops
collection rather than letting it run unchecked. Use it the moment anything looks wrong, before
you even reach for key rotation.

---

## 5. Input safety and fail-soft

Fetched content is only ever treated as **data**. It is parsed, fingerprinted, and classified
by lexical, rule-based code; it is never executed, deserialised into live objects, or handed
to a shell. §2's no-dangerous-sinks test keeps that true as the code grows.

The behaviour under a blocked, hostile, degraded, or oversized response is **fail-soft**: the
observation becomes an **abstention**, never a false zero.

- `safe_fetch()` raises a `FetchError` subclass on any refusal, and callers abstain rather
  than record "nothing there".
- The generative-firewall path distinguishes "the model refused" (a real censorship signal)
  from "we could not reach the model" (an abstention): an unreachable backend returns `None`
  and is marked `abstain`, then excluded from forks and baselining, so a transport failure is
  never counted as a deletion.
- Upstream, the deletion detector probes a known-live control post each cycle and marks the
  whole cycle `DEGRADED` — suppressing every deletion write — when the network looks
  unreliable, and confirms a deletion only after multiple independent observations agree.

This is also the answer to data poisoning (§1d): a fake or flaky response cannot manufacture a
finding, because the system's default under uncertainty is to abstain and say so, not to
assert a zero. Velocity that cannot be honestly measured is shown suppressed, never faked.

---

## 6. Scope line

Palimpsest deliberately holds the **analytical-OSINT line**. It collects and analyses
already-public information to measure the censor's behaviour, and it contains **no** deception,
honeypots, decoys, tarpits, active measures, deanonymisation, or offensive capability of any
kind. That is not a gap to be filled later; it is a boundary, and contributions that cross it
are declined (see [docs/ETHICS.md](docs/ETHICS.md) and [SECURITY.md](SECURITY.md)).

Deception and defensive-deception techniques belong to a **separate, defence-oriented
project**, kept out of this repository on purpose. Mixing them in would compromise the
public-good measurement posture that makes this tool safe to run near people who can be harmed:
an observatory that also deceives is no longer purely an observatory. Keeping the line clean —
*observe the censor, never act against a target; measure suppression, never surveil a person* —
is itself a security property.

## Reporting

Security and source-safety concerns go through private reporting, not public issues. See
[SECURITY.md](SECURITY.md). Source safety overrides every other consideration, including
completeness of measurement.
