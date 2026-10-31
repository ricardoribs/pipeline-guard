#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "jsonschema>=4.0.0",
#   "pyyaml>=6.0.3",
# ]
# ///
"""Validate pipeline-guard configuration for a consumer project."""

from __future__ import annotations

import argparse
import difflib
import importlib
import json
import re
import sys
from collections.abc import Callable, Collection, Iterable
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError, best_match

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_PATH = _PLUGIN_ROOT / "config" / "schema" / "project-config.schema.json"
_REGISTRY_PATH = _PLUGIN_ROOT / "adapters" / "registry.py"
_EVIDENCE_REGISTRY_PATH = _PLUGIN_ROOT / "adapters" / "evidence" / "registry.py"

_CREDENTIAL_REFERENCE_PATTERN = re.compile(
    r"^(?:[A-Z][A-Z0-9_]*|env:[A-Z][A-Z0-9_]*|"
    r"\$\{[A-Z][A-Z0-9_]*\}|secret:[A-Za-z0-9][A-Za-z0-9._/-]*)$"
)
_PUSH_CREDENTIAL_REFERENCE_PATTERN = re.compile(
    r"^(?:env:[A-Z][A-Z0-9_]*|\$\{[A-Z][A-Z0-9_]*\}|secret:[A-Za-z0-9][A-Za-z0-9._/-]*)$"
)
_SERVICE_IDENTITY_PATTERN = re.compile(r"^sp:[a-z][a-z0-9-]*$")
_CREDENTIAL_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "client_secret",
        "connection_string",
        "credential",
        "credentials",
        "database_url",
        "dsn",
        "jdbc_url",
        "password",
        "private_key",
        "secret",
        "token",
        "webhook_url",
    }
)
_CREDENTIAL_FIELD_SUFFIXES = (
    "_access_key",
    "_api_key",
    "_apikey",
    "_connection_string",
    "_credential",
    "_credential_ref",
    "_database_url",
    "_dsn",
    "_env",
    "_jdbc_url",
    "_password",
    "_private_key",
    "_secret",
    "_secret_name",
    "_secret_ref",
    "_token",
    "_token_ref",
    "_webhook_url",
)


class _AdapterRegistryProtocol(Protocol):
    """Operations the configuration validator needs from AdapterRegistry."""

    @property
    def registered_names(self) -> tuple[str, ...]: ...

    def resolve(
        self,
        name: str,
        *,
        allowed_names: Collection[str],
    ) -> Callable[..., object]: ...


class _EvidenceProviderRegistryProtocol(Protocol):
    """Operations the configuration validator needs from evidence providers."""

    def validate_config(
        self, provider_type: str, config: Any
    ) -> list[ValidationError]: ...


class ConfigValidationError(ValueError):
    """Configuration error containing an actionable correction."""

    def __init__(
        self,
        code: str,
        file_path: Path,
        field: str,
        message: str,
        correction: str,
    ) -> None:
        self.code = code
        self.file_path = file_path
        self.field = field
        self.correction = correction
        super().__init__(
            f"{code}: {file_path}: field {field}: {message} Correction: {correction}"
        )


def _load_config(config_path: Path) -> dict[str, Any]:
    """Read a consumer configuration without applying a permissive fallback."""
    if not config_path.is_file():
        raise ConfigValidationError(
            "CONFIG_NOT_FOUND",
            config_path,
            "$",
            "configuration file was not found",
            "materialize a pipeline-guard preset and provide its file path",
        )

    try:
        content = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigValidationError(
            "CONFIG_UNREADABLE",
            config_path,
            "$",
            "configuration could not be read as UTF-8",
            "check the file permissions and encoding",
        ) from exc

    try:
        config = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ConfigValidationError(
            "CONFIG_INVALID_YAML",
            config_path,
            "$",
            "configuration contains invalid YAML syntax",
            "correct the YAML syntax and run validation again",
        ) from exc

    if not isinstance(config, dict):
        raise ConfigValidationError(
            "CONFIG_INVALID_ROOT",
            config_path,
            "$",
            "configuration root must be an object",
            "use YAML key-value fields at the document root",
        )

    return config


def _load_schema() -> dict[str, Any]:
    """Load and verify the plugin-owned project configuration schema."""
    try:
        content = _SCHEMA_PATH.read_text(encoding="utf-8")
        schema = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigValidationError(
            "CONFIG_SCHEMA_UNAVAILABLE",
            _SCHEMA_PATH,
            "$",
            "the project configuration schema could not be loaded",
            "restore or reinstall project-config.schema.json",
        ) from exc

    if not isinstance(schema, dict):
        raise ConfigValidationError(
            "CONFIG_SCHEMA_INVALID",
            _SCHEMA_PATH,
            "$",
            "the project configuration schema root is not an object",
            "replace it with a valid JSON Schema object",
        )

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ConfigValidationError(
            "CONFIG_SCHEMA_INVALID",
            _SCHEMA_PATH,
            "$",
            "project-config.schema.json is not a valid JSON Schema",
            "correct the plugin-owned schema before validating project configuration",
        ) from exc

    return schema


def _format_field_path(parts: Iterable[object]) -> str:
    """Format a JSON Schema path such as $.pipeline.adapter."""
    field = "$"

    for part in parts:
        if isinstance(part, int):
            field += f"[{part}]"
        else:
            field += f".{part}"

    return field


def _child_field(field: str, child: object) -> str:
    if isinstance(child, int):
        return f"{field}[{child}]"
    return f"{field}.{child}"


def _is_credential_field(field_name: str) -> bool:
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", field_name)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").lower()
    return normalized in _CREDENTIAL_FIELD_NAMES or normalized.endswith(
        _CREDENTIAL_FIELD_SUFFIXES
    )


def _has_url_userinfo(value: str) -> bool:
    """Return whether an absolute URL embeds a username or password."""
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value) is None:
        return False

    try:
        parsed = urlsplit(value)
        return parsed.username is not None or parsed.password is not None
    except ValueError:
        return False


def _validate_push_url(config_path: Path, config: dict[str, Any]) -> None:
    """Keep header-based remediation credentials on HTTPS transports."""
    repository = config.get("repository")
    if not isinstance(repository, dict) or not repository.get("push_credential_ref"):
        return
    url = repository.get("url")
    if not isinstance(url, str):
        return
    local_path = Path(url).is_absolute() and not url.startswith(("//", "\\\\"))
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        scheme = ""
    if scheme != "https" and not local_path:
        raise ConfigValidationError(
            "CONFIG_INSECURE_PUSH_URL",
            config_path,
            "$.repository.url",
            "header-based push credentials require an HTTPS repository URL",
            "use an HTTPS repository URL for remediation",
        )


def _validate_service_identity(config_path: Path, config: dict[str, Any]) -> None:
    """Reject known credential formats where a service identity label is required."""
    unattended = config.get("unattended")
    if not isinstance(unattended, dict):
        return

    service_identity = unattended.get("service_identity")
    if not isinstance(service_identity, str):
        return

    if _SERVICE_IDENTITY_PATTERN.fullmatch(service_identity) is None:
        raise ConfigValidationError(
            "CONFIG_INVALID_SERVICE_IDENTITY",
            config_path,
            "$.unattended.service_identity",
            "service_identity must use the sp:<name> namespace",
            'use a lowercase service identity such as "sp:pipeline-guard"',
        )


def _validate_credential_value(
    config_path: Path,
    value: Any,
    field: str,
) -> None:
    """Require every credential leaf to be an env or secret reference."""
    if isinstance(value, dict):
        for name, child in value.items():
            _validate_credential_value(
                config_path,
                child,
                _child_field(field, name),
            )
        return

    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_credential_value(
                config_path,
                child,
                _child_field(field, index),
            )
        return

    if field.endswith(("push_credential_ref", "authorization_secret_ref")):
        if isinstance(value, str) and _PUSH_CREDENTIAL_REFERENCE_PATTERN.fullmatch(
            value
        ):
            return
        raise ConfigValidationError(
            "CONFIG_LITERAL_CREDENTIAL",
            config_path,
            field,
            "credential reference must be an env:... or secret:... reference; "
            "literal values are forbidden",
            "use env:ENV_VAR, ${ENV_VAR}, or secret:<name>",
        )

    if isinstance(value, str) and _CREDENTIAL_REFERENCE_PATTERN.fullmatch(value):
        return

    raise ConfigValidationError(
        "CONFIG_LITERAL_CREDENTIAL",
        config_path,
        field,
        "credential must be a reference; literal values are forbidden",
        "use ENV_VAR, env:ENV_VAR, ${ENV_VAR}, or secret:<name>",
    )


def _validate_credential_references(
    config_path: Path,
    value: Any,
    field: str = "$",
) -> None:
    """Find credential-shaped fields recursively without reading secret values."""
    if isinstance(value, dict):
        for name, child in value.items():
            child_field = _child_field(field, name)
            if _is_credential_field(str(name)):
                _validate_credential_value(config_path, child, child_field)
            else:
                _validate_credential_references(config_path, child, child_field)
        return

    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_credential_references(
                config_path,
                child,
                _child_field(field, index),
            )
        return

    if isinstance(value, str) and _has_url_userinfo(value):
        raise ConfigValidationError(
            "CONFIG_LITERAL_CREDENTIAL",
            config_path,
            field,
            "URL userinfo is forbidden because it can contain literal credentials",
            "remove credentials from the URL and use an environment-variable or secret reference",
        )


def _load_registry_module() -> ModuleType:
    """Load only the plugin-owned registry module from its fixed path."""
    plugin_root = str(_PLUGIN_ROOT)
    if plugin_root not in sys.path:
        sys.path.insert(0, plugin_root)

    try:
        return importlib.import_module("adapters.registry")
    except ImportError as exc:
        raise ConfigValidationError(
            "CONFIG_REGISTRY_UNAVAILABLE",
            _REGISTRY_PATH,
            "$",
            "the Adapter Registry module could not be loaded",
            "restore or reinstall adapters/registry.py",
        ) from exc


def _build_config_registry() -> _AdapterRegistryProtocol:
    """Build the closed registry from the plugin's installed adapters."""
    module = _load_registry_module()
    registry_builder = getattr(module, "build_default_registry", None)
    if registry_builder is None:
        raise ConfigValidationError(
            "CONFIG_REGISTRY_UNAVAILABLE",
            _REGISTRY_PATH,
            "$",
            "the default Adapter Registry builder is missing",
            "restore or reinstall adapters/registry.py",
        )

    try:
        return cast(_AdapterRegistryProtocol, registry_builder())
    except (ImportError, AttributeError) as exc:
        raise ConfigValidationError(
            "CONFIG_REGISTRY_UNAVAILABLE",
            _REGISTRY_PATH,
            "$",
            "one or more registered adapters could not be imported",
            "restore or reinstall the adapter implementations",
        ) from exc


def _build_evidence_provider_registry() -> _EvidenceProviderRegistryProtocol:
    """Load the plugin-owned, closed evidence provider registry."""
    plugin_root = str(_PLUGIN_ROOT)
    if plugin_root not in sys.path:
        sys.path.insert(0, plugin_root)

    try:
        module = importlib.import_module("adapters.evidence.registry")
        registry = getattr(module, "EVIDENCE_PROVIDER_REGISTRY", None)
    except ImportError as exc:
        raise ConfigValidationError(
            "CONFIG_EVIDENCE_REGISTRY_UNAVAILABLE",
            _EVIDENCE_REGISTRY_PATH,
            "$.evidence_providers",
            "the Evidence Provider Registry module could not be loaded",
            "restore or reinstall adapters/evidence/registry.py",
        ) from exc

    if registry is None:
        raise ConfigValidationError(
            "CONFIG_EVIDENCE_REGISTRY_UNAVAILABLE",
            _EVIDENCE_REGISTRY_PATH,
            "$.evidence_providers",
            "the default Evidence Provider Registry is missing",
            "restore or reinstall the evidence provider registry",
        )

    return cast(_EvidenceProviderRegistryProtocol, registry)


def _validate_evidence_provider_configs(
    config_path: Path,
    config: dict[str, Any],
    registry: _EvidenceProviderRegistryProtocol,
) -> None:
    """Validate each declared provider against its registered configuration schema."""
    providers = config.get("evidence_providers", [])
    for index, provider in enumerate(providers):
        provider_type = provider["type"]
        provider_config = provider.get("config", {})
        try:
            errors = registry.validate_config(provider_type, provider_config)
        except ValueError as exc:
            raise ConfigValidationError(
                "CONFIG_EVIDENCE_PROVIDER_REJECTED",
                config_path,
                f"$.evidence_providers[{index}].type",
                f"evidence provider '{provider_type}' is not registered "
                "with a configuration schema",
                "use an installed evidence provider with a registered configuration schema",
            ) from exc

        if errors:
            error = best_match(errors)
            if error is None:
                raise AssertionError("jsonschema returned errors without a best match")
            raise _schema_validation_error(
                config_path,
                error,
                field_prefix=("evidence_providers", index, "config"),
            )


def _schema_validation_error(
    config_path: Path,
    error: ValidationError,
    *,
    field_prefix: Iterable[object] = (),
) -> ConfigValidationError:
    """Convert jsonschema errors into safe and actionable configuration errors."""
    field = _format_field_path((*field_prefix, *error.absolute_path))

    if error.validator == "required":
        instance = error.instance if isinstance(error.instance, dict) else {}
        required = (
            error.validator_value if isinstance(error.validator_value, list) else []
        )
        missing = next(
            (str(name) for name in required if name not in instance),
            "<unknown>",
        )
        return ConfigValidationError(
            "CONFIG_REQUIRED_FIELD",
            config_path,
            _child_field(field, missing),
            f"required field '{missing}' is missing",
            f"add the required field '{missing}'",
        )

    if error.validator == "additionalProperties":
        instance = error.instance if isinstance(error.instance, dict) else {}
        error_schema = error.schema if isinstance(error.schema, dict) else {}
        properties = error_schema.get("properties", {})
        allowed = set(properties) if isinstance(properties, dict) else set()
        present = {str(name) for name in instance}
        unknown = sorted(present - allowed)
        unknown_field = unknown[0] if unknown else "<unknown>"
        suggestions = difflib.get_close_matches(unknown_field, sorted(allowed), n=1)

        if suggestions:
            correction = (
                f"replace '{unknown_field}' with '{suggestions[0]}' or remove the field"
            )
        else:
            allowed_text = ", ".join(sorted(allowed)) or "(none)"
            correction = (
                f"remove '{unknown_field}'; allowed fields here are: {allowed_text}"
            )

        return ConfigValidationError(
            "CONFIG_UNKNOWN_FIELD",
            config_path,
            _child_field(field, unknown_field),
            f"unknown field '{unknown_field}' is not allowed",
            correction,
        )

    if error.validator == "const":
        expected = json.dumps(error.validator_value, ensure_ascii=False)
        return ConfigValidationError(
            "CONFIG_INVALID_VALUE",
            config_path,
            field,
            "field has a value that is not permitted by the schema",
            f"set the field to {expected}",
        )

    if error.validator == "type":
        return ConfigValidationError(
            "CONFIG_INVALID_TYPE",
            config_path,
            field,
            "field has an invalid data type",
            f"use a value of type {error.validator_value}",
        )

    if error.validator == "minLength":
        return ConfigValidationError(
            "CONFIG_EMPTY_FIELD",
            config_path,
            field,
            "field must not be empty",
            "provide a non-empty value",
        )

    if error.validator == "minimum":
        return ConfigValidationError(
            "CONFIG_VALUE_TOO_SMALL",
            config_path,
            field,
            "numeric value is below the permitted minimum",
            f"use a value greater than or equal to {error.validator_value}",
        )

    if error.validator == "uniqueItems":
        return ConfigValidationError(
            "CONFIG_DUPLICATE_ITEMS",
            config_path,
            field,
            "array contains duplicate items",
            "remove duplicate entries",
        )

    return ConfigValidationError(
        "CONFIG_SCHEMA_VIOLATION",
        config_path,
        field,
        f"field violates the JSON Schema rule '{error.validator}'",
        (
            "adjust the field to match the registered provider configuration schema"
            if field_prefix
            else "adjust the field to match project-config.schema.json"
        ),
    )


def _validate_against_schema(
    config_path: Path,
    config: dict[str, Any],
) -> None:
    """Validate configuration and report the most relevant schema error."""
    validator = Draft202012Validator(_load_schema())
    errors = list(validator.iter_errors(config))

    if not errors:
        return

    error = best_match(errors)
    if error is None:
        raise AssertionError("jsonschema returned errors without a best match")

    raise _schema_validation_error(config_path, error)


def validate_config(
    config_path: Path,
    *,
    registry: _AdapterRegistryProtocol | None = None,
    evidence_provider_registry: _EvidenceProviderRegistryProtocol | None = None,
) -> dict[str, Any]:
    """Load and validate one consumer project configuration."""
    config = _load_config(config_path)
    _validate_credential_references(config_path, config)
    _validate_service_identity(config_path, config)
    _validate_against_schema(config_path, config)
    _validate_push_url(config_path, config)

    active_registry = registry or _build_config_registry()
    adapter_name = config["pipeline"]["adapter"]

    try:
        active_registry.resolve(
            adapter_name,
            allowed_names=active_registry.registered_names,
        )
    except ValueError as exc:
        allowed = ", ".join(active_registry.registered_names) or "(none)"
        raise ConfigValidationError(
            "CONFIG_ADAPTER_REJECTED",
            config_path,
            "$.pipeline.adapter",
            f"adapter '{adapter_name}' is not registered and allowlisted",
            f"use one of the installed adapters: {allowed}",
        ) from exc

    active_evidence_registry = (
        evidence_provider_registry
        if evidence_provider_registry is not None
        else _build_evidence_provider_registry()
    )
    _validate_evidence_provider_configs(config_path, config, active_evidence_registry)

    return config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a pipeline-guard consumer project configuration.",
    )
    parser.add_argument(
        "config",
        type=Path,
        help="path to the materialized YAML configuration file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run configuration validation as a standalone command."""
    args = _build_parser().parse_args(argv)
    config_path: Path = args.config

    try:
        validate_config(config_path)
    except ConfigValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Configuration is valid: {config_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
