from __future__ import annotations

import subprocess
import sys

from model_scheduler.instance_lock import acquire


def test_second_process_cannot_take_scheduler_lock(tmp_path) -> None:
    path = tmp_path / "scheduler.lock"
    code = "from model_scheduler.instance_lock import InstanceLocked, acquire; import sys\ntry:\n with acquire(sys.argv[1]): pass\nexcept InstanceLocked: raise SystemExit(73)\nraise SystemExit(0)"
    with acquire(path):
        result = subprocess.run([sys.executable, "-c", code, str(path)], check=False)
    assert result.returncode == 73
