from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.diagnose_cli import run_diagnosis
from scripts.execution_budget import ExecutionBudget, ExecutionBudgetExceeded
from scripts.validate_config import validate_config

sys.path.remove(str(PLUGIN_ROOT))


def _incident() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "incident_id": "incident-flow",
        "source": "ci",
        "environment": "test",
        "pipeline_id": "synthetic-pipeline",
        "run_id": "flow-run",
        "failed_step": "compile",
        "occurred_at": "2026-10-08T12:00:00Z",
        "error_summary": "A step failed",
        "project_profile": "generic",
    }


def _grounded(payload, budget, _prompt):
    budget.record_tokens(20)
    return {
        "schema_version": "1.0",
        "incident_id": payload["incident"]["incident_id"],
        "failure_family": "CODE",
        "confidence": "HIGH",
        "cause_status": "CONFIRMED",
        "cause": "The evidence establishes a failure.",
        "evidence_citations": [
            {
                "evidence_id": payload["evidences"][0]["evidence_id"],
                "claim": "An execution step failed.",
            }
        ],
    }


def _config():
    return validate_config(PLUGIN_ROOT / "config/projects/second-project.yaml")


def test_second_mock_project_produces_real_contracts_and_report_without_project_writes(
    tmp_path,
):
    before = list(tmp_path.rglob("*"))
    result = run_diagnosis(
        _incident(), project_config=_config(), project_root=tmp_path, runner=_grounded
    )
    assert result["exit_code"] == 0
    assert result["report"]["diagnosis"] == result["diagnosis"]
    assert result["report"]["eligibility_decision"] == result["eligibility_decision"]
    assert "CONFIRMED" in result["report_markdown"]
    assert list(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("limit", ["tools", "tokens", "time"])
def test_budget_exhaustion_discards_speculation_in_the_full_flow(tmp_path, limit):
    config = _config()
    clock = [100.0]
    budget = ExecutionBudget(config, clock=lambda: clock[0])

    def runner(payload, active_budget, prompt):
        if limit == "tools":
            for _ in range(11):
                active_budget.call_tool(lambda: None)
        elif limit == "tokens":
            active_budget.record_tokens(active_budget.remaining_tokens)
        else:
            clock[0] += 60
        return _grounded(payload, active_budget, prompt)

    result = run_diagnosis(
        _incident(),
        project_config=config,
        project_root=tmp_path,
        runner=runner,
        budget=budget,
    )
    assert result["exit_code"] == 3
    assert result["diagnosis"]["cause_status"] == "UNKNOWN"
    assert result["diagnosis"]["evidence_citations"] == []
    assert result["eligibility_decision"]["decision"] == "ESCALATE"


def test_model_timeout_becomes_unknown_instead_of_an_operational_error(tmp_path):
    def timed_out(_payload, _budget, _prompt):
        raise ExecutionBudgetExceeded("max_seconds")

    result = run_diagnosis(
        _incident(), project_config=_config(), project_root=tmp_path, runner=timed_out
    )
    assert result["exit_code"] == 3


def test_invalid_citation_is_not_accepted_by_the_flow(tmp_path):
    def ungrounded(payload, budget, prompt):
        diagnosis = _grounded(payload, budget, prompt)
        diagnosis["evidence_citations"][0]["evidence_id"] = "missing-evidence"
        return diagnosis

    result = run_diagnosis(
        _incident(), project_config=_config(), project_root=tmp_path, runner=ungrounded
    )
    assert result["exit_code"] == 3
    assert result["diagnosis"]["limitations"] == ["evidence_citation_not_found"]


def test_evidence_is_sanitized_and_rehashed_before_the_agent(tmp_path):
    incident = _incident()
    incident["failed_step"] = "notify user@example.com"

    def checked(payload, budget, prompt):
        assert "user@example.com" not in json.dumps(payload)
        for evidence in payload["evidences"]:
            assert (
                evidence["hash"]
                == "sha256:" + hashlib.sha256(evidence["content"].encode()).hexdigest()
            )
        return _grounded(payload, budget, prompt)

    result = run_diagnosis(
        incident, project_config=_config(), project_root=tmp_path, runner=checked
    )
    assert result["exit_code"] == 0
    assert "user@example.com" not in json.dumps(result)
