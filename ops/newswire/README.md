# Hetzner evidence-wire intake

The base timer collects the closed, reviewed RSS/Atom registry every thirty
minutes; production installs the reviewed five-minute override. The always-on
Hetzner node retains a bounded private revision ledger, builds event analysis,
and triggers the direct Railway publisher. GitHub Actions is not part of this
publication path. An independent fifteen-minute publisher timer reconciles the
two live origins even if the normal success trigger is interrupted.

The collector stores feed metadata and links only. It never downloads article
bodies, never executes fetched content, and treats each source as a receipt—not
as proof. A failed or stale source remains visible in coverage. A run where no
source is fresh preserves the prior latest document and ledger, writes an
atomic per-attempt receipt to
`/var/lib/palimpsest/newswire/newswire-status.json`, and exits non-zero. A
successful receipt records the fresh-source count and binds the run to the
exact latest-document timestamp and SHA-256. This keeps “the collector ran”
separate from “current evidence exists.”

The live timer file is `/var/lib/palimpsest/newswire/newswire-latest.json`.
Fleet collectors and the site builder resolve that path before the repo
`readings/newswire-latest.json` publish freeze. A successful wire refresh
starts `palimpsest-event-analysis-live.service`, which writes
`/var/lib/palimpsest/newswire/event-analysis-latest.json` from that same file.
Missing official-first-seen, deletion-ledger, undertext, or newsroom readings
cause those layers to abstain.

During the 2026-08-30 direct-publication transition, the protected canonical
checkout intentionally remains on its predecessor. Live event analysis must
therefore enter through `/usr/local/sbin/palimpsest-event-analysis-live`. The
wrapper accepts only the closed root-controlled publication-base pin with
SHA-256
`255e17340a38bfcc5ead6ed4a33a8f50f23da8655ca396cd99fbe1980ebd1e97` and
target `4957595735fd86fa57217309749961e1a1e0f05d`. It proves that commit is a
local ancestor of the reviewed `origin/main`, creates a bounded private
`git archive` materialization, and runs the analyzer from those exact bytes.
It never fetches, changes a ref, checks out a tree, or writes Git metadata.
The materialization is removed after every success or failure.

The Common Crawl context timer can fire and still leave
`archive-news-context.json` untouched: `ExecStartPre` `cmp -s REVISION
/etc/palimpsest/deployed-commit` is fail-closed. The unit now stamps
`archive-news-context.last-attempt.json` with `revision_pin` before that abort.
Until `anomaly_state` leaves `warming_up`, live analysis publishes that state
and no MAD score.

The scheduled command holds an exclusive lease on the persistent mode-0600
`newswire.lock` for the complete attempt, including the in-flight receipt and
terminal publication. The node backup takes a shared lease on the same inode,
so it cannot mix latest, lineage, and status files from different attempts.

The systemd unit retries a failed attempt after two minutes, but permits only
three starts in ten minutes. The five-minute production timer remains the
long-term recovery boundary after that bounded retry budget is exhausted.

## Install

For this incident, install the wrapper, unit, and exact Railway success-trigger
drop-in from a separately verified clean release worktree—not from the stale
protected checkout. Bind every staged byte to an explicitly reviewed commit
before installation. `git hash-object` does not write an object unless `-w` is
passed. These steps do not change `/home/palimpsest/palimpsest` or
`/etc/palimpsest/deployed-commit`:

```bash
reviewed_release=/srv/palimpsest/reviewed-event-analysis-runtime
reviewed_runtime_commit="${PALIMPSEST_REVIEWED_RUNTIME_COMMIT:?set the exact reviewed 40-character fix commit}"
case "$reviewed_runtime_commit" in
  *[!0-9a-f]*|'') exit 1 ;;
esac
test "${#reviewed_runtime_commit}" -eq 40
test "$(git -C "$reviewed_release" rev-parse --verify 'HEAD^{commit}')" = \
  "$reviewed_runtime_commit"
test -z "$(git -C "$reviewed_release" status --porcelain=v1 --untracked-files=no)"
for reviewed_path in \
  ops/newswire/palimpsest-event-analysis-live \
  ops/systemd/palimpsest-event-analysis-live.service \
  ops/systemd/palimpsest-event-analysis-live.railway-publish.conf; do
  expected_blob="$(git -C "$reviewed_release" rev-parse --verify \
    "$reviewed_runtime_commit:$reviewed_path")"
  actual_blob="$(git -C "$reviewed_release" hash-object \
    "$reviewed_release/$reviewed_path")"
  test "$actual_blob" = "$expected_blob"
done
sudo install -o root -g root -m 0755 \
  "$reviewed_release/ops/newswire/palimpsest-event-analysis-live" \
  /usr/local/sbin/palimpsest-event-analysis-live
sudo install -o root -g root -m 0644 \
  "$reviewed_release/ops/systemd/palimpsest-event-analysis-live.service" \
  /etc/systemd/system/palimpsest-event-analysis-live.service
sudo install -d -o root -g root -m 0755 \
  /etc/systemd/system/palimpsest-event-analysis-live.service.d
sudo install -o root -g root -m 0644 \
  "$reviewed_release/ops/systemd/palimpsest-event-analysis-live.railway-publish.conf" \
  /etc/systemd/system/palimpsest-event-analysis-live.service.d/90-railway-publish.conf
sudo cmp "$reviewed_release/ops/newswire/palimpsest-event-analysis-live" \
  /usr/local/sbin/palimpsest-event-analysis-live
sudo cmp "$reviewed_release/ops/systemd/palimpsest-event-analysis-live.service" \
  /etc/systemd/system/palimpsest-event-analysis-live.service
sudo cmp \
  "$reviewed_release/ops/systemd/palimpsest-event-analysis-live.railway-publish.conf" \
  /etc/systemd/system/palimpsest-event-analysis-live.service.d/90-railway-publish.conf
sudo systemd-analyze verify \
  /etc/systemd/system/palimpsest-event-analysis-live.service
sudo systemctl daemon-reload
event_analysis_on_success="$(systemctl show --property=OnSuccess --value \
  palimpsest-event-analysis-live.service)"
test "$event_analysis_on_success" = "palimpsest-railway-publish.service"
```

The wrapper also requires the reviewed pin at
`/etc/palimpsest/railway-publication-base.json` to be a single regular
`root:palimpsest` file with mode `0640`; a full, unmodified local repository
with the reviewed HTTPS `origin`; and the target commit reachable from the
local `refs/remotes/origin/main`. It fails closed rather than fetching or
repairing any prerequisite. The incident lane is also self-retiring: both the
root-owned mode-`0644` `/etc/palimpsest/deployed-commit` file (exactly the
40-character predecessor SHA plus LF) and canonical repository `HEAD` must
still equal `b22d809bca5ca8aed8255e8a89a06a88dc9cbcb9`.

The analyzer receives only an exclusive mode-`0600` stage path. The wrapper
admits a bounded, nonempty, closed-schema bundle only when its wire path and
clock match the input and it contains at least one event. It then changes the
stage to mode `0640`, fsyncs it, atomically replaces the public latest file,
and fsyncs the containing directory. Analyzer errors or invalid partial output
remove only that owned stage and preserve the prior latest file.

```bash
sudo install -d -o palimpsest -g palimpsest -m 0750 /var/lib/palimpsest/newswire
sudo install -o palimpsest -g palimpsest -m 0600 /dev/null \
  /var/lib/palimpsest/newswire/newswire.lock
sudo install -d -o root -g root -m 0755 /etc/palimpsest
sudo install -o root -g root -m 0644 \
  /home/palimpsest/palimpsest/ops/systemd/palimpsest-evidence-wire.service \
  /etc/systemd/system/palimpsest-evidence-wire.service
sudo install -o root -g root -m 0644 \
  /home/palimpsest/palimpsest/ops/systemd/palimpsest-evidence-wire.timer \
  /etc/systemd/system/palimpsest-evidence-wire.timer
sudo install -d -o root -g root -m 0755 \
  /etc/systemd/system/palimpsest-evidence-wire.timer.d
sudo install -o root -g root -m 0644 \
  /home/palimpsest/palimpsest/ops/systemd/palimpsest-evidence-wire.5-minute-live.conf \
  /etc/systemd/system/palimpsest-evidence-wire.timer.d/90-five-minute-live.conf
sudo systemd-analyze verify \
  /etc/systemd/system/palimpsest-evidence-wire.service \
  /etc/systemd/system/palimpsest-evidence-wire.timer
sudo systemctl daemon-reload
sudo systemctl enable --now palimpsest-evidence-wire.timer
sudo systemctl start palimpsest-evidence-wire.service
```

An optional `/etc/palimpsest/newswire.env` may define `PALIMPSEST_PROXY` for a
reviewed outbound proxy. Do not put a GitHub token or website write credential
there. Keeping collection and public publication separate limits what a
compromised feed or node can change.

Inspect without exposing configuration:

```bash
systemctl status palimpsest-evidence-wire.service --no-pager
systemctl list-timers palimpsest-evidence-wire.timer --no-pager
sudo journalctl -u palimpsest-evidence-wire.service -n 100 --no-pager
sudo -u palimpsest python3 -m json.tool \
  /var/lib/palimpsest/newswire/newswire-latest.json >/dev/null
sudo -u palimpsest python3 -m json.tool \
  /var/lib/palimpsest/newswire/newswire-status.json >/dev/null
```

Stop immediately with:

```bash
sudo systemctl disable --now palimpsest-evidence-wire.timer
sudo systemctl stop palimpsest-evidence-wire.service
```
