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
     /var/lib/palimpsest-mcp-deploy/incidents \
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
     legacy_enablement=$(sudo systemctl is-enabled \
       palimpsest-mcp.service 2>/dev/null || true)
     case "$legacy_enablement" in
       enabled|disabled) ;;
       *)
         printf 'unsupported legacy service enablement: %s\n' \
           "$legacy_enablement" >&2
         exit 1
         ;;
     esac
     mutation_started=0
     bootstrap_committed=0

     finish_bootstrap() {
       rc=$?
       trap - EXIT
       set +e
       if [[ "$mutation_started" = 1 && "$bootstrap_committed" != 1 ]]; then
         rollback_failed=0
         printf 'bootstrap failed; restoring the captured legacy runtime and unit\n' >&2
         sudo install -o root -g root -m 0644 \
           "$bootstrap_backup/palimpsest_mcp.py" \
           /opt/palimpsest-mcp/palimpsest_mcp.py || rollback_failed=1
         sudo install -o root -g root -m 0644 \
           "$bootstrap_backup/palimpsest-mcp.service" \
           /etc/systemd/system/palimpsest-mcp.service || rollback_failed=1
         sudo systemctl daemon-reload || rollback_failed=1
         if [[ "$legacy_enablement" = enabled ]]; then
           sudo systemctl enable palimpsest-mcp.service || rollback_failed=1
         else
           sudo systemctl disable palimpsest-mcp.service || rollback_failed=1
         fi
         test "$(sudo systemctl is-enabled palimpsest-mcp.service 2>/dev/null)" = \
           "$legacy_enablement" || rollback_failed=1
         test "$(sudo sha256sum /opt/palimpsest-mcp/palimpsest_mcp.py | \
           awk '{print $1}')" = "$expected_legacy_runtime_sha256" || \
           rollback_failed=1
         test "$(sudo sha256sum /etc/systemd/system/palimpsest-mcp.service | \
           awk '{print $1}')" = "$expected_legacy_unit_sha256" || \
           rollback_failed=1
         sudo systemctl restart palimpsest-mcp.service || rollback_failed=1
         sudo systemctl is-active --quiet palimpsest-mcp.service || \
           rollback_failed=1
         sudo timeout --kill-after=5s 90s \
           /usr/local/libexec/palimpsest-mcp-smoke.py \
           --url http://127.0.0.1:8793/ --allow-http-loopback \
           --module /opt/palimpsest-mcp/palimpsest_mcp.py \
           --manifest "$legacy_manifest" --basic || rollback_failed=1
         if [[ "$rollback_failed" != 0 ]]; then
           printf 'bootstrap rollback did not restore every captured invariant\n' >&2
           exit 1
         fi
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
     printf '%s\n' "$legacy_enablement" | \
       sudo tee "$bootstrap_backup/SERVICE_ENABLEMENT" >/dev/null
     sudo chmod 0600 "$bootstrap_backup/SERVICE_ENABLEMENT"
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
   cleanup_deploy_key() {
     if [[ -n "${deploy_key_dir:-}" ]]; then
       rm -f -- \
         "$deploy_key_dir/palimpsest-mcp-deploy" \
         "$deploy_key_dir/palimpsest-mcp-deploy.pub"
       rmdir -- "$deploy_key_dir" 2>/dev/null || true
       unset deploy_key_dir
     fi
   }
   trap cleanup_deploy_key EXIT HUP INT TERM
   ssh-keygen -q -t ed25519 -N '' \
     -C palimpsest-mcp-deploy \
     -f "$deploy_key_dir/palimpsest-mcp-deploy"
   ssh-keygen -lf "$deploy_key_dir/palimpsest-mcp-deploy.pub"
   ```

   ```text
   command="/usr/local/libexec/palimpsest-mcp-deploy",restrict ssh-ed25519 AAAA... palimpsest-mcp-deploy
   ```

   After the private key has been entered into the protected GitHub environment
   and the approved secret-recovery store, independently verify both copies and
   remove the temporary local key directory in the same shell. Do not leave the
   generated private key in `/tmp`; overwriting is not a reliable secure-erasure
   claim on SSD or copy-on-write storage.

   ```bash
   test -f "$deploy_key_dir/palimpsest-mcp-deploy"
   test -f "$deploy_key_dir/palimpsest-mcp-deploy.pub"
   cleanup_deploy_key
   trap - EXIT HUP INT TERM
   unset -f cleanup_deploy_key
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

Create the state manifest from the exact pre-merge `origin/main` tree, not from
the candidate checkout. The China WDI release deliberately changes
`china-econ-refresh.yml` from scheduled to manual-only: the live pre-merge union
has 34 scheduled workflow paths, including China econ, while the reviewed
post-merge tree has 33. Both exact counts and the one-path difference are drift
alarms. Do not use a shell variable named `path` in zsh; it aliases the
executable search path.

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
premerge_schedule_paths="$release_gate_dir/premerge-scheduled-paths.txt"
postmerge_schedule_paths="$release_gate_dir/postmerge-scheduled-paths.txt"

git fetch --force --no-tags origin \
  '+refs/heads/main:refs/remotes/origin/main'
frozen_main=$(git rev-parse origin/main)
git grep -l '^  schedule:' "$frozen_main" -- '.github/workflows/*.yml' | \
  LC_ALL=C sort >"$premerge_schedule_paths"
test "$(wc -l <"$premerge_schedule_paths" | tr -d '[:space:]')" = 34
grep -Fx '.github/workflows/china-econ-refresh.yml' \
  "$premerge_schedule_paths"

gh workflow list --repo "$repo" --all --json id,path,state \
  >"$workflow_inventory"
: >"$schedule_manifest"
while IFS= read -r workflow_file; do
  jq -r --arg workflow_file "$workflow_file" \
    '.[] | select(.path == $workflow_file) | [.id,.state,.path] | @tsv' \
    "$workflow_inventory" >>"$schedule_manifest"
done <"$premerge_schedule_paths"
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
sleep 10
git fetch --force --no-tags origin \
  '+refs/heads/main:refs/remotes/origin/main'
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
git grep -l '^  schedule:' "$target_sha" -- '.github/workflows/*.yml' | \
  LC_ALL=C sort >"$postmerge_schedule_paths"
test "$(wc -l <"$postmerge_schedule_paths" | tr -d '[:space:]')" = 33
test "$(comm -23 "$premerge_schedule_paths" \
  "$postmerge_schedule_paths")" = \
  '.github/workflows/china-econ-refresh.yml'
test -z "$(comm -13 "$premerge_schedule_paths" \
  "$postmerge_schedule_paths")"
gh api "repos/$repo/commits/$target_sha" --jq \
  'select(.author.login == "beepboop2025") |
   select(.committer.login == "web-flow") |
   select((.parents | length) >= 2) |
   select(.commit.verification.verified == true) |
   select(.commit.verification.reason == "valid") | .sha' | \
  grep -Fx "$target_sha"

snapshot_workflow_runs() {
  workflow_name=$1
  output_file=$2
  gh run list --repo "$repo" --workflow "$workflow_name" \
    --event workflow_dispatch --limit 100 \
    --json databaseId --jq '.[].databaseId' | \
    LC_ALL=C sort -n >"$output_file"
}

wait_for_one_new_run() {
  workflow_name=$1
  before_file=$2
  expected_sha=$3
  for attempt in $(seq 1 24); do
    new_run_ids=""
    for candidate_run_id in $(gh run list --repo "$repo" \
      --workflow "$workflow_name" --event workflow_dispatch --limit 100 \
      --json databaseId,event,headSha \
      --jq ".[] | select(.event == \"workflow_dispatch\" and .headSha == \"$expected_sha\") | .databaseId"); do
      if ! grep -Fxq "$candidate_run_id" "$before_file"; then
        new_run_ids="${new_run_ids}${candidate_run_id}\n"
      fi
    done
    new_run_count=$(printf '%b' "$new_run_ids" | sed '/^$/d' | wc -l | \
      tr -d '[:space:]')
    if [[ "$new_run_count" = 1 ]]; then
      printf '%b' "$new_run_ids" | sed '/^$/d'
      return 0
    fi
    if [[ "$new_run_count" -gt 1 ]]; then
      printf 'ambiguous new %s runs for %s\n' "$workflow_name" "$expected_sha" >&2
      return 1
    fi
    sleep 5
  done
  printf 'new %s run did not appear for %s\n' "$workflow_name" "$expected_sha" >&2
  return 1
}

deploy_runs_before="$release_gate_dir/deploy-runs-before.txt"
snapshot_workflow_runs deploy-mcp.yml "$deploy_runs_before"
gh workflow run deploy-mcp.yml --repo "$repo" --ref main \
  -f target_sha="$target_sha"
deploy_run_id=$(wait_for_one_new_run \
  deploy-mcp.yml "$deploy_runs_before" "$target_sha")
[[ "$deploy_run_id" =~ ^[1-9][0-9]*$ ]]
gh api "repos/$repo/actions/runs/$deploy_run_id" --jq \
  "select(.event == \"workflow_dispatch\") |
   select(.head_branch == \"main\") |
   select(.head_sha == \"$target_sha\") |
   select((.path | split(\"@\")[0]) == \".github/workflows/deploy-mcp.yml\") |
   .id" | grep -Fx "$deploy_run_id"
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
registry_runs_before="$release_gate_dir/registry-runs-before.txt"
snapshot_workflow_runs registry-publish.yml "$registry_runs_before"
gh workflow run registry-publish.yml \
  --repo "$repo" --ref main \
  -f target_sha="$target_sha" \
  -f deploy_run_id="$deploy_run_id"
registry_run_id=$(wait_for_one_new_run \
  registry-publish.yml "$registry_runs_before" "$target_sha")
[[ "$registry_run_id" =~ ^[1-9][0-9]*$ ]]
gh api "repos/$repo/actions/runs/$registry_run_id" --jq \
  "select(.event == \"workflow_dispatch\") |
   select(.head_branch == \"main\") |
   select(.head_sha == \"$target_sha\") |
   select((.path | split(\"@\")[0]) == \".github/workflows/registry-publish.yml\") |
   .id" | grep -Fx "$registry_run_id"
gh run watch "$registry_run_id" --repo "$repo" --exit-status
```

The Registry workflow checks out that exact current `origin/main`, requires a
clean tree, validates the selected successful deployment run and its receipt,
repeats the public smoke, and preflights the exact immutable Registry version.
It publishes only when that version is absent; if an earlier attempt already
published the exact active server card but failed before artifact upload, it
skips the irreversible publish and reconstructs the receipt. Both paths poll
the official Registry until the latest active record exactly matches
`server.json`. A successful runtime deployment is not an MCP Registry
publication, and vice versa.

Its non-secret artifact is named
`palimpsest-mcp-registry-<target-sha>-run-<run-id>-attempt-<attempt>`. It retains
the exact official Registry response plus a canonical receipt binding that
response's SHA-256, the deployed SHA and run, server identity/version, Registry
status, and publication workflow attempt.

Keep the writer gate closed while publishing Pages. An ordinary `main` push
validates the code but deliberately cannot package or deploy Pages. Snapshot the
existing repository-dispatch Tests runs, dispatch one complete contract for the
same still-current SHA, and require exactly one new run:

```bash
tests_runs_before="$release_gate_dir/tests-repository-dispatch-before.txt"
gh run list --repo "$repo" --workflow tests.yml \
  --event repository_dispatch --limit 100 \
  --json databaseId --jq '.[].databaseId' | \
  LC_ALL=C sort -n >"$tests_runs_before"

git fetch --force --no-tags origin \
  '+refs/heads/main:refs/remotes/origin/main'
test "$(git rev-parse origin/main)" = "$target_sha"
gh api --method POST "repos/$repo/dispatches" \
  -f event_type=publication_contract \
  -f 'client_payload[sha]'="$target_sha" \
  -f 'client_payload[scope]'=complete >/dev/null

tests_run_id=""
for attempt in $(seq 1 24); do
  new_run_ids=""
  for candidate_run_id in $(gh run list --repo "$repo" \
    --workflow tests.yml --event repository_dispatch --limit 100 \
    --json databaseId,event,headSha \
    --jq ".[] | select(.event == \"repository_dispatch\" and .headSha == \"$target_sha\") | .databaseId"); do
    if ! grep -Fxq "$candidate_run_id" "$tests_runs_before"; then
      new_run_ids="${new_run_ids}${candidate_run_id}\n"
    fi
  done
  new_run_count=$(printf '%b' "$new_run_ids" | sed '/^$/d' | wc -l | \
    tr -d '[:space:]')
  if [[ "$new_run_count" = 1 ]]; then
    tests_run_id=$(printf '%b' "$new_run_ids" | sed '/^$/d')
    break
  fi
  if [[ "$new_run_count" -gt 1 ]]; then
    printf 'ambiguous complete publication runs for %s\n' "$target_sha" >&2
    exit 1
  fi
  sleep 5
done
[[ "$tests_run_id" =~ ^[1-9][0-9]*$ ]]
gh api "repos/$repo/actions/runs/$tests_run_id" --jq \
  "select(.event == \"repository_dispatch\") |
   select(.head_branch == \"main\") |
   select(.head_sha == \"$target_sha\") |
   select((.path | split(\"@\")[0]) == \".github/workflows/tests.yml\") |
   .id" | grep -Fx "$tests_run_id"
gh run watch "$tests_run_id" --repo "$repo" --exit-status

pages_jobs="$release_gate_dir/pages-jobs.json"
gh api --paginate \
  "repos/$repo/actions/runs/$tests_run_id/jobs?per_page=100" \
  >"$pages_jobs"
for required_job in \
  pytest \
  contract \
  'Admit the exact tested publication' \
  'Admit exact deployed MCP release before Pages' \
  'Package exact complete Pages edition' \
  'Deploy exact complete Pages edition'; do
  jq -e --arg required_job "$required_job" '
    any(.jobs[]; .name == $required_job and .conclusion == "success")
  ' "$pages_jobs" >/dev/null
done
git fetch --force --no-tags origin \
  '+refs/heads/main:refs/remotes/origin/main'
test "$(git rev-parse origin/main)" = "$target_sha"
```

Finally, prove Pages serves selected exact blobs from that same commit. The
unique query prevents an intermediary from satisfying the check with an older
cached response. All paths must converge before the writer gate can reopen:

```bash
served_dir="$release_gate_dir/pages-served"
install -d -m 0700 "$served_dir"
for relative_path in \
  .well-known/ai-catalog.json \
  server.json \
  readings/osint-china-latest.json \
  readings/readings-ledger.jsonl \
  readings/audit/readings-ledger-recovery-20260824.json; do
  expected_digest=$(git show "$target_sha:$relative_path" | sha256sum | \
    awk '{print $1}')
  served_file="$served_dir/$(printf '%s' "$relative_path" | tr '/' '_')"
  matched=0
  for attempt in $(seq 1 24); do
    curl --disable --fail --silent --show-error \
      --proto '=https' --tlsv1.2 --max-time 30 --max-filesize 134217728 \
      --header 'Cache-Control: no-cache' \
      "https://palimpsest.info/${relative_path}?release=${target_sha}" \
      --output "$served_file"
    if [[ "$(sha256sum "$served_file" | awk '{print $1}')" = \
          "$expected_digest" ]]; then
      matched=1
      break
    fi
    sleep 10
  done
  test "$matched" = 1
done

git fetch --force --no-tags origin \
  '+refs/heads/main:refs/remotes/origin/main'
test "$(git rev-parse origin/main)" = "$target_sha"
```

Only after the live smoke, host receipt, deployment artifact, Registry receipt,
official latest record, exact complete Tests run, Pages deployment, and served
bytes all agree may the states captured in the manifest be restored. An
intentionally disabled workflow stays disabled. Restore the original 34 API
states even though the target has only 33 scheduled paths: re-enabling the China
econ workflow exposes its reviewed manual dispatch but cannot recreate the
removed schedule.

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
same-SHA receipt when a candidate fails before its transaction commits. Host
receipt schema v2 binds the exact previous-runtime backup basename, SHA-256, and
Git source SHA; the controller refuses to overwrite a completed same-SHA
receipt. Once a release prints `release complete`, never edit `deployed-sha`,
overwrite a receipt, or silently copy an old module over the live file.

For a non-emergency rollback, freeze the scheduled publishers again and prepare
a new reviewed pull request that restores the known-good behavior under a new,
monotonically higher server version. Merge it through GitHub, then run the same
deployment and Registry transactions. Registry versions are immutable; do not
attempt to republish `1.8.1` or reuse a withdrawn version number.

If availability requires an emergency host restore before that merge is ready,
freeze scheduled publishers as above and run the following transaction on the
production host. Set `incident_target_sha` to the completed release being backed
out. It accepts only the backup and digest bound into that release's immutable
host receipt, independently proves those bytes against the prior Git source,
preserves the current runtime/marker/receipt, atomically restores, restarts and
smokes the prior version, and writes an append-only incident receipt. A failed
restore automatically puts the released runtime back and smokes it against its
own manifest.

```bash
(
  set -Eeuo pipefail
  incident_target_sha=${incident_target_sha:?set the completed release SHA}
  [[ "$incident_target_sha" =~ ^[0-9a-f]{40}$ ]]
  sudo test ! -L /var/lib/palimpsest-mcp-deploy/deploy.lock
  sudo /usr/bin/flock -n /var/lib/palimpsest-mcp-deploy/deploy.lock \
    /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    /usr/bin/bash -s -- "$incident_target_sha" <<'PALIMPSEST_EMERGENCY_ROLLBACK'
  set -Eeuo pipefail
  incident_target_sha=$1
  shift
  test "$#" = 0
  readonly state_dir=/var/lib/palimpsest-mcp-deploy
  readonly repository="$state_dir/repository.git"
  readonly backup_dir="$state_dir/backups"
  readonly receipt_dir="$state_dir/receipts"
  readonly incident_root="$state_dir/incidents"
  readonly incident_state_file="$state_dir/incident-degraded.json"
  readonly marker_file="$state_dir/deployed-sha"
  readonly target_file=/opt/palimpsest-mcp/palimpsest_mcp.py
  readonly service=palimpsest-mcp.service
  readonly smoke=/usr/local/libexec/palimpsest-mcp-smoke.py
  readonly expected_smoke_sha256=1e3f1c4eb6d5b8a4960aa1f55dd3a74f6df277f93fc17a42db5a0ee2ec8846f1
  readonly legacy_sha=2a80981815680006f3daf7caf503a125d6299c3c
  readonly legacy_runtime_sha256=47d419e81ff048771acab14895a9b1e27868d7bbe14874e5cd8c1c94acfc4ed4
  [[ "$incident_target_sha" =~ ^[0-9a-f]{40}$ ]]

  require_root_file() {
    local checked_file=$1
    local checked_mode
    sudo test -f "$checked_file"
    sudo test ! -L "$checked_file"
    test "$(sudo stat -c '%u' "$checked_file")" = 0
    test "$(sudo stat -c '%h' "$checked_file")" = 1
    checked_mode=$(sudo stat -c '%a' "$checked_file")
    (( (8#$checked_mode & 0022) == 0 ))
  }

  require_root_directory() {
    local checked_directory=$1
    local checked_mode
    sudo test -d "$checked_directory"
    sudo test ! -L "$checked_directory"
    test "$(sudo stat -c '%u' "$checked_directory")" = 0
    checked_mode=$(sudo stat -c '%a' "$checked_directory")
    (( (8#$checked_mode & 0022) == 0 ))
  }

  require_root_directory "$state_dir"
  require_root_directory "$repository"
  require_root_directory "$backup_dir"
  require_root_directory "$receipt_dir"
  require_root_directory "$incident_root"
  require_root_directory "$(dirname "$target_file")"
  require_root_file "$smoke"
  test "$(sudo sha256sum "$smoke" | awk '{print $1}')" = \
    "$expected_smoke_sha256"
  test "$(sudo git --git-dir="$repository" remote get-url origin)" = \
    https://github.com/beepboop2025/palimpsest.git
  release_receipt="$receipt_dir/${incident_target_sha}.json"
  require_root_file "$release_receipt"
  require_root_file "$marker_file"
  require_root_file "$target_file"
  sudo test ! -e "$incident_state_file"
  sudo test ! -L "$incident_state_file"
  test "$(sudo stat -c '%s' "$marker_file")" = 41
  sudo grep -Eq '^[0-9a-f]{40}$' "$marker_file"
  test "$(sudo head -n 1 "$marker_file")" = "$incident_target_sha"
  receipt_fields=$(sudo python3 - "$release_receipt" <<'PY'
import json
import re
import sys

def reject_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


with open(sys.argv[1], encoding="utf-8") as handle:
    receipt = json.load(
        handle,
        object_pairs_hook=strict_object,
        parse_constant=reject_constant,
    )
expected_keys = {
    "deployed_at_utc",
    "previous_runtime_backup",
    "previous_runtime_sha256",
    "previous_runtime_source_sha",
    "previous_sha",
    "schema_version",
    "server_file_sha256",
    "server_version",
    "service",
    "target_sha",
    "verification",
}
if not isinstance(receipt, dict) or set(receipt) != expected_keys:
    raise SystemExit("host receipt shape drifted")
if receipt.get("schema_version") != 2:
    raise SystemExit("host receipt is not rollback-bound schema v2")
if receipt.get("service") != "palimpsest-mcp.service":
    raise SystemExit("host receipt service drifted")
if receipt.get("verification") != {
    "github_signature": "valid",
    "local_initialize_list_call": "passed",
    "target_on_origin_main": True,
}:
    raise SystemExit("host receipt verification evidence drifted")
target = receipt.get("target_sha")
previous = receipt.get("previous_sha") or "-"
previous_source = receipt.get("previous_runtime_source_sha") or "-"
runtime_digest = receipt.get("server_file_sha256")
previous_digest = receipt.get("previous_runtime_sha256")
backup = receipt.get("previous_runtime_backup")
if not isinstance(target, str) or re.fullmatch(r"[0-9a-f]{40}", target) is None:
    raise SystemExit("invalid target SHA in host receipt")
if previous != "-" and re.fullmatch(r"[0-9a-f]{40}", previous) is None:
    raise SystemExit("invalid previous SHA in host receipt")
if previous_source != "-" and re.fullmatch(r"[0-9a-f]{40}", previous_source) is None:
    raise SystemExit("invalid previous runtime source SHA in host receipt")
if (previous == "-") != (previous_source == "-"):
    raise SystemExit("previous marker and runtime source presence disagree")
if not isinstance(runtime_digest, str) or re.fullmatch(r"[0-9a-f]{64}", runtime_digest) is None:
    raise SystemExit("invalid released runtime digest")
if not isinstance(previous_digest, str) or re.fullmatch(r"[0-9a-f]{64}", previous_digest) is None:
    raise SystemExit("invalid previous runtime digest")
if not isinstance(backup, str) or re.fullmatch(
    r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{64}\.[A-Za-z0-9]{6}\.py", backup
) is None:
    raise SystemExit("unsafe previous runtime backup basename")
if previous_digest not in backup:
    raise SystemExit("backup basename is not bound to its runtime digest")
print("\t".join((
    target,
    previous,
    previous_source,
    runtime_digest,
    previous_digest,
    backup,
)))
PY
  )
  IFS=$'\t' read -r receipt_target previous_field previous_source_field \
    released_digest previous_digest backup_basename <<<"$receipt_fields"
  previous_sha=""
  if [[ "$previous_field" != - ]]; then
    previous_sha=$previous_field
  fi
  previous_runtime_source_sha=""
  if [[ "$previous_source_field" != - ]]; then
    previous_runtime_source_sha=$previous_source_field
  fi
  test "$receipt_target" = "$incident_target_sha"
  test "$(sudo sha256sum "$target_file" | awk '{print $1}')" = \
    "$released_digest"
  source_released_digest=$(sudo git --git-dir="$repository" \
    show "${incident_target_sha}:mcp/palimpsest_mcp.py" | sha256sum | \
    awk '{print $1}')
  test "$source_released_digest" = "$released_digest"

  backup_file="$backup_dir/$backup_basename"
  require_root_file "$backup_file"
  test "$(sudo sha256sum "$backup_file" | awk '{print $1}')" = \
    "$previous_digest"
  previous_source_sha=${previous_runtime_source_sha:-$legacy_sha}
  test "$(sudo git --git-dir="$repository" rev-parse --verify \
    "${previous_source_sha}^{commit}")" = "$previous_source_sha"
  source_previous_digest=$(sudo git --git-dir="$repository" \
    show "${previous_source_sha}:mcp/palimpsest_mcp.py" | sha256sum | \
    awk '{print $1}')
  test "$source_previous_digest" = "$previous_digest"
  if [[ "$previous_source_sha" = "$legacy_sha" ]]; then
    test "$previous_digest" = "$legacy_runtime_sha256"
  fi

  previous_manifest=$(mktemp /tmp/palimpsest-mcp-rollback-previous.XXXXXX)
  released_manifest=$(mktemp /tmp/palimpsest-mcp-rollback-released.XXXXXX)
  sudo git --git-dir="$repository" \
    show "${previous_source_sha}:server.json" | tee "$previous_manifest" >/dev/null
  sudo git --git-dir="$repository" \
    show "${incident_target_sha}:server.json" | tee "$released_manifest" >/dev/null
  chmod 0444 "$previous_manifest" "$released_manifest"

  incident_at=$(date -u '+%Y%m%dT%H%M%SZ')
  incident_dir=$(sudo mktemp -d \
    "$incident_root/${incident_at}-${incident_target_sha}.XXXXXX")
  sudo chmod 0700 "$incident_dir"
  sudo install -o root -g root -m 0600 "$target_file" \
    "$incident_dir/released-runtime.py"
  sudo install -o root -g root -m 0600 "$marker_file" \
    "$incident_dir/deployed-sha"
  sudo install -o root -g root -m 0600 "$release_receipt" \
    "$incident_dir/deployment-receipt.json"
  released_receipt_digest=$(sudo sha256sum "$release_receipt" | awk '{print $1}')
  marker_digest=$(sudo sha256sum "$marker_file" | awk '{print $1}')
  test "$(sudo sha256sum "$incident_dir/released-runtime.py" | awk '{print $1}')" = \
    "$released_digest"
  test "$(sudo sha256sum "$incident_dir/deployed-sha" | awk '{print $1}')" = \
    "$marker_digest"
  test "$(sudo sha256sum "$incident_dir/deployment-receipt.json" | awk '{print $1}')" = \
    "$released_receipt_digest"
  mutation_started=0
  rollback_committed=0
  incident_state_promoted=0
  incident_state_tmp=""
  incident_receipt_tmp=""

  # shellcheck disable=SC2329 # invoked by the EXIT trap below
  finish_emergency_restore() {
    rc=$?
    trap - EXIT
    set +e
    recovery_failed=0
    if [[ "$mutation_started" = 1 && "$rollback_committed" != 1 ]]; then
      printf 'emergency restore failed; putting released runtime back\n' >&2
      if [[ "$incident_state_promoted" = 1 ]]; then
        sudo rm -f -- "$incident_state_file" || recovery_failed=1
        sudo sync "$state_dir" || recovery_failed=1
      fi
      recovery_tmp=$(sudo mktemp \
        /opt/palimpsest-mcp/.palimpsest_mcp.py.recovery.XXXXXX)
      sudo install -o root -g root -m 0644 \
        "$incident_dir/released-runtime.py" "$recovery_tmp" || recovery_failed=1
      sudo mv -fT "$recovery_tmp" "$target_file" || recovery_failed=1
      sudo sync /opt/palimpsest-mcp || recovery_failed=1
      sudo systemctl restart "$service" || recovery_failed=1
      sudo systemctl is-active --quiet "$service" || recovery_failed=1
      sudo timeout --kill-after=5s 90s "$smoke" \
        --url http://127.0.0.1:8793/ --allow-http-loopback \
        --module "$target_file" --manifest "$released_manifest" || \
        recovery_failed=1
    fi
    if [[ -n "$incident_state_tmp" ]]; then
      sudo rm -f -- "$incident_state_tmp" || recovery_failed=1
    fi
    if [[ -n "$incident_receipt_tmp" ]]; then
      sudo rm -f -- "$incident_receipt_tmp" || recovery_failed=1
    fi
    rm -f -- "$previous_manifest" "$released_manifest"
    if [[ "$recovery_failed" != 0 ]]; then
      printf 'released runtime recovery also failed; escalate immediately\n' >&2
      exit 1
    fi
    exit "$rc"
  }
  trap finish_emergency_restore EXIT

  restore_tmp=$(sudo mktemp \
    /opt/palimpsest-mcp/.palimpsest_mcp.py.emergency.XXXXXX)
  sudo install -o root -g root -m 0644 "$backup_file" "$restore_tmp"
  test "$(sudo sha256sum "$restore_tmp" | awk '{print $1}')" = \
    "$previous_digest"
  mutation_started=1
  sudo mv -fT "$restore_tmp" "$target_file"
  sudo sync /opt/palimpsest-mcp
  sudo systemctl restart "$service"
  sudo systemctl is-active --quiet "$service"
  if [[ "$previous_source_sha" = "$legacy_sha" ]]; then
    sudo timeout --kill-after=5s 90s "$smoke" \
      --url http://127.0.0.1:8793/ --allow-http-loopback \
      --module "$target_file" --manifest "$previous_manifest" --basic
  else
    sudo timeout --kill-after=5s 90s "$smoke" \
      --url http://127.0.0.1:8793/ --allow-http-loopback \
      --module "$target_file" --manifest "$previous_manifest"
  fi
  test "$(sudo stat -c '%s' "$marker_file")" = 41
  sudo grep -Eq '^[0-9a-f]{40}$' "$marker_file"
  test "$(sudo head -n 1 "$marker_file")" = "$incident_target_sha"
  test "$(sudo sha256sum "$release_receipt" | awk '{print $1}')" = \
    "$released_receipt_digest"
  test "$(sudo sha256sum "$marker_file" | awk '{print $1}')" = \
    "$marker_digest"

  incident_receipt_tmp=$(sudo mktemp "$incident_dir/.incident-receipt.XXXXXX")
  sudo python3 - "$incident_receipt_tmp" "$incident_at" \
    "$incident_target_sha" "$previous_sha" "$previous_source_sha" \
    "$previous_digest" "$backup_basename" "$released_receipt_digest" \
    "$marker_digest" <<'PY'
import json
import os
import sys

(
    output,
    incident_at,
    released_sha,
    previous_sha,
    restored_source_sha,
    restored_digest,
    backup_basename,
    deployment_receipt_digest,
    marker_digest,
) = sys.argv[1:]
receipt = {
    "schema": "palimpsest.mcp-emergency-rollback-receipt.v1",
    "incident_at_utc": incident_at,
    "state": "incident-degraded",
    "preserved_deployed_sha": released_sha,
    "previous_deployed_sha": previous_sha or None,
    "restored_source_sha": restored_source_sha,
    "restored_runtime_sha256": restored_digest,
    "source_backup_basename": backup_basename,
    "deployment_receipt_sha256": deployment_receipt_digest,
    "preserved_marker_sha256": marker_digest,
}
with open(output, "w", encoding="utf-8") as handle:
    json.dump(receipt, handle, sort_keys=True, separators=(",", ":"))
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
PY
  sudo chown root:root "$incident_receipt_tmp"
  sudo chmod 0600 "$incident_receipt_tmp"
  incident_state_tmp=$(sudo mktemp "$state_dir/.incident-degraded.XXXXXX")
  sudo install -o root -g root -m 0600 \
    "$incident_receipt_tmp" "$incident_state_tmp"
  sudo sync "$incident_state_tmp"
  sudo mv -fT "$incident_state_tmp" "$incident_state_file"
  incident_state_tmp=""
  incident_state_promoted=1
  sudo sync "$state_dir"
  sudo mv -fT "$incident_receipt_tmp" "$incident_dir/incident-receipt.json"
  incident_receipt_tmp=""
  sudo sync "$incident_dir"
  test "$(sudo sha256sum "$incident_state_file" | awk '{print $1}')" = \
    "$(sudo sha256sum "$incident_dir/incident-receipt.json" | awk '{print $1}')"
  rollback_committed=1
  printf 'incident-degraded rollback complete; evidence: %s\n' "$incident_dir"
PALIMPSEST_EMERGENCY_ROLLBACK
)
```

The runtime now intentionally differs from `deployed-sha`, and the exact
incident receipt is also installed at the controller's root-owned
`incident-degraded.json` state pointer. Do not claim a verified deployment or
publish the Registry while this incident-degraded state exists. Repair it only
with a new signed `main` release under a monotonically higher Registry version;
the controller validates the pointer, marker, release receipt, backup, Git
source, and live bytes before deployment, then removes the pointer only at the
successful commit boundary. Record that repair SHA beside the historical
incident receipt, and never rewrite marker or receipt evidence to make the
states appear consistent.
