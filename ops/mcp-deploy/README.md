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
     set -euo pipefail
     readonly legacy_sha=2a80981815680006f3daf7caf503a125d6299c3c
     legacy_manifest=$(mktemp /tmp/palimpsest-mcp-1.8.1-server.XXXXXX)
     trap 'rm -f -- "$legacy_manifest"' EXIT

     test "$(sudo git --git-dir=/var/lib/palimpsest-mcp-deploy/repository.git \
       rev-parse --verify "${legacy_sha}^{commit}")" = "$legacy_sha"
     sudo git --git-dir=/var/lib/palimpsest-mcp-deploy/repository.git \
       show "${legacy_sha}:server.json" >"$legacy_manifest"
     chmod 0444 "$legacy_manifest"
     test "$(python3 -c \
       'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' \
       "$legacy_manifest")" = 1.8.1
     sudo timeout --kill-after=5s 90s \
       runuser --user palimpsest-mcp-verify -- \
       env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
       /usr/local/libexec/palimpsest-mcp-smoke.py \
       --url http://127.0.0.1:8793/ --allow-http-loopback \
       --module /opt/palimpsest-mcp/palimpsest_mcp.py \
       --manifest "$legacy_manifest" --basic

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
   )
   ```

4. Put a dedicated Ed25519 public key in root's `authorized_keys`, restricted to
   the root-owned controller and with every interactive/forwarding feature off:

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

## Release transaction

The target must be a reviewed merge on `main` whose GitHub API verification is
`verified: true`, not an unsigned feature-branch commit.

```bash
gh workflow run deploy-mcp.yml -f target_sha=<40-character-reviewed-main-SHA>
gh run watch --exit-status
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
  -f target_sha=<40-character-current-main-SHA> \
  -f deploy_run_id=<successful-deploy-mcp-run-id>
```

The Registry workflow checks out that exact current `origin/main`, requires a
clean tree, validates the selected successful deployment run and its receipt,
repeats the public smoke, publishes, then polls the official Registry until the
latest active record exactly matches `server.json`. A successful runtime
deployment is not an MCP Registry publication, and vice versa.
