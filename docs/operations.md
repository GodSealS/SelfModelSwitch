# Operations runbook

This runbook is intentionally conservative: do not format a drive, pull an
unverified image, alter model files, or operate containers outside the manifest.

## Prepare deployment input

Collect target hardware facts read-only, then produce one JSON deployment input with
the actual device-tree identity, SSD UUID, ext4 filesystem, fixed llama-swap ARM64 release and SHA256,
image digest, model SHA256 values, and measured memory budgets. Production
rendering additionally needs the measured validation report. Render it into a
new, empty directory:

```bash
python -m model_scheduler.deploy render --input deploy-input.json --mode production --output build/deploy
```

Review `manifest.json`, `config.yaml`, `llama-swap.yaml`, both service units,
and `fstab.fragment`. The fragment is a suggestion only; merge it into
`/etc/fstab` manually after review. Never run a formatter from this project.
Production render copies its validated prompt-free hardware evidence as
`hardware-report.json` into the deployment directory; it must exactly match the
deployment input and the target device-tree identity. Retain that file in the
release archive with the manifest it validates.

## Install and start

Create the `model-scheduler` service user, place root-owned configuration and
manifest under `/etc/self-model-switch`, and install the release under
`/opt/self-model-switch/releases/<release-id>`. Point `current` at the verified
release atomically. Install the generated unit files, then verify them before
starting:

```bash
sudo systemd-analyze verify /etc/systemd/system/model-scheduler.service /etc/systemd/system/llama-swap.service
sudo systemctl daemon-reload
sudo systemctl start llama-swap.service model-scheduler.service
curl --fail http://127.0.0.1:8090/live
curl --fail http://127.0.0.1:8090/health
```

The second request is expected to fail until storage, control-plane recovery,
and pinned preload have completed. Inspect `journalctl -u model-scheduler -u
llama-swap` rather than declaring readiness from process existence.

Every llama-swap model start invokes the manifest-only runner, which rechecks
the configured SSD mount identity and all declared model file hashes before it
executes Docker. A failed check must remain a failed start; do not bypass it by
running `docker run` manually.

## Control socket and client group

The v2 config (`schema_version: 2`) serves local clients over the Unix socket
`control.socket_path` (default `/run/self-model-switch/control.sock`). Identity
comes from the kernel's peer credential of the accepted socket only — never
from a header or body (C08). The scheduler:

* creates the socket itself as `model-scheduler`, mode `0660`, group
  `control.peer_group` when configured, and refuses to start if the parent
  directory is world-accessible;
* admits only uids listed in `control.allowed_uids`; any other uid is closed
  before one byte of HTTP is parsed. One allowed uid is one `uid:<decimal>`
  owner; different processes of the same uid are the same owner (first
  release does not claim same-uid isolation);
* keeps `/internal/*` off the TCP listener — TCP control paths answer 404.

Deployment checklist: create the client group, add service and client users to
it (`getent group <peer_group>`), and note that `RuntimeDirectory` already
provisions `/run/self-model-switch` at `0750`. The chown only succeeds when
the service account can grant that group — membership, not root, is the
requirement; a failure is a startup refusal with the reason in the log, not a
silent permission downgrade. The two-real-UID gate case (S suite) runs on
Linux with a provisioned second uid; it skips elsewhere and never counts as
passed on a skip.

## SSD loss and recovery

When the SSD mount disappears, scheduler admission must fail before models are
reused. It clears leases, attempts to stop only manifest-managed models, and
then the mount-bound units stop. An unconfirmed stop remains ERROR and keeps
its memory budget; do not force it to UNLOADED.

After the physical device is restored and its UUID is verified:

```bash
sudo mount /mnt/model-ssd
curl --fail -X POST http://127.0.0.1:8090/api/recover
```

The recovery endpoint revalidates file metadata and SHA256 before it invokes
the constrained control recovery and configured preload. If the scheduler is
not running, start both mount-bound services instead; do not treat an empty
root-disk directory as a model mount.

## Control recovery and rollback

Only the root-owned, no-argument control helper may stop the control plane:

```bash
sudo -n /usr/local/libexec/sms-control-recover
```

It must hold an exclusive lock and emit only the documented JSON result. A
failure leaves models accounted as errors. To roll back, stop the services,
repointer `current` to the previously verified release, run `daemon-reload`,
then start services and recheck `/health`. Never roll back model files or modify
unrelated Docker containers as part of this procedure.
