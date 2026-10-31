"""Contract tests for PipelineAdapter output.

Validates that any PipelineAdapter produces output conforming to
``pipeline_adapter_interface.schema.json``.  The same suite is reused for
every adapter by parameterising the ``adapter_output`` fixture in
``conftest.py``.

Scope: exercises ``incident_input_v1`` as the adapter's input contract
and ``pipeline_diagnosis_v1`` only for downstream evidence-citation
compatibility.  No other canonical workflow contract is referenced.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

EVIDENCE_TYPES = {"LOG", "ARTIFACT", "METADATA", "QUERY", "LINK"}

# ── Schema self-validation ──────────────────────────────────────────────


class TestAdapterSchemaValidity:
    def test_schema_is_valid_draft_2020_12(self, adapter_schema: dict) -> None:
        Draft202012Validator.check_schema(adapter_schema)

    def test_schema_has_additional_properties_false(self, adapter_schema: dict) -> None:
        assert adapter_schema.get("additionalProperties") is False

    def test_evidence_items_have_additional_properties_false(
        self, adapter_schema: dict
    ) -> None:
        evidence_items = adapter_schema["properties"]["evidences"]["items"]
        assert evidence_items.get("additionalProperties") is False

    def test_adapter_details_is_required(self, adapter_schema: dict) -> None:
        assert "adapter_details" in adapter_schema["required"]


# ── Adapter output validation ───────────────────────────────────────────


class TestAdapterOutputContract:
    def test_output_validates_against_schema(
        self, adapter_validator: Draft202012Validator, adapter_output: dict
    ) -> None:
        adapter_validator.validate(adapter_output)

    def test_schema_version_is_1_0(self, adapter_output: dict) -> None:
        assert adapter_output["schema_version"] == "1.0"

    def test_evidences_contain_required_fields(self, adapter_output: dict) -> None:
        required = {"evidence_id", "origin", "type", "hash", "retention", "content"}
        for evidence in adapter_output["evidences"]:
            assert required.issubset(evidence.keys())

    def test_evidence_ids_are_non_empty_strings(self, adapter_output: dict) -> None:
        for evidence in adapter_output["evidences"]:
            assert isinstance(evidence["evidence_id"], str)
            assert len(evidence["evidence_id"]) >= 1

    def test_evidence_types_are_in_allowed_set(self, adapter_output: dict) -> None:
        for evidence in adapter_output["evidences"]:
            assert evidence["type"] in EVIDENCE_TYPES

    def test_adapter_details_is_present(self, adapter_output: dict) -> None:
        assert "adapter_details" in adapter_output
        assert isinstance(adapter_output["adapter_details"], dict)


# ── Negative validation ─────────────────────────────────────────────────


class TestAdapterOutputRejection:
    def test_rejects_extra_top_level_property(
        self, adapter_validator: Draft202012Validator, adapter_output: dict
    ) -> None:
        invalid = {**adapter_output, "unexpected_field": "value"}
        with pytest.raises(ValidationError, match="Additional properties"):
            adapter_validator.validate(invalid)

    def test_rejects_extra_evidence_property(
        self, adapter_validator: Draft202012Validator, adapter_output: dict
    ) -> None:
        invalid = copy.deepcopy(adapter_output)
        invalid["evidences"][0]["unexpected_field"] = "value"
        with pytest.raises(ValidationError, match="Additional properties"):
            adapter_validator.validate(invalid)

    @pytest.mark.parametrize(
        "missing_field",
        [
            "schema_version",
            "adapter_name",
            "incident_id",
            "evidences",
            "adapter_details",
        ],
    )
    def test_rejects_missing_required_top_level_field(
        self,
        adapter_validator: Draft202012Validator,
        adapter_output: dict,
        missing_field: str,
    ) -> None:
        invalid = {k: v for k, v in adapter_output.items() if k != missing_field}
        with pytest.raises(ValidationError):
            adapter_validator.validate(invalid)

    @pytest.mark.parametrize(
        "missing_field",
        ["evidence_id", "origin", "type", "hash", "retention", "content"],
    )
    def test_rejects_missing_required_evidence_field(
        self,
        adapter_validator: Draft202012Validator,
        adapter_output: dict,
        missing_field: str,
    ) -> None:
        invalid = copy.deepcopy(adapter_output)
        del invalid["evidences"][0][missing_field]
        with pytest.raises(ValidationError):
            adapter_validator.validate(invalid)

    def test_rejects_empty_adapter_name(
        self, adapter_validator: Draft202012Validator, adapter_output: dict
    ) -> None:
        invalid = {**adapter_output, "adapter_name": ""}
        with pytest.raises(ValidationError):
            adapter_validator.validate(invalid)

    def test_rejects_empty_evidence_id(
        self, adapter_validator: Draft202012Validator, adapter_output: dict
    ) -> None:
        invalid = copy.deepcopy(adapter_output)
        invalid["evidences"][0]["evidence_id"] = ""
        with pytest.raises(ValidationError):
            adapter_validator.validate(invalid)

    def test_rejects_invalid_evidence_type(
        self, adapter_validator: Draft202012Validator, adapter_output: dict
    ) -> None:
        invalid = copy.deepcopy(adapter_output)
        invalid["evidences"][0]["type"] = "UNKNOWN_TYPE"
        with pytest.raises(ValidationError):
            adapter_validator.validate(invalid)


# ── Input contract (incident_input_v1) ──────────────────────────────────


class TestAdapterInputContract:
    @pytest.fixture
    def sample_incident(self) -> dict:
        return {
            "schema_version": "1.0",
            "incident_id": "INC-001",
            "source": "ci",
            "environment": "production",
            "pipeline_id": "pipeline-123",
            "run_id": "run-456",
            "failed_step": "dbt_test",
            "occurred_at": "2026-09-29T12:00:00Z",
            "error_summary": "Model failed: unique test on orders.order_id",
            "project_profile": "dbt",
        }

    def test_sample_incident_validates_against_incident_schema(
        self,
        incident_input_validator: Draft202012Validator,
        sample_incident: dict,
    ) -> None:
        incident_input_validator.validate(sample_incident)

    def test_adapter_accepts_valid_incident_with_unavailable_fields(
        self,
        adapter: Any,
        adapter_validator: Draft202012Validator,
        incident_input_validator: Draft202012Validator,
        sample_incident: dict,
    ) -> None:
        incident = copy.deepcopy(sample_incident)
        unavailable_fields = {
            "pipeline_id": "The host did not provide a pipeline identifier.",
            "run_id": "The host did not provide a run identifier.",
            "failed_step": "The host did not identify the failed step.",
        }
        incident.update(dict.fromkeys(unavailable_fields))
        incident["unavailable_fields"] = unavailable_fields

        incident_input_validator.validate(incident)

        output = adapter.run(incident)

        adapter_validator.validate(output)
        assert output["incident_id"] == incident["incident_id"]

        mock_details = output["adapter_details"].get("mock")
        if mock_details is not None:
            assert mock_details["pipeline_id"] is None
            assert mock_details["run_id"] is None
            assert mock_details["failed_step"] is None


# ── Downstream compatibility (pipeline_diagnosis_v1) ────────────────────


class TestDownstreamCompatibility:
    def test_evidence_ids_are_valid_diagnosis_citations(
        self,
        diagnosis_validator: Draft202012Validator,
        adapter_output: dict,
    ) -> None:
        citations = [
            {"evidence_id": ev["evidence_id"], "claim": "Validated by contract test."}
            for ev in adapter_output["evidences"]
        ]
        diagnosis = {
            "schema_version": "1.0",
            "incident_id": adapter_output["incident_id"],
            "failure_family": "UNKNOWN",
            "confidence": "LOW",
            "cause_status": "HYPOTHESIS",
            "cause": "Contract test example cause.",
            "evidence_citations": citations,
        }
        diagnosis_validator.validate(diagnosis)
