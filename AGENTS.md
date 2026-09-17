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

## Git management and synchronization ownership

- Use the `cs-git-workflow` skill to execute Git management and synchronization
  between the development and test machines. Read its `SKILL.md` before
  performing this workflow; this guide's target-safety rules take precedence.
- The delivery path is: development branch -> atomic local commit -> push to
  the shared remote -> target fast-forward pull -> matching commit SHA -> test.
  A local commit alone does not make code available to the target.
- Only the development machine creates commits, resolves conflicts, merges,
  and pushes. The target is a checkout for testing; do not commit or push there,
  copy tracked files over SSH, or use uncommitted files as release evidence.
- Before work, inspect `git status --short`, `git branch -vv`, and
  `git remote -v`. Fetch with `git fetch origin`, then inspect the upstream
  ahead/behind history. Preserve existing user changes and review unpublished
  commits before including them in a push. Never use `git add .` indiscriminately.
- Use short-lived `feature/`, `fix/`, `chore/`, or `refactor/` branches. Create
  one with `git switch -c <branch>` from the intended base; use a separate
  worktree when existing changes would interfere. Do not switch or stash away
  unrelated user work automatically. Set upstream tracking on the first push.
- Confirm the shared remote URL, delivery branch, and absolute target checkout
  path before synchronization. `origin` currently points to
  `https://github.com/GodSealS/SelfModelSwitch.git`; verify it on both machines.
  Use configured Git authentication, never credentials embedded in URLs or
  commands. The SSH key for reaching the target does not grant GitHub access.
- For a first-time target checkout, clone the verified remote into an unused
  directory with `git clone --branch <branch> --single-branch <remote-url>
  <target-repository>`. Do not clone over an existing checkout. If Git access
  fails, resolve remote authentication/connectivity before continuing.
- Stop on dirty target state, divergent history, a rejected push, or a SHA
  mismatch. Inspect `git status`, `git diff`, and `git log --oneline --graph
  --decorate --all`; resolve code conflicts locally, rerun checks, and push a
  new commit. Never force-push, reset, clean, or automatically stash target work.
- Merge accepted changes on the development side using the repository's review
  process, then push and synchronize the resulting commit again. Roll back via
  a reviewed `git revert <commit>` locally, followed by checks, commit/push,
  target synchronization, and retest; preserve the original failure evidence.

## Development and debug path

1. Write and review code on the development machine only. Do not edit tracked
   project files directly on the target machine.
2. Run local checks with the release Python version (Python 3.12). For a
   documentation-only change, review the instructions and run `git diff --check`;
   the runtime suite is required when code or configuration behavior changes.

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
   git status --short
   git diff --cached
   # If unrelated changes are already staged, isolate this work before proceeding.
   git add -- <intended-files>
   git diff --cached --check
   git diff --cached
   if git diff --cached | grep -Ei 'password|secret|api[_-]?key|token'; then
     echo 'Possible secret in staged diff; inspect before committing.' >&2
     exit 1
   fi
   git commit -m 'type: concise reason for the change'
   ```

   The keyword scan is a review aid: inspect matches, distinguish documentation
   from actual credentials, and remove any real secrets before committing.

4. Push the reviewed delivery branch from the development machine. Review all
   commits being published, including any that predate this task. Run the
   following in Bash after local checks and commit review; stop on any failure:

   ```bash
   set -euo pipefail
   sync_branch=$(git symbolic-ref --quiet --short HEAD)
   expected_sha=$(git rev-parse HEAD)
   git fetch origin
   # For an existing remote branch, review origin/<branch>..HEAD before pushing.
   # For a new branch, review the commits since its intended base.
   git push --set-upstream origin "${sync_branch}:${sync_branch}"
   remote_sha=$(git ls-remote --exit-code origin "refs/heads/${sync_branch}" | awk '{print $1}')
   test "$remote_sha" = "$expected_sha"
   printf 'branch=%s\nexpected_sha=%s\n' "$sync_branch" "$expected_sha"
   ```

5. On the target, synchronize the same branch and verify the exact commit before
   testing. Replace the three placeholders below with the confirmed checkout
   path, pushed branch, and full SHA from step 4. Check the remote URL before
   running this block. Its clean-tree guards include untracked files; keep test
   output outside the checkout. A changing remote branch may cause the final
   SHA check to fail; do not test until the intended commit is resolved.

   ```bash
   ssh -i ~/.ssh/selfmodelswitch-target-agent -o IdentitiesOnly=yes \
     jtzn@192.168.55.1 'bash -se' <<'TARGET_SYNC'
   set -euo pipefail
   target_repository='<absolute-target-repository>'
   sync_branch='<pushed-branch>'
   expected_sha='<full-commit-sha>'
   cd "$target_repository"
   git status --short
   test -z "$(git status --porcelain --untracked-files=all)"
   git fetch origin "refs/heads/${sync_branch}:refs/remotes/origin/${sync_branch}"
   git show-ref --verify "refs/remotes/origin/${sync_branch}"
   if git show-ref --verify --quiet "refs/heads/${sync_branch}"; then
     git switch "$sync_branch"
   else
     git switch --track -c "$sync_branch" "origin/$sync_branch"
   fi
   git branch --set-upstream-to="origin/$sync_branch" "$sync_branch"
   git pull --ff-only origin "$sync_branch"
   actual_sha=$(git rev-parse HEAD)
   test "$actual_sha" = "$expected_sha"
   test -z "$(git status --porcelain --untracked-files=all)"
   printf 'verified_target_sha=%s\n' "$actual_sha"
   TARGET_SYNC
   ```

6. From the development machine, control the target over SSH to run the
   hardware test. Record the commit SHA, model path and SHA-256, CUDA/runtime
   version, command, exit code, elapsed time, peak memory, and stop/quiescence
   evidence with the test result. Also record the remote URL, branch, local SHA,
   verified target SHA, and target clean-tree status before and after testing.
7. Diagnose failures without modifying the target checkout by default: collect
   service logs, process state, GPU/thermal telemetry, mount state, and disk
   space first. Make the fix locally, check and commit it, push the branch, repeat
   step 5's guarded fast-forward synchronization, and retest the same scenario.

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
a clean intended diff, an atomic local commit, a verified push to the shared
remote, target checkout at that same commit, and recorded target-test evidence
when hardware behavior is in scope. For documentation-only tasks, local review
and an atomic commit suffice unless publishing or target synchronization was
requested; explicitly report whether push and target synchronization occurred. A plan
document is not an acceptance result; follow the relevant M00--M07 acceptance
criteria in `plan/`.
