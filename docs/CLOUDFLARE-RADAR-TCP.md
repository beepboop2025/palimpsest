# Cloudflare Radar TCP reset/timeout telemetry

This adapter ingests Cloudflare Radar's aggregated distribution of the stage at
which sampled TCP connections reset or time out. Cloudflare performs the
upstream passive observation; Palimpsest makes six bounded API requests and
does not probe networks in the selected countries.

## Scope

- Countries: CN, IR, MM, PK, RU and TR.
- Window: the latest seven days at one-hour resolution.
- Stages: post-SYN, post-ACK, post-PSH, later in flow and no match.
- Output: country-level percentages, upstream update time and bounded
  confidence categories. Annotation prose, packet material, IP addresses,
  hostnames and connection identifiers are not retained.

The exact endpoint, geography allowlist and request ceilings are committed in
[`config/cloudflare_radar_tcp.json`](../config/cloudflare_radar_tcp.json).
Automation needs a least-privilege `CLOUDFLARE_API_TOKEN`; without it the CLI
returns a neutral gated state before reading config, creating files or making a
request.

## Interpretation boundary

A reset or timeout is not a censorship verdict. Lost connectivity, an
application closing abruptly, scanners and attacks can create the same signal.
Palimpsest uses this feed only as passive cross-layer context and requires
independent endpoint, routing or human-reviewed evidence before elevating an
event.

Source data are attributed to Cloudflare Radar under CC BY-NC 4.0. See the
[official API](https://developers.cloudflare.com/api/resources/radar/subresources/tcp_resets_timeouts/methods/timeseries_groups/)
and [method glossary](https://developers.cloudflare.com/radar/glossary/#tcp-resets-and-timeouts).

## Run

```bash
CLOUDFLARE_API_TOKEN=… python3 -m scripts.cloudflare_radar_tcp_pull
```

There is deliberately no endpoint, country or backfill override on the CLI.

On the production node, do not put the bearer token in the shared Compose
`.env`; every application service would inherit it. Store only the token in a
root/operator-readable file, set `PALIMPSEST_CLOUDFLARE_TOKEN_FILE` to that
path, and set `PALIMPSEST_CLOUDFLARE_RADAR_ENABLED=1`. Compose mounts the file
as a secret only in `worker-collectors`; migrate, beat, API, warehouse, default
and velocity containers never receive the credential. The committed default
secret is empty, so enabling no token produces a neutral gated state.
