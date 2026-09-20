# AGX installation checklist

This checklist is a review aid, not an installer. It does not format storage,
download models, replace `/etc/fstab`, or operate containers outside the
rendered manifest.

1. On the target AGX device, run `python -m model_scheduler.deploy collect --output /tmp/hardware-facts.json`.
   Verify the device-tree hardware identity, JetPack, Docker, SSD UUID/filesystem, and the intended fixed
   llama-swap ARM64 release before creating deployment input.
2. Render into a new empty directory. Production mode requires the real model
   measurements report and rejects placeholder hashes.
3. Review `fstab.fragment` and merge it manually only after confirming that the
   UUID points at the intended ext4 SSD. Do not execute formatting commands.
4. Install root-owned manifest/configuration and the two generated unit files;
   install `control-recover.py` as root-owned
   `/usr/local/libexec/sms-control-recover` with a sudoers rule that permits
   only its no-argument invocation. Install the tracked
   `deploy/sudoers.model-scheduler` template as
   `/etc/sudoers.d/model-scheduler` with mode `0440`, then validate it with
   `visudo -cf /etc/sudoers.d/model-scheduler`; do not replace its explicit
   empty-argument matcher with a wildcard.
5. Create a release-specific Python 3.12 virtual environment from the verified
   hash lock, place it under `/opt/self-model-switch/releases/<id>`, and update
   `current` only after preflight succeeds.
6. Run `systemd-analyze verify` on generated unit files, then daemon-reload and
   start llama-swap followed by model-scheduler. `/live` may be 200 while
   `/health` remains 503 during recovery; do not treat that as ready.
   Each model start revalidates the configured SSD and all model hashes before
   Docker is invoked; never bypass this with a manual container command.
7. Run the hardware acceptance suite and retain its prompt-free, device-bound report. Only
   all A01–A20 marked `passed` permits a production-ready declaration.

For recovery and rollback, see `docs/operations.md` in the release archive.

## Service units, switching and rollback (P27)

Render the two units and the sudoers rule from **explicit deployment inputs**
(`ServiceInputs`): service user/group, client UID/group, control socket path with
mode `0660`, blob root + disk UUID + quota, the model-disk mount unit and the
read-only model directory. Nothing falls back to a template default, the model
directory is mounted read-only, and no video/media unit is ever generated (the
renderer refuses a `video_unit=True` input or a stray `video*.service.in`
template).

```python
from pathlib import Path
from model_scheduler.deploy import ServiceInputs, render_service_units, switch_release, rollback_release

render_service_units(output=Path("build/units"), inputs=ServiceInputs(
    service_user="model-scheduler", service_group="model-scheduler",
    client_uid=1003, client_group="sms-client",
    socket_path="/run/self-model-switch/control.sock",
    model_mount="/media/<disk>", model_directory="/media/<disk>/models",
    mount_unit="media-...mount", blob_root="/var/lib/self-model-switch/blobs",
    blob_disk_uuid="<uuid>", blob_quota_bytes=17179869184,
    release_root="/opt/self-model-switch/releases",
    config_path="/etc/self-model-switch/config.yaml",
    swap_config_path="/etc/self-model-switch/llama-swap.yaml",
    allow_cidr="192.168.55.0/24",   # the compat port opens to this network only
    scheduler_port=8090))           # the port `server.port` in the v2 config uses
```

**Reaching the service from another machine (P27).** The rendered
`model-scheduler.service` carries `SMS_ALLOW_CIDR`/`SMS_SCHEDULER_PORT` and runs
`deploy/open-firewall.sh` as root before the scheduler starts. The rule is
idempotent (a restart never stacks duplicates) and it only opens the
compatibility port; the control plane stays on its Unix socket and is never
reachable over TCP. Review the rule before installing:
`iptables -S INPUT | grep -- '--dport 8090'`. Opening `0.0.0.0/0` needs
`allow_public=True` on purpose, and the compatibility surface has no
authentication of its own: put a reverse proxy (Basic Auth/mTLS) in front of it
before it leaves a trusted network. To undo the rule by hand:
`deploy/open-firewall.sh --cidr <cidr> --port <port> --remove`.

Switch order (enforced by `switch_release`): close admission → drain (queue,
leases, sessions all zero) → **prove the old instances stopped** → preflight the
new release → move `current` → start → smoke → reopen admission. A failure before
the `current` move leaves the running release untouched; a failure after it is
returned with `repair_required` plus the previous release to roll back to.

Rollback (`rollback_release`): restore the accepted older release and config; if
the downgrade cannot read the upgraded blob metadata, restore the backup taken
**before** the upgrade; with no accepted older candidate the site stops for a
manual repair instead of guessing one. Units are reviewed with
`systemd-analyze verify` before installation.
