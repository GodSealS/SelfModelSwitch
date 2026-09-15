# ADR-002: Bind production evidence to the target device

## Status

Accepted

## Context

The original production gate referred to AGX Thor by name, but a reachable
deployment target can report a different NVIDIA AGX model. Model measurements
and a successful soak on one device cannot establish readiness on another.

## Decision

Deployment input, rendered manifests, and hardware reports use schema version
2 and carry an exact `hardware` object: a device-tree model string plus the
ordered compatible strings. Production preflight reads the target device tree
and fails before service or container actions unless it exactly matches the
manifest. Evidence and release artifacts are named `hardware-report.json`.

## Consequences

Schema-v1 reports and unbound Thor-named evidence cannot satisfy production
rendering. This prevents an Orin result from being represented as a Thor
result, at the cost of requiring fresh device-bound measurements after any
hardware target change.
