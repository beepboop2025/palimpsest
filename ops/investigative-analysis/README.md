# Private investigative analysis node

This service turns the Hetzner node's retained aggregate readings into a private
editorial-lead ledger every thirty minutes. It adds **no collection traffic**.
Each run takes a stable copy of the live reading files and RSS evidence ledger,
then asks a root-owned, socket-activated broker to execute the existing
analytical stack inside the production image with Docker networking set to
`none`. The analysis identity never receives the Docker socket.

The fixed order is:

1. independent-vantage fusion;
2. conformal event flags, coverage guard, board alarm, cross-layer tests, and
   the forecast ledger;
3. the revision-aware economic pulse;
4. the OSINT roll-up and review-gated investigations builder;
5. a private, content-addressed editorial candidate ledger;
6. citation-bound evidence packets and deterministic private working templates.
7. deterministic claim audits for every accepted Wire event, including current
   collector context and competing causal scenarios rounded to five percentage
   points.

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
- Per-run evidence packets:
  `<run>/private/analytical-packets-latest.json`
- Per-run deterministic working drafts:
  `<run>/private/analytical-drafts-latest.json`
- Per-run complete Wire claim-audit edition:
  `<run>/private/wire-claim-audits-latest.json`
- Stable delivery-safe Wire projection:
  `/var/lib/palimpsest-analysis/delivery/wire-claim-audits-latest.json`
- Private run state:
  `/var/lib/palimpsest-analysis/private/state.json`
- Per-attempt health receipt:
  `/var/lib/palimpsest-analysis/private/analysis-status.json`
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

The separate Wire projection uses `automated-attributed-analysis`. It is the sole
analysis output outside the mode-0700 private tree: its directory is mode 0711
(traversable by an exact known path but not listable) and its validated file is
mode 0644 for the isolated Palimpsest bot. The directory remains writable only by
the analysis identity. That policy
does not relax the candidate ledger's review gate: it permits only the exact,
validated deterministic audit bytes to be read by the news bot. Each audit keeps
publication provenance, independent-source structure, method-compatible collector
context, and causal uncertainty separate. Its percentages sum to 100 and are
coarse competing-scenario weights conditional on the retained source account;
they are not calibrated probabilities of a hidden motive. Events that fail the
interest, source, or evidence gates remain monitor/abstention records and are not
eligible for automated briefs.

The packet is the only supported input to a later private ranking or research
assistant. It contains bounded aggregate observations, exact evidence IDs and hashes,
limitations, countercase prompts, and falsification-oriented next steps. The
bundled working draft is produced without a model and demonstrates the strict
JSON contract. Only `editorial_review` packets may contain deterministic
assertion-bearing copy; research-plan and coverage-blocked packets remain
abstentions. The v1 validator accepts only exact deterministic projections; it
does not claim to prove free-form language entailment. Any future prose model
must use a separate private submission contract plus human evidence review and
must not create a publication transition. The scheduled container remains
networkless and calls no model.

Before either a new run or the unchanged-input shortcut, the runner requires a
successful evidence-wire receipt that reports at least one fresh source, is no
more than 75 minutes old, and matches the frozen newswire bytes and publication
clock. A failed attempt writes only a sanitized exception class to the analysis
status receipt; the last complete run and candidate ledger remain intact. The
systemd unit retries after two minutes with the same three-starts-per-ten-minute
bound as the wire.

## Install

Run after the exact repository commit has been checked out cleanly and
`ops/docker/prod-compose up -d --build` has rebuilt the production image. The
installer independently checks the clean Git revision and the image's OCI
revision label, installs a root-owned versioned runner bundle, switches the
`current` symlink, and only then atomically replaces
`/etc/palimpsest/deployed-commit`.
Before every run, systemd verifies the bundle's SHA-256 manifest and requires its
revision file to byte-match that deployed receipt.

The installer also creates or validates a locked `palimpsest-analysis` host
identity at UID/GID 10001, with `/nonexistent` as its home and `nologin` as its
shell. This NSS record is required even though every path and container already
uses numeric ownership; without it, systemd fails the unit with `217/USER`.

Installation must run from the Git-backed stable release path (on the canonical
node, `/home/palimpsest/palimpsest`). A code tree copied by rsync/SCP without
`.git` metadata is intentionally unsupported and will fail provenance checks.

```sh
# Reserve UID/GID 10001 before granting it ownership or ACL access. This mode
# validates the clean checkout and all four name/ID slots, but installs no unit.
sudo bash ops/investigative-analysis/install-host-bundle.sh --ensure-identity

sudo install -d -o root -g root -m 0711 /var/lib/palimpsest-analysis
sudo install -d -o root -g palimpsest-analysis -m 0710 \
  /var/lib/palimpsest-analysis/runs
sudo install -d -o palimpsest-analysis -g palimpsest-analysis -m 0700 \
  /var/lib/palimpsest-analysis/private
sudo install -d -o palimpsest-analysis -g palimpsest-analysis -m 0711 \
  /var/lib/palimpsest-analysis/delivery
# UID 10001 is also the collector identity. Preserve its write/default-write
# access to readings; systemd presents that source read-only to this unit.
sudo setfacl -R -m u:palimpsest-analysis:rwX /var/lib/palimpsest/readings
sudo find /var/lib/palimpsest/readings -type d \
  -exec setfacl -m d:u:palimpsest-analysis:rwx {} +
# The separate RSS newswire tree is analysis input only for this identity.
sudo setfacl -R -m u:palimpsest-analysis:rX /var/lib/palimpsest/newswire
sudo find /var/lib/palimpsest/newswire -type d \
  -exec setfacl -m d:u:palimpsest-analysis:rX {} +
sudo install -o palimpsest -g palimpsest -m 0600 /dev/null \
  /var/lib/palimpsest/newswire/newswire.lock

# First install: these units may not exist yet. The installer itself verifies
# that the timer, analysis service, broker socket, and broker instances are
# inactive. It revalidates the identity before installing the bundle and units.
sudo systemctl stop palimpsest-investigative-analysis.timer 2>/dev/null || true
sudo systemctl stop palimpsest-investigative-analysis.service 2>/dev/null || true
sudo systemctl stop palimpsest-investigative-broker.socket 2>/dev/null || true
sudo bash ops/investigative-analysis/install-host-bundle.sh
sudo systemctl enable --now palimpsest-investigative-broker.socket
sudo systemctl enable --now palimpsest-investigative-analysis.timer
sudo systemctl start palimpsest-investigative-analysis.service
```

The installer's optional single argument selects the image only while root
certifies the deployment. Its immutable image ID is written into the versioned
bundle; the recurring service accepts no image environment variable. Readings,
newswire, runs, private state, commit receipt, image ID, command, mounts, and
container arguments are fixed in code and in the units' filesystem policy.
Environment variables that appear to override those roots are intentionally
unsupported.

Docker group is root-equivalent, so UID 10001 has neither
`SupplementaryGroups=docker` nor direct access to `/var/run/docker.sock`.
It can connect only to a mode-0660 systemd socket. Each connection starts a
root-owned broker that verifies `SO_PEERCRED` is exactly UID/GID 10001, accepts
one bounded strict-JSON operation, rechecks the bundle/receipt/image identities,
and constructs the sole networkless Docker command itself. The broker owns the
run-directory parent, preventing the analysis process from replacing a checked
bind-mount path. Keep the bundle and unit files root-owned, and do not grant UID
10001 write access to `/usr/local/libexec/palimpsest-analysis`.

The broker's capability bounding set contains only `CAP_CHOWN`. It uses that
capability inside its sole writable path to grant UID/GID 10001 access to a
new staging tree, then seal every completed member back to root ownership before
atomic promotion. Its ambient capability set is empty. The analysis service has
an empty bounding set and never receives this capability. The broker also removes
Docker's root-owned CID receipt after a successful `--rm` run; the unprivileged
runner never deletes that receipt from the sticky staging directory.

## Verify

```sh
systemctl status palimpsest-investigative-analysis.service --no-pager
systemctl status palimpsest-investigative-broker.socket --no-pager
systemctl show -p SupplementaryGroups palimpsest-investigative-analysis.service
systemctl list-timers palimpsest-investigative-analysis.timer --no-pager
journalctl -u palimpsest-investigative-analysis.service -n 80 --no-pager
sudo jq '{schema_version,generated_at,edition_id,n_candidates,coverage}' \
  /var/lib/palimpsest-analysis/private/ledger/candidates-latest.json
sudo jq '{schema_version,attempted_at,completed_at,status,failure_class}' \
  /var/lib/palimpsest-analysis/private/analysis-status.json
sudo jq '{network_policy,publication_policy,steps,candidate_edition_id}' \
  "$(sudo jq -r .run_path /var/lib/palimpsest-analysis/private/state.json)"/readings/analysis-run-manifest.json
sudo jq '{edition_id,n_packets,publication_policy}' \
  "$(sudo jq -r .run_path /var/lib/palimpsest-analysis/private/state.json)"/private/analytical-packets-latest.json
sudo jq '{edition_id,n_drafts,publication_policy}' \
  "$(sudo jq -r .run_path /var/lib/palimpsest-analysis/private/state.json)"/private/analytical-drafts-latest.json
sudo jq '{edition_id,n_audits,counts,eligible:([.audits[]|select(.brief_eligible)]|length)}' \
  /var/lib/palimpsest-analysis/delivery/wire-claim-audits-latest.json
```

The manifest must say `docker-network-none` and `private-review-only`. A second
manual start with no changed evidence must log `unchanged` and add no candidate
version or forecast-history row.

For every later deploy, stop the analysis timer, wait for the oneshot service to
be inactive, stop the broker socket and confirm no broker instance remains,
update to a clean commit, rebuild the image, run `install-host-bundle.sh`, and
re-enable the broker socket and timer. If any verification or atomic rename
fails, leave both stopped: the previous receipt remains the last certified
deployment.
