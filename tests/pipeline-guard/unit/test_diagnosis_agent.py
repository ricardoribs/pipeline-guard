"""Contract tests for the read-only pipeline diagnosis agent profile."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
AGENT_PATH = PLUGIN_ROOT / "agents" / "diagnosis-agent.md"
DIAGNOSIS_SCHEMA_PATH = PLUGIN_ROOT / "contracts" / "schema" / "pipeline_diagnosis_v1.schema.json"


def _load_profile() -> tuple[dict[str, Any], str]:
    document = AGENT_PATH.read_text(encoding="utf-8")
    parts = document.split("---", maxsplit=2)
    assert len(parts) == 3, "diagnosis-agent.md must contain YAML frontmatter"

    metadata = yaml.safe_load(parts[1])
    assert isinstance(metadata, dict)
    return metadata, parts[2]


def _extract_reference_json(body: str, name: str) -> dict[str, Any]:
    pattern = re.compile(
        rf"<!-- reference-{name}-start -->\s*```json\s*(.*?)\s*```\s*"
        rf"<!-- reference-{name}-end -->",
        re.DOTALL,
    )
    match = pattern.search(body)
    assert match is not None, f"missing {name} reference JSON"

    document = json.loads(match.group(1))
    assert isinstance(document, dict)
    return document


def test_profile_declares_no_tools() -> None:
    metadata, _ = _load_profile()

    assert metadata["name"] == "diagnosis-agent"
    assert metadata["model"] == "sonnet"
    assert metadata["tools"] == []


def test_profile_enforces_evidence_boundary_and_json_only_output() -> None:
    _, body = _load_profile()
    normalized_body = " ".join(body.split())

    required_rules = (
        "Use only the incident, evidence records, and `adapter_details` supplied",
        "Require evidence content inline; never open or follow an evidence or artifact path",
        "You have no tools",
        "Never access a Git remote, network service, warehouse, database",
        "Return exactly one JSON object with no Markdown fence",
        "Every cited `evidence_id` must exist in the supplied evidence records",
        "Keep adapter-specific details exclusively under `adapter_details.<adapter_name>`",
        "Describe the core fields in adapter-neutral language",
    )
    for rule in required_rules:
        assert rule in normalized_body

    for forbidden_field in ("approved", "eligible", "next_step", "merge", "deploy"):
        assert f"`{forbidden_field}`" in body


def test_reference_diagnosis_is_valid_and_cites_only_supplied_evidence() -> None:
    _, body = _load_profile()
    reference_input = _extract_reference_json(body, "input")
    diagnosis = _extract_reference_json(body, "output")
    schema = json.loads(DIAGNOSIS_SCHEMA_PATH.read_text(encoding="utf-8"))

    Draft202012Validator(schema).validate(diagnosis)

    assert diagnosis["incident_id"] == reference_input["incident"]["incident_id"]
    supplied_evidence_ids = {evidence["evidence_id"] for evidence in reference_input["evidences"]}
    cited_evidence_ids = {citation["evidence_id"] for citation in diagnosis["evidence_citations"]}
    assert cited_evidence_ids
    assert cited_evidence_ids <= supplied_evidence_ids


def test_reference_diagnosis_contains_no_decision_or_execution_fields() -> None:
    _, body = _load_profile()
    diagnosis = _extract_reference_json(body, "output")

    forbidden_fields = {"approved", "eligible", "next_step", "merge", "deploy"}
    assert forbidden_fields.isdisjoint(diagnosis)
