"""Evidence-contract tests (M01/P03, C09).

Covers the one-directional evidence chain: the candidate digest is reproducible
from the canonical body alone (defaults expanded, no self digest, no report, no
generation time), artifact paths cannot escape or duplicate, the policy may be
stricter but never looser than 06-acceptance, and a report maps every required
case to exactly one traceable final attempt while keeping failed attempts.
Structural validity is asserted to be *not* a pass verdict: there is no
`summary` and no `passed` field anywhere in these contracts.
"""
from __future__ import annotations

import json

import pytest

from model_scheduler import evidence_contracts as ec
from model_scheduler.contracts_v2 import ContractError

GGUF_PROFILE = "llama-cpp-gguf-v1"


def _device() -> dict:
    return {
        "machine_id_sha256": "1" * 64,
        "architecture": "aarch64",
        "device_tree_sha256": "2" * 64,
        "mem_total_bytes": 64_000_000_000,
        "os_release": "Ubuntu 22.04.5 LTS",
        "kernel_release": "5.15.148-tegra",
        "gpu_identity": "NVIDIA Orin (SM8.7)",
        "jetpack_release": "R36.4.7",
        "container_runtime_version": "docker 27.5.1",
        "power_mode": "MAXN",
        "clock_mode": "fixed",
        "model_disk_uuid": "16d53274-d9f5-4282-9b44-7fdd43cba9ca",
        "scratch_disk_uuid": "0f9b2c31-1111-2222-3333-444455556666",
    }


def _runtime_stack() -> dict:
    return {"cuda_version": "12.6", "compute_capability": "8.7", "python_version": "3.12.11"}


def _performance(model_id: str = "qwen25vl-7b-q4") -> dict:
    return {
        "model_id": model_id,
        "cold_start_seconds_max": 30.0,
        "infer_milliseconds_p95_max": 5000.0,
        "infer_milliseconds_p99_max": 8000.0,
        "max_input_milliseconds_max": 20000.0,
    }


def _operational() -> dict:
    return {
        "duration_seconds": 1800,
        "arrival_requests": 100,
        "arrival_gap_seconds_max": 15.0,
        "send_deviation_milliseconds_max": 1000.0,
        "error_rate_max": 0.1,
        "queue_full_rate_max": 0.1,
        "timeout_rate_max": 0.1,
    }


def _candidate_data(**overrides) -> dict:
    data = {
        "schema_version": 3,
        "deployment_id": "orin-local",
        "source_archive_sha256": "3" * 64,
        "config_sha256": "4" * 64,
        "device": _device(),
        "runtime_stack": _runtime_stack(),
        "runtimes": [
            {
                "runtime_id": "llama-cpp-cuda-sm87-4bc272f",
                "profile_id": GGUF_PROFILE,
                "image_digest": "ghcr.io/example/llama-cuda@sha256:" + "a" * 64,
                "adapter_sha256": "b" * 64,
                "lock_sha256": "c" * 64,
                "startup_args": ["--parallel", "--no-warmup"],
            }
        ],
        "models": [
            {
                "model_id": "qwen25vl-7b-q4",
                "runtime_id": "llama-cpp-cuda-sm87-4bc272f",
                "capabilities": ["chat", "vision"],
                "assets": [
                    {
                        "role": "model",
                        "path": "qwen25vl-7b-q4/model.gguf",
                        "sha256": "3f" + "0" * 62,
                        "size_bytes": 4683072320,
                    },
                    {
                        "role": "projector",
                        "path": "qwen25vl-7b-q4/mmproj.gguf",
                        "sha256": "d1" + "0" * 62,
                        "size_bytes": 1354162912,
                    },
                ],
                "port": 18081,
                "envelope": {
                    "ctx_size": 32768,
                    "max_input_tokens": 28672,
                    "max_output_tokens": 4096,
                    "max_parallel": 2,
                    "max_image_tokens": 1280,
                    "max_image_edge_pixels": 1024,
                    "max_images": 1,
                },
                "timeout_seconds": 3600,
                "reserved_bytes": 6106148045,
                "measured": True,
                "measurement_ref": "e" * 64,
                "physical_resident_peak_bytes": 5000000000,
            }
        ],
        "measurement_refs": [
            {"relative_path": "measurements/calibration.json", "size_bytes": 1000, "sha256": "5" * 64}
        ],
        "fixture_refs": [{"relative_path": "fixtures/llama_cpp_v1.json", "size_bytes": 2000, "sha256": "6" * 64}],
        "policy": {"performance": [_performance()], "operational": _operational()},
        "collector_sha256": "7" * 64,
        "evaluator_sha256": "8" * 64,
    }
    data.update(overrides)
    return data


def _attempt(candidate: ec.CandidateV3, case_id: str, **overrides) -> dict:
    data = {
        "case_id": case_id,
        "run_id": "run-0001",
        "attempt": 1,
        "candidate_sha256": ec.candidate_digest(candidate),
        "device_digest": ec.device_digest(candidate.device),
        "started_at": "2026-09-17T05:00:00Z",
        "ended_at": "2026-09-17T05:10:00Z",
        "boot_id": "boot-0001",
        "event_refs": [],
        "collector_sha256": "7" * 64,
        "evaluator_sha256": "8" * 64,
        "exit_code": 0,
    }
    data.update(overrides)
    return data


def _report_data(candidate: ec.CandidateV3) -> dict:
    cases = sorted(ec.required_case_ids(candidate))
    return {
        "schema_version": 3,
        "candidate_sha256": ec.candidate_digest(candidate),
        "device_digest": ec.device_digest(candidate.device),
        "run_id": "run-0001",
        "started_at": "2026-09-17T05:00:00Z",
        "ended_at": "2026-09-17T05:10:00Z",
        "case_attempt_refs": [_attempt(candidate, case_id) for case_id in cases],
        "final_attempts": [{"case_id": case_id, "run_id": "run-0001", "attempt": 1} for case_id in cases],
        "artifact_manifest": [{"relative_path": "candidate.json", "size_bytes": 4096, "sha256": "9" * 64}],
    }


def test_candidate_parses_and_the_digest_is_reproducible():
    first = ec.parse_candidate(_candidate_data())
    second = ec.parse_candidate(_candidate_data())
    assert ec.candidate_digest(first) == ec.candidate_digest(second)
    assert len(ec.candidate_digest(first)) == 64
    assert ec.candidate_digest(first) == ec.candidate_digest(first)


def test_candidate_digest_has_no_self_reference_and_no_generation_time():
    candidate = ec.parse_candidate(_candidate_data())
    digest = ec.candidate_digest(candidate)
    # a report for this candidate exists, yet the candidate digest is unchanged:
    # the hash chain never points back at a report
    ec.parse_acceptance_report(_report_data(candidate))
    assert ec.candidate_digest(candidate) == digest
    for forbidden in ("candidate_sha256", "generated_at", "created_at", "report", "evidence_directory", "summary"):
        assert forbidden not in ec.CANDIDATE_KEYS


def test_canonical_body_expands_every_field():
    candidate = ec.parse_candidate(_candidate_data())
    body = json.loads(ec.canonical_body(candidate))
    assert set(body) == ec.CANDIDATE_KEYS
    assert set(body["device"]) == ec.DEVICE_KEYS
    assert set(body["runtime_stack"]) == ec.RUNTIME_STACK_KEYS
    assert set(body["policy"]) == ec.POLICY_KEYS
    assert set(body["policy"]["operational"]) == ec.OPERATIONAL_KEYS
    assert set(body["policy"]["performance"][0]) == ec.PERFORMANCE_KEYS
    assert set(body["models"][0]) and set(body["runtimes"][0])


def test_candidate_rejects_unknown_and_missing_fields():
    for forbidden in ("generated_at", "evidence_directory", "summary", "candidate_sha256", "results"):
        with pytest.raises(ContractError):
            ec.parse_candidate(_candidate_data(**{forbidden: "x"}))
    for missing in ("device", "policy", "collector_sha256", "fixture_refs"):
        data = _candidate_data()
        del data[missing]
        with pytest.raises(ContractError):
            ec.parse_candidate(data)


def test_candidate_requires_consistent_models_runtimes_and_policy():
    data = _candidate_data()
    data["policy"]["performance"] = []
    with pytest.raises(ContractError):
        ec.parse_candidate(data)

    data = _candidate_data()
    data["policy"]["performance"].append(_performance("ghost-model"))
    with pytest.raises(ContractError):
        ec.parse_candidate(data)

    data = _candidate_data()
    data["models"][0]["runtime_id"] = "ghost-runtime"
    with pytest.raises(ContractError):
        ec.parse_candidate(data)

    data = _candidate_data()
    data["models"].append(dict(data["models"][0]))
    with pytest.raises(ContractError):
        ec.parse_candidate(data)


def test_candidate_requires_schema_version_three():
    with pytest.raises(ContractError):
        ec.parse_candidate(_candidate_data(schema_version=2))


def test_artifact_paths_reject_escape_and_duplicates():
    for bad_path in ("/etc/passwd", "a/../../b", "../x", "a//b", "a/./b", "a\\b", "a\x00b", ""):
        with pytest.raises(ContractError):
            ec.parse_artifact_ref({"relative_path": bad_path, "size_bytes": 1, "sha256": "a" * 64})
    with pytest.raises(ContractError):  # duplicate paths in one manifest
        ec.parse_artifact_list(
            [
                {"relative_path": "x.json", "size_bytes": 1, "sha256": "a" * 64},
                {"relative_path": "x.json", "size_bytes": 2, "sha256": "b" * 64},
            ],
            "manifest",
        )
    with pytest.raises(ContractError):
        ec.parse_artifact_ref({"relative_path": "x.json", "size_bytes": 1, "sha256": "A" * 64})


def test_policy_cannot_relax_the_acceptance_thresholds():
    for mutation in (
        {"duration_seconds": 1799},
        {"arrival_requests": 99},
        {"arrival_gap_seconds_max": 15.5},
        {"send_deviation_milliseconds_max": 1000.5},
        {"error_rate_max": 0.11},
        {"queue_full_rate_max": 0.2},
        {"timeout_rate_max": 0.2},
    ):
        operational = _operational()
        operational.update(mutation)
        with pytest.raises(ContractError):
            ec.parse_operational_policy(operational)

    stricter = _operational()
    stricter.update({"duration_seconds": 3600, "arrival_requests": 200, "error_rate_max": 0.05})
    assert ec.parse_operational_policy(stricter).duration_seconds == 3600


def test_case_id_grammar():
    for good in ("S01", "S06", "O01", "O06", "B:qwen25vl-7b-q4:load", "B:qwen25vl-7b-q4:cap:embeddings",
                 "B:qwen25vl-7b-q4:cap:tools", "B:qwen25vl-7b-q4:cap:thinking"):
        assert ec.parse_case_id(good) == good
    for bad in ("S07", "S00", "O07", "s01", "B:qwen25vl-7b-q4:predict", "B:UPPER:load", "B:qwen25vl-7b-q4:cap:audio"):
        with pytest.raises(ContractError):
            ec.parse_case_id(bad)


def test_tool_calling_capabilities_drive_their_own_required_cases():
    # TC01: capability-case generation follows the *model* capability set, so a
    # registration that turns on tools/thinking must produce those cases too.
    data = _candidate_data()
    data["models"][0]["capabilities"] = ["chat", "vision", "tools", "thinking"]
    candidate = ec.parse_candidate(data)
    required = ec.required_case_ids(candidate)
    assert {"B:qwen25vl-7b-q4:cap:tools", "B:qwen25vl-7b-q4:cap:thinking"} <= required
    assert len(required) == len(ec.SOFTWARE_CASES) + len(ec.OPERATIONAL_CASES) + 6 + 4


def test_required_case_ids_follow_the_candidate():
    candidate = ec.parse_candidate(_candidate_data())
    required = ec.required_case_ids(candidate)
    assert ec.SOFTWARE_CASES <= required
    assert ec.OPERATIONAL_CASES <= required
    assert "B:qwen25vl-7b-q4:load" in required
    assert "B:qwen25vl-7b-q4:cap:vision" in required
    assert len(required) == len(ec.SOFTWARE_CASES) + len(ec.OPERATIONAL_CASES) + 6 + 2


def test_report_mapping_accepts_complete_coverage():
    candidate = ec.parse_candidate(_candidate_data())
    report = ec.parse_acceptance_report(_report_data(candidate))
    ec.validate_report_mapping(report, candidate)
    ec.validate_report_identity(report, candidate)


def test_report_mapping_rejects_missing_unknown_and_duplicate_cases():
    candidate = ec.parse_candidate(_candidate_data())

    data = _report_data(candidate)  # missing one required case
    data["final_attempts"] = [final for final in data["final_attempts"] if final["case_id"] != "S06"]
    with pytest.raises(ContractError):
        ec.validate_report_mapping(ec.parse_acceptance_report(data), candidate)

    data = _report_data(candidate)  # a well-formed case the candidate does not require
    data["final_attempts"].append({"case_id": "B:ghost-model:load", "run_id": "run-0001", "attempt": 1})
    with pytest.raises(ContractError):
        ec.validate_report_mapping(ec.parse_acceptance_report(data), candidate)

    data = _report_data(candidate)  # an attempt for an unknown case
    data["case_attempt_refs"].append(_attempt(candidate, "B:ghost-model:infer"))
    with pytest.raises(ContractError):
        ec.validate_report_mapping(ec.parse_acceptance_report(data), candidate)

    data = _report_data(candidate)  # duplicate final conclusions
    data["final_attempts"].append({"case_id": "S01", "run_id": "run-0001", "attempt": 1})
    with pytest.raises(ContractError):
        ec.validate_report_mapping(ec.parse_acceptance_report(data), candidate)

    data = _report_data(candidate)  # final points at a non-existent attempt
    data["final_attempts"][0]["attempt"] = 2
    with pytest.raises(ContractError):
        ec.validate_report_mapping(ec.parse_acceptance_report(data), candidate)


def test_failed_attempts_are_kept_and_the_final_is_traceable():
    candidate = ec.parse_candidate(_candidate_data())
    data = _report_data(candidate)
    for attempt in data["case_attempt_refs"]:
        if attempt["case_id"] == "S02":
            attempt["exit_code"] = 1  # first attempt failed and stays in the record
    data["case_attempt_refs"].append(_attempt(candidate, "S02", attempt=2))
    for final in data["final_attempts"]:
        if final["case_id"] == "S02":
            final["attempt"] = 2

    report = ec.parse_acceptance_report(data)
    ec.validate_report_mapping(report, candidate)
    s02 = [attempt for attempt in report.attempts if attempt.case_id == "S02"]
    assert [attempt.attempt for attempt in s02] == [1, 2]
    assert s02[0].exit_code == 1


def test_report_rejects_summary_and_inconsistent_times():
    candidate = ec.parse_candidate(_candidate_data())

    data = _report_data(candidate)
    data["summary"] = {"passed": True}
    with pytest.raises(ContractError):  # a summary field does not exist on purpose
        ec.parse_acceptance_report(data)

    data = _report_data(candidate)
    data["started_at"] = "2026-09-17T04:00:00Z"  # not the earliest attempt start
    with pytest.raises(ContractError):
        ec.validate_report_mapping(ec.parse_acceptance_report(data), candidate)

    data = _report_data(candidate)
    data["ended_at"] = "2026-09-17T06:00:00Z"  # not the latest attempt end
    with pytest.raises(ContractError):
        ec.validate_report_mapping(ec.parse_acceptance_report(data), candidate)


def test_report_identity_must_match_candidate_and_device():
    candidate = ec.parse_candidate(_candidate_data())

    data = _report_data(candidate)
    data["candidate_sha256"] = "f" * 64
    with pytest.raises(ContractError):
        ec.validate_report_identity(ec.parse_acceptance_report(data), candidate)

    data = _report_data(candidate)
    data["device_digest"] = "f" * 64
    with pytest.raises(ContractError):
        ec.validate_report_identity(ec.parse_acceptance_report(data), candidate)

    other_device = _candidate_data()
    other_device["device"]["clock_mode"] = "unknown"
    with pytest.raises(ContractError):  # a different device fact is a different digest
        ec.validate_report_identity(
            ec.parse_acceptance_report(_report_data(candidate)),
            ec.parse_candidate(other_device),
        )


def test_attempt_rejects_reversed_times_and_bad_identifiers():
    candidate = ec.parse_candidate(_candidate_data())
    with pytest.raises(ContractError):
        ec.parse_case_attempt(
            _attempt(candidate, "S01", started_at="2026-09-17T05:10:00Z", ended_at="2026-09-17T05:00:00Z")
        )
    with pytest.raises(ContractError):
        ec.parse_case_attempt(_attempt(candidate, "S01", attempt=0))
    with pytest.raises(ContractError):
        ec.parse_case_attempt(_attempt(candidate, "S01", boot_id=""))
    with pytest.raises(ContractError):
        ec.parse_case_attempt(_attempt(candidate, "S01", exit_code=True))


def test_artifact_manifest_digest_follows_list_order():
    first = ec.ArtifactRef(relative_path="a.json", size_bytes=1, sha256="a" * 64)
    second = ec.ArtifactRef(relative_path="b.json", size_bytes=1, sha256="b" * 64)
    assert ec.artifact_manifest_digest([first, second]) == ec.artifact_manifest_digest([first, second])
    assert ec.artifact_manifest_digest([first, second]) != ec.artifact_manifest_digest([second, first])
