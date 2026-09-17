# SelfModelSwitch Agent Guide

## Scope and source of truth

- The current shipped service is described by the root `README.md`.
- `plan/` is the v3 design and task plan. It does not by itself mean that a
  feature has been implemented or accepted on hardware.
- Keep model-service work separate from `plan/video-analysis/`; video analysis
  is a separate future project.
- Do not add model weights, private keys, passwords, tokens, or device
  credentials to Git.

## Machines

| Role | Machine | Connection |
| --- | --- | --- |
| Development | This local workstation | Edit and commit here. |
| Debug / target | Jetson AGX Orin, user `jtzn` | `ssh jtzn@192.168.55.1` |

Use SSH public-key authentication for automated work. The password is an
out-of-band emergency credential and must never be placed in this repository,
in scripts, or in command history. A local key currently used for this target
is `~/.ssh/selfmodelswitch-target-agent`; invoke SSH with
`-i ~/.ssh/selfmodelswitch-target-agent -o IdentitiesOnly=yes`.

Before building, deploying, or interpreting a performance result, verify the
actual target hardware:

```bash
ssh -i ~/.ssh/selfmodelswitch-target-agent -o IdentitiesOnly=yes \
  jtzn@192.168.55.1 'hostname; uname -a; cat /etc/nv_tegra_release 2>/dev/null || true'
```

Stop and resolve a hardware mismatch before using device-specific CUDA
architectures, memory envelopes, or release evidence.

## Development and debug path

1. Write and review code on the development machine only. Do not edit tracked
   project files directly on the target machine.
2. Run local checks with the release Python version:

   ```bash
   python -m pytest tests -m 'not thor' -q
   python -m ruff check .
   python run.py --check-config
   ```

3. Inspect the diff for secrets and unrelated changes, then create an atomic
   commit on the development machine. Do not commit current user changes unless
   they are part of the requested work.

   ```bash
   git diff --check
   if git diff --cached | grep -Ei 'password|secret|api[_-]?key|token'; then
     echo 'Possible secret in staged diff; inspect before committing.' >&2
     exit 1
   fi
   git status --short
   git add <intended-files>
   git commit -m 'type: concise reason for the change'
   ```

4. On the target, update to the exact commit being tested. Verify the working
   tree is clean before and after updating; never discard target changes with
   `git reset --hard` or `git checkout --`.

   ```bash
   ssh -i ~/.ssh/selfmodelswitch-target-agent -o IdentitiesOnly=yes \
     jtzn@192.168.55.1 'cd <target-repository> && git status --short && git pull --ff-only && git rev-parse HEAD'
   ```

5. From the development machine, control the target over SSH to run the
   hardware test. Record the commit SHA, model path and SHA-256, CUDA/runtime
   version, command, exit code, elapsed time, peak memory, and stop/quiescence
   evidence with the test result.
6. Diagnose failures without modifying the target checkout by default: collect
   service logs, process state, GPU/thermal telemetry, mount state, and disk
   space first. Make the fix locally, commit it, update the target with a
   fast-forward-only pull, and retest the same scenario.

## Target safety and debugging

- Model media currently mounts at `/media/jtzn/sandisk-ext4`. Treat model
  weights as immutable test inputs; verify a file checksum before use.
- Never format disks, modify `/etc/fstab`, install drivers, or download model
  weights as part of normal test execution.
- Prefer a unique per-run evidence directory under
  `/home/jtzn/self-model-switch-evidence/` and keep failed-run evidence.
- Before a model test, capture `df -h`, mount details, `nvidia-smi` (when
  available), and `tegrastats`. After stopping, verify the spawned process has
  exited and compute is quiescent.
- For CUDA builds, match `CMAKE_CUDA_ARCHITECTURES` to the hardware verified at
  the start of the run; do not reuse a device-specific build across mismatched
  targets.
- Use `git pull --ff-only`, never a blind merge or reset, on the target.

## Completion criteria

For a code change, completion requires: local checks appropriate to the change,
a clean intended diff, an atomic local commit, target checkout at that commit,
and recorded target-test evidence when hardware behavior is in scope. A plan
document is not an acceptance result; follow the relevant M00--M07 acceptance
criteria in `plan/`.
