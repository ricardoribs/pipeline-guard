from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

from scripts.diagnosis_validator import (
    DiagnosisValidationError,
    validate_diagnosis,
)

sys.path.remove(str(PLUGIN_ROOT))

INCIDENT_ID = "incident-123"
EVIDENCES: list[Mapping[str, object]] = [{"evidence_id": "evidence-1"}]


def _valid_diagnosis() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "incident_id": INCIDENT_ID,
        "failure_family": "CODE",
        "confidence": "HIGH",
        "cause_status": "CONFIRMED",
        "cause": "A referenced model failed.",
        "evidence_citations": [
            {"evidence_id": "evidence-1", "claim": "The model run failed."}
        ],
    }


def _validate(
    diagnosis: object,
    *,
    evidences: list[Mapping[str, object]] | None = None,
    investigation_budget_exhausted: bool = False,
) -> dict[str, Any]:
    return validate_diagnosis(
        diagnosis,
        incident_id=INCIDENT_ID,
        evidences=EVIDENCES if evidences is None else evidences,
        investigation_budget_exhausted=investigation_budget_exhausted,
    )


def test_valid_diagnosis_with_existing_citation_is_preserved() -> None:
    diagnosis = _valid_diagnosis()

    assert _validate(diagnosis) == diagnosis


@pytest.mark.parametrize(
    ("diagnosis", "evidences", "budget_exhausted", "limitation"),
    [
        (
            {**_valid_diagnosis(), "unexpected": "field"},
            EVIDENCES,
            False,
            "diagnosis_schema_invalid",
        ),
        (
            {
                **_valid_diagnosis(),
                "evidence_citations": [
                    {"evidence_id": "missing-evidence", "claim": "No such evidence."}
                ],
            },
            EVIDENCES,
            False,
            "evidence_citation_not_found",
        ),
        (
            {**_valid_diagnosis(), "evidence_citations": []},
            EVIDENCES,
            False,
            "insufficient_evidence",
        ),
        (_valid_diagnosis(), EVIDENCES, True, "investigation_budget_exhausted"),
    ],
    ids=["invalid-schema", "unknown-citation", "no-evidence", "budget-exhausted"],
)
def test_unsafe_diagnoses_become_unknown(
    diagnosis: object,
    evidences: list[Mapping[str, object]],
    budget_exhausted: bool,
    limitation: str,
) -> None:
    result = validate_diagnosis(
        diagnosis,
        incident_id=INCIDENT_ID,
        evidences=evidences,
        investigation_budget_exhausted=budget_exhausted,
    )

    assert result["failure_family"] == "UNKNOWN"
    assert result["confidence"] == "LOW"
    assert result["cause_status"] == "UNKNOWN"
    assert result["evidence_citations"] == []
    assert result["limitations"] == [limitation]
    schema_path = (
        PLUGIN_ROOT / "contracts" / "schema" / "pipeline_diagnosis_v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(result)


def test_valid_unknown_diagnosis_may_have_no_citations() -> None:
    diagnosis = {
        **_valid_diagnosis(),
        "failure_family": "UNKNOWN",
        "confidence": "LOW",
        "cause_status": "UNKNOWN",
        "evidence_citations": [],
    }

    assert _validate(diagnosis) == diagnosis


def test_unknown_diagnosis_without_citations_uses_safe_fallback() -> None:
    diagnosis = {
        **_valid_diagnosis(),
        "cause_status": "UNKNOWN",
        "evidence_citations": [],
    }

    result = _validate(diagnosis)

    assert result["failure_family"] == "UNKNOWN"
    assert result["confidence"] == "LOW"
    assert result["cause_status"] == "UNKNOWN"
    assert result["evidence_citations"] == []
    assert result["limitations"] == ["insufficient_evidence"]


def test_diagnosis_for_a_different_incident_becomes_unknown() -> None:
    diagnosis = {**_valid_diagnosis(), "incident_id": "another-incident"}

    result = _validate(diagnosis)

    assert result["cause_status"] == "UNKNOWN"
    assert result["limitations"] == ["diagnosis_incident_id_mismatch"]


@pytest.mark.parametrize("incident_id", ["", "  "])
def test_trusted_incident_id_must_be_non_empty(incident_id: str) -> None:
    with pytest.raises(DiagnosisValidationError, match="incident_id"):
        validate_diagnosis(
            _valid_diagnosis(), incident_id=incident_id, evidences=EVIDENCES
        )
