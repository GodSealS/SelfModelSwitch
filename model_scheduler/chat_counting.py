"""TC05/CT05: the runtime-side chat counter the compatibility route binds to one lease.

The compat route never counts before it holds a lease (TC06): a cold model has no
tokenizer, and a count taken outside the lease could belong to another instance.
This module owns the count itself:

* the template request is the *projection* of the prepared body onto the policy's
  `template_request_fields` plus its fixed constants — never the whole body and
  never a locally invented prompt;
* the projected request goes through the runtime's own `/apply-template`, and the
  prompt through `/tokenize` with the policy's tokenize options (special tokens and
  the generation prefix are the tokenizer's business, not an estimate's);
* the result is a `CountReceipt` that binds the body digest, the policy, the lease
  generation, the accepted instance and the template hash;
* a receipt that cannot be validated — or an instance or generation that changed
  between the count and the dispatch — refuses the request, and nothing here ever
  retries or degrades to an estimate.

The policies themselves are versioned code constants keyed by
`(profile_id, image_digest, model_sha256)` (TC01). A combination without an entry
is refused, never guessed.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol

from .contracts import Lease
from .contracts_v2 import GGUF_PROFILE, canonical_json_bytes
from .control_protocol_v1 import InstanceIdentity
from .envelope_validator import PreparedChat

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_DIGEST_RE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
_NUL = "\x00"

#: The fixed image the lab deployment runs (CT00/CT01 material).
LAB_IMAGE_DIGEST = "sms-llama-cpp@sha256:8e572bb99c19defa9218f8c07b7ab30379040f3ead87d64b3e241213597c1bc8"
_LAB_SOURCE_REVISION = "4bc272f"

#: CT01 D09: the template/parse overrides the fixed image forwards. A model that
#: registers tools/thinking refuses all of them (TC03).
_LAB_DENIED_TEMPLATE_FIELDS = frozenset(
    {"chat_template", "chat_template_kwargs", "generation_prompt", "parse_tool_calls", "reasoning_format"}
)
#: CT01 D04/D09: the fields the fixed image answers `apply-template` with. A field
#: absent from the request is not sent; unknown template parameters are refused.
_LAB_TEMPLATE_REQUEST_FIELDS = frozenset(
    {"messages", "tools", "tool_choice", "parallel_tool_calls", "reasoning_effort"}
)
_OUTPUT_BUDGET_FIELDS = frozenset({"max_tokens", "max_completion_tokens", "n_predict"})


class ChatCountingError(RuntimeError):
    """A count that could not be proven (TC05). `code` is the route's own vocabulary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code  # "service_unavailable" | "inference_timeout"


def _unavailable(message: str) -> ChatCountingError:
    return ChatCountingError("service_unavailable", message)


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or _NUL in value:
        raise ValueError(f"{where}: must be a non-empty string without NUL")
    return value


def _sha256(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{where}: must be a lowercase 64-hex SHA-256")
    return value


def _json_object_bytes(raw: Any, where: str) -> bytes:
    """The canonical JSON object bytes a policy declares (TC02 serialization)."""
    if not isinstance(raw, bytes):
        raise ValueError(f"{where}: must be canonical JSON object bytes")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{where}: must be a JSON object") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{where}: must be a JSON object")
    if canonical_json_bytes(document) != raw:
        raise ValueError(f"{where}: must be canonical JSON (sorted keys, compact, UTF-8)")
    return raw


@dataclass(frozen=True)
class RuntimeChatPolicy:
    """One versioned runtime chat policy (TC01), keyed by `(profile_id, image_digest, model_sha256)`."""

    policy_id: str
    profile_id: str
    image_digest: str
    model_sha256: str
    template_sha256: str
    source_revision: str
    recognized_output_fields: frozenset[str]
    supported_output_fields: frozenset[str]
    effort_values: frozenset[str]
    denied_template_fields: frozenset[str]
    template_request_fields: frozenset[str]
    template_request_constants_json: bytes = b"{}"
    tokenize_options_json: bytes = b"{}"

    def __post_init__(self) -> None:
        _text(self.policy_id, "policy_id")
        _text(self.profile_id, "profile_id")
        _text(self.source_revision, "source_revision")
        if not isinstance(self.image_digest, str) or not _IMAGE_DIGEST_RE.fullmatch(self.image_digest):
            raise ValueError("image_digest: must be a pinned `reference@sha256:<64hex>` digest")
        _sha256(self.model_sha256, "model_sha256")
        _sha256(self.template_sha256, "template_sha256")
        for name, value in (("recognized_output_fields", self.recognized_output_fields),
                            ("supported_output_fields", self.supported_output_fields),
                            ("effort_values", self.effort_values),
                            ("denied_template_fields", self.denied_template_fields),
                            ("template_request_fields", self.template_request_fields)):
            if not isinstance(value, frozenset) or any(not isinstance(item, str) or not item for item in value):
                raise ValueError(f"{name}: must be a frozenset of non-empty strings")
        if "max_tokens" not in self.supported_output_fields:
            raise ValueError("supported_output_fields: max_tokens is required (TC01)")
        if not self.supported_output_fields <= self.recognized_output_fields:
            raise ValueError("supported_output_fields: must be a subset of recognized_output_fields (TC01)")
        if "messages" not in self.template_request_fields:
            raise ValueError("template_request_fields: messages is required (TC01)")
        _json_object_bytes(self.template_request_constants_json, "template_request_constants_json")
        _json_object_bytes(self.tokenize_options_json, "tokenize_options_json")
        overlap = sorted(set(self._constants()) & self.template_request_fields)
        if overlap:
            raise ValueError(f"template_request_constants_json: overlaps the request fields: {overlap}")

    def _constants(self) -> Mapping[str, Any]:
        return json.loads(self.template_request_constants_json.decode("utf-8"))

    def tokenize_options(self) -> Mapping[str, Any]:
        return json.loads(self.tokenize_options_json.decode("utf-8"))


def _lab_policy(*, model_sha256: str, template_sha256: str, model_id: str) -> RuntimeChatPolicy:
    return RuntimeChatPolicy(
        policy_id=f"llama-cpp-{_LAB_SOURCE_REVISION}-{model_id}",
        profile_id=GGUF_PROFILE,
        image_digest=LAB_IMAGE_DIGEST,
        model_sha256=model_sha256,
        template_sha256=template_sha256,
        source_revision=_LAB_SOURCE_REVISION,
        recognized_output_fields=_OUTPUT_BUDGET_FIELDS,
        supported_output_fields=_OUTPUT_BUDGET_FIELDS,
        # CT01 D06 did not run: the allowed effort values are NOT established. An
        # empty set refuses an explicit effort instead of forwarding it, and it is
        # not a claim about the image; CT06 must register the probed set.
        effort_values=frozenset(),
        denied_template_fields=_LAB_DENIED_TEMPLATE_FIELDS,
        template_request_fields=_LAB_TEMPLATE_REQUEST_FIELDS,
        # D04 matched the chat usage with the server's default tokenize options and
        # D09 identified no extra constants, so both stay empty (not guessed).
        template_request_constants_json=b"{}",
        tokenize_options_json=b"{}",
    )


#: The versioned policy registry (TC01): `(profile_id, image_digest, model_sha256)`
#: -> policy. The two lab entries carry CT01's material (template hashes from the
#: GGUF metadata, output fields from probe-e, D09's template fields). CT06 extends
#: this registry for the new profile and its 27B runtime; a combination without an
#: entry is a refused startup, never a guessed policy.
POLICY_REGISTRY: Mapping[tuple[str, str, str], RuntimeChatPolicy] = {
    (GGUF_PROFILE, LAB_IMAGE_DIGEST, "3f4513330aa7f109922bd701d773575484ae2b4a4090d6511260a2a4f8e3d069"):
        _lab_policy(model_id="qwen25vl-7b",
                    model_sha256="3f4513330aa7f109922bd701d773575484ae2b4a4090d6511260a2a4f8e3d069",
                    template_sha256="a0bc6f6fc7a29a80017a433e8f03a1cc1236e838a944a2d034295a60c4f2fddb"),
    (GGUF_PROFILE, LAB_IMAGE_DIGEST, "f1e1b337fda4ec8e39974f69a385f970381488e9bb41d039d85959aeb5350457"):
        _lab_policy(model_id="qwen36-27b",
                    model_sha256="f1e1b337fda4ec8e39974f69a385f970381488e9bb41d039d85959aeb5350457",
                    template_sha256="e84f32a23fdda27689f868aa4a1a5621f41133e51a48d7f3efcbea2839574259"),
}


def policy_source_digest(*, registry: Mapping[tuple[str, str, str], RuntimeChatPolicy] = POLICY_REGISTRY) -> str:
    """One digest for the policy source: the version plus every registered policy.

    CT09 freezes this as `policy_source_sha256`, so a later run can prove it
    counted with the same policy code and values instead of a look-alike.
    """
    entries = []
    for policy in sorted(registry.values(), key=lambda item: item.policy_id):
        document = asdict(policy)
        for key, value in document.items():
            if isinstance(value, frozenset):
                document[key] = sorted(value)
            elif isinstance(value, bytes):
                document[key] = value.decode("utf-8")
        entries.append(document)
    payload = {"source_revision": _LAB_SOURCE_REVISION, "policies": entries}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_policy(*, profile_id: str, image_digest: str, model_sha256: str,
                   registry: Mapping[tuple[str, str, str], RuntimeChatPolicy] = POLICY_REGISTRY) -> RuntimeChatPolicy:
    """The one policy for a registered runtime/model combination, or a refusal (TC01)."""
    key = (profile_id, image_digest, model_sha256)
    policy = registry.get(key)
    if policy is None:
        raise _unavailable(f"no runtime chat policy is registered for {key!r}")
    if (policy.profile_id, policy.image_digest, policy.model_sha256) != key:
        raise _unavailable("the registered policy does not match its own lookup key")
    return policy


@dataclass(frozen=True)
class CountReceipt:
    """What one count proves (TC05): the body, the policy, the identity and the tokens."""

    body_sha256: str
    policy_id: str
    generation: int
    instance: InstanceIdentity
    template_sha256: str
    template_tokens: int
    image_charge: int
    charged_input_tokens: int


def validate_receipt(receipt: Any, *, lease: Lease, request: PreparedChat, policy: RuntimeChatPolicy) -> None:
    """A receipt may only authorise a generation while it binds this exact request.

    A wrong type, hash, generation, template or instance is a refused count — never
    a zero or an estimate (TC05).
    """
    if not isinstance(receipt, CountReceipt):
        raise _unavailable("the counter returned no receipt")
    if receipt.body_sha256 != request.body_sha256:
        raise _unavailable("the receipt was taken for another request body")
    if receipt.policy_id != policy.policy_id or request.policy_id != policy.policy_id:
        raise _unavailable("the receipt was taken under another policy")
    if receipt.generation != lease.generation:
        raise _unavailable("the receipt was taken under another generation")
    if receipt.template_sha256 != policy.template_sha256:
        raise _unavailable("the receipt was taken against another template")
    instance = receipt.instance
    if not isinstance(instance, InstanceIdentity) or instance.model_id != lease.model_id:
        raise _unavailable("the receipt does not carry this model's instance")
    for name in ("template_tokens", "image_charge", "charged_input_tokens"):
        value = getattr(receipt, name)
        if type(value) is not int or value < 0:
            raise _unavailable(f"the receipt's {name} is not a token count")
    if receipt.charged_input_tokens != receipt.template_tokens + receipt.image_charge:
        raise _unavailable("the receipt's charged input does not add up")


def template_request(request: PreparedChat, policy: RuntimeChatPolicy) -> dict:
    """The template request: the body's policy fields, plus the policy's constants (TC05)."""
    body = request.decoded_body()
    projection = {key: body[key] for key in sorted(policy.template_request_fields) if key in body}
    constants = policy._constants()
    projection.update(constants)
    return projection


class ChatCountPort(Protocol):
    """What the compat route needs from a counter (TC05)."""

    async def count(self, lease: Lease, request: PreparedChat, *, deadline: float) -> CountReceipt: ...

    async def validate_binding(self, lease: Lease, receipt: CountReceipt, *, deadline: float) -> None: ...


class RuntimeChatCounter:
    """One deployment-wide counter: per-model policy, template primitive and the book's identity.

    `templates` are the runtime adapters (anything with `count_template`), `envelopes`
    give each model's `max_image_tokens`, and the two lookups read the book, which
    stays the single owner of what identity was accepted for which generation.
    """

    def __init__(self, *,
                 policies: Mapping[str, RuntimeChatPolicy],
                 templates: Mapping[str, Any],
                 envelopes: Mapping[str, Any],
                 accepted_identity: Callable[[str], InstanceIdentity | None],
                 generation: Callable[[str], int]) -> None:
        self._policies = dict(policies)
        self._templates = dict(templates)
        self._envelopes = dict(envelopes)
        self._accepted_identity = accepted_identity
        self._generation = generation

    def _policy_for(self, lease: Lease, request: PreparedChat) -> RuntimeChatPolicy:
        policy = self._policies.get(lease.model_id)
        if policy is None:
            raise _unavailable(f"no runtime chat policy is bound to {lease.model_id!r}")
        if request.model_id != lease.model_id:
            raise _unavailable("the request does not belong to the leased model")
        if request.policy_id != policy.policy_id:
            raise _unavailable("the request was prepared under another policy")
        return policy

    def _require_instance(self, lease: Lease, policy: RuntimeChatPolicy,
                          expected: InstanceIdentity | None = None) -> InstanceIdentity:
        instance = self._accepted_identity(lease.model_id)
        if not isinstance(instance, InstanceIdentity):
            raise _unavailable("no accepted instance is bound to this lease")
        if instance.model_id != lease.model_id:
            raise _unavailable("the accepted instance belongs to another model")
        if instance.image_digest != policy.image_digest:
            raise _unavailable("the accepted instance does not run the policy's image")
        if self._generation(lease.model_id) != lease.generation:
            raise _unavailable("the lease generation is no longer the accepted one")
        if expected is not None and instance != expected:
            raise _unavailable("the accepted instance changed during the count")
        return instance

    def _charge(self, request: PreparedChat) -> int:
        envelope = self._envelopes.get(request.model_id)
        if envelope is None:
            raise _unavailable(f"no envelope is bound to {request.model_id!r}")
        return request.image_count * envelope.max_image_tokens

    async def count(self, lease: Lease, request: PreparedChat, *, deadline: float) -> CountReceipt:
        """Count the projected template under the lease, then re-verify the same identity."""
        policy = self._policy_for(lease, request)
        instance = self._require_instance(lease, policy)
        template = self._templates.get(lease.model_id)
        if template is None or not callable(getattr(template, "count_template", None)):
            raise _unavailable(f"no template runtime is bound to {lease.model_id!r}")
        projection = template_request(request, policy)
        try:
            template_tokens = await template.count_template(projection, tokenize_options=policy.tokenize_options(),
                                                            deadline=deadline)
        except ChatCountingError:
            raise
        except Exception as exc:  # a dead runtime, a bad response, an elapsed deadline
            raise _counting_failed(exc) from exc
        if type(template_tokens) is not int or template_tokens < 0:
            raise _unavailable("the runtime returned no usable token count")
        # The count is only usable while the books still accept the same instance:
        # a switch during the count invalidates it instead of being retried (TC05).
        self._require_instance(lease, policy, expected=instance)
        image_charge = self._charge(request)
        receipt = CountReceipt(
            body_sha256=request.body_sha256,
            policy_id=policy.policy_id,
            generation=lease.generation,
            instance=instance,
            template_sha256=policy.template_sha256,
            template_tokens=template_tokens,
            image_charge=image_charge,
            charged_input_tokens=template_tokens + image_charge,
        )
        validate_receipt(receipt, lease=lease, request=request, policy=policy)
        return receipt

    async def validate_binding(self, lease: Lease, receipt: CountReceipt, *, deadline: float) -> None:
        """The last check before the dispatch: the same instance and generation, or a refusal."""
        del deadline  # the identity check is local bookkeeping; the request's deadline already bounds it
        policy = self._policies.get(lease.model_id)
        if policy is None:
            raise _unavailable(f"no runtime chat policy is bound to {lease.model_id!r}")
        if not isinstance(receipt, CountReceipt):
            raise _unavailable("the counter returned no receipt")
        if receipt.policy_id != policy.policy_id or receipt.template_sha256 != policy.template_sha256:
            raise _unavailable("the receipt does not belong to this model's policy")
        if receipt.generation != lease.generation:
            raise _unavailable("the receipt was taken under another generation")
        instance = self._require_instance(lease, policy)
        if instance != receipt.instance:
            raise _unavailable("the accepted instance changed after the count")


def _counting_failed(exc: Exception) -> ChatCountingError:
    if getattr(exc, "code", None) == "execution_timeout":
        return ChatCountingError("inference_timeout", "the count did not finish before the deadline")
    return _unavailable(f"the count could not be proven: {type(exc).__name__}")


def policies_for_models(registry: Mapping[tuple[str, str, str], RuntimeChatPolicy],
                        models: Iterable[tuple[str, str, str, str]]) -> dict[str, RuntimeChatPolicy]:
    """Resolve one policy per `(model_id, profile_id, image_digest, model_sha256)` row."""
    resolved: dict[str, RuntimeChatPolicy] = {}
    for model_id, profile_id, image_digest, model_sha256 in models:
        resolved[model_id] = resolve_policy(profile_id=profile_id, image_digest=image_digest,
                                            model_sha256=model_sha256, registry=registry)
    return resolved
