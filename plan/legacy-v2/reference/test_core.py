"""Synthetic contract counterexamples; these do not assert hardware readiness."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import unittest

from pydantic import ValidationError

from acceptance import canonical_digest, configuration_digest, required_scenarios, verify
from contracts import AcceptancePolicy, AcceptanceReport, Asset, Candidate, ExecutionView, PermitToken, Span
from core import Conflict, Phase, StopEvidence, can_admit, job_transition, reserve_bytes


H = "a" * 64


def candidate_data(scope="scheduler-only"):
    roles = {"asr": ("moss_transformers", "transcribe"), "face_detect": ("yunet", "face_detect"),
             "face_embed": ("insightface", "face_embed"), "depth": ("depth_v2", "depth"), "vision": ("llama", "vision")}
    if scope == "scheduler-only":
        roles = {"custom-chat": ("llama", "chat")}
    runtimes, models = [], []
    for index, (mid, (adapter, cap)) in enumerate(roles.items()):
        runtimes.append({"runtime_id": mid, "kind": "llama_cpp" if adapter == "llama" else "python_worker",
                         "image": f"example/{mid}@sha256:{H}", "adapter": adapter, "lock_sha256": H, "adapter_sha256": H})
        assets = [{"path": f"{mid}/weights.bin", "role": "weights", "size_bytes": 1, "sha256": H}]
        if cap == "vision":
            assets.append({"path": "vision/mmproj.gguf", "role": "projector", "size_bytes": 1, "sha256": H})
        models.append({"model_id": mid, "runtime_id": mid, "capabilities": [cap], "assets": assets,
                       "port": 10001 + index, "measured_peak_bytes": 100, "execution_device": "cuda",
                       "envelope": {"context_tokens": 32768, "max_output_tokens": 2048, "max_audio_ms": 1500000,
                                    "max_images": 3, "max_pixels_per_image": 1280 * 720, "batch_size": 1, "concurrency": 1}})
    device = {"machine_id_sha256": H, "device_tree_model": "fixture-device", "compatible": ["fixture-board"],
              "mem_total_bytes": 64 * 2**30, "architecture": "aarch64", "os_release": "fixture",
              "kernel": "fixture", "jetpack": "fixture", "cuda_driver": "fixture", "nvidia_container_runtime": "fixture",
              "power_mode": "fixture", "clocks_mode": "fixture", "model_ssd_uuid": "fixture", "work_ssd_uuid": "fixture"}
    pipeline = None
    if scope != "scheduler-only":
        runtimes.append({"runtime_id": "media", "kind": "media_worker", "image": f"example/media@sha256:{H}",
                         "adapter": "media", "lock_sha256": H, "adapter_sha256": H})
        stages = [("probe", None), ("shots", None), ("asr", "asr"), ("face_tracks", "face_detect"),
                  ("face_identity", "face_embed"), ("depth", "depth"), ("vision", "vision"), ("mask", None)]
        if scope == "full-pipeline":
            stages.append(("report", None))
        pipeline = {"profile": "balanced-v1", "roles": {mid: mid for mid in roles},
                    "stages": [{"stage_id": stage, "model_id": model, "work_peak_bytes": 0 if stage == "report" else 100,
                                "unit_timeout_seconds": 10.0, "stage_timeout_seconds": 100.0} for stage, model in stages],
                    "quality_policy_sha256": H, "dataset_manifest_sha256": H, "full_film_sha256": H,
                    "max_film_wall_seconds": 100000.0, "max_work_bytes": 1000000, "max_switch_seconds": 1000.0,
                    "external_adapter_sha256": H if scope == "full-pipeline" else None,
                    "external_model": "fixture" if scope == "full-pipeline" else None,
                    "external_prompt_sha256": H if scope == "full-pipeline" else None}
    return {"schema_version": 3, "deployment_id": "test", "scope": scope, "source_commit": "b" * 40,
            "source_tree_sha256": H, "release_archive_sha256": H, "scheduler_config_sha256": H,
            "runner_config_sha256": H if pipeline else None, "evaluator_sha256": H, "collector_sha256": H,
            "acceptance_policy_sha256": H, "llama_swap_sha256": H, "device": device,
            "runtimes": runtimes, "models": models, "media_runtime_id": "media" if pipeline else None,
            "media_port": 19000 if pipeline else None, "model_budget_bytes": 32 * 2**30, "pipeline": pipeline}


def report_data(candidate):
    digest = canonical_digest(candidate.model_dump(mode="json"))
    blobs, records = {}, []
    for scenario_id in sorted(required_scenarios(candidate)):
        path = scenario_id.replace(":", "-") + ".json"
        raw = json.dumps({"scenario_id": scenario_id, "candidate_sha256": digest, "actual": 2, "expected": 2,
                          "started_at": "2026-09-16T00:00:00Z", "finished_at": "2026-09-16T01:00:00Z"}).encode()
        blobs[path] = raw
        records.append({"scenario_id": scenario_id, "run_id": "run1", "candidate_sha256": digest, "disposition": "executed",
                        "started_at": "2026-09-16T00:00:00Z", "finished_at": "2026-09-16T01:00:00Z",
                        "evidence": [{"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}]})
    report = {"schema_version": 3, "candidate_sha256": digest,
              "device_identity_sha256": canonical_digest(candidate.device.model_dump(mode="json")),
              "source_tree_sha256": H, "collector_sha256": H, "evaluator_sha256": H, "scope": candidate.scope,
              "started_at": "2026-09-16T00:00:00Z", "finished_at": "2026-09-16T01:00:00Z", "scenarios": records}
    return report, blobs


def synthetic_evaluator(candidate, case, payloads):
    # Test-only fake evaluator checks actual bytes. Never a production evaluator.
    data = json.loads(next(iter(payloads.values())))
    return (data["scenario_id"] == case.scenario_id and data["candidate_sha256"] == case.candidate_sha256
            and data["started_at"] == case.started_at and data["finished_at"] == case.finished_at
            and data["actual"] == data["expected"])


class ContractTests(unittest.TestCase):
    def test_dynamic_model_id_and_all_scopes(self):
        for scope in ("scheduler-only", "local-pipeline", "full-pipeline"):
            with self.subTest(scope=scope):
                Candidate.model_validate(candidate_data(scope))

    def test_reject_unknown_bool_integer_and_duplicate_ports(self):
        for mutate in (lambda d: d.update(unknown=1),
                       lambda d: d["models"][0].update(measured_peak_bytes=True),
                       lambda d: d["models"][1].update(port=d["models"][0]["port"])):
            data = candidate_data("local-pipeline")
            mutate(data)
            with self.assertRaises(ValidationError):
                Candidate.model_validate(data)

    def test_vision_requires_projector(self):
        data = candidate_data("local-pipeline")
        data["models"][-1]["assets"] = data["models"][-1]["assets"][:1]
        with self.assertRaises(ValidationError):
            Candidate.model_validate(data)

    def test_wrong_role_and_pinned_and_stage_order_rejected(self):
        mutations = (lambda d: d["pipeline"]["roles"].update(asr="depth"),
                     lambda d: d["models"][0].update(pinned=True),
                     lambda d: d["pipeline"]["stages"].reverse())
        for mutate in mutations:
            data = candidate_data("local-pipeline")
            mutate(data)
            with self.assertRaises(ValidationError):
                Candidate.model_validate(data)

    def test_paths_reject_escape_and_alias(self):
        for path in ("../x", "/x", "a/../x", "a//x", "./x", "a\\x", "."):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                Asset(path=path, role="weights", size_bytes=1, sha256=H)

    def test_zero_or_reversed_span_rejected(self):
        for end in (0, 1):
            with self.assertRaises(ValidationError):
                Span(start_ms=1, end_ms=end)

    def test_terminal_requires_quiescence(self):
        token = dict(boot_id="boot", permit_id="permit", job_id="job", stage_id="asr", attempt=1, model_id="asr")
        with self.assertRaises(ValidationError):
            ExecutionView(execution_id="e", token=PermitToken(**token), unit_id="u", attempt=1,
                          state="succeeded", outputs=[], error_code=None, compute_quiescent=False)

    def test_configuration_hash_has_no_candidate_cycle(self):
        config = {"candidate_sha256": "pending", "models": ["m"], "timeout": 10}
        digest = configuration_digest(config)
        config["candidate_sha256"] = H
        self.assertEqual(digest, configuration_digest(config))
        config["timeout"] = 11
        self.assertNotEqual(digest, configuration_digest(config))

    def test_soak_cannot_fake_duration_with_idle_wait(self):
        slo = {k: 10.0 for k in ("cold_load_seconds", "inference_seconds", "envelope_seconds", "p95_seconds", "p99_seconds")}
        data = {"schema_version": 1, "model_slos": {"m": slo},
                "arrivals": [{"at_ms": i * 10000, "model_id": "m", "fixture_sha256": H} for i in range(180)],
                "max_429_fraction": 0.0, "max_504_fraction": 0.0}
        AcceptancePolicy.model_validate(data)
        with self.assertRaises(ValidationError):
            AcceptancePolicy.model_validate(data | {"schema_version": True})
        for item in data["arrivals"]:
            item["at_ms"] = 0
        with self.assertRaises(ValidationError):
            AcceptancePolicy.model_validate(data)


class CoreTests(unittest.TestCase):
    def phase(self):
        token = PermitToken(boot_id="boot1", permit_id="permit1", job_id="job1", stage_id="asr", attempt=1, model_id="asr")
        phase = Phase(token, 100, 100.0, 30.0, "container1")
        phase.activate(token, 0)
        return phase

    def test_budget_exact_boundaries_and_stale_samples(self):
        args = dict(total=1000, available=120, sampled_at=0.0, now=2.0, committed=200,
                    new_bytes=100, budget=300, floor=20, system_reserve=100, ready=True, slot_free=True)
        self.assertTrue(can_admit(**args))
        for patch in ({"available": 119}, {"budget": 299}, {"now": 2.01}, {"sampled_at": 3.0},
                      {"now": float("nan")}, {"slot_free": False}, {"new_bytes": True}):
            self.assertFalse(can_admit(**(args | patch)))
        self.assertEqual(reserve_bytes(101), 117)

    def test_expiry_retains_budget_and_rejects_renew(self):
        phase = self.phase()
        phase.submit(phase.token, "e1", 1)
        phase.expire(30)
        self.assertEqual((phase.state, phase.reservation, phase.execution_id), ("draining", 100, "e1"))
        with self.assertRaises(Conflict):
            phase.renew(phase.token, 30)

    def test_late_load_cannot_revive_preparing_permit(self):
        token = self.phase().token
        phase = Phase(token, 100, 100.0, 30.0, "container1")
        with self.assertRaises(Conflict):
            phase.activate(token, 31)
        self.assertEqual((phase.state, phase.reservation), ("draining", 100))

    def test_old_boot_cannot_submit_or_release(self):
        phase = self.phase()
        old = phase.token.model_copy(update={"boot_id": "old"})
        with self.assertRaises(Conflict):
            phase.submit(old, "e", 1)
        with self.assertRaises(Conflict):
            phase.close(old)
        self.assertEqual(phase.reservation, 100)

    def test_stop_ack_without_real_stop_blocks(self):
        phase = self.phase()
        phase.close(phase.token)
        for evidence in (StopEvidence("container1", False, True, True), StopEvidence("other", True, True, True),
                         StopEvidence("container1", True, False, True), StopEvidence("container1", True, True, False)):
            with self.assertRaises(Conflict):
                phase.confirm_stopped(phase.token, evidence)
            self.assertEqual((phase.state, phase.reservation), ("blocked", 100))
        phase.confirm_stopped(phase.token, StopEvidence("container1", True, True, True))
        self.assertEqual((phase.state, phase.reservation), ("closed", 0))

    def test_terminal_releases_execution_only_and_duplicate_is_safe(self):
        phase = self.phase()
        phase.submit(phase.token, "first", 1)
        with self.assertRaises(Conflict):
            phase.terminal(phase.token, "first", compute_quiescent=False)
        phase.terminal(phase.token, "first", compute_quiescent=True)
        phase.submit(phase.token, "second", 2)
        phase.terminal(phase.token, "first", compute_quiescent=True)
        self.assertEqual((phase.execution_id, phase.reservation), ("second", 100))

    def test_close_idempotent_and_stopped_orphan_clears(self):
        phase = self.phase()
        phase.submit(phase.token, "e", 1)
        phase.close(phase.token)
        phase.close(phase.token)
        phase.confirm_stopped(phase.token, StopEvidence("container1", True, True, True))
        phase.close(phase.token)
        self.assertIsNone(phase.execution_id)
        self.assertEqual(phase.state, "closed")

    def test_job_cancel_requires_actual_stop_and_resume_is_explicit(self):
        self.assertEqual(job_transition("running", "paused"), "paused")
        with self.assertRaises(Conflict):
            job_transition("cancelling", "cancelled")
        self.assertEqual(job_transition("cancelling", "cancelled", resources_stopped=True), "cancelled")
        with self.assertRaises(Conflict):
            job_transition("cancelled", "queued")


class GateTests(unittest.TestCase):
    def setUp(self):
        self.candidate = Candidate.model_validate(candidate_data())
        self.data, self.blobs = report_data(self.candidate)

    def gate(self, data=None, blobs=None, candidate=None, evaluator=synthetic_evaluator):
        return verify(candidate or self.candidate, AcceptanceReport.model_validate(data or self.data),
                      now=datetime(2026, 9, 16, 2, tzinfo=timezone.utc),
                      read_evidence=lambda path: (blobs if blobs is not None else self.blobs)[path], evaluate=evaluator)

    def test_complete_synthetic_envelope_passes_only_with_evaluator(self):
        self.assertTrue(self.gate().eligible)
        self.assertFalse(self.gate(evaluator=None).eligible)

    def test_missing_and_duplicate_scenario_fail(self):
        data = deepcopy(self.data)
        data["scenarios"].pop()
        self.assertFalse(self.gate(data).eligible)
        data = deepcopy(self.data)
        data["scenarios"].append(data["scenarios"][0])
        self.assertFalse(self.gate(data).eligible)

    def test_skip_unknown_and_not_run_fail(self):
        for disposition in ("skipped", "unknown", "not_run", "failed"):
            data = deepcopy(self.data)
            data["scenarios"][0]["disposition"] = disposition
            self.assertFalse(self.gate(data).eligible)

    def test_tampered_bytes_fail(self):
        blobs = self.blobs.copy()
        key = next(iter(blobs))
        blobs[key] += b" "
        self.assertFalse(self.gate(blobs=blobs).eligible)

    def test_missing_evidence_fails_closed(self):
        def missing(_path):
            raise FileNotFoundError("missing")
        result = verify(self.candidate, AcceptanceReport.model_validate(self.data),
                        now=datetime(2026, 9, 16, 2, tzinfo=timezone.utc), read_evidence=missing,
                        evaluate=synthetic_evaluator)
        self.assertFalse(result.eligible)

    def test_lying_summary_does_not_override_raw_evidence(self):
        data = deepcopy(self.data)
        data["scenarios"][0]["passed"] = True
        with self.assertRaises(ValidationError):
            AcceptanceReport.model_validate(data)

    def test_valid_hash_does_not_replace_semantic_evaluation(self):
        data, blobs = deepcopy(self.data), self.blobs.copy()
        ref = data["scenarios"][0]["evidence"][0]
        raw = json.loads(blobs[ref["path"]])
        raw["actual"] = 999
        payload = json.dumps(raw).encode()
        blobs[ref["path"]] = payload
        ref.update(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))
        self.assertFalse(self.gate(data, blobs).eligible)

    def test_different_physical_device_or_code_fails(self):
        for mutate in (lambda d: d["device"].update(machine_id_sha256="c" * 64),
                       lambda d: d.update(source_tree_sha256="c" * 64)):
            raw = candidate_data()
            mutate(raw)
            self.assertFalse(self.gate(candidate=Candidate.model_validate(raw)).eligible)

    def test_expired_future_and_reversed_timestamps_fail(self):
        for start, end in (("2026-09-01T00:00:00Z", "2026-09-16T01:00:00Z"),
                           ("2026-09-16T00:00:00Z", "2026-09-16T02:06:00Z"),
                           ("2026-09-16T01:30:00Z", "2026-09-16T01:00:00Z")):
            data = deepcopy(self.data)
            data.update(started_at=start, finished_at=end)
            self.assertFalse(self.gate(data).eligible)

    def test_full_scope_requires_external_and_pipeline_cases(self):
        candidate = Candidate.model_validate(candidate_data("full-pipeline"))
        cases = required_scenarios(candidate)
        self.assertTrue({"E01", "E02", "E03", "Q06", "P08", "B:asr:cap:transcribe"} <= cases)
        local = Candidate.model_validate(candidate_data("local-pipeline"))
        self.assertFalse(any(case.startswith("E") for case in required_scenarios(local)))

    def test_repackaging_old_evidence_cannot_refresh_time(self):
        data = deepcopy(self.data)
        data.update(started_at="2026-09-16T01:00:00Z", finished_at="2026-09-16T01:30:00Z")
        self.assertFalse(self.gate(data).eligible)
        for case in data["scenarios"]:
            case.update(started_at=data["started_at"], finished_at=data["finished_at"])
        self.assertFalse(self.gate(data).eligible)

    def test_old_report_and_passed_flags_are_not_schema(self):
        for patch in ({"schema_version": 2}, {"gpu_verified": True}):
            with self.assertRaises(ValidationError):
                AcceptanceReport.model_validate(self.data | patch)


if __name__ == "__main__":
    unittest.main()
