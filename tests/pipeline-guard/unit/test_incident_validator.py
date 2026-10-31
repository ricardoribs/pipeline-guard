from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"


def _load_module(module_name: str, path: Path):
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not load module from {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS_DIR))


artifact_store = _load_module("pipeline_guard_artifact_store", SCRIPTS_DIR / "artifact_store.py")
validator = _load_module(
    "pipeline_guard_incident_validator", SCRIPTS_DIR / "incident_validator.py"
)


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
    }


def test_correlated_retry_reuses_artifacts_even_when_run_id_changes(tmp_path):
    store = artifact_store.LocalArtifactStore(plugin_data_dir=tmp_path)
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
