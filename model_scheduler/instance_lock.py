"""Advisory single-instance process lock for the scheduler control plane."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
from typing import Iterator


class InstanceLocked(RuntimeError):
    pass


@contextmanager
def acquire(path: str | Path) -> Iterator[None]:
    """Hold an exclusive non-blocking lock until the service process exits."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstanceLocked(str(target)) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
