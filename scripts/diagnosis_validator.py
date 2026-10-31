# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4.0.0"]
# ///
"""Deterministic validation and safe fallback for ``pipeline_diagnosis_v1``."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "schema"
    / "pipeline_diagnosis_v1.schema.json"
)
_SCHEMA_VERSION = "1.0"


class DiagnosisValidationError(ValueError):
    """Raised when trusted context required for validation is invalid."""


def validate_diagnosis(
    diagnosis: object,
    *,
    incident_id: str,
    evidences: Iterable[Mapping[str, object]],
    investigation_budget_exhausted: bool = False,
) -> dict[str, Any]:
    """Validate a diagnosis and citations, replacing unsafe results with ``UNKNOWN``."""
    if not isinstance(incident_id, str) or not incident_id.strip():
        raise DiagnosisValidationError("incident_id must be a non-empty string")
    if not isinstance(investigation_budget_exhausted, bool):
        raise DiagnosisValidationError("investigation_budget_exhausted must be a boolean")

    if investigation_budget_exhausted:
        return _unknown_diagnosis(incident_id, "investigation_budget_exhausted")

    if not isinstance(diagnosis, Mapping):
        return _unknown_diagnosis(incident_id, "diagnosis_schema_invalid")

    validator = Draft202012Validator(_load_schema())
    if validator.is_valid(diagnosis) is False:
        return _unknown_diagnosis(incident_id, "diagnosis_schema_invalid")

    if diagnosis["incident_id"] != incident_id:
        return _unknown_diagnosis(incident_id, "diagnosis_incident_id_mismatch")

    evidence_ids = {
        evidence_id
        for evidence in evidences
        if isinstance(evidence, Mapping)
        and isinstance((evidence_id := evidence.get("evidence_id")), str)
        and evidence_id
    }
    citations = diagnosis["evidence_citations"]
    cited_ids = [citation["evidence_id"] for citation in citations]

    if any(evidence_id not in evidence_ids for evidence_id in cited_ids):
        return _unknown_diagnosis(incident_id, "evidence_citation_not_found")

    if diagnosis["cause_status"] == "UNKNOWN" and (
        diagnosis["failure_family"] != "UNKNOWN" or diagnosis["confidence"] != "LOW"
    ):
        return _unknown_diagnosis(incident_id, "insufficient_evidence")

    if diagnosis["cause_status"] != "UNKNOWN" and not cited_ids:
        return _unknown_diagnosis(incident_id, "insufficient_evidence")

    return dict(diagnosis)


def _load_schema() -> dict[str, Any]:
    """Load and check the local diagnosis schema."""
    try:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DiagnosisValidationError(
            f"cannot load pipeline_diagnosis_v1 schema: {_SCHEMA_PATH}"
        ) from error

    Draft202012Validator.check_schema(schema)
    return schema


def _unknown_diagnosis(incident_id: str, limitation: str) -> dict[str, Any]:
    """Build a minimal contract-valid result that does not preserve invalid claims."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "incident_id": incident_id,
        "failure_family": "UNKNOWN",
        "confidence": "LOW",
        "cause_status": "UNKNOWN",
        "cause": "The available evidence is insufficient to validate a diagnosis.",
        "evidence_citations": [],
        "limitations": [limitation],
    }
