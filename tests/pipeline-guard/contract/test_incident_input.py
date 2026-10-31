from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
INCIDENT_VALIDATOR_PATH = SCRIPTS_DIR / "incident_validator.py"


def _load_validator_module():
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "pipeline_guard_incident_validator", INCIDENT_VALIDATOR_PATH
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not load incident validator from {INCIDENT_VALIDATOR_PATH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS_DIR))


validator = _load_validator_module()


def _valid_incident() -> dict:
    return {
        "schema_version": "1.0",
        "incident_id": "incident-123",
        "source": "ci",
        "environment": "test",
        "pipeline_id": "daily-build",
        "run_id": "run-456",
        "failed_step": "compile",
        "occurred_at": "2026-10-01T12:00:00Z",
        "error_summary": "Compilation failed",
        "project_profile": "generic",
        "artifacts": [{"type": "log", "uri": "artifact://logs/build-1", "hash": "sha256:abc123"}],
    }


def _valid_unattended_incident() -> dict:
    return {
        **_valid_incident(),
        "schema_version": "1.1",
        "code_version": {
            "repository_url": "https://bitbucket.org/example-org/pipeline-guard.git",
            "branch": "main",
            "commit": "a" * 40,
        },
        "host_run": {
            "host": "bitbucket_pipelines",
            "run_reference": "build-789",
            "run_url": "https://bitbucket.org/example-org/pipeline-guard/addon/pipelines/home#!/results/789",
        },
    }


def test_contract_is_host_neutral_and_large_artifacts_are_references():
    incident = _valid_incident()

    assert validator.validate_incident_input(incident) == incident
    assert incident["source"] == "ci"
    assert incident["artifacts"][0]["uri"].startswith("artifact://")
    assert "content" not in incident["artifacts"][0]


def test_contract_accepts_v11_and_unattended_input():
    simple_incident = {**_valid_incident(), "schema_version": "1.1"}
    incident = _valid_unattended_incident()

    assert validator.validate_incident_input(simple_incident) == simple_incident
    assert validator.validate_incident_input(incident) == incident
    assert validator.validate_incident_input(incident, unattended=True) == incident


@pytest.mark.parametrize("v11_block", ["code_version", "host_run"])
def test_contract_rejects_v11_blocks_in_v10_documents(v11_block):
    incident = _valid_incident()
    incident[v11_block] = _valid_unattended_incident()[v11_block]

    with pytest.raises(validator.IncidentValidationError, match="should not be valid"):
        validator.validate_incident_input(incident)


def test_contract_rejects_repository_url_with_embedded_credential():
    incident = _valid_unattended_incident()
    incident["code_version"]["repository_url"] = "https://token@example.com/example-org/repo.git"

    with pytest.raises(validator.IncidentValidationError, match="repository_url"):
        validator.validate_incident_input(incident)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository_url", "https://token@example.com/example-org/repo.git"),
        ("repository_url", "https://bitbucket.org/example-org/repo.git?token=signed"),
        ("run_url", "https://token@example.com/pipelines/789"),
        ("run_url", "https://bitbucket.org/example-org/repo/pipelines/789?token=signed"),
    ],
)
def test_contract_rejects_credential_or_query_in_urls(field, value):
    incident = _valid_unattended_incident()
    container = "code_version" if field == "repository_url" else "host_run"
    incident[container][field] = value

    with pytest.raises(validator.IncidentValidationError, match=field):
        validator.validate_incident_input(incident)


def test_contract_accepts_urls_with_allowed_fragments():
    incident = _valid_unattended_incident()
    incident["host_run"]["run_url"] += "#step-log"

    assert validator.validate_incident_input(incident, unattended=True) == incident


def test_unattended_contract_requires_only_host_run():
    incident = _valid_unattended_incident()
    incident.pop("host_run")

    with pytest.raises(validator.IncidentValidationError, match="host_run"):
        validator.validate_incident_input(incident, unattended=True)


@pytest.mark.parametrize("missing_field", ["code_version", "commit", "branch"])
def test_unattended_contract_accepts_missing_code_version_input(missing_field):
    incident = _valid_unattended_incident()
    if missing_field == "code_version":
        incident.pop(missing_field)
    else:
        incident["code_version"].pop(missing_field)

    assert validator.validate_incident_input(incident, unattended=True) == incident


def test_contract_requires_fields_and_rejects_unmarked_unavailable_values():
    incident = _valid_incident()
    del incident["failed_step"]
    with pytest.raises(validator.IncidentValidationError, match=r"failed_step.*required"):
        validator.validate_incident_input(incident)

    incident = _valid_incident()
    incident["source"] = None
    with pytest.raises(validator.IncidentValidationError, match="unavailable_fields"):
        validator.validate_incident_input(incident)

    incident = _valid_incident()
    incident["error_summary"] = ""
    with pytest.raises(validator.IncidentValidationError, match="error_summary"):
        validator.validate_incident_input(incident)

    incident = _valid_incident()
    incident["failed_step"] = None
    incident["unavailable_fields"] = {
        "failed_step": "The orchestrator did not provide the failed step."
    }
    assert validator.validate_incident_input(incident) == incident

    incident["failed_step"] = "compile"
    with pytest.raises(validator.IncidentValidationError, match="failed_step"):
        validator.validate_incident_input(incident)


def test_contract_requires_artifact_reference_and_hash():
    incident = _valid_incident()
    incident["artifacts"][0].pop("hash")
    with pytest.raises(validator.IncidentValidationError, match=r"hash.*required"):
        validator.validate_incident_input(incident)


@pytest.mark.parametrize(
    "sensitive_incident_id",
    [
        "owner@example.com",
        "DATABRICKS_TOKEN=placeholder",
    ],
)
def test_contract_rejects_sensitive_incident_identifiers(sensitive_incident_id):
    incident = _valid_incident()
    incident["incident_id"] = sensitive_incident_id

    with pytest.raises(
        validator.IncidentValidationError,
        match=r"incident_id: must be an opaque identifier",
    ):
        validator.validate_incident_input(incident)


def test_contract_reuses_evidence_for_correlated_retry(tmp_path):
    artifact_store_path = SCRIPTS_DIR / "artifact_store.py"
    spec = importlib.util.spec_from_file_location(
        "pipeline_guard_contract_artifact_store", artifact_store_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load artifact store from {artifact_store_path}")
    artifact_store_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = artifact_store_module
    spec.loader.exec_module(artifact_store_module)
    store = artifact_store_module.LocalArtifactStore(plugin_data_dir=tmp_path)
    incident = _valid_incident()
    incident["correlation"] = {"attempt": 1}
    calls = []

    def collect_evidence(_incident):
        calls.append("collected")
        return {"evidence_id": "evidence-1", "uri": "artifact://evidence/1", "hash": "abc"}

    first = validator.validate_or_reuse_incident(
        incident, artifact_store=store, collect_evidence=collect_evidence
    )
    retry = {**incident, "run_id": "run-789", "correlation": {"attempt": 2}}
    second = validator.validate_or_reuse_incident(
        retry, artifact_store=store, collect_evidence=collect_evidence
    )

    assert first[1] == second[1]
    assert first[2] is False
    assert second[2] is True
    assert calls == ["collected"]
