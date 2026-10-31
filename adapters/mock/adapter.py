"""Deterministic PipelineAdapter mock used by contract tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

_SCHEMA_VERSION = "1.0"
_ADAPTER_NAME = "mock"
_RETENTION = "session"


class MockAdapter:
    """Produce deterministic synthetic evidence without external services."""

    name = _ADAPTER_NAME

    def run(self, incident: Mapping[str, Any]) -> dict[str, Any]:
        """Collect synthetic evidence for a valid incident input."""
        incident_id = _required_text(incident, "incident_id")
        pipeline_id = _nullable_text(incident, "pipeline_id")
        run_id = _nullable_text(incident, "run_id")
        failed_step = _nullable_text(incident, "failed_step")

        failed_step_label = failed_step if failed_step is not None else "<unavailable>"
        log_content = f"Synthetic failure at step {failed_step_label}."
        metadata_content = json.dumps(
            {
                "failed_step": failed_step,
                "pipeline_id": pipeline_id,
                "run_id": run_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        evidences = [
            _make_evidence(
                evidence_id=f"{incident_id}:mock-log",
                evidence_type="LOG",
                content=log_content,
            ),
            _make_evidence(
                evidence_id=f"{incident_id}:mock-metadata",
                evidence_type="METADATA",
                content=metadata_content,
            ),
        ]

        return {
            "schema_version": _SCHEMA_VERSION,
            "adapter_name": _ADAPTER_NAME,
            "incident_id": incident_id,
            "evidences": evidences,
            "adapter_details": {
                "mock": {
                    "synthetic": True,
                    "pipeline_id": pipeline_id,
                    "run_id": run_id,
                    "failed_step": failed_step,
                    "evidence_count": len(evidences),
                }
            },
        }


def _required_text(incident: Mapping[str, Any], field: str) -> str:
    value = incident.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"incident field {field!r} must be a non-empty string")
    return value


def _nullable_text(incident: Mapping[str, Any], field: str) -> str | None:
    if field not in incident:
        raise ValueError(f"incident field {field!r} is required")

    value = incident[field]
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"incident field {field!r} must be null or a non-empty string")
    return value


def _make_evidence(
    *,
    evidence_id: str,
    evidence_type: str,
    content: str,
) -> dict[str, Any]:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

    return {
        "evidence_id": evidence_id,
        "origin": _ADAPTER_NAME,
        "type": evidence_type,
        "hash": f"sha256:{digest}",
        "retention": _RETENTION,
        "content": content,
    }
