"""Hardware-only release gate; absent evidence is a failure, never a pass."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.thor
_SCENARIOS = frozenset(f"A{index:02d}" for index in range(1, 21))


def test_thor_report_contains_all_passed_scenarios_without_prompts() -> None:
    report_path = os.environ.get("THOR_REPORT_PATH")
    if not report_path:
        pytest.fail("THOR_REPORT_PATH is required for hardware acceptance")
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        pytest.fail(f"cannot read Thor report: {exc}")
    results = report.get("scenarios") if isinstance(report, dict) else None
    if not isinstance(results, list):
        pytest.fail("Thor report has no scenario results")
    status = {item.get("id"): item.get("status") for item in results if isinstance(item, dict)}
    assert status == {scenario: "passed" for scenario in _SCENARIOS}
    serialized = json.dumps(report, ensure_ascii=False).lower()
    assert "prompt" not in serialized
