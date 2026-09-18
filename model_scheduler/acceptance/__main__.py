"""`python -m model_scheduler.acceptance` — the M06 commands (plan/08 §5).

Implemented here: `collect` (P21) and `calibrate` (P21). The candidate/run/
merge/verify commands belong to P22—P25 and are refused explicitly until they
exist, so no documentation can claim a command that is not delivered.

Uniform exit codes: 0 success, 2 input/material error, 3 execution/semantic
failure. Outputs are never overwritten, and no input path is searched for.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import EXIT_FAILED, EXIT_INPUT, EXIT_OK
from .collect import FactsError, collect_facts

_NOT_YET = ("run", "merge", "verify")


class InputError(RuntimeError):
    """A missing or ill-formed input: exit 2, with the reason on stderr."""


def require_fresh_output(path: Path) -> None:
    """Every command refuses a non-empty output; an empty file is unused."""
    if path.exists() and (not path.is_file() or path.stat().st_size > 0):
        raise InputError(f"the output already exists and is not empty: {path}")


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _disk_paths(args) -> tuple[str, str]:
    model = args.model_disk
    scratch = args.scratch_disk
    if args.config is not None:
        from ..config import ConfigError, load_config

        try:
            config = load_config(args.config)
        except ConfigError as exc:
            raise InputError(f"cannot load {args.config}: {exc}") from exc
        if getattr(config, "schema_version", None) != 2:
            raise InputError("the config must be schema v2 (schema v1 disks are not a v2 deployment)")
        model = model or str(config.storage.model_directory)
        scratch = scratch or str(config.blobs.root)
    if not model or not scratch:
        raise InputError("--model-disk and --scratch-disk (or --config) are required: "
                         "disk identity is read on site, never guessed")
    return str(model), str(scratch)


def _collect(args) -> int:
    require_fresh_output(args.output)
    model_disk, scratch_disk = _disk_paths(args)
    facts = collect_facts(model_disk=model_disk, scratch_disk=scratch_disk)
    document = facts.document()
    from ..evidence_contracts import parse_device_fact, parse_runtime_stack

    parse_device_fact(document["device"])  # the tool refuses to emit facts it cannot re-parse
    parse_runtime_stack(document["runtime_stack"])
    write_json(args.output, document)
    print(f"facts written to {args.output}")
    return EXIT_OK


def _calibrate(args) -> int:
    from .calibrate import CalibrationError, run_calibration

    require_fresh_output(args.output / "measurements.json")
    try:
        summary = run_calibration(config_path=args.config, facts_path=args.facts,
                                  maintenance_path=args.maintenance, budget_bytes=args.budget_bytes,
                                  runs=args.runs, output=args.output, from_evidence=getattr(args, "from_evidence", None))
    except CalibrationError as exc:
        print(f"calibrate: {exc}", file=sys.stderr)
        return EXIT_FAILED if exc.semantic else EXIT_INPUT
    print(f"calibration written to {args.output}: {summary}")
    # A blocked verdict is a semantic failure (C02): the material is kept, the
    # exit code still says the calibration did not pass.
    return EXIT_OK if summary.get("verdict") == "passed" else EXIT_FAILED


def _source(args) -> int:
    from .candidate import build_source_archive

    require_fresh_output(args.output)
    result = build_source_archive(root=args.root, output=args.output)
    print(f"source archive written to {result['output']}: {result['sha256']}")
    if result["excluded"]:
        print(f"excluded from the archive: {', '.join(result['excluded'])}", file=sys.stderr)
    return EXIT_OK


def _candidate(args) -> int:
    from .candidate import CandidateError, build_candidate

    require_fresh_output(args.output)
    try:
        result = build_candidate(config_path=args.config, facts_path=args.facts,
                                 measurements_dir=args.measurements, policy_path=args.policy,
                                 fixtures_path=args.fixtures, source_path=args.source,
                                 deployment_id=args.deployment_id, output=args.output)
    except CandidateError as exc:
        print(f"candidate: {exc}", file=sys.stderr)
        return EXIT_INPUT if exc.input_error else EXIT_FAILED
    print(json.dumps(result, indent=2, sort_keys=True))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m model_scheduler.acceptance")
    sub = parser.add_subparsers(dest="command", required=True)

    collect_parser = sub.add_parser("collect", help="read the C09 site facts; never starts a model")
    collect_parser.add_argument("--output", type=Path, required=True)
    collect_parser.add_argument("--config", type=Path, help="schema v2 config: model/scratch disk locations")
    collect_parser.add_argument("--model-disk", help="explicit model directory whose filesystem UUID is recorded")
    collect_parser.add_argument("--scratch-disk", help="explicit scratch path whose filesystem UUID is recorded")

    calibrate_parser = sub.add_parser("calibrate", help="controlled calibration: temporary budget, exclusive, sampled")
    calibrate_parser.add_argument("--config", type=Path, required=True)
    calibrate_parser.add_argument("--facts", type=Path, required=True)
    calibrate_parser.add_argument("--maintenance", type=Path, required=True)
    calibrate_parser.add_argument("--budget-bytes", type=int, required=True)
    calibrate_parser.add_argument("--runs", type=int, default=3)
    calibrate_parser.add_argument("--output", type=Path, required=True)
    calibrate_parser.add_argument("--from-evidence", type=Path,
                                  help="recompute from preserved raw sampling material instead of a fresh run")

    source_parser = sub.add_parser("source", help="deterministic allowlisted source archive; no deployment needed")
    source_parser.add_argument("--root", type=Path, required=True)
    source_parser.add_argument("--output", type=Path, required=True)

    candidate_parser = sub.add_parser("candidate", help="freeze facts/measurements/policy/fixtures/source into one body")
    candidate_parser.add_argument("--config", type=Path, required=True)
    candidate_parser.add_argument("--facts", type=Path, required=True)
    candidate_parser.add_argument("--measurements", type=Path, required=True)
    candidate_parser.add_argument("--policy", type=Path, required=True)
    candidate_parser.add_argument("--fixtures", type=Path, required=True)
    candidate_parser.add_argument("--source", type=Path, required=True)
    candidate_parser.add_argument("--deployment-id", required=True,
                                  help="deployment identity; it is an explicit input and is never derived or guessed")
    candidate_parser.add_argument("--output", type=Path, required=True)

    for name in _NOT_YET:
        stub = sub.add_parser(name, help=f"{name} is delivered by a later plan task and refuses to pretend")
        stub.add_argument("rest", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "collect":
            return _collect(args)
        if args.command == "calibrate":
            return _calibrate(args)
        if args.command == "source":
            return _source(args)
        if args.command == "candidate":
            return _candidate(args)
        raise InputError(f"`{args.command}` is not implemented yet: the plan assigns it to a later task")
    except (InputError, FactsError) as exc:
        print(f"{args.command}: {exc}", file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
