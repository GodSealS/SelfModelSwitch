"""v2 plan contracts. Strict wire schemas, not production integration code.

Requires Pydantic 2; model_json_schema() is the normative JSON Schema export.
All IDs and paths are deployment-controlled; untrusted file paths are never commands.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


Id = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Positive = Annotated[int, Field(gt=0)]
Nonnegative = Annotated[int, Field(ge=0)]
Seconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Capability = Literal["chat", "embeddings", "rerank", "transcribe", "face_detect", "face_embed", "depth", "vision"]
Scope = Literal["scheduler-only", "local-pipeline", "full-pipeline"]
StageId = Literal["probe", "shots", "asr", "face_tracks", "face_identity", "depth", "vision", "mask", "report"]
STAGES = ("probe", "shots", "asr", "face_tracks", "face_identity", "depth", "vision", "mask", "report")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @model_validator(mode="before")
    @classmethod
    def reject_boolean_version(cls, value):
        if isinstance(value, dict) and "schema_version" in value and type(value["schema_version"]) is not int:
            raise ValueError("schema_version must be a strict integer")
        return value


def safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and str(path) == value and ".." not in path.parts and "\\" not in value and "\x00" not in value and value != "."


class Asset(StrictModel):
    path: str
    role: Literal["weights", "projector", "config", "tokenizer", "engine", "support"]
    size_bytes: Positive
    sha256: Digest

    @model_validator(mode="after")
    def path_is_relative(self):
        if not safe_relative(self.path):
            raise ValueError("unsafe asset path")
        return self


class RuntimeSpec(StrictModel):
    runtime_id: Id
    kind: Literal["llama_cpp", "python_worker", "media_worker"]
    image: Annotated[str, Field(pattern=r"^[^@\s]+@sha256:[0-9a-f]{64}$")]
    adapter: Literal["llama", "moss_transformers", "yunet", "scrfd", "insightface", "depth_v2", "media"]
    lock_sha256: Digest
    adapter_sha256: Digest

    @model_validator(mode="after")
    def compatible_adapter(self):
        allowed = {"llama_cpp": {"llama"}, "media_worker": {"media"},
                   "python_worker": {"moss_transformers", "yunet", "scrfd", "insightface", "depth_v2"}}
        if self.adapter not in allowed[self.kind]:
            raise ValueError("runtime/adapter mismatch")
        return self


class Envelope(StrictModel):
    context_tokens: Positive
    max_output_tokens: Positive
    max_audio_ms: Positive
    max_images: Positive
    max_pixels_per_image: Positive
    batch_size: Positive
    concurrency: Positive

    @model_validator(mode="after")
    def context_fits(self):
        if self.max_output_tokens >= self.context_tokens:
            raise ValueError("output must leave room for input")
        return self


class ModelSpec(StrictModel):
    model_id: Id
    runtime_id: Id
    capabilities: Annotated[list[Capability], Field(min_length=1)]
    assets: Annotated[list[Asset], Field(min_length=1)]
    port: Annotated[int, Field(ge=10001, le=19999)]
    measured_peak_bytes: Positive
    execution_device: Literal["cuda", "cpu"]
    envelope: Envelope
    pinned: bool = False
    preload: bool = False
    ttl_seconds: Nonnegative = 0
    priority: Annotated[int, Field(ge=0, le=1000)] = 50

    @model_validator(mode="after")
    def unique_fields(self):
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("duplicate capabilities")
        if len({item.path for item in self.assets}) != len(self.assets):
            raise ValueError("duplicate assets")
        if "vision" in self.capabilities and not any(item.role == "projector" for item in self.assets):
            raise ValueError("vision requires projector")
        return self


class DeviceIdentity(StrictModel):
    machine_id_sha256: Digest
    device_tree_model: str
    compatible: Annotated[list[str], Field(min_length=1)]
    mem_total_bytes: Positive
    architecture: Literal["aarch64"]
    os_release: str
    kernel: str
    jetpack: str
    cuda_driver: str
    nvidia_container_runtime: str
    power_mode: str
    clocks_mode: str
    model_ssd_uuid: str
    work_ssd_uuid: str

    @model_validator(mode="after")
    def facts_nonempty(self):
        if any(isinstance(v, str) and not v.strip() for v in self.model_dump().values()) or any(not v.strip() for v in self.compatible):
            raise ValueError("empty device fact")
        return self


class StagePolicy(StrictModel):
    stage_id: StageId
    model_id: Id | None
    work_peak_bytes: Nonnegative
    unit_timeout_seconds: Seconds
    stage_timeout_seconds: Seconds

    @model_validator(mode="after")
    def time_order(self):
        if self.unit_timeout_seconds > self.stage_timeout_seconds:
            raise ValueError("unit timeout exceeds stage timeout")
        if (self.stage_id == "report") != (self.work_peak_bytes == 0):
            raise ValueError("report has no local reservation; local stages require positive work peak")
        return self


class PipelinePolicy(StrictModel):
    profile: Literal["balanced-v1"]
    roles: dict[Literal["asr", "face_detect", "face_embed", "depth", "vision"], Id]
    stages: Annotated[list[StagePolicy], Field(min_length=8, max_length=9)]
    quality_policy_sha256: Digest
    dataset_manifest_sha256: Digest
    full_film_sha256: Digest
    max_film_wall_seconds: Seconds
    max_work_bytes: Positive
    max_switch_seconds: Seconds
    external_adapter_sha256: Digest | None
    external_model: str | None
    external_prompt_sha256: Digest | None


class Candidate(StrictModel):
    schema_version: Literal[3]
    deployment_id: Id
    scope: Scope
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")]
    source_tree_sha256: Digest
    release_archive_sha256: Digest
    scheduler_config_sha256: Digest
    runner_config_sha256: Digest | None
    evaluator_sha256: Digest
    collector_sha256: Digest
    acceptance_policy_sha256: Digest
    llama_swap_sha256: Digest | None
    device: DeviceIdentity
    runtimes: Annotated[list[RuntimeSpec], Field(min_length=1)]
    models: Annotated[list[ModelSpec], Field(min_length=1)]
    media_runtime_id: Id | None
    media_port: Annotated[int, Field(ge=10001, le=19999)] | None
    model_budget_bytes: Positive
    system_reserve_bytes: Nonnegative = 8589934592
    free_floor_bytes: Nonnegative = 2147483648
    pipeline: PipelinePolicy | None

    @model_validator(mode="after")
    def cross_references(self):
        runtimes = {r.runtime_id: r for r in self.runtimes}
        models = {m.model_id: m for m in self.models}
        if len(runtimes) != len(self.runtimes) or len(models) != len(self.models):
            raise ValueError("duplicate model/runtime")
        if any(r.kind == "llama_cpp" for r in self.runtimes) != (self.llama_swap_sha256 is not None):
            raise ValueError("llama-swap hash required exactly when llama runtime exists")
        ports = [m.port for m in self.models] + ([self.media_port] if self.media_port is not None else [])
        if len(set(ports)) != len(ports):
            raise ValueError("duplicate port")
        for model in self.models:
            runtime = runtimes.get(model.runtime_id)
            if runtime is None or runtime.kind == "media_worker":
                raise ValueError("invalid model runtime")
            allowed = {"llama": {"chat", "vision", "embeddings", "rerank"}, "moss_transformers": {"transcribe"},
                       "yunet": {"face_detect"}, "scrfd": {"face_detect"}, "insightface": {"face_embed"}, "depth_v2": {"depth"}}
            if not set(model.capabilities) <= allowed[runtime.adapter]:
                raise ValueError("adapter capability mismatch")
            if (model.measured_peak_bytes * 115 + 99) // 100 + self.free_floor_bytes > self.device.mem_total_bytes:
                raise ValueError("model exceeds physical capacity")
            if (model.measured_peak_bytes * 115 + 99) // 100 > self.model_budget_bytes:
                raise ValueError("model exceeds managed budget")
        if self.model_budget_bytes > self.device.mem_total_bytes - self.system_reserve_bytes:
            raise ValueError("budget exceeds physical policy")
        if self.scope == "scheduler-only":
            if self.pipeline is not None or self.runner_config_sha256 is not None or self.media_runtime_id is not None or self.media_port is not None:
                raise ValueError("scheduler-only cannot claim pipeline")
            return self
        if self.pipeline is None or self.runner_config_sha256 is None or self.media_port is None:
            raise ValueError("pipeline configuration required")
        if self.media_runtime_id not in runtimes or runtimes[self.media_runtime_id].kind != "media_worker":
            raise ValueError("media runtime required")
        if any(m.pinned or m.preload or m.envelope.concurrency != 1 for m in self.models):
            raise ValueError("balanced-v1 requires unpinned serial models")
        required_roles = {"asr": "transcribe", "face_detect": "face_detect", "face_embed": "face_embed", "depth": "depth", "vision": "vision"}
        if set(self.pipeline.roles) != set(required_roles):
            raise ValueError("exact pipeline roles required")
        for role, capability in required_roles.items():
            model = models.get(self.pipeline.roles[role])
            if model is None or capability not in model.capabilities:
                raise ValueError("role capability mismatch")
        expected = list(STAGES if self.scope == "full-pipeline" else STAGES[:-1])
        if [s.stage_id for s in self.pipeline.stages] != expected:
            raise ValueError("stage ordering or scope mismatch")
        stage_roles = {"asr": "asr", "face_tracks": "face_detect", "face_identity": "face_embed", "depth": "depth", "vision": "vision"}
        for stage in self.pipeline.stages:
            expected_model = self.pipeline.roles[stage_roles[stage.stage_id]] if stage.stage_id in stage_roles else None
            if stage.model_id != expected_model:
                raise ValueError("stage role mismatch")
            if stage.stage_id != "report":
                model_peak = models[stage.model_id].measured_peak_bytes if stage.model_id else 0
                combined = (model_peak * 115 + 99) // 100 + (stage.work_peak_bytes * 115 + 99) // 100
                if combined > self.model_budget_bytes:
                    raise ValueError("stage exceeds managed budget")
        external = (self.pipeline.external_adapter_sha256, self.pipeline.external_model, self.pipeline.external_prompt_sha256)
        if self.scope == "full-pipeline" and any(x is None or x == "" for x in external):
            raise ValueError("external report configuration required")
        if self.scope == "local-pipeline" and any(x is not None for x in external):
            raise ValueError("local scope cannot call external report")
        return self


class PermitToken(StrictModel):
    boot_id: Id
    permit_id: Id
    job_id: Id
    stage_id: StageId
    attempt: Positive
    model_id: Id | None


class InputRef(StrictModel):
    kind: Literal["input", "artifact"]
    ref_id: Id
    sha256: Digest


class ExecutionRequest(StrictModel):
    token: PermitToken
    unit_id: Id
    attempt: Positive
    inputs: Annotated[list[InputRef], Field(min_length=1)]
    parameters_sha256: Digest
    # Parameters are immutable server-side stage config, never caller shell/URL.


class CreateJob(StrictModel):
    input_id: Id
    input_sha256: Digest
    profile: Literal["balanced-v1"]
    scope: Literal["local-pipeline", "full-pipeline"]
    candidate_sha256: Digest


class Span(StrictModel):
    start_ms: Nonnegative
    end_ms: Positive

    @model_validator(mode="after")
    def nonempty(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("empty/reversed span")
        return self


class Artifact(StrictModel):
    artifact_id: Id
    job_id: Id
    stage_id: StageId
    unit_id: Id
    attempt: Positive
    path: str
    media_type: str
    size_bytes: Positive
    sha256: Digest
    input_sha256: Digest
    parameters_sha256: Digest
    model_assets_sha256: Digest
    implementation_sha256: Digest

    @model_validator(mode="after")
    def relative(self):
        if not safe_relative(self.path):
            raise ValueError("unsafe artifact path")
        return self


class EvidenceRef(StrictModel):
    path: str
    sha256: Digest
    size_bytes: Positive

    @model_validator(mode="after")
    def relative(self):
        if not safe_relative(self.path):
            raise ValueError("unsafe evidence path")
        return self


class ScenarioRecord(StrictModel):
    scenario_id: str
    run_id: Id
    candidate_sha256: Digest
    started_at: str
    finished_at: str
    disposition: Literal["executed", "failed", "not_run", "skipped", "unknown"]
    evidence: Annotated[list[EvidenceRef], Field(min_length=1)]


class AcceptanceReport(StrictModel):
    schema_version: Literal[3]
    candidate_sha256: Digest
    device_identity_sha256: Digest
    source_tree_sha256: Digest
    evaluator_sha256: Digest
    collector_sha256: Digest
    scope: Scope
    started_at: str
    finished_at: str
    scenarios: list[ScenarioRecord]


class InstanceRef(StrictModel):
    boot_id: Id
    runtime_id: Id
    model_id: Id | None
    generation: Positive
    container_id: str
    started_at: str
    candidate_sha256: Digest


class Operation(StrictModel):
    operation_id: Id
    boot_id: Id
    generation: Positive
    runtime_id: Id
    model_id: Id | None
    candidate_sha256: Digest
    instance: InstanceRef | None


class Observation(StrictModel):
    operation_id: Id
    instance: InstanceRef | None
    presence: Literal["running", "stopped", "unknown"]
    ready: bool
    launch_finished: bool
    port_closed: bool
    sampled_monotonic: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    error_code: str | None


class ArtifactDraft(StrictModel):
    path: str
    media_type: str
    size_bytes: Positive
    sha256: Digest

    @model_validator(mode="after")
    def relative(self):
        if not safe_relative(self.path):
            raise ValueError("unsafe draft path")
        return self


class ExecutionView(StrictModel):
    execution_id: Id
    token: PermitToken
    unit_id: Id
    attempt: Positive
    state: Literal["queued", "running", "cancelling", "succeeded", "failed", "cancelled", "unknown"]
    outputs: list[ArtifactDraft]
    error_code: str | None
    compute_quiescent: bool

    @model_validator(mode="after")
    def terminal(self):
        if self.state in {"succeeded", "failed", "cancelled"} and not self.compute_quiescent:
            raise ValueError("terminal compute not quiescent")
        if self.state != "succeeded" and self.outputs:
            raise ValueError("non-success cannot publish output")
        return self


class ResumeJob(StrictModel):
    allow_external_retry: bool = False


class JobView(StrictModel):
    job_id: Id
    state: Literal["queued", "running", "paused", "failed", "cancelling", "cancelled", "succeeded"]
    stage_id: StageId | None
    completed_units: Nonnegative
    total_units: Nonnegative | None
    attempt: Positive
    candidate_sha256: Digest
    input_sha256: Digest
    scope: Literal["local-pipeline", "full-pipeline"]
    created_at: str
    updated_at: str
    error_code: str | None
    quality_status: Literal["unchecked", "passed", "failed"]


class ModelSLO(StrictModel):
    cold_load_seconds: Seconds
    inference_seconds: Seconds
    envelope_seconds: Seconds
    p95_seconds: Seconds
    p99_seconds: Seconds

    @model_validator(mode="after")
    def quantiles_ordered(self):
        if self.p95_seconds > self.p99_seconds:
            raise ValueError("p95 threshold exceeds p99")
        return self


class Arrival(StrictModel):
    at_ms: Nonnegative
    model_id: Id
    fixture_sha256: Digest


class AcceptancePolicy(StrictModel):
    schema_version: Literal[1]
    model_slos: dict[Id, ModelSLO]
    sample_interval_ms: Annotated[int, Field(ge=10, le=100)] = 100
    max_sample_gap_ms: Annotated[int, Field(ge=100, le=500)] = 500
    baseline_seconds: Annotated[int, Field(ge=10)] = 10
    repeats: Annotated[int, Field(ge=3)] = 3
    soak_seconds: Annotated[int, Field(ge=1800)] = 1800
    arrivals: Annotated[list[Arrival], Field(min_length=100)]
    max_429_fraction: Annotated[float, Field(ge=0, le=0.1, allow_inf_nan=False)]
    max_504_fraction: Annotated[float, Field(ge=0, le=0.1, allow_inf_nan=False)]

    @model_validator(mode="after")
    def schedule_valid(self):
        if not self.model_slos or self.max_sample_gap_ms < self.sample_interval_ms:
            raise ValueError("invalid SLO or sampling policy")
        times = [a.at_ms for a in self.arrivals]
        if times != sorted(times) or times[-1] >= self.soak_seconds * 1000:
            raise ValueError("invalid arrival timing")
        if max(b - a for a, b in zip([0] + times, times + [self.soak_seconds * 1000])) > 15000:
            raise ValueError("soak cannot contain idle gaps over 15 seconds")
        if any(a.model_id not in self.model_slos for a in self.arrivals):
            raise ValueError("unknown arrival model")
        if any(sum(a.model_id == mid for a in self.arrivals) < 3 for mid in self.model_slos):
            raise ValueError("each model needs at least three soak arrivals")
        return self


class BackendPort(Protocol):
    async def load(self, operation: Operation, deadline: float) -> Observation: ...
    async def observe(self, operation: Operation) -> Observation: ...
    async def execute(self, request: ExecutionRequest, deadline: float) -> ExecutionView: ...
    async def poll(self, execution_id: str, token: PermitToken) -> ExecutionView: ...
    async def cancel(self, execution_id: str, token: PermitToken, deadline: float) -> ExecutionView: ...
    async def stop(self, operation: Operation, deadline: float) -> Observation: ...
