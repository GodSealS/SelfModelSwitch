# AGX Thor Model Scheduler

This service is a single-process scheduler for four local llama.cpp model
servers. It owns residency, memory accounting, leases, and direct inference
routing. llama-swap is a narrow lifecycle controller only: inference traffic
never goes through its automatic router.

The HTTP listener is loopback-only. Public interfaces are `/live`, `/health`,
`/v1/models`, `/v1/chat/completions`, `/v1/embeddings`, `/v1/rerank`,
`/api/status`, `/api/models`, `POST /api/models/{model_id}/unload`, and
`POST /api/recover`.

`POST /api/recover` is a loopback-only administrative operation. It does not
blindly reopen inference after an SSD fault: it revalidates storage, invokes
the constrained control-plane recovery port, and then retries configured
preloads. A failed validation or recovery keeps admission closed.

## Local verification

Use Python 3.12 for releases. On a development machine, install the project
test dependencies and run:

```bash
python -m pytest tests -m 'not thor' -q
python -m ruff check .
python run.py --check-config
```

`config.yaml` is a lab-only example. It contains non-production identifiers and
model hashes; it is not a deployable Thor configuration.

## Deployment boundary

Create exactly one deployment input JSON, then render all derived files:

```bash
python -m model_scheduler.deploy render --input deploy-input.json --mode lab --output build/deploy
```

The renderer refuses placeholders, floating images, unsafe model paths,
unmeasured production budgets, and a non-empty output directory. It generates
the manifest, strict scheduler YAML, llama-swap YAML, mount-bound systemd units,
and an fstab *suggestion*. It never formats a disk, downloads a model, or edits
`/etc/fstab`.

For the full hand-operated installation, recovery, and rollback procedure, see
[`docs/operations.md`](docs/operations.md). A release archive can be made only
from a rendered deployment directory:

```bash
python scripts/build-release.py --deployment build/deploy --output build/release --release-id <id>
```

## Current release status

This repository is **not production-ready** until the fixed ARM64 llama-swap
release and real HTTP fixtures are recorded, Python 3.12/ARM64 locks install,
and all Thor acceptance scenarios complete with a real SSD, GPU, model hashes,
and `thor-report.json`. The repository deliberately does not contain device
credentials, model files, or a claim that those checks have passed.
