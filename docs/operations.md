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

## Upgrade, rollback and blob metadata (P27)

1. Render the new release's units from the deployment inputs (see
   `deploy/INSTALL.md`); the model directory stays read-only and the control
   socket stays `0660` with the registered client group.
2. Take the blob-metadata backup **before** switching — a downgrade that cannot
   read the upgraded metadata restores this backup.
3. Run the switch sequence (`deploy.switch_release`): admission closed → drained
   → instances proven stopped → preflight → `current` switch → start → smoke.
   Anything unproven aborts before `current` moves.
4. On a failed smoke after the switch, roll back (`deploy.rollback_release`) to
   the accepted release recorded in the switch result; without an accepted older
   candidate the site stops for repair.
5. Record the resulting systemd/PID/socket state (unit states, `current` target,
   socket owner/mode) with the release id.

## Evidence index (target device)

All acceptance material lives under `/home/jtzn/self-model-switch-evidence/` on the
target. The index lists what each directory contains and how to re-verify it
**offline**. No credentials, tokens or device keys are recorded here — none are
needed to re-verify, and none may be added.

| Directory | Contents | Offline re-verification |
|---|---|---|
| `p21-calibration/` | `facts.json` (16 C09 facts with provenance), `scheduler-v2.yaml`, `maintenance.json`, `calibration-final/{measurements.json, raw/run1..3/}` | re-run `acceptance calibrate --from-evidence …/p21-calibration/calibration-final` (recomputes §5 criteria and the C02 bound from the preserved rows; it stays `blocked` for material without MemFree/window) |
| `p22/` | `source-a.tar.gz`/`source-b.tar.gz` (identical deterministic archives), `policy.json`, `fixtures.json`, `fixtures/`, `scheduler-v2-claims-measured.yaml` | `acceptance source --root <checkout> --output <new tar>` then compare sha256; `acceptance candidate …` (refuses on unproven material, never writes a partial candidate) |
| `p23/` | M00-envelope fixtures (`fixtures/`) with their boundary dimensions | `pytest tests/test_backend_cases.py -q`; compare fixture sha256 against the recorded ones |
| `p25/` | 26-case synthetic evidence set and its tampered copy | `acceptance verify --candidate <c.json> --evidence <dir>` → 0 clean, 3 tampered/missing |
| `p26/` | rendered v3 deployment (`deploy/manifest.json` + service units) and the layer-1 result | `model_scheduler.deploy preflight --manifest …/manifest.json` (layer 1, loads nothing) |
| `p27/` | rendered service units + `service-facts.json` | `systemd-analyze verify <units>` (exit 0) |
| `p29/` | `facts.json`, `source.tar.gz` (sha256 `43708bc7…`), `source.log` — the P29 prerequisite audit | re-run `collect`, `source`, `candidate`, `run --layers B` and compare exit codes (2 / 3 by design until the blockers clear) |
| `p29-s-layer-20260920T040254Z/` | rebuilt `source-s3.tar.gz`/`candidate-s3.json`, `inventory.json` + `context.txt` (S01's explicit migration input and its provenance), `run-s3/` (S layer 6/6), `run-b21/` (kept failure: cancel/stop without raw rows), `run-b22/` (B layer 12/12), `final-sb/` (merged 18 attempts / 14 cases) | `acceptance verify --candidate …/candidate-s3.json --evidence …/final-sb` → exit 2 only for `O01—O06`; recompute any case offline with `acceptance.evaluator.evaluate_case` from its `case.json` + `samples/` |

Re-verification never starts a model, never talks to Docker and never opens a
socket: `acceptance verify` and the P26 preflight are pure file-and-hash checks.
