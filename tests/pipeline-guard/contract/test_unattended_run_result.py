from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = PLUGIN_ROOT
SCHEMA_PATH = (
    PLUGIN_ROOT / "contracts" / "schema" / "unattended_run_result_v1.schema.json"
)
SPEC_SCHEMA_PATH = (
    REPO_ROOT
    / "docs"
    / "specs"
    / "pipeline-guard"
    / "unattended"
    / "contracts"
    / "unattended_run_result_v1.schema.json"
)

COMMIT = "a" * 40
FAILURE_SIGNATURE = "sha256:" + "b" * 64
DIAGNOSIS_HASH = "sha256:" + "c" * 64


def _load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(schema, dict)
    Draft202012Validator.check_schema(schema)
    return schema


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_load_schema(), format_checker=FormatChecker())


def _base_result(
    outcome: str, exit_code: int, reason_code: str | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "mode": "unattended",
        "outcome": outcome,
        "exit_code": exit_code,
        "incident_id": "incident-unattended-001",
        "service_identity": "sp:pipeline-guard",
        "host_run": {
            "host": "airflow",
            "run_reference": "scheduled__2026-10-07T10:00:00+00:00",
        },
        "started_at": "2026-10-07T10:05:00Z",
        "finished_at": "2026-10-07T10:06:00Z",
        "decisions": [{"id": "remediation", "resolution": "SAFE_STOP"}],
        "summary": "Pipeline Guard unattended result.",
    }
    if reason_code is not None:
        result["reason_code"] = reason_code
    return result


def _published_result(*, reused: bool = False) -> dict[str, Any]:
    result = _base_result("DRAFT_PR_PUBLISHED", 0)
    result.update(
        {
            "failure_signature": FAILURE_SIGNATURE,
            "code_version": {"branch": "main", "commit": COMMIT},
            "draft_pr": {
                "url": "https://bitbucket.org/example/repository/pull-requests/42",
                "reused": reused,
            },
        }
    )
    if not reused:
        result.update(
            {
                "diagnosis": {
                    "failure_family": "CODE",
                    "confidence": "HIGH",
                    "cause_status": "CONFIRMED",
                    "diagnosis_hash": DIAGNOSIS_HASH,
                },
                "eligibility": {
                    "decision": "ELIGIBLE_FOR_REMEDIATION",
                    "rule_id": "code-confirmed",
                    "policy_version": "1.0",
                },
                "authorization": {
                    "channel_type": "policy",
                    "actor": "sp:pipeline-guard",
                    "decided_at": "2026-10-07T10:05:15Z",
                    "base_commit": COMMIT,
                    "diagnosis_hash": DIAGNOSIS_HASH,
                    "authenticated": True,
                },
                "remediation_result_ref": "artifacts/remediation-result.json",
            }
        )
    return result


def _valid_results() -> list[dict[str, Any]]:
    return [
        _published_result(),
        _base_result("REPORT_ONLY", 3, "MISSING_INPUT"),
        _base_result("BLOCKED", 4, "VALIDATION_FAILED"),
        _base_result("LIMIT_REACHED", 5, "TIME_LIMIT"),
        _base_result("ERROR", 1, "OPERATIONAL_FAILURE"),
    ]


def test_published_schema_matches_the_validated_spec_contract() -> None:
    assert _load_schema() == _load_schema(SPEC_SCHEMA_PATH)


@pytest.mark.parametrize(
    "result",
    _valid_results(),
    ids=["draft-published", "report-only", "blocked", "limit-reached", "error"],
)
def test_accepts_each_terminal_outcome(result: dict[str, Any]) -> None:
    _validator().validate(result)


@pytest.mark.parametrize(
    ("index", "wrong_exit_code"),
    [(0, 1), (1, 4), (2, 5), (3, 4), (4, 2)],
    ids=["draft-published", "report-only", "blocked", "limit-reached", "error"],
)
def test_rejects_an_exit_code_from_another_outcome(
    index: int, wrong_exit_code: int
) -> None:
    result = copy.deepcopy(_valid_results()[index])
    result["exit_code"] = wrong_exit_code

    with pytest.raises(ValidationError):
        _validator().validate(result)


@pytest.mark.parametrize(
    ("index", "wrong_reason_code"),
    [
        (1, "TIME_LIMIT"),
        (2, "MISSING_INPUT"),
        (3, "VALIDATION_FAILED"),
        (4, "MISSING_INPUT"),
    ],
    ids=["report-only", "blocked", "limit-reached", "error"],
)
def test_rejects_a_reason_code_from_another_outcome(
    index: int, wrong_reason_code: str
) -> None:
    result = copy.deepcopy(_valid_results()[index])
    result["reason_code"] = wrong_reason_code

    with pytest.raises(ValidationError):
        _validator().validate(result)


def test_report_only_missing_commit_does_not_require_a_failure_signature() -> None:
    result = _base_result("REPORT_ONLY", 3, "MISSING_INPUT")

    _validator().validate(result)
    assert "code_version" not in result
    assert "failure_signature" not in result


def test_failure_signature_cannot_exist_without_code_version() -> None:
    result = _base_result("REPORT_ONLY", 3, "MISSING_INPUT")
    result["failure_signature"] = FAILURE_SIGNATURE

    with pytest.raises(ValidationError, match="code_version"):
        _validator().validate(result)


def test_reused_draft_requires_failure_signature() -> None:
    result = _published_result(reused=True)
    del result["failure_signature"]

    with pytest.raises(ValidationError, match="failure_signature"):
        _validator().validate(result)


def test_pr_target_refused_requires_the_pushed_branch() -> None:
    result = _base_result("BLOCKED", 4, "PR_TARGET_REFUSED")

    with pytest.raises(ValidationError, match="pushed_branch"):
        _validator().validate(result)

    result["pushed_branch"] = "pipeline-guard/incident-unattended-001"
    _validator().validate(result)


def test_invalid_input_error_does_not_require_validated_incident_context() -> None:
    result = _base_result("ERROR", 2, "INVALID_INPUT")
    for field in ("incident_id", "service_identity", "host_run"):
        del result[field]

    _validator().validate(result)


def test_draft_published_rejects_reason_code_and_pushed_branch() -> None:
    for forbidden_field, value in (
        ("reason_code", "OPERATIONAL_FAILURE"),
        ("pushed_branch", "pipeline-guard/incident-unattended-001"),
    ):
        result = _published_result()
        result[forbidden_field] = value

        with pytest.raises(ValidationError):
            _validator().validate(result)
