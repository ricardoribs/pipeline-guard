"""Contract tests for isolating dbt details from the diagnosis core."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Mapping
from importlib import import_module
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
DBT_FIXTURE_DIR = PLUGIN_ROOT / "tests" / "pipeline-guard" / "fixtures" / "dbt"
AGENT_PATH = PLUGIN_ROOT / "agents" / "diagnosis-agent.md"

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

DbtAdapter = import_module("adapters.dbt").DbtAdapter
validate_diagnosis = import_module("scripts.diagnosis_validator").validate_diagnosis

DBT_DETAIL_KEYS = {
    "adapter_response",
    "artifacts",
    "compiled_code",
    "dbt_version",
    "evidence_count",
    "failed_node_count",
    "failed_nodes",
    "failures",
    "invocation_id",
    "lineage",
    "name",
    "original_file_path",
    "relation_name",
    "resource_type",
    "status",
    "unique_id",
}


def _reference_json(name: str) -> dict[str, Any]:
    body = AGENT_PATH.read_text(encoding="utf-8")
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


def _nested_keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return set(value) | {
            nested_key
            for nested_value in value.values()
            for nested_key in _nested_keys(nested_value)
        }
    if isinstance(value, list):
        return {nested_key for item in value for nested_key in _nested_keys(item)}
    return set()


def _dbt_specific_values(failed_nodes: Iterable[Mapping[str, object]]) -> set[str]:
    detail_fields = (
        "compiled_code",
        "name",
        "original_file_path",
        "relation_name",
        "resource_type",
        "unique_id",
    )
    return {
        value
        for failed_node in failed_nodes
        for field in detail_fields
        if isinstance((value := failed_node.get(field)), str) and value
    }


def test_dbt_diagnosis_keeps_adapter_details_out_of_core(sample_incident: dict) -> None:
    reference_input = _reference_json("input")
    diagnosis = _reference_json("output")
    incident = {
        **sample_incident,
        "incident_id": reference_input["incident"]["incident_id"],
    }
    adapter_output = DbtAdapter(artifacts_dir=DBT_FIXTURE_DIR).run(incident)

    assert reference_input["evidences"] == adapter_output["evidences"]
    assert reference_input["adapter_details"] == adapter_output["adapter_details"]

    validated = validate_diagnosis(
        diagnosis,
        incident_id=incident["incident_id"],
        evidences=adapter_output["evidences"],
    )

    assert validated == diagnosis
    assert validated["cause_status"] == "CONFIRMED"

    adapter_details = validated["adapter_details"]["dbt"]
    failed_nodes = adapter_details["failed_nodes"]
    assert len(failed_nodes) == 2
    assert {node["resource_type"] for node in failed_nodes} == {"model", "test"}
    assert all(node["compiled_code"] for node in failed_nodes)
    assert _nested_keys(validated["adapter_details"]) >= DBT_DETAIL_KEYS

    diagnosis_core = {
        field: value for field, value in validated.items() if field != "adapter_details"
    }
    assert DBT_DETAIL_KEYS.isdisjoint(_nested_keys(diagnosis_core))

    serialized_core = json.dumps(diagnosis_core, ensure_ascii=False, sort_keys=True)
    fixture_payloads = [
        json.loads(evidence["content"]) for evidence in adapter_output["evidences"]
    ]
    for detail_value in _dbt_specific_values(fixture_payloads):
        assert json.dumps(detail_value, ensure_ascii=False) not in serialized_core
