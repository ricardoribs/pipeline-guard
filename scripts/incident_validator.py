"""Validation and retry idempotency for ``incident_input_v1`` payloads."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

try:  # Support direct script execution and package-style imports.
    from .artifact_store import ArtifactStore
    from .redaction import redact_text
except ImportError:  # pragma: no cover - used by direct script loading
    from artifact_store import ArtifactStore
    from redaction import redact_text

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_PATH = _PLUGIN_ROOT / "contracts" / "schema" / "incident_input_v1.schema.json"
_IDEMPOTENCY_REFERENCE = "processed.json"
_UNAVAILABLE_FIELDS = (
    "source",
    "environment",
    "pipeline_id",
    "run_id",
    "failed_step",
    "occurred_at",
    "error_summary",
)


class IncidentValidationError(ValueError):
    """Raised when an incident does not satisfy the canonical input contract."""


def validate_incident_input(payload: object, *, unattended: bool = False) -> dict[str, Any]:
    """Validate and return an incident payload without assuming a specific host.

    When ``unattended`` is true, require the host trace. Missing code-version
    fields remain valid so the caller can report ``MISSING_INPUT`` safely. The
    default preserves the 1.0 interactive contract.
    """
    if not isinstance(payload, dict):
        raise IncidentValidationError("incident_input_v1 must be a JSON object")

    if unattended:
        host_run = payload.get("host_run")
        if not isinstance(host_run, dict):
            raise IncidentValidationError("host_run: required for unattended remediation input")

    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        raise IncidentValidationError(f"{path}: {error.message}")

    incident_id = payload["incident_id"]
    if redact_text(incident_id) != incident_id:
        raise IncidentValidationError(
            "incident_id: must be an opaque identifier without PII or secret-shaped values"
        )

    unavailable_fields = payload.get("unavailable_fields", {})
    for field in _UNAVAILABLE_FIELDS:
        is_null = payload[field] is None
        has_unavailability_reason = field in unavailable_fields
        if is_null and not has_unavailability_reason:
            raise IncidentValidationError(
                f"{field}: null requires a non-empty reason in unavailable_fields.{field}"
            )
        if has_unavailability_reason and not is_null:
            raise IncidentValidationError(
                f"unavailable_fields.{field}: field must be null when marked unavailable"
            )

    return payload


def validate_or_reuse_incident(
    payload: object,
    *,
    artifact_store: ArtifactStore,
    collect_evidence: Callable[[Mapping[str, Any]], Any],
) -> tuple[dict[str, Any], Any, bool]:
    """Validate an incident and reuse prior evidence for a correlated retry.

    Returns ``(incident, evidence, reused)``. The host's stable ``incident_id``
    and ``correlation.attempt`` identify retries; the processed marker is stored
    under a stable, opaque key derived from ``incident_id`` in the configured
    artifact store, so a retry remains idempotent even if its ``run_id`` changes.
    """
    incident = validate_incident_input(payload)
    correlation = incident.get("correlation")
    attempt = correlation.get("attempt") if isinstance(correlation, Mapping) else None
    incident_key = hashlib.sha256(incident["incident_id"].encode("utf-8")).hexdigest()
    reference = _IDEMPOTENCY_REFERENCE

    if attempt is not None and artifact_store.exists(incident_key, reference):
        return incident, json.loads(artifact_store.get(incident_key, reference)), True

    evidence = collect_evidence(incident)
    if attempt is not None:
        try:
            serialized = json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError(
                "collected evidence must be JSON serializable for idempotency"
            ) from error
        artifact_store.put(incident_key, reference, serialized)

    return incident, evidence, False
