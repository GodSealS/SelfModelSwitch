"""Runtime-keyed adapter routing for dynamic registrations (M02/P06).

The router is the single place that answers "which adapter serves this model":
one registered runtime identity maps to exactly one adapter object, a model is
served only by the adapter bound to its own `runtime_id`, and a model whose
runtime has no bound adapter is refused instead of falling back to another
runtime. Registration-only profiles never get a binding, so accepting a
`runtime_id` can never claim that an unimplemented runtime is usable.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .contracts_v2 import ContractError, ModelSpec, RuntimeSpec, require_startable_profile
from .ports_v3 import BackendPort


class BackendRouterError(ValueError):
    """A runtime cannot be bound to exactly one adapter identity."""


@dataclass(frozen=True)
class AdapterBinding:
    """The exact runtime identity one adapter object serves."""

    runtime_id: str
    profile_id: str
    image_digest: str
    adapter: Any


class BackendRouter:
    """Bind adapters to registered runtimes and select them by model."""

    def __init__(self, *, runtimes: Mapping[str, RuntimeSpec], models: Mapping[str, ModelSpec]) -> None:
        self._runtimes = dict(runtimes)
        self._models = dict(models)
        self._bindings: dict[str, AdapterBinding] = {}

    @property
    def bindings(self) -> Mapping[str, AdapterBinding]:
        return MappingProxyType(self._bindings)

    def register(self, runtime_id: str, adapter: Any) -> AdapterBinding:
        """Bind one adapter object to one startable registered runtime."""
        runtime = self._runtimes.get(runtime_id)
        if runtime is None:
            raise BackendRouterError(f"unknown runtime {runtime_id!r}")
        if runtime_id in self._bindings:
            raise BackendRouterError(f"runtime {runtime_id!r} is already registered")
        try:
            require_startable_profile(runtime)
        except ContractError as exc:
            raise BackendRouterError(str(exc)) from exc
        declared = getattr(adapter, "runtime_id", None)
        if declared is not None and declared != runtime_id:
            raise BackendRouterError(
                f"adapter declares runtime identity {declared!r}, not {runtime_id!r}"
            )
        for bound_id, binding in self._bindings.items():
            if binding.adapter is adapter:
                raise BackendRouterError(f"adapter is already bound to runtime {bound_id!r}")
        if not isinstance(adapter, BackendPort):
            raise BackendRouterError(
                f"adapter for runtime {runtime_id!r} does not satisfy the BackendPort protocol"
            )
        binding = AdapterBinding(
            runtime_id=runtime_id,
            profile_id=runtime.profile_id,
            image_digest=runtime.image_digest,
            adapter=adapter,
        )
        self._bindings[runtime_id] = binding
        return binding

    def runtime_spec(self, model_id: str) -> RuntimeSpec:
        model = self._model(model_id)
        runtime = self._runtimes.get(model.runtime_id)
        if runtime is None:
            raise BackendRouterError(f"unknown runtime {model.runtime_id!r} for model {model_id!r}")
        return runtime

    def backend_for(self, model_id: str) -> Any:
        """Return the adapter bound to this model's own runtime; never another one."""
        model = self._model(model_id)
        binding = self._bindings.get(model.runtime_id)
        if binding is None:
            raise BackendRouterError(f"no adapter for runtime {model.runtime_id!r}")
        return binding.adapter

    def _model(self, model_id: str) -> ModelSpec:
        model = self._models.get(model_id)
        if model is None:
            raise BackendRouterError(f"unknown model {model_id!r}")
        return model
