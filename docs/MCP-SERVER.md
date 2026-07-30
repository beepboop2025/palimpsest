# The Palimpsest MCP server — the board, as agent tools

Everything Palimpsest publishes is static JSON on [palimpsest.info](https://palimpsest.info).
That is fine for a human with a browser and useless to a language model that has to decide,
mid-answer, whether it actually knows what is being blocked today. The MCP server closes that
gap: it exposes the same published readings over the Model Context Protocol, so any agent can
call them instead of guessing from stale training data.

It changes nothing about the record. It is a **read-only re-serving layer** over files that are
already public — no private data, no extra measurement, no privileged view.

## Connect

| | |
| --- | --- |
| Endpoint | `https://api.seiche.info/palimpsest/mcp` |
| Transport | streamable HTTP, stateless JSON-RPC 2.0 |
| Auth | none — everything it serves is already public |
| Source | [`mcp/palimpsest_mcp.py`](../mcp/palimpsest_mcp.py) (stdlib only, one file) |
| Manifest | [`server.json`](../server.json) |

Any MCP client works the same way: register the endpoint as a **streamable-HTTP** (remote)
server and it will discover the tools itself. Most clients want a config entry shaped like
this:

```json
{
  "mcpServers": {
    "palimpsest": {
      "type": "http",
      "url": "https://api.seiche.info/palimpsest/mcp"
    }
  }
}
```

To check it by hand, without any client at all:

```bash
curl -s -X POST https://api.seiche.info/palimpsest/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -m json.tool
```

## The four tools

| Tool | What it answers |
| --- | --- |
| `list_signals` | What is measured at all — every signal's name, one-line description, and source URL. Call this first. |
| `get_signal(name)` | One signal's full latest reading, exactly as served on the site, with its `generated_at` and upstream sources. This is also the door to the model-evaluation side: `eval-registry` and `refusal-drift`. |
| `whats_happening` | The censorship board's own cross-signal verdict: is anything actually happening right now, with multiplicity paid for and coverage confounds flagged as artifacts rather than findings. |
| `gfw_reading` | The Great Firewall at both layers in one call — network blocking measured inside China (OONI) beside model-layer censorship (the Generative Firewall Index). |

`whats_happening` exists because the reconciliation *is* the observatory's work. An agent that
pulls every signal and reasons over them itself will reproduce exactly the two errors this
board was built to avoid: reading a per-signal false-alarm rate as if it were a board-level
one, and reading a shrinking measurement base as easing censorship. `list_signals` is the
authoritative roster — it is generated from the server's own table, so it never goes stale
against this page.

## The rules it keeps

- **Fail-loud, never stale-silent.** Each signal is cached for ten minutes. A signal that
  cannot be fetched is returned as explicitly `unavailable` with the reason attached. Nothing
  is served past its window, and nothing is invented to fill a hole.
- **Cite what you are handed.** Every payload carries its own `generated_at` and its upstream
  sources. Agents are instructed to cite both; a reading without its timestamp is not a
  reading.
- **Read-only, closed world.** The tools are annotated `readOnlyHint` and `openWorldHint:
  false`. There is no tool that writes, and no tool that will fetch a URL you supply — the
  caller passes a *signal name* from a fixed list, and the server maps it to a path we chose.
- **No privileged access.** The server holds no key, touches no database, and reads nothing
  the public cannot read. Compromising it would leak nothing that is not on the website; it
  could only lie about readings, and the readings are hash-chained and independently
  verifiable ([INTEGRITY.md](INTEGRITY.md)).

The security threat model for this endpoint — the one inbound surface the project exposes — is
in [SECURITY-HARDENING.md](../SECURITY-HARDENING.md) §1(e).

## Running your own

The server is one standard-library file with no dependencies. It binds loopback and expects a
reverse proxy in front of it:

```bash
PYTHONPATH=. python3 mcp/palimpsest_mcp.py      # serves on 127.0.0.1:8793
curl -s http://127.0.0.1:8793/ | python3 -m json.tool
```

It fetches from `palimpsest.info` like anyone else, so a self-hosted copy sees precisely what
the public copy sees. Running your own is the honest way to check that the hosted endpoint is
not editorialising: call both, diff the payloads.
