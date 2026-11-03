# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4.0.0"]
# ///
"""Issue and consume offline policy authorizations for unattended remediation."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

# isort: split
from scripts.approval_authenticator import canonical_approval_bytes
from scripts.approval_validator import validate_approval
from scripts.artifact_store import ArtifactSession, ArtifactStore, DurableArtifactStore

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_BASE_SCHEMA_PATH = (
    _PLUGIN_ROOT / "contracts" / "schema" / "approval_input_v1.schema.json"
)
_POLICY_PROFILE_PATH = (
    _PLUGIN_ROOT
    / "contracts"
    / "schema"
    / "approval_input_v1.policy_profile.schema.json"
)
_SIGNATURE_PREFIX = "hmac-sha256:"
_SIGNATURE_PATTERN = re.compile(r"^hmac-sha256:([0-9a-fA-F]{64})$")
_SECRET_REFERENCE_PATTERN = re.compile(
    r"^(?:env:[A-Z][A-Z0-9_]*|\$\{[A-Z][A-Z0-9_]*\}|secret:[A-Za-z0-9][A-Za-z0-9._/-]*)$"
)
_CONSUMPTION_RUN_ID = "authorization-consumption"


class PolicyAuthorizationError(ValueError):
    """Raised when policy authorization cannot safely be issued or consumed."""


def issue_policy_authorization(
    *,
    config: Mapping[str, object],
    incident: Mapping[str, object],
    diagnosis_hash: str,
    eligibility_decision: str,
    current_time: datetime | None = None,
    host_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Create a signed authorization after the unattended policy gate passes."""
    unattended = _enabled_unattended_config(config)
    if eligibility_decision != "ELIGIBLE_FOR_REMEDIATION":
        raise PolicyAuthorizationError(
            "policy authorization requires ELIGIBLE_FOR_REMEDIATION"
        )

    now = current_time if current_time is not None else datetime.now(UTC)
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise PolicyAuthorizationError("current_time must be a timezone-aware datetime")

    actor = _required_string(unattended, "service_identity")
    secret = resolve_authorization_secret(
        _required_string(unattended, "authorization_secret_ref"),
        host_environment=host_environment,
    )
    document: dict[str, Any] = {
        "schema_version": "1.0",
        "incident_id": _required_string(incident, "incident_id"),
        "diagnosis_hash": _non_empty_string(diagnosis_hash, "diagnosis_hash"),
        "decision": "APPROVED",
        "actor": actor,
        "decided_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "channel_type": "policy",
        "external_reference": _host_run_reference(incident),
        "base_commit": _code_version_commit(incident),
    }
    document["signature"] = _sign(document, secret)
    _validate_base_contract(document)
    _validate_policy_profile(document)
    return document


def validate_policy_authorization(
    authorization: object,
    *,
    config: Mapping[str, object],
    expected_incident_id: str,
    expected_diagnosis_hash: str,
    expected_base_commit: str,
    expected_external_reference: str,
    current_time: datetime | None = None,
    host_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Authenticate and validate an issued policy authorization without network access."""
    unattended = _enabled_unattended_config(config)
    expected_actor = _required_string(unattended, "service_identity")
    expected_host_run = _non_empty_string(
        expected_external_reference, "expected_external_reference"
    )
    secret = resolve_authorization_secret(
        _required_string(unattended, "authorization_secret_ref"),
        host_environment=host_environment,
    )
    _validate_base_contract(authorization)
    _validate_policy_profile(authorization)
    try:
        validated = validate_approval(
            authorization,
            expected_incident_id=expected_incident_id,
            expected_diagnosis_hash=expected_diagnosis_hash,
            expected_base_commit=expected_base_commit,
            max_age=timedelta(seconds=_max_age_seconds(unattended)),
            current_time=current_time,
            unattended=True,
            shared_secret=secret,
        )
    except ValueError as error:
        raise PolicyAuthorizationError(str(error)) from error
    if validated["actor"] != expected_actor:
        raise PolicyAuthorizationError(
            "actor: policy authorization does not match unattended.service_identity"
        )
    if validated["external_reference"] != expected_host_run:
        raise PolicyAuthorizationError(
            "external_reference: policy authorization does not match the host run"
        )
    return validated


def validate_and_consume_policy_authorization(
    authorization: object,
    *,
    config: Mapping[str, object],
    expected_incident_id: str,
    expected_diagnosis_hash: str,
    expected_base_commit: str,
    expected_external_reference: str,
    artifact_store: ArtifactStore,
    current_time: datetime | None = None,
    host_environment: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], str]:
    """Validate an authorization and atomically consume it before remediation."""
    validated = validate_policy_authorization(
        authorization,
        config=config,
        expected_incident_id=expected_incident_id,
        expected_diagnosis_hash=expected_diagnosis_hash,
        expected_base_commit=expected_base_commit,
        expected_external_reference=expected_external_reference,
        current_time=current_time,
        host_environment=host_environment,
    )
    reference = _consume_validated_policy_authorization(
        validated, artifact_store=artifact_store
    )
    return validated, reference


def _consume_validated_policy_authorization(
    authorization: Mapping[str, object],
    *,
    artifact_store: ArtifactStore,
) -> str:
    """Consume a validated authorization once and return its reference."""
    if not isinstance(artifact_store, ArtifactStore):
        raise TypeError("artifact_store must implement put, get, and exists")
    if isinstance(artifact_store, ArtifactSession):
        raise TypeError(
            "artifact_store must be durable; session-scoped stores cannot consume authorizations"
        )
    if (
        not isinstance(artifact_store, DurableArtifactStore)
        or artifact_store.is_durable is not True
    ):
        raise TypeError(
            "artifact_store must explicitly declare durable cross-execution persistence"
        )
    reference = authorization_consumption_reference(authorization)
    if not artifact_store.put(_CONSUMPTION_RUN_ID, reference, b"consumed"):
        raise PolicyAuthorizationError("policy authorization was already consumed")
    return reference


def authorization_consumption_reference(authorization: Mapping[str, object]) -> str:
    """Return the stable marker address for one signed authorization."""
    external_reference = _required_string(authorization, "external_reference")
    signature = _required_string(authorization, "signature")
    match = _SIGNATURE_PATTERN.fullmatch(signature)
    if match is None:
        raise PolicyAuthorizationError(
            "signature must use hmac-sha256 with a 64-digit digest"
        )
    signature_bytes = bytes.fromhex(match.group(1))
    signed_document = canonical_approval_bytes(authorization) + b"\n" + signature_bytes
    digest = hashlib.sha256(signed_document).hexdigest()
    return f"{external_reference}:{digest}"


def resolve_authorization_secret(
    secret_ref: str,
    *,
    host_environment: Mapping[str, str] | None = None,
) -> bytes:
    """Resolve a host secret reference without ever returning it in an error."""
    if (
        not isinstance(secret_ref, str)
        or _SECRET_REFERENCE_PATTERN.fullmatch(secret_ref) is None
    ):
        raise PolicyAuthorizationError(
            "authorization_secret_ref must be an env:... or secret:... reference"
        )
    environment = host_environment if host_environment is not None else os.environ
    variable = _secret_variable_name(secret_ref, environment)
    value = environment.get(variable)
    if not value:
        raise PolicyAuthorizationError(
            "authorization secret was not found in the host environment"
        )
    secret = value.encode("utf-8")
    if len(secret) < 32:
        raise PolicyAuthorizationError(
            "authorization secret must contain at least 32 bytes"
        )
    return secret


def _enabled_unattended_config(config: Mapping[str, object]) -> Mapping[str, object]:
    unattended = config.get("unattended")
    if (
        not isinstance(unattended, Mapping)
        or unattended.get("remediation_enabled") is not True
    ):
        raise PolicyAuthorizationError(
            "unattended remediation must be explicitly enabled"
        )
    return cast(Mapping[str, object], unattended)


def _host_run_reference(incident: Mapping[str, object]) -> str:
    host_run = incident.get("host_run")
    if not isinstance(host_run, Mapping):
        raise PolicyAuthorizationError("incident.host_run is required")
    for field in ("run_url", "run_reference"):
        value = host_run.get(field)
        if isinstance(value, str) and value.strip():
            return value
    raise PolicyAuthorizationError(
        "incident.host_run requires run_url or run_reference"
    )


def _code_version_commit(incident: Mapping[str, object]) -> str:
    code_version = incident.get("code_version")
    if not isinstance(code_version, Mapping):
        raise PolicyAuthorizationError("incident.code_version is required")
    return _required_string(code_version, "commit")


def _max_age_seconds(unattended: Mapping[str, object]) -> int:
    value = unattended.get("authorization_max_age_seconds", 900)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PolicyAuthorizationError(
            "authorization_max_age_seconds must be a positive integer"
        )
    return value


def _required_string(document: Mapping[str, object], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PolicyAuthorizationError(f"{field} must be a non-empty string")
    return value


def _non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyAuthorizationError(f"{label} must be a non-empty string")
    return value


def _secret_variable_name(secret_ref: str, environment: Mapping[str, str]) -> str:
    if secret_ref.startswith("env:"):
        return secret_ref[4:]
    if secret_ref.startswith("${"):
        return secret_ref[2:-1]
    secret_name = secret_ref[7:].replace("/", "_").replace("-", "_").upper()
    default_name = f"SECRET_{secret_name}"
    if default_name not in environment and secret_ref in environment:
        return secret_ref
    return default_name


def _sign(document: Mapping[str, object], secret: bytes) -> str:
    digest = hmac.new(
        secret, canonical_approval_bytes(document), hashlib.sha256
    ).hexdigest()
    return f"{_SIGNATURE_PREFIX}{digest}"


def _validate_base_contract(document: object) -> None:
    _validate_contract(document, _base_validator(), "approval_input_v1")


def _validate_policy_profile(document: object) -> None:
    _validate_contract(document, _policy_validator(), "policy authorization profile")


def _validate_contract(
    document: Any, validator: Draft202012Validator, label: str
) -> None:
    errors = sorted(
        validator.iter_errors(document), key=lambda error: tuple(error.absolute_path)
    )
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        raise PolicyAuthorizationError(
            f"{label} validation failed at {path}: {error.message}"
        )


@cache
def _base_validator() -> Draft202012Validator:
    return _load_validator(_BASE_SCHEMA_PATH)


@cache
def _policy_validator() -> Draft202012Validator:
    return _load_validator(_POLICY_PROFILE_PATH)


def _load_validator(path: Path) -> Draft202012Validator:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PolicyAuthorizationError(
            f"cannot load authorization schema: {path}"
        ) from error
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())
