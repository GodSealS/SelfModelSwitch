"""Hardware-only release gate; absent evidence is a failure, never a pass."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from model_scheduler.deploy import DeployError, validate_thor_report


pytestmark = pytest.mark.thor


def test_thor_report_contains_all_passed_scenarios_without_prompts() -> None:
    report_path = os.environ.get("THOR_REPORT_PATH")
    if not report_path:
        pytest.fail("THOR_REPORT_PATH is required for hardware acceptance")
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        pytest.fail(f"cannot read Thor report: {exc}")
    try:
        validate_thor_report(report)
    except DeployError as exc:
        pytest.fail(f"Thor report does not satisfy production acceptance: {exc}")
    serialized = json.dumps(report, ensure_ascii=False).lower()
    assert "prompt" not in serialized
