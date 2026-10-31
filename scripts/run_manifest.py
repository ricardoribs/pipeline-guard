# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4.0.0"]
# ///
"""Build and persist a terminal manifest for a pipeline-guard diagnosis run."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

try:  # Support package-style imports.
    from .artifact_store import ArtifactStore
    from .redaction import redact_payload
except ImportError:  # pragma: no cover - used when imported from the scripts directory
    from artifact_store import ArtifactStore
    from redaction import redact_payload

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_PATH = _PLUGIN_ROOT / "contracts" / "schema" / "run_manifest_v1.schema.json"
_CONTRACT_SCHEMA_PATHS = {
    "eligibility_decision_v1": _PLUGIN_ROOT
    / "contracts"
    / "schema"
    / "eligibility_decision_v1.schema.json",
    "incident_input_v1": _PLUGIN_ROOT
    / "contracts"
    / "schema"
    / "incident_input_v1.schema.json",
    "pipeline_adapter_interface": _PLUGIN_ROOT
    / "contracts"
    / "schema"
    / "pipeline_adapter_interface.schema.json",
    "pipeline_diagnosis_v1": _PLUGIN_ROOT
    / "contracts"
    / "schema"
    / "pipeline_diagnosis_v1.schema.json",
}
_MANIFEST_REFERENCE = "manifest.json"
_SCHEMA_VERSION = "1.0"
_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "STOPPED_UNKNOWN"})
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMING_FIELDS = frozenset(
    {"created_at", "started_at", "finished_at", "duration_seconds"}
)
_RUN_ID_KEY_FILENAME = ".run-id-hmac.key"
_RUN_ID_KEY_BYTES = 32


class RunManifestError(ValueError):
    """Raised when a run manifest cannot be built, validated, or persisted safely."""


class RunManifestConflictError(RunManifestError):
    """Raised when an immutable manifest reference already contains different bytes."""


def derive_manifest_run_id(
    *,
    incident_id: str,
    source_run_id: str | None,
    attempt_payload: object | None = None,
    plugin_data_dir: Path | str | None = None,
) -> str:
    """Derive a safe store key for one idempotent diagnosis attempt.

    The optional keyed payload fingerprint keeps identical retries idempotent while
    allowing a corrected request for the same external run to create its own
    immutable terminal manifest. Its key is created once in CLAUDE_PLUGIN_DATA
    (or plugin_data_dir), outside run artifacts, and must be retained for retries.
    Existing IDs without an attempt payload remain unchanged.
    """
    if not isinstance(incident_id, str) or not incident_id.strip():
        raise RunManifestError("incident_id must be a non-empty string")
    if source_run_id is not None and (
        not isinstance(source_run_id, str) or not source_run_id.strip()
    ):
        raise RunManifestError("source_run_id must be null or a non-empty string")

    try:
        serialized_attempt = (
            _serialize_json(attempt_payload) if attempt_payload is not None else None
        )
    except (TypeError, ValueError) as error:
        raise RunManifestError("attempt_payload must be JSON serializable") from error

    attempt_fingerprint = (
        hmac.new(
            _load_run_id_key(plugin_data_dir), serialized_attempt, hashlib.sha256
        ).hexdigest()
        if serialized_attempt is not None
        else None
    )

    identity_fields = {
        "incident_id": incident_id.strip(),
        "source_run_id": source_run_id,
    }
    if attempt_fingerprint is not None:
        identity_fields["attempt_fingerprint"] = attempt_fingerprint
    identity = _serialize_json(identity_fields)
    return f"diagnose-{hashlib.sha256(identity).hexdigest()}"


def _load_run_id_key(plugin_data_dir: Path | str | None) -> bytes:
    """Publish one private installation key atomically, without overwriting it."""
    if plugin_data_dir is None:
        plugin_data_dir = os.environ.get("CLAUDE_PLUGIN_DATA")
    if plugin_data_dir is None or not str(plugin_data_dir).strip():
        raise RunManifestError(
            "CLAUDE_PLUGIN_DATA is required for keyed attempt identifiers"
        )

    data_dir = Path(plugin_data_dir).expanduser()
    key_path = data_dir / _RUN_ID_KEY_FILENAME
    temporary_path: Path | None = None
    try:
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if key_path.is_symlink():
            raise RunManifestError("run ID key must not be a symbolic link")
        if not key_path.exists():
            # NamedTemporaryFile is private on POSIX; on Windows it inherits the
            # installation data directory's ACL. Close before linking on Windows.
            with tempfile.NamedTemporaryFile(
                dir=data_dir, prefix=".run-id-key-", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(secrets.token_bytes(_RUN_ID_KEY_BYTES))
                temporary.flush()
                os.fsync(temporary.fileno())
            try:
                # Readers see a complete key; competing creators use the winner.
                os.link(temporary_path, key_path)
            except FileExistsError:
                if key_path.is_symlink():
                    raise RunManifestError(
                        "run ID key must not be a symbolic link"
                    ) from None
        key = key_path.read_bytes()
        if len(key) != _RUN_ID_KEY_BYTES:
            raise RunManifestError("run ID key must contain exactly 32 bytes")
        return key
    except OSError as error:
        raise RunManifestError(
            "could not initialize or read the installation run ID key"
        ) from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _serialize_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def current_contract_versions() -> dict[str, str]:
    """Return versions declared by the contracts used in the V1 diagnosis flow."""
    versions: dict[str, str] = {}
    for name, path in sorted(_CONTRACT_SCHEMA_PATHS.items()):
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
            version_rule = schema["properties"]["schema_version"]
            version = version_rule.get("const")
            if version is None:
                supported_versions = version_rule["enum"]
                if not isinstance(supported_versions, list) or not supported_versions:
                    raise TypeError("schema_version.enum must be a non-empty list")
                version = supported_versions[-1]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise RunManifestError(
                f"could not resolve version for contract {name!r}"
            ) from error
        if not isinstance(version, str) or not version:
            raise RunManifestError(
                f"contract {name!r} declares an invalid schema version"
            )
        versions[name] = version
    return versions


def build_run_manifest(
    *,
    run_id: str,
    incident_id: str,
    status: str,
    started_at: datetime,
    finished_at: datetime,
    evidences: Sequence[Mapping[str, object]] = (),
    limitations: Sequence[str] = (),
    contract_versions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build, sanitize, and validate one terminal run manifest."""
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        raise RunManifestError(
            "run_id must contain only letters, digits, dots, underscores, or hyphens"
        )
    if not isinstance(incident_id, str) or not incident_id.strip():
        raise RunManifestError("incident_id must be a non-empty string")
    if status not in _TERMINAL_STATUSES:
        allowed = ", ".join(sorted(_TERMINAL_STATUSES))
        raise RunManifestError(f"status must be terminal; expected one of: {allowed}")

    started = _require_aware_datetime(started_at, "started_at")
    finished = _require_aware_datetime(finished_at, "finished_at")
    if finished < started:
        raise RunManifestError("finished_at cannot be earlier than started_at")

    versions = _normalize_contract_versions(
        current_contract_versions() if contract_versions is None else contract_versions
    )
    manifest: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "run_id": run_id,
        "incident_id": incident_id.strip(),
        "status": status,
        "created_at": _timestamp(finished),
        "started_at": _timestamp(started),
        "finished_at": _timestamp(finished),
        "duration_seconds": round((finished - started).total_seconds(), 6),
        "contract_versions": versions,
        "evidence_hashes": _normalize_evidence_hashes(evidences),
        "limitations": _normalize_limitations(limitations),
    }
    sanitized = redact_payload(manifest)
    if not isinstance(
        sanitized, dict
    ):  # Defensive: the redactor preserves JSON containers.
        raise RunManifestError("sanitization produced an invalid manifest")
    return validate_run_manifest(sanitized)


def validate_run_manifest(manifest: object) -> dict[str, Any]:
    """Validate and copy a manifest against its internal JSON schema."""
    if not isinstance(manifest, dict):
        raise RunManifestError("run_manifest_v1 must be a JSON object")

    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            manifest
        ),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        raise RunManifestError(f"{path}: {error.message}")

    return json.loads(json.dumps(manifest, ensure_ascii=False, allow_nan=False))


def persist_run_manifest(manifest: object, *, artifact_store: ArtifactStore) -> bool:
    """Persist an immutable manifest, returning false for an identical retry."""
    document = validate_run_manifest(redact_payload(manifest))
    run_id = document["run_id"]
    serialized = _serialize(document)

    if artifact_store.put(run_id, _MANIFEST_REFERENCE, serialized):
        return True

    try:
        existing = artifact_store.get(run_id, _MANIFEST_REFERENCE)
    except FileNotFoundError as error:
        raise RunManifestConflictError(
            "artifact store reported an existing manifest but could not read it"
        ) from error
    if existing == serialized:
        return False

    try:
        existing_document = validate_run_manifest(json.loads(existing))
    except (json.JSONDecodeError, UnicodeDecodeError, RunManifestError) as error:
        raise RunManifestConflictError(
            f"run {run_id!r} already has an invalid immutable manifest"
        ) from error
    if _without_timing(existing_document) != _without_timing(document):
        raise RunManifestConflictError(
            f"run {run_id!r} already has a different immutable manifest"
        )
    return False


def load_run_manifest(*, run_id: str, artifact_store: ArtifactStore) -> dict[str, Any]:
    """Load and validate the persisted manifest for ``run_id``."""
    try:
        payload = json.loads(artifact_store.get(run_id, _MANIFEST_REFERENCE))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RunManifestError(f"could not load manifest for run {run_id!r}") from error
    return validate_run_manifest(payload)


def _require_aware_datetime(value: datetime, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise RunManifestError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalize_contract_versions(versions: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(versions, Mapping) or not versions:
        raise RunManifestError("contract_versions must contain at least one contract")
    normalized: dict[str, str] = {}
    for name, version in sorted(versions.items()):
        if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_]*", name) is None:
            raise RunManifestError(
                "contract version names must use lowercase snake_case"
            )
        if not isinstance(version, str) or not version.strip():
            raise RunManifestError(f"contract {name!r} must have a non-empty version")
        normalized[name] = version.strip()
    return normalized


def _normalize_evidence_hashes(
    evidences: Sequence[Mapping[str, object]],
) -> list[dict[str, str]]:
    if isinstance(evidences, str | bytes | bytearray):
        raise RunManifestError("evidences must be a sequence of objects")
    normalized: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for evidence in evidences:
        if not isinstance(evidence, Mapping):
            raise RunManifestError("evidences must contain only objects")
        evidence_id = evidence.get("evidence_id")
        content_hash = evidence.get("hash")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise RunManifestError("every evidence must have a non-empty evidence_id")
        if evidence_id in seen_ids:
            raise RunManifestError(f"duplicate evidence_id {evidence_id!r}")
        if (
            not isinstance(content_hash, str)
            or _SHA256_RE.fullmatch(content_hash) is None
        ):
            raise RunManifestError(
                f"evidence {evidence_id!r} hash must use sha256:<64 lowercase hex>"
            )
        seen_ids.add(evidence_id)
        normalized.append({"evidence_id": evidence_id, "hash": content_hash})
    return sorted(normalized, key=lambda item: item["evidence_id"])


def _normalize_limitations(limitations: Sequence[str]) -> list[str]:
    if isinstance(limitations, str | bytes | bytearray):
        raise RunManifestError("limitations must be a sequence of strings")
    normalized: list[str] = []
    seen: set[str] = set()
    for limitation in limitations:
        if not isinstance(limitation, str) or not limitation.strip():
            raise RunManifestError("limitations must contain only non-empty strings")
        value = limitation.strip()
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return normalized


def _serialize(manifest: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _without_timing(manifest: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in manifest.items() if key not in _TIMING_FIELDS}
