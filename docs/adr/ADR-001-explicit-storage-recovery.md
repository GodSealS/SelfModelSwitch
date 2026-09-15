# ADR-001: Require explicit storage recovery

## Status

Accepted.

## Context

An SSD remount does not prove that the expected UUID, filesystem, model files,
and SHA-256 values are valid. Automatically reopening admission after a mount
event could serve root-disk files or race a late llama-swap model process.

## Decision

Expose loopback-only `POST /api/recover`. The scheduler verifies the injected
storage guard, runs the injected constrained control-plane recovery port, then
clears its storage admission block and retries configured preload. Any failed
step leaves the block in place.

## Consequences

Operators must deliberately invoke recovery after an SSD fault. This adds one
step, but prevents a mount event from being mistaken for proof of safe model
residency. The endpoint follows the existing JSON error envelope and never
accepts arbitrary command or path arguments.

## Alternatives considered

- Automatically recover when the watchdog next reports a mount: rejected
  because a transient or wrong mount is insufficient proof.
- Expose a generic shell/control endpoint: rejected because it would widen the
  scheduler's authority beyond the fixed recovery helper.
