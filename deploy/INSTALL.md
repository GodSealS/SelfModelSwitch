# Thor installation checklist

This checklist is a review aid, not an installer. It does not format storage,
download models, replace `/etc/fstab`, or operate containers outside the
rendered manifest.

1. On Thor, run `python -m model_scheduler.deploy collect --output /tmp/thor-facts.json`.
   Verify JetPack, Docker, SSD UUID/filesystem, and the intended fixed
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
7. Run the hardware acceptance suite and retain its prompt-free report. Only
   all A01–A20 marked `passed` permits a production-ready declaration.

For recovery and rollback, see `docs/operations.md` in the release archive.
