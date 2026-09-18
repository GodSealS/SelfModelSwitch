"""Acceptance tooling (M06): site facts, calibration, candidates and runs.

Uniform CLI exit codes (plan/08-execution-plan.md §5):

* 0 — success
* 2 — input or material error (missing/ill-formed input, non-empty output)
* 3 — execution or semantic failure (a live precondition failed, an
  unprovable stop, a measurement that does not meet the acceptance criteria)

Every command refuses to overwrite a non-empty output, validates its inputs
against the published schemas, and never searches for "the latest" material:
paths are explicit arguments of this run.
"""
from __future__ import annotations

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_FAILED = 3
