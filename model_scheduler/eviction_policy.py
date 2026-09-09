from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .config import SchedulerConfig
from .heat_tracker import HeatTracker
from .model_registry import ModelRegistry
from .models import ModelRuntime


@dataclass(frozen=True)
class EvictionCandidate:
    model_id: str
    reserved_bytes: int
    heat: float
    priority: int
    score: float


class EvictionPolicy:
    """
    Select a small set of models whose reserved memory satisfies the deficit.

    The score is deliberately interpretable:
      - low heat => easier to evict
      - low priority => easier to evict
      - high memory => attractive because it releases more memory

    We greedily sort by eviction_score / GiB released. For a local single-user
    scheduler with a small model count this is predictable and fast.
    """

    def __init__(self, registry: ModelRegistry, heat: HeatTracker, cfg: SchedulerConfig):
        self.registry = registry
        self.heat = heat
        self.cfg = cfg

    def candidates(self) -> List[EvictionCandidate]:
        out = []
        for r in self.registry.evictable():
            c = self.registry.configs[r.model_id]
            heat = self.heat.score(r, c.scheduling.priority)

            # Higher value means "more worth keeping".
            keep_value = (
                heat * 100.0
                + c.scheduling.priority * 5.0
            )

            # Lower score = easier to evict.
            # Large models get a modest bonus because they solve memory pressure efficiently.
            score = keep_value / max(c.memory.reserved_bytes / (1024**3), 0.25)

            out.append(EvictionCandidate(
                model_id=r.model_id,
                reserved_bytes=c.memory.reserved_bytes,
                heat=heat,
                priority=c.scheduling.priority,
                score=score,
            ))

        # Low score first; if similar, evict the one releasing more memory first.
        out.sort(key=lambda x: (x.score, -x.reserved_bytes))
        return out

    def choose(self, bytes_needed: int) -> List[EvictionCandidate]:
        selected = []
        released = 0

        for c in self.candidates():
            selected.append(c)
            released += c.reserved_bytes
            if released >= bytes_needed:
                break
            if len(selected) >= self.cfg.max_evictions_per_request:
                break

        if released < bytes_needed:
            return []
        return selected
