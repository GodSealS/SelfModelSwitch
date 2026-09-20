"""P23: the B-case executor drives the official API and never passes what it cannot prove.

A fake driver stands in for the real control API: the tests pin the matrix
(six kinds + one case per capability, three cold starts, three reload rounds),
the single-request combined boundary, the provider/device/output attribution,
`unknown` staying `unknown`, and crash attempts keeping both their failure and
their cleanup record.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from model_scheduler.acceptance import backend_cases as bc
from model_scheduler.acceptance import fixtures as fx
from model_scheduler.contracts_v2 import Envelope

ENVELOPE = Envelope(ctx_size=1024, max_input_tokens=512, max_output_tokens=64, max_parallel=2,
                    max_image_tokens=64, max_image_edge_pixels=64, max_images=1)
# A measured ratio: one filler unit of "a" really costs this tokenizer one token.
INSTRUCTION = "Reply briefly."
INSTRUCTION_WORDS = 2  # how the fake counts it
INSTRUCTION_TOKENS = 3  # the measured cost, separator included
FILLER = fx.FillerSpec(unit="a", tokens_per_unit=1.0, template_overhead_tokens=19,
                       vision_template_overhead_tokens=50, instruction=INSTRUCTION,
                       instruction_tokens=INSTRUCTION_TOKENS)
FILLERS = {"qwen-small": FILLER}
FAKE_TEMPLATE_TOKENS = 19  # the measured template costs the fake runtime reproduces
FAKE_VISION_TEMPLATE_TOKENS = 50


def _text_words(request) -> int:
    words = 0
    for message in request.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            words += len(content.split())
        else:
            words += sum(len(item.get("text", "").split()) for item in content if item.get("type") == "text")
    return words


class FakeDriver:
    """A scripted official-API surface; every fault is injected explicitly."""

    def __init__(self) -> None:
        self.loads: list[bool] = []
        self.executions: list[dict] = []
        self.cancels: list[str] = []
        self.stops = 0
        self.cleanups: list[str] = []
        self.fail_on: str | None = None
        self.fail_cleanup = False
        self.no_device_activity = False
        self.split_boundary = False
        self.nan_embeddings = False
        self.empty_vectors = False
        self.cancel_proof: bool | None = True
        self.stop_proven = True
        self.no_execution_id = False
        self._execution_seq = 0

    def load(self, model_id: str, *, cold: bool):
        self.loads.append(cold)
        return {"provider": "llama-cpp-1", "instance": {"container_id": "c1", "model_id": model_id}, "cold": cold}

    def start(self, model_id: str, request):
        self._execution_seq += 1
        if self.no_execution_id:
            return {"provider": "llama-cpp-1"}
        return {"execution_id": f"exec-{self._execution_seq}", "provider": "llama-cpp-1"}

    def cancel(self, model_id: str, execution_id: str):
        self.cancels.append(execution_id)
        if self.cancel_proof is None:
            return {"provider": "llama-cpp-1"}
        return {"provider": "llama-cpp-1", "cancelled": self.cancel_proof}

    def stop(self, model_id: str):
        self.stops += 1
        return {"provider": "llama-cpp-1", "stop_proven": self.stop_proven, "instance": None}

    def cleanup(self, model_id: str):
        self.cleanups.append(model_id)
        if self.fail_cleanup:
            raise RuntimeError("cleanup channel is down")
        return {"stopped": True, "model_id": model_id}

    def execute(self, model_id: str, request):
        self.executions.append(dict(request))
        if self.fail_on is not None and self.fail_on in json.dumps(request):
            raise RuntimeError("the executor crashed")
        activity = None if self.no_device_activity else {"gr3d_peak_pct": 87, "cuda_library_mapped": True,
                                                         "raw_samples": {"tegrastats": 4}}
        common = {"provider": "runtime-1", "device_activity": activity}
        if "messages" in request:
            content = [item for message in request["messages"] for item in (
                message["content"] if isinstance(message["content"], list) else
                [{"type": "text", "text": message["content"]}])]
            images = sum(1 for item in content if item.get("type") == "image_url")
            # A real round costs the filler plus its chat template: the fake runtime
            # mirrors that, so a declared boundary is reached exactly.
            tokens = (_text_words(request) + FAKE_TEMPLATE_TOKENS
                      + (INSTRUCTION_TOKENS - INSTRUCTION_WORDS))
            if images:  # the runtime charges the declared image budget and the vision template
                tokens += images * ENVELOPE.max_image_tokens + (FAKE_VISION_TEMPLATE_TOKENS
                                                                - FAKE_TEMPLATE_TOKENS)
            output_tokens = int(request.get("max_tokens", 0))
            if self.split_boundary:
                tokens = tokens // 2
            return {**common, "output": {"message": {"role": "assistant",
                                                     "content": " ".join(f"out{i}" for i in range(output_tokens))}},
                    "observed": {"input_tokens": tokens, "output_tokens": output_tokens, "images": images,
                                 "parallel": int(request.get("n_parallel", 1))}}
        if "inputs" in request:
            if self.empty_vectors:
                return {**common, "output": {"vectors": []}, "observed": {"batch": len(request["inputs"])}}
            vectors = [[float(index), 1.0] for index in range(len(request["inputs"]))]
            if self.nan_embeddings:
                vectors[0] = [math.nan, 1.0]
            return {**common, "output": {"vectors": vectors}, "observed": {"batch": len(request["inputs"])}}
        return {**common, "output": {"results": [{"index": index, "score": 0.5}
                                                 for index in range(len(request["documents"]))]},
                "observed": {"documents": len(request["documents"])}}


def _run(driver: FakeDriver, capabilities=("chat", "vision"), collector=None, **kwargs):
    executor = bc.CaseExecutor(driver, collector=collector, filler_of=FILLERS, **kwargs)
    return executor.run_model(model_id="qwen-small", capabilities=capabilities, envelope=ENVELOPE)


def test_the_matrix_covers_every_case_with_three_cold_starts_and_three_reloads() -> None:
    driver = FakeDriver()
    attempts = _run(driver)

    summary = bc.summarize(attempts)
    assert set(summary["cases"]) == {"B:qwen-small:load", "B:qwen-small:infer", "B:qwen-small:envelope",
                                     "B:qwen-small:cancel", "B:qwen-small:stop", "B:qwen-small:reload",
                                     "B:qwen-small:cap:chat", "B:qwen-small:cap:vision"}
    assert driver.loads == [True, True, True, True, True, True]  # 3 cold starts + 3 reload loads, all cold
    assert [attempt.attempt for attempt in attempts if attempt.case_id.endswith(":reload")] == [1, 2, 3]
    assert driver.stops == 1 + 3  # the stop case plus one stop per reload round
    assert summary["passed"] is True and summary["counts"]["unknown"] == 0


def test_the_envelope_round_reaches_every_declared_boundary_in_one_request() -> None:
    driver = FakeDriver()
    attempts = _run(driver)

    envelope = [attempt for attempt in attempts if attempt.case_id.endswith(":envelope")][0]
    fixture = fx.fixtures_for("qwen-small", ("chat",), ENVELOPE, filler=FILLER)[0]
    assert envelope.status == "passed"
    assert fx.boundary_shortfalls(fixture, envelope.facts["observed"]) == []
    # one request per inference case: infer + envelope + two capability cases, never split up
    assert len(driver.executions) == 4

    split = FakeDriver()
    split.split_boundary = True
    split_attempts = _run(split)
    envelope = [attempt for attempt in split_attempts if attempt.case_id.endswith(":envelope")][0]
    assert envelope.status == "failed"
    assert any("did not reach the declared boundary" in problem for problem in envelope.problems)
    assert len(split.executions) == 4  # still one request: the boundary was simply not reached


def test_a_case_without_attributable_device_activity_stays_unknown() -> None:
    driver = FakeDriver()
    driver.no_device_activity = True

    attempts = _run(driver)

    infer = [attempt for attempt in attempts if attempt.case_id.endswith(":infer")][0]
    assert infer.status == "unknown"
    assert any("device activity" in problem for problem in infer.problems)
    assert bc.summarize(attempts)["passed"] is False  # unknown never converts to passed


def test_the_raw_rows_a_driver_returns_become_the_case_material(tmp_path) -> None:
    """A driver that sampled the device hands the rows over; the executor persists them.

    The rows are the evidence the evaluator later recomputes from, so they must
    reach the run's material even though only their counts travel on the attempt.
    """
    from model_scheduler.acceptance.collector import FileCollector

    class SamplingDriver(FakeDriver):
        def execute(self, model_id, request):
            result = dict(super().execute(model_id, request))
            result["samples"] = ({"kind": "tegrastats", "raw": "RAM 1/2MB GR3D_FREQ 41%"},
                                 {"kind": "tegrastats", "raw": "RAM 2/2MB GR3D_FREQ 87%"},
                                 {"kind": "proc_maps", "raw": "7f00 r-xp libcudart.so.12"})
            return result

    driver = SamplingDriver()
    collector = FileCollector(tmp_path, run_id="run-1", candidate_sha256="a" * 64, device_digest="b" * 64,
                              boot_id="boot-1")

    executor = bc.CaseExecutor(driver, collector=collector, cold_starts=3, reload_rounds=3, filler_of=FILLERS)
    executor.run_model(model_id="qwen-small", capabilities=("chat",), envelope=ENVELOPE)

    rows = [json.loads(line) for line in (tmp_path / "samples" / "tegrastats.jsonl").read_text().splitlines()
            if line.strip()]
    maps = [json.loads(line) for line in (tmp_path / "samples" / "proc_maps.jsonl").read_text().splitlines()
            if line.strip()]
    # infer + envelope + cap:chat are the three inference calls of this model
    assert len(rows) == 3 * 2 and len(maps) == 3
    assert rows[-1]["kind"] == "tegrastats" and rows[-1]["raw"] == "RAM 2/2MB GR3D_FREQ 87%"
    assert maps[0]["raw"] == "7f00 r-xp libcudart.so.12"  # attributable to the instance that ran it
    attribution = collector.attribution()
    assert attribution["gr3d_peak_pct"] == 87 and attribution["cuda_library_mapped"] is True
    assert attribution["raw_samples"] == {"proc_maps": 3, "tegrastats": 6}


def test_a_case_records_what_it_saw_and_why_it_did_not_pass(tmp_path) -> None:
    """Material must explain a failure without re-running it."""
    from model_scheduler.acceptance.collector import FileCollector

    driver = FakeDriver()
    driver.split_boundary = True  # the round falls short of its declared boundary
    collector = FileCollector(tmp_path, run_id="run-1", candidate_sha256="a" * 64, device_digest="b" * 64,
                              boot_id="boot-1")
    executor = bc.CaseExecutor(driver, collector=collector, cold_starts=3, reload_rounds=3, filler_of=FILLERS)

    executor.run_model(model_id="qwen-small", capabilities=("chat",), envelope=ENVELOPE)

    rows = [json.loads(line) for line in (tmp_path / "cases.jsonl").read_text().splitlines() if line.strip()]
    envelope = [row for row in rows if row["case_id"] == "B:qwen-small:envelope"][0]
    assert envelope["status"] == "failed"
    assert any("did not reach the declared boundary" in problem for problem in envelope["problems"])
    assert envelope["facts"]["observed"]["output_tokens"] == ENVELOPE.max_output_tokens  # what it really saw
    passed = [row for row in rows if row["case_id"] == "B:qwen-small:infer"][0]
    assert passed["status"] == "passed" and passed["failure"] is None and passed["problems"] == []


def test_a_crash_leaves_a_failed_attempt_and_a_cleanup_record(tmp_path) -> None:
    from model_scheduler.acceptance.collector import FileCollector

    collector = FileCollector(tmp_path, run_id="run-1", candidate_sha256="a" * 64, device_digest="b" * 64,
                              boot_id="boot-1")
    driver = FakeDriver()
    driver.fail_on = "image_url"  # only the vision capability case carries an image

    attempts = _run(driver, collector=collector)

    crashed = [attempt for attempt in attempts if attempt.status == "failed" and attempt.failure]
    assert crashed, "the crashing case must leave a failed attempt"
    for attempt in crashed:
        assert attempt.cleanup == {"stopped": True, "model_id": "qwen-small"}
    assert driver.cleanups  # cleanup was actually invoked through the official API
    failure_rows = [json.loads(line) for line in (tmp_path / "failures.jsonl").read_text().splitlines()]
    assert any(row["error"].startswith("RuntimeError") for row in failure_rows)
    assert (tmp_path / "cases.jsonl").is_file()  # every attempt is closed, passed or not


def test_a_failed_cleanup_is_itself_recorded() -> None:
    driver = FakeDriver()
    driver.fail_on = "image_url"
    driver.fail_cleanup = True

    attempts = _run(driver)

    crashed = [attempt for attempt in attempts if attempt.failure][0]
    assert crashed.cleanup is not None and crashed.cleanup["error"].startswith("RuntimeError")


def test_capability_outputs_are_checked_for_shape_and_finiteness() -> None:
    nan = FakeDriver()
    nan.nan_embeddings = True
    attempts = _run(nan, capabilities=("embeddings",))
    embeddings = [attempt for attempt in attempts if attempt.case_id.endswith(":cap:embeddings")][0]
    assert embeddings.status == "failed"
    assert any("non-finite" in problem for problem in embeddings.problems)

    empty = FakeDriver()
    empty.empty_vectors = True
    attempts = _run(empty, capabilities=("embeddings",))
    embeddings = [attempt for attempt in attempts if attempt.case_id.endswith(":cap:embeddings")][0]
    assert embeddings.status == "failed" and any("no vectors" in problem for problem in embeddings.problems)

    healthy = _run(FakeDriver(), capabilities=("embeddings", "rerank"))
    assert bc.summarize(healthy)["passed"] is True


def test_cancel_and_stop_need_real_evidence() -> None:
    unproven = FakeDriver()
    unproven.cancel_proof = False
    attempts = _run(unproven)
    cancel = [attempt for attempt in attempts if attempt.case_id.endswith(":cancel")][0]
    assert cancel.status == "unknown" and any("cancel" in problem for problem in cancel.problems)

    no_id = FakeDriver()
    no_id.no_execution_id = True
    attempts = _run(no_id)
    cancel = [attempt for attempt in attempts if attempt.case_id.endswith(":cancel")][0]
    assert cancel.status == "failed" and "execution id" in (cancel.failure or "")

    not_stopped = FakeDriver()
    not_stopped.stop_proven = False
    attempts = _run(not_stopped)
    reload_attempts = [attempt for attempt in attempts if attempt.case_id.endswith(":reload")]
    assert all(attempt.status == "failed" for attempt in reload_attempts)


def test_the_executor_refuses_fewer_than_the_acceptance_minimum() -> None:
    with pytest.raises(bc.BackendCaseError, match="three independent cold starts"):
        bc.CaseExecutor(FakeDriver(), cold_starts=2)
    with pytest.raises(bc.BackendCaseError, match="three full reload rounds"):
        bc.CaseExecutor(FakeDriver(), reload_rounds=2)


# ---------------------------------------------------------------------------
# fixtures


def test_capability_fixtures_are_deterministic_and_reach_the_declared_boundary() -> None:
    fixtures = fx.fixtures_for("qwen-small", ("chat", "vision", "embeddings", "rerank"), ENVELOPE, filler=FILLER)

    assert [fixture.capability for fixture in fixtures] == ["chat", "vision", "embeddings", "rerank"]
    chat = fixtures[0]
    assert chat.boundary == {"input_tokens": 512, "output_tokens": 64, "parallel": 2}
    # 512 minus the 19-token template and the 3-token instruction, plus its two words
    assert len(chat.payload["messages"][0]["content"].split()) == 492
    vision = fixtures[1]
    assert vision.boundary["images"] == 1 and vision.boundary["image_edge_pixels"] == 64
    assert sum(1 for item in vision.payload["messages"][0]["content"] if item["type"] == "image_url") == 1
    assert fx.fixture_bytes(chat) == fx.fixture_bytes(fx.fixtures_for("qwen-small", ("chat",), ENVELOPE,
                                                                      filler=FILLER)[0])


def test_a_text_boundary_is_built_from_the_measured_token_ratio() -> None:
    """7 tokens per unit is what the target tokenizer really costs: 512/7 units, not 512."""
    expensive = fx.FillerSpec(unit="tok000123", tokens_per_unit=7.0, template_overhead_tokens=19,
                              vision_template_overhead_tokens=50, instruction=INSTRUCTION,
                              instruction_tokens=INSTRUCTION_TOKENS)

    chat = fx.fixtures_for("qwen-small", ("chat",), ENVELOPE, filler=expensive)[0]

    # ceil((512 - 19 - 3) / 7) filler units plus the instruction's own words
    assert len(chat.payload["messages"][0]["content"].split()) == 72
    assert chat.boundary["input_tokens"] == ENVELOPE.max_input_tokens  # the declaration never moves


def test_the_run_reads_the_measured_ratio_from_the_frozen_material(tmp_path) -> None:
    """The candidate names the material; the run refuses anything else."""
    from model_scheduler.acceptance.runner import LayerError, load_filler_specs

    (tmp_path / "fillers.json").write_text(json.dumps({"schema_version": 1, "fillers": [
        {"model_id": "qwen-small", "unit": "a", "tokens_per_unit": 1.0, "template_overhead_tokens": 19,
         "vision_template_overhead_tokens": 50, "instruction": INSTRUCTION,
         "instruction_tokens": INSTRUCTION_TOKENS}]}), encoding="utf-8")

    class _Candidate:
        fixture_refs = []

    specs = load_filler_specs(tmp_path, _Candidate())

    assert specs["qwen-small"].unit == "a" and specs["qwen-small"].tokens_per_unit == 1.0

    class _Naming(_Candidate):
        fixture_refs = [type("Ref", (), {"relative_path": "gone.json", "size_bytes": 1, "sha256": "0" * 64})()]

    with pytest.raises(LayerError, match="is missing"):
        load_filler_specs(tmp_path, _Naming())

    (tmp_path / "fillers.json").write_text(json.dumps({"schema_version": 1, "fillers": [
        {"model_id": "qwen-small", "unit": "a", "tokens_per_unit": 0, "template_overhead_tokens": 19,
         "vision_template_overhead_tokens": 50, "instruction": INSTRUCTION,
         "instruction_tokens": INSTRUCTION_TOKENS}]}), encoding="utf-8")
    with pytest.raises(LayerError, match="measured positive number"):  # an assumed ratio is not a measurement
        load_filler_specs(tmp_path, _Candidate())


def test_a_boundary_without_a_measured_ratio_is_refused() -> None:
    with pytest.raises(fx.FixtureError, match="measured tokens-per-unit"):
        fx.fixtures_for("qwen-small", ("chat",), ENVELOPE)
    with pytest.raises(bc.BackendCaseError, match="measured tokens-per-unit"):
        bc.CaseExecutor(FakeDriver()).run_model(model_id="qwen-small", capabilities=("chat",), envelope=ENVELOPE)


def test_video_and_audio_quality_are_never_fixtures() -> None:
    for kind in ("video", "audio", "transcription", "voiceprint", "face"):
        with pytest.raises(fx.FixtureError, match="never claimed"):
            fx.fixtures_for("qwen-small", (kind,), ENVELOPE)


def test_fixture_material_carries_size_and_hash(tmp_path) -> None:
    fixtures = fx.fixtures_for("qwen-small", ("chat",), ENVELOPE, filler=FILLER)

    entries = fx.write_fixture_material(tmp_path, fixtures)

    entry = entries[0]
    path = tmp_path / entry["artifact"]["relative_path"]
    assert path.stat().st_size == entry["artifact"]["size_bytes"]
    assert __import__("hashlib").sha256(path.read_bytes()).hexdigest() == entry["artifact"]["sha256"]
    assert entry["fixture_id"] == "qwen-small-chat" and entry["capabilities"] == ["chat"]


def test_the_run_command_validates_layers_and_refuses_until_the_orchestration_exists(tmp_path, capsys) -> None:
    from model_scheduler.acceptance import EXIT_FAILED, EXIT_INPUT
    from model_scheduler.acceptance.__main__ import main

    root = str(tmp_path / "fixtures")
    assert main(["run", "--candidate", "c.json", "--layers", "B,Q", "--output", str(tmp_path / "a"),
                 "--fixtures-root", root]) == EXIT_INPUT
    assert main(["run", "--candidate", "c.json", "--layers", "B,B", "--output", str(tmp_path / "a"),
                 "--fixtures-root", root]) == EXIT_INPUT

    code = main(["run", "--candidate", "c.json", "--layers", "S,B", "--output", str(tmp_path / "b"),
                 "--fixtures-root", root])

    assert code == EXIT_FAILED  # an undelivered layer must not be executed, even partly
    assert "no orchestration yet" in capsys.readouterr().err
    assert not (tmp_path / "b" / "report.json").exists()

    # B alone is wired now: it refuses *before* touching anything when the site is not up.
    code = main(["run", "--candidate", "c.json", "--layers", "B", "--output", str(tmp_path / "c"),
                 "--fixtures-root", root])
    assert code == EXIT_INPUT
    assert "SMS_CONTROL_SOCKET" in capsys.readouterr().err
    assert not (tmp_path / "c" / "report.json").exists()


def test_a_model_without_a_usable_capability_is_refused() -> None:
    with pytest.raises(fx.FixtureError, match="no declared capability"):
        fx.fixtures_for("qwen-small", (), ENVELOPE)
    with pytest.raises(fx.FixtureError, match="no fixture is defined"):
        fx.fixtures_for("qwen-small", ("speech",), ENVELOPE)
