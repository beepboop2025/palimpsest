# Palimpsest MCP release controller

This is the narrow production path for `palimpsest-mcp.service`. It releases one
reviewed commit from `origin/main`; it does not deploy the site, run collectors,
publish `server.json` to the MCP Registry, or accept a branch/path/URL from the
caller.

The controller enforces all of these before replacing runtime bytes:

- the requested value is an exact 40-character lowercase commit SHA;
- the commit is reachable from the repository's current `origin/main`;
- the Git author is the pinned Palimpsest release principal;
- GitHub attributes it to the pinned maintainer and reports a valid `web-flow`
  signature for that exact reviewed merge commit;
- `mcp/palimpsest_mcp.py` and `server.json` are exact blobs from the commit;
- server/manifest versions match, all six tools and four prompts discover, every
  tool is declared read-only/closed-world, and `get_newsroom` advertises
  `interconnection`;
- after an atomic replacement and service restart, loopback MCP initialize,
  tool/prompt discovery, `list_signals`, and
  `get_newsroom(view="interconnection")` all pass;
- only then is `/var/lib/palimpsest-mcp-deploy/deployed-sha` advanced. A failure
  after promotion restores the previous server file and restarts it.

The wrapper also pins the SHA-256 of its installed verifier, smoke client, and
systemd unit. Candidate inspection and live probing run as the separate,
unprivileged `palimpsest-mcp-verify` account with an empty environment; candidate
Python is never imported by the root controller process and does not share a UID
with the running service.

The GitHub workflow repeats the identity and contract checks in a no-secret job,
waits at the `palimpsest-mcp-production` environment gate, sends only
`deploy <sha>` through a pinned SSH host key, then repeats the full smoke through
`https://api.seiche.info/palimpsest/mcp` (therefore including the Caddy route).

## One-time host bootstrap

Do this manually from an independently reviewed, GitHub-verified `origin/main`
checkout. Compare the installed files before replacing them. The release workflow
cannot bootstrap or broaden its own authority.

1. Create the unprivileged runtime account and root-owned controller state:

   ```bash
   sudo useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin \
     palimpsest-mcp
   sudo useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin \
     palimpsest-mcp-verify
   sudo install -d -o root -g root -m 0755 /opt/palimpsest-mcp
   sudo install -d -o root -g root -m 0700 \
     /var/lib/palimpsest-mcp-deploy \
     /var/lib/palimpsest-mcp-deploy/backups \
     /var/lib/palimpsest-mcp-deploy/bootstrap-backups \
     /var/lib/palimpsest-mcp-deploy/receipts
   sudo git clone --mirror https://github.com/beepboop2025/palimpsest.git \
     /var/lib/palimpsest-mcp-deploy/repository.git
   sudo git --git-dir=/var/lib/palimpsest-mcp-deploy/repository.git \
     config transfer.fsckObjects true
   sudo git --git-dir=/var/lib/palimpsest-mcp-deploy/repository.git \
     config fetch.fsckObjects true
   ```

2. Install the trusted controller files. They stay outside the replaceable
   application directory, so a candidate cannot rewrite its own verifier:

   ```bash
   sudo install -o root -g root -m 0755 \
     ops/mcp-deploy/palimpsest-mcp-deploy-wrapper.sh \
     /usr/local/libexec/palimpsest-mcp-deploy
   sudo install -o root -g root -m 0755 \
     ops/mcp-deploy/verify_release.py \
     /usr/local/libexec/palimpsest-mcp-verify-release.py
   sudo install -o root -g root -m 0755 \
     scripts/smoke_palimpsest_mcp.py \
     /usr/local/libexec/palimpsest-mcp-smoke.py
   ```

3. Preserve the existing runtime until its current source commit is known. The
   pre-controller runtime is version 1.8.1 from exact core commit
   `2a80981815680006f3daf7caf503a125d6299c3c`. Extract that commit's exact
   `server.json` from the root-owned mirror and run the compatibility smoke with
   `--basic` before changing the service. This proves the old runtime against its
   own discovery contract without asking it for the later `interconnection`
   call.

   Installing new unit bytes does not replace an already-active legacy process:
   `enable --now` can leave its root-owned PID running. Install and reload the
   reviewed unit, explicitly restart the service, then prove the effective unit,
   PID owner, service identity, and hardening properties. Repeat the 1.8.1 basic
   smoke after the restart. Do not write `deployed-sha` by hand; the first
   successful controlled release creates it.

   ```bash
   (
     set -Eeuo pipefail
     readonly legacy_sha=2a80981815680006f3daf7caf503a125d6299c3c
     readonly expected_legacy_runtime_sha256=47d419e81ff048771acab14895a9b1e27868d7bbe14874e5cd8c1c94acfc4ed4
     readonly expected_legacy_unit_sha256=629e684f553c129f9c2ba570dc5369bbea2f8904f6b54cd297c0f01ead6b1155
     legacy_manifest=$(mktemp /tmp/palimpsest-mcp-1.8.1-server.XXXXXX)
     bootstrap_backup=$(sudo mktemp -d \
       /var/lib/palimpsest-mcp-deploy/bootstrap-backups/pre-controller.XXXXXX)
     sudo chmod 0700 "$bootstrap_backup"
     mutation_started=0
     bootstrap_committed=0

     finish_bootstrap() {
       rc=$?
       trap - EXIT
       set +e
       if [[ "$mutation_started" = 1 && "$bootstrap_committed" != 1 ]]; then
         printf 'bootstrap failed; restoring the captured legacy runtime and unit\n' >&2
         sudo install -o root -g root -m 0644 \
           "$bootstrap_backup/palimpsest_mcp.py" \
           /opt/palimpsest-mcp/palimpsest_mcp.py
         sudo install -o root -g root -m 0644 \
           "$bootstrap_backup/palimpsest-mcp.service" \
           /etc/systemd/system/palimpsest-mcp.service
         sudo systemctl daemon-reload
         sudo systemctl restart palimpsest-mcp.service
         sudo systemctl is-active --quiet palimpsest-mcp.service
         sudo timeout --kill-after=5s 90s \
           /usr/local/libexec/palimpsest-mcp-smoke.py \
           --url http://127.0.0.1:8793/ --allow-http-loopback \
           --module /opt/palimpsest-mcp/palimpsest_mcp.py \
           --manifest "$legacy_manifest" --basic
       fi
       rm -f -- "$legacy_manifest"
       exit "$rc"
     }
     trap finish_bootstrap EXIT

     test "$(sudo git --git-dir=/var/lib/palimpsest-mcp-deploy/repository.git \
       rev-parse --verify "${legacy_sha}^{commit}")" = "$legacy_sha"
     sudo git --git-dir=/var/lib/palimpsest-mcp-deploy/repository.git \
       show "${legacy_sha}:server.json" >"$legacy_manifest"
     chmod 0444 "$legacy_manifest"
     test "$(python3 -c \
       'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' \
       "$legacy_manifest")" = 1.8.1
     legacy_runtime_sha256=$(sudo sha256sum \
       /opt/palimpsest-mcp/palimpsest_mcp.py | awk '{print $1}')
     legacy_unit_sha256=$(sudo sha256sum \
       /etc/systemd/system/palimpsest-mcp.service | awk '{print $1}')
     test "$legacy_runtime_sha256" = "$expected_legacy_runtime_sha256"
     test "$legacy_unit_sha256" = "$expected_legacy_unit_sha256"
     sudo install -o root -g root -m 0600 \
       /opt/palimpsest-mcp/palimpsest_mcp.py \
       "$bootstrap_backup/palimpsest_mcp.py"
     sudo install -o root -g root -m 0600 \
       /etc/systemd/system/palimpsest-mcp.service \
       "$bootstrap_backup/palimpsest-mcp.service"
     printf '%s  %s\n%s  %s\n' \
       "$legacy_runtime_sha256" palimpsest_mcp.py \
       "$legacy_unit_sha256" palimpsest-mcp.service | \
       sudo tee "$bootstrap_backup/SHA256SUMS" >/dev/null
     sudo chmod 0600 "$bootstrap_backup/SHA256SUMS"
     test "$(sudo sha256sum "$bootstrap_backup/palimpsest_mcp.py" | \
       awk '{print $1}')" = "$legacy_runtime_sha256"
     test "$(sudo sha256sum "$bootstrap_backup/palimpsest-mcp.service" | \
       awk '{print $1}')" = "$legacy_unit_sha256"
     sudo timeout --kill-after=5s 90s \
       runuser --user palimpsest-mcp-verify -- \
       env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
       /usr/local/libexec/palimpsest-mcp-smoke.py \
       --url http://127.0.0.1:8793/ --allow-http-loopback \
       --module /opt/palimpsest-mcp/palimpsest_mcp.py \
       --manifest "$legacy_manifest" --basic

     mutation_started=1
     sudo install -o root -g root -m 0644 ops/systemd/palimpsest-mcp.service \
       /etc/systemd/system/palimpsest-mcp.service
     sudo systemctl daemon-reload
     sudo systemctl enable palimpsest-mcp.service
     sudo systemctl restart palimpsest-mcp.service
     sudo systemctl is-active --quiet palimpsest-mcp.service
     test "$(sudo systemctl show --property=FragmentPath --value \
       palimpsest-mcp.service)" = /etc/systemd/system/palimpsest-mcp.service
     test "$(sudo systemctl show --property=NeedDaemonReload --value \
       palimpsest-mcp.service)" = no
     test -z "$(sudo systemctl show --property=DropInPaths --value \
       palimpsest-mcp.service)"
     test "$(sudo systemctl show --property=User --value \
       palimpsest-mcp.service)" = palimpsest-mcp
     test "$(sudo systemctl show --property=Group --value \
       palimpsest-mcp.service)" = palimpsest-mcp

     main_pid=$(sudo systemctl show --property=MainPID --value \
       palimpsest-mcp.service)
     exec_main_pid=$(sudo systemctl show --property=ExecMainPID --value \
       palimpsest-mcp.service)
     [[ "$main_pid" =~ ^[1-9][0-9]*$ ]]
     test "$exec_main_pid" = "$main_pid"
     test "$(sudo ps -o uid= -p "$main_pid" | tr -d '[:space:]')" = \
       "$(id -u palimpsest-mcp)"

     for property_expected in \
       NoNewPrivileges=yes \
       ProtectSystem=strict \
       PrivateDevices=yes \
       PrivateTmp=yes \
       PrivateUsers=yes \
       ProtectHome=yes \
       ProtectKernelTunables=yes \
       RestrictSUIDSGID=yes \
       LockPersonality=yes \
       MemoryDenyWriteExecute=yes \
       RemoveIPC=yes \
       CapabilityBoundingSet= \
       AmbientCapabilities=; do
       property=${property_expected%%=*}
       expected=${property_expected#*=}
       actual=$(sudo systemctl show --property="$property" --value \
         palimpsest-mcp.service)
       test "$actual" = "$expected"
     done

     sudo timeout --kill-after=5s 90s \
       runuser --user palimpsest-mcp-verify -- \
       env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
       /usr/local/libexec/palimpsest-mcp-smoke.py \
       --url http://127.0.0.1:8793/ --allow-http-loopback \
       --module /opt/palimpsest-mcp/palimpsest_mcp.py \
       --manifest "$legacy_manifest" --basic
     bootstrap_committed=1
     printf 'bootstrap complete; legacy preimage retained at %s\n' \
       "$bootstrap_backup"
   )
   ```

4. Put a dedicated Ed25519 public key in root's `authorized_keys`, restricted to
   the root-owned controller and with every interactive/forwarding feature off:

   Generate this key specifically for the workflow. Do not reuse a workstation,
   Seiche administration, or general Hetzner key. Keep the private key out of
   shell history and logs, and retain it only in the protected GitHub environment
   and an approved secret-recovery store.

   ```bash
   umask 077
   deploy_key_dir=$(mktemp -d /tmp/palimpsest-mcp-deploy-key.XXXXXX)
   ssh-keygen -q -t ed25519 -N '' \
     -C palimpsest-mcp-deploy \
     -f "$deploy_key_dir/palimpsest-mcp-deploy"
   ssh-keygen -lf "$deploy_key_dir/palimpsest-mcp-deploy.pub"
   ```

   ```text
   command="/usr/local/libexec/palimpsest-mcp-deploy",restrict ssh-ed25519 AAAA... palimpsest-mcp-deploy
   ```

   `restrict` requires a modern OpenSSH. On an older host, use the explicit
   equivalent `no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding`.
   The key is intentionally not shared with general server administration.

5. Create a protected GitHub environment named
   `palimpsest-mcp-production`, require a reviewer, and configure:

   - secret `PALIMPSEST_MCP_DEPLOY_HOST` — hostname or IPv4 address;
   - secret `PALIMPSEST_MCP_DEPLOY_KEY` — that dedicated private key;
   - secret `PALIMPSEST_MCP_SSH_HOST_KEY` — exact `ssh-ed25519 AAAA...` host key;
   - optional variable `PALIMPSEST_MCP_SSH_PORT` — defaults to `22`.

   Verify the server's Ed25519 host-key fingerprint through the host console or
   another independent channel before storing its raw public key; `ssh-keyscan`
   alone is not identity verification. Configure the environment before adding
   secrets, enable required-reviewer protection, and test that the dedicated key
   cannot open a shell or run anything except the exact forced command.

## Release transaction

The target must be a reviewed merge on `main` whose GitHub API verification is
`verified: true`, not an unsigned feature-branch commit. Scheduled publishers
normally advance `main` with single-parent data commits, while Registry
publication deliberately requires the deployed SHA to remain the exact current
tip. Freeze every scheduled workflow before the release merge, wait for all
already-started runs, and keep that gate closed through deployment and Registry
verification.

Create a fresh state manifest from the reviewed checkout. The expected count is
an intentional drift alarm: review this transaction whenever a scheduled
workflow is added or removed. Do not use a shell variable named `path` in zsh;
it aliases the executable search path.

If this release continues an already-open publication gate, reuse that gate's
original preservation manifest. Never recapture state after workflows have been
temporarily disabled: doing so would misclassify the gate as the desired steady
state and leave schedules off after release.

```bash
set -euo pipefail
repo=beepboop2025/palimpsest
release_gate_dir=$(mktemp -d /tmp/palimpsest-mcp-release-gate.XXXXXX)
schedule_manifest="$release_gate_dir/scheduled-workflows.tsv"
workflow_inventory="$release_gate_dir/workflows.json"

gh workflow list --repo "$repo" --all --json id,path,state \
  >"$workflow_inventory"
: >"$schedule_manifest"
for workflow_file in $(rg -l '^  schedule:' .github/workflows/*.yml); do
  jq -r --arg workflow_file "$workflow_file" \
    '.[] | select(.path == $workflow_file) | [.id,.state,.path] | @tsv' \
    "$workflow_inventory" >>"$schedule_manifest"
done
LC_ALL=C sort -o "$schedule_manifest" "$schedule_manifest"
test "$(wc -l <"$schedule_manifest" | tr -d '[:space:]')" = 34
awk -F '\t' 'NF != 3 { exit 1 }' "$schedule_manifest"

while IFS=$'\t' read -r workflow_id expected_state workflow_file; do
  if [[ "$expected_state" = active ]]; then
    gh workflow disable "$workflow_id" --repo "$repo"
  fi
done <"$schedule_manifest"

while gh run list --repo "$repo" --limit 1000 \
  --json status --jq '.[] | select(.status == "queued" or .status == "in_progress")' |
  grep -q .; do
  sleep 15
done
git fetch origin --prune
frozen_main=$(git rev-parse origin/main)
sleep 10
git fetch origin --prune
test "$(git rev-parse origin/main)" = "$frozen_main"
printf 'release gate: %s\nschedule manifest: %s\n' \
  "$frozen_main" "$schedule_manifest"
```

Only now merge the final reviewed pull request through GitHub. Capture its
GitHub-signed merge commit as `target_sha`, prove it is still the exact tip, and
dispatch explicitly from `main`. Keep the schedule manifest and the gate in
place if either workflow needs a retry.

```bash
git fetch origin --prune
target_sha=$(git rev-parse origin/main)
test "$target_sha" != "$frozen_main"
gh api "repos/$repo/commits/$target_sha" --jq \
  'select(.author.login == "beepboop2025") |
   select(.committer.login == "web-flow") |
   select((.parents | length) >= 2) |
   select(.commit.verification.verified == true) |
   select(.commit.verification.reason == "valid") | .sha' | \
  grep -Fx "$target_sha"

gh workflow run deploy-mcp.yml --repo "$repo" --ref main \
  -f target_sha="$target_sha"
deploy_run_id=$(gh run list --repo "$repo" --workflow deploy-mcp.yml \
  --event workflow_dispatch --limit 20 \
  --json databaseId,headSha,createdAt \
  --jq "map(select(.headSha == \"$target_sha\"))[0].databaseId")
[[ "$deploy_run_id" =~ ^[1-9][0-9]*$ ]]
gh run watch "$deploy_run_id" --repo "$repo" --exit-status
```

After the workflow succeeds, independently check the public endpoint and the
receipt on the host:

```bash
python3 scripts/smoke_palimpsest_mcp.py \
  --url https://api.seiche.info/palimpsest/mcp \
  --module mcp/palimpsest_mcp.py --manifest server.json
sudo cat /var/lib/palimpsest-mcp-deploy/deployed-sha
sudo cat /var/lib/palimpsest-mcp-deploy/receipts/<target-sha>.json
```

The successful deploy run uploads a non-secret artifact named
`palimpsest-mcp-deployment-<target-sha>-run-<run-id>-attempt-<attempt>`. It binds
the exact SHA, run attempt, and server version to the forced-command deployment
and public smoke. Only after the live SHA, version, discovery, calls, and
artifact are verified should the separate Registry transaction run:

```bash
gh workflow run registry-publish.yml \
  --repo "$repo" --ref main \
  -f target_sha="$target_sha" \
  -f deploy_run_id="$deploy_run_id"
registry_run_id=$(gh run list --repo "$repo" --workflow registry-publish.yml \
  --event workflow_dispatch --limit 20 \
  --json databaseId,headSha,createdAt \
  --jq "map(select(.headSha == \"$target_sha\"))[0].databaseId")
[[ "$registry_run_id" =~ ^[1-9][0-9]*$ ]]
gh run watch "$registry_run_id" --repo "$repo" --exit-status
```

The Registry workflow checks out that exact current `origin/main`, requires a
clean tree, validates the selected successful deployment run and its receipt,
repeats the public smoke, publishes, then polls the official Registry until the
latest active record exactly matches `server.json`. A successful runtime
deployment is not an MCP Registry publication, and vice versa.

Its non-secret artifact is named
`palimpsest-mcp-registry-<target-sha>-run-<run-id>-attempt-<attempt>`. It retains
the exact official Registry response plus a canonical receipt binding that
response's SHA-256, the deployed SHA and run, server identity/version, Registry
status, and publication workflow attempt.

After the live smoke, host receipt, deployment artifact, Registry receipt, and
official latest record all agree, restore exactly the states captured in the
manifest. An intentionally disabled workflow stays disabled.

```bash
while IFS=$'\t' read -r workflow_id expected_state workflow_file; do
  if [[ "$expected_state" = active ]]; then
    gh workflow enable "$workflow_id" --repo "$repo"
  fi
done <"$schedule_manifest"

gh workflow list --repo "$repo" --all --json id,path,state \
  >"$release_gate_dir/workflows-restored.json"
while IFS=$'\t' read -r workflow_id expected_state workflow_file; do
  actual_state=$(jq -r --argjson workflow_id "$workflow_id" \
    '.[] | select(.id == $workflow_id) | .state' \
    "$release_gate_dir/workflows-restored.json")
  test "$actual_state" = "$expected_state"
done <"$schedule_manifest"
```

Archive the manifest beside the deployment and Registry receipts. If the
transaction is abandoned before publication, restore the captured workflow
states; a later attempt needs a new frozen tip and a new signed merge.

## Rollback after a completed release

The controller automatically restores the previous runtime, marker, and
same-SHA receipt when a candidate fails before its transaction commits. Once a
release prints `release complete`, never edit `deployed-sha`, overwrite a receipt,
or silently copy an old module over the live file.

For a non-emergency rollback, freeze the scheduled publishers again and prepare
a new reviewed pull request that restores the known-good behavior under a new,
monotonically higher server version. Merge it through GitHub, then run the same
deployment and Registry transactions. Registry versions are immutable; do not
attempt to republish `1.8.1` or reuse a withdrawn version number.

If availability requires an emergency host restore before that merge is ready,
preserve the completed deployment receipt and restore only the exact controller
backup recorded for the failed release. Treat the host as incident-degraded—the
runtime intentionally no longer agrees with `deployed-sha`—and do not claim a
verified deployment or publish the Registry until a new signed release repairs
that divergence. Record the backup digest, the preserved marker, the incident
time, and the subsequent repair SHA; never rewrite historical evidence to make
the states appear consistent.
