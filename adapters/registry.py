"""Closed registry for pipeline adapters.

Only trusted plugin code supplies adapter factories. A name coming from project
configuration is matched by dictionary lookup and is never converted into an
import path or filesystem path.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from types import MappingProxyType

AdapterFactory = Callable[..., object]

_ADAPTER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


class AdapterRegistryError(ValueError):
    """Raised when a configured adapter cannot be resolved safely."""

    def __init__(self, code: str, adapter_name: str, message: str) -> None:
        self.code = code
        self.adapter_name = adapter_name
        super().__init__(message)


class AdapterRegistry:
    """Resolve adapters from an immutable, explicitly registered mapping."""

    def __init__(self, adapters: Mapping[str, AdapterFactory]) -> None:
        registered: dict[str, AdapterFactory] = {}

        for name, factory in adapters.items():
            if _ADAPTER_NAME_PATTERN.fullmatch(name) is None:
                raise ValueError(
                    f"registered adapter name {name!r} must use lowercase kebab-case"
                )

            if not callable(factory):
                raise TypeError(
                    f"factory registered for adapter {name!r} must be callable"
                )

            registered[name] = factory

        self._adapters: Mapping[str, AdapterFactory] = MappingProxyType(registered)

    @property
    def registered_names(self) -> tuple[str, ...]:
        """Return registered adapter names in deterministic order."""
        return tuple(sorted(self._adapters))

    def is_registered(self, name: str) -> bool:
        """Return whether an adapter name was registered by trusted plugin code."""
        return name in self._adapters

    def resolve(
        self,
        name: str,
        *,
        allowed_names: Collection[str],
    ) -> AdapterFactory:
        """Resolve an installed adapter that is also explicitly allowlisted."""
        if isinstance(allowed_names, str | bytes):
            raise TypeError("allowed_names must be a collection of names, not a string")

        allowed = frozenset(allowed_names)

        if name not in allowed:
            raise AdapterRegistryError(
                "ADAPTER_NOT_ALLOWED",
                name,
                f"adapter {name!r} is not allowlisted; allowed adapters: {_format_names(allowed)}.",
            )

        factory = self._adapters.get(name)
        if factory is None:
            raise AdapterRegistryError(
                "ADAPTER_NOT_REGISTERED",
                name,
                f"adapter {name!r} is allowlisted but is not installed or registered; "
                f"registered adapters: {_format_names(self.registered_names)}.",
            )

        return factory


def _format_names(names: Collection[str]) -> str:
    """Format adapter names for actionable error messages."""
    return ", ".join(sorted(names)) or "(none)"


def build_default_registry() -> AdapterRegistry:
    """Build the registry from the adapter implementations shipped by the plugin."""
    from adapters.dbt import DbtAdapter
    from adapters.mock import MockAdapter

    return AdapterRegistry(
        {
            "dbt": DbtAdapter,
            "mock": MockAdapter,
        }
    )
