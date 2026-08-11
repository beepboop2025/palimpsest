# Private investigative analysis node

This service turns the Hetzner node's retained aggregate readings into a private
editorial-lead ledger every thirty minutes. It adds **no collection traffic**.
Each run takes a stable copy of the live reading files and RSS evidence ledger,
then executes the existing analytical stack inside the production image with
Docker networking set to `none`.

The fixed order is:

1. independent-vantage fusion;
2. conformal event flags, coverage guard, board alarm, cross-layer tests, and
   the forecast ledger;
3. the revision-aware economic pulse;
4. the OSINT roll-up and review-gated investigations builder;
5. a private, content-addressed editorial candidate ledger.

Silence Index is deliberately excluded because its current implementation calls
the GDELT DOC API. Public newsroom rendering, sealing, anchoring, and Git commits
also remain excluded: the node is an analysis environment, not a publisher.

## Storage contract

- Stable run snapshots:
  `/var/lib/palimpsest-analysis/runs/run-<UTC>-<input hash>/`
- Private latest leads:
  `/var/lib/palimpsest-analysis/private/ledger/candidates-latest.json`
- Private immutable lead versions:
  `/var/lib/palimpsest-analysis/private/ledger/candidate-versions.jsonl`
- Private run state:
  `/var/lib/palimpsest-analysis/private/state.json`
- Mode-0600 cascade lease:
  `/var/lib/palimpsest-analysis/private/cascade.lock`

Only 48 complete run snapshots are retained (about one day at the normal
twice-hourly cadence). One frozen input cohort may contain at most 256 files and
512 MiB, and the runner requires at least 10 GiB free before staging it. A run is
eligible for cleanup only
when its name matches the service's exact run-directory pattern and it is not the
current run. Failed staging directories are removed; live readings and RSS files
are mounted/read as immutable inputs and are never removed or replaced.
The non-blocking host lease also rejects an overlapping timer, operator, or recovery
invocation before it can snapshot or launch a second container.

The append-only candidate-version ledger has a separate 256 MiB hard ceiling.
Alert and review it at 192 MiB (75%) so an editor can archive it under a reviewed
retention policy. The service deliberately fails closed rather than truncating,
rotating, or silently discarding candidate history after the ceiling is reached.

Candidate records are questions, not findings. They contain aggregate artifact
hashes, selectors, limitations, falsification-oriented next steps, and a fixed
`private-review-only` publication policy. They cannot alter
`config/investigations.json` and cannot reach the public website.

## Install

Run after the exact repository commit has been checked out cleanly and
`ops/docker/prod-compose up -d --build` has rebuilt the production image. The
installer independently checks the clean Git revision and the image's OCI
revision label, installs a root-owned versioned runner bundle, switches the
`current` symlink, and only then atomically replaces
`/etc/palimpsest/deployed-commit`.
Before every run, systemd verifies the bundle's SHA-256 manifest and requires its
revision file to byte-match that deployed receipt.

Installation must run from the Git-backed stable release path (on the canonical
node, `/home/palimpsest/palimpsest`). A code tree copied by rsync/SCP without
`.git` metadata is intentionally unsupported and will fail provenance checks.

```sh
sudo install -d -o root -g root -m 0711 /var/lib/palimpsest-analysis
sudo install -d -o 10001 -g 10001 -m 0700 \
  /var/lib/palimpsest-analysis/runs \
  /var/lib/palimpsest-analysis/private
# UID 10001 is also the collector identity. Preserve its write/default-write
# access to readings; systemd presents that source read-only to this unit.
sudo setfacl -R -m u:10001:rwX /var/lib/palimpsest/readings
sudo find /var/lib/palimpsest/readings -type d \
  -exec setfacl -m d:u:10001:rwx {} +
# The separate RSS newswire tree is analysis input only for this identity.
sudo setfacl -R -m u:10001:rX /var/lib/palimpsest/newswire
sudo find /var/lib/palimpsest/newswire -type d \
  -exec setfacl -m d:u:10001:rX {} +

# First install: these units may not exist yet. The installer itself verifies
# that neither unit is active and refuses an unknown state.
sudo systemctl stop palimpsest-investigative-analysis.timer 2>/dev/null || true
sudo systemctl stop palimpsest-investigative-analysis.service 2>/dev/null || true
sudo bash ops/investigative-analysis/install-host-bundle.sh
sudo systemctl enable --now palimpsest-investigative-analysis.timer
sudo systemctl start palimpsest-investigative-analysis.service
```

The optional `/etc/palimpsest/investigative-analysis.env` supports only
`PALIMPSEST_ANALYSIS_IMAGE`; pass the same image reference as the installer's
single argument so it verifies that image before writing the receipt. Readings,
newswire, runs, private state, and commit-receipt paths are fixed in code and in
the unit's filesystem policy. Environment variables that appear to override
those roots are intentionally unsupported.

`SupplementaryGroups=docker` is operationally necessary for this host runner,
but membership in the Docker group is root-equivalent: a process able to command
the daemon can ask it to mount host paths. The unit's unprivileged UID and systemd
hardening are defense in depth, not a security boundary against a compromised
Docker daemon. Keep the bundle and unit files root-owned, and do not grant UID
10001 write access to `/usr/local/libexec/palimpsest-analysis`.

## Verify

```sh
systemctl status palimpsest-investigative-analysis.service --no-pager
systemctl list-timers palimpsest-investigative-analysis.timer --no-pager
journalctl -u palimpsest-investigative-analysis.service -n 80 --no-pager
sudo jq '{schema_version,generated_at,edition_id,n_candidates,coverage}' \
  /var/lib/palimpsest-analysis/private/ledger/candidates-latest.json
sudo jq '{network_policy,publication_policy,steps,candidate_edition_id}' \
  "$(sudo jq -r .run_path /var/lib/palimpsest-analysis/private/state.json)"/readings/analysis-run-manifest.json
```

The manifest must say `docker-network-none` and `private-review-only`. A second
manual start with no changed evidence must log `unchanged` and add no candidate
version or forecast-history row.

For every later deploy, stop the analysis timer, wait for the oneshot service to
be inactive, update to a clean commit, rebuild the image, run
`install-host-bundle.sh`, and re-enable the timer. If any verification or atomic
rename fails, leave the timer stopped: the previous receipt remains the last
certified deployment.
