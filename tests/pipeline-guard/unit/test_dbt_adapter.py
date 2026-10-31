"""Unit tests for the dbt pipeline adapter."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from importlib import import_module
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = PLUGIN_ROOT / "tests" / "pipeline-guard" / "fixtures" / "dbt"

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


def _artifact_reference(path: Path, artifact_type: str) -> dict[str, str]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "type": artifact_type,
        "uri": f"artifact://dbt/{path.name}",
        "hash": f"sha256:{digest}",
    }


def _sample_incident(artifacts_dir: Path = FIXTURE_DIR) -> dict:
    artifacts = []

    for filename, artifact_type in (
        ("run_results.json", "dbt_run_results"),
        ("manifest.json", "dbt_manifest"),
    ):
        artifact_path = artifacts_dir / filename

        if artifact_path.is_file():
            artifacts.append(_artifact_reference(artifact_path, artifact_type))

    return {
        "schema_version": "1.0",
        "incident_id": "INC-DBT-001",
        "source": "ci",
        "environment": "production",
        "pipeline_id": "daily-dbt-build",
        "run_id": "run-456",
        "failed_step": "dbt build",
        "occurred_at": "2026-10-02T12:00:04Z",
        "error_summary": "A model failed and a data test returned three rows.",
        "project_profile": "dbt",
        "artifacts": artifacts,
    }


def _run_adapter(
    artifacts_dir: Path = FIXTURE_DIR,
    incident: dict | None = None,
) -> dict:
    dbt_adapter = import_module("adapters.dbt").DbtAdapter
    if incident is None:
        incident = _sample_incident(artifacts_dir)

    return dbt_adapter(artifacts_dir=artifacts_dir).run(incident)


def test_proposes_supported_logical_validation_commands() -> None:
    dbt_adapter = import_module("adapters.dbt").DbtAdapter

    assert dbt_adapter().propose_validation_commands() == (
        "deps",
        "parse",
        "compile",
        "test",
        "build",
    )


def _evidence_payloads(output: dict) -> list[dict]:
    return [json.loads(evidence["content"]) for evidence in output["evidences"]]


def test_collects_one_evidence_for_each_failed_node() -> None:
    output = _run_adapter()

    assert output["adapter_name"] == "dbt"
    assert output["incident_id"] == "INC-DBT-001"
    assert len(output["evidences"]) == 2

    payloads = _evidence_payloads(output)

    assert {payload["unique_id"] for payload in payloads} == {
        "model.pipeline_guard.fct_orders",
        "test.pipeline_guard.not_null_fct_orders_customer_id.4f3a2b1c",
    }
    assert {payload["status"] for payload in payloads} == {"error", "fail"}

    for evidence in output["evidences"]:
        assert evidence["evidence_id"]
        assert evidence["origin"] == "dbt"
        assert evidence["type"] == "METADATA"
        assert evidence["hash"].startswith("sha256:")
        assert evidence["retention"] == "session"


def test_enriches_failure_with_manifest_metadata_and_compiled_code() -> None:
    output = _run_adapter()

    payload = next(
        payload
        for payload in _evidence_payloads(output)
        if payload["unique_id"] == "model.pipeline_guard.fct_orders"
    )

    assert payload["name"] == "fct_orders"
    assert payload["resource_type"] == "model"
    assert payload["original_file_path"] == "models/marts/fct_orders.sql"
    assert payload["compiled_code"] == (
        "select order_id, customer_id from analytics.staging.stg_orders"
    )


def test_failed_test_references_related_model_from_lineage() -> None:
    output = _run_adapter()

    test_payload = next(
        payload
        for payload in _evidence_payloads(output)
        if payload["unique_id"] == "test.pipeline_guard.not_null_fct_orders_customer_id.4f3a2b1c"
    )

    assert test_payload["lineage"] == {
        "depends_on": [
            {
                "unique_id": "model.pipeline_guard.fct_orders",
                "name": "fct_orders",
                "resource_type": "model",
                "original_file_path": "models/marts/fct_orders.sql",
                "relation_name": '"analytics"."marts"."fct_orders"',
            }
        ]
    }


def test_failed_model_references_its_direct_upstream_model() -> None:
    output = _run_adapter()

    model_payload = next(
        payload
        for payload in _evidence_payloads(output)
        if payload["unique_id"] == "model.pipeline_guard.fct_orders"
    )

    assert model_payload["lineage"] == {
        "depends_on": [
            {
                "unique_id": "model.pipeline_guard.stg_orders",
                "name": "stg_orders",
                "resource_type": "model",
                "original_file_path": "models/staging/stg_orders.sql",
                "relation_name": '"analytics"."staging"."stg_orders"',
            }
        ]
    }


def test_successful_nodes_do_not_generate_failure_evidence() -> None:
    output = _run_adapter()
    payloads = _evidence_payloads(output)

    assert all(payload["unique_id"] != "model.pipeline_guard.stg_orders" for payload in payloads)


def test_evidence_hash_matches_its_content() -> None:
    output = _run_adapter()

    for evidence in output["evidences"]:
        digest = hashlib.sha256(evidence["content"].encode("utf-8")).hexdigest()
        assert evidence["hash"] == f"sha256:{digest}"


def test_repeated_collection_is_deterministic() -> None:
    first = _run_adapter()
    second = _run_adapter()

    assert first == second


def test_ignores_stale_artifacts_not_declared_by_incident() -> None:
    incident = _sample_incident()
    incident["artifacts"] = []

    output = _run_adapter(incident=incident)

    assert output["evidences"] == []
    assert output["adapter_details"]["dbt"]["artifacts"] == {
        "run_results.json": "missing",
        "manifest.json": "missing",
    }


def test_rejects_declared_artifact_when_hash_does_not_match() -> None:
    adapter_module = import_module("adapters.dbt.adapter")
    incident = _sample_incident()
    incident["artifacts"][0]["hash"] = f"sha256:{'0' * 64}"

    with pytest.raises(adapter_module.DbtArtifactError, match="does not match its incident hash"):
        _run_adapter(incident=incident)


def test_node_without_depends_on_has_empty_lineage(tmp_path: Path) -> None:
    shutil.copy2(
        FIXTURE_DIR / "run_results.json",
        tmp_path / "run_results.json",
    )

    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))

    test_id = "test.pipeline_guard.not_null_fct_orders_customer_id.4f3a2b1c"
    del manifest["nodes"][test_id]["depends_on"]

    (tmp_path / "manifest.json").write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    output = _run_adapter(tmp_path)

    test_payload = next(
        payload for payload in _evidence_payloads(output) if payload["unique_id"] == test_id
    )

    assert test_payload["lineage"] == {"depends_on": []}


def test_missing_manifest_does_not_prevent_collection(tmp_path: Path) -> None:
    shutil.copy2(
        FIXTURE_DIR / "run_results.json",
        tmp_path / "run_results.json",
    )

    output = _run_adapter(tmp_path)

    assert len(output["evidences"]) == 2
    assert output["adapter_details"]["dbt"]["artifacts"] == {
        "run_results.json": "available",
        "manifest.json": "missing",
    }


def test_missing_run_results_returns_valid_empty_collection(tmp_path: Path) -> None:
    shutil.copy2(
        FIXTURE_DIR / "manifest.json",
        tmp_path / "manifest.json",
    )

    output = _run_adapter(tmp_path)

    assert output["evidences"] == []
    assert output["adapter_details"]["dbt"]["failed_node_count"] == 0
    assert output["adapter_details"]["dbt"]["artifacts"] == {
        "run_results.json": "missing",
        "manifest.json": "available",
    }
