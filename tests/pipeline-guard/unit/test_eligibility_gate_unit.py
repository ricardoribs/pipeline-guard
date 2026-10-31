from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.eligibility_gate import (  # noqa: E402
    EligibilityGateError,
    evaluate_eligibility,
)

sys.path.remove(str(PLUGIN_ROOT))

INCIDENT_ID = "incident-eligibility-001"


def _diagnosis(
    *,
    failure_family: str = "CODE",
    confidence: str = "HIGH",
    cause_status: str = "CONFIRMED",
) -> dict[str, Any]:
    citations = (
        []
        if cause_status == "UNKNOWN"
        else [{"evidence_id": "evidence-1", "claim": "The failing node is cited."}]
    )
    return {
        "schema_version": "1.0",
        "incident_id": INCIDENT_ID,
        "failure_family": failure_family,
        "confidence": confidence,
        "cause_status": cause_status,
        "cause": "A deterministic test diagnosis.",
        "evidence_citations": citations,
    }


def _config(**eligibility_overrides: object) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "pipeline": {"adapter": "dbt"},
        "runtime": {
            "execution_budget": {
                "max_tool_calls": 1,
                "max_seconds": 1,
                "max_tokens": 1,
            }
        },
        "eligibility": {
            "policy_version": "2026-10-06",
            "minimum_confidence": "MEDIUM",
            "require_confirmed_cause": True,
            "required_signals": [],
            **eligibility_overrides,
        },
    }


def _validate_decision(decision: dict[str, Any]) -> None:
    schema_path = PLUGIN_ROOT / "contracts" / "schema" / "eligibility_decision_v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(decision)


@pytest.mark.parametrize("confidence", ["HIGH", "MEDIUM"])
def test_confirmed_code_with_sufficient_confidence_is_eligible(confidence: str) -> None:
    result = evaluate_eligibility(
        _diagnosis(confidence=confidence),
        project_config=_config(),
    )

    assert result["decision"] == "ELIGIBLE_FOR_REMEDIATION"
    assert result["rule_id"] == "eligibility.code-policy-satisfied"
    assert result["policy_version"] == "2026-10-06"
    _validate_decision(result)


@pytest.mark.parametrize(
    "failure_family",
    [
        "DATA",
        "BUSINESS_RULE",
        "CONFIGURATION",
        "DEPENDENCY",
        "PLATFORM_INFRA",
        "ACCESS_PERMISSION",
        "EXTERNAL_SYSTEM",
    ],
)
def test_non_code_family_is_never_eligible(failure_family: str) -> None:
    result = evaluate_eligibility(
        _diagnosis(failure_family=failure_family),
        project_config=_config(),
    )

    assert result["decision"] == "REPORT_ONLY"
    assert result["rule_id"] == "eligibility.non-code-family"
    _validate_decision(result)


def test_unknown_cause_is_escalated() -> None:
    result = evaluate_eligibility(
        _diagnosis(failure_family="UNKNOWN", confidence="LOW", cause_status="UNKNOWN"),
        project_config=_config(),
    )

    assert result["decision"] == "ESCALATE"
    assert result["rule_id"] == "eligibility.unknown-cause"


@pytest.mark.parametrize(
    ("diagnosis", "rule_id"),
    [
        (_diagnosis(confidence="LOW"), "eligibility.code-confidence-insufficient"),
        (_diagnosis(cause_status="HYPOTHESIS"), "eligibility.code-cause-unconfirmed"),
    ],
    ids=["low-confidence", "unconfirmed-cause"],
)
def test_code_that_does_not_satisfy_policy_is_escalated(
    diagnosis: dict[str, Any],
    rule_id: str,
) -> None:
    result = evaluate_eligibility(diagnosis, project_config=_config())

    assert result["decision"] == "ESCALATE"
    assert result["rule_id"] == rule_id


def test_required_independent_signals_must_all_pass() -> None:
    config = _config(required_signals=["tests_passed", "workspace_clean"])

    blocked = evaluate_eligibility(
        _diagnosis(),
        project_config=config,
        independent_signals={"tests_passed": True, "workspace_clean": False},
    )
    eligible = evaluate_eligibility(
        _diagnosis(),
        project_config=config,
        independent_signals={"tests_passed": True, "workspace_clean": True},
    )

    assert blocked["decision"] == "ESCALATE"
    assert blocked["rule_id"] == "eligibility.required-signal-failed"
    assert blocked["signals_used"] == ["tests_passed", "workspace_clean"]
    assert blocked["limitations"] == ["required_signal_failed:workspace_clean"]
    assert eligible["decision"] == "ELIGIBLE_FOR_REMEDIATION"
    assert eligible["signals_used"] == ["tests_passed", "workspace_clean"]


def test_diagnosis_hash_is_canonical_and_deterministic() -> None:
    diagnosis = _diagnosis()
    reordered = dict(reversed(list(diagnosis.items())))

    first = evaluate_eligibility(diagnosis, project_config=_config())
    second = evaluate_eligibility(reordered, project_config=_config())
    canonical = json.dumps(
        diagnosis,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert first == second
    assert first["diagnosis_hash"] == f"sha256:{hashlib.sha256(canonical).hexdigest()}"


@pytest.mark.parametrize(
    ("diagnosis", "project_config", "signals", "error_match"),
    [
        ({"schema_version": "1.0"}, _config(), None, "pipeline_diagnosis_v1"),
        (_diagnosis(), {"schema_version": "1.0"}, None, "project configuration"),
        (_diagnosis(), _config(), {"tests_passed": "yes"}, "must be a boolean"),
    ],
    ids=["invalid-diagnosis", "invalid-config", "invalid-signal"],
)
def test_invalid_trusted_inputs_are_rejected(
    diagnosis: object,
    project_config: object,
    signals: Any,
    error_match: str,
) -> None:
    with pytest.raises(EligibilityGateError, match=error_match):
        evaluate_eligibility(
            diagnosis,
            project_config=project_config,
            independent_signals=signals,
        )
