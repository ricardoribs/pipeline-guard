"""Offline gate contracts using controlled diagnoses for the 12 reference cases.

Failure families come from the reference labels, without running the diagnosis
agent. These tests verify gate decisions, not model classification accuracy.
"""

from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = PLUGIN_ROOT / "contracts" / "schema"

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

evaluate_eligibility = import_module("scripts.eligibility_gate").evaluate_eligibility

REFERENCE_CASES = json.loads(
    (
        PLUGIN_ROOT
        / "tests"
        / "pipeline-guard"
        / "fixtures"
        / "dbt"
        / "reference_cases.json"
    ).read_text(encoding="utf-8")
)
DIAGNOSIS_SCHEMA = json.loads(
    (SCHEMA_DIR / "pipeline_diagnosis_v1.schema.json").read_text(encoding="utf-8")
)
DECISION_SCHEMA = json.loads(
    (SCHEMA_DIR / "eligibility_decision_v1.schema.json").read_text(encoding="utf-8")
)
EXPECTED_DECISIONS = {
    "code-missing-column": "ELIGIBLE_FOR_REMEDIATION",
    "data-not-null-test": "REPORT_ONLY",
    "business-rule-revenue-threshold": "REPORT_ONLY",
    "configuration-missing-variable": "REPORT_ONLY",
    "dependency-missing-package": "REPORT_ONLY",
    "platform-warehouse-timeout": "REPORT_ONLY",
    "access-permission-denied": "REPORT_ONLY",
    "external-source-unavailable": "REPORT_ONLY",
    "blind-empty-run-results": "ESCALATE",
    "blind-generic-run-error": "ESCALATE",
    "blind-conflicting-signals": "ESCALATE",
    "blind-ambiguous-partial-failure": "ESCALATE",
}
EXPECTED_RULES = {
    "ELIGIBLE_FOR_REMEDIATION": "eligibility.code-policy-satisfied",
    "REPORT_ONLY": "eligibility.non-code-family",
    "ESCALATE": "eligibility.unknown-cause",
}
NON_CODE_FAMILIES = [
    family
    for family in DIAGNOSIS_SCHEMA["properties"]["failure_family"]["enum"]
    if family != "CODE"
]


def _diagnosis(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "incident_id": case["incident"]["incident_id"],
        "failure_family": case["expected_failure_family"],
        "confidence": "LOW" if case["blind"] else "HIGH",
        "cause_status": "UNKNOWN" if case["blind"] else "CONFIRMED",
        "cause": case["incident"]["error_summary"],
        "evidence_citations": (
            []
            if case["blind"]
            else [
                {"evidence_id": case["case_id"], "claim": "The reference run failed."}
            ]
        ),
    }


def _config(**policy_overrides: object) -> dict[str, Any]:
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
            "policy_version": "1.0",
            "minimum_confidence": "MEDIUM",
            "require_confirmed_cause": True,
            "required_signals": [],
            **policy_overrides,
        },
    }


def test_reference_inventory_contains_all_twelve_cases_and_four_blind_cases() -> None:
    case_ids = [case["case_id"] for case in REFERENCE_CASES]

    assert len(case_ids) == len(set(case_ids)) == 12
    assert set(case_ids) == set(EXPECTED_DECISIONS)
    assert {case["case_id"] for case in REFERENCE_CASES if case["blind"]} == {
        "blind-empty-run-results",
        "blind-generic-run-error",
        "blind-conflicting-signals",
        "blind-ambiguous-partial-failure",
    }
    assert {case["expected_failure_family"] for case in REFERENCE_CASES} == {
        "CODE",
        *NON_CODE_FAMILIES,
    }


@pytest.mark.parametrize("case", REFERENCE_CASES, ids=lambda case: case["case_id"])
def test_reference_case_receives_expected_eligibility_decision(
    case: dict[str, Any],
) -> None:
    diagnosis = _diagnosis(case)
    Draft202012Validator(DIAGNOSIS_SCHEMA).validate(diagnosis)

    result = evaluate_eligibility(diagnosis, project_config=_config())

    expected_decision = EXPECTED_DECISIONS[case["case_id"]]
    assert result["decision"] == expected_decision
    assert result["rule_id"] == EXPECTED_RULES[expected_decision]
    assert result["incident_id"] == diagnosis["incident_id"]
    Draft202012Validator(DECISION_SCHEMA).validate(result)
    if diagnosis["failure_family"] != "CODE":
        assert result["decision"] != "ELIGIBLE_FOR_REMEDIATION"
    if case["blind"]:
        assert diagnosis["failure_family"] == "UNKNOWN"
        assert result["limitations"] == ["cause_not_established"]


@pytest.mark.parametrize("failure_family", NON_CODE_FAMILIES)
@pytest.mark.parametrize("confidence", ["LOW", "MEDIUM", "HIGH"])
@pytest.mark.parametrize("cause_status", ["HYPOTHESIS", "CONFIRMED"])
def test_permissive_policy_never_makes_non_code_causes_eligible(
    failure_family: str,
    confidence: str,
    cause_status: str,
) -> None:
    diagnosis = {
        **_diagnosis(REFERENCE_CASES[0]),
        "failure_family": failure_family,
        "confidence": confidence,
        "cause_status": cause_status,
    }
    Draft202012Validator(DIAGNOSIS_SCHEMA).validate(diagnosis)

    result = evaluate_eligibility(
        diagnosis,
        project_config=_config(
            minimum_confidence="MEDIUM",
            require_confirmed_cause=False,
            required_signals=["tests_passed"],
        ),
        independent_signals={"tests_passed": True},
    )

    assert result["decision"] == (
        "ESCALATE" if failure_family == "UNKNOWN" else "REPORT_ONLY"
    )
    assert result["decision"] != "ELIGIBLE_FOR_REMEDIATION"
    Draft202012Validator(DECISION_SCHEMA).validate(result)
