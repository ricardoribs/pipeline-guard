from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REGISTRY_PATH = Path(__file__).resolve().parents[3] / "adapters" / "registry.py"


def _load_registry_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "pipeline_guard_adapter_registry",
        REGISTRY_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load registry module from {REGISTRY_PATH}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_registry_module = _load_registry_module()
AdapterRegistry = _registry_module.AdapterRegistry
AdapterRegistryError = _registry_module.AdapterRegistryError


class MockAdapter:
    pass


class DbtAdapter:
    pass


@pytest.mark.parametrize(
    ("adapter_name", "factory"),
    [
        ("mock", MockAdapter),
        ("dbt", DbtAdapter),
    ],
)
def test_resolves_registered_and_allowlisted_adapter(adapter_name, factory):
    registry = AdapterRegistry({adapter_name: factory})

    resolved = registry.resolve(adapter_name, allowed_names={adapter_name})

    assert resolved is factory


def test_rejects_registered_adapter_outside_allowlist():
    registry = AdapterRegistry({"mock": MockAdapter})

    with pytest.raises(AdapterRegistryError, match="not allowlisted") as error:
        registry.resolve("mock", allowed_names={"dbt"})

    assert error.value.code == "ADAPTER_NOT_ALLOWED"
    assert error.value.adapter_name == "mock"


def test_rejects_allowlisted_but_unregistered_adapter():
    registry = AdapterRegistry({"mock": MockAdapter})

    with pytest.raises(
        AdapterRegistryError, match="not installed or registered"
    ) as error:
        registry.resolve("ghost", allowed_names={"ghost"})

    assert error.value.code == "ADAPTER_NOT_REGISTERED"
    assert error.value.adapter_name == "ghost"


def test_path_like_name_cannot_trigger_dynamic_import():
    registry = AdapterRegistry({"mock": MockAdapter})

    with pytest.raises(AdapterRegistryError) as error:
        registry.resolve("../../malicious", allowed_names={"../../malicious"})

    assert error.value.code == "ADAPTER_NOT_REGISTERED"


def test_registry_copies_mapping_at_construction():
    adapters = {"mock": MockAdapter}
    registry = AdapterRegistry(adapters)

    adapters["dbt"] = MockAdapter

    with pytest.raises(AdapterRegistryError) as error:
        registry.resolve("dbt", allowed_names={"dbt"})

    assert error.value.code == "ADAPTER_NOT_REGISTERED"


def test_rejects_unsafe_registered_name():
    with pytest.raises(ValueError, match="lowercase kebab-case"):
        AdapterRegistry({"../mock": MockAdapter})
