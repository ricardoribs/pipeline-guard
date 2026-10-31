from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR_PATH = PLUGIN_ROOT / "scripts" / "validate_config.py"
PRESETS_DIR = PLUGIN_ROOT / "config" / "presets"
PROJECTS_DIR = PLUGIN_ROOT / "config" / "projects"

sys.path.insert(0, str(PLUGIN_ROOT))
from adapters.evidence.base import EvidenceCapability, EvidencePayload
from adapters.evidence.registry import (
    EVIDENCE_PROVIDER_REGISTRY,
    EvidenceProviderRegistration,
    EvidenceProviderRegistry,
    EvidenceProviderRegistryError,
)

sys.path.remove(str(PLUGIN_ROOT))


def _load_validator_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "pipeline_guard_config_validator",
        VALIDATOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load config validator from {VALIDATOR_PATH}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_validator_module = _load_validator_module()
ConfigValidationError = _validator_module.ConfigValidationError
validate_config = _validator_module.validate_config


@pytest.mark.parametrize(
    ("preset_name", "expected_adapter"),
    [
        ("generic.yaml", "mock"),
        ("dbt.yaml", "dbt"),
    ],
)
def test_materialized_preset_is_valid(
    preset_name: str,
    expected_adapter: str,
) -> None:
    preset_path = PRESETS_DIR / preset_name
    original_content = preset_path.read_bytes()

    config = validate_config(preset_path)

    assert config["schema_version"] == "1.0"
    assert config["pipeline"]["adapter"] == expected_adapter
    assert preset_path.read_bytes() == original_content


def test_second_consumer_project_configuration_is_valid() -> None:
    config_path = PROJECTS_DIR / "second-project.yaml"
    original_content = config_path.read_bytes()

    config = validate_config(config_path)

    assert config["project"]["id"] == "pipeline-guard-second-project"
    assert config["pipeline"]["adapter"] == "mock"
    assert config["runtime"]["execution_budget"] == {
        "max_tool_calls": 10,
        "max_seconds": 60,
        "max_tokens": 16384,
    }
    assert config_path.read_bytes() == original_content


def test_missing_required_adapter_reports_actionable_error(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "missing-adapter.yaml"
    config_text = """\
schema_version: "1.0"
pipeline:
  root: "."
runtime:
  execution_budget:
    max_tool_calls: 1
    max_seconds: 1
    max_tokens: 1
"""
    config_path.write_text(config_text, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as captured:
        validate_config(config_path)

    error = captured.value

    assert error.code == "CONFIG_REQUIRED_FIELD"
    assert error.file_path == config_path
    assert error.field == "$.pipeline.adapter"
    assert error.correction == "add the required field 'adapter'"
    assert config_path.read_text(encoding="utf-8") == config_text


def test_unknown_field_reports_suggested_correction(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "unknown-field.yaml"
    config_text = """\
schema_version: "1.0"
pipeline:
  adapter: "mock"
  rot: "."
runtime:
  execution_budget:
    max_tool_calls: 1
    max_seconds: 1
    max_tokens: 1
"""
    config_path.write_text(config_text, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as captured:
        validate_config(config_path)

    error = captured.value

    assert error.code == "CONFIG_UNKNOWN_FIELD"
    assert error.file_path == config_path
    assert error.field == "$.pipeline.rot"
    assert error.correction == "replace 'rot' with 'root' or remove the field"
    assert config_path.read_text(encoding="utf-8") == config_text


def test_project_without_config_is_rejected(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "pipeline-guard.yaml"

    assert not config_path.exists()

    with pytest.raises(ConfigValidationError) as captured:
        validate_config(config_path)

    error = captured.value

    assert error.code == "CONFIG_NOT_FOUND"
    assert error.file_path == config_path
    assert error.field == "$"
    assert (
        error.correction
        == "materialize a pipeline-guard preset and provide its file path"
    )
    assert not config_path.exists()


def test_unregistered_adapter_is_rejected(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "unknown-adapter.yaml"
    config_text = """\
schema_version: "1.0"
pipeline:
  adapter: "airflow"
runtime:
  execution_budget:
    max_tool_calls: 1
    max_seconds: 1
    max_tokens: 1
"""
    config_path.write_text(config_text, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as captured:
        validate_config(config_path)

    error = captured.value

    assert error.code == "CONFIG_ADAPTER_REJECTED"
    assert error.file_path == config_path
    assert error.field == "$.pipeline.adapter"
    assert error.correction == "use one of the installed adapters: dbt, mock"
    assert config_path.read_text(encoding="utf-8") == config_text


def test_literal_credential_is_rejected_without_leaking_value(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "literal-credential.yaml"
    literal_credential = "literal-super-secret-123"
    config_text = f"""\
schema_version: "1.0"
pipeline:
  adapter: "mock"
evidence_providers:
  - name: "logs"
    type: "api"
    config:
      credentials:
        token: "{literal_credential}"
"""
    config_path.write_text(config_text, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as captured:
        validate_config(config_path)

    error = captured.value
    error_text = str(error)

    assert error.code == "CONFIG_LITERAL_CREDENTIAL"
    assert error.file_path == config_path
    assert error.field == "$.evidence_providers[0].config.credentials.token"
    assert error.correction == "use ENV_VAR, env:ENV_VAR, ${ENV_VAR}, or secret:<name>"
    assert literal_credential not in error_text
    assert config_path.read_text(encoding="utf-8") == config_text


def test_camel_case_literal_credential_is_rejected_without_leaking_value(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "camel-case-literal-credential.yaml"
    literal_credential = "literal-client-secret-123"
    config_text = f"""\
schema_version: "1.0"
pipeline:
  adapter: "mock"
evidence_providers:
  - name: "metadata"
    type: "api"
    config:
      clientSecret: "{literal_credential}"
"""
    config_path.write_text(config_text, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as captured:
        validate_config(config_path)

    error = captured.value
    error_text = str(error)

    assert error.code == "CONFIG_LITERAL_CREDENTIAL"
    assert error.file_path == config_path
    assert error.field == "$.evidence_providers[0].config.clientSecret"
    assert error.correction == "use ENV_VAR, env:ENV_VAR, ${ENV_VAR}, or secret:<name>"
    assert literal_credential not in error_text
    assert config_path.read_text(encoding="utf-8") == config_text


def test_repository_url_with_literal_userinfo_is_rejected_without_leaking_value(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "repository-url-with-userinfo.yaml"
    literal_credential = "literal-password"
    config_text = f"""\
schema_version: "1.0"
pipeline:
  adapter: "mock"
repository:
  url: "https://user:{literal_credential}@example.com/repository.git"
"""
    config_path.write_text(config_text, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as captured:
        validate_config(config_path)

    error = captured.value
    error_text = str(error)

    assert error.code == "CONFIG_LITERAL_CREDENTIAL"
    assert error.file_path == config_path
    assert error.field == "$.repository.url"
    assert literal_credential not in error_text
    assert config_path.read_text(encoding="utf-8") == config_text


def test_literal_connection_string_is_rejected_without_leaking_value(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "literal-connection-string.yaml"
    literal_credential = "literal-password"
    config_text = f"""\
schema_version: "1.0"
pipeline:
  adapter: "mock"
evidence_providers:
  - name: "warehouse"
    type: "database"
    config:
      connectionString: "postgresql://user:{literal_credential}@host/db"
"""
    config_path.write_text(config_text, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as captured:
        validate_config(config_path)

    error = captured.value
    error_text = str(error)

    assert error.code == "CONFIG_LITERAL_CREDENTIAL"
    assert error.file_path == config_path
    assert error.field == "$.evidence_providers[0].config.connectionString"
    assert literal_credential not in error_text
    assert config_path.read_text(encoding="utf-8") == config_text


def test_repository_url_without_userinfo_is_allowed(tmp_path: Path) -> None:
    config_path = tmp_path / "repository-url-without-userinfo.yaml"
    config_text = """\
schema_version: "1.0"
pipeline:
  adapter: "mock"
repository:
  url: "https://example.com/repository.git"
evidence_providers:
  - name: "warehouse"
    type: "database"
    config:
      connectionString: "env:DATABASE_URL"
runtime:
  execution_budget:
    max_tool_calls: 1
    max_seconds: 1
    max_tokens: 1
"""
    config_path.write_text(config_text, encoding="utf-8")

    config = validate_config(
        config_path,
        evidence_provider_registry=_evidence_registry(
            "database",
            _fixture_provider_schema(),
        ),
    )

    assert config["repository"]["url"] == "https://example.com/repository.git"
    assert (
        config["evidence_providers"][0]["config"]["connectionString"]
        == "env:DATABASE_URL"
    )
    assert config_path.read_text(encoding="utf-8") == config_text


def test_http_repository_with_push_credential_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "insecure-push-url.yaml"
    config_path.write_text(
        """\
schema_version: "1.0"
pipeline:
  adapter: "mock"
repository:
  url: "http://example.com/org/repo.git"
  push_credential_ref: "env:PUSH_TOKEN"
runtime:
  execution_budget:
    max_tool_calls: 1
    max_seconds: 1
    max_tokens: 1
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="HTTPS repository URL") as captured:
        validate_config(config_path)

    assert captured.value.field == "$.repository.url"
    assert captured.value.code == "CONFIG_INSECURE_PUSH_URL"


def test_config_registry_uses_installed_adapter_factories() -> None:
    registry = _validator_module._build_config_registry()

    mock_factory = registry.resolve("mock", allowed_names=registry.registered_names)
    dbt_factory = registry.resolve("dbt", allowed_names=registry.registered_names)

    assert mock_factory.__name__ == "MockAdapter"
    assert mock_factory.__module__ == "adapters.mock.adapter"
    assert dbt_factory.__name__ == "DbtAdapter"
    assert dbt_factory.__module__ == "adapters.dbt.adapter"


def test_config_registry_rejects_unimportable_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_module = _validator_module._load_registry_module()

    def unavailable_registry() -> None:
        raise ImportError("simulated missing adapter")

    monkeypatch.setattr(
        registry_module,
        "build_default_registry",
        unavailable_registry,
    )

    with pytest.raises(ConfigValidationError) as captured:
        _validator_module._build_config_registry()

    error = captured.value

    assert error.code == "CONFIG_REGISTRY_UNAVAILABLE"
    assert error.field == "$"
    assert error.correction == "restore or reinstall the adapter implementations"
    assert "simulated missing adapter" not in str(error)


class _FixtureProvider:
    name = "fixture"
    capabilities: frozenset[EvidenceCapability] = frozenset()

    def collect(
        self,
        capability: EvidenceCapability,
        request: Mapping[str, object],
    ) -> Iterable[EvidencePayload]:
        _ = capability, request
        return ()


_CREDENTIAL_REFERENCE_SCHEMA_ID = "urn:pipeline-guard:credential-reference.schema.json"


def _evidence_registry(
    provider_type: str = "fixture",
    config_schema: dict[str, Any] | None = None,
) -> EvidenceProviderRegistry:
    return EvidenceProviderRegistry(
        {
            provider_type: EvidenceProviderRegistration(
                implementation=_FixtureProvider,
                config_schema=config_schema,
            )
        }
    )


def _fixture_provider_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "$defs": {
            "credential_reference": {"$ref": _CREDENTIAL_REFERENCE_SCHEMA_ID},
        },
        "properties": {
            "token": {"$ref": "#/$defs/credential_reference"},
            "auth": {
                "$ref": "#/$defs/credential_reference",
                "x-credential-reference": True,
            },
            "connectionString": {"$ref": "#/$defs/credential_reference"},
            "connection_string": {"$ref": "#/$defs/credential_reference"},
            "options": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"region": {"type": "string"}},
            },
        },
    }


def _provider_config(
    tmp_path: Path,
    *,
    provider_type: str = "fixture",
    provider_config: dict[str, Any] | None = None,
    providers: list[dict[str, Any]] | None = None,
) -> Path:
    if providers is None:
        providers = [
            {
                "name": "fixture-provider",
                "type": provider_type,
                "config": provider_config or {},
            }
        ]
    path = tmp_path / "pipeline-guard.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "pipeline": {"adapter": "mock", "root": "."},
                "evidence_providers": providers,
                "runtime": {
                    "execution_budget": {
                        "max_tool_calls": 1,
                        "max_seconds": 1,
                        "max_tokens": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_provider_credential_rejects_literal_value(tmp_path: Path) -> None:
    config_path = _provider_config(
        tmp_path, provider_config={"token": "literal-secret-value"}
    )

    with pytest.raises(ConfigValidationError) as captured:
        validate_config(
            config_path,
            evidence_provider_registry=_evidence_registry(
                config_schema=_fixture_provider_schema()
            ),
        )

    assert captured.value.code == "CONFIG_LITERAL_CREDENTIAL"
    assert captured.value.field == "$.evidence_providers[0].config.token"
    assert "literal-secret-value" not in str(captured.value)


@pytest.mark.parametrize(
    "reference",
    ["MY_TOKEN", "env:MY_TOKEN", "${MY_TOKEN}", "secret:team/provider-token"],
)
def test_provider_credential_accepts_reference_forms(
    tmp_path: Path, reference: str
) -> None:
    config_path = _provider_config(tmp_path, provider_config={"token": reference})

    config = validate_config(
        config_path,
        evidence_provider_registry=_evidence_registry(
            config_schema=_fixture_provider_schema()
        ),
    )

    assert config["evidence_providers"][0]["config"]["token"] == reference


@pytest.mark.parametrize("literal", ["hunter2", "literalSecret"])
def test_annotated_provider_credential_rejects_literal_values(
    tmp_path: Path, literal: str
) -> None:
    config_path = _provider_config(tmp_path, provider_config={"auth": literal})

    with pytest.raises(ConfigValidationError):
        validate_config(
            config_path,
            evidence_provider_registry=_evidence_registry(
                config_schema=_fixture_provider_schema()
            ),
        )


def test_unknown_provider_is_rejected_without_fallback(tmp_path: Path) -> None:
    config_path = _provider_config(tmp_path, provider_type="unknown")

    with pytest.raises(ConfigValidationError) as captured:
        validate_config(
            config_path, evidence_provider_registry=EvidenceProviderRegistry({})
        )

    assert captured.value.code == "CONFIG_EVIDENCE_PROVIDER_REJECTED"
    assert captured.value.field == "$.evidence_providers[0].type"


def test_registry_rejects_provider_without_schema() -> None:
    with pytest.raises(EvidenceProviderRegistryError, match="no configuration schema"):
        _evidence_registry(config_schema=None)


def test_registry_rejects_provider_schema_with_open_nested_object() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "options": {"type": "object", "additionalProperties": True},
        },
    }

    with pytest.raises(
        EvidenceProviderRegistryError, match="additionalProperties: false"
    ):
        _evidence_registry(config_schema=schema)


def test_registry_requires_credential_fields_to_reference_shared_schema() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"token": {"type": "string"}},
    }

    with pytest.raises(
        EvidenceProviderRegistryError, match="must reference the shared"
    ):
        _evidence_registry(config_schema=schema)


def test_main_schema_is_validated_before_provider_registry_lookup(
    tmp_path: Path,
) -> None:
    config_path = _provider_config(tmp_path, provider_type="unknown")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    del config["pipeline"]["adapter"]
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(ConfigValidationError) as captured:
        validate_config(
            config_path, evidence_provider_registry=EvidenceProviderRegistry({})
        )

    assert captured.value.code == "CONFIG_REQUIRED_FIELD"
    assert captured.value.field == "$.pipeline.adapter"


def test_provider_schema_rejects_unknown_nested_configuration_keys(
    tmp_path: Path,
) -> None:
    config_path = _provider_config(
        tmp_path,
        provider_config={"options": {"region": "us-east-1", "extra": "value"}},
    )

    with pytest.raises(ConfigValidationError) as captured:
        validate_config(
            config_path,
            evidence_provider_registry=_evidence_registry(
                config_schema=_fixture_provider_schema()
            ),
        )

    assert captured.value.code == "CONFIG_UNKNOWN_FIELD"
    assert captured.value.field == "$.evidence_providers[0].config.options.extra"


def test_empty_evidence_provider_list_is_valid(tmp_path: Path) -> None:
    config_path = _provider_config(tmp_path, providers=[])

    config = validate_config(
        config_path, evidence_provider_registry=EVIDENCE_PROVIDER_REGISTRY
    )

    assert config["evidence_providers"] == []


def test_connection_string_accepts_a_credential_reference(tmp_path: Path) -> None:
    config_path = _provider_config(
        tmp_path,
        provider_type="database",
        provider_config={"connectionString": "env:DATABASE_URL"},
    )

    config = validate_config(
        config_path,
        evidence_provider_registry=_evidence_registry(
            "database",
            _fixture_provider_schema(),
        ),
    )

    assert (
        config["evidence_providers"][0]["config"]["connectionString"]
        == "env:DATABASE_URL"
    )


@pytest.mark.parametrize(
    "ref",
    [
        "env:PIPELINE_GUARD_GIT_PUSH_TOKEN",
        "${PIPELINE_GUARD_GIT_PUSH_TOKEN}",
        "secret:pipeline-guard/git-push-token",
    ],
)
def test_push_credential_ref_accepts_valid_references(tmp_path: Path, ref: str) -> None:
    config_path = tmp_path / "push-cred-valid.yaml"
    config_path.write_text(
        f"""schema_version: "1.0"
pipeline:
  adapter: "mock"
repository:
  url: "https://github.com/org/repo.git"
  default_branch: "main"
  remediation_branch_prefix: "pipeline-guard"
  push_credential_ref: "{ref}"
  allowed_git_operations:
    - "push_incident_branch"
runtime:
  execution_budget:
    max_tool_calls: 1
    max_seconds: 1
    max_tokens: 1
""",
        encoding="utf-8",
    )
    config = validate_config(config_path)
    assert config["repository"]["push_credential_ref"] == ref
    assert config["repository"]["allowed_git_operations"] == ["push_incident_branch"]


@pytest.mark.parametrize(
    "invalid_ref",
    [
        "ghp_literal_personal_access_token",
        "PIPELINE_GUARD_GIT_PUSH_TOKEN",
    ],
)
def test_push_credential_ref_rejects_literal_or_bare_name(
    tmp_path: Path, invalid_ref: str
) -> None:
    config_path = tmp_path / "push-cred-invalid.yaml"
    config_path.write_text(
        f"""schema_version: "1.0"
pipeline:
  adapter: "mock"
repository:
  url: "https://github.com/org/repo.git"
  default_branch: "main"
  remediation_branch_prefix: "pipeline-guard"
  push_credential_ref: "{invalid_ref}"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(config_path)
    assert "push_credential_ref" in str(exc_info.value)
    assert invalid_ref not in str(exc_info.value)


def _unattended_config(unattended: str) -> str:
    return f"""schema_version: "1.0"
pipeline:
  adapter: "mock"
runtime:
  execution_budget:
    max_tool_calls: 1
    max_seconds: 1
    max_tokens: 1
{unattended}"""


def test_config_without_unattended_section_is_valid(tmp_path: Path) -> None:
    config_path = tmp_path / "without-unattended.yaml"
    config_path.write_text(_unattended_config(""), encoding="utf-8")

    config = validate_config(config_path)

    assert "unattended" not in config


def test_disabled_unattended_configuration_is_valid(tmp_path: Path) -> None:
    config_path = tmp_path / "unattended-disabled.yaml"
    config_path.write_text(
        _unattended_config(
            """unattended:
  remediation_enabled: false
  service_identity: "sp:pipeline-guard"
"""
        ),
        encoding="utf-8",
    )

    config = validate_config(config_path)

    assert config["unattended"]["remediation_enabled"] is False


@pytest.mark.parametrize(
    "invalid_identity",
    [
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
        "ATBBabcdefghijklmnopqrstuvwxyz0123456789",
        "glpat-abcdefghijklmnopqrstuvwxyz0123456789",
        "xoxb-abcdefghijklmnopqrstuvwxyz0123456789",
        "AIzaabcdefghijklmnopqrstuvwxyz0123456789",
        "ordinary-user",
        "sp:ghp_abcdefghijklmnopqrstuvwxyz1234567890",
    ],
)
def test_unattended_service_identity_requires_service_namespace(
    tmp_path: Path,
    invalid_identity: str,
) -> None:
    config_path = tmp_path / "unattended-token-shaped-service-identity.yaml"
    config_path.write_text(
        _unattended_config(
            f"""unattended:
  remediation_enabled: false
  service_identity: "{invalid_identity}"
"""
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError) as captured:
        validate_config(config_path)

    error = captured.value
    assert error.code == "CONFIG_INVALID_SERVICE_IDENTITY"
    assert error.file_path == config_path
    assert error.field == "$.unattended.service_identity"
    assert (
        error.correction
        == 'use a lowercase service identity such as "sp:pipeline-guard"'
    )
    assert invalid_identity not in str(error)


def test_enabled_unattended_configuration_requires_secret_and_model_limit(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "unattended-enabled.yaml"
    config_path.write_text(
        _unattended_config(
            """unattended:
  remediation_enabled: true
  service_identity: "sp:pipeline-guard"
  authorization_secret_ref: "env:PIPELINE_GUARD_POLICY_SECRET"
  max_model_tokens: 200000
  authorization_max_age_seconds: 900
  target_branch: null
  max_seconds: 3600
"""
        ),
        encoding="utf-8",
    )

    config = validate_config(config_path)

    assert config["unattended"]["max_model_tokens"] == 200000


@pytest.mark.parametrize(
    "missing_field", ["authorization_secret_ref", "max_model_tokens"]
)
def test_enabled_unattended_configuration_reports_missing_required_field(
    tmp_path: Path,
    missing_field: str,
) -> None:
    unattended_fields = {
        "authorization_secret_ref": (
            '  authorization_secret_ref: "env:PIPELINE_GUARD_POLICY_SECRET"\n'
        ),
        "max_model_tokens": "  max_model_tokens: 200000\n",
    }
    del unattended_fields[missing_field]
    config_path = tmp_path / f"unattended-missing-{missing_field}.yaml"
    config_path.write_text(
        _unattended_config(
            "unattended:\n"
            "  remediation_enabled: true\n"
            '  service_identity: "sp:pipeline-guard"\n'
            + "".join(unattended_fields.values())
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError) as captured:
        validate_config(config_path)

    error = captured.value
    assert error.code == "CONFIG_REQUIRED_FIELD"
    assert error.file_path == config_path
    assert error.field == f"$.unattended.{missing_field}"
    assert error.correction == f"add the required field '{missing_field}'"


def test_unattended_secret_literal_is_rejected_without_leaking_value(
    tmp_path: Path,
) -> None:
    literal_secret = "literal-policy-secret-value"
    config_path = tmp_path / "unattended-literal-secret.yaml"
    config_path.write_text(
        _unattended_config(
            f"""unattended:
  remediation_enabled: true
  service_identity: "sp:pipeline-guard"
  authorization_secret_ref: "{literal_secret}"
  max_model_tokens: 200000
"""
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError) as captured:
        validate_config(config_path)

    error = captured.value
    assert error.code == "CONFIG_LITERAL_CREDENTIAL"
    assert error.file_path == config_path
    assert error.field == "$.unattended.authorization_secret_ref"
    assert error.correction == "use env:ENV_VAR, ${ENV_VAR}, or secret:<name>"
    assert literal_secret not in str(error)


def test_allowed_git_operations_rejects_unauthorized_operations(tmp_path: Path) -> None:
    config_path = tmp_path / "push-ops-invalid.yaml"
    config_path.write_text(
        """schema_version: "1.0"
pipeline:
  adapter: "mock"
repository:
  url: "https://github.com/org/repo.git"
  default_branch: "main"
  remediation_branch_prefix: "pipeline-guard"
  allowed_git_operations:
    - "merge"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(config_path)
    assert "allowed_git_operations" in str(exc_info.value)
