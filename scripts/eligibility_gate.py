# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4.0.0"]
# ///
"""Deterministic eligibility decisions for validated pipeline diagnoses."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_DIAGNOSIS_SCHEMA_PATH = _PLUGIN_ROOT / "contracts" / "schema" / "pipeline_diagnosis_v1.schema.json"
_DECISION_SCHEMA_PATH = (
    _PLUGIN_ROOT / "contracts" / "schema" / "eligibility_decision_v1.schema.json"
)
_CONFIG_SCHEMA_PATH = _PLUGIN_ROOT / "config" / "schema" / "project-config.schema.json"
_SCHEMA_VERSION = "1.0"
_DEFAULT_POLICY_VERSION = "1.0"
_CONFIDENCE_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


class EligibilityGateError(ValueError):
    """Raised when a trusted gate input or policy is invalid."""


@dataclass(frozen=True)
class _EligibilityPolicy:
    version: str
    minimum_confidence: str
    require_confirmed_cause: bool
    required_signals: tuple[str, ...]


def evaluate_eligibility(
    diagnosis: object,
    *,
    project_config: object,
    independent_signals: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """Apply the configured deterministic policy to one validated diagnosis."""
    validated_diagnosis = _validated_mapping(
        diagnosis,
        schema_path=_DIAGNOSIS_SCHEMA_PATH,
        label="pipeline_diagnosis_v1",
    )
    validated_config = _validated_mapping(
        project_config,
        schema_path=_CONFIG_SCHEMA_PATH,
        label="project configuration",
    )
    policy = _load_policy(validated_config)
    signals = _validate_signals(independent_signals)
    diagnosis_hash = _hash_document(validated_diagnosis)

    failure_family = validated_diagnosis["failure_family"]
    confidence = validated_diagnosis["confidence"]
    cause_status = validated_diagnosis["cause_status"]

    if failure_family == "UNKNOWN" or cause_status == "UNKNOWN":
        return _decision(
            diagnosis=validated_diagnosis,
            diagnosis_hash=diagnosis_hash,
            policy=policy,
            decision="ESCALATE",
            rule_id="eligibility.unknown-cause",
            reason="The diagnosis does not establish a remediable cause.",
            signals_used=(),
            limitations=("cause_not_established",),
        )

    if failure_family != "CODE":
        return _decision(
            diagnosis=validated_diagnosis,
            diagnosis_hash=diagnosis_hash,
            policy=policy,
            decision="REPORT_ONLY",
            rule_id="eligibility.non-code-family",
            reason=f"Failure family {failure_family} is not eligible for code remediation.",
            signals_used=(),
            limitations=(),
        )

    if policy.require_confirmed_cause and cause_status != "CONFIRMED":
        return _decision(
            diagnosis=validated_diagnosis,
            diagnosis_hash=diagnosis_hash,
            policy=policy,
            decision="ESCALATE",
            rule_id="eligibility.code-cause-unconfirmed",
            reason="Code remediation requires a confirmed cause under the active policy.",
            signals_used=(),
            limitations=("cause_not_confirmed",),
        )

    if _CONFIDENCE_RANK[confidence] < _CONFIDENCE_RANK[policy.minimum_confidence]:
        return _decision(
            diagnosis=validated_diagnosis,
            diagnosis_hash=diagnosis_hash,
            policy=policy,
            decision="ESCALATE",
            rule_id="eligibility.code-confidence-insufficient",
            reason=(
                f"Confidence {confidence} is below the configured minimum "
                f"{policy.minimum_confidence}."
            ),
            signals_used=(),
            limitations=("confidence_below_policy",),
        )

    failed_signals = tuple(
        signal_name
        for signal_name in policy.required_signals
        if signals.get(signal_name) is not True
    )
    if failed_signals:
        return _decision(
            diagnosis=validated_diagnosis,
            diagnosis_hash=diagnosis_hash,
            policy=policy,
            decision="ESCALATE",
            rule_id="eligibility.required-signal-failed",
            reason="One or more independently supplied signals did not pass.",
            signals_used=policy.required_signals,
            limitations=tuple(f"required_signal_failed:{name}" for name in failed_signals),
        )

    return _decision(
        diagnosis=validated_diagnosis,
        diagnosis_hash=diagnosis_hash,
        policy=policy,
        decision="ELIGIBLE_FOR_REMEDIATION",
        rule_id="eligibility.code-policy-satisfied",
        reason="The confirmed code cause satisfies confidence and independent-signal policy.",
        signals_used=policy.required_signals,
        limitations=(),
    )


def _validated_mapping(
    value: object,
    *,
    schema_path: Path,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EligibilityGateError(f"{label} must be an object")

    document = dict(value)
    validator = Draft202012Validator(_load_schema(schema_path))
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        first_error = errors[0]
        field = ".".join(str(part) for part in first_error.absolute_path) or "<root>"
        raise EligibilityGateError(f"invalid {label} at {field}: {first_error.message}")
    return document


def _load_policy(project_config: Mapping[str, Any]) -> _EligibilityPolicy:
    raw_policy = project_config.get("eligibility", {})
    if not isinstance(raw_policy, Mapping):
        raise EligibilityGateError("project configuration eligibility must be an object")

    required_signals = raw_policy.get("required_signals", [])
    if not isinstance(required_signals, Sequence) or isinstance(required_signals, str):
        raise EligibilityGateError("eligibility.required_signals must be an array")

    return _EligibilityPolicy(
        version=str(raw_policy.get("policy_version", _DEFAULT_POLICY_VERSION)),
        minimum_confidence=str(raw_policy.get("minimum_confidence", "MEDIUM")),
        require_confirmed_cause=bool(raw_policy.get("require_confirmed_cause", True)),
        required_signals=tuple(sorted(str(signal) for signal in required_signals)),
    )


def _validate_signals(signals: Mapping[str, bool] | None) -> dict[str, bool]:
    if signals is None:
        return {}
    if not isinstance(signals, Mapping):
        raise EligibilityGateError("independent_signals must be an object")

    validated: dict[str, bool] = {}
    for name, value in signals.items():
        if not isinstance(name, str) or not name.strip():
            raise EligibilityGateError("independent signal names must be non-empty strings")
        if not isinstance(value, bool):
            raise EligibilityGateError(f"independent signal {name!r} must be a boolean")
        validated[name] = value
    return validated


def _decision(
    *,
    diagnosis: Mapping[str, Any],
    diagnosis_hash: str,
    policy: _EligibilityPolicy,
    decision: str,
    rule_id: str,
    reason: str,
    signals_used: tuple[str, ...],
    limitations: tuple[str, ...],
) -> dict[str, Any]:
    result = {
        "schema_version": _SCHEMA_VERSION,
        "incident_id": diagnosis["incident_id"],
        "decision": decision,
        "policy_version": policy.version,
        "rule_id": rule_id,
        "signals_used": list(signals_used),
        "reason": reason,
        "limitations": list(limitations),
        "diagnosis_hash": diagnosis_hash,
    }
    validator = Draft202012Validator(_load_schema(_DECISION_SCHEMA_PATH))
    if validator.is_valid(result) is False:
        raise EligibilityGateError("eligibility gate produced an invalid decision")
    return result


def _hash_document(document: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


@cache
def _load_schema(path: Path) -> dict[str, Any]:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EligibilityGateError(f"cannot load schema: {path}") from error
    Draft202012Validator.check_schema(schema)
    return schema
