# Hardened sandbox for the recurring reading

This directory runs Palimpsest's **outbound** collector — the recurring
Generative Firewall Index reading — inside a disposable, isolated container.

The reading reaches out to a public model API, grades the responses with the
repo's own lexical rule-set, and writes an auditable point into `readings/`.
That is the whole job. It is an **outbound scraper in a box**: it publishes no
ports, serves nothing, and answers to nothing. This is egress + sandbox
isolation, **not** a honeypot and **not** an inbound service.

The design goal is blast-radius containment. If the collector is ever
compromised by something hostile it fetches, the damage is confined to a
throwaway, non-root, read-only container that cannot reach the rest of the
machine or its accounts. It watches the censor, never the censored — and it
does so from behind a locked door.

LINE-HELD: the analytical cores are standard-library only, so the recurring
reading installs **no** Python packages. Nothing here weakens the existing
`launchd`/`run_gfi.sh` path — this is an alternative, sandboxed way to take the
same reading.

---

## Build and run

Prerequisite: your OpenRouter key lives in the same git-ignored env file the
scheduled runner already uses:

```
~/.config/palimpsest/gfi.env      # contains: export OPENROUTER_API_KEY=sk-or-...
                                   # mode 0600, never committed, never in the image
```

From this directory:

```bash
# Build the hardened image (no build tools, no pip installs, non-root).
docker compose build

# Take one reading. Runs to completion, writes into ../../readings/, then exits.
docker compose up

# Or in one step, detaching the log stream:
docker compose up --build

# Tear the container down (the readings/ output persists on the host).
docker compose down
```

The container runs once and exits by design (`restart: "no"`). Recurrence is the
host scheduler's job — keep using `ops/install_schedule.sh` (launchd) for the
weekly cadence, or wrap `docker compose up` in your own timer if you want the
reading to run sandboxed on a schedule.

Output lands in the repo's `readings/`:

- `history.jsonl` — the committed time series (one point per run)
- `latest.json` — the full raw dataset from the most recent run
- `generative-firewall-index.html` — the regenerated dashboard

Run logs (`readings/state/`) are written to an in-RAM tmpfs and vanish with the
container — they are ephemeral by design and are git-ignored anyway.

---

## What each hardening flag defends against

Every flag maps to a concrete threat. The model is defense-in-depth: assume the
collector process is compromised, then make that compromise worth as little as
possible.

| Flag | Threat it contains |
| --- | --- |
| `user: "10001:10001"` + non-root image | **Least privilege.** The process is never root, so a breakout starts with an unprivileged account, not the run-of-the-container keys to the kingdom. |
| `cap_drop: [ALL]` | **Privilege-escalation containment.** Every Linux capability is dropped — no `NET_ADMIN`, no `SYS_ADMIN`, no `CHOWN`. A scraper needs none of them. |
| `security_opt: [no-new-privileges:true]` | **Privilege-escalation containment.** Neutralizes setuid/setgid tricks — a child can never end up with more privilege than the parent started with. |
| `read_only: true` | **Tamper / persistence containment.** The root filesystem is immutable. An attacker cannot drop a payload, overwrite the collector, or leave anything behind that survives the next run. |
| `tmpfs: /tmp, /app/readings/state` | **Ephemerality.** The only scratch space lives in RAM and is destroyed on exit, with its own size caps. Nothing scratched persists. |
| `mem_limit` / `memswap_limit` / `cpus` | **Resource-exhaustion containment.** A hostile response cannot balloon memory or peg every core on the host. |
| `pids_limit: 256` | **Fork-bomb containment.** A cap on processes/threads stops a runaway or malicious fan-out from taking the machine down. |
| single `readings/` bind, everything else RO | **Data-exfiltration / lateral-movement containment.** The container's one writable window onto the host is the output directory it must write. It cannot see your home, your keys, or the rest of the repo. |
| no `ports:` (outbound only) | **No inbound attack surface.** Nothing is published and nothing listens — this is a client, not a server. |
| `restart: "no"` | **Fail-loud, no silent loops.** A crashed or hijacked run stops instead of restarting into a tight loop. |

---

## How the API key is supplied safely

The key is **never** in the image and **never** in git. It is injected at run
time only, from one of two host-side sources:

1. **`env_file`** — `${HOME}/.config/palimpsest/gfi.env` (the same 0600 file the
   launchd runner sources). Compose reads it at `up` time and passes
   `OPENROUTER_API_KEY` into the container's environment. It is marked
   `required: false`, so the stack still starts if you prefer option 2.
2. **Host environment passthrough** — the `environment:` block lists bare
   variable names (`OPENROUTER_API_KEY`, etc.) with no values. Compose forwards
   whatever is set in your shell and nothing more, so `export OPENROUTER_API_KEY=...`
   in the invoking shell works without a file.

If the key is absent, the reading **fails loud**: it prints a fatal notice and
exits without appending a false point. That behavior is unchanged from the
scheduled runner.

---

## Composing with an egress proxy

Palimpsest already has an app-level egress seam, `PALIMPSEST_PROXY`, used by the
in-country collectors (`baike_redaction`, `undertext`) to route reads through
outside-the-wall infrastructure. This sandbox composes with it cleanly, on two
independent layers:

- **App layer (`PALIMPSEST_PROXY`)** — passed straight through to the container.
  Point it at your egress path and the in-country collectors honor it exactly as
  they do on the host. The recurring GFI reading itself talks to OpenRouter,
  which is reachable *outside* the wall, so it does not need this seam — but the
  variable is forwarded for any collector you add that does.
- **Transport layer (`HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY`)** — the GFI
  reading calls OpenRouter with stdlib `urllib`, which honors the standard proxy
  environment variables automatically. Set `HTTPS_PROXY` in your `gfi.env` (or
  shell) and the OpenRouter call routes through your egress proxy with no code
  change.

For stronger network containment you can also run the container on a Docker
network whose only route out is an egress-proxy sidecar, so the collector
physically cannot reach anything except the proxy. That is an infrastructure
choice layered on top of the isolation here; this compose file keeps the default
outbound network so it runs anywhere, and leaves the proxy pinning to you.

---

## What this is not

- Not a honeypot. It attracts nothing and traps nothing.
- Not an inbound service. It opens no ports and accepts no connections.
- Not a replacement for the safety posture in `SAFETY.md` / `SECURITY.md` — it
  reinforces it. Public, permitted reads only; no jailbreak; the repo's lexical
  rule-set remains the analyst.
