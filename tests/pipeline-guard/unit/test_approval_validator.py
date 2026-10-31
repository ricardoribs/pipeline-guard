from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.approval_validator import (  # noqa: E402
    ApprovalValidationError,
    validate_approval,
)

sys.path.remove(str(PLUGIN_ROOT))

INCIDENT_ID = "incident-approval-001"
DIAGNOSIS_HASH = f"sha256:{'a' * 64}"
BASE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
EXTERNAL_REFERENCE = "https://approvals.example.invalid/approval-001"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
MAX_AGE = timedelta(hours=1)


def _approval(**overrides: object) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "incident_id": INCIDENT_ID,
        "diagnosis_hash": DIAGNOSIS_HASH,
        "decision": "APPROVED",
        "actor": "maintainer@example.invalid",
        "decided_at": "2026-10-15T11:30:00Z",
        "channel_type": "local",
        "external_reference": EXTERNAL_REFERENCE,
        "base_commit": BASE_COMMIT,
        **overrides,
    }


def _validate(approval: object) -> dict[str, Any]:
    return validate_approval(
        approval,
        expected_incident_id=INCIDENT_ID,
        expected_diagnosis_hash=DIAGNOSIS_HASH,
        expected_base_commit=BASE_COMMIT,
        max_age=MAX_AGE,
        current_time=NOW,
    )


def test_valid_bound_approval_is_accepted() -> None:
    approval = _approval()

    result = _validate(approval)

    assert result == approval
    assert result is not approval


@pytest.mark.parametrize(
    ("field", "value", "error_match"),
    [
        ("incident_id", "another-incident", "incident_id"),
        ("diagnosis_hash", f"sha256:{'b' * 64}", "diagnosis_hash"),
        ("base_commit", "f" * 40, "base_commit"),
    ],
    ids=["incident", "diagnosis", "base-commit"],
)
def test_approval_must_match_the_remediation_context(
    field: str,
    value: str,
    error_match: str,
) -> None:
    with pytest.raises(ApprovalValidationError, match=error_match):
        _validate(_approval(**{field: value}))


@pytest.mark.parametrize(
    "decided_at",
    [
        "2026-10-15T11:00:00Z",
        "2026-10-15T10:59:59Z",
    ],
    ids=["exact-boundary", "older-than-policy"],
)
def test_expired_approval_is_rejected(decided_at: str) -> None:
    with pytest.raises(ApprovalValidationError, match="expired"):
        _validate(_approval(decided_at=decided_at))


def test_future_approval_is_rejected() -> None:
    with pytest.raises(ApprovalValidationError, match="future"):
        _validate(_approval(decided_at="2026-10-15T12:00:01Z"))


def test_rejected_decision_cannot_authorize_remediation() -> None:
    approval = _approval(decision="REJECTED")
    approval.pop("base_commit")

    with pytest.raises(ApprovalValidationError, match="requires APPROVED"):
        _validate(approval)


@pytest.mark.parametrize("external_reference", [None, ""])
def test_external_reference_is_required(external_reference: str | None) -> None:
    approval = _approval()
    if external_reference is None:
        approval.pop("external_reference")
    else:
        approval["external_reference"] = external_reference

    with pytest.raises(ApprovalValidationError, match="external_reference"):
        _validate(approval)


@pytest.mark.parametrize(
    ("approval", "error_match"),
    [
        (None, "JSON object"),
        ({"schema_version": "1.0"}, "incident_id"),
        (_approval(unexpected=True), "Additional properties"),
        (_approval(decided_at="not-a-date"), "date-time"),
        (_approval(base_commit=None), "not of type 'string'"),
    ],
    ids=[
        "not-object",
        "missing-fields",
        "unknown-field",
        "bad-date",
        "bad-base-commit",
    ],
)
def test_invalid_approval_contract_is_rejected(
    approval: object,
    error_match: str,
) -> None:
    with pytest.raises(ApprovalValidationError, match=error_match):
        _validate(approval)


@pytest.mark.parametrize(
    ("overrides", "error_match"),
    [
        ({"expected_incident_id": ""}, "expected_incident_id"),
        ({"expected_diagnosis_hash": ""}, "expected_diagnosis_hash"),
        ({"expected_base_commit": ""}, "expected_base_commit"),
        ({"max_age": timedelta(0)}, "positive timedelta"),
        ({"current_time": datetime(2026, 10, 15, 12, 0)}, "timezone-aware"),
    ],
    ids=["incident", "diagnosis", "base-commit", "max-age", "current-time"],
)
def test_invalid_trusted_context_is_rejected(
    overrides: dict[str, object],
    error_match: str,
) -> None:
    arguments: dict[str, object] = {
        "expected_incident_id": INCIDENT_ID,
        "expected_diagnosis_hash": DIAGNOSIS_HASH,
        "expected_base_commit": BASE_COMMIT,
        "max_age": MAX_AGE,
        "current_time": NOW,
        **overrides,
    }

    with pytest.raises(ApprovalValidationError, match=error_match):
        validate_approval(_approval(), **arguments)  # type: ignore[arg-type]
