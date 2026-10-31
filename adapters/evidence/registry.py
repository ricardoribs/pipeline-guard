"""Closed registry for evidence provider implementations and configuration schemas."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource

from .base import EvidenceProvider

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_CREDENTIAL_SCHEMA_ID = (
    "urn:pipeline-guard:credential-reference.schema.json"
)
_CREDENTIAL_PROPERTY = re.compile(
    r"(?:credential|secret|token|password|passwd|pwd|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|client[_-]?secret)",
    re.IGNORECASE,
)
_CREDENTIAL_SCHEMA_PATH = _PLUGIN_ROOT / "config" / "schema" / "credential-reference.schema.json"


def _load_schema_registry() -> Registry:
    """Build a local-only JSON Schema registry for shared definitions."""
    try:
        credential_schema = json.loads(_CREDENTIAL_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceProviderRegistryError(
            f"Cannot load shared credential reference schema {_CREDENTIAL_SCHEMA_PATH}: {exc}"
        ) from exc
    Draft202012Validator.check_schema(credential_schema)
    schema_id = credential_schema.get("$id")
    if schema_id != _CREDENTIAL_SCHEMA_ID:
        raise EvidenceProviderRegistryError(
            f"Credential reference schema must use the stable ID {_CREDENTIAL_SCHEMA_ID!r}."
        )
    return Registry().with_resource(schema_id, Resource.from_contents(credential_schema))


class EvidenceProviderRegistryError(ValueError):
    """Raised for an unknown provider or an invalid registry entry."""


@dataclass(frozen=True, slots=True)
class EvidenceProviderRegistration:
    """Bind one installed provider implementation to its strict config schema."""

    implementation: type[EvidenceProvider]
    config_schema: Mapping[str, Any] | None


class EvidenceProviderRegistry:
    """Resolve only explicitly registered evidence provider types."""

    def __init__(self, entries: Mapping[str, EvidenceProviderRegistration]) -> None:
        validated_entries: dict[str, EvidenceProviderRegistration] = {}
        for provider_type, registration in entries.items():
            if not provider_type or not provider_type.strip():
                raise EvidenceProviderRegistryError("Provider type names must not be empty.")
            if not isinstance(registration, EvidenceProviderRegistration):
                raise EvidenceProviderRegistryError(
                    f"Provider {provider_type!r} must have an implementation and schema entry."
                )
            if not isinstance(registration.implementation, type):
                raise EvidenceProviderRegistryError(
                    f"Provider {provider_type!r} has no implementation class."
                )
            missing_interface = [
                member
                for member in ("name", "capabilities", "collect")
                if not hasattr(registration.implementation, member)
            ]
            if missing_interface:
                raise EvidenceProviderRegistryError(
                    f"Provider {provider_type!r} implementation does not satisfy "
                    f"EvidenceProvider; missing: {', '.join(missing_interface)}."
                )
            if registration.config_schema is None:
                raise EvidenceProviderRegistryError(
                    f"Provider {provider_type!r} has no configuration schema."
                )

            schema = dict(registration.config_schema)
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                raise EvidenceProviderRegistryError(
                    f"Provider {provider_type!r} has an invalid configuration schema: {exc}"
                ) from exc
            self._require_closed_object_schemas(provider_type, schema)
            self._require_credential_reference_schemas(provider_type, schema)
            validated_entries[provider_type] = EvidenceProviderRegistration(
                implementation=registration.implementation,
                config_schema=copy.deepcopy(schema),
            )

        self._entries = MappingProxyType(validated_entries)
        self._schema_registry = _load_schema_registry()

    @staticmethod
    def _require_closed_object_schemas(provider_type: str, schema: Mapping[str, Any]) -> None:
        """Require every declared object shape to reject undeclared properties."""
        pending: list[Any] = [schema]
        while pending:
            current = pending.pop()
            if isinstance(current, dict):
                if (current.get("type") == "object" or "properties" in current) and current.get(
                    "additionalProperties"
                ) is not False:
                    raise EvidenceProviderRegistryError(
                        f"Provider {provider_type!r} schema must set "
                        "additionalProperties: false on every object."
                    )
                pending.extend(current.values())
            elif isinstance(current, list):
                pending.extend(current)

    @staticmethod
    def _require_credential_reference_schemas(provider_type: str, schema: dict[str, Any]) -> None:
        """Require named or annotated credential fields to use the shared schema."""

        def resolves_to_credential_schema(candidate: Any, seen: set[str]) -> bool:
            if not isinstance(candidate, dict):
                return False
            reference = candidate.get("$ref")
            if not isinstance(reference, str):
                return False
            if reference == _CREDENTIAL_SCHEMA_ID:
                return True
            if not reference.startswith("#/") or reference in seen:
                return False

            resolved: Any = schema
            for part in reference[2:].split("/"):
                key = unquote(part.replace("~1", "/").replace("~0", "~"))
                if not isinstance(resolved, dict) or key not in resolved:
                    return False
                resolved = resolved[key]
            return resolves_to_credential_schema(resolved, {*seen, reference})

        pending: list[Any] = [schema]
        while pending:
            current = pending.pop()
            if isinstance(current, dict):
                properties = current.get("properties", {})
                if isinstance(properties, dict):
                    for property_name, property_schema in properties.items():
                        is_credential = bool(_CREDENTIAL_PROPERTY.search(property_name))
                        is_annotated = (
                            isinstance(property_schema, dict)
                            and property_schema.get("x-credential-reference") is True
                        )
                        if (is_credential or is_annotated) and not resolves_to_credential_schema(
                            property_schema, set()
                        ):
                            raise EvidenceProviderRegistryError(
                                f"Provider {provider_type!r} credential field {property_name!r} "
                                "must reference the shared credential-reference schema."
                            )
                pending.extend(current.values())
            elif isinstance(current, list):
                pending.extend(current)

    def get(self, provider_type: str) -> EvidenceProviderRegistration:
        """Return a registration or fail closed for an unknown provider type."""
        try:
            registration = self._entries[provider_type]
        except KeyError as exc:
            raise EvidenceProviderRegistryError(
                f"Evidence provider {provider_type!r} is not registered."
            ) from exc
        return EvidenceProviderRegistration(
            implementation=registration.implementation,
            config_schema=copy.deepcopy(registration.config_schema),
        )

    def validate_config(
        self,
        provider_type: str,
        config: Any,
    ) -> list[ValidationError]:
        """Validate provider configuration using its registered schema and local references."""
        registration = self.get(provider_type)
        if registration.config_schema is None:
            raise EvidenceProviderRegistryError(
                f"Evidence provider {provider_type!r} has no configuration schema."
            )
        validator = Draft202012Validator(
            dict(registration.config_schema),
            registry=self._schema_registry,
        )
        return list(validator.iter_errors(config))

    @property
    def types(self) -> frozenset[str]:
        """Names that are installed and allowed by this registry."""
        return frozenset(self._entries)


# No concrete evidence provider is shipped yet. Adding one requires registering both its
# implementation and its config schema here; an empty registry accepts only evidence_providers: [].
EVIDENCE_PROVIDER_REGISTRY = EvidenceProviderRegistry({})
