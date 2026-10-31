"""Read-only adapter for collecting evidence from dbt artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

_SCHEMA_VERSION = "1.0"
_ADAPTER_NAME = "dbt"
_RETENTION = "session"

_RUN_RESULTS_FILE = "run_results.json"
_MANIFEST_FILE = "manifest.json"
_RUN_RESULTS_ARTIFACT_TYPE = "dbt_run_results"
_MANIFEST_ARTIFACT_TYPE = "dbt_manifest"
_ARTIFACT_TYPE_BY_FILE = {
    _RUN_RESULTS_FILE: _RUN_RESULTS_ARTIFACT_TYPE,
    _MANIFEST_FILE: _MANIFEST_ARTIFACT_TYPE,
}

_FAILED_STATUSES = frozenset(
    {
        "error",
        "fail",
        "runtime error",
        "partial success",
    }
)

_VALIDATION_COMMANDS = (
    "deps",
    "parse",
    "compile",
    "test",
    "build",
)


class DbtArtifactError(ValueError):
    """Raised when an available dbt artifact cannot be interpreted safely."""


class DbtAdapter:
    """Collect deterministic evidence from local dbt artifacts."""

    name = _ADAPTER_NAME

    def __init__(self, artifacts_dir: str | Path = "target") -> None:
        """Configure the directory containing dbt-generated artifacts."""
        self._artifacts_dir = Path(artifacts_dir)

    def propose_validation_commands(self) -> tuple[str, ...]:
        """Return the logical validation commands supported by the dbt adapter.

        The executor resolves each logical name through ``validation.commands`` in
        the consumer configuration.  This adapter deliberately does not provide
        an executable command string.
        """
        return _VALIDATION_COMMANDS

    def run(self, incident: Mapping[str, Any]) -> dict[str, Any]:
        """Interpret dbt artifacts and return contract-compatible evidence."""
        incident_id = _required_text(incident, "incident_id")
        artifact_declarations = _dbt_artifact_declarations(incident)

        run_results = _load_declared_json_object(
            self._artifacts_dir / _RUN_RESULTS_FILE,
            artifact_declarations.get(_RUN_RESULTS_ARTIFACT_TYPE),
        )
        manifest = _load_declared_json_object(
            self._artifacts_dir / _MANIFEST_FILE,
            artifact_declarations.get(_MANIFEST_ARTIFACT_TYPE),
        )

        failed_results = _failed_results(run_results)
        manifest_nodes = _manifest_nodes(manifest)

        evidences = [
            _make_failure_evidence(
                incident_id=incident_id,
                result=result,
                manifest_node=_find_manifest_node(
                    manifest_nodes,
                    _required_result_text(result, "unique_id"),
                ),
                manifest_nodes=manifest_nodes,
            )
            for result in failed_results
        ]

        metadata = _artifact_metadata(run_results)

        return {
            "schema_version": _SCHEMA_VERSION,
            "adapter_name": _ADAPTER_NAME,
            "incident_id": incident_id,
            "evidences": evidences,
            "adapter_details": {
                "dbt": {
                    "artifacts": {
                        _RUN_RESULTS_FILE: (
                            "available" if run_results is not None else "missing"
                        ),
                        _MANIFEST_FILE: (
                            "available" if manifest is not None else "missing"
                        ),
                    },
                    "dbt_version": _optional_text(metadata.get("dbt_version")),
                    "invocation_id": _optional_text(metadata.get("invocation_id")),
                    "failed_node_count": len(failed_results),
                    "evidence_count": len(evidences),
                }
            },
        }


def _dbt_artifact_declarations(
    incident: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Return the dbt artifacts explicitly associated with this incident."""
    artifacts = incident.get("artifacts", [])

    if not isinstance(artifacts, list):
        raise DbtArtifactError("incident field 'artifacts' must be an array")

    declarations: dict[str, Mapping[str, Any]] = {}
    accepted_types = frozenset(_ARTIFACT_TYPE_BY_FILE.values())

    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, Mapping):
            raise DbtArtifactError(
                f"incident artifact at index {index} must be an object"
            )

        artifact_type = artifact.get("type")

        if artifact_type not in accepted_types:
            continue

        if artifact_type in declarations:
            raise DbtArtifactError(
                f"incident declares duplicate artifact type {artifact_type!r}"
            )

        uri = artifact.get("uri")
        expected_hash = artifact.get("hash")

        if not isinstance(uri, str) or not uri.strip():
            raise DbtArtifactError(
                f"incident artifact {artifact_type!r} field 'uri' must be a non-empty string"
            )

        if not _is_sha256_reference(expected_hash):
            raise DbtArtifactError(
                f"incident artifact {artifact_type!r} field 'hash' must use sha256:<64 hex>"
            )

        declarations[artifact_type] = artifact

    return declarations


def _load_declared_json_object(
    path: Path,
    declaration: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Load only an incident-declared artifact whose content hash matches."""
    if declaration is None:
        return None

    try:
        raw_bytes = path.read_bytes()
    except OSError as error:
        raise DbtArtifactError(
            f"declared dbt artifact {path.name!r} is unavailable: {error}"
        ) from error

    expected_hash = cast(str, declaration["hash"]).lower()
    actual_hash = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"

    if actual_hash != expected_hash:
        raise DbtArtifactError(
            f"declared dbt artifact {path.name!r} does not match its incident hash"
        )

    try:
        raw_content = raw_bytes.decode("utf-8")
        document: object = json.loads(raw_content)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise DbtArtifactError(
            f"could not read dbt artifact {path.name!r}: {error}"
        ) from error

    if not isinstance(document, dict):
        raise DbtArtifactError(f"dbt artifact {path.name!r} must contain a JSON object")

    return cast(dict[str, Any], document)


def _is_sha256_reference(value: Any) -> bool:
    """Return whether a value is a canonical SHA-256 artifact reference."""
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False

    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in digest
    )


def _failed_results(
    run_results: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    """Return only results whose normalized status represents a failure."""
    if run_results is None:
        return []

    results = run_results.get("results", [])

    if not isinstance(results, list):
        raise DbtArtifactError(
            "dbt artifact 'run_results.json' field 'results' must be an array"
        )

    failed: list[Mapping[str, Any]] = []

    for index, result in enumerate(results):
        if not isinstance(result, Mapping):
            raise DbtArtifactError(
                f"dbt artifact 'run_results.json' result at index {index} must be an object"
            )

        status = _required_result_text(result, "status").strip().lower()

        if status in _FAILED_STATUSES:
            failed.append(result)

    return failed


def _manifest_nodes(
    manifest: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Return the manifest node mapping or an empty mapping when absent."""
    if manifest is None:
        return {}

    nodes = manifest.get("nodes", {})

    if not isinstance(nodes, Mapping):
        raise DbtArtifactError(
            "dbt artifact 'manifest.json' field 'nodes' must be an object"
        )

    return nodes


def _artifact_metadata(
    run_results: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Return run-results metadata when it is available."""
    if run_results is None:
        return {}

    metadata = run_results.get("metadata", {})

    if not isinstance(metadata, Mapping):
        raise DbtArtifactError(
            "dbt artifact 'run_results.json' field 'metadata' must be an object"
        )

    return metadata


def _find_manifest_node(
    nodes: Mapping[str, Any],
    unique_id: str,
) -> Mapping[str, Any]:
    """Find one manifest node without requiring the manifest to exist."""
    node = nodes.get(unique_id)

    if node is None:
        return {}

    if not isinstance(node, Mapping):
        raise DbtArtifactError(f"manifest node {unique_id!r} must be an object")

    return node


def _lineage_dependencies(
    manifest_node: Mapping[str, Any],
    manifest_nodes: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Resolve direct node dependencies declared by the dbt manifest."""
    depends_on = manifest_node.get("depends_on")

    if depends_on is None:
        return []

    if not isinstance(depends_on, Mapping):
        raise DbtArtifactError("manifest field 'depends_on' must be an object")

    dependency_ids = depends_on.get("nodes", [])

    if not isinstance(dependency_ids, list):
        raise DbtArtifactError("manifest field 'depends_on.nodes' must be an array")

    dependencies: list[dict[str, Any]] = []

    for index, dependency_id in enumerate(dependency_ids):
        if not isinstance(dependency_id, str) or not dependency_id.strip():
            raise DbtArtifactError(
                f"manifest dependency at index {index} must be a non-empty string"
            )

        dependency_node = _find_manifest_node(
            manifest_nodes,
            dependency_id,
        )

        dependencies.append(
            _lineage_node_reference(
                dependency_id,
                dependency_node,
            )
        )

    return dependencies


def _lineage_node_reference(
    unique_id: str,
    manifest_node: Mapping[str, Any],
) -> dict[str, Any]:
    """Create a compact reference to one directly related dbt node."""
    return {
        "unique_id": unique_id,
        "name": (
            _optional_text(manifest_node.get("name")) or _name_from_unique_id(unique_id)
        ),
        "resource_type": (
            _optional_text(manifest_node.get("resource_type"))
            or _resource_type_from_unique_id(unique_id)
        ),
        "original_file_path": _optional_text(manifest_node.get("original_file_path")),
        "relation_name": _optional_text(manifest_node.get("relation_name")),
    }


def _make_failure_evidence(
    *,
    incident_id: str,
    result: Mapping[str, Any],
    manifest_node: Mapping[str, Any],
    manifest_nodes: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one deterministic evidence item for a failed dbt node."""
    unique_id = _required_result_text(result, "unique_id")
    status = _required_result_text(result, "status").strip().lower()

    payload = {
        "adapter_response": result.get("adapter_response", {}),
        "compiled_code": _first_text(
            result.get("compiled_code"),
            manifest_node.get("compiled_code"),
            manifest_node.get("compiled_sql"),
        ),
        "execution_time": result.get("execution_time"),
        "failures": result.get("failures"),
        "lineage": {
            "depends_on": _lineage_dependencies(
                manifest_node,
                manifest_nodes,
            )
        },
        "message": _optional_text(result.get("message")),
        "name": (
            _optional_text(manifest_node.get("name")) or _name_from_unique_id(unique_id)
        ),
        "original_file_path": _optional_text(manifest_node.get("original_file_path")),
        "relation_name": _first_text(
            result.get("relation_name"),
            manifest_node.get("relation_name"),
        ),
        "resource_type": (
            _optional_text(manifest_node.get("resource_type"))
            or _resource_type_from_unique_id(unique_id)
        ),
        "status": status,
        "unique_id": unique_id,
    }

    content = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    node_digest = hashlib.sha256(unique_id.encode("utf-8")).hexdigest()

    return {
        "evidence_id": f"{incident_id}:dbt:{node_digest[:16]}",
        "origin": _ADAPTER_NAME,
        "type": "METADATA",
        "hash": f"sha256:{content_digest}",
        "retention": _RETENTION,
        "content": content,
    }


def _required_text(
    values: Mapping[str, Any],
    field: str,
) -> str:
    """Read a required non-empty string from an input mapping."""
    value = values.get(field)

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"incident field {field!r} must be a non-empty string")

    return value


def _required_result_text(
    result: Mapping[str, Any],
    field: str,
) -> str:
    """Read a required non-empty string from a dbt result."""
    value = result.get(field)

    if not isinstance(value, str) or not value.strip():
        raise DbtArtifactError(f"dbt result field {field!r} must be a non-empty string")

    return value


def _optional_text(value: Any) -> str | None:
    """Normalize an optional textual value."""
    if isinstance(value, str) and value:
        return value

    return None


def _first_text(*values: Any) -> str | None:
    """Return the first available non-empty string."""
    for value in values:
        normalized = _optional_text(value)

        if normalized is not None:
            return normalized

    return None


def _resource_type_from_unique_id(unique_id: str) -> str:
    """Infer the dbt resource type from its unique identifier."""
    return unique_id.split(".", maxsplit=1)[0]


def _name_from_unique_id(unique_id: str) -> str:
    """Provide a useful name when manifest metadata is unavailable."""
    parts = unique_id.split(".", maxsplit=2)

    if len(parts) == 3:
        return parts[2]

    return unique_id
