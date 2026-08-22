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

3. Preserve the existing runtime until its current source commit is known. Then
   install the reviewed unit, confirm the diff, reload systemd, and run the local
   smoke. Do not write `deployed-sha` by hand; the first successful controlled
   release creates it.

   ```bash
   sudo install -o root -g root -m 0644 ops/systemd/palimpsest-mcp.service \
     /etc/systemd/system/palimpsest-mcp.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now palimpsest-mcp.service
   sudo /usr/local/libexec/palimpsest-mcp-smoke.py \
     --url http://127.0.0.1:8793/ --allow-http-loopback \
     --module /opt/palimpsest-mcp/palimpsest_mcp.py \
     --manifest /path/to/the-matching-reviewed/server.json
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

Only after the live SHA, version, discovery, and calls are verified should the
separate `registry-publish.yml` workflow be considered. A successful runtime
deployment is not an MCP Registry publication, and vice versa.
