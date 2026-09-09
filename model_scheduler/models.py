from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from time import monotonic
from typing import Any


class ModelState(str, Enum):
    UNKNOWN = "unknown"
    UNLOADED = "unloaded"
    LOADING = "loading"
    READY = "ready"
    ACTIVE = "active"
    EVICTING = "evicting"
    ERROR = "error"


@dataclass
class ModelRuntime:
    model_id: str
    state: ModelState = ModelState.UNKNOWN
    last_used: float = 0.0
    last_loaded: float = 0.0
    in_flight: int = 0
    total_requests: int = 0
    total_tokens: int = 0
    last_error: str | None = None
    actual_reserved_bytes: int | None = None
    last_state_change: float = field(default_factory=monotonic)

    def touch(self):
        self.last_used = monotonic()
        self.total_requests += 1

    def mark_loaded(self):
        self.state = ModelState.READY
        now = monotonic()
        self.last_loaded = now
        self.last_state_change = now
        self.last_error = None

    def mark_error(self, error: str):
        self.state = ModelState.ERROR
        self.last_error = error
        self.last_state_change = monotonic()
