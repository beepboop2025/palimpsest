# Public research-corpus ingest

This collector maintains a small source ledger for five public, Git-backed research
corpora:

- `github/gov-takedowns`
- `github/dmca`
- `citizenlab/test-lists`
- `citizenlab/chat-censorship`
- `gfwlist/gfwlist`

It is deliberately not a mirror. Notice bodies, affected account identifiers, individual
Git ref names, URL test targets, censorship keywords, and routing rules never cross the
publication boundary. One run fetches only each repository's bounded Git smart-HTTP ref
advertisement and publishes the exact `master` commit cursor, a response hash, aggregate ref
counts, and aggregate changes from the preceding cursor.

## Run one snapshot

From the repository root:

```bash
python3 -m scripts.research_corpus_ingest --readings readings
```

The direct form, `python3 scripts/research_corpus_ingest.py --readings readings`, is also
supported from any working directory.

The command reads `config/research_corpus_sources.json` and writes:

- `readings/research-corpus-latest.json`
- `readings/research-corpus-history.jsonl`

Latest is atomically replaced. History is a logical append-only ledger: every atomic update
preserves the existing bytes as an exact prefix and adds one canonical JSON line. It is never
trimmed silently; reaching the configured history ceiling fails before altering the file. A
bounded, aggregate-only transaction journal makes the two-file publication recoverable: if a
process stops after extending history but before replacing latest, the next locked invocation
finishes that exact publication without duplicating its history row.

The standard collector-fleet profile runs every 12 hours. The vigorous profile runs every
6 hours. The public publication lane in
`.github/workflows/research-corpus-refresh.yml` also runs every 6 hours, rebuilds the Evidence
Atlas catalog, seals and scrubs the exact public tree, and recollects against the winning
`main` revision before either bounded push attempt. The fleet remains an independent durable
collection lane. Each round is exactly five unauthenticated GETs, with per-source response
ceilings, a whole-run byte ceiling, a request timeout, a packet-count ceiling, a ref-name
ceiling, no redirects, and a contact-bearing User-Agent.

## Egress

The only required destination is:

| Protocol | Host | Port | Purpose |
| --- | --- | ---: | --- |
| HTTPS | `github.com` | 443 | Git `upload-pack` ref advertisements |

The collector does not contact `api.github.com`, `codeload.github.com`,
`raw.githubusercontent.com`, repository submodules, or any URL supplied at runtime. GitHub
addresses should be resolved normally rather than pinned in a firewall because the service's
edge addresses can change. Application-level redirects are disabled.

## Failure and gating semantics

- The global Palimpsest kill switch returns `status: halted` before lock creation or egress,
  is rechecked before each source request, and is checked once more immediately before the
  publication transaction begins. A mid-round halt publishes no partial observation.
- A Git transport outage returns `status: skipped`, leaves the last good latest/history
  unchanged, and exits cleanly so the fleet records an abstention rather than a false zero.
- Configuration drift, an unapproved source, malformed Git metadata, a size/packet limit, a
  corrupt cursor, or a corrupt history is a failure. None is converted into an empty reading.
- A concurrent run fails on the non-blocking publication lock rather than duplicating work.

## Rights declarations

Rights remain repository-specific even though Palimpsest publishes only aggregate metadata.
The committed declaration is intentionally conservative:

| Repository | Declared status used by the collector |
| --- | --- |
| `github/gov-takedowns` | No repository licence stated; metadata only, no content redistribution |
| `github/dmca` | No repository licence stated; metadata only, no content redistribution |
| `citizenlab/test-lists` | CC BY-NC-SA 4.0; metadata only |
| `citizenlab/chat-censorship` | CC BY-NC-SA 4.0; metadata only |
| `gfwlist/gfwlist` | LGPL-2.1-only; metadata only |

Both the JSON config and an independent code-level allowlist must agree on repository,
branch, corpus class, source status, sensitivity class, publication mode, and rights status.
Changing one side alone prevents egress.
