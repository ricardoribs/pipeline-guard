# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4.0.0"]
# ///
"""Deterministic validation for approvals that authorize remediation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "schema"
    / "approval_input_v1.schema.json"
)


class ApprovalValidationError(ValueError):
    """Raised when an approval cannot authorize the requested remediation."""


def validate_approval(
    approval: object,
    *,
    expected_incident_id: str,
    expected_diagnosis_hash: str,
    expected_base_commit: str,
    max_age: timedelta,
    current_time: datetime | None = None,
) -> dict[str, Any]:
    """Validate one approval against the exact remediation context.

    ``max_age`` is supplied by the caller so that approval freshness remains an
    explicit policy rather than a hidden constant in the validator. ``current_time``
    may be injected for deterministic execution and tests.

    This validator does not authenticate the approval source. An orchestrated
    caller must verify the approval in its host adapter before calling this
    function; an interactive consumer must enforce the local-only trust boundary.
    """
    context = {
        "expected_incident_id": expected_incident_id,
        "expected_diagnosis_hash": expected_diagnosis_hash,
        "expected_base_commit": expected_base_commit,
    }
    for label, value in context.items():
        if not isinstance(value, str) or not value.strip():
            raise ApprovalValidationError(f"{label} must be a non-empty string")

    if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
        raise ApprovalValidationError("max_age must be a positive timedelta")

    now = current_time if current_time is not None else datetime.now(UTC)
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ApprovalValidationError("current_time must be a timezone-aware datetime")

    if not isinstance(approval, Mapping):
        raise ApprovalValidationError("approval_input_v1 must be a JSON object")

    document = dict(approval)
    errors = sorted(
        _validator().iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        raise ApprovalValidationError(f"{path}: {error.message}")

    if document["decision"] != "APPROVED":
        raise ApprovalValidationError("decision: remediation requires APPROVED")
    if document["incident_id"] != expected_incident_id:
        raise ApprovalValidationError(
            "incident_id: approval does not match the incident"
        )
    if document["diagnosis_hash"] != expected_diagnosis_hash:
        raise ApprovalValidationError(
            "diagnosis_hash: approval does not match the diagnosis"
        )
    if document["base_commit"] != expected_base_commit:
        raise ApprovalValidationError(
            "base_commit: approval does not match the base commit"
        )

    decided_at = _parse_datetime(document["decided_at"])
    if decided_at > now:
        raise ApprovalValidationError("decided_at: approval timestamp is in the future")
    if now >= decided_at + max_age:
        raise ApprovalValidationError("decided_at: approval has expired")

    return document


def _parse_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except (
        ValueError
    ) as error:  # pragma: no cover - guarded by the schema format checker
        raise ApprovalValidationError(
            "decided_at: must be a valid date-time"
        ) from error
    if (
        parsed.utcoffset() is None
    ):  # pragma: no cover - guarded by the schema format checker
        raise ApprovalValidationError("decided_at: must include a timezone")
    return parsed


@cache
def _validator() -> Draft202012Validator:
    try:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ApprovalValidationError(
            f"cannot load approval_input_v1 schema: {_SCHEMA_PATH}"
        ) from error
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())
