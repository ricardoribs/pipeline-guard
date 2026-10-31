from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ARTIFACT_STORE_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "artifact_store.py"
)


def _load_artifact_store_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "pipeline_guard_artifact_store",
        ARTIFACT_STORE_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"could not load artifact store module from {ARTIFACT_STORE_PATH}"
        )

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_artifact_store_module = _load_artifact_store_module()
ArtifactStoreConfigurationError = _artifact_store_module.ArtifactStoreConfigurationError
ArtifactSession = _artifact_store_module.ArtifactSession
LocalArtifactStore = _artifact_store_module.LocalArtifactStore
SessionArtifactStore = _artifact_store_module.SessionArtifactStore
resolve_artifact_store = _artifact_store_module.resolve_artifact_store


def test_local_store_put_get_and_exists(tmp_path):
    store = LocalArtifactStore(plugin_data_dir=tmp_path)

    assert store.exists("run-123", "diagnosis.json") is False
    assert store.put("run-123", "diagnosis.json", b'{"status":"ok"}') is True
    assert store.exists("run-123", "diagnosis.json") is True
    assert store.get("run-123", "diagnosis.json") == b'{"status":"ok"}'
    assert (tmp_path / "runs" / "run-123").is_dir()


def test_local_store_uses_claude_plugin_data(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

    store = LocalArtifactStore()
    artifact = b"result"
    assert store.put("run-123", "result.json", artifact) is True

    assert store.get("run-123", "result.json") == artifact
    assert (tmp_path / "runs" / "run-123").is_dir()


def test_local_store_reuses_existing_reference_without_overwriting(tmp_path):
    store = LocalArtifactStore(plugin_data_dir=tmp_path)
    reference = "stages/diagnose/result.json"

    assert store.put("run-123", reference, b"first") is True
    assert store.put("run-123", reference, b"second") is False
    assert store.get("run-123", reference) == b"first"


def test_local_session_removes_only_artifacts_created_in_its_scope(tmp_path):
    store = LocalArtifactStore(plugin_data_dir=tmp_path)
    store.put("run-123", "processed.json", b"durable")

    assert isinstance(store, SessionArtifactStore)

    with store.session() as session:
        assert isinstance(session, ArtifactSession)
        assert session.put("run-123", "logs/build.log", b"temporary log") is True
        assert (
            session.put("run-123", "logs/build.index.json", b"temporary index") is True
        )
        assert session.exists("run-123", "logs/build.log") is True

    assert store.exists("run-123", "processed.json") is True
    assert store.exists("run-123", "logs/build.log") is False
    assert store.exists("run-123", "logs/build.index.json") is False


def test_local_session_cleans_up_when_collection_raises(tmp_path):
    store = LocalArtifactStore(plugin_data_dir=tmp_path)
    session = store.session()

    def collect_and_fail():
        with session:
            session.put("run-123", "logs/build.log", b"temporary log")
            raise RuntimeError("collection failed")

    with pytest.raises(RuntimeError, match="collection failed"):
        collect_and_fail()

    assert store.exists("run-123", "logs/build.log") is False
    with pytest.raises(RuntimeError, match="session is closed"):
        session.get("run-123", "logs/build.log")


def test_local_store_rejects_unsafe_run_ids(tmp_path):
    store = LocalArtifactStore(plugin_data_dir=tmp_path)

    with pytest.raises(ValueError, match="run_id"):
        store.put("../outside", "artifact", b"value")


def test_host_store_is_used_instead_of_local_store(tmp_path):
    class HostStore:
        def __init__(self):
            self.artifacts = {}

        def put(self, run_id, reference, artifact):
            key = (run_id, reference)
            if key in self.artifacts:
                return False
            self.artifacts[key] = artifact
            return True

        def get(self, run_id, reference):
            return self.artifacts[(run_id, reference)]

        def exists(self, run_id, reference):
            return (run_id, reference) in self.artifacts

    host_store = HostStore()
    store = resolve_artifact_store(
        {"runtime": {"artifact_store": "local"}},
        host_reference=host_store,
        plugin_data_dir=tmp_path,
    )

    assert store is host_store
    assert store.put("run-123", "result", b"host") is True
    assert store.get("run-123", "result") == b"host"
    assert store.exists("run-123", "result") is True
    assert not (tmp_path / "runs").exists()


def test_missing_store_configuration_does_not_fall_back_to_local(tmp_path):
    with pytest.raises(ArtifactStoreConfigurationError, match="not configured"):
        resolve_artifact_store({}, plugin_data_dir=tmp_path)

    assert not (tmp_path / "runs").exists()


def test_host_store_reference_is_required_for_non_local_backend():
    with pytest.raises(
        ArtifactStoreConfigurationError, match="provide its host implementation"
    ):
        resolve_artifact_store({"runtime": {"artifact_store": "host"}})


def test_local_store_requires_plugin_data_directory(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)

    with pytest.raises(ArtifactStoreConfigurationError, match="CLAUDE_PLUGIN_DATA"):
        LocalArtifactStore()
