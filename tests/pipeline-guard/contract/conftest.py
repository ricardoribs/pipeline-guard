"""Shared fixtures for PipelineAdapter contract tests.

The ``adapter_output`` fixture resolves each real adapter through the trusted
registry and submits its output to the shared contract suite.
"""

from __future__ import annotations

import hashlib
import json
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = PLUGIN_ROOT / "contracts" / "schema"
DBT_FIXTURE_DIR = PLUGIN_ROOT / "tests" / "pipeline-guard" / "fixtures" / "dbt"

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

DbtAdapter = import_module("adapters.dbt").DbtAdapter
MockAdapter = import_module("adapters.mock").MockAdapter
AdapterRegistry = import_module("adapters.registry").AdapterRegistry

ADAPTER_SCHEMA_PATH = SCHEMA_DIR / "pipeline_adapter_interface.schema.json"
INCIDENT_SCHEMA_PATH = SCHEMA_DIR / "incident_input_v1.schema.json"
DIAGNOSIS_SCHEMA_PATH = SCHEMA_DIR / "pipeline_diagnosis_v1.schema.json"


def _load_schema(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def adapter_schema() -> dict:
    return _load_schema(ADAPTER_SCHEMA_PATH)


@pytest.fixture(scope="session")
def adapter_validator(adapter_schema: dict) -> Draft202012Validator:
    Draft202012Validator.check_schema(adapter_schema)
    return Draft202012Validator(adapter_schema)


@pytest.fixture(scope="session")
def incident_input_schema() -> dict:
    return _load_schema(INCIDENT_SCHEMA_PATH)


@pytest.fixture(scope="session")
def incident_input_validator(incident_input_schema: dict) -> Draft202012Validator:
    return Draft202012Validator(incident_input_schema)


@pytest.fixture(scope="session")
def diagnosis_schema() -> dict:
    return _load_schema(DIAGNOSIS_SCHEMA_PATH)


@pytest.fixture(scope="session")
def diagnosis_validator(diagnosis_schema: dict) -> Draft202012Validator:
    return Draft202012Validator(diagnosis_schema)


# ---------------------------------------------------------------------------
# Real adapter outputs.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def sample_incident() -> dict:
    """Return a valid incident consumed by every adapter under contract."""
    artifacts = [
        {
            "type": artifact_type,
            "uri": f"artifact://dbt/{filename}",
            "hash": (
                "sha256:"
                + hashlib.sha256((DBT_FIXTURE_DIR / filename).read_bytes()).hexdigest()
            ),
        }
        for filename, artifact_type in (
            ("run_results.json", "dbt_run_results"),
            ("manifest.json", "dbt_manifest"),
        )
    ]

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
        "artifacts": artifacts,
    }


@pytest.fixture(params=["mock", "dbt"], ids=["mock", "dbt"])
def adapter(
    request: pytest.FixtureRequest,
) -> Any:
    """Resolve and instantiate every adapter through the trusted registry."""
    adapter_name = str(request.param)

    registry = AdapterRegistry(
        {
            "mock": MockAdapter,
            "dbt": lambda: DbtAdapter(artifacts_dir=DBT_FIXTURE_DIR),
        }
    )

    adapter_factory = registry.resolve(
        adapter_name,
        allowed_names={"mock", "dbt"},
    )
    return adapter_factory()


@pytest.fixture
def adapter_output(
    adapter: Any,
    sample_incident: dict,
) -> dict:
    """Run every adapter under contract with a fully populated incident."""
    return adapter.run(sample_incident)
