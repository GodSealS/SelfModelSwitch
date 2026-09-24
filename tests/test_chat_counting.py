"""TC05/CT05: the lease-bound count, its receipt and its identity checks.

The counter is the only place the compat route learns a token count. These tests
pin the whole contract without a runtime: the template projection, the receipt
arithmetic, the identity/generation checks (entry and repeated), the deadline
mapping and the refusal of anything that cannot be proven — never a zero, never an
estimate, never a retry.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from model_scheduler.chat_counting import (
    ChatCountingError,
    CountReceipt,
    RuntimeChatCounter,
    RuntimeChatPolicy,
    resolve_policy,
    template_request,
    validate_receipt,
)
from model_scheduler.contracts import Lease
from model_scheduler.contracts_v2 import Envelope
from model_scheduler.control_protocol_v1 import InstanceIdentity
from model_scheduler.envelope_validator import prepare_chat

MODEL = "qwen-small"
IMAGE_DIGEST = "sms-llama-cpp@sha256:" + "a" * 64
ENVELOPE = Envelope(ctx_size=8192, max_input_tokens=4096, max_output_tokens=1024, max_parallel=1,
                    max_image_tokens=1280, max_image_edge_pixels=1024, max_images=1)


def _policy(**overrides) -> RuntimeChatPolicy:
    policy = RuntimeChatPolicy(
        policy_id="llama-cpp-4bc272f-qwen-small",
        profile_id="llama-cpp-gguf-v1",
        image_digest=IMAGE_DIGEST,
        model_sha256="b" * 64,
        template_sha256="c" * 64,
        source_revision="4bc272f",
        recognized_output_fields=frozenset({"max_tokens", "max_completion_tokens", "n_predict"}),
        supported_output_fields=frozenset({"max_tokens", "max_completion_tokens"}),
        effort_values=frozenset({"none", "low"}),
        denied_template_fields=frozenset({"chat_template"}),
        template_request_fields=frozenset({"messages", "tools", "tool_choice", "parallel_tool_calls",
                                           "reasoning_effort"}),
    )
    return replace(policy, **overrides) if overrides else policy


def _prepared(policy: RuntimeChatPolicy, **body):
    payload = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], **body}
    return prepare_chat(payload, capabilities={"chat", "vision", "tools", "thinking"},
                        envelope=ENVELOPE, policy=policy)


def _lease(generation: int = 1) -> Lease:
    return Lease("lease-1", "request-1", MODEL, generation)


def _instance(model_id: str = MODEL, *, container_id: str = "container-1") -> InstanceIdentity:
    return InstanceIdentity(container_id=container_id, started_at="2026-09-24T00:00:00Z", deployment_id="orin-lab",
                            model_id=model_id, runtime_id="llama-cpp-1", candidate_digest="d" * 64,
                            image_digest=IMAGE_DIGEST)


class TemplateRuntime:
    """The adapter primitive the counter drives: one template, one tokenizer."""

    def __init__(self, *, tokens: int = 11, error: Exception | None = None) -> None:
        self.tokens, self.error = tokens, error
        self.calls: list[tuple[dict, dict]] = []

    async def count_template(self, template, *, tokenize_options=None, deadline=None):
        self.calls.append((dict(template), dict(tokenize_options or {})))
        if self.error is not None:
            raise self.error
        return self.tokens


class AdapterFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _counter(policy: RuntimeChatPolicy | None = None, *, template: TemplateRuntime | None = None,
             instance=None, generation: int = 1, envelope=ENVELOPE):
    policy = policy or _policy()
    template = template or TemplateRuntime()
    accepted = {"value": _instance() if instance is None else instance}
    counter = RuntimeChatCounter(policies={MODEL: policy}, templates={MODEL: template},
                                 envelopes={MODEL: envelope},
                                 accepted_identity=lambda model_id: accepted["value"],
                                 generation=lambda model_id: generation)
    return counter, template, accepted


# --------------------------------------------------------------------------- the policy and its registry


def test_a_policy_rejects_unpinned_identity_and_non_canonical_json() -> None:
    with pytest.raises(ValueError):
        _policy(image_digest="sms-llama-cpp:latest")
    with pytest.raises(ValueError):
        _policy(template_sha256="C" * 64)
    with pytest.raises(ValueError):
        _policy(supported_output_fields=frozenset({"n_predict"}))  # max_tokens is required
    with pytest.raises(ValueError):
        _policy(recognized_output_fields=frozenset({"max_tokens"}))  # supported must be a subset
    with pytest.raises(ValueError):
        _policy(tokenize_options_json=b'{"add_special": true, "x": 1}')  # not canonical (key order)
    with pytest.raises(ValueError):
        _policy(template_request_constants_json=b'{"tools": []}')  # overlaps a request field


def test_policy_registry_resolution_is_keyed_and_fail_closed() -> None:
    policy = _policy()
    registry = {("llama-cpp-gguf-v1", IMAGE_DIGEST, "b" * 64): policy}

    assert resolve_policy(profile_id="llama-cpp-gguf-v1", image_digest=IMAGE_DIGEST,
                          model_sha256="b" * 64, registry=registry) is policy
    with pytest.raises(ChatCountingError) as refused:
        resolve_policy(profile_id="llama-cpp-gguf-v1", image_digest=IMAGE_DIGEST,
                       model_sha256="e" * 64, registry=registry)
    assert refused.value.code == "service_unavailable"


def test_the_registered_lab_policies_are_the_ct01_material() -> None:
    from model_scheduler.chat_counting import POLICY_REGISTRY

    assert len(POLICY_REGISTRY) == 2
    for key, policy in POLICY_REGISTRY.items():
        assert key == (policy.profile_id, policy.image_digest, policy.model_sha256)
        assert policy.effort_values == frozenset()  # CT01 D06 did not run: never a guessed value
        assert policy.template_sha256 in {
            "a0bc6f6fc7a29a80017a433e8f03a1cc1236e838a944a2d034295a60c4f2fddb",
            "e84f32a23fdda27689f868aa4a1a5621f41133e51a48d7f3efcbea2839574259",
        }


# --------------------------------------------------------------------------- the template projection


def test_the_template_request_projects_only_the_policy_fields_and_constants() -> None:
    # A constant never overlaps a request field (TC01): this one adds a verified
    # server-side constant that the request itself cannot carry.
    policy = _policy(template_request_constants_json=b'{"add_generation_prompt":true}')
    request = _prepared(policy, tools=[{"type": "function", "function": {"name": "f"}}],
                        parallel_tool_calls=False, temperature=0.5, custom_extension={"keep": 1})
    projection = template_request(request, policy)

    assert projection["messages"] == [{"role": "user", "content": "hi"}]
    assert projection["tools"] == [{"type": "function", "function": {"name": "f"}}]
    assert projection["parallel_tool_calls"] is False  # taken from the request, unchanged
    assert projection["add_generation_prompt"] is True  # the policy's own constant
    assert "temperature" not in projection and "custom_extension" not in projection
    assert "max_tokens" not in projection  # the output budget never enters the template contract


def test_an_absent_template_field_is_not_supplied() -> None:
    policy = _policy()
    projection = template_request(_prepared(policy), policy)
    assert set(projection) == {"messages"}


# --------------------------------------------------------------------------- the receipt


def _receipt(request, policy, **overrides) -> CountReceipt:
    receipt = CountReceipt(body_sha256=request.body_sha256, policy_id=policy.policy_id, generation=1,
                           instance=_instance(), template_sha256=policy.template_sha256,
                           template_tokens=11, image_charge=0, charged_input_tokens=11)
    return replace(receipt, **overrides) if overrides else receipt


def test_a_receipt_must_bind_the_whole_request() -> None:
    policy = _policy()
    request = _prepared(policy)
    lease = _lease()
    validate_receipt(_receipt(request, policy), lease=lease, request=request, policy=policy)

    for broken in (
        {"body_sha256": "e" * 64},
        {"policy_id": "another-policy"},
        {"generation": 2},
        {"template_sha256": "f" * 64},
        {"instance": _instance("other-model")},
        {"instance": "not-an-identity"},
        {"template_tokens": "11"},
        {"image_charge": -1},
        {"charged_input_tokens": 12},
        {"template_tokens": True},
    ):
        with pytest.raises(ChatCountingError) as refused:
            validate_receipt(_receipt(request, policy, **broken), lease=lease, request=request, policy=policy)
        assert refused.value.code == "service_unavailable"
    with pytest.raises(ChatCountingError):
        validate_receipt("not-a-receipt", lease=lease, request=request, policy=policy)


# --------------------------------------------------------------------------- the count itself


@pytest.mark.asyncio
async def test_the_count_projects_the_template_and_charges_the_images() -> None:
    policy = _policy()
    counter, template, _ = _counter(policy)
    request = _prepared(policy)
    receipt = await counter.count(_lease(), request, deadline=123.0)

    assert template.calls == [({"messages": [{"role": "user", "content": "hi"}]}, {})]
    assert receipt.body_sha256 == request.body_sha256 and receipt.policy_id == policy.policy_id
    assert receipt.template_sha256 == policy.template_sha256 and receipt.generation == 1
    assert receipt.instance.model_id == MODEL
    assert receipt.template_tokens == 11 and receipt.image_charge == 0
    assert receipt.charged_input_tokens == 11


#: One real 1x1 PNG: the image scan decodes the bytes, never the file size.
_ONE_PIXEL_PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
                  "hKmMIQAAAABJRU5ErkJggg==")


@pytest.mark.asyncio
async def test_the_image_charge_is_the_envelope_charge_not_the_real_count() -> None:
    policy = _policy()
    counter, template, _ = _counter(policy, template=TemplateRuntime(tokens=3))
    request = _prepared(policy, messages=[{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": _ONE_PIXEL_PNG}}]}])
    receipt = await counter.count(_lease(), request, deadline=1.0)

    assert request.image_count == 1
    # a picture is charged at the registered worst case, never underestimated
    assert receipt.image_charge == ENVELOPE.max_image_tokens
    assert receipt.charged_input_tokens == 3 + ENVELOPE.max_image_tokens


@pytest.mark.asyncio
async def test_a_changed_instance_or_generation_refuses_the_count() -> None:
    policy = _policy()
    counter, _, accepted = _counter(policy)
    request = _prepared(policy)

    accepted["value"] = None
    with pytest.raises(ChatCountingError):
        await counter.count(_lease(), request, deadline=1.0)

    accepted["value"] = _instance("other-model")
    with pytest.raises(ChatCountingError):
        await counter.count(_lease(), request, deadline=1.0)

    accepted["value"] = _instance()
    stale, _, _ = _counter(policy, instance=_instance(), generation=7)
    with pytest.raises(ChatCountingError):
        await stale.count(_lease(), request, deadline=1.0)


@pytest.mark.asyncio
async def test_a_switch_during_the_count_invalidates_it_instead_of_retrying() -> None:
    policy = _policy()

    class SwitchingTemplate(TemplateRuntime):
        def __init__(self, accepted):
            super().__init__()
            self._accepted = accepted

        async def count_template(self, template, *, tokenize_options=None, deadline=None):
            self._accepted["value"] = _instance(container_id="container-2")  # the instance changed mid-count
            return await super().count_template(template, tokenize_options=tokenize_options, deadline=deadline)

    accepted = {"value": _instance()}
    template = SwitchingTemplate(accepted)
    counter = RuntimeChatCounter(policies={MODEL: _policy()}, templates={MODEL: template}, envelopes={MODEL: ENVELOPE},
                                 accepted_identity=lambda model_id: accepted["value"],
                                 generation=lambda model_id: 1)
    with pytest.raises(ChatCountingError):
        await counter.count(_lease(), _prepared(policy), deadline=1.0)
    assert len(template.calls) == 1  # counted once, refused, never retried


@pytest.mark.asyncio
async def test_a_count_failure_maps_to_service_or_timeout(monkeypatch) -> None:
    policy = _policy()
    counter, _, _ = _counter(policy, template=TemplateRuntime(error=AdapterFailure("execution_timeout")))
    with pytest.raises(ChatCountingError) as refused:
        await counter.count(_lease(), _prepared(policy), deadline=1.0)
    assert refused.value.code == "inference_timeout"

    counter, _, _ = _counter(policy, template=TemplateRuntime(error=AdapterFailure("backend_failed")))
    with pytest.raises(ChatCountingError) as refused:
        await counter.count(_lease(), _prepared(policy), deadline=1.0)
    assert refused.value.code == "service_unavailable"

    counter, _, _ = _counter(policy, template=TemplateRuntime(tokens="11"))
    with pytest.raises(ChatCountingError) as refused:
        await counter.count(_lease(), _prepared(policy), deadline=1.0)
    assert refused.value.code == "service_unavailable"


@pytest.mark.asyncio
async def test_the_count_refuses_a_request_prepared_under_another_policy_or_model() -> None:
    policy = _policy()
    counter, _, _ = _counter(policy)

    with pytest.raises(ChatCountingError):
        await counter.count(Lease("lease-1", "request-1", "other-model", 1), _prepared(policy), deadline=1.0)

    other = _policy(policy_id="another-policy")
    with pytest.raises(ChatCountingError):
        await counter.count(_lease(), _prepared(other), deadline=1.0)


@pytest.mark.asyncio
async def test_validate_binding_refuses_a_changed_identity_after_the_count() -> None:
    policy = _policy()
    counter, _, accepted = _counter(policy)
    request = _prepared(policy)
    lease = _lease()
    receipt = await counter.count(lease, request, deadline=1.0)

    await counter.validate_binding(lease, receipt, deadline=1.0)  # unchanged: passes

    accepted["value"] = _instance(container_id="container-2")
    with pytest.raises(ChatCountingError) as refused:
        await counter.validate_binding(lease, receipt, deadline=1.0)
    assert refused.value.code == "service_unavailable"

    accepted["value"] = _instance()
    with pytest.raises(ChatCountingError):
        await counter.validate_binding(_lease(generation=2), receipt, deadline=1.0)
