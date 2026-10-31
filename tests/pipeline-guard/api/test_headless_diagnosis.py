"""Live end-to-end diagnosis through the same entrypoint used by orchestrators."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.api
PLUGIN_ROOT = Path(__file__).resolve().parents[3]


def test_live_second_mock_consumer_receives_diagnosis_and_reports(tmp_path):
    if not os.environ.get("ANTHROPIC_MODEL"):
        pytest.fail("ANTHROPIC_MODEL is required for live headless diagnosis")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    environment = os.environ.copy()
    environment["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "host-data")
    incident = {
        "schema_version": "1.0",
        "incident_id": "INC-MOCK-HEADLESS-001",
        "source": "ci",
        "environment": "test",
        "pipeline_id": "second-project",
        "run_id": "headless-live-001",
        "failed_step": "compile",
        "occurred_at": "2026-10-08T12:00:00Z",
        "error_summary": "An execution step failed; explain only what evidence supports.",
        "project_profile": "generic",
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts/diagnose_cli.py"),
            "--config",
            str(PLUGIN_ROOT / "config/projects/second-project.yaml"),
            "--project-root",
            str(consumer),
        ],
        input=json.dumps(incident),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=90,
        check=False,
    )
    assert completed.returncode in {0, 3}, completed.stderr
    result = json.loads(completed.stdout)
    assert result["diagnosis"]["incident_id"] == incident["incident_id"]
    assert result["report"]["diagnosis"] == result["diagnosis"]
    assert result["report"]["eligibility_decision"] == result["eligibility_decision"]
    assert result["report_markdown"]
    assert list(consumer.iterdir()) == []
