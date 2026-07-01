# Security hardening: threat model and defences

Palimpsest is an **outbound** measurement tool. It reads public, sometimes adversarial
surfaces (Chinese web feeds, state-aligned model APIs) and turns deletion into data. It
exposes **no inbound service**: there is no listener, no login, no port for anyone to attack.
So the security question is not "can someone hack back in" — nothing is listening. The real
questions are narrower and answerable in code:

- Can a hostile server weaponise *our own client* against us?
- If a collector box is compromised, how large is the blast radius?
- Can our one secret (an API key) be turned into a bigger loss?
- Can a censor feed us fake data and poison the measurement?

This document states each risk plainly and points at the exact code that answers it. It sits
alongside [SAFETY.md](SAFETY.md) (source protection) and [docs/ETHICS.md](docs/ETHICS.md)
(do-no-harm rules and the OSINT-only line); it does not restate them, it covers the software
and operational security beneath them. Both safety lines hold throughout: **public reads
only**, and **no Beijing-aligned model is ever the analyst**.

---

## 1. Threat model

The adversary is the surface we read, plus whoever might reach a collector box. There is no
inbound attack surface to defend, so the threats are all about *what a response can do to us*
and *what a compromise can reach*.

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

**(b) Blast radius of a compromised collector.** A collector box sits at an egress seam and
holds one API key. If it is compromised, the exposure should stop at that box: it must not
reach the operator's other projects, vaults, keys, or identity, and its egress must not be
traceable to a person inside the censoring jurisdiction.

**(c) Secret exposure.** The one secret is an OpenRouter API key used by the live Generative
Firewall reading. A leaked key means an attacker can spend against it. The goal is that this
costs a capped bill, never an account takeover or a pivot into anything else.

**(d) Data poisoning.** A censor who notices they are being measured can feed fake deletions
or fake "still live" answers to skew the index. This is a *measurement-integrity* threat, not
a code-execution one, and it is handled by the detection design (control-post probing,
multi-observation confirmation, fail-soft abstention) rather than by the fetch layer.

What is explicitly **not** in the model: inbound intrusion (no service), and "hacking back"
(out of scope by design — see §6).

---

## 2. Client self-defence — `core/safe_fetch.py`

Every outbound read that touches an untrusted surface goes through `safe_fetch()`. It is
standard-library only, so the whole defence is auditable in one short file. Any refusal raises
a `FetchError` (or a subclass), which the caller treats as an **abstention**, never a false
zero (see §5).

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

The proxy path (`_fetch_via_proxy`, used when the `PALIMPSEST_PROXY` egress seam is set) is
kept minimal and clearly delimited: host resolution happens *at the trusted proxy*, so
client-side IP pinning does not apply there, but the scheme allowlist, byte cap, redirect cap,
and timeout still hold.

These defences are pinned by offline tests in
[`tests/test_safe_fetch.py`](tests/test_safe_fetch.py): loopback and metadata IPs are refused,
non-http schemes are rejected, an oversized body raises `ResponseTooLarge`, and a real gzip
bomb (≈1 MB from under 2 KB on the wire) is rejected by the decompression cap.

**No-dangerous-sinks guard (CI).** [`tests/test_no_dangerous_sinks.py`](tests/test_no_dangerous_sinks.py)
scans every collection/processing path (`collectors`, `processors`, `core`, `censorwatch`,
`api`, `storage`, `scripts`) and **fails the build** if a code-execution sink is ever
introduced: `eval`/`exec`, `pickle.load(s)`, `marshal.load(s)`, `subprocess.*`,
`os.system`/`os.popen`, `__import__`, `yaml.load`, or `shell=True`. This turns "we never
execute fetched bytes" from a promise into a test. (`compile`/`re.compile` are excluded; a
mention in a comment is documentation, not a sink.)

---

## 3. Isolation architecture

Two independent goals: keep egress clean, and keep a compromise contained.

**Egress and sandboxing.** A live collector should run in a disposable, least-privilege
container: **no inbound ports** (there is no service to expose), outbound-only, and nothing
mounted from the host beyond what the run needs. When it is done, throw it away. Operational
scaffolding lives under [`ops/`](ops/): the launchd scheduling for the recurring reading, and a
hardened non-root, read-only, capability-dropped container at [`ops/docker/`](ops/docker/) — the
recommended packaging for any always-on collector box. The analytical core is standard-library
only, which keeps that image small and its supply chain short.

**Route egress through the seam, never through a person.** Live probing goes through the
optional `PALIMPSEST_PROXY` egress seam — deliberately an outside-the-wall path. The
in-country vantage backends are infrastructure, **never a residential exit tied to an
identifiable person**; that line is stated in [docs/ETHICS.md](docs/ETHICS.md) and
[SAFETY.md](SAFETY.md) and is a hard rule, not a preference. The seam is also the single place
where egress can be swapped, rate-limited, or cut.

**Blast-radius containment.** The collector box should hold only what it needs: the code, one
scoped key (§4), and its own working files. It must be separated from the operator's other
projects, private vaults, and unrelated credentials, so that compromising the box yields a
censorship collector and a capped API key — nothing more. The most sensitive collection path
(CensorWatch deletion detection) is additionally feature-flagged and writes to its own
database tables, inert unless `CENSORWATCH_ENABLED` is set (see SAFETY.md).

---

## 4. Secret scoping and rotation

There is exactly one secret: the OpenRouter API key for the live Generative Firewall reading.

**Where it lives.** Only in a local, git-ignored env file: `~/.config/palimpsest/gfi.env`,
mode `0600`, exporting `OPENROUTER_API_KEY`. It is **never** committed. `scripts/run_gfi.sh`
sources it at runtime and nothing else reads it from disk; `ops/install_schedule.sh` refuses
to install the scheduled agent if the file is missing. The stdlib analytical core never needs
a key at all — only the live ops runner
(`scripts/generative_firewall_reading.py`) does, and it **fails loud** if the key is unset
rather than emitting a false reading.

**Scope it so a leak is capped.** Use a **dedicated, low-spend, rotatable** key for this box:

- a hard spending limit / credit cap on the key, so a compromise is a bounded bill;
- no privileges beyond model inference (no billing, no org admin, no other services);
- one key per collector box, so revoking one never touches another.

A compromised box then means a capped charge, not an account takeover or a pivot.

**Rotation steps.**

1. Create a new key in the OpenRouter dashboard with the same low spend cap.
2. Update the secret in place, preserving permissions:
   `printf 'export OPENROUTER_API_KEY=%s\n' "$NEW" > ~/.config/palimpsest/gfi.env && chmod 600 ~/.config/palimpsest/gfi.env`
3. Revoke the old key in the dashboard.
4. Trigger one reading to confirm (`zsh scripts/run_gfi.sh`) and check
   `readings/state/gfi.log` for a clean run.

Rotate on any suspected exposure, on operator change, and on a routine schedule. Rotation is
cheap because only one file and one dashboard entry are involved.

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
