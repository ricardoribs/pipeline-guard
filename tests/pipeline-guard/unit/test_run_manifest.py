from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT))

# These runtime imports must follow sys.path injection; Ruff versions classify them differently.
# isort: off
from scripts.artifact_store import LocalArtifactStore  # noqa: E402
from scripts.redaction import redact_payload  # noqa: E402
from scripts.run_manifest import (  # noqa: E402
    RunManifestConflictError,
    RunManifestError,
    build_run_manifest,
    current_contract_versions,
    derive_manifest_run_id,
    load_run_manifest,
    persist_run_manifest,
    validate_run_manifest,
)
# isort: on

sys.path.remove(str(PLUGIN_ROOT))

STARTED_AT = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
FINISHED_AT = STARTED_AT + timedelta(seconds=12.345678)


def _evidence(evidence_id: str, content: str) -> dict[str, str]:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {"evidence_id": evidence_id, "hash": f"sha256:{digest}", "content": content}


def _manifest(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "run_id": "run-manifest-001",
        "incident_id": "incident-manifest-001",
        "status": "COMPLETED",
        "started_at": STARTED_AT,
        "finished_at": FINISHED_AT,
        "evidences": [_evidence("evidence-b", "second"), _evidence("evidence-a", "first")],
        "limitations": ["warehouse metadata unavailable"],
    }
    values.update(overrides)
    return build_run_manifest(**values)


def _validate_schema(manifest: dict[str, Any]) -> None:
    path = PLUGIN_ROOT / "contracts" / "schema" / "run_manifest_v1.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(manifest)


def test_complete_manifest_records_versions_hashes_limitations_and_timing() -> None:
    manifest = _manifest()

    assert manifest["schema_version"] == "1.0"
    assert manifest["run_id"] == "run-manifest-001"
    assert manifest["incident_id"] == "incident-manifest-001"
    assert manifest["status"] == "COMPLETED"
    assert manifest["created_at"] == "2026-10-07T12:00:12.345678Z"
    assert manifest["started_at"] == "2026-10-07T12:00:00.000000Z"
    assert manifest["finished_at"] == "2026-10-07T12:00:12.345678Z"
    assert manifest["duration_seconds"] == 12.345678
    assert manifest["contract_versions"] == current_contract_versions()
    assert [item["evidence_id"] for item in manifest["evidence_hashes"]] == [
        "evidence-a",
        "evidence-b",
    ]
    assert manifest["limitations"] == ["warehouse metadata unavailable"]
    _validate_schema(manifest)


def test_current_contract_versions_uses_latest_declared_incident_input_version() -> None:
    versions = current_contract_versions()

    assert versions["incident_input_v1"] == "1.1"


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "STOPPED_UNKNOWN"])
def test_only_terminal_execution_statuses_are_supported(status: str) -> None:
    manifest = _manifest(status=status)

    assert manifest["status"] == status
    _validate_schema(manifest)


@pytest.mark.parametrize(
    ("source_run_id", "incident_id"),
    [
        ("scheduled__2026-10-07T10:00:00+00:00", "incident-airflow-001"),
        (None, "incident-without-source-run"),
    ],
)
def test_manifest_run_id_is_safe_and_deterministic(
    source_run_id: str | None,
    incident_id: str,
) -> None:
    first = derive_manifest_run_id(
        incident_id=incident_id,
        source_run_id=source_run_id,
    )
    second = derive_manifest_run_id(
        incident_id=incident_id,
        source_run_id=source_run_id,
    )

    assert first == second
    assert first.startswith("diagnose-")
    assert len(first) == len("diagnose-") + 64
    assert ":" not in first
    assert "+" not in first


def test_corrected_attempt_uses_a_distinct_manifest_run_id(tmp_path: Path) -> None:
    invalid = {"incident_id": "incident-001", "run_id": "run-001"}
    corrected = {**invalid, "source": "ci"}

    failed_run_id = derive_manifest_run_id(
        incident_id=invalid["incident_id"],
        source_run_id=invalid["run_id"],
        attempt_payload=invalid,
        plugin_data_dir=tmp_path,
    )
    completed_run_id = derive_manifest_run_id(
        incident_id=corrected["incident_id"],
        source_run_id=corrected["run_id"],
        attempt_payload=corrected,
        plugin_data_dir=tmp_path,
    )

    assert failed_run_id != completed_run_id


def _attempt_run_id(data_dir: Path, payload: object) -> str:
    return derive_manifest_run_id(
        incident_id="incident-001",
        source_run_id="run-001",
        attempt_payload=payload,
        plugin_data_dir=data_dir,
    )


def test_attempt_fingerprint_uses_installation_hmac(tmp_path: Path) -> None:
    key = b"synthetic-test-key".ljust(32, b"0")
    (tmp_path / ".run-id-hmac.key").write_bytes(key)
    payload = {"token": "synthetic-secret"}
    fingerprint = hmac.new(key, b'{"token":"synthetic-secret"}', hashlib.sha256).hexdigest()
    identity = json.dumps(
        {
            "incident_id": "incident-001",
            "source_run_id": "run-001",
            "attempt_fingerprint": fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    assert _attempt_run_id(tmp_path, payload) == f"diagnose-{hashlib.sha256(identity).hexdigest()}"


def test_identical_attempt_reuses_persisted_key_and_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    payload = {"token": "synthetic-secret"}
    first = _attempt_run_id(tmp_path, payload)
    key = (tmp_path / ".run-id-hmac.key").read_bytes()
    second = derive_manifest_run_id(
        incident_id="incident-001",
        source_run_id="run-001",
        attempt_payload=payload,
    )

    assert first == second
    assert len(key) == 32
    assert (tmp_path / ".run-id-hmac.key").read_bytes() == key
    assert not (tmp_path / "runs").exists()
    assert list(tmp_path.iterdir()) == [tmp_path / ".run-id-hmac.key"]
    if os.name != "nt":
        assert (tmp_path / ".run-id-hmac.key").stat().st_mode & 0o777 == 0o600


def test_different_installations_use_distinct_attempt_ids(tmp_path: Path) -> None:
    payload = {"token": "synthetic-secret"}

    assert _attempt_run_id(tmp_path / "first", payload) != _attempt_run_id(
        tmp_path / "second", payload
    )


def test_corrected_secrets_do_not_collide_after_redaction(tmp_path: Path) -> None:
    first = {"error_summary": "Authorization: Bearer synthetic-secret-first-123"}
    corrected = {"error_summary": "Authorization: Bearer synthetic-secret-second-456"}

    assert redact_payload(first) == redact_payload(corrected)
    assert _attempt_run_id(tmp_path, first) != _attempt_run_id(tmp_path, corrected)


def test_concurrent_attempts_publish_one_complete_key(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(
            executor.map(lambda _: _attempt_run_id(tmp_path, {"token": "synthetic"}), range(16))
        )

    assert len(set(ids)) == 1
    assert len((tmp_path / ".run-id-hmac.key").read_bytes()) == 32
    assert list(tmp_path.iterdir()) == [tmp_path / ".run-id-hmac.key"]


def test_attempt_id_fails_without_installation_directory(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)

    with pytest.raises(RunManifestError, match="CLAUDE_PLUGIN_DATA"):
        derive_manifest_run_id(
            incident_id="incident-001",
            source_run_id="run-001",
            attempt_payload={},
        )


@pytest.mark.parametrize("key", [b"", b"short", b"x" * 33])
def test_invalid_installation_key_is_rejected_without_replacement(
    tmp_path: Path, key: bytes
) -> None:
    key_path = tmp_path / ".run-id-hmac.key"
    key_path.write_bytes(key)

    with pytest.raises(RunManifestError, match="exactly 32 bytes"):
        _attempt_run_id(tmp_path, {})

    assert key_path.read_bytes() == key


def test_key_publication_failure_does_not_fall_back_to_plain_hash(
    tmp_path: Path, monkeypatch
) -> None:
    def deny_link(*_args, **_kwargs):
        raise PermissionError("synthetic publication failure")

    monkeypatch.setattr(os, "link", deny_link)

    with pytest.raises(RunManifestError, match="installation run ID key"):
        _attempt_run_id(tmp_path, {})

    assert list(tmp_path.iterdir()) == []


def test_manifest_run_id_without_attempt_payload_remains_backward_compatible() -> None:
    run_id = derive_manifest_run_id(
        incident_id="incident-airflow-001",
        source_run_id="scheduled__2026-10-07T10:00:00+00:00",
    )

    assert run_id == ("diagnose-15bcd9de0dc9786c81360ded0f28722e93a19565dd21224c2189dbabecaaf293")


def test_non_terminal_status_is_rejected_instead_of_waiting_for_human() -> None:
    with pytest.raises(RunManifestError, match="status must be terminal"):
        _manifest(status="WAITING_FOR_APPROVAL")


def test_manifest_is_sanitized_before_persistence(tmp_path: Path) -> None:
    store = LocalArtifactStore(plugin_data_dir=tmp_path)
    manifest = _manifest()
    manifest["incident_id"] = "owner@example.com"
    manifest["limitations"] = [
        "contact owner@example.com",
        "Authorization: Bearer literal-token-value-123456",
    ]

    persist_run_manifest(manifest, artifact_store=store)
    persisted = load_run_manifest(run_id=manifest["run_id"], artifact_store=store)
    serialized = json.dumps(persisted)

    assert "owner@example.com" not in serialized
    assert "literal-token-value-123456" not in serialized
    assert persisted["incident_id"] == "[REDACTED_EMAIL]"
    assert persisted["limitations"] == [
        "contact [REDACTED_EMAIL]",
        "Authorization: Bearer [REDACTED_SECRET]",
    ]


def test_local_store_persists_and_loads_manifest_by_run_id(tmp_path: Path) -> None:
    store = LocalArtifactStore(plugin_data_dir=tmp_path)
    manifest = _manifest()

    assert persist_run_manifest(manifest, artifact_store=store) is True
    assert store.exists(manifest["run_id"], "manifest.json") is True
    assert load_run_manifest(run_id=manifest["run_id"], artifact_store=store) == manifest
    assert (tmp_path / "runs" / manifest["run_id"]).is_dir()


def test_identical_manifest_retry_reuses_immutable_artifact(tmp_path: Path) -> None:
    store = LocalArtifactStore(plugin_data_dir=tmp_path)
    manifest = _manifest()

    assert persist_run_manifest(manifest, artifact_store=store) is True
    original = store.get(manifest["run_id"], "manifest.json")
    assert persist_run_manifest(manifest, artifact_store=store) is False
    assert store.get(manifest["run_id"], "manifest.json") == original


def test_semantically_identical_retry_preserves_original_timestamps(tmp_path: Path) -> None:
    store = LocalArtifactStore(plugin_data_dir=tmp_path)
    original = _manifest()
    retry = _manifest(
        started_at=STARTED_AT + timedelta(minutes=1),
        finished_at=FINISHED_AT + timedelta(minutes=1),
    )

    assert persist_run_manifest(original, artifact_store=store) is True
    original_bytes = store.get(original["run_id"], "manifest.json")
    assert persist_run_manifest(retry, artifact_store=store) is False
    assert store.get(original["run_id"], "manifest.json") == original_bytes


def test_different_manifest_for_same_run_is_rejected_without_overwrite(tmp_path: Path) -> None:
    store = LocalArtifactStore(plugin_data_dir=tmp_path)
    original = _manifest()
    conflicting = _manifest(limitations=["different limitation"])

    persist_run_manifest(original, artifact_store=store)
    original_bytes = store.get(original["run_id"], "manifest.json")

    with pytest.raises(RunManifestConflictError, match="different immutable manifest"):
        persist_run_manifest(conflicting, artifact_store=store)

    assert store.get(original["run_id"], "manifest.json") == original_bytes


@pytest.mark.parametrize(
    ("evidences", "message"),
    [
        ([{"evidence_id": "evidence-1", "hash": "sha256:not-a-digest"}], "64 lowercase"),
        (
            [_evidence("duplicate", "first"), _evidence("duplicate", "second")],
            "duplicate evidence_id",
        ),
        ([{"hash": f"sha256:{'0' * 64}"}], "non-empty evidence_id"),
    ],
)
def test_invalid_evidence_metadata_is_rejected(
    evidences: list[dict[str, str]], message: str
) -> None:
    with pytest.raises(RunManifestError, match=message):
        _manifest(evidences=evidences)


def test_invalid_timing_and_identifiers_are_rejected() -> None:
    with pytest.raises(RunManifestError, match="finished_at"):
        _manifest(finished_at=STARTED_AT - timedelta(seconds=1))
    with pytest.raises(RunManifestError, match="timezone-aware"):
        _manifest(started_at=datetime(2026, 10, 7, 12, 0))
    with pytest.raises(RunManifestError, match="run_id"):
        _manifest(run_id="../outside")
    with pytest.raises(RunManifestError, match="incident_id"):
        _manifest(incident_id="  ")


def test_scalar_collections_are_rejected() -> None:
    with pytest.raises(RunManifestError, match="evidences must be a sequence"):
        _manifest(evidences="not-a-sequence-of-evidence-objects")
    with pytest.raises(RunManifestError, match="limitations must be a sequence"):
        _manifest(limitations="not-a-sequence-of-limitations")


def test_schema_validation_rejects_unknown_fields_and_non_terminal_status() -> None:
    manifest = _manifest()
    manifest["unexpected"] = True

    with pytest.raises(RunManifestError, match="unexpected"):
        validate_run_manifest(manifest)

    manifest.pop("unexpected")
    manifest["status"] = "RUNNING"
    with pytest.raises(RunManifestError, match="RUNNING"):
        validate_run_manifest(manifest)
