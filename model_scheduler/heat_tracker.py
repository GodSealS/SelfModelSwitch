from __future__ import annotations

import math
from time import monotonic
from .config import HeatConfig
from .models import ModelRuntime


class HeatTracker:
    def __init__(self, config: HeatConfig):
        self.cfg = config
        self._last_score = {}

    def _decay(self, score: float, elapsed: float) -> float:
        if score <= 0 or elapsed <= 0:
            return max(score, 0.0)
        return score * math.pow(0.5, elapsed / self.cfg.half_life_seconds)

    def score(self, runtime: ModelRuntime, priority: int = 0) -> float:
        now = monotonic()
        base = self._last_score.get(runtime.model_id, 0.0)
        last = runtime.last_used or runtime.last_loaded or now
        value = self._decay(base, now - last)
        if runtime.state.value == "active":
            value += self.cfg.active_bonus
        value += priority * 0.01
        return value

    def record_request(self, runtime: ModelRuntime, tokens: int = 0):
        now = monotonic()
        old = self._last_score.get(runtime.model_id, 0.0)
        elapsed = 0 if not runtime.last_used else now - runtime.last_used
        old = self._decay(old, elapsed)
        self._last_score[runtime.model_id] = (
            old + self.cfg.request_weight + tokens * self.cfg.token_weight
        )

    def raw_score(self, model_id: str) -> float:
        return self._last_score.get(model_id, 0.0)
